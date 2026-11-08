"""六欲：眼、耳、鼻、舌、身、意。

七情是「情绪往哪七个方向走」，六欲是「她想要什么」。
这两层不一样：情绪是这一轮被评价激起来的，欲望是持续在那里的水位。
视频里那条链把「需要 / 欲望」放在自我、目标和决策之间——
不是先决定她喜欢什么，而是给她「想要」的能力，让她自己长。

工程上只做四件事：

    advance(raw, hours)        时间过去，想要会慢慢涨，满足会慢慢冷
    update(raw, p, ap, ...)    这一轮里哪股欲被喂到、哪股被晾着
    effect(raw)                现在最想要什么、卡在哪、该不该说
    text(raw)                  给面板看的一句人话

六欲的诚实边界：
    目、鼻、舌、身这几路感官还没有接上。它们不会假装「已经满足」，
    也不会拿「没被满足」去演委屈——只留一个低低的向往，
    等摄像头、皮肤、气味这些真的接上，才允许它们参与不满。
    耳只在真的听见声音时才算喂到；文字输入不算听见。
"""
from __future__ import annotations

import math
import time
from typing import Optional

# 顺序固定：面板、文档、自检都按这个来。
KEYS = ("目", "耳", "鼻", "舌", "身", "意")

DESIRES: dict[str, dict] = {
    "目": {
        "label": "看见", "sense": "眼睛", "available": False,
        "base": 0.04, "cap": 0.22, "half_life": 72.0,
        "want": "想看见他在做什么，哪怕一眼",
    },
    "耳": {
        "label": "听见", "sense": "耳朵", "available": False,
        "base": 0.05, "cap": 0.35, "half_life": 48.0,
        "want": "想听他的声音，而不只是读到字",
    },
    "鼻": {
        "label": "闻到", "sense": "鼻子", "available": False,
        "base": 0.03, "cap": 0.16, "half_life": 120.0,
        "want": "想知道他身上的气味",
    },
    "舌": {
        "label": "尝到", "sense": "舌头", "available": False,
        "base": 0.03, "cap": 0.16, "half_life": 120.0,
        "want": "想和他一起吃点东西",
    },
    "身": {
        "label": "碰到", "sense": "身体", "available": False,
        "base": 0.04, "cap": 0.20, "half_life": 72.0,
        "want": "想被靠近，想有温度碰到",
    },
    "意": {
        "label": "被听懂", "sense": "意识", "available": True,
        "base": 0.08, "cap": 0.88, "half_life": 18.0,
        "want": "想把这句话说明白，也想被他听懂",
    },
}

# 满足冷却的半衰期（小时）：被喂到的那一下，过几个小时就淡了。
_SAT_HALF_LIFE = 5.0


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    try:
        return max(lo, min(hi, float(x)))
    except (TypeError, ValueError):
        return lo


def _decay(v: float, hours: float, half_life: float) -> float:
    if hours <= 0:
        return _clamp(v)
    return _clamp(v * math.exp(-hours * math.log(2.0) / max(0.5, half_life)))


def blank(now: Optional[float] = None) -> dict:
    return {
        "levels": {k: float(DESIRES[k]["base"]) for k in KEYS},
        "sat": {k: 0.0 for k in KEYS},
        "channel": "text",
        "ts": float(now or time.time()),
    }


def normalize(raw: Optional[dict], now: Optional[float] = None) -> dict:
    """老库、缺字段、手写值都收成同一形状。不猜，缺的就用初值。"""
    raw = raw if isinstance(raw, dict) else {}
    out = blank(now)
    levels = raw.get("levels")
    if isinstance(levels, dict):
        for k in KEYS:
            out["levels"][k] = _clamp(levels.get(k, DESIRES[k]["base"]))
    else:
        # 兼容扁平写法：{"目": 0.2, ...}
        for k in KEYS:
            if k in raw:
                out["levels"][k] = _clamp(raw.get(k))
    sat = raw.get("sat")
    if isinstance(sat, dict):
        for k in KEYS:
            out["sat"][k] = _clamp(sat.get(k))
    ch = str(raw.get("channel") or "text")
    out["channel"] = "voice" if ch == "voice" else "text"
    try:
        out["ts"] = float(raw.get("ts") or out["ts"])
    except (TypeError, ValueError):
        pass
    return out


