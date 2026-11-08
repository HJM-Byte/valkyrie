"""意义层：只表达“这段经历对我们意味着什么”，不新增事实。

边界：
- 意义候选必须能回到真实事件或真实反馈，不能凭空生成；
- 共同章节至少引用两条真实事件，章节里的意义是派生理解，不是事实；
- 用户否定后进入 disputed；反驳、关闭、撤回都不修改原事件原文；
- 这里只做纯数据整理，不写数据库，也不把候选自动升成事实。
"""
from __future__ import annotations

import json
from typing import Iterable, Optional

from . import pre_answer_feedback as _paf

MEANING_KINDS = ("episode_meaning", "repair", "chapter_meaning")
MEANING_STATUSES = ("candidate", "confirmed", "disputed", "retracted")
CHAPTER_STATUSES = ("open", "closed", "disputed", "retracted")
REPAIR_WINDOW_S = 7 * 86400.0


def canonical_ids(ids: Optional[Iterable]) -> list[int]:
    """只留正整数，去重并按升序排；签名和来源都靠它稳定。"""
    out: set[int] = set()
    for raw in ids or ():
        try:
            n = int(raw)
        except (TypeError, ValueError):
            continue
        if n > 0:
            out.add(n)
    return sorted(out)


def ids_json(ids: Optional[Iterable]) -> str:
    return json.dumps(canonical_ids(ids), separators=(",", ":"))


def feedback_ids_json(ids: Optional[Iterable]) -> str:
    return json.dumps(sorted({str(x) for x in (ids or ()) if str(x).strip()}),
                      separators=(",", ":"))


def parse_ids(raw: str) -> list[int]:
    try:
        data = json.loads(raw or "[]")
    except Exception:
        return []
    return canonical_ids(data if isinstance(data, list) else [])


def parse_feedback_ids(raw: str) -> list[str]:
    try:
        data = json.loads(raw or "[]")
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    return sorted({str(x) for x in data if str(x).strip()})


def signature(kind: str, episode_ids: Optional[Iterable] = None,
              feedback_ids: Optional[Iterable] = None) -> str:
    """同一组来源只允许一条候选；重复派生不制造第二份意义。"""
    eps = ",".join(str(x) for x in canonical_ids(episode_ids))
    fbs = ",".join(sorted(str(x) for x in (feedback_ids or ()) if x))
    return f"{kind}|e:{eps}|f:{fbs}"


def legacy_for(episodes: list[dict]) -> int:
    """来源不完整时保持 legacy_unknown=1；有来源不等于来源被确认。"""
    if not episodes:
        return 1
    for e in episodes:
        if not str(e.get("source") or "").strip():
            return 1
        if str(e.get("status") or "active") != "active":
            return 1
    return 0


def repair_candidates(rows: list[dict]) -> list[dict]:
    """从已结算的回答前反馈里找“被纠正/拒绝 → 后来接受”的窄模式。

    只生成 candidate，不生成 confirmed；一次接受不足以证明策略稳定。
    没有明确的前后两条记录就不猜。
    """
    by_strategy: dict[str, list[dict]] = {}
    for r in rows or []:
        action = str(r.get("action") or "")
        if action not in ("accepted", "corrected", "rejected"):
            continue
        strategy = str(r.get("target_id") or "").strip()
        if not strategy:
            continue
        by_strategy.setdefault(strategy, []).append(dict(r))

    out: list[dict] = []
    for strategy, items in by_strategy.items():
        items.sort(key=lambda x: (float(x.get("recorded_at") or 0), str(x.get("id") or "")))
        for i, first in enumerate(items):
            if str(first.get("action")) not in ("corrected", "rejected"):
                continue
            t0 = float(first.get("recorded_at") or 0.0)
            later = next(
                (x for x in items[i + 1:]
                 if str(x.get("action")) == "accepted"
                 and 0.0 <= float(x.get("recorded_at") or 0.0) - t0 <= REPAIR_WINDOW_S),
                None,
            )
            if later is None:
                continue
            fids = [str(first.get("id") or ""), str(later.get("id") or "")]
            if not all(fids):
                continue
            label = _paf.strategy_label(strategy)
            out.append({
                "kind": "repair",
                "title": f"{label}：被纠正后的一次修复",
                "claim": (
                    f"上次用「{label}」时，他{_paf.action_label(str(first.get('action')))}；"
                    f"后来他接受了同一类分寸。下一次相似场景先按当前证据确认，"
                    "不把这一次接受当成稳定结论。"
                ),
                "source_episode_ids": [],
                "source_feedback_ids": fids,
                "confidence": 0.42,
            })
    return out
