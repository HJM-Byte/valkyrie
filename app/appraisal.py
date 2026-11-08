"""三类情感对象：用户情绪假设、她的当下状态、关系评价。

这一层只读。它回答三个不同的问题，不把它们压成一个情绪值：

- `user_emotion`：他此刻可能怎样感受？依据是哪句话？有多不确定？
- `self_state`：她自己此刻的唤醒、心情、精力和情绪场是什么？
- `relationship_appraisal`：这次互动偏暖还是偏冷？它只是候选，不是关系结论。

边界写死在这里：

1. 用户明确说出的情绪优先；没有明确表达就保持未知，不从问句、命令或
   她自己的情绪反推他的心理。
2. “我没事”不自动等于平静，也不自动等于压抑；允许用户否定。
3. 单次互动只形成关系候选，不改九变量、不改人格、不写数据库。
4. 返回里的 `writes` 为空；调用方即使拿来拼提示，也不能拿它当事实裁判。
"""
from __future__ import annotations

import time
from typing import Optional

from . import affect as _affect

VERSION = 1
USER_TTL_S = 15 * 60          # 用户情绪假设的有效期：一句话只管这一小段时间
REL_TTL_S = 30 * 60           # 单次互动的关系评价，过了这段时间不再当当前判断

# 只收直接描述自己状态的词。不把“问句”“命令”“感叹”当情绪证据。
_USER_CUES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("疲惫", ("好累", "太累", "累了", "累死", "疲惫", "好困", "很困", "困了",
             "我困", "犯困", "撑不住", "没力气", "有点累", "很累", "真累", "我累")),
    ("焦虑", ("焦虑", "害怕", "紧张", "不安", "担心", "慌")),
    ("委屈", ("委屈",)),
    ("生气", ("生气", "气死", "火大", "烦死")),
    ("烦躁", ("烦躁", "好烦", "烦人", "心烦", "别烦")),
    ("低落", ("难过", "难受", "心情不好", "有点丧", "心里堵", "想哭", "失落")),
    ("开心", ("开心", "高兴", "太好了", "愉快")),
)

# 明确否定：不反推成相反情绪，也不把“没事”解释成“其实很难过”。
_NEGATIONS = ("不难过", "不难受", "不累", "不烦", "没生气", "没有生气",
              "不焦虑", "不害怕", "不委屈", "不开心", "没有难过", "没难过")

# “没事”是歧义，不是平静的同义词。只有没有其它明确情绪词时才用它。
_AMBIGUOUS = ("我没事", "没事", "没关系", "还好")

_WARM_REL = ("谢谢", "陪我", "辛苦了", "麻烦你", "对不起", "抱歉",
             "在呢", "等你", "说到做到")
_COLD_REL = ("随便你", "无所谓", "敷衍", "别问了", "好烦", "烦死",
             "别烦我", "不想理")

# 只在“他说想安静/拒绝”这种明确边界时使用暂停策略。
_PAUSE_CUES = ("想静静", "想安静", "让我安静", "别烦我", "不想聊", "先别问")

_EMOTION_MAP = {
    "开心": "开心", "安心": "开心", "心动": "开心", "期待": "开心",
    "低落": "低落", "委屈": "委屈", "失望": "低落", "孤独": "低落",
    "生气": "生气", "厌恶": "生气", "烦躁": "烦躁",
    "不安": "焦虑", "害怕": "焦虑", "焦虑": "焦虑",
    "疲惫": "疲惫", "困": "疲惫",
}


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, float(x)))


def _matches(text: str, cues: tuple[str, ...]) -> list[str]:
    return [c for c in cues if c and c in text]


def _occurrences(text: str, cue: str):
    start = 0
    while True:
        i = text.find(cue, start)
        if i < 0:
            return
        yield i
        start = i + 1


def _negated_at(text: str, cue: str) -> bool:
    """这个词的至少一次出现前面紧挨着“不/没”。"""
    for i in _occurrences(text, cue):
        prefix = text[max(0, i - 2):i]
        if "不" in prefix or "没" in prefix:
            return True
    return False


