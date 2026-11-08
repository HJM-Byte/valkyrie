"""P4-C：人格四层里的“可变化三层”与版本链。

这里只做纯判断，不写数据库：
- “身份与硬边界”不在本模块的可写范围里，它只从 PERSONA.md 读取；
  它只是名字、身体事实和事实/隐私边界，不是预设人格；
- 稳定倾向、互动偏好、自我叙事都要有真实意义/章节作证据；
- 证据门槛按层不同，单次事件不能直接改人格；
- 版本先候选，用户确认才 active，回滚是新建一个指向旧版本的版本，
  不把旧版本从历史里抹掉。

数据库读写与提示注入仍在 memory.py / persona.py。这样测试可以单独
验证“证据不够就不让写”，不必先搭一个模型。
"""
from __future__ import annotations

import re
import time
from typing import Iterable, Optional

LAYERS = {
    "core": "身份与硬边界",
    "tendency": "稳定倾向",
    "interaction": "互动偏好",
    "narrative": "自我叙事",
}
AUTO_LAYERS = ("tendency", "interaction", "narrative")
STATUSES = ("candidate", "active", "superseded", "rejected", "disputed", "retracted")
FIELD_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")


def layer_label(layer: str) -> str:
    return LAYERS.get(str(layer or ""), str(layer or ""))


def normalize_field(field: str) -> str:
    f = str(field or "").strip().lower()
    if not FIELD_RE.match(f):
        raise ValueError("字段名只能是小写字母、数字和下划线，最多 40 字")
    return f


def normalize_value(value: str) -> str:
    v = str(value or "").strip()
    if not v:
        raise ValueError("人格版本不能为空")
    if len(v) > 300:
        raise ValueError("人格版本最多 300 字，写短一点")
    return v


def evidence_from(meanings: Iterable[dict], chapters: Iterable[dict]) -> dict:
    """把已确认的来源压成可核对的证据摘要。

    只做汇总，不判断真假；调用方传入的应该已经是 confirmed/usable 的记录。
    """
    ms = [dict(x) for x in (meanings or []) if isinstance(x, dict)]
    cs = [dict(x) for x in (chapters or []) if isinstance(x, dict)]
    episode_ids: set[int] = set()
    days: set[str] = set()
    for m in ms:
        for eid in m.get("source_episode_ids") or []:
            try:
                if int(eid) > 0:
                    episode_ids.add(int(eid))
            except (TypeError, ValueError):
                continue
        for s in m.get("sources") or []:
            if s.get("day"):
                days.add(str(s["day"]))
    for c in cs:
        for eid in c.get("event_ids") or []:
            try:
                if int(eid) > 0:
                    episode_ids.add(int(eid))
            except (TypeError, ValueError):
                continue
        for s in c.get("sources") or []:
            if s.get("day"):
                days.add(str(s["day"]))
    legacy = any(int(x.get("legacy_unknown") or 0) for x in ms + cs)
    return {
        "meaning_ids": sorted(str(x.get("id") or "") for x in ms if x.get("id")),
        "chapter_ids": sorted(str(x.get("id") or "") for x in cs if x.get("id")),
        "episode_ids": sorted(episode_ids),
        "days": sorted(days),
        "meaning_count": len(ms),
        "chapter_count": len(cs),
        "episode_count": len(episode_ids),
        "day_count": len(days),
        "legacy_unknown": 1 if legacy else 0,
    }


def evidence_gate(layer: str, ev: dict) -> tuple[bool, str]:
    """按层判断证据够不够；返回 (是否够, 不够的原因码)。"""
    layer = str(layer or "")
    if layer not in AUTO_LAYERS:
        return False, "core_locked"
    if int(ev.get("legacy_unknown") or 0):
        return False, "legacy_unknown"
    # 先报这一层最具体的原因，再报通用的“事件不够”；
    # 否则只有一条意义时会被笼统地说成 need_two_events。
    if layer == "tendency":
        # 稳定倾向：至少两条独立意义，且跨日。不是“同一天说了两遍”。
        if int(ev.get("meaning_count") or 0) < 2:
            return False, "need_two_meanings"
        if int(ev.get("day_count") or 0) < 2:
            return False, "need_cross_day"
    elif layer == "interaction":
        # 互动偏好：两条意义，或一段已确认章节 + 一条意义。
        if not (int(ev.get("meaning_count") or 0) >= 2
                or (int(ev.get("chapter_count") or 0) >= 1
                    and int(ev.get("meaning_count") or 0) >= 1)):
            return False, "need_two_sources"
    elif layer == "narrative":
        # 自我叙事：至少一段已确认共同章节，加一条已确认意义。
        if int(ev.get("chapter_count") or 0) < 1:
            return False, "need_chapter"
        if int(ev.get("meaning_count") or 0) < 1:
            return False, "need_meaning"
    else:
        return False, "bad_layer"
    if int(ev.get("episode_count") or 0) < 2:
        return False, "need_two_events"
    return True, ""


def regression(layer: str, field: str, value: str, ev: dict) -> dict:
    """激活前的提示级回归，不是跨模型行为回归。

    它只证明：核心没被改、证据还在、值没超长、字段合法、版本没有指向
    未确认来源。真正的跨模型回归仍要另跑同一组场景；报告里必须这样写。
    """
    checks = {
        "core_unchanged": str(layer or "") != "core",
        "field_valid": bool(FIELD_RE.match(str(field or ""))),
        "value_nonempty": bool(str(value or "").strip()),
        "value_short_enough": len(str(value or "")) <= 300,
        "evidence_present": int(ev.get("episode_count") or 0) >= 2,
        "evidence_not_legacy": not int(ev.get("legacy_unknown") or 0),
    }
    return {"passed": all(checks.values()), "checks": checks}


def diff(previous: str, value: str) -> dict:
    old = str(previous or "")
    new = str(value or "")
    return {"previous": old, "value": new, "changed": old != new}


def source_episode_ids(ev: dict) -> list[int]:
    return [int(x) for x in ev.get("episode_ids") or [] if int(x) > 0]


def now() -> float:
    return time.time()


def evidence_text(ev: dict) -> str:
    """给人和日志读的一句证据，不参与模型判断。"""
    return (f"{int(ev.get('meaning_count') or 0)} 条意义 / "
            f"{int(ev.get('chapter_count') or 0)} 段章节 / "
            f"{int(ev.get('episode_count') or 0)} 条事件 / "
            f"{int(ev.get('day_count') or 0)} 天")
