"""桌面版识海：把网页用的图数据整理成桌面程序能直接投影、交互的坐标。

这一层不依赖 tkinter，也不碰数据库。它只做四件事：
1. 把 payload 里的记忆点按「沉得多深」分到四层；
2. 在单位球里做一次和网页版同源的布局（壳层 + 力导向 + 网格排斥）；
3. 补齐残响、留存、颜色、屏幕属性；
4. 给桌面端留好时间轴与连线结构。

网页版是浏览器里的 canvas 2D 手写透视投影；桌面版不再经过浏览器，
但布局与判据必须和网页版一致，否则两边看到的是两片不同的海。
"""
from __future__ import annotations

import math
from typing import Any, Optional

GREY = "#9aa7b8"
INK = "#dfe7f5"
DEFAULT_EMOTION = {
    "安心": "#ffc94d", "开心": "#7de08a", "心动": "#ff7ab8",
    "好奇": "#5fe0ff", "委屈": "#7aa2ff", "平静": "#c3ccdb",
    "被理解": "#8fe3d0", "期待": "#b6e86b", "感激": "#f7c873",
    "专注": "#9fb7d9", "疲惫": "#b8b0a6", "说不清": "#a3a9c9",
    "低落": "#6d8dbb", "失望": "#8d7ad6", "不安": "#d98a7a",
    "生气": "#e06666", "孤独": "#7f9ab0", "内疚": "#c98aa8",
    "亲近": "#f2a6c8", "厌恶": "#b07a6a", "渴望": "#ffb36b",
}

# 桌面版星色：桌面窗口是深底、单层 canvas，没有浏览器里的辉光叠加，
# 用记忆层原始色会偏灰、偏白，尤其「平静」占了示例数据的大半。
# 这里只提高桌面星图的可见度与区分度，语义仍是「颜色 = 当时的情绪」，
# 网页版和数据库里的颜色都不动。
DESKTOP_EMOTION = {
    "安心": "#ffc857", "开心": "#68e39a", "心动": "#ff6fae",
    "好奇": "#22c8ff", "委屈": "#5b86ff", "平静": "#4f84ff",
    "被理解": "#35ddbd", "期待": "#a8e83c", "感激": "#ffb547",
    "专注": "#5f8df7", "疲惫": "#c69a70", "说不清": "#9f78ff",
    "低落": "#4569c8", "失望": "#8f68ff", "不安": "#e06b4f",
    "生气": "#ff5f6d", "孤独": "#6f9ed8", "内疚": "#d69abf",
    "亲近": "#ff9cc8", "厌恶": "#c0846f", "渴望": "#ffa94d",
}

# 层级色只用于出生期的壳环，不参与「星星颜色」的语义。
SHELL_TINTS = ["#5b77a1", "#4b6b92", "#3b5a80", "#2d486b"]
SHELLS = [
    {"r0": 0.055, "r1": 0.305, "def": "深层种子"},
    {"r0": 0.305, "r1": 0.530, "def": "自我与人物"},
    {"r0": 0.530, "r1": 0.775, "def": "可唤起"},
    {"r0": 0.775, "r1": 1.000, "def": "当前意识"},
]
FLAT_Y = 0.11
GOLD = math.pi * (3.0 - math.sqrt(5.0))

# 和网页版同步：情绪正负只给一点垂直方向，暖的略高、沉的略低。
EMO_VALENCE = {
    "安心": .55, "开心": .75, "心动": .70, "好奇": .35, "委屈": -.55,
    "平静": 0.0, "被理解": .60, "期待": .55, "感激": .70, "专注": .15,
    "疲惫": -.35, "说不清": -.10, "低落": -.60, "失望": -.65, "不安": -.55,
    "生气": -.60, "孤独": -.50, "内疚": -.45, "亲近": .65, "厌恶": -.55,
    "渴望": .45,
}


def clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else (hi if v > hi else v)


def fnv1a(s: str) -> int:
    h = 2166136261
    for ch in str(s):
        h ^= ord(ch)
        h = (h * 16777619) & 0xFFFFFFFF
    return h


def rnd01(ident: str, key: str) -> float:
    return (fnv1a(str(ident) + "#" + str(key)) % 10007) / 10007.0


def display_palette(meta: Optional[dict] = None) -> dict[str, str]:
    """桌面版情绪色：已知情绪用更亮的桌面调色，未知的自定义情绪保留原色。"""
    out = dict(DESKTOP_EMOTION)
    raw = (meta or {}).get("emotion") if isinstance(meta, dict) else None
    if isinstance(raw, dict):
        for name, color in raw.items():
            name = str(name)
            if name not in out:
                out[name] = str(color)
    return out