def _has_unnegated(text: str, cue: str) -> bool:
    for i in _occurrences(text, cue):
        prefix = text[max(0, i - 2):i]
        if "不" not in prefix and "没" not in prefix:
            return True
    return False


def _unknown_emotion(now: float, ttl_s: int, *,
                     source: str = "unknown",
                     confidence: float = 0.15,
                     evidence: Optional[list[str]] = None,
                     negated: bool = False,
                     ambiguous: bool = False,
                     note: str = "") -> dict:
    conf = _clamp(confidence)
    return {
        "label": "未知",
        "confidence": round(conf, 3),
        "uncertainty": round(1.0 - conf, 3),
        "source": source,
        "evidence": list(evidence or []),
        "negated": bool(negated),
        "ambiguous": bool(ambiguous),
        "allows_user_denial": True,
        "is_fact": False,
        "valid_from": now,
        "valid_until": now + max(1, int(ttl_s)),
        "note": note or "没有足够依据判断他的情绪，保持未知。",
    }


def _detect_user_emotion(text: str, percept: Optional[dict],
                         now: float, ttl_s: int) -> dict:
    t = (text or "").strip()
    if not t:
        return _unknown_emotion(now, ttl_s, evidence=["空输入"])

    # 1) 明确描述自己状态的词，且没有被“不/没”否定。
    hits: list[tuple[str, list[str]]] = []
    negated_hits: list[str] = []
    for label, cues in _USER_CUES:
        good = [c for c in cues if _has_unnegated(t, c)]
        if good:
            hits.append((label, good))
        for c in cues:
            if _negated_at(t, c):
                negated_hits.append(c)

    if hits:
        label, cues = hits[0]
        # 同一句里出现多个同向词，置信度略高；不因为词多就变成确定事实。
        conf = min(0.86, 0.66 + 0.06 * len(cues))
        return {
            "label": label,
            "confidence": round(conf, 3),
            "uncertainty": round(1.0 - conf, 3),
            "source": "explicit",
            "evidence": [f"原话里有「{c}」" for c in cues],
            "negated": False,
            "ambiguous": False,
            "allows_user_denial": True,
            "is_fact": False,
            "valid_from": now,
            "valid_until": now + max(1, int(ttl_s)),
            "note": "这是他直接说出的状态，但仍只是这一小段时间的假设。",
        }

    # 2) 明确否定：不反推成相反情绪。既看情绪词前的否定，也看“不累/没生气”
    # 这类没有进入正向词表的否定短语。
    negated_any = list(dict.fromkeys(negated_hits + _matches(t, _NEGATIONS)))
    if negated_any:
        return _unknown_emotion(
            now, ttl_s, source="negated", confidence=0.25,
            evidence=[f"情绪词被否定：「{c}」" for c in negated_any],
            negated=True,
            note="他说的是否定句；不能据此推断相反情绪。",
        )

    # 3) “没事/还好”是歧义，不是平静。
    amb = _matches(t, _AMBIGUOUS)
    if amb:
        return _unknown_emotion(
            now, ttl_s, source="ambiguous", confidence=0.30,
            evidence=[f"原话里有「{c}」" for c in amb],
            ambiguous=True,
            note="他说没事，但这不自动等于平静，也不自动等于压抑；保留不确定。",
        )

    # 4) 旧词表只作为弱证据，不越过“没有明确表达就未知”。
    emo = str((percept or {}).get("emotion") or "").strip()
    if emo and emo != "平静" and emo in _EMOTION_MAP:
        return {
            "label": _EMOTION_MAP[emo],
            "confidence": 0.48,
            "uncertainty": 0.52,
            "source": "lexical",
            "evidence": [f"语气词表命中「{emo}」，不是他明确说出的情绪"],
            "negated": False,
            "ambiguous": False,
            "allows_user_denial": True,
            "is_fact": False,
            "valid_from": now,
            "valid_until": now + max(1, int(ttl_s)),
            "note": "弱证据；若他否定，立即撤回这个假设。",
        }

    return _unknown_emotion(
        now, ttl_s, confidence=0.15,
        evidence=["没有明确的情绪表达"],
        note="没有足够依据判断他的情绪，保持未知。",
    )


