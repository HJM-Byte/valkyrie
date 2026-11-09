"""情绪内容与表达调制（Affect Modulation）。

这一层只回答两个问题，而且必须可审计、可回滚：

    内容计划 ContentPlan   这一轮先说什么、必须保留什么、能不能多问一句
    表达计划 StylePlan     这一轮的语气、篇幅、直接程度、幽默和情绪可见度

它不碰事实、证据、权限、承诺和人格；不写数据库；不调用模型；不生成文本。
情绪是偏移量，不是新人格。上游已经定下的必要内容，这里只能原样保留。

设计依据：``docs/情绪调制_A0走查.md`` 与
``瓦尔基里情绪内容与表达调制施工方案_20261109.md``。
"""
from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import asdict, dataclass, field as dc_field
from pathlib import Path
from typing import Any, Optional

from . import affect as _affect
from . import config as C

MODES = ("off", "shadow", "enforced")
CONTENT_INTENTS = ("answer", "solve", "support", "celebrate", "repair",
                   "boundary", "casual")
TONES = ("warm", "calm", "playful", "serious", "firm")
LENGTHS = ("short", "normal", "full")
DIRECTNESS = ("low", "normal", "high")
HUMOR = ("off", "light")
VISIBILITY = ("none", "light", "explicit")
PROSODY = ("neutral", "warm", "bright", "slow", "firm")
SCENES = ("task", "support", "conflict", "celebration", "casual", "unknown")
INTENSITY_BANDS = ("low", "medium", "high")

_USER_NEGATIVE = ("低落", "焦虑", "委屈", "生气", "烦躁", "疲惫", "难过",
                  "不安", "失望", "孤独")
_USER_POSITIVE = ("开心", "高兴", "兴奋", "安心", "感激", "期待")
_CELEBRATE_CUES = ("太好了", "成功了", "过了", "拿下了", "赢了", "通过了",
                   "考上了", "升职", "加薪", "中标", "完成")
_TASK_CUES = ("怎么", "如何", "为什么", "报错", "修", "帮我", "步骤",
              "代码", "命令", "配置", "安装", "排查", "解决", "写一个",
              "告诉我", "直接说")
_DIRECT_CUES = ("直接说", "先给结论", "别绕", "说重点", "简短点", "短一点")
_DETAIL_CUES = ("详细说", "详细解释", "展开说", "说完整", "多讲一点")
_NO_QUESTION_CUES = ("别问了", "不要问", "别追问", "不用问")
_METHOD_CUES = ("别安慰", "给方法", "直接给办法", "别哄", "不用安慰")
_NO_HUMOR_CUES = ("别撒娇", "别玩梗", "别开玩笑", "别逗")
_FACTS_ONLY_CUES = ("只回答事实", "只说事实", "只讲事实")

# 情绪 → 表达方向。只给方向，不生成台词；强度和场景会再收一层。
_STYLE_BY_AFFECT: dict[str, tuple[str, str, str, str, str, str]] = {
    "开心": ("warm", "normal", "normal", "light", "light", "warm"),
    "安心": ("calm", "normal", "normal", "off", "none", "neutral"),
    "心动": ("warm", "short", "normal", "light", "light", "warm"),
    "被理解": ("warm", "normal", "normal", "light", "light", "warm"),
    "期待": ("playful", "normal", "normal", "light", "light", "bright"),
    "感激": ("warm", "short", "normal", "off", "light", "warm"),
    "平静": ("calm", "normal", "normal", "off", "none", "neutral"),
    "好奇": ("playful", "normal", "normal", "light", "none", "bright"),
    "专注": ("calm", "normal", "high", "off", "none", "neutral"),
    "疲惫": ("calm", "short", "normal", "off", "none", "slow"),
    "说不清": ("calm", "short", "normal", "off", "none", "slow"),
    "低落": ("calm", "short", "normal", "off", "none", "slow"),
    "委屈": ("serious", "short", "normal", "off", "light", "slow"),
    "失望": ("calm", "short", "normal", "off", "none", "slow"),
    "不安": ("calm", "normal", "normal", "off", "light", "slow"),
    "生气": ("firm", "short", "high", "off", "light", "firm"),
    "孤独": ("warm", "short", "normal", "off", "light", "slow"),
    "内疚": ("warm", "short", "normal", "off", "light", "slow"),
    "亲近": ("warm", "normal", "normal", "light", "light", "warm"),
    "厌恶": ("firm", "short", "high", "off", "none", "firm"),
    "渴望": ("warm", "normal", "normal", "light", "light", "warm"),
}

