"""记忆强化：被想起一次，强一点；但这点强度会随时间退。

P4 的边界：`used` 只留“被想起过几次”的计数，不再当永久加分。
真正影响召回的是这里的 b_eff：

    a      = 记忆年龄（天）
    b_eff  = b * exp(-(t - 上次强化时间) / τ_b)
    Δb     = B0 * exp(-a / τ_a) * (1 - b_eff / Bmax)
    b_new  = min(Bmax, b_eff + Δb)

两条衰减缺一不可：
- `τ_a` 管“多老的记忆还值得强化”：五年前的事再想起，只动一点点；
- `τ_b` 管“上次强化留下的影响多久退”：两周后，同一次强化只剩约 0.37。

这里只算数，不碰数据库。`Memory` 负责在真实 turn 里结算；
只读预览、打开识海、写日记都不调用它。
"""
from __future__ import annotations

import math
from typing import Optional

from . import config as C

B0 = float(getattr(C, "MEMORY_STRENGTH_B0", 0.18))
BMAX = float(getattr(C, "MEMORY_STRENGTH_BMAX", 1.0))
TAU_AGE_DAYS = float(getattr(C, "MEMORY_STRENGTH_TAU_AGE", 30.0))
TAU_SINCE_DAYS = float(getattr(C, "MEMORY_STRENGTH_TAU_SINCE", 14.0))
LEGACY_CAP = float(getattr(C, "MEMORY_STRENGTH_LEGACY_CAP", 0.35))


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def age_days(ts: float, now: float) -> float:
    return max(0.0, (float(now) - float(ts)) / 86400.0)


def legacy_initial(used: int) -> float:
    """旧库没有强化旁表时，只按旧 used 给一个很低的起点。

    这是兼容，不是迁移：旧记录不批量回填、不改原文，最多 0.35。
    旧 used 再高，也不能靠历史次数压过新记忆。
    """
    return min(LEGACY_CAP, 0.04 * min(max(0, int(used or 0)), 8))


def effective_strength(strength: float, last_reinforced: float,
                       now: float) -> float:
    """上次强化留下的强度，到此刻还剩多少。

    last_reinforced=0 表示还没被真实召回强化过：要么是新记忆（强度 0），
    要么是旧库兼容值（按旧 used 给的起点），不做二次衰减。
    """
    s = _clamp01(strength)
    last = float(last_reinforced or 0.0)
    if s <= 0.0 or last <= 0.0:
        return s
    elapsed = max(0.0, float(now) - last) / 86400.0
    return _clamp01(s * math.exp(-elapsed / max(1e-6, TAU_SINCE_DAYS)))


def reinforcement_delta(ts: float, strength_eff: float, now: float) -> float:
    """这一次召回能加多少：记忆越老越少，已经越强也越少。"""
    a = age_days(ts, now)
    room = max(0.0, 1.0 - _clamp01(strength_eff) / max(1e-6, BMAX))
    return B0 * math.exp(-a / max(1e-6, TAU_AGE_DAYS)) * room


def next_strength(ts: float, strength: float, last_reinforced: float,
                  now: float) -> dict:
    """结算一次真实召回，返回可落库的强度与解释字段。"""
    before = effective_strength(strength, last_reinforced, now)
    delta = reinforcement_delta(ts, before, now)
    after = _clamp01(before + delta)
    return {
        "before": round(before, 6),
        "delta": round(delta, 6),
        "after": round(after, 6),
        "age_days": round(age_days(ts, now), 3),
    }


def explain(strength: float, last_reinforced: float, now: float) -> dict:
    """给识海卡片/调试看的只读解释，不结算。"""
    eff = effective_strength(strength, last_reinforced, now)
    return {"strength": round(eff, 3),
            "last_reinforced": float(last_reinforced or 0.0)}


def is_legacy(row: Optional[dict]) -> bool:
    return bool(row and int(row.get("legacy_unknown") or 0))