def _self_state(state: Optional[dict], feel: Optional[dict], now: float) -> dict:
    st = state or {}
    field = feel if isinstance(feel, dict) else (
        st.get("feel") if isinstance(st.get("feel"), dict) else {})
    dom, dom_v = _affect.dominant(field)
    label = dom if dom_v >= 0.20 else "平静"
    return {
        "label": label,
        "intensity": round(_affect.intensity(field), 3),
        "valence": round(float(st.get("mood") or 0.0), 3),
        "arousal": round(float(st.get("arousal") or 0.2), 3),
        "energy": round(float(st.get("energy") or 1.0), 3),
        "lonely": round(float(st.get("lonely") or 0.0), 3),
        "feel_text": _affect.top_text(field) if field else "",
        "feel": dict(field),
        "source": "mind.snapshot",
        "is_user_emotion": False,
        "is_fact": True,
        "read_only": True,
        "as_of": now,
        "note": "这是她自己的状态，不是对用户心理的判断。",
    }


def _relationship_appraisal(text: str, state: Optional[dict],
                            now: float, ttl_s: int) -> dict:
    t = (text or "").strip()
    warm = _matches(t, _WARM_REL)
    cold = _matches(t, _COLD_REL)
    if warm and cold:
        label, conf = "有张力", 0.55
        note = "同一句里既有暖意也有冷意；只记候选，不下关系结论。"
    elif warm:
        label, conf = "偏暖", min(0.85, 0.45 + 0.12 * len(warm))
        note = "这次互动有暖意，但单次互动不改变长期关系。"
    elif cold:
        label, conf = "偏冷", min(0.85, 0.45 + 0.12 * len(cold))
        note = "这次互动有冷意，但单次互动不改变长期关系。"
    else:
        label, conf = "中性", 0.20
        note = "没有足够的关系信号；不把普通对话解释成关系变化。"
    evidence = [f"暖意词「{c}」" for c in warm] + [f"冷意词「{c}」" for c in cold]
    rel = (state or {}).get("rel") if isinstance((state or {}).get("rel"), dict) else {}
    return {
        "label": label,
        "confidence": round(_clamp(conf), 3),
        "uncertainty": round(1.0 - _clamp(conf), 3),
        "evidence": evidence,
        "evidence_count": len(evidence),
        "candidate": True,
        "single_event": True,
        "permanent_change": False,
        "changes_relationship_variables": False,
        "current": dict(rel),
        "is_fact": False,
        "valid_from": now,
        "valid_until": now + max(1, int(ttl_s)),
        "note": note,
    }


