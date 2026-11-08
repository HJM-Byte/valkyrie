"""好奇心分流：先判断「这是什么问题」，再决定要不要上网。

旧链路把答不上来的问句直接塞进 `learning.note_curiosity()`，于是
「你为什么不回我」「现在房间里几个人」也会变成研究任务。真正该查的
是可验证的事实问题；关系、现场、自我、动作四类都不该拿网络资料顶替。

这里是纯函数，不写库、不联网。路由固定六类：

    fact_research            可验证事实，允许进研究队列
    relationship_clarification 关系/感受，转为对话澄清，禁止上网
    scene_observation        当前场景/感知，禁止用网络替代现场
    self_exploration         关于她自己的身份/能力/记忆，禁止默认搜索
    action_candidate         提醒/打开/发送/执行，进入权限判断
    unknown                  歧义，保留原因，不默认搜索

规则先用词面线索和句式，不引入小模型；宁可 `unknown` 多，也不强行分类。
"""
from __future__ import annotations

import re

ROUTES = (
    "fact_research",
    "relationship_clarification",
    "scene_observation",
    "self_exploration",
    "action_candidate",
    "unknown",
)

# 动作：明确要求她做事。注意「帮我查/搜/找」不算动作，那是研究。
_ACTION_CUES = (
    "打开", "关掉", "关闭", "启动", "发送", "发给", "提醒我", "提醒一下",
    "定个闹钟", "设个闹钟", "定个提醒", "设个提醒", "设个", "设置",
    "播放", "暂停", "取消", "打电话", "发消息", "发微信", "记一下",
    "帮我拿", "帮我开", "帮我关", "帮我定", "帮我设", "帮我放",
    "把灯", "把空调", "把音乐", "导航到", "订一张", "买一张",
)

# 关系：第二人称 + 关系/情绪词。不能只靠「你为什么不」，否则
# 「你为什么天是蓝的」也会被误判。
_RELATION_CUES = (
    "不回我", "不理我", "不理你", "不在乎我", "不喜欢我", "讨厌我",
    "爱我吗", "想我吗", "生我气", "你还理我", "你还要我", "离开我",
    "不要我", "冷落", "疏远", "我们之间", "我们的关系", "你是不是不",
    "你为什么这样", "你怎么这样", "你变了", "你都不",
)

# 现场：问的是此刻眼前/耳边/房间里的事，答案只能来自感知。
_SCENE_CUES = (
    "你看见", "你看到", "你听见", "你听到", "我手里", "我手上", "桌上",
    "房间里", "周围", "眼前", "现场", "摄像头", "画面里", "屏幕上是",
    "现在几个人", "现在几点", "外面下雨", "外面天气", "我穿", "我拿",
)

# 自我：身份、能力、记忆、身体。这些不是网上能查的。
_SELF_CUES = (
    "你是谁", "你叫什么", "你的名字", "你是什么", "你从哪", "你几岁",
    "你多大", "你的身体", "你的记忆", "你记得", "你还记得", "你忘了",
    "你能看见", "你能听见", "你会不会", "你喜欢什么", "你想要什么",
    "你有意识", "你是真人", "你是机器人", "你是什么做的",
)

# 事实：可验证的知识问题。只要命中一个知识疑问线索就算，
# 但必须真的在问，而不是陈述。
_FACT_CUES = (
    "是什么", "为什么", "怎么会", "怎么", "如何", "原理", "区别", "定义",
    "多少", "多大", "多远", "多久", "哪年", "哪一年", "哪个", "哪里",
    "谁", "科学家", "历史", "宇宙", "物理", "化学", "生物", "数学",
    "量子", "光速", "地球", "太阳", "温度", "电压", "电流", "代码",
    "算法", "模型", "记忆", "情绪", "睡眠", "身体", "社会", "经济",
)

_QUESTION_MARK = re.compile(r"[?？]|吗|呢|么")


def _hit(text: str, cues: tuple[str, ...]) -> list[str]:
    return [c for c in cues if c in text]


def _result(route: str, reason: str, confidence: float,
            hits: list[str] | None = None) -> dict:
    return {
        "route": route,
        "reason": reason,
        "confidence": round(max(0.0, min(1.0, confidence)), 3),
        "hits": list(hits or []),
        "may_create_research_task": route == "fact_research",
        "requires_permission": route == "action_candidate",
    }


def classify(text: str) -> dict:
    """把一句话分到六类之一。纯函数，不读写任何状态。"""
    t = " ".join(str(text or "").split())
    if len(t) < 2:
        return _result("unknown", "内容太短，无法判断", 0.2)

    # 动作优先：先看她是不是被要求做事；但「帮我查/搜/找」是研究。
    action_hits = _hit(t, _ACTION_CUES)
    if action_hits and not any(k in t for k in ("查", "搜", "找", "研究", "解释")):
        return _result("action_candidate",
                       "含执行动作：" + "、".join(action_hits[:3]),
                       0.75 + 0.05 * min(3, len(action_hits)), action_hits)

    rel_hits = _hit(t, _RELATION_CUES)
    if rel_hits:
        return _result("relationship_clarification",
                       "问的是关系/感受：" + "、".join(rel_hits[:3]),
                       0.80 + 0.04 * min(3, len(rel_hits)), rel_hits)

    scene_hits = _hit(t, _SCENE_CUES)
    if scene_hits:
        return _result("scene_observation",
                       "问的是当前现场：" + "、".join(scene_hits[:3]),
                       0.72 + 0.05 * min(3, len(scene_hits)), scene_hits)

    self_hits = _hit(t, _SELF_CUES)
    if self_hits:
        return _result("self_exploration",
                       "问的是她自己：" + "、".join(self_hits[:3]),
                       0.78 + 0.04 * min(3, len(self_hits)), self_hits)

    fact_hits = _hit(t, _FACT_CUES)
    # 「那个东西呢」「然后呢」这类没有知识线索的短句，不强判事实。
    if fact_hits and (len(t) >= 6 or _QUESTION_MARK.search(t)):
        return _result("fact_research",
                       "可验证的知识问题：" + "、".join(fact_hits[:3]),
                       0.58 + 0.06 * min(4, len(fact_hits)), fact_hits)

    return _result("unknown", "没有足够线索，保留原样，不默认搜索", 0.35)


def may_research(text: str) -> bool:
    return classify(text)["may_create_research_task"]


def dry_run(rows: list[dict]) -> dict:
    """旧好奇只做只读分流：不改状态、不触发搜索。"""
    items = []
    counts = {r: 0 for r in ROUTES}
    for row in rows or []:
        q = str(row.get("question") or "")
        c = classify(q)
        counts[c["route"]] += 1
        items.append({
            "id": row.get("id"),
            "question": q,
            "route": c["route"],
            "reason": c["reason"],
            "confidence": c["confidence"],
            "may_create_research_task": c["may_create_research_task"],
            "needs_manual_review": c["route"] == "unknown",
        })
    return {"total": len(items), "counts": counts, "items": items,
            "dry_run": True}
