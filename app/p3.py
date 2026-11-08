"""P3 最小闭环：关系气候、未竟承诺、正式沉默。

这一层不写事实，也不改九变量和人格。它只回答三个问题：

1. 关系气候：最近相处是偏暖、偏冷、有张力，还是正在修复？
   中速变化，需要多次事件和反证，单次互动不改变它。
2. 未竟承诺：有没有一件已经答应、需要跟进或应该结束的事？
   只有她明确说出口、对方接受或约定的事项才进入 active；候选不反复提醒。
3. 正式沉默：这一次为什么决定不主动开口？
   沉默是一条可审计的决定，不是“没有生成消息”；同一意图多端共用。

存储独立在 `data/p3.db`，不碰 `memory.db` 的事实表。所有来源都带原话或
turn_id；没有来源就不写。数据库不存在时，只读摘要返回空，不自动建库。
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

from . import config as C

P3_DB = C.DATA / "p3.db"
VERSION = 1

CLIMATE_SIGNALS = ("warm", "cold", "repair", "neutral")
PROMISE_STATES = ("candidate", "active", "paused", "done", "cancelled", "expired")
CLIMATE_TAU_S = 14 * 86400.0
CLIMATE_MIN_EVENTS = 3
CLIMATE_MIN_DAYS = 2
QUIET_WINDOW_S = 2 * 3600.0
SILENCE_DEDUPE_S = 600.0
PROMISE_FOLLOWUP_GRACE_S = 3 * 86400.0

_ACCEPT_RE = re.compile(
    r"^(好|好的|好呀|行|可以|没问题|嗯|嗯嗯)"
    r"(?:[，,。.!！?？、\s]|$)")
_CANCEL_RE = re.compile(r"不用了|不用啦|算了|取消|别提醒|不需要|先不|不去了")
_PAUSE_RE = re.compile(r"先别|等会|等一会儿|回头再说|现在不方便|晚点|改天")
_QUIET_RE = re.compile(r"想静静|想安静|让我安静|别烦我|不想聊|先别问|安静一会")
_DONE_REPLY_RE = re.compile(
    r"已经(?:查|做|弄|问|整理|发)|完成了|查到了|弄好了|发你了|整理好了|问过了|查好了")

_PROMISE_PATTERNS = (
    re.compile(
        r"我(?:明天|今晚|待会儿|待会|回头|稍后|这周|等会|马上)"
        r"[^。！？!?\n]{0,30}(?:帮你|给你|去|查|提醒你|看看|整理|写|发|做)"),
    re.compile(
        r"我(?:帮你|给你|提醒你|去查|去问|去整理|去写)"
        r"[^。！？!?\n]{0,35}(?:明天|今晚|待会儿|待会|回头|稍后|这周|等会)"),
    re.compile(
        r"我(?:明天|今晚|待会儿|待会|回头|稍后|这周)"
        r"[^。！？!?\n]{0,20}(?:提醒你|帮你)"),
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS climate_events(
    id TEXT PRIMARY KEY,
    ts REAL NOT NULL,
    signal TEXT NOT NULL CHECK(signal IN ('warm','cold','repair','neutral')),
    weight REAL NOT NULL DEFAULT 1.0,
    source_kind TEXT NOT NULL DEFAULT 'turn',
    source_ref TEXT DEFAULT '',
    source_text TEXT DEFAULT '',
    turn_id INTEGER DEFAULT 0,
    note TEXT DEFAULT '',
    legacy_unknown INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_climate_ts ON climate_events(ts DESC);

CREATE TABLE IF NOT EXISTS promises(
    id TEXT PRIMARY KEY,
    text TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'candidate'
        CHECK(state IN ('candidate','active','paused','done','cancelled','expired')),
    due_at REAL NOT NULL DEFAULT 0,
    condition TEXT DEFAULT '',
    source_turn_id INTEGER DEFAULT 0,
    source_text TEXT DEFAULT '',
    accepted_text TEXT DEFAULT '',
    completion_evidence TEXT DEFAULT '',
    follow_up_at REAL DEFAULT 0,
    last_follow_up REAL DEFAULT 0,
    follow_up_count INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    note TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_promise_state ON promises(state, updated_at DESC);
CREATE INDEX IF NOT EXISTS ix_promise_due ON promises(state, due_at);

CREATE TABLE IF NOT EXISTS promise_events(
    id TEXT PRIMARY KEY,
    promise_id TEXT NOT NULL,
    from_state TEXT DEFAULT '',
    to_state TEXT NOT NULL,
    action TEXT NOT NULL,
    source_text TEXT DEFAULT '',
    evidence TEXT DEFAULT '',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_promise_event
    ON promise_events(promise_id, created_at DESC);

CREATE TABLE IF NOT EXISTS silence_decisions(
    id TEXT PRIMARY KEY,
    ts REAL NOT NULL,
    until_ts REAL NOT NULL DEFAULT 0,
    reason_code TEXT NOT NULL,
    reason TEXT DEFAULT '',
    channel TEXT NOT NULL DEFAULT 'any',
    intent_key TEXT DEFAULT '',
    dedupe TEXT DEFAULT '',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_silence_ts ON silence_decisions(ts DESC);
CREATE INDEX IF NOT EXISTS ix_silence_dedupe ON silence_decisions(dedupe);
"""