def _apply_feedback(reg: dict, feedback: Optional[dict]) -> dict:
    """上一轮反馈只调下一次的措辞，不改策略归属和权限。

    接受 / 纠正 / 拒绝 / 没回应四种结果，只在“同一种回答前策略”上生效；
    换了一种分寸就从头按当前证据判断，不把旧反馈带过去。
    """
    fb = feedback if isinstance(feedback, dict) else {}
    last = str(fb.get("last_strategy") or fb.get("previous_strategy") or "")
    action = str(fb.get("last_action") or fb.get("outcome") or "")
    if not last or action not in ("accepted", "corrected", "rejected", "no_response"):
        return reg
    if last != str(reg.get("strategy") or ""):
        return reg

    out = dict(reg)
    out["feedback_informed"] = True
    out["feedback_action"] = action
    out["feedback_count"] = int((fb.get("stats") or {}).get("count") or 0)
    strategy = str(out.get("strategy") or "")

    if strategy == "respect_ambiguous":
        if action in ("rejected", "corrected"):
            out.update(max_chars=30, allow_question=False, clarify_allowed=False,
                       directive=("他上次不想被追问/纠正过这次的说法：这次不追问，"
                                  "先按字面接住，最多一句；不再确认。"),
                       note="上次轻量澄清被拒绝/纠正：这次不追问，不重复确认。")
        elif action == "no_response":
            out.update(max_chars=30, allow_question=False, clarify_allowed=False,
                       directive=("上次轻量确认后没有明确回应：这次最多一句，"
                                  "先按字面接住，不追问。"),
                       note="上次澄清没有回应：缩到一句，不再追问。")
        else:
            out["directive"] = ("上次轻量确认被接住了：这次仍只确认一次，"
                                "最多一句，不展开。")
            out["note"] = "上次澄清被接住：保留轻量确认。"
    elif strategy == "understand":
        if action in ("corrected", "rejected"):
            out.update(max_chars=32, task_pressure="low",
                       directive=("上次的安慰被拒绝/纠正：这次只接一句，"
                                  "不追问、不建议、不替他说；把任务压力降到最低。"),
                       note="上次安慰被拒绝/纠正：只接一句，不追加解释。")
        elif action == "no_response":
            out.update(max_chars=40,
                       directive=("上次接住后没有明确回应：这次更短，只接一句，"
                                  "不追问、不展开。"),
                       note="上次接住没有回应：收短，不追问。")
        else:
            out["directive"] = ("上次这样接住是有效的：这次仍只接一句，"
                                "不追问原因，不急着给建议。")
            out["note"] = "上次接住被接受：保留理解，不加码。"
    elif strategy == "pause":
        if action == "rejected":
            out.update(max_chars=16,
                       directive=("上次退得太远被拒绝：这次只回一句，让他在，"
                                  "不追问；不要消失。"),
                       note="上次暂停被拒绝：不消失，只留一句。")
        elif action == "corrected":
            out.update(max_chars=20, allow_question=True,
                       directive=("上次暂停被纠正：这次先确认他是不是真要安静，"
                                  "最多一句，不追问原因。"),
                       note="上次暂停被纠正：先轻确认一次。")
        elif action == "no_response":
            out.update(max_chars=16,
                       directive=("上次暂停后没有明确回应：这次仍不追问，"
                                  "只留一句就停。"),
                       note="上次暂停没有回应：保持安静，不追问。")
        else:
            out["directive"] = ("上次暂停被接住了：这次仍不追问，"
                                "最多一句，允许不接。")
            out["note"] = "上次暂停被接受：保持尊重暂停。"
    elif strategy == "hold_boundary":
        if action in ("corrected", "rejected"):
            out.update(max_chars=40, allow_question=True,
                       directive=("上次冷处理被纠正/拒绝：这次不升级亲密，"
                                  "但不要把话说冷，先处理具体事。"),
                       note="上次边界被纠正/拒绝：不冷处理，先处理具体事。")
        elif action == "no_response":
            out.update(max_chars=42,
                       directive=("上次守边界后没有回应：这次不追问关系，"
                                  "只把具体事说清楚，一句就停。"),
                       note="上次边界没有回应：不追问，简短处理。")
        else:
            out["directive"] = ("上次守边界被接住了：这次仍不升级亲密，"
                                "不追问关系，先处理具体事。")
            out["note"] = "上次边界被接受：保持边界，不加码。"
    return out