def hex_mix(a: str, b: str, t: float) -> str:
    """把 a 按 t 混向 b。用于在没有 alpha 的 Tk canvas 上模拟透明。"""
    t = clamp(float(t), 0.0, 1.0)
    try:
        aa = a.lstrip("#")
        bb = b.lstrip("#")
        ra, ga, ba = int(aa[0:2], 16), int(aa[2:4], 16), int(aa[4:6], 16)
        rb, gb, bbx = int(bb[0:2], 16), int(bb[2:4], 16), int(bb[4:6], 16)
    except Exception:
        return a
    return "#%02x%02x%02x" % (
        round(ra + (rb - ra) * t),
        round(ga + (gb - ga) * t),
        round(ba + (bbx - ba) * t),
    )


def derive_layer(n: dict) -> int:
    layer = n.get("layer")
    if layer is not None:
        try:
            v = int(layer)
            if 0 <= v <= 3:
                return v
        except Exception:
            pass
    if n.get("kind") == "self":
        return 1
    if n.get("kind") == "echo":
        return 0
    try:
        sink = float(n.get("sink", 0.5))
    except Exception:
        sink = 0.5
    if n.get("kind") == "fact":
        return 0 if sink >= 0.55 else 1
    if sink >= 0.55:
        return 2
    return 3


def retention_of(n: dict, t_max: float) -> float:
    if n.get("retention") is not None:
        try:
            return clamp(float(n["retention"]), 0.0, 1.0)
        except Exception:
            pass
    try:
        d0 = float(n.get("detail0", 1.0))
    except Exception:
        d0 = 1.0
    try:
        age = max(0.0, (t_max - float(n.get("ts") or t_max)) / 86400.0)
    except Exception:
        age = 0.0
    try:
        imp = float(n.get("imp") or 0.5)
    except Exception:
        imp = 0.5
    return clamp(d0 * math.exp(-age / 45.0) * (0.72 + 0.28 * imp), 0.0, 1.0)


def mark_of(n: dict) -> float:
    try:
        imp = float(n.get("imp") or 0)
    except Exception:
        imp = 0.0
    try:
        used = min(1.0, float(n.get("used") or 0) / 6.0)
    except Exception:
        used = 0.0
    try:
        emo = clamp(float(n.get("emo_i") or 0), 0.0, 1.0)
    except Exception:
        emo = 0.0
    return clamp(0.45 * imp + 0.30 * used + 0.25 * emo, 0.0, 1.0)


def is_echo_of(n: dict, t_min: float, t_max: float) -> bool:
    ret = retention_of(n, t_max)
    has_res = n.get("residue") is not None
    has_echo = n.get("echo") is not None
    if has_res or has_echo:
        if ret >= 0.25:
            return False
        return (float(n.get("residue") or 0) > 0.4
                or float(n.get("echo") or 0) > 0.4)
    if ret < 0.25:
        return mark_of(n) > 0.35
    try:
        span = max(1.0, (t_max - t_min) / 86400.0) if t_max else 0.0
        age = max(0.0, (t_max - float(n.get("ts") or t_max)) / 86400.0) if t_max else 0.0
    except Exception:
        span, age = 0.0, 0.0
    old = age > max(8.0, span * 0.45)
    return old and mark_of(n) > 0.60


def _compute_bands(nodes: list[dict]) -> list[dict]:
    counts = [0, 0, 0, 0]
    for n in nodes:
        pl = int(n.get("pl") or 0)
        counts[pl] = counts[pl] + 1
    weights = [math.sqrt(max(1, c)) for c in counts]
    total = sum(weights) or 1.0
    out: list[dict] = []
    r = 0.05
    for w in weights:
        wid = 0.95 * w / total
        out.append({"r0": r, "r1": r + wid})
        r += wid
    out[-1]["r1"] = 1.0
    return out


def _cluster_of(n: dict) -> str:
    """和网页版同一个扇区分组：情绪 + 日期 + 稳定桶。

    按「人」分会因为库里几乎全是“他”而挤成一团；按情绪分，同色的记忆
    自然聚成一片，跨情绪连线才真的像是「拐了个弯想起」。
    """
    if n.get("kind") == "self":
        return "self"
    if n.get("kind") == "fact":
        return "fact:" + str(n.get("label") or "")[:1]
    day = str(n.get("day") or "")[:10]
    bucket = fnv1a(str(n.get("id"))) % 3
    return f"emotion:{n.get('emotion') or '平静'}:{day}:{bucket}"