def _now() -> float:
    return time.time()


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, float(x)))


def _norm(text: str) -> str:
    return " ".join(str(text or "").split()).strip()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _day(ts: float) -> str:
    return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d")


def _promise_due(text: str, now: float) -> float:
    if "明天" in text:
        return now + 86400.0
    if "今晚" in text:
        return now + 6 * 3600.0
    if "这周" in text:
        return now + 3 * 86400.0
    if any(x in text for x in ("待会", "待会儿", "回头", "稍后", "等会")):
        return now + 2 * 3600.0
    return 0.0


def detect_promise(text: str, now: Optional[float] = None) -> dict:
    """从她那句话里找“明确答应的小事”。

    不认“我会一直陪着你”这类没有动作、没有条件的承诺；它不能被跟进，
    也不该变成情感义务。只认带具体动作/时间的少量事项。
    """
    t = _norm(text)
    if not t:
        return {"ok": False, "reason": "empty"}
    at = _now() if now is None else float(now)
    for pat in _PROMISE_PATTERNS:
        m = pat.search(t)
        if not m:
            continue
        phrase = t[max(0, m.start() - 12):m.end() + 24].strip()
        return {"ok": True, "text": t[:220], "condition": phrase[:180],
                "due_at": _promise_due(t, at)}
    return {"ok": False, "reason": "no_explicit_promise"}


def _public_promise(row: dict) -> dict:
    return {
        "id": str(row.get("id") or ""),
        "text": str(row.get("text") or "")[:220],
        "state": str(row.get("state") or "candidate"),
        "due_at": float(row.get("due_at") or 0.0),
        "condition": str(row.get("condition") or "")[:180],
        "source_turn_id": int(row.get("source_turn_id") or 0),
        "source_text": str(row.get("source_text") or "")[:220],
        "accepted_text": str(row.get("accepted_text") or "")[:220],
        "completion_evidence": str(row.get("completion_evidence") or "")[:220],
        "follow_up_count": int(row.get("follow_up_count") or 0),
        "last_follow_up": float(row.get("last_follow_up") or 0.0),
        "created_at": float(row.get("created_at") or 0.0),
        "updated_at": float(row.get("updated_at") or 0.0),
    }