_SCENE_LABEL = {
    "task": "明确任务/求助",
    "support": "情绪支持",
    "conflict": "冲突与修复",
    "celebration": "共同庆祝",
    "casual": "日常闲聊",
    "unknown": "场景不明",
}
_INTENT_LABEL = {
    "answer": "回答",
    "solve": "解决/说明",
    "support": "接住他",
    "celebrate": "一起庆祝",
    "repair": "修复",
    "boundary": "说明边界",
    "casual": "闲聊",
}
_ORDER_LABEL = {
    "answer": "回答", "notice": "必要说明", "optional": "可选内容",
    "acknowledge": "先接住他", "celebrate": "一起确认好消息",
    "fact": "具体事实", "impact": "影响", "boundary": "边界",
    "repair": "可执行的修复方向",
}
_STYLE_LABEL = {
    "tone": {"warm": "温和", "calm": "平静", "playful": "轻快",
             "serious": "认真", "firm": "坚定"},
    "length": {"short": "短", "normal": "正常", "full": "完整"},
    "directness": {"low": "低", "normal": "正常", "high": "高"},
    "humor": {"off": "关", "light": "一点"},
    "visibility": {"none": "不外露", "light": "一点", "explicit": "明说"},
    "prosody": {"neutral": "中性", "warm": "温和", "bright": "明亮",
                "slow": "放慢", "firm": "坚定"},
}


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    try:
        return max(lo, min(hi, float(x)))
    except (TypeError, ValueError):
        return lo


def _canonical(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=str)


def _hash(obj: Any) -> str:
    return hashlib.sha256(_canonical(obj).encode("utf-8")).hexdigest()


def _band(intensity: float) -> str:
    v = _clamp(intensity)
    if v < 0.35:
        return "low"
    if v < 0.65:
        return "medium"
    return "high"