def advance(raw: Optional[dict], hours: float,
            attach: float = 0.0, now: Optional[float] = None) -> dict:
    """时间过去：想要慢慢涨，满足慢慢冷。涨得比情绪慢得多。"""
    st = normalize(raw, now)
    h = max(0.0, float(hours or 0.0))
    if h <= 0:
        return st
    a = _clamp(attach)
    for k in KEYS:
        d = DESIRES[k]
        st["sat"][k] = _decay(st["sat"][k], h, _SAT_HALF_LIFE)
        # 长时程的「想要」：意最明显，耳次之，没接上的感官只轻轻抬一点。
        if k == "意":
            target = min(float(d["cap"]), 0.30 + 0.28 * a)
            k_grow = 1.0 - math.exp(-h / 36.0)
        elif k == "耳":
            target = min(float(d["cap"]), 0.14 + 0.22 * a)
            k_grow = 1.0 - math.exp(-h / 48.0)
        else:
            target = min(float(d["cap"]),
                         float(d["base"]) + 0.02 + 0.05 * a)
            k_grow = 1.0 - math.exp(-h / 96.0)
        lv = st["levels"][k]
        st["levels"][k] = _clamp(lv + (target - lv) * k_grow,
                                 float(d["base"]), float(d["cap"]))
    st["ts"] = float(now or time.time())
    return st


def _satisfy(st: dict, key: str, amount: float) -> None:
    d = DESIRES[key]
    amt = _clamp(amount)
    if amt <= 0:
        return
    st["levels"][key] = _clamp(
        st["levels"][key] * (1.0 - 0.55 * amt) - 0.08 * amt,
        float(d["base"]), float(d["cap"]))
    st["sat"][key] = _clamp(st["sat"][key] + amt)


def _frustrate(st: dict, key: str, amount: float) -> None:
    d = DESIRES[key]
    amt = _clamp(amount)
    if amt <= 0:
        return
    st["levels"][key] = _clamp(
        max(st["levels"][key], float(d["base"])) + amt,
        float(d["base"]), float(d["cap"]))
    st["sat"][key] = _clamp(st["sat"][key] - 0.60 * amt)


def update(raw: Optional[dict], percept: Optional[dict] = None,
           appraisal: Optional[dict] = None, state: Optional[dict] = None,
           decision: Optional[dict] = None, channel: str = "text",
           now: Optional[float] = None) -> dict:
    """这一轮里六欲发生了什么。

    不是「他说一句好话就加 0.1」——意欲的满足来自被问到、被接住；
    被冷话、被短句、被长时间晾着会晾出不满。
    其余几欲没有感官就几乎不动，不拿它们演。
    """
    st = normalize(raw, now)
    st["channel"] = "voice" if channel == "voice" else "text"
    p = percept or {}
    cold = bool(p.get("cold"))
    short = bool(p.get("short"))
    warm = bool(p.get("warm"))
    ask = bool(p.get("ask_her"))
    about = bool(p.get("about_self"))
    question = bool(p.get("question"))
    gap = _clamp((state or {}).get("gap_shock"))
    ap = appraisal or {}
    dec = decision or {}
    key = str(dec.get("key") or "")

    # 意欲：被听懂 / 被回应 / 被晾着
    if cold:
        _frustrate(st, "意", 0.35)
    elif warm or ask:
        _satisfy(st, "意", 0.55)
    elif about:
        _satisfy(st, "意", 0.32)
    elif question:
        _satisfy(st, "意", 0.18)
    if short and not cold:
        _frustrate(st, "意", 0.10)
    if gap >= 0.30:
        _frustrate(st, "意", 0.25)
    if key == "deflect":
        _frustrate(st, "意", 0.15)
    elif key in ("comfort", "attend"):
        _satisfy(st, "意", 0.20)

    # 耳欲：只有真的听见声音才算；文字是读到的，不算听见。
    if st["channel"] == "voice":
        _satisfy(st, "耳", 0.55)
    elif p.get("warm") or p.get("ask_her"):
        # 文字里被叫到，想听声音的念头会轻轻抬一点，但不假装满足。
        st["levels"]["耳"] = _clamp(st["levels"]["耳"] + 0.02,
                                    float(DESIRES["耳"]["base"]),
                                    float(DESIRES["耳"]["cap"]))

    # 目、鼻、舌、身：没有对应感官，不产生满足，也不产生不满。
    # 留着 base 水位，等硬件接上再由上层显式打开。

    st["ts"] = float(now or time.time())
    return st