def _layout(nodes: list[dict], edges: list[dict], emotion: dict,
            t_min: float = 0.0) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    for n in nodes:
        n["pl"] = derive_layer(n)

    bands = _compute_bands(nodes)
    keys = sorted({_cluster_of(n) for n in nodes})
    sector = math.pi * 2.0 / max(1, len(keys))
    base_of = {k: i * sector + math.pi * 0.25 for i, k in enumerate(keys)}
    for i, n in enumerate(nodes):
        layer = n["pl"]
        band = bands[layer]
        sink = clamp(float(n.get("sink") or 0.0), 0.0, 1.0)
        jitter = (rnd01(str(n.get("id")), "r") - 0.5) * 0.10
        # 沉淀度决定靠里还是靠外；留足带内分布，别全挤成一条环。
        rr = band["r0"] + (band["r1"] - band["r0"]) * clamp(
            0.10 + 0.88 * (1.0 - sink) + jitter, 0.04, 0.98)
        base = base_of.get(_cluster_of(n), 0.0)
        th = base + (rnd01(str(n.get("id")), "t") - 0.5) * sector * 0.84 + layer * 0.17
        val = EMO_VALENCE.get(str(n.get("emotion") or "平静"), 0.0)
        y = (val * 0.52 + (rnd01(str(n.get("id")), "y") * 2.0 - 1.0) * 0.48) * FLAT_Y * rr
        hr = math.sqrt(max(0.0009, rr * rr - y * y))
        n["px3"] = math.cos(th) * hr
        n["pz3"] = math.sin(th) * hr
        n["py3"] = y
        n["prad"] = rr
        n["_i"] = i

    by_id = {str(n.get("id")): n for n in nodes}
    active_edges: list[dict] = []
    for e in edges:
        a = by_id.get(str(e.get("a")))
        b = by_id.get(str(e.get("b")))
        if not a or not b:
            continue
        edge = dict(e)
        edge["A"] = a
        edge["B"] = b
        edge["zavg"] = 0.0
        active_edges.append(edge)

    # 扇区/半径先定位置；桌面再跑少量「只排斥、不吸边」的松弛，
    # 把同一扇区里叠在一起的星推开。网页不跑这段，因为浏览器端
    # 的标签/辉光已经足够区分；桌面单层 canvas 需要更松一点。
    iters = 35
    cell = 0.070
    min_sep2 = cell * cell
    for it in range(iters):
        k = 1.0 - it / float(iters)

        for n in nodes:
            d = math.sqrt(n["px3"] ** 2 + n["py3"] ** 2 + n["pz3"] ** 2) or 1e-6
            f = (n["prad"] - d) * 0.06 * k
            n["px3"] += n["px3"] / d * f
            n["py3"] += n["py3"] / d * f
            n["pz3"] += n["pz3"] / d * f
            ylim = FLAT_Y * n["prad"] * 1.4
            n["py3"] = max(-ylim, min(ylim, n["py3"]))

        grid: dict[tuple[int, int, int], list[dict]] = {}
        for n in nodes:
            key = (math.floor(n["px3"] / cell), math.floor(n["py3"] / cell),
                   math.floor(n["pz3"] / cell))
            grid.setdefault(key, []).append(n)
        for n in nodes:
            ix = math.floor(n["px3"] / cell)
            iy = math.floor(n["py3"] / cell)
            iz = math.floor(n["pz3"] / cell)
            for ax in (-1, 0, 1):
                for ay in (-1, 0, 1):
                    for az in (-1, 0, 1):
                        arr = grid.get((ix + ax, iy + ay, iz + az))
                        if not arr:
                            continue
                        for m in arr:
                            if m["_i"] <= n["_i"]:
                                continue
                            dx = n["px3"] - m["px3"]
                            dy = n["py3"] - m["py3"]
                            dz = n["pz3"] - m["pz3"]
                            d2 = dx * dx + dy * dy + dz * dz
                            if d2 > min_sep2 or d2 < 1e-10:
                                continue
                            d = math.sqrt(d2)
                            f = (cell - d) * 0.42 * k
                            n["px3"] += dx / d * f
                            n["py3"] += dy / d * f
                            n["pz3"] += dz / d * f
                            m["px3"] -= dx / d * f
                            m["py3"] -= dy / d * f
                            m["pz3"] -= dz / d * f

    maxr = max((math.hypot(n["px3"], n["py3"], n["pz3"]) for n in nodes), default=0.0)
    if maxr > 1.02:
        s = 1.02 / maxr
        for n in nodes:
            n["px3"] *= s
            n["py3"] *= s
            n["pz3"] *= s

    t_max = 0.0
    for n in nodes:
        t_max = max(t_max, float(n.get("ts") or 0.0))
    if t_min <= 0:
        t_min = min((float(n.get("ts") or t_max) for n in nodes), default=t_max)
    for n in nodes:
        n["r_world"] = 0.0026 * float(n.get("size") or 4) * (
            0.85 + 0.30 * rnd01(str(n.get("id")), "sz"))
        n["is_echo"] = is_echo_of(n, t_min, t_max)
        n["is_ambient"] = (not n["is_echo"]) and (
            float(n.get("residue") or 0) > 0.40
            or float(n.get("echo") or 0) > 0.40)
        n["ret"] = retention_of(n, t_max)
        n["col"] = GREY if n["is_echo"] else str(
            emotion.get(n.get("emotion")) or DEFAULT_EMOTION.get(
                str(n.get("emotion")), "#c3ccdb"))
        n["x"], n["y"], n["z"] = n["px3"], n["py3"], n["pz3"]
        n["_p"] = {"x": 0.0, "y": 0.0, "z": 0.0, "s": 0.0}

    circles: list[dict] = []
    for i in range(3):
        r = bands[i]["r1"]
        world = []
        for k in range(61):
            a = k / 60.0 * math.pi * 2.0
            world.append((math.cos(a) * r, 0.0, math.sin(a) * r))
        circles.append({"r": r, "role": "ring", "world": world, "scr": []})
    for i in range(4):
        r = (bands[i]["r0"] + bands[i]["r1"]) * 0.5
        world = []
        for k in range(61):
            a = k / 60.0 * math.pi * 2.0
            world.append((math.cos(a) * r, 0.0, math.sin(a) * r))
        circles.append({"r": r, "role": "anchor", "world": world, "scr": []})
    return nodes, active_edges, bands, circles