def _regulation(text: str, user: dict, relationship: dict,
                feedback: Optional[dict] = None) -> dict:
    """回答前的分寸建议。`evaluate` 只给建议，`think` 才把它接到表达上。

    这一层只碰“怎么说”，不碰“想起什么”“她是谁”“关系怎么走”：
    `change_scope` 恒为 expression，检索 / 人格 / 关系变量三项都写死 False。
    """
    t = text or ""
    if _matches(t, _PAUSE_CUES):
        strategy = "pause"
        max_chars, allow_question, task_pressure = 18, False, "low"
        directive = ("他说想安静：不追问、不解释、不劝说；最多一句，"
                     "允许不接，不要换话题。")
        note = "他明确要安静/拒绝；尊重暂停，减少追问。"
    elif user.get("ambiguous"):
        strategy = "respect_ambiguous"
        max_chars, allow_question, task_pressure = 42, True, "normal"
        directive = ("他说「没事/还好」但语义不确定：不要替他解释成平静或压抑；"
                     "可以轻量确认一次（最多一句），也可以先不追问，"
                     "不要用安慰套话。")
        note = "保留不确定；可以轻量确认一次，不替他解释。"
    elif user.get("label") in ("低落", "焦虑", "委屈", "生气", "烦躁", "疲惫") \
            and float(user.get("confidence") or 0) >= 0.55:
        strategy = "understand"
        max_chars, allow_question, task_pressure = 48, False, "low"
        directive = ("他直接说了不舒服/累/烦：先接住这一句，不追问原因，"
                     "不急着给建议；把任务压力降到最低，先给最小的一步，"
                     "或问他要不要先歇。")
        note = "先接住他的状态，不追问原因，减少任务压力。"
    elif relationship.get("label") == "偏冷":
        strategy = "hold_boundary"
        max_chars, allow_question, task_pressure = 45, True, "normal"
        directive = ("他这句话偏冷：不升级亲密、不追问关系意义、不讨好；"
                     "先把具体事处理清楚。")
        note = "不升级亲密，不追问关系意义，先处理具体事。"
    else:
        strategy = "answer"
        max_chars, allow_question, task_pressure = 60, True, "normal"
        directive = ""
        note = "正常回应；这一层不额外改变表达。"

    reg = {
        "strategy": strategy,
        "applied": False,          # evaluate 只给建议；think 接线后才置 True
        "permission": "expression_only",
        "phase": "pre_answer",
        "change_scope": ["expression"] if strategy != "answer" else [],
        "changes_retrieval": False,
        "changes_personality": False,
        "changes_relationship_variables": False,
        "max_chars": max_chars,
        "allow_question": allow_question,
        "clarify_allowed": strategy == "respect_ambiguous",
        "task_pressure": task_pressure,
        "directive": directive,
        "note": note,
        "feedback_informed": False,
        "feedback_action": "",
        "feedback_count": 0,
    }
    return _apply_feedback(reg, feedback)


def evaluate(text: str, state: Optional[dict] = None,
             percept: Optional[dict] = None,
             feel: Optional[dict] = None,
             now: Optional[float] = None,
             user_ttl_s: int = USER_TTL_S,
             relationship_ttl_s: int = REL_TTL_S,
             feedback: Optional[dict] = None) -> dict:
    """只读评价一轮，返回三类对象。不写库、不改 state、不改关系变量。

    feedback 是上一轮回答前策略的结果（接受/纠正/拒绝/没回应），只用于
    这一次的措辞微调；不改变三类对象本身，也不让它变成事实。
    """
    at = time.time() if now is None else float(now)
    user = _detect_user_emotion(text, percept, at, user_ttl_s)
    self_state = _self_state(state, feel, at)
    relationship = _relationship_appraisal(text, state, at, relationship_ttl_s)
    return {
        "version": VERSION,
        "as_of": at,
        "read_only": True,
        "writes": [],
        "user_emotion": user,
        "self_state": self_state,
        "relationship_appraisal": relationship,
        "regulation": _regulation(text, user, relationship, feedback),
        "rules": {
            "user_emotion_is_hypothesis": True,
            "explicit_user_expression_first": True,
            "user_can_deny": True,
            "single_event_does_not_change_relationship": True,
            "no_personality_or_retrieval_permission": True,
        },
    }


def is_valid(obj: Optional[dict], now: Optional[float] = None) -> bool:
    """一条评价是否还在有效期内。过期的假设不能再当当前判断。"""
    if not isinstance(obj, dict):
        return False
    at = time.time() if now is None else float(now)
    try:
        return float(obj.get("valid_until") or 0.0) > at
    except (TypeError, ValueError):
        return False


def render(obj: Optional[dict]) -> str:
    """给日志/调试看的一行摘要；不进入提示词，也不替代事实。"""
    if not isinstance(obj, dict):
        return "（没有评价）"
    user = obj.get("user_emotion") or {}
    self_state = obj.get("self_state") or {}
    rel = obj.get("relationship_appraisal") or {}
    return (f"他可能「{user.get('label', '未知')}」"
            f"（{float(user.get('confidence') or 0):.2f}，{user.get('source', '')}）"
            f" · 她此刻「{self_state.get('label', '平静')}」"
            f" · 这次互动「{rel.get('label', '中性')}」（候选）")
