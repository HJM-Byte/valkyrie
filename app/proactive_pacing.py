"""主动消息节奏控制（Proactive Pacing）。

这一层只决定「现在有没有发送权」，不生成文本、不发送、不写记忆/关系/情绪。
它接在现有主动候选之后、真实模型调用之前；所有非必要主动消息共享同一份
节奏状态，手机 / 网页 / 桌面 / ESP32 不能各自领一份额度。

三种模式：
- off：完全不改变原有行为；不建表、不写日志。
- shadow：只记录“会允许 / 会抑制”，真实发送仍按原行为，真实节奏状态不变。
- enforced：按等级、安静、间隔、日上限、宽限和退避拦截；发送后原子结算。

边界：
- 必要事项（到期承诺/提醒、安全、用户明确要求）不占非必要额度，但仍受安静
  和停止状态约束。
- 未回复只保护注意力，不改关系、亲密度、情绪或人格。
- 被拦截的候选不进入 backlog，不补发，不换通道。
- 状态不可用时，非必要候选宁可不发；不阻塞 Chat。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from . import config as C

MODES = ("off", "shadow", "enforced")
LEVELS = ("essential", "low", "normal")
NONESSENTIAL_INTENTS = frozenset(
    ("share", "continue", "react", "care", "invite", "play", "check_in", "remind"))
LOW_INTENTS = frozenset(("share", "continue"))
STATE_KEYS = (
    "timezone", "last_nonessential_sent_at", "day_key", "day_count",
    "unanswered_count", "backoff_until", "post_user_grace_until",
    "last_user_input_at", "reservation_id", "reservation_plan_id",
    "reservation_at", "reservation_expires", "reservation_context",
    "updated_at",
)
_SCHEMA = """
CREATE TABLE IF NOT EXISTS pacing_state(
    id INTEGER PRIMARY KEY CHECK(id = 1),
    timezone TEXT NOT NULL DEFAULT '',
    last_nonessential_sent_at REAL NOT NULL DEFAULT 0,
    day_key TEXT NOT NULL DEFAULT '',
    day_count INTEGER NOT NULL DEFAULT 0,
    unanswered_count INTEGER NOT NULL DEFAULT 0,
    backoff_until REAL NOT NULL DEFAULT 0,
    post_user_grace_until REAL NOT NULL DEFAULT 0,
    last_user_input_at REAL NOT NULL DEFAULT 0,
    reservation_id TEXT NOT NULL DEFAULT '',
    reservation_plan_id TEXT NOT NULL DEFAULT '',
    reservation_at REAL NOT NULL DEFAULT 0,
    reservation_expires REAL NOT NULL DEFAULT 0,
    reservation_context TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO pacing_state(id) VALUES(1);
"""
_LOG_LOCK = threading.Lock()
_SHADOW_LOGGED: dict[str, float] = {}


def _now(now: Optional[float] = None) -> float:
    return time.time() if now is None else float(now)


def _clean(value: Any, n: int = 120) -> str:
    return " ".join(str(value or "").split()).strip()[:max(1, int(n))]


def _hash(text: Any, n: int = 20) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8", "replace")).hexdigest()[:n]


def _iso(ts: float) -> str:
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(float(ts)).astimezone().isoformat(
            timespec="seconds")
    except (TypeError, ValueError, OSError):
        return ""


def _parse_iso(value: Any) -> float:
    if not value:
        return 0.0
    raw = str(value).strip()
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, OSError):
        return 0.0


def _day_key(ts: Optional[float] = None) -> str:
    return datetime.fromtimestamp(_now(ts)).astimezone().strftime("%Y-%m-%d")


def _tz_name() -> str:
    tz = datetime.now().astimezone().tzinfo
    name = getattr(tz, "key", "")
    if not name:
        try:
            name = tz.tzname(None) if tz is not None else ""
        except Exception:
            name = ""
    return str(name or tz or "local")[:80]


def _cfg_int(attr: str, default: int, lo: int, hi: int) -> int:
    """配置值只从 config 模块读；测试可直接改 C.*，不再被环境变量盖住。"""
    try:
        value = int(getattr(C, attr, default) or default)
    except (TypeError, ValueError):
        value = default
    return max(lo, min(hi, value))


def mode(explicit: Optional[str] = None) -> str:
    raw = (explicit if explicit is not None else
           getattr(C, "PROACTIVE_PACING_MODE", "off"))
    value = str(raw or "off").strip().lower()
    return value if value in MODES else "off"


def level(explicit: Optional[str] = None) -> str:
    raw = (explicit if explicit is not None else
           getattr(C, "PROACTIVE_LEVEL", "low"))
    value = str(raw or "low").strip().lower()
    return value if value in LEVELS else "low"


def settings() -> dict[str, Any]:
    """返回经过上下界收敛的节奏参数；不写任何文件。"""
    return {
        "mode": mode(),
        "level": level(),
        "min_interval_minutes": _cfg_int(
            "PROACTIVE_MIN_INTERVAL_MINUTES", 180, 1, 10080),
        "daily_ceiling": _cfg_int(
            "PROACTIVE_NONESSENTIAL_DAILY_CEILING", 3, 0, 100),
        "post_user_grace_minutes": _cfg_int(
            "PROACTIVE_POST_USER_GRACE_MINUTES", 60, 0, 1440),
        "first_unanswered_backoff_minutes": _cfg_int(
            "PROACTIVE_FIRST_UNANSWERED_BACKOFF_MINUTES", 720, 1, 10080),
        "repeated_unanswered_backoff_minutes": _cfg_int(
            "PROACTIVE_REPEATED_UNANSWERED_BACKOFF_MINUTES", 1440, 1, 20160),
        "reservation_ttl_s": _cfg_int(
            "PROACTIVE_PACING_RESERVATION_TTL_S", 120, 15, 1800),
        "log": bool(getattr(C, "PROACTIVE_PACING_LOG", True)),
        "dir": Path(getattr(C, "PROACTIVE_PACING_DIR",
                            C.DATA / "proactive_pacing")),
        "timezone": _tz_name(),
    }


def classify(seed: Optional[dict]) -> dict[str, Any]:
    """把现有主动种子分成必要/非必要，并抽出确定性的节奏字段。"""
    data = dict(seed or {})
    plan = data.get("plan")
    if hasattr(plan, "as_dict"):
        plan = plan.as_dict()
    plan = dict(plan or {}) if isinstance(plan, dict) else {}
    kind = _clean(data.get("kind"), 30)
    intent = _clean(plan.get("intent") or data.get("intent"), 40)
    if not intent:
        intent = {"promise": "remind", "ask": "ask", "greet": "greet"}.get(
            kind, kind or "unknown")
    sources = [str(x) for x in (plan.get("source_types") or []) if str(x)]
    evidence = _clean(plan.get("evidence_state"), 30)
    new_value = _clean(plan.get("new_value_code"), 50)
    essential = False
    essential_reason = ""
    if kind == "promise":
        essential, essential_reason = True, "promise"
    elif bool(data.get("essential")):
        essential, essential_reason = True, "explicit"
    elif kind == "topic" and intent == "remind" and "commitment" in sources:
        essential, essential_reason = True, "commitment_reminder"
    expires = _parse_iso(plan.get("expires_at"))
    return {
        "kind": kind,
        "intent": intent,
        "source_types": sources,
        "evidence_state": evidence,
        "new_value_code": new_value,
        "essential": essential,
        "essential_reason": essential_reason,
        "expires_at": expires,
        "candidate_key": _clean(
            plan.get("plan_id") or data.get("seed") or data.get("plan_id")
            or f"{kind}:{intent}", 100),
        "novelty_key": _clean(plan.get("novelty_key"), 100),
        "why_now_code": _clean(plan.get("why_now_code"), 60),
        "sources_count": len(sources),
    }


def _fresh_state(now: Optional[float] = None) -> dict[str, Any]:
    at = _now(now)
    return {
        "timezone": _tz_name(),
        "last_nonessential_sent_at": 0.0,
        "day_key": _day_key(at),
        "day_count": 0,
        "unanswered_count": 0,
        "backoff_until": 0.0,
        "post_user_grace_until": 0.0,
        "last_user_input_at": 0.0,
        "reservation_id": "",
        "reservation_plan_id": "",
        "reservation_at": 0.0,
        "reservation_expires": 0.0,
        "reservation_context": "",
        "updated_at": at,
    }


def _state_copy(state: Optional[dict]) -> dict[str, Any]:
    out = _fresh_state()
    for key in STATE_KEYS:
        if isinstance(state, dict) and key in state:
            out[key] = state[key]
    return out


def _normalized(state: dict, now: float) -> dict[str, Any]:
    """按当前本地自然日归一，并清掉已经超时的预留（只在写入时落盘）。"""
    out = _state_copy(state)
    today = _day_key(now)
    if str(out.get("day_key") or "") != today:
        out["day_key"] = today
        out["day_count"] = 0
    if (out.get("reservation_id")
            and float(out.get("reservation_expires") or 0) <= now):
        out["reservation_id"] = ""
        out["reservation_plan_id"] = ""
        out["reservation_at"] = 0.0
        out["reservation_expires"] = 0.0
        out["reservation_context"] = ""
    out["timezone"] = _tz_name()
    return out


def _value_ok(candidate: dict, level_name: str) -> bool:
    intent = str(candidate.get("intent") or "")
    if level_name == "normal":
        return intent in NONESSENTIAL_INTENTS or intent in ("ask", "greet")
    if level_name != "low":
        return False
    new_value = str(candidate.get("new_value_code") or "")
    sources = set(candidate.get("source_types") or [])
    evidence = str(candidate.get("evidence_state") or "")
    if intent == "share":
        return (evidence == "eligible" and "learning_result" in sources
                and new_value in ("new_information", "new_understanding"))
    if intent == "continue":
        return ("shared_event" in sources
                and new_value == "old_topic_progress"
                and evidence in ("eligible", "contextual_only"))
    return False


def decide(candidate: Optional[dict], state: Optional[dict], *,
           quiet: bool = False, paused: bool = False,
           now: Optional[float] = None, mode_name: Optional[str] = None,
           level_name: Optional[str] = None) -> dict[str, Any]:
    """纯函数版闸门；不写状态、不写日志。返回稳定理由，便于重放。"""
    at = _now(now)
    m = mode(mode_name)
    lv = level(level_name)
    cand = dict(candidate or {})
    st = _normalized(state or _fresh_state(at), at)
    base = {"mode": m, "level": lv, "essential": bool(cand.get("essential")),
            "intent": str(cand.get("intent") or ""),
            "candidate_key": str(cand.get("candidate_key") or "")}
    if m == "off":
        return {**base, "allowed": True, "result": "allow", "reason": "gate_off"}
    if quiet:
        return {**base, "allowed": False, "result": "suppress", "reason": "quiet"}
    if paused:
        return {**base, "allowed": False, "result": "suppress",
                "reason": "pause_stated"}
    if bool(cand.get("essential")):
        return {**base, "allowed": True, "result": "allow",
                "reason": str(cand.get("essential_reason") or "essential")}
    if lv == "essential":
        return {**base, "allowed": False, "result": "suppress",
                "reason": "level_essential"}
    expires = float(cand.get("expires_at") or 0.0)
    if expires and expires <= at:
        return {**base, "allowed": False, "result": "suppress", "reason": "expired"}
    intent = str(cand.get("intent") or "")
    if lv == "low" and intent not in LOW_INTENTS:
        return {**base, "allowed": False, "result": "suppress",
                "reason": "intent_not_allowed"}
    if not _value_ok(cand, lv):
        return {**base, "allowed": False, "result": "suppress",
                "reason": "low_value"}
    grace = float(st.get("post_user_grace_until") or 0.0)
    if grace > at:
        return {**base, "allowed": False, "result": "suppress",
                "reason": "user_grace"}
    s = settings()
    day_count = int(st.get("day_count") or 0)
    if day_count >= int(s["daily_ceiling"]):
        return {**base, "allowed": False, "result": "suppress",
                "reason": "daily_ceiling"}
    backoff = float(st.get("backoff_until") or 0.0)
    if backoff > at:
        return {**base, "allowed": False, "result": "suppress",
                "reason": "backoff"}
    last_sent = float(st.get("last_nonessential_sent_at") or 0.0)
    if last_sent and at < last_sent + int(s["min_interval_minutes"]) * 60:
        return {**base, "allowed": False, "result": "suppress",
                "reason": "min_interval"}
    return {**base, "allowed": True, "result": "allow", "reason": "allowed"}


class PacingStore:
    """节奏状态的唯一入口；enforced 使用 proactive.db 的短事务原子预留。"""

    def __init__(self, path: Optional[Path | str] = None, *,
                 mode_name: Optional[str] = None,
                 level_name: Optional[str] = None,
                 now_fn=time.time,
                 log_path: Optional[Path | str] = None) -> None:
        self.mode = mode(mode_name)
        self.level = level(level_name)
        self.path = Path(path) if path else None
        self._now = now_fn
        self._lock = threading.RLock()
        self._db: Optional[sqlite3.Connection] = None
        self._mem = _fresh_state(self._now())
        self.log_path = (Path(log_path) if log_path is not None else
                         Path(settings()["dir"]) / "events.jsonl")
        if self.mode == "enforced" and self.path is not None:
            self._open()

    # ---- 生命周期 ---------------------------------------------------------
    def _open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(str(self.path), check_same_thread=False, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA busy_timeout=15000")
        except Exception:
            pass
        db.executescript(_SCHEMA)
        db.commit()
        self._db = db

    def close(self) -> None:
        with self._lock:
            if self._db is not None:
                try:
                    self._db.close()
                except Exception:
                    pass
                self._db = None

    @property
    def enabled(self) -> bool:
        return self.mode in ("shadow", "enforced")

    # ---- 状态读写 ---------------------------------------------------------
    def _load(self) -> dict[str, Any]:
        if self._db is None:
            return _state_copy(self._mem)
        row = self._db.execute(
            "SELECT * FROM pacing_state WHERE id=1").fetchone()
        if row is None:
            return _fresh_state(self._now())
        return _state_copy(dict(row))

    def _write(self, state: dict) -> None:
        if self._db is None:
            self._mem = _state_copy(state)
            return
        values = [state.get(k) for k in STATE_KEYS]
        self._db.execute(
            "UPDATE pacing_state SET " +
            ",".join(f"{k}=?" for k in STATE_KEYS) + " WHERE id=1",
            values)

    def _tx(self):
        if self._db is None:
            return None
        self._db.execute("BEGIN IMMEDIATE")
        return self._db

    def note_user_input(self, now: Optional[float] = None) -> dict[str, Any]:
        """真实用户输入：结束未回复退避，并开启输入后的宽限期。"""
        at = _now(now)
        if self.mode == "off":
            return {"ok": True, "skipped": True, "reason": "off"}
        with self._lock:
            if self._db is None:
                state = _normalized(self._mem, at)
                state.update({
                    "last_user_input_at": at,
                    "post_user_grace_until": at +
                    int(settings()["post_user_grace_minutes"]) * 60,
                    "backoff_until": 0.0,
                    "unanswered_count": 0,
                    "updated_at": at,
                })
                self._mem = state
                return {"ok": True, "mode": self.mode,
                        "post_user_grace_until": _iso(
                            float(state["post_user_grace_until"]))}
            db = self._tx()
            try:
                state = _normalized(self._load(), at)
                state.update({
                    "last_user_input_at": at,
                    "post_user_grace_until": at +
                    int(settings()["post_user_grace_minutes"]) * 60,
                    "backoff_until": 0.0,
                    "unanswered_count": 0,
                    "updated_at": at,
                })
                self._write(state)
                db.commit()
            except Exception:
                db.rollback()
                raise
            return {"ok": True, "mode": self.mode,
                    "post_user_grace_until": _iso(
                        float(state["post_user_grace_until"]))}

    def status(self, now: Optional[float] = None) -> dict[str, Any]:
        at = _now(now)
        with self._lock:
            state = _normalized(self._load(), at)
        s = settings()
        return {
            "mode": self.mode, "level": self.level,
            "timezone": state.get("timezone") or s["timezone"],
            "min_interval_minutes": s["min_interval_minutes"],
            "daily_ceiling": s["daily_ceiling"],
            "post_user_grace_minutes": s["post_user_grace_minutes"],
            "first_unanswered_backoff_minutes":
                s["first_unanswered_backoff_minutes"],
            "repeated_unanswered_backoff_minutes":
                s["repeated_unanswered_backoff_minutes"],
            "last_nonessential_sent_at": _iso(
                float(state.get("last_nonessential_sent_at") or 0)),
            "day_key": state.get("day_key"),
            "day_count": int(state.get("day_count") or 0),
            "unanswered_count": int(state.get("unanswered_count") or 0),
            "backoff_until": _iso(float(state.get("backoff_until") or 0)),
            "post_user_grace_until": _iso(
                float(state.get("post_user_grace_until") or 0)),
            "last_user_input_at": _iso(
                float(state.get("last_user_input_at") or 0)),
            "reservation_active": bool(state.get("reservation_id")),
            "reservation_expires": _iso(
                float(state.get("reservation_expires") or 0)),
        }

    # ---- 日志 -------------------------------------------------------------
    def _log(self, event: dict[str, Any], *, dedupe_s: float = 0.0) -> None:
        if not bool(getattr(C, "PROACTIVE_PACING_LOG", True)):
            return
        key = str(event.get("candidate_key") or "")
        if dedupe_s > 0 and key:
            at = time.time()
            with _LOG_LOCK:
                last = float(_SHADOW_LOGGED.get(key) or 0.0)
                if last and at - last < dedupe_s:
                    return
                _SHADOW_LOGGED[key] = at
                if len(_SHADOW_LOGGED) > 512:
                    for old in sorted(_SHADOW_LOGGED,
                                      key=_SHADOW_LOGGED.get)[:128]:
                        _SHADOW_LOGGED.pop(old, None)
        row = {
            "ts": _iso(time.time()),
            "event_id": "pce_" + _hash(
                f"{event.get('candidate_key')}|{time.time_ns()}", 16),
            "mode": self.mode,
            "level": self.level,
            "intent": event.get("intent", ""),
            "essential": bool(event.get("essential")),
            "result": event.get("result", ""),
            "reason": event.get("reason", ""),
            "candidate_key": key,
            "reservation_id": str(event.get("reservation_id") or ""),
            "real_candidate_changed": False,
            "extra_real_llm_calls": 0,
            "extra_budget_consumed": False,
            "real_pacing_state_changed": bool(
                event.get("real_pacing_state_changed", False)),
            "visible_message_created": bool(
                event.get("visible_message_created", False)),
            "catch_up_created": False,
            "relationship_write": False,
            "pre_llm_suppressed": bool(
                event.get("pre_llm_suppressed", False)),
        }
        target = self.log_path
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with _LOG_LOCK:
                with target.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception:
            pass

    # ---- 发送权 -----------------------------------------------------------
    def begin(self, candidate: Optional[dict], *, quiet: bool = False,
              paused: bool = False, now: Optional[float] = None) -> dict[str, Any]:
        """模型调用前的入口：返回 allowed 与 reservation_id。"""
        at = _now(now)
        if self.mode == "off":
            return {"allowed": True, "result": "allow", "reason": "gate_off",
                    "mode": "off", "shadow": False, "reservation_id": ""}
        cand = dict(candidate or {})
        if self.mode == "shadow":
            decision = decide(cand, self._load(), quiet=quiet, paused=paused,
                              now=at, mode_name="shadow", level_name=self.level)
            self._log({
                **decision,
                "result": ("would_suppress" if not decision.get("allowed")
                           else "would_allow"),
                "pre_llm_suppressed": False,
                "real_pacing_state_changed": False,
                "visible_message_created": False,
            }, dedupe_s=300)
            return {**decision, "allowed": True, "shadow": True,
                    "shadow_result": ("would_allow" if decision.get("allowed")
                                      else "would_suppress"),
                    "reservation_id": ""}
        if bool(cand.get("essential")):
            decision = decide(cand, self._load(), quiet=quiet, paused=paused,
                              now=at, mode_name="enforced", level_name=self.level)
            if not decision.get("allowed"):
                self._log({**decision, "result": "suppress",
                           "pre_llm_suppressed": True,
                           "real_pacing_state_changed": False})
                return decision
            self._log({**decision, "result": "allow", "pre_llm_suppressed": False,
                       "real_pacing_state_changed": False})
            return {**decision, "reservation_id": "", "shadow": False}
        if quiet or paused:
            decision = decide(cand, self._load(), quiet=quiet, paused=paused,
                              now=at, mode_name="enforced", level_name=self.level)
            self._log({**decision, "result": "suppress",
                       "pre_llm_suppressed": True,
                       "real_pacing_state_changed": False})
            return decision
        return self.reserve(cand, now=at)

    def reserve(self, candidate: Optional[dict], *,
                now: Optional[float] = None) -> dict[str, Any]:
        at = _now(now)
        cand = dict(candidate or {})
        if self.mode != "enforced":
            return self.begin(cand, now=at)
        if bool(cand.get("essential")):
            return {"allowed": True, "result": "allow",
                    "reason": str(cand.get("essential_reason") or "essential"),
                    "mode": self.mode, "level": self.level,
                    "essential": True, "intent": str(cand.get("intent") or ""),
                    "reservation_id": "", "shadow": False}
        if self._db is None:
            return {"allowed": False, "result": "suppress",
                    "reason": "pacing_unavailable", "mode": self.mode,
                    "level": self.level, "reservation_id": ""}
        with self._lock:
            db = self._tx()
            try:
                state = _normalized(self._load(), at)
                decision = decide(cand, state, now=at, mode_name="enforced",
                                  level_name=self.level)
                if not decision.get("allowed"):
                    self._write(state)
                    db.commit()
                    self._log({**decision, "result": "suppress",
                               "pre_llm_suppressed": True,
                               "real_pacing_state_changed": False})
                    return decision
                if state.get("reservation_id"):
                    result = {**decision, "allowed": False,
                              "result": "suppress",
                              "reason": "reservation_active"}
                    self._write(state)
                    db.commit()
                    self._log({**result, "pre_llm_suppressed": True,
                               "real_pacing_state_changed": False})
                    return result
                s = settings()
                expires = float(cand.get("expires_at") or 0.0)
                ttl_until = at + int(s["reservation_ttl_s"])
                rid = "pr_" + _hash(
                    f"{cand.get('candidate_key')}|{at}|{os.getpid()}", 24)
                state.update({
                    "reservation_id": rid,
                    "reservation_plan_id": str(cand.get("candidate_key") or ""),
                    "reservation_at": at,
                    "reservation_expires": (min(ttl_until, expires)
                                            if expires else ttl_until),
                    "reservation_context": str(cand.get("novelty_key")
                                                or cand.get("candidate_key") or ""),
                    "updated_at": at,
                })
                self._write(state)
                db.commit()
            except Exception:
                db.rollback()
                raise
            out = {**decision, "allowed": True, "result": "reserved",
                   "reason": "reserved", "reservation_id": rid,
                   "shadow": False}
            self._log({**out, "pre_llm_suppressed": False,
                       "real_pacing_state_changed": True})
            return out

    def settle_sent(self, reservation_id: str,
                    candidate: Optional[dict] = None,
                    now: Optional[float] = None) -> dict[str, Any]:
        """确认已经写入发送队列后原子结算；不重试、不重复计数。"""
        at = _now(now)
        rid = str(reservation_id or "")
        if self.mode != "enforced" or not rid:
            return {"ok": False, "reason": "not_enforced_or_no_reservation"}
        if self._db is None:
            return {"ok": False, "reason": "pacing_unavailable"}
        with self._lock:
            db = self._tx()
            try:
                state = _normalized(self._load(), at)
                if str(state.get("reservation_id") or "") != rid:
                    db.rollback()
                    return {"ok": False, "reason": "reservation_not_found"}
                prev = int(state.get("unanswered_count") or 0)
                s = settings()
                if prev >= 1:
                    backoff = at + int(
                        s["repeated_unanswered_backoff_minutes"]) * 60
                else:
                    backoff = at + int(
                        s["first_unanswered_backoff_minutes"]) * 60
                state.update({
                    "last_nonessential_sent_at": at,
                    "day_count": int(state.get("day_count") or 0) + 1,
                    "unanswered_count": prev + 1,
                    "backoff_until": backoff,
                    "reservation_id": "",
                    "reservation_plan_id": "",
                    "reservation_at": 0.0,
                    "reservation_expires": 0.0,
                    "reservation_context": "",
                    "updated_at": at,
                })
                self._write(state)
                db.commit()
            except Exception:
                db.rollback()
                raise
            self._log({
                "result": "sent", "reason": "confirmed",
                "intent": str((candidate or {}).get("intent") or ""),
                "essential": bool((candidate or {}).get("essential")),
                "candidate_key": str((candidate or {}).get("candidate_key") or ""),
                "reservation_id": rid,
                "real_pacing_state_changed": True,
                "visible_message_created": True,
            })
            return {"ok": True, "backoff_until": _iso(backoff),
                    "day_count": int(state["day_count"]),
                    "unanswered_count": int(state["unanswered_count"])}

    def release(self, reservation_id: str, reason: str = "cancelled",
                now: Optional[float] = None) -> dict[str, Any]:
        """生成失败/未产生可见输出时释放预留；不重排候选。"""
        rid = str(reservation_id or "")
        if self.mode != "enforced" or not rid or self._db is None:
            return {"ok": False, "reason": "not_enforced_or_no_reservation"}
        at = _now(now)
        with self._lock:
            db = self._tx()
            try:
                state = _normalized(self._load(), at)
                if str(state.get("reservation_id") or "") != rid:
                    db.rollback()
                    return {"ok": False, "reason": "reservation_not_found"}
                state.update({
                    "reservation_id": "", "reservation_plan_id": "",
                    "reservation_at": 0.0, "reservation_expires": 0.0,
                    "reservation_context": "", "updated_at": at,
                })
                self._write(state)
                db.commit()
            except Exception:
                db.rollback()
                raise
            self._log({"result": "released", "reason": reason,
                       "candidate_key": str(state.get("reservation_plan_id")
                                             or ""),
                       "reservation_id": rid,
                       "real_pacing_state_changed": True,
                       "visible_message_created": False})
            return {"ok": True, "reason": reason}

    def observe_sent(self, candidate: Optional[dict] = None,
                     now: Optional[float] = None) -> dict[str, Any]:
        """shadow 模式下的模拟结算；只改进程内模拟状态，不碰真实库。"""
        if self.mode != "shadow":
            return {"ok": False, "reason": "not_shadow"}
        at = _now(now)
        with self._lock:
            state = _normalized(self._mem, at)
            prev = int(state.get("unanswered_count") or 0)
            s = settings()
            state.update({
                "last_nonessential_sent_at": at,
                "day_count": int(state.get("day_count") or 0) + 1,
                "unanswered_count": prev + 1,
                "backoff_until": at + int(
                    (s["repeated_unanswered_backoff_minutes"] if prev >= 1
                     else s["first_unanswered_backoff_minutes"])) * 60,
                "updated_at": at,
            })
            self._mem = state
            self._log({"result": "shadow_sent", "reason": "simulated",
                       "intent": str((candidate or {}).get("intent") or ""),
                       "essential": bool((candidate or {}).get("essential")),
                       "candidate_key": str(
                           (candidate or {}).get("candidate_key") or ""),
                       "real_pacing_state_changed": False,
                       "visible_message_created": True})
            return {"ok": True, "shadow": True}


def reset_for_tests() -> None:
    with _LOG_LOCK:
        _SHADOW_LOGGED.clear()


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="app.proactive_pacing",
                                 description="主动消息节奏（只读查看）")
    ap.add_argument("--status", action="store_true", help="看当前节奏状态")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    a = ap.parse_args(argv)
    path = Path(getattr(C, "DATA", ".")) / "proactive.db"
    store = PacingStore(path, mode_name=mode(), level_name=level())
    try:
        data = store.status()
        if a.json:
            print(json.dumps(data, ensure_ascii=False, indent=2))
        else:
            print(f"模式 {data['mode']} / 等级 {data['level']} / "
                  f"时区 {data['timezone']}")
            print(f"今日非必要 {data['day_count']}/{data['daily_ceiling']}，"
                  f"未回复 {data['unanswered_count']}")
            print(f"上次非必要发送 {data['last_nonessential_sent_at'] or '—'}")
            print(f"退避到 {data['backoff_until'] or '—'}；"
                  f"输入宽限到 {data['post_user_grace_until'] or '—'}")
            print(f"预留 {'有' if data['reservation_active'] else '无'}")
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
