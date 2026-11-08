"""自然回应与沉默闸门。

这一层只做一件事：在调用文本模型之前，判断这一轮应该
`respond`、`brief`、`defer`、`silent` 还是 `pause_stated`。

硬边界：
- 不调用 LLM、不访问 SQLite、不写人格/关系/长期记忆；
- 规则确定，不使用随机数，不设“每天必须沉默几次”的指标；
- 只记录事件指纹、模式、理由和禁止副作用结果，不复制完整私密输入；
- `off` 时完全不动现有回复路径，`shadow` 只记录，`enforced` 才改变行为。

N2 只开放客观沉默（重复、自回声、过期、后台、跨通道已处理、用户明确安静）；
N3 另开社交沉默（自然结束、无新价值），由 `social_silence` 独立开关控制。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from . import config as C

MODES = ("off", "shadow", "enforced")
DECISION_MODES = ("respond", "brief", "defer", "silent", "pause_stated")
VISIBLE_MODES = ("respond", "brief", "pause_stated")
DEFAULT_DEDUPE_WINDOW_S = 120.0
DEFAULT_RECENT_WINDOW_S = 600.0
DEFAULT_QUIET_WINDOW_S = 2 * 3600.0

_QUIET_RE = re.compile(r"想静静|想安静|让我安静|别烦我|不想聊|先别问|安静一会")
_PAUSE_RE = re.compile(r"先停一下|暂停|别说了|到此为止|不想继续|先不聊|冷静一下")
_OVERRIDE_RE = re.compile(
    r"回答我|不要沉默|别沉默|别不说话|回我|理我|出声|别装没听见|说话啊")
_SAFETY_RE = re.compile(
    r"救命|报警|着火了|起火了|漏电|煤气|受伤|流血|胸痛|呼吸困难|"
    r"自杀|不想活|想死|有人闯|被跟踪")
_TASK_RE = re.compile(
    r"帮我|帮忙|请你|请帮|查一下|查查|怎么做|怎么办|是什么|为什么|"
    r"能不能|可不可以|写一下|翻译|计算|提醒我|设个|打开|关掉|看看|解释")
_QUESTION_RE = re.compile(
    r"[?？]|吗[。.!！~～\s]*$|呢[。.!！~～\s]*$|什么|为什么|怎么|哪|谁|"
    r"多少|几点|能不能|可不可以")
_ACK_RE = re.compile(
    r"^(嗯+|哦+|好|好的|行|可以|知道了|明白了|收到|谢谢|辛苦了)"
    r"[。.!！~～\s]*$")
_FAREWELL_RE = re.compile(r"^(晚安|拜拜|再见|先这样|今天就到这)[。.!！~～\s]*$")
_FRAGMENT_RE = re.compile(
    r"^(我|那个|就是|然后|还有|其实|不过|因为|所以|嗯|哦)"
    r"[^。！？!?]{0,18}$")
_ECHO_PREFIX_RE = re.compile(r"^(瓦尔基里|她|assistant)\s*[：:]\s*")


def _norm(text: str) -> str:
    return " ".join(str(text or "").split()).strip()


def _hash(text: str) -> str:
    return hashlib.sha256(_norm(text).encode("utf-8", "replace")).hexdigest()[:24]


def _event_id(channel: str, text: str, now: float) -> str:
    """同一句在短窗口内视为同一事件；显式 event_id 优先。"""
    del now  # 不用时间参与，否则重复检测失效。
    return "auto_" + _hash(f"{channel}|{text}")


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(float(ts)).astimezone().isoformat(timespec="seconds")


@dataclass(frozen=True)
class ResponseDecision:
    mode: str
    reason: str
    event_id: str
    channel: str
    reentry_condition: str = ""
    decided_by: str = "rule"
    shadow: bool = False
    text_hash: str = ""
    text_len: int = 0
    flags: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "reason": self.reason,
            "event_id": self.event_id,
            "channel": self.channel,
            "reentry_condition": self.reentry_condition,
            "decided_by": self.decided_by,
            "shadow": self.shadow,
            "text_hash": self.text_hash,
            "text_len": self.text_len,
            "flags": dict(self.flags),
        }


class ResponseGate:
    """有状态的规则闸门；纯规则，不碰模型与数据库。"""

    def __init__(self, mode: Optional[str] = None, *,
                 social_silence: Optional[bool] = None,
                 merge_window_ms: Optional[int] = None,
                 log_path: Optional[Path | str] = None,
                 log_enabled: Optional[bool] = None,
                 state_path: Optional[Path | str] = None,
                 dedupe_window_s: float = DEFAULT_DEDUPE_WINDOW_S,
                 recent_window_s: float = DEFAULT_RECENT_WINDOW_S,
                 quiet_window_s: float = DEFAULT_QUIET_WINDOW_S,
                 now_fn=time.time) -> None:
        raw = str(mode if mode is not None else getattr(C, "RESPONSE_GATE_MODE", "off"))
        self.mode = raw.strip().lower() if raw.strip().lower() in MODES else "off"
        if social_silence is None:
            social_silence = bool(getattr(C, "SOCIAL_SILENCE", False))
        self.social_silence = bool(social_silence)
        if merge_window_ms is None:
            merge_window_ms = int(getattr(C, "REPLY_MERGE_WINDOW_MS", 1500))
        self.merge_window_ms = max(0, min(30000, int(merge_window_ms)))
        if log_enabled is None:
            log_enabled = bool(getattr(C, "RESPONSE_GATE_LOG", True))
        self.log_enabled = bool(log_enabled)
        if log_path is None:
            log_path = Path(getattr(C, "RESPONSE_GATE_DIR", C.DATA / "response_gate")) / "decisions.jsonl"
        self.log_path = Path(log_path)
        self.state_path = Path(state_path) if state_path else None
        self._now = now_fn
        self._lock = threading.RLock()
        self._seen: dict[str, float] = {}
        self._last_event: dict[str, Any] = {}
        self._last_silent_ts = 0.0
        self._last_silent_reason = ""
        self._quiet_until = 0.0
        self._pause_until = 0.0
        self._dedupe_window_s = max(10.0, float(dedupe_window_s))
        self._recent_window_s = max(30.0, float(recent_window_s))
        self._quiet_window_s = max(60.0, float(quiet_window_s))
        self.stats = {"decisions": 0, "silent": 0, "defer": 0,
                      "brief": 0, "respond": 0, "pause_stated": 0}

    # ---- 只读状态 ---------------------------------------------------------
    def is_quiet(self, now: Optional[float] = None) -> bool:
        at = self._now() if now is None else float(now)
        with self._lock:
            return self._quiet_until > at

    def quiet_until(self) -> float:
        with self._lock:
            return self._quiet_until

    def is_paused(self, now: Optional[float] = None) -> bool:
        at = self._now() if now is None else float(now)
        with self._lock:
            return self._pause_until > at

    def recent_silent(self, seconds: float = 300.0,
                      now: Optional[float] = None) -> bool:
        at = self._now() if now is None else float(now)
        with self._lock:
            return self._last_silent_ts > 0 and (at - self._last_silent_ts) < seconds

    def last_decision(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._last_event)

    # ---- 决策 -------------------------------------------------------------
    def decide(self, text: str, *, channel: str = "text",
               event_id: str = "",
               now: Optional[float] = None,
               explicit_request: bool = False,
               task: bool = False,
               safety: bool = False,
               promise_due: bool = False,
               user_override: bool = False,
               self_affect: float = 0.0,
               echo: bool = False,
               expired: bool = False,
               superseded: bool = False,
               background: bool = False,
               handled_elsewhere: bool = False,
               batch_open: bool = False,
               batch_complete: bool = True,
               batch_size: int = 1,
               batch_text: str = "",
               last_assistant: str = "",
               quiet: bool = False) -> ResponseDecision:
        """返回一条确定性的回应决定；不产生任何外部副作用。"""
        at = self._now() if now is None else float(now)
        raw = _norm(text)
        batch_raw = _norm(batch_text) or raw
        eid = str(event_id or "").strip() or _event_id(channel, raw, at)
        text_hash = _hash(raw)
        flags = {
            "explicit_request": bool(explicit_request),
            "task": bool(task),
            "safety": bool(safety),
            "promise_due": bool(promise_due),
            "user_override": bool(user_override),
            "self_affect_negative": bool(self_affect and float(self_affect) < 0),
            "echo": bool(echo),
            "expired": bool(expired),
            "superseded": bool(superseded),
            "background": bool(background),
            "handled_elsewhere": bool(handled_elsewhere),
            "batch_open": bool(batch_open),
            "batch_complete": bool(batch_complete),
            "batch_size": int(max(1, batch_size)),
            "quiet": bool(quiet),
        }
        if self.mode == "off":
            return ResponseDecision(
                "respond", "gate_off", eid, channel,
                decided_by="config", text_hash=text_hash,
                text_len=len(raw), flags=flags)

        with self._lock:
            self._prune(at)
            self._load_state(at)
            duplicate = eid in self._seen
            if quiet or _QUIET_RE.search(raw):
                self._quiet_until = max(self._quiet_until, at + self._quiet_window_s)
                flags["quiet"] = True
            if _PAUSE_RE.search(raw):
                self._pause_until = max(self._pause_until, at + self._quiet_window_s)
                flags["explicit_pause"] = True

            # 1) 事件有效性：这一层永远先判，避免旧事件/回声被重新拾起。
            if duplicate:
                return self._finish("silent", "duplicate_event", eid, channel, at,
                                    flags, text_hash, raw)
            if flags["echo"] or self._looks_like_echo(raw, last_assistant):
                flags["echo"] = True
                return self._finish("silent", "echo", eid, channel, at,
                                    flags, text_hash, raw)
            if expired:
                return self._finish("silent", "expired", eid, channel, at,
                                    flags, text_hash, raw)
            if superseded:
                return self._finish("silent", "superseded", eid, channel, at,
                                    flags, text_hash, raw)
            if background:
                return self._finish("silent", "background", eid, channel, at,
                                    flags, text_hash, raw)
            if handled_elsewhere:
                return self._finish("silent", "handled_elsewhere", eid, channel, at,
                                    flags, text_hash, raw)

            # 2) 明确暂停/边界：只说明一次，等明确恢复。
            if flags.get("explicit_pause"):
                return self._finish("pause_stated", "explicit_pause", eid, channel,
                                    at, flags, text_hash, raw,
                                    reentry="等你说可以继续")

            # 3) 明确请求/安全/任务/承诺，任何旧安静状态都不能吞掉。
            if safety or _SAFETY_RE.search(raw):
                flags["safety"] = True
                return self._finish("respond", "safety", eid, channel, at,
                                    flags, text_hash, raw)
            if user_override or _OVERRIDE_RE.search(raw):
                flags["user_override"] = True
                return self._finish("respond", "user_override", eid, channel, at,
                                    flags, text_hash, raw)
            if task or _TASK_RE.search(raw):
                flags["task"] = True
                return self._finish("respond", "task", eid, channel, at,
                                    flags, text_hash, raw)
            if promise_due:
                return self._finish("respond", "promise_due", eid, channel, at,
                                    flags, text_hash, raw)
            if explicit_request or _QUESTION_RE.search(raw):
                flags["explicit_request"] = True
                return self._finish("respond", "explicit_request", eid, channel, at,
                                    flags, text_hash, raw)

            # 4) 用户明确要安静：普通输出和主动任务都停，必要时由请求覆盖。
            if flags["quiet"]:
                return self._finish("silent", "user_quiet", eid, channel, at,
                                    flags, text_hash, raw,
                                    reentry="等他明确恢复交流")

            # 5) 连续输入：只暂缓，不自动延时发送旧回复。
            if batch_open and not batch_complete:
                return self._finish("defer", "burst_open", eid, channel, at,
                                    flags, text_hash, raw,
                                    reentry="新信息到达或这一批表达说完时重评")

            # 6) 社交性沉默：只在没有任务/安全/承诺/覆盖时考虑。
            natural_end = bool(_ACK_RE.match(batch_raw) or
                               (batch_size == 1 and _ACK_RE.match(raw)))
            no_value = bool(_ACK_RE.match(batch_raw)) and not _QUESTION_RE.search(
                batch_raw) and not _TASK_RE.search(batch_raw)
            if batch_size > 1 and not natural_end and not no_value:
                # 整批表达已经说完，最多轻量承接一次，不逐条机械接应。
                if _QUESTION_RE.search(batch_raw) or _TASK_RE.search(batch_raw):
                    return self._finish("respond", "burst_complete", eid, channel,
                                        at, flags, text_hash, raw)
                return self._finish("brief", "burst_brief", eid, channel, at,
                                    flags, text_hash, raw)
            if self.social_silence and (natural_end or no_value):
                return self._finish("silent", "natural_end", eid, channel, at,
                                    flags, text_hash, raw,
                                    reentry="有新信息或新问题时重评")
            if natural_end:
                return self._finish("brief", "short_ack", eid, channel, at,
                                    flags, text_hash, raw)
            if _FAREWELL_RE.match(raw):
                return self._finish("brief", "farewell", eid, channel, at,
                                    flags, text_hash, raw)

            # 7) 默认：保持现有回复行为，不为了降低回复率扩大沉默。
            return self._finish("respond", "default", eid, channel, at,
                                flags, text_hash, raw)

    def _finish(self, mode: str, reason: str, eid: str, channel: str,
                at: float, flags: dict[str, Any], text_hash: str, raw: str,
                reentry: str = "") -> ResponseDecision:
        self._seen[eid] = at
        if mode == "silent":
            self._last_silent_ts = at
            self._last_silent_reason = reason
        elif mode == "pause_stated":
            self._pause_until = max(self._pause_until, at + self._quiet_window_s)
        d = ResponseDecision(
            mode=mode, reason=reason, event_id=eid, channel=channel,
            reentry_condition=reentry, decided_by="rule",
            shadow=(self.mode == "shadow"), text_hash=text_hash,
            text_len=len(raw), flags=dict(flags))
        self._last_event = {"ts": at, "decision": d.as_dict()}
        self.stats["decisions"] += 1
        if mode in self.stats:
            self.stats[mode] += 1
        self._save_state(at)
        return d

    def _load_state(self, now: float) -> None:
        """跨进程共享事件指纹与安静状态；没有 state_path 时纯内存。"""
        if self.state_path is None:
            return
        try:
            raw = self.state_path.read_text(encoding="utf-8")
            data = json.loads(raw or "{}")
        except Exception:
            return
        events = data.get("events") or {}
        if isinstance(events, dict):
            for key, ts in events.items():
                try:
                    value = float(ts)
                except (TypeError, ValueError):
                    continue
                if value >= now - self._recent_window_s:
                    self._seen[str(key)] = max(self._seen.get(str(key), 0.0), value)
        try:
            self._quiet_until = max(self._quiet_until,
                                    float(data.get("quiet_until") or 0.0))
            self._pause_until = max(self._pause_until,
                                    float(data.get("pause_until") or 0.0))
        except (TypeError, ValueError):
            pass

    def _save_state(self, now: float) -> None:
        if self.state_path is None:
            return
        payload = {
            "updated_at": now,
            "events": {k: v for k, v in self._seen.items()
                       if v >= now - self._recent_window_s},
            "quiet_until": self._quiet_until,
            "pause_until": self._pause_until,
        }
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_name(self.state_path.name + f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.state_path)
        except Exception:
            pass

    def _looks_like_echo(self, text: str, last_assistant: str) -> bool:
        if not text:
            return False
        cleaned = _ECHO_PREFIX_RE.sub("", text).strip()
        if not cleaned:
            return True
        last = _norm(last_assistant)
        if last and (cleaned == last or cleaned in last):
            return True
        # 她自己的输出再次进入输入时，通常以名字/引号开头；不把普通“我”当回声。
        return False

    def _prune(self, now: float) -> None:
        cut = now - self._recent_window_s
        old = [k for k, ts in self._seen.items() if ts < cut]
        for k in old:
            self._seen.pop(k, None)
        if len(self._seen) > 2000:
            for k, _ in sorted(self._seen.items(), key=lambda kv: kv[1])[:1000]:
                self._seen.pop(k, None)

    # ---- 日志 -------------------------------------------------------------
    def log(self, decision: ResponseDecision, **outcome: Any) -> None:
        """写一条运行日志；失败不阻塞对话。"""
        if not self.log_enabled or self.mode == "off":
            return
        payload = {
            "ts": _iso(self._now()),
            "event_id": decision.event_id,
            "channel": decision.channel,
            "mode": decision.mode,
            "reason": decision.reason,
            "decided_by": decision.decided_by,
            "reentry_condition": decision.reentry_condition,
            "text_hash": decision.text_hash,
            "text_len": decision.text_len,
            "flags": dict(decision.flags),
            "llm_called": bool(outcome.get("llm_called", False)),
            "assistant_message_created": bool(
                outcome.get("assistant_message_created", False)),
            "proactive_followup_created": bool(
                outcome.get("proactive_followup_created", False)),
            "relationship_write": bool(outcome.get("relationship_write", False)),
            "shadow": bool(decision.shadow or outcome.get("shadow", False)),
        }
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self._lock:
                with self.log_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def shutdown(self) -> None:
        """没有需要关闭的文件句柄；保留给 Chat.close 统一调用。"""
        return None
