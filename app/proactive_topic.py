"""主动话题：她先开口时，说的可以不是“一个问题”。

这一层是旁路，不替换原有主动候选，也不进入 Chat 同步回复路径。它只做五件事：

    只读快照 → TopicSeed → 硬过滤 → ProactivePlan → 交给现有主动生成

边界（与施工方案一致）：
- 不直接访问生产库；来源由调用方以最小快照传入。
- 不调用模型、不生成最终文本、不发送、不写记忆/情绪/关系/承诺/学习状态。
- 不新建调度器、发送器、频控或预算；仍由现有主动 worker 执行。
- off 不采集；shadow 只记录计划；enforced 才把计划交给现有生成。
- 没有新价值、来源过期、安静/停止、证据不足时，宁可不发。
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import asdict, dataclass, field as dc_field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from . import affect as _affect
from . import config as C

MODES = ("off", "shadow", "enforced")
INTENTS = ("share", "continue", "react", "care", "invite", "play",
           "remind", "check_in")
DEFAULT_INTENTS = ("share", "continue", "react", "care", "play")
SOURCE_TYPES = ("shared_event", "learning_result", "affect_state",
                "commitment", "observation", "imagination")
SENSITIVITIES = ("normal", "sensitive", "forbidden")
EVIDENCE_STATES = ("eligible", "contextual_only", "invalid")
AVAILABILITY = ("available", "unavailable", "expired")
REPLY_EXPECTATIONS = ("none", "open", "requested")

# 每类意图的默认语义；question_budget 只限制“可选问题”，必要确认不算社交提问。
INTENT_RULES: dict[str, dict] = {
    "share": {"reply": "none", "questions": 0,
              "new_value": "new_information", "label": "分享"},
    "continue": {"reply": "open", "questions": 0,
                 "new_value": "old_topic_progress", "label": "接续"},
    "react": {"reply": "none", "questions": 0,
              "new_value": "specific_stance", "label": "态度"},
    "care": {"reply": "none", "questions": 0,
             "new_value": "actionable_care", "label": "关心"},
    "invite": {"reply": "open", "questions": 1,
               "new_value": "actionable_invite", "label": "邀请"},
    "play": {"reply": "open", "questions": 0,
             "new_value": "marked_imagination", "label": "玩耍"},
    "remind": {"reply": "requested", "questions": 1,
               "new_value": "commitment_due", "label": "提醒"},
    "check_in": {"reply": "open", "questions": 1,
                 "new_value": "necessary_confirmation", "label": "轻量确认"},
}

_SOURCE_INTENTS: dict[str, tuple[str, ...]] = {
    "learning_result": ("share",),
    "shared_event": ("continue", "react", "invite"),
    "affect_state": ("react", "care"),
    "commitment": ("remind",),
    "observation": ("care", "check_in"),
    "imagination": ("play", "invite"),
}

_INTENT_PRIORITY = {
    "continue": 0.92, "share": 0.86, "react": 0.78, "care": 0.72,
    "play": 0.66, "invite": 0.60, "check_in": 0.56, "remind": 0.99,
}

_CARE_CUES = ("累", "忙", "困", "加班", "熬夜", "考试", "答辩", "头疼",
              "不舒服", "难受", "发烧", "感冒", "赶", "来不及")
_PLAY_CUES = ("无聊", "玩", "游戏", "陪我", "猜", "二选一")
_NEGATIVE = ("低落", "焦虑", "委屈", "生气", "烦躁", "疲惫", "难过",
             "不安", "失望", "孤独", "内耗")
_POSITIVE = ("开心", "高兴", "兴奋", "安心", "期待", "好奇")

_LOG_LOCK = threading.Lock()
_SHADOW_LOGGED: dict[str, float] = {}
_FAILED: dict[str, float] = {}


def _now(now: Optional[float] = None) -> float:
    return time.time() if now is None else float(now)


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat(
        timespec="seconds")


def _parse_iso(value: Any) -> float:
    if not value:
        return 0.0
    raw = str(value).strip()
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _short(text: Any, n: int = 48) -> str:
    s = " ".join(str(text or "").split()).strip()
    return s[:max(1, int(n))]


def _clean_hint(text: Any, n: int = 160) -> str:
    return _short(text, n).rstrip("。！？!?；;，,、 ")


def _hash(text: Any, n: int = 24) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8", "replace")).hexdigest()[:n]


def mode(explicit: Optional[str] = None) -> str:
    raw = explicit if explicit is not None else getattr(
        C, "PROACTIVE_TOPIC_MODE", "off")
    m = str(raw or "off").strip().lower()
    return m if m in MODES else "off"


def enabled_intents(raw: Optional[str] = None) -> tuple[str, ...]:
    text = raw if raw is not None else getattr(C, "PROACTIVE_TOPIC_INTENTS", "")
    if not text:
        return DEFAULT_INTENTS
    out = []
    for part in str(text).replace(";", ",").split(","):
        name = part.strip().lower()
        if name in INTENTS and name not in out:
            out.append(name)
    return tuple(out)


def max_candidates() -> int:
    try:
        return max(1, min(32, int(getattr(C, "PROACTIVE_TOPIC_MAX_CANDIDATES", 6))))
    except (TypeError, ValueError):
        return 6


@dataclass(frozen=True)
class TopicSeed:
    seed_id: str
    source_type: str
    source_refs: list[str]
    thread_id: Optional[str]
    created_at: str
    valid_until: str
    novelty_key: str
    sensitivity: str = "normal"
    evidence_state: str = "eligible"
    availability: str = "available"
    why_code: str = "source_available"
    content_hint: str = ""
    claim_ids: list[str] = dc_field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ProactivePlan:
    plan_id: str
    trigger_event_id: str
    intent: str
    seed_refs: list[str]
    why_now: str
    why_now_code: str
    new_value: str
    new_value_code: str
    content_goal: str
    reply_expectation: str
    question_budget: int
    required_intents: list[str]
    eligible_claim_ids: list[str]
    required_notices: list[str]
    expires_at: str
    channel: str
    novelty_key: str
    source_types: list[str]
    evidence_state: str
    source_refs: list[str]
    sensitivity: str
    content_hint: str

    def as_dict(self) -> dict:
        return asdict(self)


def make_seed(*, source_type: str, source_refs: Iterable[str],
              content_hint: str = "", now: Optional[float] = None,
              ttl_hours: float = 24.0, thread_id: Optional[str] = None,
              density: str = "normal", evidence_state: str = "eligible",
              availability: str = "available",
              why_code: str = "source_available",
              claim_ids: Optional[Iterable[str]] = None,
              sensitivity: str = "normal") -> TopicSeed:
    """构造一个最小种子；不做来源读取，调用方负责只读快照。"""
    at = _now(now)
    st = str(source_type or "").strip()
    if st not in SOURCE_TYPES:
        raise ValueError(f"非法来源类型: {st}")
    refs = [str(x)[:120] for x in (source_refs or []) if str(x).strip()]
    if not refs:
        raise ValueError("TopicSeed 至少需要一个来源引用")
    ttl = max(0.05, float(ttl_hours))
    valid = at + ttl * 3600.0
    hint = _clean_hint(content_hint, 160)
    key = _hash(f"{st}|{'|'.join(refs)}|{hint}")
    return TopicSeed(
        seed_id="ts_" + _hash(f"{st}|{refs}|{at}", 16),
        source_type=st,
        source_refs=refs,
        thread_id=(str(thread_id)[:80] if thread_id else None),
        created_at=_iso(at),
        valid_until=_iso(valid),
        novelty_key=key,
        sensitivity=(sensitivity if sensitivity in SENSITIVITIES else "normal"),
        evidence_state=(evidence_state if evidence_state in EVIDENCE_STATES
                        else "invalid"),
        availability=(availability if availability in AVAILABILITY
                      else "unavailable"),
        why_code=str(why_code or "source_available")[:48],
        content_hint=hint,
        claim_ids=[str(x)[:120] for x in (claim_ids or []) if str(x).strip()][:4],
    )


def validate_seed(seed: TopicSeed | dict, now: Optional[float] = None) -> dict:
    """返回 {ok, reason}；不抛异常，便于故障隔离。"""
    try:
        d = seed.as_dict() if isinstance(seed, TopicSeed) else dict(seed or {})
    except Exception as exc:
        return {"ok": False, "reason": f"seed_unreadable:{type(exc).__name__}"}
    if str(d.get("source_type") or "") not in SOURCE_TYPES:
        return {"ok": False, "reason": "bad_source_type"}
    if not d.get("source_refs"):
        return {"ok": False, "reason": "no_source_refs"}
    if str(d.get("sensitivity") or "") == "forbidden":
        return {"ok": False, "reason": "forbidden_sensitivity"}
    if str(d.get("evidence_state") or "") == "invalid":
        return {"ok": False, "reason": "invalid_evidence"}
    if str(d.get("availability") or "") != "available":
        return {"ok": False, "reason": "unavailable"}
    until = _parse_iso(d.get("valid_until"))
    if until <= 0 or until <= _now(now):
        return {"ok": False, "reason": "expired"}
    if not str(d.get("novelty_key") or ""):
        return {"ok": False, "reason": "missing_novelty"}
    return {"ok": True, "reason": ""}


def _why_text(code: str) -> str:
    return {
        "completed_result_available": "你查过、也已审过的一条内容，现在可以说",
        "study_session_available": "你最近自己查过一点东西，这件事本身可以讲",
        "shared_thread_continuity": "这件事你们一起经历过，你现在有新的看法",
        "affect_shift": "你此刻的情绪变了，有一句具体的态度想说",
        "promise_due": "你答应过的事到时间了",
        "user_state_observed": "他刚才提到自己的状态，你有一句关心",
        "imagination_window": "你想发起一个明确标记的小游戏或共同设定",
        "source_available": "有可用的只读来源",
    }.get(str(code or ""), "有可用的只读来源")


def _new_value_text(code: str) -> str:
    return {
        "new_information": "一条新信息",
        "new_understanding": "一点新理解",
        "specific_stance": "一个具体态度",
        "old_topic_progress": "把旧话题往前推一步的新内容",
        "actionable_care": "一件具体、可执行的关心",
        "actionable_invite": "一个可以拒绝的共同活动",
        "marked_imagination": "一个明确标记的想象或游戏",
        "commitment_due": "履行一条到期的有效承诺",
        "necessary_confirmation": "一次确有必要的轻量确认",
    }.get(str(code or ""), "新的内容")


def _goal_for(seed: TopicSeed, intent: str) -> str:
    hint = _clean_hint(seed.content_hint, 90)
    if seed.source_type == "learning_result":
        if seed.evidence_state == "eligible" and seed.claim_ids:
            return ("把你查过、也已经过审的这条讲给他：" + hint +
                    "。再给一句你自己的判断，不要把它说成刚发生的事。")
        return ("你最近自己查过一点东西： " + hint +
                "。材料还没过审，所以只讲你正在弄明白这件事，"
                "不要把查到的内容说成结论。")
    if seed.source_type == "shared_event":
        return ("从你们共同经历过的这件事往下说：" + hint +
                "。只说你确定的部分，不补细节，不要把它变成追问。")
    if seed.source_type == "affect_state":
        return ("说清你此刻具体的态度或感受，并落到一件具体的事上：" + hint +
                "。不要说成事实判断。")
    if seed.source_type == "commitment":
        return ("你答应过：" + hint + "。现在条件到了，把你能做到的那一步直接说清楚。")
    if seed.source_type == "observation":
        return ("他刚才提到自己的状态：" + hint +
                "。只说你的在意或能帮上的一点，不要要求他汇报。")
    if seed.source_type == "imagination":
        return ("发起一个明确标记的小游戏或共同设定：" + hint +
                "。先给出你自己的选择，不要把所有创作都丢给他。")
    return hint or "说你此刻真正想说的那一句。"


def plan_for_seed(seed: TopicSeed, intent: str,
                  now: Optional[float] = None,
                  trigger_event_id: str = "proactive") -> ProactivePlan:
    """把种子映射成确定性计划；同一输入必须得到同一 plan_id。"""
    if intent not in INTENTS:
        raise ValueError(f"非法主动意图: {intent}")
    at = _now(now)
    rule = INTENT_RULES[intent]
    evidence = seed.evidence_state
    claims = list(seed.claim_ids) if evidence == "eligible" else []
    new_code = str(rule["new_value"])
    # 学习结果如果不是已审 claim，价值只能算“过程/理解”，不能冒充新信息。
    if seed.source_type == "learning_result" and evidence != "eligible":
        new_code = "new_understanding"
    if seed.source_type == "imagination":
        new_code = "marked_imagination"
    key = _hash(f"{intent}|{seed.novelty_key}|{new_code}", 24)
    until = min(_parse_iso(seed.valid_until), at + 72 * 3600)
    return ProactivePlan(
        plan_id="pp_" + _hash(key, 16),
        trigger_event_id=str(trigger_event_id or "proactive")[:80],
        intent=intent,
        seed_refs=list(seed.source_refs),
        why_now=_why_text(seed.why_code),
        why_now_code=str(seed.why_code or "source_available"),
        new_value=_new_value_text(new_code),
        new_value_code=new_code,
        content_goal=_goal_for(seed, intent),
        reply_expectation=str(rule["reply"]),
        question_budget=int(rule["questions"]),
        required_intents=[intent],
        eligible_claim_ids=claims,
        required_notices=[],
        expires_at=_iso(until),
        channel="proactive",
        novelty_key=key,
        source_types=[seed.source_type],
        evidence_state=evidence,
        source_refs=list(seed.source_refs),
        sensitivity=seed.sensitivity,
        content_hint=seed.content_hint,
    )


def validate_plan(plan: ProactivePlan | dict, now: Optional[float] = None) -> dict:
    try:
        d = plan.as_dict() if isinstance(plan, ProactivePlan) else dict(plan or {})
    except Exception as exc:
        return {"ok": False, "reason": f"plan_unreadable:{type(exc).__name__}"}
    for field in ("plan_id", "trigger_event_id", "intent", "why_now",
                  "why_now_code", "new_value", "new_value_code",
                  "content_goal", "reply_expectation", "expires_at",
                  "channel", "novelty_key"):
        if not str(d.get(field) or "").strip():
            return {"ok": False, "reason": f"missing_{field}"}
    if str(d.get("intent")) not in INTENTS:
        return {"ok": False, "reason": "bad_intent"}
    if str(d.get("reply_expectation")) not in REPLY_EXPECTATIONS:
        return {"ok": False, "reason": "bad_reply_expectation"}
    try:
        qb = int(d.get("question_budget"))
    except (TypeError, ValueError):
        return {"ok": False, "reason": "bad_question_budget"}
    if qb not in (0, 1):
        return {"ok": False, "reason": "bad_question_budget"}
    if qb > 0 and str(d.get("reply_expectation")) == "none":
        return {"ok": False, "reason": "question_without_expectation"}
    if _parse_iso(d.get("expires_at")) <= _now(now):
        return {"ok": False, "reason": "plan_expired"}
    if str(d.get("evidence_state")) not in EVIDENCE_STATES:
        return {"ok": False, "reason": "bad_evidence_state"}
    if (str(d.get("evidence_state")) != "eligible"
            and d.get("eligible_claim_ids")):
        return {"ok": False, "reason": "claims_without_eligibility"}
    if str(d.get("sensitivity")) == "forbidden":
        return {"ok": False, "reason": "forbidden_sensitivity"}
    if not d.get("seed_refs") or not d.get("source_types"):
        return {"ok": False, "reason": "missing_sources"}
    return {"ok": True, "reason": ""}


# ---------------------------------------------------------------------------
# 只读提供者
# ---------------------------------------------------------------------------
def _safe(fn, errors: dict, name: str, default):
    try:
        return fn()
    except Exception as exc:
        errors[name] = f"{type(exc).__name__}: {_short(exc, 100)}"
        return default


def collect_seeds(*, mem=None, mind=None, autonomy=None, p3=None,
                  now: Optional[float] = None,
                  limit: Optional[int] = None) -> tuple[list[TopicSeed], dict]:
    """从现有只读接口采集最小快照；每个提供者单独失败，不阻断其他来源。"""
    at = _now(now)
    cap = max(1, min(32, int(limit if limit is not None else max_candidates())))
    seeds: list[TopicSeed] = []
    errors: dict[str, str] = {}

    def add(seed: Optional[TopicSeed]) -> None:
        if seed is not None and len(seeds) < cap:
            seeds.append(seed)

    # 1) 到期承诺：只读；不创建、不延长。
    if p3 is not None:
        due = _safe(lambda: p3.due_promises(at), errors, "promise", [])
        for p in (due or [])[:1]:
            text = _short(p.get("text") or "", 80)
            if text:
                add(make_seed(
                    source_type="commitment", source_refs=[
                        "promise:" + str(p.get("id") or "")],
                    content_hint=text, now=at, ttl_hours=24,
                    why_code="promise_due", thread_id="promise",
                    evidence_state="eligible"))

    # 2) 已审学习成果：只读已完成、来源可定位的 claim。
    if autonomy is not None:
        learning = getattr(autonomy, "learning", None)
        if learning is not None:
            cards = _safe(lambda: learning.approved(limit=8), errors,
                          "learning", [])
            for card in (cards or [])[:2]:
                points = card.get("points") or []
                claim = ""
                for p in points:
                    if isinstance(p, dict) and str(p.get("claim") or "").strip():
                        claim = _short(p.get("claim"), 110)
                        break
                if not claim:
                    continue
                kid = "k:" + str(card.get("id") or "")
                add(make_seed(
                    source_type="learning_result", source_refs=[kid],
                    content_hint=claim, now=at, ttl_hours=72,
                    why_code="completed_result_available",
                    evidence_state="eligible", claim_ids=[kid],
                    thread_id="learning"))
            if not (cards or []):
                studies = _safe(lambda: learning.recent_studies(5), errors,
                                "study", [])
                for st in (studies or [])[:1]:
                    if str(st.get("status") or "") != "studied":
                        continue
                    q = _short(st.get("question") or "", 80)
                    if q:
                        add(make_seed(
                            source_type="learning_result",
                            source_refs=["study:" + str(st.get("id") or "")],
                            content_hint=q, now=at, ttl_hours=48,
                            why_code="study_session_available",
                            evidence_state="contextual_only",
                            thread_id="learning"))

    # 3) 共同经历：只当背景，不把原文补成事实。
    if mem is not None:
        eps = _safe(lambda: mem.recent_episodes(5), errors, "memory", [])
        for ep in (eps or [])[:2]:
            if str(ep.get("status") or "active") != "active":
                continue
            ts = float(ep.get("ts") or 0.0)
            age_h = max(0.0, (at - ts) / 3600.0) if ts else 9999
            if age_h < 1.0 or age_h > 14 * 24:
                continue
            text = _short(ep.get("text") or "", 90)
            if not text:
                continue
            add(make_seed(
                source_type="shared_event", source_refs=[
                    "mem:" + str(ep.get("id") or "")],
                content_hint=text, now=at, ttl_hours=24,
                why_code="shared_thread_continuity",
                evidence_state="contextual_only", thread_id="memory"))

    # 4) 具体互动观察：只看最近一条用户原话里明确提到的状态。
    last_user = None
    if mem is not None:
        turns = _safe(lambda: mem.recent_turns(10), errors, "turns", [])
        for t in reversed(turns or []):
            if str(t.get("role") or "") == "user":
                last_user = t
                break
    if last_user:
        ts = float(last_user.get("ts") or 0.0)
        if ts and (at - ts) <= 6 * 3600:
            text = _short(last_user.get("text") or "", 80)
            if len(text) >= 3:
                if any(k in text for k in _PLAY_CUES):
                    add(make_seed(
                        source_type="imagination",
                        source_refs=["turn:" + str(int(ts))],
                        content_hint="一个可以一起玩的小设定", now=at,
                        ttl_hours=6, why_code="imagination_window",
                        evidence_state="contextual_only", thread_id="play"))
                elif any(k in text for k in _CARE_CUES):
                    add(make_seed(
                        source_type="observation",
                        source_refs=["turn:" + str(int(ts))],
                        content_hint=text, now=at, ttl_hours=6,
                        why_code="user_state_observed",
                        evidence_state="eligible", thread_id="care"))

    # 5) 当轮情绪：只用于态度/关心，不成为事实证据。
    if mind is not None and mem is not None:
        snap = _safe(lambda: mind.snapshot(at), errors, "affect", {})
        feel = (snap or {}).get("feel") or {}
        try:
            dom, strength = _affect.dominant(feel)
            intensity = float(_affect.intensity(feel))
        except Exception:
            dom, strength, intensity = "", 0.0, 0.0
        if dom and dom not in ("平静", "专注") and intensity >= 0.26:
            eps = _safe(lambda: mem.recent_episodes(1), errors,
                        "affect_memory", [])
            if eps:
                ep = eps[0]
                text = _short(ep.get("text") or "", 50)
                if text:
                    add(make_seed(
                        source_type="affect_state",
                        source_refs=["affect:" + dom,
                                     "mem:" + str(ep.get("id") or "")],
                        content_hint=f"{dom}；关于{text}", now=at,
                        ttl_hours=6, why_code="affect_shift",
                        evidence_state="contextual_only", thread_id="affect"))

    return seeds[:cap], errors


def plan_candidates(*, seeds: Iterable[TopicSeed], intent_whitelist: Optional[
        Iterable[str]] = None, now: Optional[float] = None,
        recent_novelty: Optional[Iterable[str]] = None,
        mode_name: Optional[str] = None) -> list[ProactivePlan]:
    """确定性生成并排序候选；没有合格候选就返回空列表。"""
    m = mode(mode_name)
    if m == "off":
        return []
    allowed = set(enabled_intents() if intent_whitelist is None
                  else [x for x in intent_whitelist if x in INTENTS])
    if not allowed:
        return []
    seen_novelty = {str(x) for x in (recent_novelty or []) if str(x).strip()}
    at = _now(now)
    out: list[ProactivePlan] = []
    for seed in seeds:
        if validate_seed(seed, at).get("ok") is not True:
            continue
        for intent in _SOURCE_INTENTS.get(seed.source_type, ()):
            if intent not in allowed:
                continue
            try:
                plan = plan_for_seed(seed, intent, at)
            except Exception:
                continue
            if validate_plan(plan, at).get("ok") is not True:
                continue
            if plan.novelty_key in seen_novelty or _is_failed(plan.novelty_key):
                continue
            out.append(plan)
    # 稳定二级键：分数 → 种子创建时间 → seed_id → intent；不使用随机数。
    def _key(p: ProactivePlan) -> tuple:
        base = float(_INTENT_PRIORITY.get(p.intent, 0.5))
        if p.evidence_state == "eligible":
            base += 0.06
        try:
            created = _parse_iso(p.expires_at) - 72 * 3600
        except Exception:
            created = 0.0
        return (-round(base, 4), created, p.seed_refs[0] if p.seed_refs else "",
                p.intent, p.plan_id)
    out.sort(key=_key)
    # 同一 novelty_key 只留一个。
    dedup: list[ProactivePlan] = []
    seen: set[str] = set()
    for p in out:
        if p.novelty_key in seen:
            continue
        seen.add(p.novelty_key)
        dedup.append(p)
    return dedup[:max_candidates()]


def choose_plan(*, seeds: Iterable[TopicSeed], intent_whitelist: Optional[
        Iterable[str]] = None, now: Optional[float] = None,
        recent_novelty: Optional[Iterable[str]] = None,
        mode_name: Optional[str] = None) -> Optional[ProactivePlan]:
    rows = plan_candidates(
        seeds=seeds, intent_whitelist=intent_whitelist, now=now,
        recent_novelty=recent_novelty, mode_name=mode_name)
    return rows[0] if rows else None


# ---------------------------------------------------------------------------
# 生成指令与审计
# ---------------------------------------------------------------------------
def prompt_directive(plan: ProactivePlan | dict, style_block: str = "") -> str:
    p = plan.as_dict() if isinstance(plan, ProactivePlan) else dict(plan or {})
    intent = str(p.get("intent") or "")
    label = INTENT_RULES.get(intent, {}).get("label", intent)
    qb = int(p.get("question_budget") or 0)
    reply = str(p.get("reply_expectation") or "none")
    lines = [
        "【这一轮是你先开口】",
        f"主动意图：{label}。",
        f"为什么是现在：{p.get('why_now') or ''}",
        f"这一轮的新价值：{p.get('new_value') or ''}",
        f"内容目标：{p.get('content_goal') or ''}",
    ]
    evidence = str(p.get("evidence_state") or "")
    sources = set(p.get("source_types") or [])
    if "imagination" in sources:
        lines.append("边界：这是你明确标记的想象或游戏，不要冒充真实经历、"
                     "外部事实或你查到的结论。")
    elif evidence == "eligible" and p.get("eligible_claim_ids"):
        lines.append("边界：事实只用已审过的 claim，不要扩写成新事实，"
                     "也不要引用未经审核的材料。")
    elif evidence == "contextual_only":
        lines.append("边界：来源只能当背景，不补细节，不说成确定事实；"
                     "不确定就不说。")
    if qb <= 0:
        lines.append("不要提问，不要用问号收尾，也不要要求他回应；"
                     "他不接这句话也应当完整成立。")
    else:
        lines.append("最多只问一个确实需要他回答的问题；不要用寒暄包装成提问。")
    if reply == "none":
        lines.append("回复期待：说完就结束，不等待回答。")
    elif reply == "open":
        lines.append("回复期待：他可以接，也可以不接；不追问、不换题补偿。")
    else:
        lines.append("回复期待：确有需要他回答的事项，直接说清楚；仍只发一次。")
    lines.append("长度：一到两句短话为主，不要写成小作文。")
    lines.append("只写你要发出去的那一句；不要解释你为什么发，不要汇报状态，"
                 "不要列表，不要总结。")
    if style_block:
        lines += ["", style_block]
    return "\n".join(lines).strip()


def _log_path(path: Optional[Path | str] = None) -> Path:
    if path is not None:
        return Path(path)
    return Path(getattr(C, "PROACTIVE_TOPIC_DIR", C.DATA / "proactive_topic")) \
        / "events.jsonl"


def log_plan(plan: ProactivePlan | dict, *, mode_name: str,
             trigger_event_id: str = "proactive",
             candidate_added_to_real_set: bool = False,
             visible_message_created: bool = False,
             followup_created: bool = False,
             fallback_reason: Optional[str] = None,
             extra: Optional[dict] = None,
             path: Optional[Path | str] = None,
             enabled: Optional[bool] = None) -> Optional[dict]:
    """只写运行日志；不写长期记忆、不写关系。shadow 也走这里。"""
    on = (getattr(C, "PROACTIVE_TOPIC_LOG", True) if enabled is None
          else bool(enabled))
    if not on:
        return None
    p = plan.as_dict() if isinstance(plan, ProactivePlan) else dict(plan or {})
    row = {
        "ts": _iso(time.time()),
        "trigger_event_id": str(trigger_event_id or "proactive")[:80],
        "plan_id": str(p.get("plan_id") or ""),
        "mode": str(mode_name or "off"),
        "intent": str(p.get("intent") or ""),
        "source_types": list(p.get("source_types") or []),
        "why_now_code": str(p.get("why_now_code") or ""),
        "new_value_code": str(p.get("new_value_code") or ""),
        "reply_expectation": str(p.get("reply_expectation") or ""),
        "question_budget": int(p.get("question_budget") or 0),
        "seed_state": "available",
        "evidence_state": str(p.get("evidence_state") or ""),
        "gate_result": ("not_applicable_in_shadow"
                        if str(mode_name) == "shadow" else "pending_existing_gate"),
        "candidate_added_to_real_set": bool(candidate_added_to_real_set),
        "real_llm_calls_added": 0,
        "visible_message_created": bool(visible_message_created),
        "followup_created": bool(followup_created),
        "affect_relationship_writes": 0,
        "fallback_reason": fallback_reason,
        "content_goal_sha": _hash(p.get("content_goal") or "", 12),
        "content_goal_chars": len(str(p.get("content_goal") or "")),
        "novelty_key": str(p.get("novelty_key") or ""),
    }
    if extra:
        row.update({str(k): v for k, v in extra.items()})
    target = _log_path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with _LOG_LOCK:
            with target.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        return None
    return row


def log_shadow_once(plan: ProactivePlan | dict, *,
                    trigger_event_id: str = "proactive",
                    path: Optional[Path | str] = None,
                    cooldown_s: float = 6 * 3600,
                    extra: Optional[dict] = None) -> Optional[dict]:
    """shadow 同一计划不每 20 秒刷一遍日志；按 novelty 去重。"""
    p = plan.as_dict() if isinstance(plan, ProactivePlan) else dict(plan or {})
    key = str(p.get("novelty_key") or p.get("plan_id") or "")
    if not key:
        return None
    now = time.time()
    with _LOG_LOCK:
        last = float(_SHADOW_LOGGED.get(key) or 0.0)
        if last and now - last < cooldown_s:
            return None
        _SHADOW_LOGGED[key] = now
        if len(_SHADOW_LOGGED) > 256:
            for k in sorted(_SHADOW_LOGGED, key=_SHADOW_LOGGED.get)[:64]:
                _SHADOW_LOGGED.pop(k, None)
    return log_plan(
        p, mode_name="shadow", trigger_event_id=trigger_event_id,
        candidate_added_to_real_set=False, visible_message_created=False,
        fallback_reason=None, path=path, extra=extra)


def mark_failed(plan: ProactivePlan | dict, *, ttl_s: float = 2 * 3600) -> None:
    """同一计划生成失败后短期不再重试，避免每 20 秒重打模型。"""
    p = plan.as_dict() if isinstance(plan, ProactivePlan) else dict(plan or {})
    key = str(p.get("novelty_key") or p.get("plan_id") or "")
    if not key:
        return
    with _LOG_LOCK:
        _FAILED[key] = time.time() + max(60.0, float(ttl_s))
        if len(_FAILED) > 256:
            now = time.time()
            for k in [k for k, until in _FAILED.items() if until <= now][:64]:
                _FAILED.pop(k, None)


def _is_failed(key: str) -> bool:
    with _LOG_LOCK:
        until = float(_FAILED.get(str(key)) or 0.0)
        if until <= 0:
            return False
        if until <= time.time():
            _FAILED.pop(str(key), None)
            return False
        return True


def reset_for_tests() -> None:
    with _LOG_LOCK:
        _SHADOW_LOGGED.clear()
        _FAILED.clear()
