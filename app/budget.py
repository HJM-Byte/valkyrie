"""每日 token 预算：跨重启、跨入口的硬上限。

学习（research）和学校（school）都会调模型。旧代码里没有累计账本，
一个进程重启就能重新领一份额度。这里用 SQLite 两张表做原子预留：

    budget_usage        每个 (day, domain) 的 committed / reserved
    budget_reservations 每一笔预留的完整生命周期

关键语义：
- `reserve()` 在 `BEGIN IMMEDIATE` 里完成「检查 + 扣预留 + 写 reservation」，
  两个进程不能同时读到「未超限」再各记一笔。
- `settle()` 把预留转成实际用量，不是再加一笔；同一 id 重复结算幂等。
- 调用前失败用 `release()`；调用后失败按实际/预留结算，不释放。
- 崩溃留下的过期 reservation 由 `recover_stale()` 保守结算，不释放。
- `record()` 只能带 reservation_id；没有 id 返回 `unreserved_record`。

PID 锁只作为「同域第二个常驻入口」提示，不参与额度正确性。
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from . import config as C

try:  # Python 3.9+
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore


_SCHEMA = """
CREATE TABLE IF NOT EXISTS budget_usage(
  day TEXT NOT NULL,
  domain TEXT NOT NULL,
  committed INTEGER NOT NULL DEFAULT 0,
  reserved INTEGER NOT NULL DEFAULT 0,
  updated_at REAL NOT NULL,
  PRIMARY KEY(day, domain)
);
CREATE TABLE IF NOT EXISTS budget_reservations(
  reservation_id TEXT PRIMARY KEY,
  day TEXT NOT NULL,
  domain TEXT NOT NULL,
  reserved INTEGER NOT NULL,
  actual INTEGER,
  status TEXT NOT NULL CHECK(status IN ('reserved','settled','released','stale')),
  estimated INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL,
  expires_at REAL NOT NULL,
  settled_at REAL,
  note TEXT
);
CREATE INDEX IF NOT EXISTS idx_budget_res_status
  ON budget_reservations(status, expires_at);