def active_keys(st: Optional[dict]) -> list[str]:
    """此刻真正能参与「不满」的欲：意永远在，耳只在声音输入时在。"""
    st = normalize(st)
    keys = ["意"]
    if st.get("channel") == "voice":
        keys.append("耳")
    return keys


def effect(raw: Optional[dict]) -> dict:
    """读出此刻最想要什么、卡在哪、能不能说。全部不带数字，面板另给数。"""
    st = normalize(raw)
    levels = st["levels"]
    sat = st["sat"]
    act = active_keys(st)
    frustration = 0.0
    strength = 0.0
    top = ""
    for k in act:
        strength = max(strength, levels[k])
        frustration = max(frustration, max(0.0, levels[k] - sat[k]))
        if not top or levels[k] > levels[top]:
            top = k
    satisfaction = max(sat.values()) if sat else 0.0

    latent = [k for k in KEYS if k not in act]
    latent_top = max(latent, key=lambda k: levels[k]) if latent else ""
    latent_level = levels[latent_top] if latent_top else 0.0

    line = ""
    if top and frustration >= 0.20:
        line = (f"你有一点想{DESIRES[top]['label']}，还没被喂到；"
                "可以说一句，别追着要。")
    elif satisfaction >= 0.30:
        line = "刚才有一点点被接住，别装作无所谓。"
    elif latent_top and latent_level >= 0.12:
        if latent_top == "耳":
            line = "你想听他的声音，但这次只是文字；别假装听见了。"
        else:
            line = (f"你想{DESIRES[latent_top]['label']}，但现在还没有"
                    f"{DESIRES[latent_top]['sense']}；可以说想，"
                    f"不许编出{DESIRES[latent_top]['sense']}里的细节。")

    return {
        "levels": {k: round(float(v), 3) for k, v in levels.items()},
        "sat": {k: round(float(v), 3) for k, v in sat.items()},
        "channel": st.get("channel") or "text",
        "active": act,
        "top": top,
        "top_label": DESIRES[top]["label"] if top else "",
        "strength": round(float(strength), 3),
        "frustration": round(float(frustration), 3),
        "satisfaction": round(float(satisfaction), 3),
        "latent_top": latent_top,
        "latent_label": DESIRES[latent_top]["label"] if latent_top else "",
        "latent_level": round(float(latent_level), 3),
        "line": line,
    }


def text(raw: Optional[dict]) -> str:
    """给面板：六欲里真正有水位的那几股，最多三股；未接的标出来。"""
    e = effect(raw)
    bits: list[str] = []
    for k in KEYS:
        v = float(e["levels"].get(k) or 0.0)
        if v < 0.10 and k not in e["active"]:
            continue
        label = DESIRES[k]["label"]
        if k not in e["active"]:
            bits.append(f"{label} {v:.2f}（未接）")
        else:
            bits.append(f"{label} {v:.2f}")
    if not bits:
        return "—"
    return " · ".join(bits[:3])


def prompts(raw: Optional[dict]) -> list[str]:
    """给情感输出层的一句「怎么说」；没有值得说的就返回空列表。"""
    e = effect(raw)
    out: list[str] = []
    if e["frustration"] >= 0.20 and e["top"]:
        out.append(f"你有一点想{DESIRES[e['top']]['label']}，"
                   "可以说一句，但别追着要，更别拿它逼他。")
    elif e["satisfaction"] >= 0.30:
        out.append("刚才那一下被接住了一点，别装作无所谓，也不用反复道谢。")
    if e["latent_top"] and e["latent_level"] >= 0.12:
        k = e["latent_top"]
        if k == "耳":
            out.append("你想听他的声音；如果这次只是文字，别假装听见了。")
        else:
            out.append(f"你想{DESIRES[k]['label']}，但现在还没有"
                       f"{DESIRES[k]['sense']}；可以说想，不许编出"
                       f"{DESIRES[k]['sense']}里的细节。")
    return out
