"""主动开口：她自己决定先找他，而不是等他先发话。

这一层只做两件事：
    1. 待投递队列（她写好的那句话先落库，手机轮询/Android 服务来取）；
    2. 一个总开关。2026-11-01 用户要求取消固定节律：不限次数、不限间隔、
       不设夜间禁发，她想说就说。

不在这里生成内容——内容必须由 Chat 用她的状态、记忆和自己长出来的问题生成，
否则就会变成「定时推送的机器人」，不是她先开口。

保留的是边界，不是节律：
- 睡着（sleep.json 还在）不主动发；她没有醒，不该越过休眠。
- 精力太低（<0.25）不发，由 Chat.proactive_seed() 判断。
- 同一句话/同一个问题 48 小时内不重复问。
- 主动消息不写记忆，只进队列。
"""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import config as C

DB_PATH = C.DATA / "proactive.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages(
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL NOT NULL,
    text    TEXT NOT NULL,
    kind    TEXT NOT NULL DEFAULT 'ask',
    seed    TEXT DEFAULT '',
    status  TEXT NOT NULL DEFAULT 'new'
);
CREATE INDEX IF NOT EXISTS idx_proactive_ts ON messages(ts DESC);
"""


def _now() -> float:
    return time.time()


def _day_key(ts: float) -> str:
    return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d")


class ProactiveStore:
    def __init__(self, path: Path | str = DB_PATH) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, timeout=15)
        self._db.row_factory = sqlite3.Row
        try:
            self._db.execute("PRAGMA journal_mode=WAL")
        except Exception:
            pass
        self._db.executescript(_SCHEMA)
        self._db.commit()

    def close(self) -> None:
        try:
            self._db.close()
        except Exception:
            pass

    def record(self, text: str, kind: str = "ask", seed: str = "",
               ts: Optional[float] = None) -> dict:
        text = " ".join(str(text or "").split()).strip()
        if not text:
            raise ValueError("主动消息不能为空")
        now = float(ts if ts is not None else _now())
        cur = self._db.execute(
            "INSERT INTO messages(ts,text,kind,seed,status) VALUES(?,?,?,?,'new')",
            (now, text[:500], str(kind or "ask")[:20], str(seed or "")[:300]))
        self._db.commit()
        return {"id": int(cur.lastrowid), "ts": now, "text": text[:500],
                "kind": kind, "seed": seed}

    def since(self, since_id: int = 0, limit: int = 20) -> list[dict]:
        rows = self._db.execute(
            "SELECT id,ts,text,kind,seed,status FROM messages "
            "WHERE id > ? ORDER BY id ASC LIMIT ?",
            (max(0, int(since_id)), max(1, min(50, int(limit))))).fetchall()
        return [dict(r) for r in rows]

    def latest(self, limit: int = 5, hours: Optional[float] = None) -> list[dict]:
        lim = max(1, min(20, int(limit)))
        if hours is None:
            rows = self._db.execute(
                "SELECT id,ts,text,kind,seed,status FROM messages "
                "ORDER BY id DESC LIMIT ?", (lim,)).fetchall()
        else:
            rows = self._db.execute(
                "SELECT id,ts,text,kind,seed,status FROM messages "
                "WHERE ts >= ? ORDER BY id DESC LIMIT ?",
                (time.time() - max(0.1, float(hours)) * 3600, lim)).fetchall()
        return [dict(r) for r in reversed(rows)]

    def last(self) -> Optional[dict]:
        row = self._db.execute(
            "SELECT id,ts,text,kind,seed,status FROM messages "
            "ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def last_ts(self) -> float:
        row = self._db.execute("SELECT MAX(ts) m FROM messages").fetchone()
        return float(row["m"] or 0.0) if row else 0.0

    def count_today(self, now: Optional[float] = None) -> int:
        day = _day_key(now or _now())
        rows = self._db.execute(
            "SELECT ts FROM messages WHERE ts >= ?", (_now() - 3 * 86400,)).fetchall()
        return sum(1 for r in rows if _day_key(float(r["ts"])) == day)

    def recent_texts(self, hours: float = 48.0) -> list[str]:
        row = self._db.execute(
            "SELECT text FROM messages WHERE ts >= ? ORDER BY id DESC",
            (time.time() - max(1.0, float(hours)) * 3600,)).fetchall()
        return [str(r["text"]) for r in rows]

    def recent_seeds(self, hours: float = 48.0) -> set[str]:
        row = self._db.execute(
            "SELECT seed FROM messages WHERE ts >= ? AND seed IS NOT NULL AND seed!=''",
            (time.time() - max(1.0, float(hours)) * 3600,)).fetchall()
        return {str(r["seed"]) for r in row}

    def stats(self) -> dict:
        row = self._db.execute(
            "SELECT COUNT(*) n FROM messages").fetchone()
        return {"total": int(row["n"] if row else 0),
                "today": self.count_today(),
                "last": self.last()}


def can_send() -> dict:
    """只检查总开关。节律已取消，何时开口由 Chat 的由头决定。"""
    if not C.PROACTIVE_ENABLED:
        return {"ok": False, "reason": "主动消息关着（ROBOT_PROACTIVE_ENABLED=0）"}
    return {"ok": True, "reason": ""}