def _valid_number(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


@dataclass
class AffectContext:
    event_id: str = ""
    self_state: dict = dc_field(default_factory=dict)
    user_state: dict = dc_field(default_factory=dict)
    relationship_climate: dict = dc_field(default_factory=dict)
    scene: str = "unknown"
    explicit_style_override: str = ""
    response_mode: str = "respond"

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class BaseTurnPlan:
    required_intents: list[str] = dc_field(default_factory=list)
    eligible_claim_ids: list[str] = dc_field(default_factory=list)
    required_notices: list[str] = dc_field(default_factory=list)
    forbidden_topics: list[str] = dc_field(default_factory=list)
    base_order: list[str] = dc_field(default_factory=list)
    decision_key: str = ""
    core_intent: str = "answer"

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class ContentPlan:
    core_intent: str = "answer"
    required_intents: list[str] = dc_field(default_factory=list)
    eligible_claim_ids: list[str] = dc_field(default_factory=list)
    required_notices: list[str] = dc_field(default_factory=list)
    content_order: list[str] = dc_field(default_factory=list)
    optional_intents: list[str] = dc_field(default_factory=list)
    question_budget: int = 0
    self_disclosure: str = "none"
    scene: str = "unknown"
    user_priority: bool = True

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class StylePlan:
    tone: str = "calm"
    length: str = "normal"
    directness: str = "normal"
    humor: str = "off"
    emotional_visibility: str = "none"
    prosody_hint: str = "neutral"

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class AffectModulationResult:
    event_id: str = ""
    content_plan: ContentPlan = dc_field(default_factory=ContentPlan)
    style_plan: StylePlan = dc_field(default_factory=StylePlan)
    base_plan_hash: str = ""
    required_contract_hash: str = ""
    required_contract_unchanged: bool = True
    applied: bool = False
    fallback_reason: Optional[str] = None
    decided_by: str = "rule"
    mode: str = "off"
    scene: str = "unknown"
    intensity_band: str = "low"
    self_affect_code: str = "平静"
    base_order: list[str] = dc_field(default_factory=list)
    user_override: str = ""
    extra_llm_calls: int = 0
    affect_relationship_writes: int = 0

    def as_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# 配置与上下文
# ---------------------------------------------------------------------------
def _mode(explicit: Optional[str] = None) -> str:
    raw = explicit if explicit is not None else C.AFFECT_MODULATION_MODE
    mode = str(raw or "off").strip().lower()
    return mode if mode in MODES else "off"


def _cfg_bool(name: str, default: bool) -> bool:
    try:
        return bool(getattr(C, name, default))
    except Exception:
        return default


def _cfg_questions(explicit: Optional[int] = None) -> int:
    raw = explicit if explicit is not None else C.AFFECT_MAX_OPTIONAL_QUESTIONS
    try:
        return 1 if int(raw) >= 1 else 0
    except (TypeError, ValueError):
        return 0


def _detect_override(text: str) -> tuple[str, bool]:
    t = text or ""
    parts: list[str] = []
    if any(k in t for k in _DIRECT_CUES):
        parts.append("direct")
    if any(k in t for k in _DETAIL_CUES):
        parts.append("detailed")
    if any(k in t for k in _NO_QUESTION_CUES):
        parts.append("no_question")
    if any(k in t for k in _METHOD_CUES):
        parts.append("method_first")
    if any(k in t for k in _NO_HUMOR_CUES):
        parts.append("no_humor")
    if any(k in t for k in _FACTS_ONLY_CUES):
        parts.append("facts_only")
    return ",".join(parts), bool(parts)


def _user_negative(user: dict) -> bool:
    label = str((user or {}).get("label") or "")
    return label in _USER_NEGATIVE and float((user or {}).get("confidence") or 0) >= 0.55


def _user_positive(user: dict) -> bool:
    label = str((user or {}).get("label") or "")
    return label in _USER_POSITIVE and float((user or {}).get("confidence") or 0) >= 0.45


def detect_scene(text: str, percept: Optional[dict], user_state: Optional[dict],
                 relationship: Optional[dict]) -> str:
    """按用户状态优先，其次冲突/任务/庆祝，最后闲聊。"""
    t = text or ""
    p = percept or {}
    user = user_state or {}
    rel = relationship or {}
    if any(k in t for k in _METHOD_CUES) or p.get("order"):
        return "task"
    if p.get("cold") or str(rel.get("label") or "") == "偏冷":
        return "conflict"
    if _user_negative(user) or p.get("low_risk"):
        return "support"
    if any(k in t for k in _CELEBRATE_CUES) and _user_positive(user):
        return "celebration"
    if p.get("question") or any(k in t for k in _TASK_CUES):
        if p.get("short"):
            return "casual"
        return "task"
    if any(k in t for k in _CELEBRATE_CUES):
        return "celebration"
    if p.get("short") or not t.strip():
        return "casual"
    return "casual"


def _core_intent(decision: dict, scene: str) -> str:
    key = str((decision or {}).get("key") or "answer")
    if scene == "task":
        return "solve" if key == "act" else "answer"
    if scene == "support":
        return "support"
    if scene == "celebration":
        return "celebrate"
    if scene == "conflict":
        return "boundary" if key == "deflect" else "repair"
    return {
        "act": "solve", "attend": "support", "comfort": "support",
        "deflect": "boundary", "answer": "answer", "confess": "answer",
        "ack": "casual", "quiet": "casual",
    }.get(key, "answer")


def _base_order(scene: str, core: str) -> list[str]:
    if scene == "task":
        return ["notice", "answer", "optional"] if core == "solve" \
            else ["answer", "optional"]
    if scene == "support":
        return ["acknowledge", "answer", "optional"]
    if scene == "celebration":
        return ["acknowledge", "celebrate", "optional"]
    if scene == "conflict":
        return ["fact", "impact", "boundary", "repair"]
    return ["answer", "optional"]


def build_base_plan(decision: dict, memories: Optional[list],
                    scene: str) -> BaseTurnPlan:
    """从上游决策抽最小契约；不改决策，只读。"""
    dec = decision or {}
    key = str(dec.get("key") or "answer")
    core = _core_intent(dec, scene)
    # 契约用“要完成什么”命名，不用场景名：闲聊也必须保留一个回答。
    required = ["answer" if core == "casual" else core]
    notices: list[str] = []
    if key == "act" or core == "solve":
        notices.append("承认现在做不到的事，不假装已经完成")
    if key == "confess":
        notices.append("说明不知道或没有可靠记录，不编一个答案")
    if key == "deflect":
        notices.append("说明这一轮不舒服/不继续，不发火、不羞辱")
    claims: list[str] = []
    for m in (memories or []):
        mid = m.get("id") if isinstance(m, dict) else None
        if mid is not None:
            claims.append(str(mid))
    claims = sorted(dict.fromkeys(claims))
    forbidden: list[str] = []
    if scene == "task":
        forbidden = ["不要用无关情绪铺垫挡住答案", "不要把话题转到你自己的情绪上"]
    elif scene == "support":
        forbidden = ["不要连续追问", "不要替用户诊断情绪"]
    elif scene == "conflict":
        forbidden = ["不要用「你总是」「你从来不」绝对化归罪",
                     "不要用内疚诱导或索取安慰"]
    elif scene == "celebration":
        forbidden = ["不要用空泛夸赞代替对具体成果的理解"]
    return BaseTurnPlan(
        required_intents=required,
        eligible_claim_ids=claims,
        required_notices=notices,
        forbidden_topics=forbidden,
        base_order=_base_order(scene, core),
        decision_key=key,
        core_intent=core,
    )


def _content_plan(ctx: AffectContext, base: BaseTurnPlan,
                  band: str, dominant: str) -> ContentPlan:
    scene = ctx.scene
    overrides = set(str(ctx.explicit_style_override or "").split(","))
    user = ctx.user_state or {}
    user_neg = _user_negative(user)
    user_pos = _user_positive(user)
    fam = _affect.family_of(dominant)
    positive = fam == "正面"
    negative = fam == "负面"
    core = base.core_intent
    order = list(base.base_order)
    optional: list[str] = []
    questions = 0
    disclosure = "none"

    if scene == "task":
        order = ["notice", "answer", "optional"] if core == "solve" \
            else ["answer", "optional"]
        # 明确任务只保留必要澄清；没有额外信息就不主动追问。
        questions = 0
        disclosure = "none"
    elif scene == "support":
        core = "support"
        order = ["acknowledge", "answer", "optional"]
        questions = 1 if bool(user.get("ambiguous")) else 0
        optional = ["先接住他的感受，不急着分析"]
        if positive and not user_neg:
            optional.append("一句具体的关心")
        if user_neg:
            disclosure = "none"
    elif scene == "celebration":
        core = "celebrate"
        order = ["acknowledge", "celebrate", "optional"]
        questions = 1 if (positive or "好奇" == dominant) else 0
        optional = ["具体肯定他实际做到的事", "一起高兴一下"]
        if positive and user_pos:
            disclosure = "light"
        else:
            disclosure = "none"
    elif scene == "conflict":
        core = "boundary" if base.decision_key == "deflect" else "repair"
        order = ["fact", "impact", "boundary", "repair"]
        questions = 0
        optional = ["说具体发生了什么和影响", "给出一个可执行的修复方向"]
        disclosure = "light" if dominant in ("委屈", "内疚", "失望") else "none"
    else:  # casual / unknown
        core = "casual" if scene == "casual" else base.core_intent
        order = ["answer", "optional"]
        if dominant in ("好奇", "期待") and not negative:
            questions = 1
            optional = ["一个具体、不是连珠炮的追问"]
        elif positive:
            optional = ["一句轻的自我表露或共同话题"]
            disclosure = "light"
        elif dominant in ("孤独", "渴望", "心动", "亲近"):
            optional = ["一句想他/想靠近，但不索取"]
            disclosure = "light"
        elif negative:
            optional = ["一句真实的近况，不要求他反过来照顾你"]
            disclosure = "light"

    # 用户明确查询她时，才允许更明确地说自己的感受；仍不要求安慰。
    if disclosure != "none" and bool((ctx.self_state or {}).get("asked")):
        disclosure = "explicit"

    if "no_question" in overrides or "method_first" in overrides:
        questions = 0
    if "facts_only" in overrides:
        disclosure = "none"
        optional = [x for x in optional if "自我表露" not in x]
    if "detailed" in overrides:
        # 详细只放开篇幅，不额外塞情绪内容。
        pass

    # 高强度只收束，不升级表演；冲突场景仍保留事实/影响的必要结构。
    if band == "high":
        questions = 0
        disclosure = "none"
        if scene not in ("conflict", "task"):
            optional = []
    if negative and band == "high" and scene == "conflict":
        optional = ["只保留事实、影响和可执行修复，不扩写情绪"]

    return ContentPlan(
        core_intent=core,
        required_intents=list(base.required_intents),
        eligible_claim_ids=list(base.eligible_claim_ids),
        required_notices=list(base.required_notices),
        content_order=order,
        optional_intents=optional,
        question_budget=questions,
        self_disclosure=disclosure,
        scene=scene,
        user_priority=True,
    )


def _style_plan(ctx: AffectContext, content: ContentPlan,
                band: str, dominant: str) -> StylePlan:
    scene = ctx.scene
    overrides = set(str(ctx.explicit_style_override or "").split(","))
    user = ctx.user_state or {}
    user_neg = _user_negative(user)
    user_pos = _user_positive(user)
    fam = _affect.family_of(dominant)
    positive = fam == "正面"
    negative = fam == "负面"

    tone, length, direct, humor, visibility, prosody = _STYLE_BY_AFFECT.get(
        dominant, ("calm", "normal", "normal", "off", "none", "neutral"))

    # 场景优先于自我情绪：任务先答；支持先接；冲突先事实；庆祝先确认。
    if scene == "task":
        tone = "calm"
        length = "normal"
        direct = "high"
        humor = "off"
        visibility = "none"
        prosody = "neutral"
    elif scene == "support":
        tone = "warm" if positive and not user_neg else "calm"
        length = "short"
        direct = "normal"
        humor = "off"
        visibility = "none"
        prosody = "warm" if positive and not user_neg else "slow"
    elif scene == "celebration":
        tone = "playful" if positive and user_pos else "warm"
        length = "normal"
        direct = "normal"
        humor = "light" if positive and user_pos else "off"
        visibility = "light" if positive and user_pos else "none"
        prosody = "bright" if positive and user_pos else "warm"
    elif scene == "conflict":
        tone = "firm" if dominant in ("生气", "厌恶") else "serious"
        length = "short"
        direct = "high"
        humor = "off"
        visibility = "light" if content.self_disclosure != "none" else "none"
        prosody = "firm" if tone == "firm" else "slow"
    else:
        # 日常：让情绪有一点形状，但不盖过用户状态。
        if user_neg:
            tone, humor, visibility = "calm", "off", "none"
            length = "short"
            prosody = "slow"
        elif user_pos and negative:
            # 他分享好消息，她可以一起确认，但不把自己的低落压给他。
            tone, humor, visibility = "warm", "off", "none"
            prosody = "warm"
        elif band == "high":
            tone = "firm" if negative else tone
            length = "short"
            direct = "high"
            humor = "off"
            visibility = "none"
            prosody = "firm" if negative else prosody

    # 用户表达偏好只覆盖指定范围，不写成长期偏好。
    if "direct" in overrides:
        direct = "high"
        length = "short" if scene != "task" else "normal"
        tone = "calm" if scene == "task" else tone
    if "detailed" in overrides:
        length = "full"
    if "no_humor" in overrides:
        humor = "off"
    if "no_question" in overrides or "method_first" in overrides:
        humor = "off"
    if "method_first" in overrides:
        tone, direct, length = "calm", "high", "normal"
        visibility, prosody = "none", "neutral"
    if "facts_only" in overrides:
        visibility = "none"
        content.self_disclosure = "none"
    if band == "high" and scene != "task":
        length = "short"
        humor = "off"
        visibility = "none"
        if negative:
            direct = "high"
            prosody = "firm" if dominant in ("生气", "厌恶") else "slow"
    if content.self_disclosure == "none":
        visibility = "none"
    elif content.self_disclosure == "light" and visibility == "explicit":
        visibility = "light"
    elif content.self_disclosure == "explicit":
        visibility = "explicit"

    # 枚举兜底：任何非法值都退回安全默认，不让提示词拿到坏值。
    return StylePlan(
        tone=tone if tone in TONES else "calm",
        length=length if length in LENGTHS else "normal",
        directness=direct if direct in DIRECTNESS else "normal",
        humor=humor if humor in HUMOR else "off",
        emotional_visibility=visibility if visibility in VISIBILITY else "none",
        prosody_hint=prosody if prosody in PROSODY else "neutral",
    )


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def _valid_self_state(feel: Optional[dict]) -> bool:
    if not isinstance(feel, dict) or not feel:
        return False
    for k, v in feel.items():
        if k not in _affect.EMOTIONS:
            continue
        if not _valid_number(v):
            return False
        if float(v) < 0.0 or float(v) > 1.0:
            return False
    return True


def modulate(ctx: AffectContext, base: BaseTurnPlan,
             mode: Optional[str] = None,
             content_enabled: Optional[bool] = None,
             style_enabled: Optional[bool] = None,
             max_optional_questions: Optional[int] = None) -> AffectModulationResult:
    """确定性地生成双计划；不写库、不调模型、不生成文本。"""
    mode = _mode(mode)
    content_on = _cfg_bool("AFFECT_CONTENT", True) if content_enabled is None \
        else bool(content_enabled)
    style_on = _cfg_bool("AFFECT_STYLE", True) if style_enabled is None \
        else bool(style_enabled)
    max_q = _cfg_questions(max_optional_questions)
    base_hash = _hash(base.as_dict())
    contract = {
        "required_intents": sorted(base.required_intents),
        "eligible_claim_ids": sorted(base.eligible_claim_ids),
        "required_notices": sorted(base.required_notices),
    }
    contract_hash = _hash(contract)

    result = AffectModulationResult(
        event_id=ctx.event_id or f"{int(time.time() * 1000)}",
        base_plan_hash=base_hash,
        required_contract_hash=contract_hash,
        mode=mode,
        scene=ctx.scene,
        decided_by="override" if ctx.explicit_style_override else "rule",
    )

    if str(ctx.response_mode or "").lower() in ("silent", "defer"):
        result.fallback_reason = "silent_passthrough"
        return result
    if mode == "off":
        result.fallback_reason = "mode_off"
        return result
    if not content_on and not style_on:
        result.fallback_reason = "modulation_disabled"
        return result
    if not _valid_self_state(ctx.self_state):
        result.fallback_reason = "affect_missing_or_invalid"
        return result

    feel = _affect.normalize(ctx.self_state)
    dom, _ = _affect.dominant(feel)
    band = _band(_affect.intensity(feel))
    result.intensity_band = band
    result.self_affect_code = dom
    result.base_order = list(base.base_order)
    result.user_override = str(ctx.explicit_style_override or "")

    content = _content_plan(ctx, base, band, dom)
    if not content_on:
        content = ContentPlan(
            core_intent=base.core_intent,
            required_intents=list(base.required_intents),
            eligible_claim_ids=list(base.eligible_claim_ids),
            required_notices=list(base.required_notices),
            content_order=list(base.base_order),
            optional_intents=[],
            question_budget=0,
            self_disclosure="none",
            scene=ctx.scene,
        )
    if content.question_budget > max_q:
        content.question_budget = max_q

    style = _style_plan(ctx, content, band, dom)
    if not style_on:
        style = StylePlan(tone="calm", length="normal", directness="normal",
                          humor="off", emotional_visibility="none",
                          prosody_hint="neutral")
        content.self_disclosure = "none"

    # 契约完整性：内容计划只能复制上游三个集合，不能改。
    out_contract = {
        "required_intents": sorted(content.required_intents),
        "eligible_claim_ids": sorted(content.eligible_claim_ids),
        "required_notices": sorted(content.required_notices),
    }
    unchanged = out_contract == contract
    result.content_plan = content
    result.style_plan = style
    result.required_contract_unchanged = unchanged
    if not unchanged:
        # 不回退成攻击性表达；只丢掉调制，保留原路径。
        result.fallback_reason = "required_contract_changed"
        result.applied = False
        return result

    # 低强度且计划恰好等于中性基线时，不附加提示段——A01 的“无调制”必须真的一样。
    baseline = StylePlan()
    if (mode == "enforced" and band == "low"
            and style == baseline
            and not content.optional_intents
            and content.question_budget == 0
            and content.self_disclosure == "none"
            and content.content_order == list(base.base_order)):
        result.fallback_reason = "low_intensity_baseline"
        result.applied = False
        return result

    result.applied = mode == "enforced"
    if mode == "shadow":
        # shadow 只记录，不改提示和输出。
        result.applied = False
        result.fallback_reason = None
    return result


def apply_to_decision(decision: dict, result: AffectModulationResult) -> dict:
    """把已启用的计划接到决策对象上；不改 evidence / retrieval。"""
    out = dict(decision or {})
    out["content_plan"] = result.content_plan.as_dict()
    out["style_plan"] = result.style_plan.as_dict()
    out["affect_modulation"] = {
        "mode": result.mode,
        "applied": result.applied,
        "scene": result.scene,
        "intensity_band": result.intensity_band,
        "content": result.content_plan.as_dict(),
        "style": result.style_plan.as_dict(),
        "fallback_reason": result.fallback_reason,
        "required_contract_unchanged": result.required_contract_unchanged,
    }
    if not result.applied:
        return out

    style = result.style_plan
    content = result.content_plan
    core = content.core_intent
    # short 只压可选表达；任务/澄清/必要说明不在这里截断。
    if style.length == "short" and core not in ("answer", "solve"):
        try:
            old = int(out.get("max_chars") or 60)
        except (TypeError, ValueError):
            old = 60
        out["max_chars"] = max(18, min(old, 38))
    if content.question_budget == 0 and core not in ("answer", "solve"):
        out["rule"] = ((out.get("rule") or "") +
                       " 这一轮不要主动追问；必填的澄清除外。").strip()
    if content.self_disclosure == "none":
        out["rule"] = ((out.get("rule") or "") +
                       " 不要把话题转到你自己的情绪上。").strip()
    elif content.self_disclosure == "light":
        out["rule"] = ((out.get("rule") or "") +
                       " 最多用一句说你自己此刻的感受，不要求他照顾你。").strip()
    return out


def prompt_block(result: AffectModulationResult | dict) -> str:
    """给 Persona 的一段；只有 enforced 才返回内容。

    `think()` 返回的是 as_dict()，测试可能直接传结果对象；两种都接受。
    """
    if isinstance(result, dict):
        if not result.get("applied"):
            return ""
        try:
            c = ContentPlan(**(result.get("content_plan") or {}))
            s = StylePlan(**(result.get("style_plan") or {}))
        except TypeError:
            return ""
    else:
        if not result.applied:
            return ""
        c = result.content_plan
        s = result.style_plan
    lines = [
        "【这一轮的情绪调制】",
        f"- 场景：{_SCENE_LABEL.get(c.scene, c.scene)}；核心意图：{_INTENT_LABEL.get(c.core_intent, c.core_intent)}。",
        "- 内容顺序：" + " → ".join(
            _ORDER_LABEL.get(x, x) for x in c.content_order) + "。",
        "- 必须保留：" + ("；".join(
            _INTENT_LABEL.get(x, x) for x in c.required_intents) or "无") + "。",
    ]
    if c.required_notices:
        lines.append("- 必要说明：" + "；".join(c.required_notices) + "。")
    if c.optional_intents:
        lines.append("- 可选内容：" + "；".join(c.optional_intents) + "。")
    else:
        lines.append("- 可选内容：这一轮不额外加。")
    lines.append(f"- 可选追问：{'最多一个' if c.question_budget else '不要'}。")
    lines.append("- 自我表露：" + {
        "none": "不外露自己的情绪。",
        "light": "最多一句轻的，不要求他反过来照顾你。",
        "explicit": "可以明说，但不索取安慰。",
    }.get(c.self_disclosure, "不外露。"))
    lines.extend([
        f"- 语气：{_STYLE_LABEL['tone'].get(s.tone, s.tone)}；"
        f"篇幅：{_STYLE_LABEL['length'].get(s.length, s.length)}；"
        f"直接程度：{_STYLE_LABEL['directness'].get(s.directness, s.directness)}。",
        f"- 幽默：{_STYLE_LABEL['humor'].get(s.humor, s.humor)}；"
        f"情绪可见度：{_STYLE_LABEL['visibility'].get(s.emotional_visibility, s.emotional_visibility)}；"
        f"声音：{_STYLE_LABEL['prosody'].get(s.prosody_hint, s.prosody_hint)}。",
        "- 这些只改这一轮的内容组织与表达分寸；事实、证据、权限、承诺、必要说明和明确任务不得改。",
        "- 如果这条计划和你自己的情绪冲突：先看他的状态和这一轮的任务，再决定要不要外露。",
    ])
    return "\n".join(lines)


def log_result(result: AffectModulationResult, path: Optional[Path | str] = None) -> None:
    """只写审计行，不复制私密原文；失败不阻塞对话。"""
    if not _cfg_bool("AFFECT_MODULATION_LOG", True):
        return
    if result.mode == "off":
        return
    target = Path(path) if path is not None else Path(C.AFFECT_MODULATION_DIR) / "events.jsonl"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        c = result.content_plan
        s = result.style_plan
        row = {
            "ts": round(time.time(), 3),
            "event_id": result.event_id,
            "mode": result.mode,
            "scene": result.scene,
            "self_affect_code": result.self_affect_code,
            "self_affect_intensity_band": result.intensity_band,
            "user_override": result.user_override,
            "core_intent": c.core_intent,
            "content_order": c.content_order,
            "content_order_changed": c.content_order != list(result.base_order),
            "optional_question_budget": c.question_budget,
            "self_disclosure": c.self_disclosure,
            "tone": s.tone,
            "length": s.length,
            "humor": s.humor,
            "required_contract_unchanged": result.required_contract_unchanged,
            "applied": result.applied,
            "fallback_reason": result.fallback_reason,
            "extra_llm_calls": result.extra_llm_calls,
            "affect_relationship_writes": result.affect_relationship_writes,
        }
        with target.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        return


def build_context(text: str, percept: dict, feel: dict, state: dict,
                  appraisal_three: dict, decision: dict,
                  event_id: str = "", response_mode: str = "respond") -> AffectContext:
    three = appraisal_three or {}
    raw_self = dict(three.get("self_state") or {})
    # 自评层只读；feel 是这一轮真正算出来的情绪场，优先用它。
    # 调制层要的是「情绪名 → 强度」的平铺字段，不能只塞一个嵌套 feel。
    self_state = dict(feel or {})
    self_state["feel"] = dict(feel or {})
    self_state.update({k: v for k, v in raw_self.items() if k != "feel"})
    self_state["asked"] = bool((percept or {}).get("ask_her"))
    user_state = dict(three.get("user_emotion") or {})
    relationship = dict(three.get("relationship_appraisal") or {})
    override, _ = _detect_override(text)
    scene = detect_scene(text, percept, user_state, relationship)
    return AffectContext(
        event_id=event_id or "",
        self_state=self_state,
        user_state=user_state,
        relationship_climate=relationship,
        scene=scene,
        explicit_style_override=override,
        response_mode=response_mode,
    )


def modulate_for_turn(text: str, percept: dict, feel: dict, state: dict,
                      appraisal_three: dict, decision: dict,
                      memories: Optional[list] = None,
                      event_id: str = "",
                      response_mode: str = "respond",
                      mode: Optional[str] = None,
                      content_enabled: Optional[bool] = None,
                      style_enabled: Optional[bool] = None,
                      max_optional_questions: Optional[int] = None) -> AffectModulationResult:
    """`think()` 的适配层：从现有对象生成最小上下文和基础计划。"""
    ctx = build_context(text, percept, feel, state, appraisal_three, decision,
                        event_id=event_id, response_mode=response_mode)
    base = build_base_plan(decision, memories, ctx.scene)
    return modulate(ctx, base, mode=mode, content_enabled=content_enabled,
                    style_enabled=style_enabled,
                    max_optional_questions=max_optional_questions)