"""


def _tz():
    name = (os.environ.get("ROBOT_BUDGET_TZ") or C.BUDGET_TZ or "Asia/Shanghai").strip()
    if ZoneInfo is not None:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    # Windows 没有 tzdata 时退回固定 +8；中国无夏令时，够用。
    return timezone(timedelta(hours=8), name="Asia/Shanghai")


def day_key(ts: Optional[float] = None) -> str:
    """按 Asia/Shanghai 的 00:00 切日。"""
    t = time.time() if ts is None else float(ts)
    return datetime.fromtimestamp(t, tz=_tz()).strftime("%Y-%m-%d")


def _to_ts(value) -> float:
    if value is None:
        return time.time()
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=_tz()).timestamp()
        return value.timestamp()
    if isinstance(value, str):
        s = value.strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return float(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_tz())
        return dt.timestamp()
    return float(value)


class Budget:
    def __init__(self, path: Path | str = None, *, limits: Optional[dict] = None,
                 ttl_seconds: int = 900) -> None:
        self.path = Path(path or getattr(C, "BUDGET_DB", C.DATA / "budget.db"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.limits = {
            "research": int(getattr(C, "RESEARCH_DAILY_TOKENS", 120000)),
            "school": int(getattr(C, "SCHOOL_DAILY_TOKENS", 240000)),
        }
        if limits:
            self.limits.update({str(k): int(v) for k, v in limits.items()})
        self.ttl_seconds = int(ttl_seconds)
        self._schema_lock = threading.Lock()
        self._init_schema()

    # ---- 连接与建表 -------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(str(self.path), timeout=5.0, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=5000")
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        return db

    def _init_schema(self) -> None:
        with self._schema_lock:
            db = self._connect()
            try:
                db.executescript(_SCHEMA)
            finally:
                db.close()

    def close(self) -> None:
        """兼容 with/清理：连接都是短连接，这里只保留接口。"""
        return None

    def limit_of(self, domain: str) -> Optional[int]:
        return self.limits.get(str(domain))

    def _get_usage(self, db: sqlite3.Connection, day: str, domain: str) -> sqlite3.Row:
        row = db.execute(
            "SELECT * FROM budget_usage WHERE day=? AND domain=?", (day, domain)
        ).fetchone()
        if row is not None:
            return row
        db.execute(
            "INSERT INTO budget_usage(day,domain,committed,reserved,updated_at) "
            "VALUES(?,?,0,0,?)", (day, domain, time.time()))
        return db.execute(
            "SELECT * FROM budget_usage WHERE day=? AND domain=?", (day, domain)
        ).fetchone()

    # ---- 预留 -------------------------------------------------------------
    def reserve(self, domain: str, estimate_tokens: int, *,
                now=None, limit: Optional[int] = None) -> dict:
        domain = str(domain or "")
        try:
            estimate = int(estimate_tokens)
        except (TypeError, ValueError):
            return {"ok": False, "reason": "invalid_estimate"}
        if estimate <= 0:
            return {"ok": False, "reason": "invalid_estimate"}
        limit = self.limit_of(domain) if limit is None else int(limit)
        if limit is None:
            # chat 等不纳入硬上限的域：不建 reservation，也不记假账。
            return {"ok": True, "unlimited": True, "reservation_id": None,
                    "domain": domain, "estimate": estimate, "limit": None}
        ts = _to_ts(now)
        day = day_key(ts)
        rid = "br_" + uuid.uuid4().hex
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            self._recover_stale_conn(db, ts)
            usage = self._get_usage(db, day, domain)
            used = int(usage["committed"]) + int(usage["reserved"])
            if used + estimate > limit:
                db.execute("ROLLBACK")
                return {"ok": False, "reason": "budget_exhausted", "domain": domain,
                        "day": day, "limit": limit, "used": used,
                        "remaining": max(0, limit - used), "estimate": estimate}
            db.execute(
                "UPDATE budget_usage SET reserved=reserved+?, updated_at=? "
                "WHERE day=? AND domain=?", (estimate, ts, day, domain))
            db.execute(
                "INSERT INTO budget_reservations(reservation_id,day,domain,reserved,"
                "actual,status,estimated,created_at,expires_at,settled_at,note) "
                "VALUES(?,?,?,?,NULL,'reserved',0,?,?,NULL,NULL)",
                (rid, day, domain, estimate, ts, ts + self.ttl_seconds))
            db.execute("COMMIT")
            return {"ok": True, "reservation_id": rid, "domain": domain,
                    "day": day, "reserved": estimate, "limit": limit,
                    "remaining_after_reserve": max(0, limit - used - estimate)}
        except sqlite3.OperationalError as exc:
            try:
                db.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            msg = str(exc).lower()
            reason = "budget_locked" if "locked" in msg or "busy" in msg else "budget_unavailable"
            return {"ok": False, "reason": reason, "domain": domain, "detail": str(exc)}
        except sqlite3.Error as exc:
            try:
                db.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            return {"ok": False, "reason": "budget_unavailable",
                    "domain": domain, "detail": str(exc)}
        finally:
            db.close()

    # ---- 结算 / 释放 ------------------------------------------------------
    def settle(self, reservation_id: str, actual_tokens: int, *,
               estimated: bool = False, note: str = "", now=None) -> dict:
        rid = str(reservation_id or "")
        if not rid:
            return {"ok": False, "reason": "missing_reservation_id"}
        try:
            actual = max(0, int(actual_tokens))
        except (TypeError, ValueError):
            return {"ok": False, "reason": "invalid_actual"}
        ts = _to_ts(now)
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM budget_reservations WHERE reservation_id=?", (rid,)
            ).fetchone()
            if row is None:
                db.execute("ROLLBACK")
                return {"ok": False, "reason": "unknown_reservation"}
            if row["status"] == "settled":
                db.execute("ROLLBACK")
                return {"ok": True, "idempotent": True, "reservation_id": rid,
                        "committed": int(row["actual"] or 0)}
            if row["status"] != "reserved":
                db.execute("ROLLBACK")
                return {"ok": False, "reason": "not_reserved",
                        "status": row["status"]}
            reserved = int(row["reserved"])
            day, domain = row["day"], row["domain"]
            self._get_usage(db, day, domain)
            db.execute(
                "UPDATE budget_usage SET reserved=max(0,reserved-?), "
                "committed=committed+?, updated_at=? WHERE day=? AND domain=?",
                (reserved, actual, ts, day, domain))
            db.execute(
                "UPDATE budget_reservations SET status='settled', actual=?, "
                "settled_at=?, estimated=?, note=? WHERE reservation_id=?",
                (actual, ts, 1 if estimated else 0, (note or "")[:200], rid))
            db.execute("COMMIT")
            return {"ok": True, "reservation_id": rid, "committed": actual,
                    "reserved": reserved, "overage": max(0, actual - reserved),
                    "estimated": bool(estimated)}
        except sqlite3.Error as exc:
            try:
                db.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            return {"ok": False, "reason": "budget_unavailable", "detail": str(exc)}
        finally:
            db.close()

    def release(self, reservation_id: str, *, note: str = "", now=None) -> dict:
        rid = str(reservation_id or "")
        if not rid:
            return {"ok": False, "reason": "missing_reservation_id"}
        ts = _to_ts(now)
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM budget_reservations WHERE reservation_id=?", (rid,)
            ).fetchone()
            if row is None:
                db.execute("ROLLBACK")
                return {"ok": False, "reason": "unknown_reservation"}
            if row["status"] == "released":
                db.execute("ROLLBACK")
                return {"ok": True, "idempotent": True, "reservation_id": rid}
            if row["status"] != "reserved":
                db.execute("ROLLBACK")
                return {"ok": False, "reason": "not_reserved",
                        "status": row["status"]}
            reserved = int(row["reserved"])
            day, domain = row["day"], row["domain"]
            self._get_usage(db, day, domain)
            db.execute(
                "UPDATE budget_usage SET reserved=max(0,reserved-?), updated_at=? "
                "WHERE day=? AND domain=?", (reserved, ts, day, domain))
            db.execute(
                "UPDATE budget_reservations SET status='released', settled_at=?, "
                "note=? WHERE reservation_id=?",
                (ts, (note or "released_before_call")[:200], rid))
            db.execute("COMMIT")
            return {"ok": True, "reservation_id": rid, "released": reserved}
        except sqlite3.Error as exc:
            try:
                db.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            return {"ok": False, "reason": "budget_unavailable", "detail": str(exc)}
        finally:
            db.close()

    def record(self, reservation_id: str, actual_tokens: int, **kwargs) -> dict:
        """兼容入口：必须带 reservation_id，否则拒绝直接加账。"""
        if not str(reservation_id or "").strip():
            return {"ok": False, "reason": "unreserved_record"}
        return self.settle(reservation_id, actual_tokens, **kwargs)

    # ---- 陈旧预留 ---------------------------------------------------------
    def _recover_stale_conn(self, db: sqlite3.Connection, ts: float) -> int:
        rows = db.execute(
            "SELECT * FROM budget_reservations WHERE status='reserved' AND expires_at<?",
            (ts,)).fetchall()
        n = 0
        for row in rows:
            reserved = int(row["reserved"])
            day, domain = row["day"], row["domain"]
            self._get_usage(db, day, domain)
            db.execute(
                "UPDATE budget_usage SET reserved=max(0,reserved-?), "
                "committed=committed+?, updated_at=? WHERE day=? AND domain=?",
                (reserved, reserved, ts, day, domain))
            db.execute(
                "UPDATE budget_reservations SET status='stale', actual=?, "
                "settled_at=?, estimated=1, note='stale_recovered' "
                "WHERE reservation_id=?", (reserved, ts, row["reservation_id"]))
            n += 1
        return n

    def recover_stale(self, now=None) -> dict:
        ts = _to_ts(now)
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            n = self._recover_stale_conn(db, ts)
            db.execute("COMMIT")
            return {"ok": True, "recovered": n}
        except sqlite3.Error as exc:
            try:
                db.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            return {"ok": False, "reason": "budget_unavailable", "detail": str(exc)}
        finally:
            db.close()

    # ---- 读 ---------------------------------------------------------------
    def usage(self, domain: str, *, now=None) -> dict:
        day = day_key(_to_ts(now))
        db = self._connect()
        try:
            row = db.execute(
                "SELECT * FROM budget_usage WHERE day=? AND domain=?", (day, domain)
            ).fetchone()
        finally:
            db.close()
        committed = int(row["committed"]) if row else 0
        reserved = int(row["reserved"]) if row else 0
        limit = self.limit_of(domain)
        return {"day": day, "domain": domain, "committed": committed,
                "reserved": reserved, "limit": limit,
                "remaining": (max(0, limit - committed - reserved)
                              if limit is not None else None)}

    def remaining(self, domain: str, *, now=None) -> Optional[int]:
        return self.usage(domain, now=now)["remaining"]

    def snapshot(self, *, now=None) -> dict:
        day = day_key(_to_ts(now))
        db = self._connect()
        try:
            rows = [dict(r) for r in db.execute(
                "SELECT * FROM budget_usage WHERE day=? ORDER BY domain", (day,))]
            res = [dict(r) for r in db.execute(
                "SELECT * FROM budget_reservations WHERE day=? ORDER BY created_at",
                (day,))]
        finally:
            db.close()
        return {"day": day, "usage": rows, "reservations": res}


_DEFAULT: Optional[Budget] = None
_LOCK = threading.Lock()


def default_budget() -> Budget:
    global _DEFAULT
    with _LOCK:
        if _DEFAULT is None:
            _DEFAULT = Budget()
        return _DEFAULT


def reset_default(path: Optional[Path | str] = None) -> None:
    global _DEFAULT
    with _LOCK:
        _DEFAULT = Budget(path) if path else None