def _climate_from_rows(rows: Iterable[dict], now: float) -> dict:
    """从事件算中速气候；纯函数，读库和测试共用。"""
    ev = [dict(r) for r in rows]
    sums = {s: 0.0 for s in ("warm", "cold", "repair", "neutral")}
    counts = {s: 0 for s in sums}
    days: set[str] = set()
    usable: list[dict] = []
    for r in ev:
        try:
            ts = float(r.get("ts") or 0.0)
        except (TypeError, ValueError):
            continue
        if ts <= 0:
            continue
        age = max(0.0, float(now) - ts)
        decay = math.exp(-age / CLIMATE_TAU_S)
        w = max(0.0, float(r.get("weight") or 1.0)) * decay
        signal = str(r.get("signal") or "neutral")
        if signal not in sums:
            signal = "neutral"
        sums[signal] += w
        counts[signal] += 1
        days.add(_day(ts))
        usable.append({"ts": ts, "signal": signal, "weight": round(w, 4),
                       "text": str(r.get("source_text") or "")[:180],
                       "source_kind": str(r.get("source_kind") or ""),
                       "turn_id": int(r.get("turn_id") or 0)})
    usable.sort(key=lambda x: x["ts"], reverse=True)
    count = len(usable)
    base = {
        "label": "还看不出来",
        "confidence": 0.15,
        "uncertainty": 0.85,
        "evidence_count": count,
        "day_count": len(days),
        "weight": {k: round(v, 3) for k, v in sums.items()},
        "counts": dict(counts),
        "evidence": usable[:5],
        "counter_evidence": [],
        "last_ts": usable[0]["ts"] if usable else 0.0,
        "as_of": float(now),
        "read_only": True,
        "changes_relationship_variables": False,
        "note": "关系气候需要多次事件和反证；单次互动不改变它。",
    }
    if count < CLIMATE_MIN_EVENTS or len(days) < CLIMATE_MIN_DAYS:
        base["note"] = (f"目前只有 {count} 件、跨 {len(days)} 天；"
                        "还不足以形成中速关系气候。")
        return base

    warm, cold = sums["warm"], sums["cold"]
    repair = sums["repair"]
    if warm > 0 and cold > 0 and repair > 0 and abs(warm - cold) <= max(warm, cold) * 0.6:
        label = "正在修复"
    elif warm > cold * 1.35 and warm > 0:
        label = "偏暖"
    elif cold > warm * 1.35 and cold > 0:
        label = "偏冷"
    elif warm > 0 and cold > 0:
        label = "有张力"
    else:
        label = "中性"

    counter_signal = "cold" if label in ("偏暖", "正在修复") else "warm"
    counter = [x for x in usable if x["signal"] == counter_signal][:3]
    total = warm + cold + repair
    conf = 0.30 + min(0.50, total / 10.0 * 0.50)
    if count >= 6 and len(days) >= 3:
        conf += 0.05
    conf = _clamp(conf, 0.25, 0.86)
    lines = []
    if label == "偏暖":
        lines.append("最近相处偏暖；但这不是永久信任，也不等于她可以越界。")
    elif label == "偏冷":
        lines.append("最近相处偏冷；先给空间、处理具体事，不追问关系意义。")
    elif label == "有张力":
        lines.append("最近既有暖意也有冷意；不把单次冲突升级成关系结论。")
    elif label == "正在修复":
        lines.append("最近有过冲突，也有修复动作；修复是否有效仍看后续行为。")
    else:
        lines.append("最近没有足够强的方向，保持中性。")
    if counter:
        lines.append(f"有 {len(counter)} 件反向证据，没有被忽略。")
    prompt_line = (
        f"最近相处：{label}（{count} 件证据、跨 {len(days)} 天，"
        f"{len(counter)} 件反证）。这只是中速判断，不是关系结论。"
    )
    return {
        "label": label,
        "confidence": round(conf, 3),
        "uncertainty": round(1.0 - conf, 3),
        "evidence_count": count,
        "day_count": len(days),
        "weight": {k: round(v, 3) for k, v in sums.items()},
        "counts": dict(counts),
        "evidence": usable[:5],
        "counter_evidence": counter,
        "last_ts": usable[0]["ts"],
        "as_of": float(now),
        "read_only": True,
        "changes_relationship_variables": False,
        "note": " ".join(lines),
        "prompt_line": prompt_line,
    }


def _readonly_connect(path: Path) -> Optional[sqlite3.Connection]:
    p = Path(path)
    if not p.exists():
        return None
    try:
        uri = p.resolve().as_uri() + "?mode=ro"
        db = sqlite3.connect(uri, uri=True, timeout=5)
        db.row_factory = sqlite3.Row
        return db
    except Exception:
        return None


