"""瓦尔基里 · 水镜：把她的识海装进一部手机里。

这不是又一套聊天窗。手机只负责「看见她、翻她的记忆、替她记住」，
真正的她仍然跑在这台电脑上：Chat / Mind / Memory / 情绪都留在 PC。
服务端只做三件事：

1. 把 `memory.db` 读成一个手机能翻的时间线；
2. 把手机里的新增、更正、撤回写回同一套 `Memory` API；
3. 把「此刻的她」压缩成状态，给首页那面水镜。

传输与业务分开：
- Wi-Fi：`pocket.cmd` 起在 0.0.0.0:8766，手机打开打印出来的局域网地址；
- 有线：Android USB 调试 + `adb reverse tcp:8766 tcp:8766`，手机打开
  `http://127.0.0.1:8766`；或者 USB 网络共享后打开电脑在共享网段的地址。
两条路走的是同一个 HTTP API，换线不改代码。

安全边界：
- 除 `/api/health` 外所有 API 都要配对码（`X-Valkyrie-Token` 或 `?token=`）；
- 默认只监听 127.0.0.1，只有 `--lan` 才监听 0.0.0.0；
- 不提供任意 SQL、不提供硬删除；「撤回」只标失效，原文留在库里；
- 不自动开口：只有 `--chat` 才启用 `/api/chat`，且发送一句才会叫醒她。

启动：
    C:\\robot\\venv\\Scripts\\python.exe -m app.pocket_server --lan
    C:\\robot\\venv\\Scripts\\python.exe -m app.pocket_server --demo --lan
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import os
import queue
import re
import secrets
import socket
import sqlite3
import threading
import time
import wave
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from aiohttp import web

from . import config as C
from .memory import DB_PATH, Memory, normalize_region
from .proactive import ProactiveStore, can_send
from .response_gate import ResponseGate

APP_NAME = "瓦尔基里 · 水镜"
APP_VERSION = "0.8.0"
HERE = Path(__file__).resolve().parent
WEB_DIR = HERE / "pocket_web"
APK_FILE = HERE / "pocket_apk" / "dist" / "valkyrie-pocket.apk"
TOKEN_FILE = C.DATA / "pocket_token.txt"
DEMO_DB = C.DATA / "pocket_demo.db"

DEFAULT_PORT = int(os.environ.get("VALKYRIE_APP_PORT", "8766"))
MAX_TEXT = 500
MAX_BODY = 32 * 1024
# 手机发来的是 WAV（约 30KB/s）和一张压缩过的 JPEG；8MB 足够，也挡住
# 误传的大视频。aiohttp 自己的 client_max_size 要同步放开。
MAX_CHAT_BODY = 10 * 1024 * 1024
MAX_AUDIO_BYTES = 2 * 1024 * 1024
MAX_IMAGE_BYTES = 6 * 1024 * 1024
ALLOWED_CLAIMS = ("fact", "wish", "quote", "guess")
ALLOWED_REGIONS = ("experience", "relation", "self", "knowledge")
ALLOWED_STATUS = ("active", "retracted", "superseded", "all")
PERSONA_LABELS = {
    "core": "身份与硬边界",
    "tendency": "稳定倾向",
    "interaction": "互动偏好",
    "narrative": "自我叙事",
}


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _json(obj: Any, status: int = 200) -> web.Response:
    return web.json_response(obj, status=status, dumps=lambda o: json.dumps(
        o, ensure_ascii=False, separators=(",", ":")))


def _sse(obj: Any) -> bytes:
    """一条 SSE；手机端按空行切事件。"""
    return ("data: " + json.dumps(
        obj, ensure_ascii=False, separators=(",", ":")) + "\n\n").encode("utf-8")


def _err(message: str, status: int = 400, code: str = "BAD_REQUEST") -> web.Response:
    return _json({"ok": False, "error": message, "code": code}, status)


def _like(q: str) -> str:
    """把用户输入变成 LIKE 子串，`%`/`_` 不当通配符。"""
    q = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{q}%"


def _voice_choices() -> list[dict[str, str]]:
    return C.breeze_voice_choices()


def _voice_name(voice_id: str) -> str:
    for item in _voice_choices():
        if item["id"] == voice_id:
            return item["name"]
    return voice_id


def _requested_voice(raw: Any) -> str:
    """只接受手机端列出的音色，避免把任意 voice_id 塞给云端。"""
    wanted = str(raw or "").strip() or C.BREEZE_VOICE_ID
    valid = {item["id"] for item in _voice_choices()}
    if wanted not in valid:
        raise ValueError("没有这个音色")
    return wanted


def _clamp(value: Any, lo: float, hi: float, default: float) -> float:
    try:
        return max(lo, min(hi, float(value)))
    except (TypeError, ValueError):
        return default


def _b64decode(value: Any, limit: int, label: str) -> bytes:
    """手机传来的 base64：可以先带 data:...;base64, 前缀，但必须能解。"""
    if not value:
        return b""
    if not isinstance(value, str):
        raise ValueError(f"{label}必须是 base64 字符串")
    s = value.strip()
    if s.startswith("data:") and "," in s:
        s = s.split(",", 1)[1]
    try:
        raw = base64.b64decode(s, validate=True)
    except Exception as e:
        raise ValueError(f"{label}不是合法的 base64") from e
    if len(raw) > limit:
        raise ValueError(f"{label}太大（最多 {limit // 1024}KB）")
    return raw


def _ipv4s() -> list[str]:
    """本机可用于手机访问的 IPv4。不把 127.0.0.1 混进局域网列表。"""
    out: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip and not ip.startswith("127."):
                out.add(ip)
    except Exception:
        pass
    # UDP connect 不真发包，但能拿到出口网卡地址；掉线时静默跳过。
    for probe in ("8.8.8.8", "223.5.5.5"):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(0.15)
            s.connect((probe, 80))
            ip = s.getsockname()[0]
            if ip and not ip.startswith("127."):
                out.add(ip)
            s.close()
        except Exception:
            pass
    return sorted(out)


def _read_token(path: Path) -> str:
    try:
        raw = path.read_text(encoding="utf-8").strip()
        if re.fullmatch(r"[0-9A-Za-z_-]{4,64}", raw):
            return raw
    except Exception:
        pass
    return ""


def _new_token() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def _write_token(path: Path, token: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(token, encoding="utf-8")
    except Exception:
        pass


class PocketAuth:
    """一个配对码，不存用户账号；失败多了短时间锁一下。"""

    def __init__(self, token: str) -> None:
        self.token = token
        self._fails: dict[str, list[float]] = {}

    def _client(self, request: web.Request) -> str:
        return request.remote or "?"

    def locked(self, request: web.Request) -> bool:
        now = time.time()
        arr = [t for t in self._fails.get(self._client(request), []) if now - t < 60]
        self._fails[self._client(request)] = arr
        return len(arr) >= 8

    def fail(self, request: web.Request) -> None:
        self._fails.setdefault(self._client(request), []).append(time.time())

    def check(self, request: web.Request) -> bool:
        offered = (request.headers.get("X-Valkyrie-Token")
                   or request.query.get("token") or "")
        return bool(self.token) and secrets.compare_digest(str(offered), self.token)


class PocketStore:
    """读写同一份 memory.db；读连接 query_only，不产生副作用。

    注意：读接口仍不用 `Memory.search()`，而是直接 SQL 只读。
    翻记忆不是回忆，不能因为打开 App 就改变她记得多清楚；
    P4 起 `search(reinforce=False)` 默认只读，但读接口继续走自己的路径，
    不把“默认值”当成唯一保险。
    """

    def __init__(self, path: Path, *, writable: bool = True, demo: bool = False) -> None:
        self.path = Path(path)
        self.demo = demo
        self.writable = bool(writable)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            # 先用 Memory 建一遍 schema，再关掉；不改她的数据。
            Memory(self.path).close()
        if demo and not self._has_episodes():
            self._seed_demo()
        self._read = sqlite3.connect(str(self.path), check_same_thread=False)
        self._read.row_factory = sqlite3.Row
        self._read.execute("PRAGMA query_only=ON")
        self._write = Memory(self.path) if self.writable else None

    def close(self) -> None:
        try:
            self._read.close()
        except Exception:
            pass
        if self._write is not None:
            self._write.close()

    def _has_episodes(self) -> bool:
        db = sqlite3.connect(str(self.path))
        try:
            row = db.execute("SELECT COUNT(*) c FROM episodes").fetchone()
            return bool(row and int(row[0]) > 0)
        finally:
            db.close()

    def _seed_demo(self) -> None:
        """示例库只写 pocket_demo.db，永远不碰真实 memory.db。"""
        mem = Memory(self.path)
        try:
            now = time.time()
            mem.add_fact("称呼", "他", conf=0.95, source="示例数据")
            mem.add_fact("常待的地方", "靠窗的书桌", conf=0.78, source="示例数据")
            mem.add_episode(
                "今天他把窗子推开，说屋里有点闷。",
                kind="note", who="他", imp=0.42, ts=now - 2 * 3600,
                scene="窗边", emo="平静", emo_i=0.12,
                source="示例数据", about="他", claim="fact",
            )
            mem.add_episode(
                "他提起小时候住在江边，夏天的风带着水腥味。",
                kind="note", who="他", imp=0.68, ts=now - 26 * 3600,
                scene="聊天", emo="怀念", emo_i=0.45,
                source="示例数据", about="他", claim="fact",
            )
            mem.add_episode(
                "他说周末想去海边，但还没定是哪一天。",
                kind="note", who="他", imp=0.74, ts=now - 3 * 86400,
                scene="聊天", emo="期待", emo_i=0.52,
                source="示例数据", about="他", claim="wish",
            )
        finally:
            mem.close()

    # ---- 读 ---------------------------------------------------------------
    def stats(self) -> dict[str, Any]:
        db = self._read

        def one(sql: str, *args) -> int:
            r = db.execute(sql, args).fetchone()
            return int(r[0] or 0) if r else 0

        first = db.execute("SELECT MIN(ts) m FROM episodes").fetchone()
        since = None
        if first and first["m"]:
            since = datetime.fromtimestamp(float(first["m"])).strftime("%Y-%m-%d")
        regions = {str(r["region"] or "experience"): int(r["c"]) for r in db.execute(
            "SELECT region, COUNT(*) c FROM episodes GROUP BY region")}
        return {
            "episodes": one("SELECT COUNT(*) FROM episodes"),
            "active": one("SELECT COUNT(*) FROM episodes WHERE status='active'"),
            "retracted": one("SELECT COUNT(*) FROM episodes WHERE status='retracted'"),
            "superseded": one("SELECT COUNT(*) FROM episodes WHERE status='superseded'"),
            "facts": one("SELECT COUNT(*) FROM facts"),
            "turns": one("SELECT COUNT(*) FROM turns"),
            "pending": one(
                "SELECT COUNT(*) FROM episodes WHERE stage='raw' AND status='active'"),
            "deep": one(
                "SELECT COUNT(*) FROM episodes WHERE stage IN ('segment','meaning','deep') "
                "AND status='active'"),
            "regions": regions,
            "since": since,
            "db_kb": round(self.path.stat().st_size / 1024, 1) if self.path.exists() else 0,
        }

    def _episode_row(self, row: sqlite3.Row) -> dict[str, Any]:
        from . import affect as _affect
        try:
            feel = json.loads(row["feel"] or "{}")
        except Exception:
            feel = {}
        emo = str(row["emo"] or "")
        return {
            "id": int(row["id"]),
            "ts": float(row["ts"]),
            "day": str(row["day"] or ""),
            "kind": str(row["kind"] or ""),
            "region": normalize_region(row["region"] or "experience"),
            "who": str(row["who"] or ""),
            "text": str(row["text"] or ""),
            "imp": float(row["imp"] or 0.0),
            "emo": emo,
            "color": _affect.color_of(emo),
            "emo_i": float(row["emo_i"] or 0.0),
            "feel": feel if isinstance(feel, dict) else {},
            "scene": str(row["scene"] or ""),
            "stage": str(row["stage"] or "raw"),
            "claim": str(row["claim"] or "fact"),
            "source": str(row["source"] or ""),
            "about": str(row["about"] or ""),
            "status": str(row["status"] or "active"),
            "superseded_by": int(row["superseded_by"] or 0),
            "retract_reason": str(row["retract_reason"] or ""),
        }

    def list_episodes(self, *, query: str = "", limit: int = 40, offset: int = 0,
                      status: str = "active", region: str = "") -> dict[str, Any]:
        status = status if status in ALLOWED_STATUS else "active"
        where: list[str] = []
        args: list[Any] = []
        if status != "all":
            where.append("status=?")
            args.append(status)
        if region:
            where.append("region=?")
            args.append(normalize_region(region))
        q = (query or "").strip()
        if q:
            like = _like(q[:80])
            where.append("(text LIKE ? ESCAPE '\\' OR who LIKE ? ESCAPE '\\' "
                         "OR source LIKE ? ESCAPE '\\' OR about LIKE ? ESCAPE '\\')")
            args.extend([like, like, like, like])
        sql_where = (" WHERE " + " AND ".join(where)) if where else ""
        count = int(self._read.execute(
            "SELECT COUNT(*) c FROM episodes" + sql_where, tuple(args)).fetchone()["c"])
        rows = self._read.execute(
            "SELECT * FROM episodes" + sql_where +
            " ORDER BY ts DESC LIMIT ? OFFSET ?",
            tuple(args + [limit, offset])).fetchall()
        return {"items": [self._episode_row(r) for r in rows], "total": count,
                "limit": limit, "offset": offset}

    def get_episode(self, episode_id: int) -> Optional[dict[str, Any]]:
        row = self._read.execute(
            "SELECT * FROM episodes WHERE id=?", (int(episode_id),)).fetchone()
        if row is None:
            return None
        item = self._episode_row(row)
        item["feedback"] = [dict(r) for r in self._read.execute(
            "SELECT id,kind,source_kind,turn_id,source_ref,source_text,"
            "target_id,target_text,action,replacement_id,reason,recorded_at "
            "FROM feedback_events WHERE target_id=? "
            "ORDER BY recorded_at DESC, id DESC", (str(int(episode_id)),)
        ).fetchall()]
        related = self._read.execute(
            "SELECT * FROM episodes WHERE id!=? AND status='active' "
            "AND (day=? OR (who!='' AND who=?)) "
            "ORDER BY ABS(ts-?) LIMIT 4",
            (int(episode_id), item["day"], item["who"], item["ts"])).fetchall()
        item["related"] = [self._episode_row(r) for r in related]
        return item

    def pre_answer_feedback(self, limit: int = 20) -> dict[str, Any]:
        """最近的分寸反馈；读连接只读，翻页面不改她的反馈。"""
        from . import pre_answer_feedback as paf

        n = max(1, min(100, int(limit)))
        rows = self._read.execute(
            "SELECT * FROM feedback_events WHERE kind='pre_answer' "
            "AND target_kind='pre_answer' "
            "ORDER BY recorded_at DESC, id DESC LIMIT ?", (n,)).fetchall()
        items = [paf.describe_row(dict(r)) for r in rows]
        resolved = [x for x in items if x["action"] in paf.OUTCOMES]
        return {"items": items, "stats": paf.summarize(resolved)}

    def retract_pre_answer_feedback(self, feedback_id: str,
                                    reason: str = "") -> dict[str, Any]:
        if self._write is None:
            raise PermissionError("只读模式")
        return self._write.retract_pre_answer_feedback(feedback_id, reason)

    def list_facts(self) -> list[dict[str, Any]]:
        rows = self._read.execute(
            "SELECT key,value,conf,ts,updated,source FROM facts "
            "ORDER BY updated DESC, key ASC").fetchall()
        return [{"key": str(r["key"]), "value": str(r["value"]),
                 "conf": float(r["conf"] or 0), "ts": float(r["ts"] or 0),
                 "updated": float(r["updated"] or 0),
                 "source": str(r["source"] or "")} for r in rows]

    def state(self) -> dict[str, Any]:
        if self.demo:
            return {
                "source": "demo", "name": C.ROBOT_NAME,
                "state": {
                    "days": 3.2, "hours": 76.8, "turns": 0,
                    "energy": 0.82, "mood": 0.01, "arousal": 0.22,
                    "feel_top": "平静 0.42 · 好奇 0.28",
                    "feel_text": "海面很静，她在等第一句话。",
                    "feel": {"平静": 0.42, "好奇": 0.28},
                    "seven_text": "喜 0.10 · 哀 0.05",
                    "desire_text": "被听懂 0.31",
                    "lonely": 0.18, "silent_h": 2.5,
                    "growth": "frozen",
                    "text": "- 你现在很安静。\n- 识海里只有示例数据。",
                    "existence": "示例状态：这不是她真实的那一天。",
                    "rel": {"熟悉": 0.2, "信任": 0.1},
                },
                "sleep": None,
                "memory": self.stats(),
                "chat": False,
                "notice": "示例数据，不是她的真实记录。",
                "growth": self.growth_summary(),
                "p3": self.p3_payload(),
            }
        snap: dict[str, Any] = {}
        sleep_brief = None
        error = ""
        try:
            from .mind import Mind
            from .sleep import status as sleep_status
            mind = Mind()
            try:
                raw = mind.snapshot()
            finally:
                mind.close()
            keep = ("days", "hours", "turns", "energy", "mood", "arousal",
                    "feel", "feel_top", "feel_text", "feel_family",
                    "seven_text", "desire_text", "lonely", "silent_h",
                    "growth", "rel", "text", "existence")
            snap = {k: raw.get(k) for k in keep if k in raw}
            sl = sleep_status()
            if sl:
                sleep_brief = {
                    "sleeping": True,
                    "slept_h": sl.get("slept_h"),
                    "iso": sl.get("iso"),
                    "reason": sl.get("reason"),
                    "wake_when": sl.get("wake_when"),
                }
        except Exception as e:  # 状态读不到也不该让记忆页跟着挂
            error = f"{type(e).__name__}: {e}"
        from . import affect as _affect
        top = str(snap.get("feel_top") or "").split(" ")[0]
        color = _affect.color_of(top) if top else "#c3ccdb"
        return {"source": "real", "name": C.ROBOT_NAME, "state": snap,
                "mood_color": color, "sleep": sleep_brief,
                "memory": self.stats(), "chat": False, "error": error,
                "notice": "", "growth": self.growth_summary(),
                "p3": self.p3_payload()}

    # ---- P4 的只读面：手机看见她长成什么，不在手机上改 -------------------
    def growth_summary(self) -> dict[str, Any]:
        """首页/状态用的轻量摘要；只查计数和 active 版本，不构造完整面板。"""
        def count(sql: str) -> int:
            row = self._read.execute(sql).fetchone()
            return int(row[0] or 0) if row else 0

        rows = self._read.execute(
            "SELECT id,layer,field,value,confidence,confirmed_at,updated_at "
            "FROM persona_versions WHERE status='active' "
            "ORDER BY updated_at DESC, id").fetchall()
        active = [{
            "id": str(r["id"]),
            "layer": str(r["layer"] or ""),
            "layer_label": PERSONA_LABELS.get(str(r["layer"] or ""), str(r["layer"] or "")),
            "field": str(r["field"] or ""),
            "value": str(r["value"] or "")[:180],
            "confidence": float(r["confidence"] or 0.0),
            "confirmed_at": float(r["confirmed_at"] or 0.0),
        } for r in rows]
        return {
            "meanings_confirmed": count(
                "SELECT COUNT(*) FROM meaning_candidates WHERE status='confirmed'"),
            "chapters_confirmed": count(
                "SELECT COUNT(*) FROM chapter_confirmations cc "
                "JOIN chapters ch ON ch.id=cc.chapter_id WHERE ch.status='open'"),
            "persona_active": len(active),
            "persona_candidates": count(
                "SELECT COUNT(*) FROM persona_versions WHERE status='candidate'"),
            "active": active,
        }

    @staticmethod
    def _public_meaning(row: dict[str, Any]) -> dict[str, Any]:
        sources = [{
            "id": int(s.get("id") or 0),
            "text": str(s.get("text") or "")[:180],
            "day": str(s.get("day") or ""),
            "who": str(s.get("who") or ""),
            "status": str(s.get("status") or "active"),
        } for s in (row.get("sources") or []) if isinstance(s, dict)]
        return {
            "id": str(row.get("id") or ""),
            "kind": str(row.get("kind") or ""),
            "title": str(row.get("title") or ""),
            "claim": str(row.get("claim") or ""),
            "status": str(row.get("status") or ""),
            "confidence": float(row.get("confidence") or 0.0),
            "usable": bool(row.get("usable")),
            "sources_ok": bool(row.get("sources_ok")),
            "updated_at": float(row.get("updated_at") or 0.0),
            "source_count": len(sources),
            "sources": sources[:5],
        }

    @staticmethod
    def _public_chapter(row: dict[str, Any]) -> dict[str, Any]:
        sources = [{
            "id": int(s.get("id") or 0),
            "text": str(s.get("text") or "")[:180],
            "day": str(s.get("day") or ""),
            "who": str(s.get("who") or ""),
            "status": str(s.get("status") or "active"),
        } for s in (row.get("sources") or []) if isinstance(s, dict)]
        return {
            "id": str(row.get("id") or ""),
            "title": str(row.get("title") or ""),
            "meaning": str(row.get("meaning") or ""),
            "status": str(row.get("status") or ""),
            "effective_status": str(row.get("effective_status") or row.get("status") or ""),
            "confirmed": bool(row.get("confirmed")),
            "usable": bool(row.get("usable")),
            "sources_ok": bool(row.get("sources_ok")),
            "updated_at": float(row.get("updated_at") or 0.0),
            "source_count": len(sources),
            "sources": sources[:6],
        }

    def _readonly_meanings(self, limit: int) -> list[dict[str, Any]]:
        rows = self._read.execute(
            "SELECT * FROM meaning_candidates ORDER BY updated_at DESC, id LIMIT ?",
            (limit,)).fetchall()
        out: list[dict[str, Any]] = []
        for raw in rows:
            r = dict(raw)
            try:
                ids = [int(x) for x in json.loads(r.get("source_episode_ids") or "[]")]
            except Exception:
                ids = []
            eps = []
            if ids:
                marks = ",".join("?" * len(ids))
                eps = [dict(x) for x in self._read.execute(
                    f"SELECT id,text,day,who,status FROM episodes WHERE id IN ({marks})",
                    tuple(ids)).fetchall()]
            sources_ok = bool(eps) and all(
                str(x.get("status") or "active") == "active" for x in eps)
            r["sources"] = eps
            r["sources_ok"] = sources_ok
            r["usable"] = (str(r.get("status")) == "confirmed" and sources_ok
                           and not int(r.get("legacy_unknown") or 0))
            out.append(r)
        return out

    def _readonly_chapters(self, limit: int) -> list[dict[str, Any]]:
        rows = self._read.execute(
            "SELECT * FROM chapters ORDER BY updated_at DESC, id LIMIT ?",
            (limit,)).fetchall()
        confirmed_ids = {str(x["chapter_id"]) for x in self._read.execute(
            "SELECT chapter_id FROM chapter_confirmations").fetchall()}
        out: list[dict[str, Any]] = []
        for raw in rows:
            r = dict(raw)
            events = self._read.execute(
                "SELECT ep_id FROM chapter_events WHERE chapter_id=? "
                "ORDER BY position, ep_id", (str(r["id"]),)).fetchall()
            ids = [int(x["ep_id"]) for x in events]
            eps = []
            if ids:
                marks = ",".join("?" * len(ids))
                eps = [dict(x) for x in self._read.execute(
                    f"SELECT id,text,day,who,status FROM episodes WHERE id IN ({marks})",
                    tuple(ids)).fetchall()]
            sources_ok = len(eps) == len(ids) and bool(eps) and all(
                str(x.get("status") or "active") == "active" for x in eps)
            confirmed = str(r["id"]) in confirmed_ids
            r["sources"] = eps
            r["sources_ok"] = sources_ok
            r["confirmed"] = confirmed
            r["effective_status"] = (
                "disputed" if str(r.get("status")) == "open" and not sources_ok
                else str(r.get("status")))
            r["usable"] = (str(r.get("status")) == "open" and sources_ok
                           and confirmed and not int(r.get("legacy_unknown") or 0))
            out.append(r)
        return out

    def meaning_payload(self, limit: int = 24) -> dict[str, Any]:
        """手机识海读到的意义与共同章节；只读，不确认、不撤回。"""
        limit = max(1, min(50, int(limit)))
        if self._write is not None:
            meanings = self._write.meaning_candidates(limit=limit)
            chapters = self._write.chapters(limit=limit)
        else:
            meanings = self._readonly_meanings(limit)
            chapters = self._readonly_chapters(limit)
        public_meanings = [self._public_meaning(x) for x in meanings]
        public_chapters = [self._public_chapter(x) for x in chapters]
        return {
            "items": public_meanings,
            "chapters": public_chapters,
            "stats": {
                "confirmed": sum(1 for x in public_meanings if x["usable"]),
                "candidates": sum(1 for x in public_meanings if x["status"] == "candidate"),
                "chapters": len(public_chapters),
                "chapters_confirmed": sum(1 for x in public_chapters if x["usable"]),
            },
        }

    def p3_payload(self) -> dict[str, Any]:
        """手机只读的关系气候、未竟承诺和沉默决定；库不存在就返回空。"""
        try:
            from .p3 import read_summary
            return read_summary()
        except Exception as e:
            return {"ok": False, "available": False,
                    "error": f"{type(e).__name__}: {e}",
                    "climate": {}, "promises": [], "silence": [],
                    "candidate_count": 0}

    def persona_payload(self) -> dict[str, Any]:
        """手机只读的人格面板；身份与硬边界正文不离开电脑。"""
        if self._write is not None:
            rows = self._write.persona_versions(limit=300)
        else:
            rows = [dict(r) for r in self._read.execute(
                "SELECT * FROM persona_versions ORDER BY updated_at DESC, id LIMIT 300"
            ).fetchall()]
            for r in rows:
                r["layer_label"] = PERSONA_LABELS.get(str(r.get("layer") or ""), "")
                r["usable"] = (str(r.get("status")) == "active"
                               and not int(r.get("legacy_unknown") or 0))
                r["effective_status"] = str(r.get("status") or "")
        layers: dict[str, dict[str, Any]] = {
            key: {"layer": key, "label": label, "active": None, "candidates": []}
            for key, label in PERSONA_LABELS.items() if key != "core"
        }
        for r in rows:
            layer = str(r.get("layer") or "")
            if layer not in layers:
                continue
            try:
                source_count = len(json.loads(r.get("source_episode_ids") or "[]"))
            except Exception:
                source_count = 0
            public = {
                "id": str(r.get("id") or ""),
                "layer": layer,
                "layer_label": str(r.get("layer_label") or layers[layer]["label"]),
                "field": str(r.get("field") or ""),
                "value": str(r.get("value") or "")[:180],
                "confidence": float(r.get("confidence") or 0.0),
                "status": str(r.get("status") or ""),
                "usable": bool(r.get("usable")),
                "effective_status": str(r.get("effective_status") or r.get("status") or ""),
                "confirmed_at": float(r.get("confirmed_at") or 0.0),
                "updated_at": float(r.get("updated_at") or 0.0),
                "source_count": source_count,
            }
            if str(r.get("status")) == "active":
                layers[layer]["active"] = public
            elif str(r.get("status")) == "candidate":
                layers[layer]["candidates"].append(public)
        return {
            "ok": True,
            "layers": layers,
            "active_count": sum(1 for v in layers.values() if v["active"]),
            "candidate_count": sum(len(v["candidates"]) for v in layers.values()),
            "core": {"locked": True, "label": PERSONA_LABELS["core"],
                     "note": "身份与硬边界只从 PERSONA.md 的 core 块读，手机不显示正文。"},
        }

    # ---- 写 ---------------------------------------------------------------
    def add_episode(self, body: dict[str, Any]) -> dict[str, Any]:
        if self._write is None:
            raise PermissionError("只读模式")
        text = str(body.get("text") or "").strip()
        if not text:
            raise ValueError("记忆不能为空")
        if len(text) > MAX_TEXT:
            raise ValueError(f"一条记忆最多 {MAX_TEXT} 字")
        who = str(body.get("who") or "他").strip()[:20] or "他"
        region = str(body.get("region") or "experience").strip()
        if region not in ALLOWED_REGIONS:
            region = "experience"
        claim = str(body.get("claim") or "fact").strip()
        if claim not in ALLOWED_CLAIMS:
            claim = "fact"
        imp = _clamp(body.get("imp"), 0.05, 1.0, 0.55)
        source = str(body.get("source") or "App 手动记录").strip()[:20]
        eid = self._write.add_episode(
            text, kind="note", who=who, imp=imp,
            scene=str(body.get("scene") or "App")[:60],
            emo=str(body.get("emo") or "")[:20],
            emo_i=_clamp(body.get("emo_i"), 0.0, 1.0, 0.35),
            source=source, about=str(body.get("about") or who)[:20],
            region=region, claim=claim,
        )
        return {"id": int(eid), "text": text, "source": source}

    def correct_episode(self, episode_id: int, body: dict[str, Any]) -> dict[str, Any]:
        if self._write is None:
            raise PermissionError("只读模式")
        text = str(body.get("text") or "").strip()
        if not text:
            raise ValueError("更正后的内容不能为空")
        if len(text) > MAX_TEXT:
            raise ValueError(f"一条记忆最多 {MAX_TEXT} 字")
        old = self._read.execute(
            "SELECT * FROM episodes WHERE id=? AND status='active'",
            (int(episode_id),)).fetchone()
        if old is None:
            raise LookupError("没有找到这条有效记忆")
        reason = str(body.get("reason") or "App 更正")[:80]
        new_id = self._write.supersede(
            int(episode_id), text, reason=reason,
            kind="note", who=str(old["who"] or "他"), imp=float(old["imp"] or 0.55),
            source="App 更正", about=str(old["about"] or old["who"] or "他"),
            region=str(old["region"] or "experience"),
            claim=str(old["claim"] or "fact"),
            feedback_kind="correct", source_kind="app",
            source_text=text, source_ref=f"app:memory:{int(episode_id)}",
        )
        if not new_id:
            raise ValueError("更正没有写进去，可能和刚才那条重复")
        return {"id": int(new_id), "old_id": int(episode_id), "text": text}

    def retract_episode(self, episode_id: int, body: dict[str, Any]) -> dict[str, Any]:
        if self._write is None:
            raise PermissionError("只读模式")
        reason = str(body.get("reason") or "App 要求撤回").strip()[:80]
        res = self._write.retract(
            int(episode_id), reason=reason, kind="delete", source_kind="app",
            source_text=reason, source_ref=f"app:memory:{int(episode_id)}")
        if not int(res.get("retracted") or 0):
            raise LookupError("这条记忆已经不在可撤回状态")
        return res

    def add_fact(self, body: dict[str, Any]) -> dict[str, Any]:
        if self._write is None:
            raise PermissionError("只读模式")
        key = str(body.get("key") or "").strip()[:40]
        value = str(body.get("value") or "").strip()[:200]
        if not key or not value:
            raise ValueError("事实的标题和内容都不能为空")
        conf = _clamp(body.get("conf"), 0.05, 1.0, 0.7)
        self._write.add_fact(key, value, conf=conf,
                             source=str(body.get("source") or "App 手动")[:20])
        return {"key": key, "value": value, "conf": conf}

    def delete_fact(self, key: str) -> bool:
        if self._write is None:
            raise PermissionError("只读模式")
        with self._write._lock:  # 同一把锁；删除事实不碰 episodes
            cur = self._write._db.execute("DELETE FROM facts WHERE key=?", (key[:40],))
            self._write._db.commit()
            return int(cur.rowcount) > 0

    def export(self) -> dict[str, Any]:
        episodes = self.list_episodes(status="all", limit=100000, offset=0)["items"]
        return {"ok": True, "exported_at": _now_iso(), "source": "demo" if self.demo else "real",
                "stats": self.stats(), "episodes": episodes, "facts": self.list_facts()}


class PocketMedia:
    """手机送来的声音与照片：STT 在电脑上跑，照片交给视觉模型看。

    这两样都不落盘：WAV 只在内存里被转成文字，JPEG 只在一次请求里被压成
    一段观察。手机端拿到的只有转写和回复，不保存第二份。
    """

    def __init__(self) -> None:
        self._stt = None
        self._tts: dict[str, Any] = {}
        self._vision = None
        self._stt_lock = threading.Lock()
        self._tts_lock = threading.Lock()
        self._vision_lock = threading.Lock()
        # Breeze 的 Cloudflare 线路不稳，不在后台抢锁预连；这里只预热百炼
        # 复刻音色，避免第一次断线兜底时赶上云侧冷启动。
        self._warm_interval = 600.0
        self._warm_stop = threading.Event()
        self._warm_thread = threading.Thread(
            target=self._warm_loop, name="pocket-tts-warm", daemon=True)
        self._warm_thread.start()

    def _warm_voices(self) -> int:
        from .tts import TTS
        # 先把两个 TTS 实例准备好，再放锁；百炼预热可能要十几秒，不能
        # 握着 _tts_lock 让手机请求排队。
        ttses: list[Any] = []
        if not self._tts_lock.acquire(timeout=0.05):
            return 0
        try:
            for item in _voice_choices():
                voice_id = item["id"]
                tts = self._tts.get(voice_id)
                if tts is None:
                    # 手机端也只认 Breeze 原声；Breeze 断线时宁可返回
                    # TTS_FAILED，也不静默换成 edge/本地或百炼复刻。
                    tts = TTS(breeze_voice=voice_id, strict_breeze=True)
                    self._tts[voice_id] = tts
                ttses.append(tts)
        finally:
            self._tts_lock.release()
        warmed = 0
        for tts in ttses:
            try:
                if tts.warm_fallback():
                    warmed += 1
            except Exception:
                pass
        return warmed

    def _warm_loop(self) -> None:
        if self._warm_stop.wait(1.0):
            return
        while not self._warm_stop.is_set():
            try:
                self._warm_voices()
            except Exception:
                pass
            if self._warm_stop.wait(self._warm_interval):
                return

    def close(self) -> None:
        self._warm_stop.set()
        with self._tts_lock:
            for tts in self._tts.values():
                try:
                    tts.close()
                except Exception:
                    pass
            self._tts.clear()

    def transcribe_wav(self, raw: bytes) -> dict[str, Any]:
        import numpy as np

        try:
            with wave.open(io.BytesIO(raw), "rb") as wf:
                channels = wf.getnchannels()
                width = wf.getsampwidth()
                rate = wf.getframerate()
                frames = wf.readframes(wf.getnframes())
        except Exception as e:
            raise ValueError(f"手机录音不是有效的 WAV：{type(e).__name__}") from e
        if channels != 1 or width != 2:
            raise ValueError("手机录音要是 16-bit 单声道 WAV")
        pcm = np.frombuffer(frames, dtype="<i2").copy()
        if pcm.size == 0:
            raise ValueError("手机录音是空的")
        if rate != C.SAMPLE_RATE:
            # 浏览器已经会降采样；这里再兜一次，免得不同 WebView 给 44.1k。
            duration = len(pcm) / float(rate)
            n = max(1, int(round(duration * C.SAMPLE_RATE)))
            src = np.arange(len(pcm), dtype=np.float64) / float(rate)
            dst = np.arange(n, dtype=np.float64) / float(C.SAMPLE_RATE)
            pcm = np.interp(dst, src, pcm).astype(np.int16)
        with self._stt_lock:
            if self._stt is None:
                from .stt import STT
                self._stt = STT()
            res = self._stt.transcribe(pcm)
        res["pcm_seconds"] = round(len(pcm) / float(C.SAMPLE_RATE), 2)
        return res

    def describe_image(self, raw: bytes, question: str = "") -> dict[str, Any]:
        with self._vision_lock:
            if self._vision is None:
                from .phone_vision import PhoneVision
                self._vision = PhoneVision()
            return self._vision.describe(raw, question=question)

    def synthesize(self, text: str, voice_id: str = "") -> dict[str, Any]:
        """按手机选择的音色合成；同一个音色复用 TTS 实例和缓存。"""
        wanted = (voice_id or C.BREEZE_VOICE_ID).strip() or C.BREEZE_VOICE_ID
        with self._tts_lock:
            tts = self._tts.get(wanted)
            if tts is None:
                from .tts import TTS
                tts = TTS(breeze_voice=wanted, strict_breeze=True)
                self._tts[wanted] = tts
            t0 = time.time()
            path = tts.synth(text)
            raw = Path(path).read_bytes()
            suffix = Path(path).suffix.lower()
            mime = "audio/mpeg" if suffix == ".mp3" else "audio/wav"
            return {
                "audio": base64.b64encode(raw).decode("ascii"),
                "mime": mime,
                "backend": getattr(tts, "last_backend", ""),
                "voice": tts.breeze_voice,
                "voice_name": _voice_name(tts.breeze_voice),
                "seconds": round(time.time() - t0, 2),
            }

    async def stream_pcm(self, text: str,
                         voice_id: str = "") -> AsyncIterator[bytes]:
        """按句流式吐 24k PCM；供手机 `/api/tts/stream` 边收边播。

        线程里跑同步 TTS（含 Breeze 网络读），事件循环只负责把块写到
        HTTP；第一块不等整句合成完，手机先出声，后面的块继续流。
        """
        wanted = (voice_id or C.BREEZE_VOICE_ID).strip() or C.BREEZE_VOICE_ID
        with self._tts_lock:
            tts = self._tts.get(wanted)
            if tts is None:
                from .tts import TTS
                tts = TTS(breeze_voice=wanted, strict_breeze=True)
                self._tts[wanted] = tts

        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue()
        stop = threading.Event()

        def put(kind: str, payload: Any = None) -> None:
            try:
                loop.call_soon_threadsafe(q.put_nowait, (kind, payload))
            except RuntimeError:
                pass

        def run() -> None:
            try:
                # 和 /api/tts 共用同一把锁：同一音色不能同时跑两条 Breeze
                # 流，否则连接池和冷却状态会互相踩。
                with self._tts_lock:
                    for chunk in tts.synth_stream_pcm(text):
                        if stop.is_set():
                            break
                        put("data", chunk)
            except Exception as exc:
                put("error", exc)
            finally:
                put("eof")

        task = asyncio.create_task(
            asyncio.to_thread(run), name="pocket-tts-stream")
        try:
            while True:
                kind, payload = await q.get()
                if kind == "eof":
                    return
                if kind == "error":
                    raise RuntimeError(str(payload))
                yield payload
        finally:
            stop.set()
            if task.done():
                try:
                    task.exception()
                except Exception:
                    pass


class PocketChat:
    """只有在 --chat 下才存在；一次只跑一轮，别让手机并发叫醒她。"""

    def __init__(self, response_gate: Optional[ResponseGate] = None) -> None:
        self._chat = None
        self._response_gate = response_gate
        self._lock = asyncio.Lock()
        # 主动开口和用户对话都可能碰同一个 Chat / SQLite；两个入口共用一把
        # 线程锁，宁可让主动消息多等一秒，也不让两条线程同时写她的状态。
        self._worker_lock = threading.RLock()
        self.media = PocketMedia()

    def close(self) -> None:
        if self._chat is not None:
            try:
                self._chat.close()
            except Exception:
                pass
            self._chat = None
        try:
            self.media.close()
        except Exception:
            pass

    async def turn(self, text: str, *, audio_b64: str = "",
                   image_b64: str = "", event_id: str = "") -> dict[str, Any]:
        async with self._lock:
            return await asyncio.to_thread(
                self._turn_sync, text, audio_b64, image_b64, event_id)

    def _turn_sync(self, text: str, audio_b64: str, image_b64: str,
                   event_id: str = "") -> dict[str, Any]:
        with self._worker_lock:
            return self._turn_sync_locked(text, audio_b64, image_b64, event_id)

    def _prepare_media(self, text: str, audio_b64: str,
                       image_b64: str) -> dict[str, Any]:
        """先把手机送来的录音/照片变成一句能交给 Chat 的话。"""
        text = (text or "").strip()
        audio_raw = _b64decode(audio_b64, MAX_AUDIO_BYTES, "录音")
        image_raw = _b64decode(image_b64, MAX_IMAGE_BYTES, "照片")

        transcript = ""
        stt_seconds = 0.0
        if audio_raw:
            stt = self.media.transcribe_wav(audio_raw)
            transcript = str(stt.get("text") or "").strip()
            stt_seconds = float(stt.get("seconds") or 0.0)
            if not text:
                text = transcript

        vision = ""
        vision_seconds = 0.0
        if image_raw:
            seen = self.media.describe_image(image_raw, question=text)
            vision = str(seen.get("text") or "").strip()
            vision_seconds = float(seen.get("seconds") or 0.0)
            if not text:
                text = "看看这个。"

        return {
            "text": text, "channel": "voice" if audio_raw else "text",
            "vision": vision, "transcript": transcript,
            "stt_seconds": stt_seconds, "vision_seconds": vision_seconds,
        }

    def _public_result(self, res: dict[str, Any], prep: dict[str, Any]) -> dict[str, Any]:
        """手机只需要能看的字段，不把完整提示词/思维链丢进网络。"""
        out: dict[str, Any] = {"ok": bool(res.get("ok"))}
        for key in ("reply", "seconds", "birth", "thought", "expression", "state",
                    "memory", "silent", "mode", "reason", "event_id",
                    "response_gate", "llm_called", "assistant_message_created"):
            if key in res:
                out[key] = res[key]
        out["transcript"] = prep["transcript"]
        out["stt_seconds"] = prep["stt_seconds"]
        out["vision"] = prep["vision"]
        out["vision_seconds"] = prep["vision_seconds"]
        if not res.get("ok"):
            out["error"] = res.get("error") or "这一轮没有答上来"
        elif res.get("silent"):
            # 零输出是正常终态，不是错误；手机端据此不加空气泡、不触发 TTS。
            out["reply"] = ""
        return out

    def _turn_sync_locked(self, text: str, audio_b64: str,
                          image_b64: str, event_id: str = "") -> dict[str, Any]:
        prep = self._prepare_media(text, audio_b64, image_b64)
        if not prep["text"]:
            return {"ok": True, "empty": True, "reply": "",
                    "transcript": prep["transcript"],
                    "stt_seconds": prep["stt_seconds"],
                    "vision": prep["vision"],
                    "vision_seconds": prep["vision_seconds"]}

        if self._chat is None:
            from .chat import Chat
            self._chat = Chat(response_gate=self._response_gate)
        turn_kw: dict[str, Any] = {"scene": prep["vision"],
                                   "channel": prep["channel"]}
        if event_id:
            turn_kw["event_id"] = event_id
        res = self._chat.turn(prep["text"], **turn_kw)
        return self._public_result(res, prep)

    def _stream_sync(self, text: str, audio_b64: str,
                     image_b64: str, event_id: str = ""):
        """在后台线程里跑：先做 STT/视觉，再把 Chat 的流式事件往外送。"""
        prep = self._prepare_media(text, audio_b64, image_b64)
        yield {
            "type": "meta",
            "transcript": prep["transcript"],
            "stt_seconds": prep["stt_seconds"],
            "vision": prep["vision"],
            "vision_seconds": prep["vision_seconds"],
        }
        if not prep["text"]:
            yield {"type": "done", "result": {
                "ok": True, "empty": True, "reply": "",
                "transcript": prep["transcript"],
                "stt_seconds": prep["stt_seconds"],
                "vision": prep["vision"],
                "vision_seconds": prep["vision_seconds"],
            }}
            return

        chat = self._ensure_chat()
        streamer = getattr(chat, "turn_stream", None)
        if streamer is None:
            # 旧 Chat / 测试替身没有流式接口时，退回完整回复，但保持事件格式。
            res = chat.turn(prep["text"], scene=prep["vision"],
                            channel=prep["channel"],
                            **({"event_id": event_id} if event_id else {}))
            reply = str(res.get("reply") or "")
            if reply:
                yield {"type": "delta", "text": reply}
            yield {"type": "done", "result": self._public_result(res, prep)}
            return

        result: dict[str, Any] | None = None
        stream_kw: dict[str, Any] = {"scene": prep["vision"],
                                     "channel": prep["channel"]}
        if event_id:
            stream_kw["event_id"] = event_id
        for event in streamer(prep["text"], **stream_kw):
            kind = str(event.get("type") or "")
            if kind == "delta":
                text_piece = str(event.get("text") or "")
                if text_piece:
                    yield {"type": "delta", "text": text_piece}
            elif kind == "error":
                yield {"type": "error",
                       "error": str(event.get("error") or "这一轮没有答上来")}
                return
            elif kind == "done":
                raw = event.get("result") or {}
                if isinstance(raw, dict):
                    result = self._public_result(raw, prep)
        if result is None:
            yield {"type": "error", "error": "这一轮没有答完"}
            return
        yield {"type": "done", "result": result}

    async def stream_turn(self, text: str, *, audio_b64: str = "",
                          image_b64: str = "", event_id: str = ""):
        """异步流式入口：后台线程持有写锁，写到记忆的始终是完整回复。"""
        q: queue.Queue[Optional[dict[str, Any]]] = queue.Queue()

        def worker() -> None:
            try:
                with self._worker_lock:
                    for event in self._stream_sync(text, audio_b64, image_b64,
                                                   event_id):
                        q.put(event)
            except Exception as e:
                q.put({"type": "error", "error": f"{type(e).__name__}: {e}"})
            finally:
                q.put(None)

        threading.Thread(target=worker, name="pocket-chat-stream",
                         daemon=True).start()
        while True:
            event = await asyncio.to_thread(q.get)
            if event is None:
                return
            yield event

    def _ensure_chat(self):
        if self._chat is None:
            from .chat import Chat
            self._chat = Chat(response_gate=self._response_gate)
        return self._chat

    def snapshot(self) -> dict[str, Any]:
        """只读状态，不生成内容。"""
        with self._worker_lock:
            chat = self._ensure_chat()
            return {"state": chat.mind.snapshot(),
                    "memory": chat.mem.stats()}

    def proactive_seed(self) -> dict[str, Any]:
        with self._worker_lock:
            return self._ensure_chat().proactive_seed()

    def proactive(self, seed: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        with self._worker_lock:
            return self._ensure_chat().proactive(seed)


def _auth_middleware(auth: PocketAuth):
    @web.middleware
    async def middleware(request: web.Request, handler):
        if request.path.startswith("/api/") and request.path != "/api/health":
            if auth.locked(request):
                return _err("配对尝试过多，等一分钟再试", 429, "PAIR_LOCKED")
            if not auth.check(request):
                auth.fail(request)
                return _err("先输入配对码", 401, "PAIR_REQUIRED")
        response = await handler(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Frame-Options", "DENY")
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response
    return middleware


def create_app(store: PocketStore, *, auth: PocketAuth, token: str,
               demo: bool = False, readonly: bool = False,
               chat_enabled: bool = False, proactive_enabled: bool = False,
               proactive_store: Optional[ProactiveStore] = None,
               autonomy_enabled: bool = False,
               response_gate: Optional[ResponseGate] = None) -> web.Application:
    app = web.Application(middlewares=[_auth_middleware(auth)],
                          client_max_size=MAX_CHAT_BODY)
    app["store"] = store
    app["chat"] = PocketChat(response_gate=response_gate) if chat_enabled else None
    app["proactive_store"] = proactive_store
    app["meta"] = {"demo": demo, "readonly": readonly, "chat": chat_enabled,
                   "proactive": proactive_enabled, "autonomy": autonomy_enabled,
                   "response_gate": response_gate.mode if response_gate else "off",
                   "social_silence": bool(response_gate.social_silence)
                   if response_gate else False}
    # 日记是独立于对话的定时任务：只要常驻服务活着，23:00 就写；演示/只读不写真实目录。
    if not demo and not readonly:
        try:
            from .diary import start_scheduler
            start_scheduler()
        except Exception:
            pass

    async def read_json(request: web.Request,
                        max_body: int = MAX_BODY) -> dict[str, Any]:
        if request.content_length and request.content_length > max_body:
            raise web.HTTPRequestEntityTooLarge(max_size=max_body)
        try:
            raw = await request.text()
            data = json.loads(raw or "{}")
            if not isinstance(data, dict):
                raise ValueError
            return data
        except Exception:
            raise web.HTTPBadRequest(text=json.dumps(
                {"ok": False, "error": "请求不是合法 JSON", "code": "BAD_JSON"},
                ensure_ascii=False), content_type="application/json")

    async def index(_: web.Request) -> web.StreamResponse:
        return web.FileResponse(WEB_DIR / "index.html",
                                headers={"Cache-Control": "no-store"})

    async def manifest(_: web.Request) -> web.StreamResponse:
        return web.FileResponse(WEB_DIR / "manifest.webmanifest",
                                headers={"Content-Type": "application/manifest+json"})

    async def favicon(_: web.Request) -> web.StreamResponse:
        return web.FileResponse(WEB_DIR / "icons" / "icon-192.png")

    async def service_worker(_: web.Request) -> web.StreamResponse:
        return web.FileResponse(WEB_DIR / "sw.js",
                                headers={"Content-Type": "application/javascript; charset=utf-8",
                                         "Cache-Control": "no-cache"})

    async def android_apk(_: web.Request) -> web.StreamResponse:
        if not APK_FILE.is_file():
            return _err("APK 还没打包：先运行 robot\\app\\pocket_apk\\build-apk.cmd",
                        404, "APK_MISSING")
        return web.FileResponse(APK_FILE, headers={
            "Content-Type": "application/vnd.android.package-archive",
            "Content-Disposition": 'attachment; filename="valkyrie-pocket.apk"',
            "Cache-Control": "no-store",
        })

    async def health(request: web.Request) -> web.Response:
        st = store.stats()
        return _json({
            "ok": True, "app": "valkyrie-pocket", "version": APP_VERSION,
            "name": C.ROBOT_NAME, "time": _now_iso(),
            "auth": "required", "paired": auth.check(request),
            "demo": demo, "readonly": readonly, "chat": chat_enabled,
            "proactive": proactive_enabled, "autonomy": autonomy_enabled,
            "features": {"voice": chat_enabled, "camera": chat_enabled,
                         "tts": chat_enabled, "proactive": proactive_enabled,
                         "meaning": True, "persona": True, "p3": True,
                         "response_gate": bool(chat_enabled)},
            "response_gate": {
                "mode": (response_gate.mode if response_gate else "off"),
                "social_silence": (bool(response_gate.social_silence)
                                   if response_gate else False),
            },
            "memory": {"episodes": st["episodes"], "active": st["active"],
                       "facts": st["facts"]},
        })

    async def state(_: web.Request) -> web.Response:
        try:
            data = await asyncio.to_thread(store.state)
            data["chat"] = chat_enabled
            data["readonly"] = readonly
            data["demo"] = demo
            data["voices"] = _voice_choices()
            data["voice_default"] = C.BREEZE_VOICE_ID
            return _json({"ok": True, **data})
        except Exception as e:
            return _err(f"读不到她的状态：{type(e).__name__}: {e}", 500, "STATE_FAILED")

    async def meaning_list(request: web.Request) -> web.Response:
        try:
            limit = max(1, min(50, int(request.query.get("limit", "24"))))
        except ValueError:
            return _err("limit 必须是整数")
        try:
            data = await asyncio.to_thread(store.meaning_payload, limit)
            return _json({"ok": True, **data})
        except Exception as e:
            return _err(f"读意义失败：{type(e).__name__}: {e}", 500, "READ_FAILED")

    async def persona_view(_: web.Request) -> web.Response:
        try:
            data = await asyncio.to_thread(store.persona_payload)
            return _json(data)
        except Exception as e:
            return _err(f"读人格失败：{type(e).__name__}: {e}", 500, "READ_FAILED")

    async def p3_view(_: web.Request) -> web.Response:
        try:
            data = await asyncio.to_thread(store.p3_payload)
            return _json(data)
        except Exception as e:
            return _err(f"读关系气候失败：{type(e).__name__}: {e}",
                        500, "READ_FAILED")

    async def memory_list(request: web.Request) -> web.Response:
        try:
            limit = max(1, min(100, int(request.query.get("limit", "40"))))
            offset = max(0, min(50000, int(request.query.get("offset", "0"))))
        except ValueError:
            return _err("limit/offset 必须是整数")
        try:
            data = await asyncio.to_thread(
                store.list_episodes,
                query=request.query.get("q", ""),
                limit=limit, offset=offset,
                status=request.query.get("status", "active"),
                region=request.query.get("region", ""),
            )
            return _json({"ok": True, **data})
        except Exception as e:
            return _err(f"读记忆失败：{type(e).__name__}: {e}", 500, "READ_FAILED")

    async def memory_get(request: web.Request) -> web.Response:
        try:
            eid = int(request.match_info["id"])
        except ValueError:
            return _err("记忆 id 不合法")
        try:
            item = await asyncio.to_thread(store.get_episode, eid)
            if item is None:
                return _err("没有这条记忆", 404, "NOT_FOUND")
            return _json({"ok": True, "item": item})
        except Exception as e:
            return _err(f"读记忆失败：{type(e).__name__}: {e}", 500, "READ_FAILED")

    async def memory_add(request: web.Request) -> web.Response:
        try:
            body = await read_json(request)
        except web.HTTPException as e:
            return e
        try:
            res = await asyncio.to_thread(store.add_episode, body)
            return _json({"ok": True, **res}, 201)
        except PermissionError:
            return _err("服务是只读模式", 403, "READONLY")
        except ValueError as e:
            return _err(str(e), 400, "INVALID")
        except Exception as e:
            return _err(f"写入失败：{type(e).__name__}: {e}", 500, "WRITE_FAILED")

    async def memory_correct(request: web.Request) -> web.Response:
        try:
            eid = int(request.match_info["id"])
            body = await read_json(request)
            res = await asyncio.to_thread(store.correct_episode, eid, body)
            return _json({"ok": True, **res})
        except web.HTTPException as e:
            return e
        except PermissionError:
            return _err("服务是只读模式", 403, "READONLY")
        except LookupError as e:
            return _err(str(e), 404, "NOT_FOUND")
        except ValueError as e:
            return _err(str(e), 400, "INVALID")
        except Exception as e:
            return _err(f"更正失败：{type(e).__name__}: {e}", 500, "WRITE_FAILED")

    async def memory_retract(request: web.Request) -> web.Response:
        try:
            eid = int(request.match_info["id"])
            body = await read_json(request)
            res = await asyncio.to_thread(store.retract_episode, eid, body)
            return _json({"ok": True, **res})
        except web.HTTPException as e:
            return e
        except PermissionError:
            return _err("服务是只读模式", 403, "READONLY")
        except LookupError as e:
            return _err(str(e), 404, "NOT_FOUND")
        except Exception as e:
            return _err(f"撤回失败：{type(e).__name__}: {e}", 500, "WRITE_FAILED")

    async def pre_answer_feedback(request: web.Request) -> web.Response:
        try:
            limit = max(1, min(50, int(request.query.get("limit", "20"))))
        except ValueError:
            return _err("limit 必须是整数")
        try:
            data = await asyncio.to_thread(store.pre_answer_feedback, limit)
            return _json({"ok": True, **data})
        except Exception as e:
            return _err(f"读反馈失败：{type(e).__name__}: {e}", 500, "READ_FAILED")

    async def pre_answer_feedback_retract(request: web.Request) -> web.Response:
        try:
            feedback_id = str(request.match_info["id"])
            body = await read_json(request)
            res = await asyncio.to_thread(
                store.retract_pre_answer_feedback, feedback_id,
                str(body.get("reason") or ""))
            if not res.get("ok"):
                reason = str(res.get("reason") or "not_found")
                message = {
                    "not_found": "没有这条反馈",
                    "not_pre_answer": "这不是回答前反馈",
                    "already_retracted": "这次误判已经撤回过了",
                    "pending": "这条还没结算，不能撤回",
                    "not_retractable": "这条反馈不能撤回",
                }.get(reason, reason)
                return _err(message, 409, reason.upper())
            return _json({"ok": True, **res})
        except web.HTTPException as e:
            return e
        except PermissionError:
            return _err("服务是只读模式", 403, "READONLY")
        except LookupError as e:
            return _err(str(e), 404, "NOT_FOUND")
        except ValueError as e:
            return _err(str(e), 400, "INVALID")
        except Exception as e:
            return _err(f"撤回失败：{type(e).__name__}: {e}", 500, "WRITE_FAILED")

    async def facts(request: web.Request) -> web.Response:
        try:
            return _json({"ok": True, "items": await asyncio.to_thread(store.list_facts)})
        except Exception as e:
            return _err(f"读事实失败：{type(e).__name__}: {e}", 500, "READ_FAILED")

    async def fact_add(request: web.Request) -> web.Response:
        try:
            body = await read_json(request)
            res = await asyncio.to_thread(store.add_fact, body)
            return _json({"ok": True, **res}, 201)
        except web.HTTPException as e:
            return e
        except PermissionError:
            return _err("服务是只读模式", 403, "READONLY")
        except ValueError as e:
            return _err(str(e), 400, "INVALID")
        except Exception as e:
            return _err(f"写入失败：{type(e).__name__}: {e}", 500, "WRITE_FAILED")

    async def fact_delete(request: web.Request) -> web.Response:
        key = request.match_info["key"]
        if request.query.get("confirm") != "DELETE":
            return _err("删除事实要带 confirm=DELETE", 400, "CONFIRM_REQUIRED")
        try:
            deleted = await asyncio.to_thread(store.delete_fact, key)
            if not deleted:
                return _err("没有这条事实", 404, "NOT_FOUND")
            return _json({"ok": True, "deleted": key})
        except PermissionError:
            return _err("服务是只读模式", 403, "READONLY")
        except Exception as e:
            return _err(f"删除失败：{type(e).__name__}: {e}", 500, "WRITE_FAILED")

    async def export(_: web.Request) -> web.Response:
        try:
            data = await asyncio.to_thread(store.export)
            return _json(data)
        except Exception as e:
            return _err(f"导出失败：{type(e).__name__}: {e}", 500, "EXPORT_FAILED")

    async def chat(request: web.Request) -> web.Response:
        if not chat_enabled or app["chat"] is None:
            return _err("对话未开。用 talk.cmd 启动，并且发送第一句会叫醒她。",
                        403, "CHAT_DISABLED")
        try:
            body = await read_json(request, max_body=MAX_CHAT_BODY)
        except web.HTTPException as e:
            return e
        text = str(body.get("text") or "").strip()
        audio = body.get("audio") or ""
        image = body.get("image") or ""
        event_id = str(body.get("event_id") or "").strip()
        if not text and not audio and not image:
            return _err("说一句再发", 400, "EMPTY")
        if len(text) > 2000:
            return _err("这一句太长了", 400, "TOO_LONG")
        try:
            res = await app["chat"].turn(
                text, audio_b64=str(audio), image_b64=str(image),
                event_id=event_id)
            return _json({"ok": True, **res})
        except ValueError as e:
            return _err(str(e), 400, "BAD_MEDIA")
        except Exception as e:
            return _err(f"这一轮没有答上来：{type(e).__name__}: {e}",
                        500, "CHAT_FAILED")

    async def chat_stream(request: web.Request) -> web.StreamResponse:
        """边生成边发：手机先看见字，第一句合成完就开口，不等整段回复。"""
        if not chat_enabled or app["chat"] is None:
            return _err("对话未开。用 talk.cmd 启动，并且发送第一句会叫醒她。",
                        403, "CHAT_DISABLED")
        try:
            body = await read_json(request, max_body=MAX_CHAT_BODY)
        except web.HTTPException as e:
            return e
        text = str(body.get("text") or "").strip()
        audio = body.get("audio") or ""
        image = body.get("image") or ""
        event_id = str(body.get("event_id") or "").strip()
        if not text and not audio and not image:
            return _err("说一句再发", 400, "EMPTY")
        if len(text) > 2000:
            return _err("这一句太长了", 400, "TOO_LONG")

        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream; charset=utf-8",
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
            },
        )
        await response.prepare(request)
        try:
            async for event in app["chat"].stream_turn(
                    text, audio_b64=str(audio), image_b64=str(image),
                    event_id=event_id):
                await response.write(_sse(event))
        except asyncio.CancelledError:
            raise
        except (ConnectionResetError, BrokenPipeError):
            pass
        except Exception as e:
            try:
                await response.write(_sse(
                    {"type": "error",
                     "error": f"这一轮没有答上来：{type(e).__name__}: {e}"}))
            except Exception:
                pass
        finally:
            try:
                await response.write_eof()
            except Exception:
                pass
        return response

    async def tts(request: web.Request) -> web.Response:
        if not chat_enabled or app["chat"] is None:
            return _err("对话未开，没有她的声音。", 403, "CHAT_DISABLED")
        try:
            body = await read_json(request)
        except web.HTTPException as e:
            return e
        text = str(body.get("text") or "").strip()
        if not text:
            return _err("没有要读的字", 400, "EMPTY")
        if len(text) > 600:
            return _err("这句话太长，先拆开", 400, "TOO_LONG")
        try:
            voice_id = _requested_voice(body.get("voice"))
        except ValueError:
            return _err("没有这个音色", 400, "BAD_VOICE")
        try:
            res = await asyncio.to_thread(
                app["chat"].media.synthesize, text, voice_id)
            return _json({"ok": True, **res})
        except Exception as e:
            return _err(f"这声没出来：{type(e).__name__}: {e}",
                        503, "TTS_FAILED")

    async def tts_stream(request: web.Request) -> web.StreamResponse:
        """流式 TTS：先等第一块 PCM，拿到后才挂响应头，手机边收边播。

        不能等整段 mp3 再 base64 回手机；首块一到就开 HTTP，后面的块
        继续写。首块前失败仍能返回正常 JSON 错误，手机可退回旧接口。
        """
        if not chat_enabled or app["chat"] is None:
            return _err("对话未开，没有她的声音。", 403, "CHAT_DISABLED")
        try:
            body = await read_json(request)
        except web.HTTPException as e:
            return e
        text = str(body.get("text") or "").strip()
        if not text:
            return _err("没有要读的字", 400, "EMPTY")
        if len(text) > 600:
            return _err("这句话太长，先拆开", 400, "TOO_LONG")
        try:
            voice_id = _requested_voice(body.get("voice"))
        except ValueError:
            return _err("没有这个音色", 400, "BAD_VOICE")

        agen = app["chat"].media.stream_pcm(text, voice_id)
        try:
            first = await anext(agen)
        except StopAsyncIteration:
            await agen.aclose()
            return _err("这声没出来：TTS 没有返回音频", 503, "TTS_EMPTY")
        except Exception as e:
            await agen.aclose()
            return _err(f"这声没出来：{type(e).__name__}: {e}",
                        503, "TTS_FAILED")

        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "audio/pcm; rate=24000",
                "X-Sample-Rate": "24000",
                "X-Channels": "1",
                "X-Audio-Format": "s16le",
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
            },
        )
        try:
            await response.prepare(request)
            await response.write(first)
            async for chunk in agen:
                await response.write(chunk)
            await response.write_eof()
        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
            pass
        finally:
            try:
                await agen.aclose()
            except Exception:
                pass
        return response

    async def proactive_list(request: web.Request) -> web.Response:
        """手机取她主动发来的消息。没有 since_id 就只给游标，不补旧消息。"""
        pstore = app.get("proactive_store")
        if not proactive_enabled or pstore is None:
            return _json({"ok": True, "enabled": False, "items": [],
                          "last_id": 0, "server_time": time.time()})
        try:
            since_id = max(0, int(request.query.get("since_id", "0")))
            limit = max(1, min(20, int(request.query.get("limit", "10"))))
        except ValueError:
            return _err("since_id/limit 必须是整数")
        try:
            last = await asyncio.to_thread(pstore.last)
            last_id = int((last or {}).get("id") or 0)
            items: list[dict[str, Any]] = []
            bootstrap = request.query.get("bootstrap") == "1"
            if since_id > 0:
                items = await asyncio.to_thread(pstore.since, since_id, limit)
            elif bootstrap:
                # 手机上第一次打开页面：只给页面看最近几条，让通知点进来时
                # 能看见她说了什么；Android 常驻服务不会带 bootstrap，避免补旧通知。
                items = await asyncio.to_thread(
                    pstore.latest, min(3, limit), 24.0)
            if items:
                last_id = int(items[-1]["id"])
            return _json({"ok": True, "enabled": True, "items": items,
                          "last_id": last_id, "server_time": time.time(),
                          "stats": await asyncio.to_thread(pstore.stats)})
        except Exception as e:
            return _err(f"取主动消息失败：{type(e).__name__}: {e}",
                        500, "PROACTIVE_FAILED")

    app.router.add_get("/", index)
    app.router.add_get("/manifest.webmanifest", manifest)
    app.router.add_get("/favicon.ico", favicon)
    app.router.add_get("/sw.js", service_worker)
    app.router.add_get("/download/valkyrie.apk", android_apk)
    app.router.add_static("/assets", WEB_DIR / "assets", name="assets")
    app.router.add_static("/icons", WEB_DIR / "icons", name="icons")
    app.router.add_get("/api/health", health)
    app.router.add_get("/api/state", state)
    app.router.add_get("/api/meaning", meaning_list)
    app.router.add_get("/api/persona", persona_view)
    app.router.add_get("/api/p3", p3_view)
    app.router.add_get("/api/proactive", proactive_list)
    app.router.add_get("/api/memory", memory_list)
    app.router.add_get("/api/memory/{id:\\d+}", memory_get)
    app.router.add_post("/api/memory", memory_add)
    app.router.add_post("/api/memory/{id:\\d+}/correct", memory_correct)
    app.router.add_post("/api/memory/{id:\\d+}/retract", memory_retract)
    app.router.add_get("/api/pre-answer-feedback", pre_answer_feedback)
    app.router.add_post("/api/pre-answer-feedback/{id}/retract",
                        pre_answer_feedback_retract)
    app.router.add_get("/api/facts", facts)
    app.router.add_post("/api/facts", fact_add)
    app.router.add_delete("/api/facts/{key}", fact_delete)
    app.router.add_get("/api/export", export)
    app.router.add_post("/api/chat", chat)
    app.router.add_post("/api/chat/stream", chat_stream)
    app.router.add_post("/api/tts", tts)
    app.router.add_post("/api/tts/stream", tts_stream)
    return app


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="app.pocket_server",
        description="瓦尔基里 · 水镜：手机端的识海/状态服务")
    ap.add_argument("--host", default="127.0.0.1",
                    help="监听地址；默认只给本机，手机连不上时用 --lan")
    ap.add_argument("--lan", action="store_true",
                    help="监听 0.0.0.0，让同一 Wi-Fi / USB 网络共享的手机访问")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--demo", action="store_true", help="示例库，不碰真实 memory.db")
    ap.add_argument("--readonly", action="store_true", help="只读：不允许新增/更正/撤回")
    ap.add_argument("--chat", action="store_true",
                    help="启用对话；发送第一句会叫醒她，不开就只看记忆")
    ap.add_argument("--school", action="store_true",
                    help="随服务运行学校常驻：开学后每天每 5 分钟一节")
    ap.add_argument("--autonomy", action="store_true",
                    help="随服务运行自主自习：按她的情绪/好奇自己找材料")
    ap.add_argument("--proactive", action="store_true",
                    help="让她主动发消息：手机轮询/Android 通知会收到")
    ap.add_argument("--token", default="", help="固定配对码；默认沿用/生成 data/pocket_token.txt")
    ap.add_argument("--response-gate", choices=("off", "shadow", "enforced"),
                    default=None,
                    help="自然回应闸门：off 保持原行为，shadow 只记录，enforced 执行")
    ap.add_argument("--social-silence", choices=("off", "on"), default=None,
                    help="社交性沉默（自然结束/无新价值）；默认跟随 ROBOT_SOCIAL_SILENCE")
    ap.add_argument("--merge-window-ms", type=int, default=None,
                    help="连续输入合并窗口（毫秒）；只影响 defer 适配，不影响固定秒数")
    a = ap.parse_args(argv)

    if (a.chat or a.school or a.autonomy or a.proactive) and (
            a.demo or a.readonly):
        print("--chat / --school / --autonomy / --proactive 不能和 --demo / "
              "--readonly 一起用：示例和只读都不该叫醒她或写她的库。")
        return 2
    if (a.autonomy or a.proactive) and not a.chat:
        print("--autonomy / --proactive 需要同时开 --chat：她要先能被叫醒，"
              "才谈得上自己学、自己找你。")
        return 2

    if a.lan:
        a.host = "0.0.0.0"

    token = (a.token or os.environ.get("VALKYRIE_APP_TOKEN", "")).strip()
    if not token:
        token = _read_token(TOKEN_FILE)
    if not token:
        token = _new_token()
        _write_token(TOKEN_FILE, token)
    elif a.token or not TOKEN_FILE.exists():
        _write_token(TOKEN_FILE, token)

    db = DEMO_DB if a.demo else DB_PATH
    gate_mode = (a.response_gate or C.RESPONSE_GATE_MODE or "off").strip().lower()
    if gate_mode not in ("off", "shadow", "enforced"):
        gate_mode = "off"
    social_silence = (C.SOCIAL_SILENCE if a.social_silence is None
                      else a.social_silence == "on")
    merge_ms = (C.REPLY_MERGE_WINDOW_MS if a.merge_window_ms is None
                else a.merge_window_ms)
    response_gate = ResponseGate(
        mode=gate_mode, social_silence=social_silence,
        merge_window_ms=merge_ms,
        state_path=C.RESPONSE_GATE_STATE) if a.chat else None
    try:
        store = PocketStore(db, writable=not a.readonly, demo=a.demo)
    except Exception as e:
        print(f"打不开记忆库 {db}：{type(e).__name__}: {e}")
        return 1
    auth = PocketAuth(token)
    pstore = ProactiveStore() if a.proactive else None
    app = create_app(store, auth=auth, token=token, demo=a.demo,
                     readonly=a.readonly, chat_enabled=a.chat,
                     proactive_enabled=a.proactive, proactive_store=pstore,
                     autonomy_enabled=a.autonomy, response_gate=response_gate)

    print(f"\n{APP_NAME}  v{APP_VERSION}")
    print(f"数据    {db}  {'（示例）' if a.demo else '（真实）'}"
          f"{'  · 只读' if a.readonly else ''}")
    print(f"对话    {'开：发送第一句会叫醒她' if a.chat else '关：只看记忆，不叫醒她'}")
    if a.chat:
        print(f"回应    {gate_mode} · 社交沉默 {'开' if social_silence else '关'}")
    if a.school:
        print(f"学校    开：开学日 {C.SCHOOL_START_DATE or '立即'}，"
              f"每天 {C.SCHOOL_LESSONS_PER_DAY} 节 × "
              f"{C.SCHOOL_LESSON_SECONDS // 60} 分钟；课程以像人说话/思考为主")
    else:
        print("学校    关：只跑手动 school.cmd")
    if a.autonomy:
        print(f"自学    开：每约 {C.AUTONOMY_INTERVAL_MINUTES} 分钟看她想不想学")
    else:
        print("自学    关")
    if a.proactive:
        print("主动    开：不设固定节律，她想说就说")
    else:
        print("主动    关")
    print(f"配对码  {token}  （手机第一次打开要输入）")
    local = f"http://127.0.0.1:{a.port}/?token={token}"
    print(f"本机    {local}")
    if a.lan:
        for ip in _ipv4s():
            print(f"局域网  http://{ip}:{a.port}/?token={token}")
    else:
        print("手机    加 --lan 才会监听局域网；USB 调试用 usb.cmd 做反向转发。")
    print("\nCtrl+C 停止。\n")

    if a.school:
        from .school import School
        school = School()

        def _school_worker() -> None:
            try:
                school.run_loop()
            finally:
                school.close()

        threading.Thread(target=_school_worker, name="school-loop",
                         daemon=True).start()

    stop_proactive = threading.Event()
    stop_autonomy = threading.Event()

    if a.proactive:
        def _proactive_worker() -> None:
            # 不设节律：不限次数、不限间隔、不设夜间禁发。
            # 每 20 秒只看一眼总开关；何时开口由 Chat 自己的由头决定，
            # 没有由头就继续等。
            while not stop_proactive.wait(20):
                try:
                    gate = can_send()
                    if not gate.get("ok"):
                        continue
                    seed = app["chat"].proactive_seed()
                    if not seed.get("ok"):
                        continue
                    key = str(seed.get("seed") or "")
                    if key and key in pstore.recent_seeds(48):
                        continue
                    res = app["chat"].proactive(seed)
                    if not res.get("ok") or not res.get("text"):
                        continue
                    rec = pstore.record(res["text"], kind=res.get("kind") or "ask",
                                        seed=key)
                    print(f"[{datetime.now():%m-%d %H:%M}] 她先开口："
                          f"{rec['text']}", flush=True)
                except Exception as e:
                    print(f"[{datetime.now():%m-%d %H:%M}] 主动消息失败："
                          f"{type(e).__name__}: {e}", flush=True)

        threading.Thread(target=_proactive_worker, name="proactive-loop",
                         daemon=True).start()

    if a.autonomy:
        from .autonomy import Autonomy
        from .learning import Learning
        from .memory import Memory as _Memory
        from .mind import Mind as _Mind

        au_learning = Learning()
        au_mem = _Memory()
        au_mind = _Mind()
        au = Autonomy(learning=au_learning, mem=au_mem, mind=au_mind)

        def _autonomy_worker() -> None:
            try:
                au.run_forever(stop_event=stop_autonomy)
            finally:
                try:
                    au_learning.close()
                except Exception:
                    pass
                try:
                    au_mem.close()
                except Exception:
                    pass
                try:
                    au_mind.close()
                except Exception:
                    pass

        threading.Thread(target=_autonomy_worker, name="autonomy-loop",
                         daemon=True).start()

    try:
        web.run_app(app, host=a.host, port=a.port,
                    print=lambda *args, **kwargs: None,
                    access_log=None, shutdown_timeout=3.0)
    finally:
        stop_proactive.set()
        stop_autonomy.set()
        if pstore is not None:
            pstore.close()
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