def _birth(node: dict) -> dict:
    node = dict(node or {})
    node.setdefault("id", "star_self")
    node.setdefault("kind", "self")
    node.setdefault("label", "我")
    node.setdefault("text", "我在这里。")
    node["pl"] = 1
    node["px3"] = node["py3"] = node["pz3"] = 0.0
    node["x"] = node["y"] = node["z"] = 0.0
    node["prad"] = 0.0
    node["r_world"] = 0.05
    node["is_echo"] = False
    node["ret"] = 1.0
    node["col"] = "#cfe4ff"
    node["_p"] = {"x": 0.0, "y": 0.0, "z": 0.0, "s": 0.0}
    return node


def prepare_graph(data: dict) -> dict:
    """把 graph_payload 变成可投影的图。

    返回的字典不修改调用方传进来的原始数据（节点复制过）。
    """
    data = data or {}
    meta = dict(data.get("meta") or {})
    raw_nodes = [dict(n) for n in (data.get("nodes") or []) if isinstance(n, dict)]
    raw_edges = [dict(e) for e in (data.get("edges") or []) if isinstance(e, dict)]
    birth = bool(meta.get("locked"))

    emotion = display_palette(meta)
    if not isinstance(emotion, dict):
        emotion = dict(DESKTOP_EMOTION)

    if birth:
        nodes = [_birth(raw_nodes[0])] if raw_nodes else []
        edges: list[dict] = []
        bands = [_birth_band(i) for i in range(4)]
        circles: list[dict] = []
        t_min = float(meta.get("since") or 0.0)
        t_max = float(meta.get("until") or 0.0)
    else:
        t_min = float(meta.get("since") or 0.0)
        t_max = float(meta.get("until") or 0.0)
        nodes, edges, bands, circles = _layout(raw_nodes, raw_edges, emotion, t_min)
        if not t_max:
            t_max = max((float(n.get("ts") or 0.0) for n in nodes), default=0.0)

    return {
        "nodes": nodes,
        "edges": edges,
        "bands": bands,
        "circles": circles,
        "meta": meta,
        "birth": birth,
        "source": str(meta.get("source") or "real"),
        "t_min": t_min,
        "t_max": t_max,
    }


def _birth_band(i: int) -> dict:
    return dict(SHELLS[i])


def node_color(n: dict) -> str:
    return str(n.get("col") or "#c3ccdb")


def node_retention(n: dict) -> float:
    return clamp(float(n.get("ret") or 0.0), 0.0, 1.0)


def layer_name(meta: dict, i: int) -> str:
    layers = meta.get("layers")
    if isinstance(layers, list):
        for it in layers:
            if isinstance(it, dict) and int(it.get("id", -1)) == i:
                return str(it.get("name") or SHELLS[i]["def"])
    return SHELLS[i]["def"]


def fmt_age(meta: dict) -> str:
    try:
        since = float(meta.get("since") or 0)
    except Exception:
        since = 0.0
    if not since:
        return "—"
    h = max(0.0, (float(meta.get("until") or 0) - since) / 3600.0)
    if h < 1:
        return f"{int(h * 60)} 分钟"
    if h < 24:
        return f"{h:.1f} 小时"
    return f"{h / 24:.2f} 天"