def read_summary(path: Path | str = P3_DB, now: Optional[float] = None) -> dict:
    """只读摘要；库不存在时返回空，不创建文件。"""
    at = _now() if now is None else float(now)
    empty = {
        "ok": True, "available": False,
        "climate": _climate_from_rows([], at),
        "promises": [], "candidate_count": 0,
        "silence": [], "stats": {"promises": 0, "active": 0,
                                 "silence": 0, "climate_events": 0},
    }
    db = _readonly_connect(Path(path))
    if db is None:
        return empty
    try:
        tables = {r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "climate_events" not in tables:
            return empty
        rows = [dict(r) for r in db.execute(
            "SELECT * FROM climate_events ORDER BY ts DESC LIMIT 500").fetchall()]
        climate = _climate_from_rows(rows, at)
        promises = []
        if "promises" in tables:
            promises = [dict(r) for r in db.execute(
                "SELECT * FROM promises WHERE state='active' "
                "ORDER BY updated_at DESC, id LIMIT 8").fetchall()]
        stats = {"promises": 0, "active": 0, "silence": 0,
                 "climate_events": len(rows)}
        if "promises" in tables:
            stats["promises"] = int(db.execute(
                "SELECT COUNT(*) c FROM promises").fetchone()[0] or 0)
            stats["active"] = int(db.execute(
                "SELECT COUNT(*) c FROM promises WHERE state='active'").fetchone()[0] or 0)
        silence = []
        if "silence_decisions" in tables:
            stats["silence"] = int(db.execute(
                "SELECT COUNT(*) c FROM silence_decisions").fetchone()[0] or 0)
            silence = [dict(r) for r in db.execute(
                "SELECT * FROM silence_decisions ORDER BY ts DESC LIMIT 8").fetchall()]
        return {"ok": True, "available": True, "climate": climate,
                "promises": [_public_promise(x) for x in promises],
                "candidate_count": int(db.execute(
                    "SELECT COUNT(*) c FROM promises WHERE state='candidate'"
                ).fetchone()[0] or 0) if "promises" in tables else 0,
                "silence": silence, "stats": stats}
    finally:
        db.close()


class P3Store:
    def __init__(self, path: Path | str = P3_DB) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, timeout=15)
        self._db.row_factory = sqlite3.Row
        try:
            self._db.execute("PRAGMA journal_mode=WAL")
        except Exception:
            pass
        self._db.executescript(_SCHEMA)
        self._db.execute("PRAGMA user_version=1")
        self._db.commit()

    def close(self) -> None:
        try:
            self._db.close()
        except Exception:
            pass

    # ---- 关系气候 ---------------------------------------------------------
    def add_climate_event(self, signal: str, *, source_text: str = "",
                          source_kind: str = "turn", source_ref: str = "",
                          turn_id: int = 0, weight: float = 1.0,
                          ts: Optional[float] = None, note: str = "") -> dict:
        signal = str(signal or "").strip()
        if signal not in CLIMATE_SIGNALS:
            raise ValueError(f"非法关系气候信号: {signal}")
        now = _now() if ts is None else float(ts)
        cid = _new_id("cl")
        self._db.execute(
            "INSERT INTO climate_events(id,ts,signal,weight,source_kind,source_ref,"
            "source_text,turn_id,note,legacy_unknown,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (cid, now, signal, max(0.1, min(3.0, float(weight))),
             str(source_kind or "turn")[:30], str(source_ref or "")[:120],
             _norm(source_text)[:300], int(turn_id or 0),
             str(note or "")[:200], 0, _now()))
        self._db.commit()
        return {"ok": True, "id": cid, "signal": signal, "ts": now}

    def climate(self, now: Optional[float] = None) -> dict:
        at = _now() if now is None else float(now)
        rows = [dict(r) for r in self._db.execute(
            "SELECT * FROM climate_events WHERE ts >= ? ORDER BY ts DESC",
            (at - 30 * 86400.0,)).fetchall()]
        return _climate_from_rows(rows, at)

    # ---- 承诺 -------------------------------------------------------------
    def promises(self, state: str = "", limit: int = 50) -> list[dict]:
        lim = max(1, min(100, int(limit)))
        if state:
            rows = self._db.execute(
                "SELECT * FROM promises WHERE state=? ORDER BY updated_at DESC, id LIMIT ?",
                (str(state), lim)).fetchall()
        else:
            rows = self._db.execute(
                "SELECT * FROM promises ORDER BY updated_at DESC, id LIMIT ?",
                (lim,)).fetchall()
        return [_public_promise(dict(r)) for r in rows]

    def latest_candidate(self) -> Optional[dict]:
        row = self._db.execute(
            "SELECT * FROM promises WHERE state='candidate' "
            "ORDER BY updated_at DESC, id DESC LIMIT 1").fetchone()
        return _public_promise(dict(row)) if row else None

    def active_promises(self, limit: int = 20) -> list[dict]:
        return self.promises("active", limit)

    def due_promises(self, now: Optional[float] = None) -> list[dict]:
        at = _now() if now is None else float(now)
        rows = self._db.execute(
            "SELECT * FROM promises WHERE state='active' AND due_at>0 "
            "AND due_at<=? AND follow_up_count=0 "
            "ORDER BY due_at ASC, id LIMIT 5", (at,)).fetchall()
        return [_public_promise(dict(r)) for r in rows]

    def add_candidate(self, text: str, *, source_text: str = "",
                      source_turn_id: int = 0, due_at: float = 0,
                      condition: str = "", now: Optional[float] = None) -> dict:
        body = _norm(text)
        if not body:
            raise ValueError("承诺不能为空")
        at = _now() if now is None else float(now)
        # 同一句话 24 小时内不重复建候选；少量事项，不刷屏。
        row = self._db.execute(
            "SELECT * FROM promises WHERE text=? AND created_at>? "
            "ORDER BY created_at DESC LIMIT 1", (body[:500], at - 86400)).fetchone()
        if row:
            return {"ok": False, "reason": "duplicate", "promise": _public_promise(dict(row))}
        pid = _new_id("pr")
        self._db.execute(
            "INSERT INTO promises(id,text,state,due_at,condition,source_turn_id,"
            "source_text,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (pid, body[:500], "candidate", float(due_at or 0),
             _norm(condition)[:180], int(source_turn_id or 0),
             _norm(source_text)[:300], at, at))
        self._event(pid, "", "candidate", "proposed", source_text, "")
        self._db.commit()
        return {"ok": True, "promise": self.get(pid)}

    def get(self, promise_id: str) -> Optional[dict]:
        row = self._db.execute("SELECT * FROM promises WHERE id=?",
                               (str(promise_id),)).fetchone()
        return _public_promise(dict(row)) if row else None

    def _event(self, promise_id: str, from_state: str, to_state: str,
               action: str, source_text: str = "", evidence: str = "") -> None:
        self._db.execute(
            "INSERT INTO promise_events(id,promise_id,from_state,to_state,action,"
            "source_text,evidence,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (_new_id("pe"), str(promise_id), str(from_state or ""), str(to_state),
             str(action or ""), _norm(source_text)[:300], _norm(evidence)[:300], _now()))

    def _transition(self, promise_id: str, to_state: str, action: str,
                    *, source_text: str = "", evidence: str = "",
                    accepted_text: str = "", due_at: Optional[float] = None) -> dict:
        to_state = str(to_state)
        if to_state not in PROMISE_STATES:
            raise ValueError(f"非法承诺状态: {to_state}")
        row = self._db.execute("SELECT * FROM promises WHERE id=?",
                               (str(promise_id),)).fetchone()
        if row is None:
            return {"ok": False, "reason": "not_found"}
        cur = dict(row)
        old = str(cur.get("state") or "")
        allowed = {
            "candidate": {"active", "cancelled", "expired"},
            "active": {"paused", "done", "cancelled", "expired"},
            "paused": {"active", "done", "cancelled", "expired"},
            "done": set(), "cancelled": set(), "expired": set(),
        }
        if to_state not in allowed.get(old, set()):
            return {"ok": False, "reason": "bad_transition", "from": old,
                    "to": to_state}
        at = _now()
        sets = ["state=?", "updated_at=?"]
        args: list = [to_state, at]
        if accepted_text:
            sets.append("accepted_text=?")
            args.append(_norm(accepted_text)[:300])
        if evidence:
            sets.append("completion_evidence=?")
            args.append(_norm(evidence)[:300])
        if due_at is not None:
            sets.append("due_at=?")
            args.append(float(due_at))
        if to_state in ("done", "cancelled", "expired"):
            sets.append("follow_up_at=0")
        args.append(str(promise_id))
        self._db.execute(f"UPDATE promises SET {','.join(sets)} WHERE id=?", tuple(args))
        self._event(str(promise_id), old, to_state, action, source_text, evidence)
        self._db.commit()
        return {"ok": True, "promise": self.get(str(promise_id))}

    def accept(self, promise_id: str, accepted_text: str = "") -> dict:
        return self._transition(promise_id, "active", "accepted",
                                source_text=accepted_text,
                                accepted_text=accepted_text)

    def pause(self, promise_id: str, source_text: str = "") -> dict:
        return self._transition(promise_id, "paused", "paused",
                                source_text=source_text)

    def cancel(self, promise_id: str, source_text: str = "") -> dict:
        return self._transition(promise_id, "cancelled", "cancelled",
                                source_text=source_text)

    def complete(self, promise_id: str, evidence: str = "") -> dict:
        return self._transition(promise_id, "done", "completed",
                                evidence=evidence, source_text=evidence)

    def expire(self, promise_id: str, source_text: str = "") -> dict:
        return self._transition(promise_id, "expired", "expired",
                                source_text=source_text)

    def mark_followed(self, promise_id: str, now: Optional[float] = None) -> dict:
        at = _now() if now is None else float(now)
        row = self._db.execute("SELECT * FROM promises WHERE id=?",
                               (str(promise_id),)).fetchone()
        if row is None:
            return {"ok": False, "reason": "not_found"}
        if str(row["state"]) != "active":
            return {"ok": False, "reason": "not_active"}
        if int(row["follow_up_count"] or 0) >= 1:
            return {"ok": False, "reason": "already_followed"}
        self._db.execute(
            "UPDATE promises SET follow_up_count=follow_up_count+1,"
            "last_follow_up=?,updated_at=? WHERE id=?",
            (at, at, str(promise_id)))
        self._event(str(promise_id), "active", "active", "followed_up",
                    evidence="主动跟进一次")
        self._db.commit()
        return {"ok": True, "promise": self.get(str(promise_id))}

    def expire_stale(self, now: Optional[float] = None) -> dict:
        at = _now() if now is None else float(now)
        rows = self._db.execute(
            "SELECT * FROM promises WHERE state IN ('active','paused') "
            "AND due_at>0 AND due_at<?", (at - PROMISE_FOLLOWUP_GRACE_S,)
        ).fetchall()
        done = []
        for r in rows:
            res = self.expire(str(r["id"]), "期限过去，未完成")
            if res.get("ok"):
                done.append(str(r["id"]))
        return {"expired": done, "count": len(done)}

    # ---- 正式沉默 ---------------------------------------------------------
    def record_silence(self, reason_code: str, reason: str = "",
                       channel: str = "any", intent_key: str = "",
                       until_ts: float = 0, now: Optional[float] = None,
                       dedupe_s: float = SILENCE_DEDUPE_S) -> dict:
        at = _now() if now is None else float(now)
        code = str(reason_code or "no_value")[:40]
        key = str(intent_key or "")[:120]
        bucket = int(at // max(30.0, float(dedupe_s)))
        dedupe = f"{code}|{channel}|{key}|{bucket}"
        row = self._db.execute(
            "SELECT * FROM silence_decisions WHERE dedupe=? ORDER BY ts DESC LIMIT 1",
            (dedupe,)).fetchone()
        if row:
            out = dict(row)
            out["deduped"] = True
            return {"ok": True, "decision": out, "deduped": True}
        did = _new_id("sl")
        self._db.execute(
            "INSERT INTO silence_decisions(id,ts,until_ts,reason_code,reason,"
            "channel,intent_key,dedupe,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (did, at, max(0.0, float(until_ts or 0)), code,
             _norm(reason)[:220], str(channel or "any")[:20], key, dedupe, at))
        self._db.commit()
        row = self._db.execute("SELECT * FROM silence_decisions WHERE id=?",
                               (did,)).fetchone()
        return {"ok": True, "decision": dict(row), "deduped": False}

    def quiet_until(self, now: Optional[float] = None) -> float:
        at = _now() if now is None else float(now)
        row = self._db.execute(
            "SELECT MAX(until_ts) m FROM silence_decisions "
            "WHERE reason_code='user_quiet' AND until_ts>?", (at,)).fetchone()
        return float(row["m"] or 0.0) if row else 0.0

    def is_quiet(self, now: Optional[float] = None) -> bool:
        return self.quiet_until(now) > (_now() if now is None else float(now))

    def silent_for(self, intent_key: str, now: Optional[float] = None,
                   within_s: float = 6 * 3600.0) -> bool:
        """同一意图是否已经有通道决定沉默；跨端共用的一条记录。"""
        key = str(intent_key or "")
        if not key:
            return False
        at = _now() if now is None else float(now)
        row = self._db.execute(
            "SELECT 1 FROM silence_decisions WHERE intent_key=? AND ts>=? "
            "ORDER BY ts DESC LIMIT 1",
            (key, at - max(60.0, float(within_s)))).fetchone()
        return row is not None

    def recent_silence(self, limit: int = 20) -> list[dict]:
        rows = self._db.execute(
            "SELECT * FROM silence_decisions ORDER BY ts DESC LIMIT ?",
            (max(1, min(100, int(limit))),)).fetchall()
        return [dict(r) for r in rows]

    # ---- 一轮之后的接线 ---------------------------------------------------
    def observe_turn(self, user_text: str, assistant_text: str = "",
                     *, turn_id: int = 0, appraisal: Optional[dict] = None,
                     pre_answer: Optional[dict] = None,
                     now: Optional[float] = None) -> dict:
        """真实一轮之后更新 P3；只做派生状态，不改 memory 事实。"""
        at = _now() if now is None else float(now)
        out: dict = {"climate": None, "promise": None, "quiet": False,
                     "silence": None}
        user = _norm(user_text)
        reply = _norm(assistant_text)
        ap = appraisal if isinstance(appraisal, dict) else {}
        rel = ap.get("relationship_appraisal") if isinstance(
            ap.get("relationship_appraisal"), dict) else {}
        label = str((rel or {}).get("label") or "")
        signal = {"偏暖": "warm", "偏冷": "cold", "有张力": "cold"}.get(label)
        if signal:
            out["climate"] = self.add_climate_event(
                signal, source_text=user, source_kind="turn", turn_id=turn_id,
                ts=at, note=f"评价候选：{label}")
        pre = pre_answer if isinstance(pre_answer, dict) else {}
        quiet = (str(pre.get("strategy") or "") == "pause"
                 or bool(_QUIET_RE.search(user)))
        if quiet:
            out["quiet"] = True
            out["silence"] = self.record_silence(
                "user_quiet", "他明确说想安静；不追问、不换通道继续",
                channel="all", intent_key="conversation", now=at,
                until_ts=at + QUIET_WINDOW_S)
        else:
            cand = self.latest_candidate()
            if cand and _ACCEPT_RE.search(user[:30]):
                out["promise"] = self.accept(cand["id"], user)
            active = self.active_promises(limit=1)
            if active:
                cur = active[0]
                if _CANCEL_RE.search(user):
                    out["promise"] = self.cancel(cur["id"], user)
                elif _PAUSE_RE.search(user):
                    out["promise"] = self.pause(cur["id"], user)
                elif reply and _DONE_REPLY_RE.search(reply):
                    out["promise"] = self.complete(cur["id"], reply)
        if reply and not quiet:
            found = detect_promise(reply, now=at)
            if found.get("ok"):
                res = self.add_candidate(
                    found["text"], source_text=reply, source_turn_id=turn_id,
                    due_at=float(found.get("due_at") or 0),
                    condition=str(found.get("condition") or ""), now=at)
                if res.get("ok"):
                    out["promise"] = res.get("promise")
        return out

    # ---- 给提示/界面 ------------------------------------------------------
    def prompt_block(self, now: Optional[float] = None) -> str:
        at = _now() if now is None else float(now)
        c = self.climate(at)
        parts: list[str] = []
        if c.get("label") != "还看不出来" and c.get("prompt_line"):
            parts.append("【关系气候】\n" + str(c["prompt_line"]))
        ps = self.active_promises(limit=3)
        if ps:
            lines = []
            for p in ps:
                due = ""
                if float(p.get("due_at") or 0) > 0:
                    due = datetime.fromtimestamp(float(p["due_at"])).strftime("%m-%d %H:%M")
                lines.append(f"- {p['text'][:120]}"
                             + (f"（约在 {due}）" if due else "")
                             + "；只在条件满足时提一次，不追问。")
            parts.append("【还没做完的事】\n" + "\n".join(lines))
        return "\n\n".join(parts)

    def stats(self) -> dict:
        with self._lock:
            q = lambda sql, *a: int(self._db.execute(sql, a).fetchone()[0] or 0)
            return {
                "climate_events": q("SELECT COUNT(*) FROM climate_events"),
                "promises": q("SELECT COUNT(*) FROM promises"),
                "active": q("SELECT COUNT(*) FROM promises WHERE state='active'"),
                "candidate": q("SELECT COUNT(*) FROM promises WHERE state='candidate'"),
                "silence": q("SELECT COUNT(*) FROM silence_decisions"),
                "schema_version": int(self._db.execute(
                    "PRAGMA user_version").fetchone()[0] or 0),
            }

    def summary(self, now: Optional[float] = None) -> dict:
        at = _now() if now is None else float(now)
        return {"ok": True, "available": True,
                "climate": self.climate(at),
                "promises": self.active_promises(limit=8),
                "candidate_count": len(self.promises("candidate", limit=10)),
                "silence": self.recent_silence(8),
                "stats": self.stats()}
