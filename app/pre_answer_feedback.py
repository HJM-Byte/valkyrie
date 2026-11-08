"""回答前分寸的反馈：他说了什么，下一次就该怎么调。

这一层不判断“他是不是喜欢我”，只回答一个窄问题：
上一轮的理解 / 轻量澄清 / 尊重暂停，他这次是接住了、纠正了、拒绝了，
还是根本没回应？结果只改下一次的表达分寸，不改事实、人格和关系。

分类只用明确词面线索，宁可记成“没回应”，不替他编一个态度。
"""
from __future__ import annotations

VERSION = 1
NO_RESPONSE_AFTER_S = 6 * 3600
OUTCOMES = ("accepted", "corrected", "rejected", "no_response")

ACTION_LABELS = {
    "offered": "待反馈",
    "accepted": "接住了",
    "corrected": "纠正了",
    "rejected": "拒绝了",
    "no_response": "没回应",
    "retracted": "已撤回",
}
STRATEGY_LABELS = {
    "respect_ambiguous": "轻量澄清",
    "understand": "理解",
    "pause": "尊重暂停",
    "hold_boundary": "守边界",
    "answer": "正常回答",
}

# 先判拒绝，再判纠正，最后才判接受：一句“别问了，不是这样”不能被“好”抵消。
_REJECT_CUES = (
    "别问了", "不想说", "别管我", "不用你管", "烦不烦", "别说了",
    "不想聊", "走开", "不用了", "别烦我", "别安慰", "少来",
)
_CORRECT_CUES = (
    "不是这样", "我没难过", "我没生气", "我没难受", "我没烦",
    "别替我", "你误会", "我没让你", "不需要你", "用不着",
    "别乱猜", "不是那个意思", "我没那个意思",
)
_ACCEPT_CUES = (
    "谢谢", "嗯", "好", "行", "知道了", "听你的", "你说得对",
    "是的", "对", "我在", "继续", "然后呢", "你说",
)

_REJECT_REASON = "他明确表示不想被追问/安慰/管。"
_CORRECT_REASON = "他纠正了刚才的理解或说法。"
_ACCEPT_REASON = "他接住了这次的分寸。"
_NO_RESPONSE_REASON = "这句话没有明确接受、纠正或拒绝这次分寸。"


def _hits(text: str, cues: tuple[str, ...]) -> list[str]:
    return [c for c in cues if c and c in text]


def classify_response(text: str, previous_strategy: str = "",
                      elapsed_s: float = 0.0) -> dict:
    """把下一句话归成四种反馈之一；拿不准就落到 no_response。

    `previous_strategy` 只用于说明来源，不用于把“嗯”解释成别的意思。
    """
    t = (text or "").strip()
    strategy = str(previous_strategy or "")
    if not t:
        return {"action": "no_response", "confidence": 0.2,
                "evidence": ["空输入"], "reason": _NO_RESPONSE_REASON,
                "previous_strategy": strategy, "elapsed_s": round(float(elapsed_s), 1)}

    rejected = _hits(t, _REJECT_CUES)
    if rejected:
        return {"action": "rejected", "confidence": 0.82,
                "evidence": [f"拒绝词「{c}」" for c in rejected],
                "reason": _REJECT_REASON, "previous_strategy": strategy,
                "elapsed_s": round(float(elapsed_s), 1)}

    corrected = _hits(t, _CORRECT_CUES)
    if corrected:
        return {"action": "corrected", "confidence": 0.78,
                "evidence": [f"纠正词「{c}」" for c in corrected],
                "reason": _CORRECT_REASON, "previous_strategy": strategy,
                "elapsed_s": round(float(elapsed_s), 1)}

    accepted = _hits(t, _ACCEPT_CUES)
    if accepted:
        return {"action": "accepted", "confidence": 0.66,
                "evidence": [f"接受词「{c}」" for c in accepted],
                "reason": _ACCEPT_REASON, "previous_strategy": strategy,
                "elapsed_s": round(float(elapsed_s), 1)}

    note = _NO_RESPONSE_REASON
    if float(elapsed_s or 0.0) >= NO_RESPONSE_AFTER_S:
        note = "隔了六小时以上才回，且没有明确接住/纠正/拒绝这次分寸；按没回应记。"
    return {"action": "no_response", "confidence": 0.35,
            "evidence": ["没有明确的接受、纠正或拒绝线索"],
            "reason": note, "previous_strategy": strategy,
            "elapsed_s": round(float(elapsed_s), 1)}


def action_label(action: str) -> str:
    return ACTION_LABELS.get(str(action or ""), str(action or ""))


def strategy_label(strategy: str) -> str:
    return STRATEGY_LABELS.get(str(strategy or ""), str(strategy or ""))


def describe_row(row: dict) -> dict:
    """把一条 feedback_events 行转成页面能读的反馈记录。

    `reason` 是分类原因（新记录会带证据），`source_text` 是触发判断的下一句原话。
    只有已经结算的四类才可撤回；offered 还没判，retracted 不能二次撤回。
    """
    action = str((row or {}).get("action") or "")
    strategy = str((row or {}).get("target_id") or "")
    return {
        "id": str(row.get("id") or ""),
        "strategy": strategy,
        "strategy_label": strategy_label(strategy),
        "target_id": strategy,
        "action": action,
        "action_label": action_label(action),
        "reason": str(row.get("reason") or ""),
        "source_text": str(row.get("source_text") or ""),
        "target_text": str(row.get("target_text") or ""),
        "source_kind": str(row.get("source_kind") or ""),
        "source_ref": str(row.get("source_ref") or ""),
        "turn_id": int(row.get("turn_id") or 0),
        "recorded_at": float(row.get("recorded_at") or 0.0),
        "can_retract": action in OUTCOMES,
        "retracted": action == "retracted",
    }


def summarize(rows: list[dict]) -> dict:
    """把同一策略的历史反馈压成计数 + 最近一次；只做统计，不推断态度。"""
    counts = {k: 0 for k in OUTCOMES}
    for r in rows or []:
        a = str((r or {}).get("action") or "")
        if a in counts:
            counts[a] += 1
    latest = (rows or [{}])[0] if rows else {}
    return {
        "count": sum(counts.values()),
        **counts,
        "last_action": str(latest.get("action") or ""),
        "last_strategy": str(latest.get("target_id") or ""),
        "last_text": str(latest.get("source_text") or ""),
        "last_reason": str(latest.get("reason") or ""),
    }


def feedback_line(feedback: dict | None) -> str:
    """给 trace / 调试看的一行；不进入人格提示。"""
    fb = feedback or {}
    action = str(fb.get("last_action") or fb.get("outcome") or "none")
    strategy = str(fb.get("last_strategy") or "")
    if not strategy:
        return "反馈    上一轮没有可用的回答前策略反馈"
    return f"反馈    上次 {strategy} → {action}（只调下一次表达）"
