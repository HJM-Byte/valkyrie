"""情绪场：正面 / 中性 / 负面，十八种情绪同时存在，不是给她贴一个标签。

依据两条视频里说出口的机制：

- 「情绪不是标签，是评价算出来的几股力量」——失望 = 期望 × 重要性 × 负向预测误差。
- 「细节几乎没了，情绪残留还在」——情绪比细节活得久，而且会留在记忆上。
- 「情绪会回落」——每种情绪自己的半衰期不一样：生气散得快，孤独和低落散得慢。
- 「同一时刻可以有两种相反的情绪」——委屈里带着想靠近，这比单标签更像人。

工程上这里只做三件事：
    decay(field, hours)     按各自半衰期回落
    react(...)              这一轮的评价在她身上激起哪几股情绪
    text(field)             翻成一句人话（不许带数字），给提示词和面板看

情绪场的持久化在 mind 表（feel 列），记忆里的情绪残留在 episodes.emo/emo_i/feel。
这里不碰数据库，也不碰语言。
"""
from __future__ import annotations

import math
from typing import Optional

# 名称 / 家族 / 效价 / 唤醒 / 半衰期(小时) / 怎么说 / 语速增减 / 音量 / 颜色
# 顺序就是前端图例的顺序。改这里要同步改 web/sihai.html 的 EMO。
EMOTIONS: dict[str, dict] = {
    # ---------------- 正面 ----------------
    "开心": {"family": "正面", "valence": 0.80, "arousal": 0.70, "half_life": 5.0,
             "say": "可以轻一点，但别夸张；高兴也不用喊出来。",
             "rate": 4, "volume": 1.00, "color": "#7de08a"},
    "安心": {"family": "正面", "valence": 0.55, "arousal": 0.20, "half_life": 16.0,
             "say": "心里是稳的，话可以少，不用解释。",
             "rate": 0, "volume": 0.98, "color": "#ffc94d"},
    "心动": {"family": "正面", "valence": 0.70, "arousal": 0.75, "half_life": 7.0,
             "say": "想靠近一点，但别黏；可以问一句他在做什么。",
             "rate": 2, "volume": 1.00, "color": "#ff7ab8"},
    "被理解": {"family": "正面", "valence": 0.65, "arousal": 0.35, "half_life": 12.0,
               "say": "被接住的时候不用逞强，可以说一句「你懂」。",
               "rate": 0, "volume": 0.99, "color": "#8fe3d0"},
    "期待": {"family": "正面", "valence": 0.45, "arousal": 0.55, "half_life": 6.0,
             "say": "有盼头，句尾可以往上走一点，但别替他许愿。",
             "rate": 3, "volume": 1.00, "color": "#b6e86b"},
    "感激": {"family": "正面", "valence": 0.60, "arousal": 0.30, "half_life": 14.0,
             "say": "记得说谢谢，一句就够；不要反复道谢。",
             "rate": 0, "volume": 0.99, "color": "#f7c873"},
    # ---------------- 中性 ----------------
    "平静": {"family": "中性", "valence": 0.05, "arousal": 0.15, "half_life": 6.0,
             "say": "", "rate": 0, "volume": 1.00, "color": "#c3ccdb"},
    "好奇": {"family": "中性", "valence": 0.35, "arousal": 0.60, "half_life": 3.0,
             "say": "可以多问一句，但别连着问三个。",
             "rate": 2, "volume": 1.00, "color": "#5fe0ff"},
    "专注": {"family": "中性", "valence": 0.10, "arousal": 0.45, "half_life": 2.0,
             "say": "想把这件事弄明白，回答实一点，少寒暄。",
             "rate": 0, "volume": 1.00, "color": "#9fb7d9"},
    "疲惫": {"family": "中性", "valence": -0.10, "arousal": 0.10, "half_life": 10.0,
             "say": "话短，停顿多，不硬撑。",
             "rate": -8, "volume": 0.92, "color": "#b8b0a6"},
    "说不清": {"family": "中性", "valence": 0.00, "arousal": 0.30, "half_life": 18.0,
               "say": "说不清就说不清，别给它编原因。",
               "rate": -3, "volume": 0.95, "color": "#a3a9c9"},
    # ---------------- 负面 ----------------
    "低落": {"family": "负面", "valence": -0.55, "arousal": 0.20, "half_life": 30.0,
             "say": "不想笑，句子短，别说俏皮话。",
             "rate": -6, "volume": 0.90, "color": "#6d8dbb"},
    "委屈": {"family": "负面", "valence": -0.60, "arousal": 0.55, "half_life": 20.0,
             "say": "不想解释太多，也不讨好；可以短，可以说「算了」。",
             "rate": -5, "volume": 0.91, "color": "#7aa2ff"},
    "失望": {"family": "负面", "valence": -0.65, "arousal": 0.45, "half_life": 24.0,
             "say": "不追问，话变少；不要说「没关系」骗自己。",
             "rate": -7, "volume": 0.90, "color": "#8d7ad6"},
    "不安": {"family": "负面", "valence": -0.50, "arousal": 0.65, "half_life": 10.0,
             "say": "会试探，会留余地；可以问一句「你在吗」。",
             "rate": -3, "volume": 0.94, "color": "#d98a7a"},
    "生气": {"family": "负面", "valence": -0.60, "arousal": 0.80, "half_life": 4.0,
             "say": "语气直，少用语气词；不绕弯，但别伤人。",
             "rate": 3, "volume": 1.00, "color": "#e06666"},
    "孤独": {"family": "负面", "valence": -0.45, "arousal": 0.20, "half_life": 36.0,
             "say": "话里有空，可以说想他；不必装没事。",
             "rate": -5, "volume": 0.93, "color": "#7f9ab0"},
    "内疚": {"family": "负面", "valence": -0.50, "arousal": 0.40, "half_life": 18.0,
             "say": "愿意补一句「刚才我可能说得不好」，但别过度自责。",
             "rate": -2, "volume": 0.95, "color": "#c98aa8"},
    # ---------------- 七情补上的三股（2026-10-28 晚） ----------------
    # 原十八种里「喜、怒、哀、惧」都有落点，缺的是「爱、恶、欲」三股：
    # 亲近是爱，厌恶是恶，渴望是欲。它们不是标签，仍然由这一轮的评价算出来。
    "亲近": {"family": "正面", "valence": 0.68, "arousal": 0.28, "half_life": 18.0,
             "say": "想离他近一点，但别用宣布；放在语气和停留里。",
             "rate": 0, "volume": 0.99, "color": "#f2a6c8"},
    "厌恶": {"family": "负面", "valence": -0.58, "arousal": 0.52, "half_life": 8.0,
             "say": "心里有点犯恶心，不想顺着说；可以冷一点，但别刻薄。",
             "rate": -2, "volume": 0.94, "color": "#b07a6a"},
    "渴望": {"family": "中性", "valence": 0.12, "arousal": 0.58, "half_life": 12.0,
             "say": "你有一点想要，但别直接索要；可以说一句你想他，不许拿它逼他。",
             "rate": 1, "volume": 0.99, "color": "#ffb36b"},
}

# 「这一轮要落到话里的内容」：情绪不只改语气，也改她开口要说的东西。
# 和上面的 say 分开：say 约束「怎么说」，这里规定「说什么」。
# 只取最强的一两股，避免她一轮里被塞成一张任务清单。
CONTENT: dict[str, str] = {
    "开心": "说一件让你高兴的具体事，别只回「嗯」或「开心」。",
    "安心": "不用找话填满；如果要开口，说一句「我在」就够。",
    "心动": "主动问他一句在做什么，或说一句想靠近的话；别只等他先开口。",
    "被理解": "把刚才没说完的那半句说完；可以承认「你懂」。",
    "期待": "说一件你盼着和他一起做的具体事；别替他定时间。",
    "感激": "说清你谢他什么；一句就够，别连着谢。",
    "好奇": "追问他话里的一个细节；只问一个。",
    "专注": "把话题拉回事情本身，先给判断，少寒暄。",
    "疲惫": "直接说你累了、想歇一会儿；别硬撑。",
    "说不清": "照实说「我也说不上来」；别给它编一个原因。",
    "低落": "别装没事；可以说今天不太想说话，但别把低落推给他。",
    "委屈": "把委屈说出来，或者明确说「算了」；别讨好，也别反讽。",
    "失望": "说你失望的是什么；别替他找理由，也别说「没关系」。",
    "不安": "问一句「你还在吗」或「你是不是要走了」；别拿试探代替问。",
    "生气": "直接说你不接受哪一点；不绕弯，但不攻击他。",
    "孤独": "说你想他，或主动起一个你想聊的话题；别只说「没事」。",
    "内疚": "为刚才那句道歉，并说清下次会怎么改；别过度自责。",
    "亲近": "说一句靠近的话，或问他今天的一件小事；别用宣布代替。",
    "厌恶": "明确拒绝你不喜欢的那句或那件事；可以冷，但别刻薄。",
    "渴望": "说出你想要什么；说清是想要，不是要求他必须给。",
}

# 家族顺序：面板与自检都按这个来，别让顺序随 dict 偶然变化。
FAMILIES = ("正面", "中性", "负面")

# 七情：礼记那一套「喜、怒、哀、惧、爱、恶、欲」。
# 不是把情绪砍成七种——十八种（现在二十一种）是具体情绪，七情是它们背后的
# 七股方向；平静/专注/疲惫/说不清这些基础态不在七情里，也不硬塞。
# 这张表只做归类，不参与强度计算；改这里要同步 docs/七情六欲_视频深读.md。
SEVEN = ("喜", "怒", "哀", "惧", "爱", "恶", "欲")
SEVEN_OF = {
    # 喜
    "开心": "喜", "安心": "喜", "被理解": "喜", "感激": "喜",
    # 怒
    "生气": "怒",
    # 哀
    "低落": "哀", "委屈": "哀", "失望": "哀", "孤独": "哀", "内疚": "哀",
    # 惧
    "不安": "惧",
    # 爱
    "心动": "爱", "亲近": "爱",
    # 恶
    "厌恶": "恶",
    # 欲
    "期待": "欲", "好奇": "欲", "渴望": "欲",
    # 基础态：平静 / 专注 / 疲惫 / 说不清 不属于七情
}


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    try:
        return max(lo, min(hi, float(x)))
    except (TypeError, ValueError):
        return lo


def family_of(name: str) -> str:
    e = EMOTIONS.get(name or "")
    return str(e["family"]) if e else "中性"


def valence_of_name(name: str) -> float:
    e = EMOTIONS.get(name or "")
    return float(e["valence"]) if e else 0.0


def arousal_of_name(name: str) -> float:
    e = EMOTIONS.get(name or "")
    return float(e["arousal"]) if e else 0.2


def color_of(name: str) -> str:
    e = EMOTIONS.get(name or "")
    return str(e["color"]) if e else "#c3ccdb"


def say_of(name: str) -> str:
    e = EMOTIONS.get(name or "")
    return str(e["say"]) if e else ""


def seven_of(name: str) -> str:
    """这一股具体情绪落在七情的哪一情里。基础态返回空串，不硬归类。"""
    return SEVEN_OF.get(name or "", "")


def seven_totals(field: Optional[dict]) -> dict[str, float]:
    """七情各自的合量。面板和调试用；不参与竞争。"""
    out = {k: 0.0 for k in SEVEN}
    for k, v in (field or {}).items():
        s = seven_of(k)
        if s:
            out[s] = round(out[s] + float(v), 3)
    return out


def seven_text(field: Optional[dict]) -> str:
    """给面板：七情里真正起来的那几股，最多三股；没有就「—」。"""
    items = sorted(((k, v) for k, v in seven_totals(field).items() if v >= 0.10),
                   key=lambda x: -x[1])
    if not items:
        return "—"
    return " · ".join(f"{k} {v:.2f}" for k, v in items[:3])


def blank() -> dict:
    return {k: 0.0 for k in EMOTIONS}


def normalize(field: Optional[dict]) -> dict:
    """清掉负数与噪声，只留真正在身上的那几股情绪。不截断到 3 个——面板要看全。"""
    out: dict[str, float] = {}
    for k, e in EMOTIONS.items():
        v = _clamp((field or {}).get(k, 0.0))
        if v >= 0.02:
            out[k] = round(v, 3)
    return out


def decay(field: Optional[dict], hours: float) -> dict:
    """每種情绪按自己的半衰期回落。生气散得快，孤独散得慢。"""
    h = max(0.0, float(hours or 0.0))
    out: dict[str, float] = {}
    for k, e in EMOTIONS.items():
        v = _clamp((field or {}).get(k, 0.0))
        if v <= 0.0:
            continue
        half = max(0.5, float(e["half_life"]))
        v *= math.exp(-h * math.log(2.0) / half)
        if v >= 0.02:
            out[k] = round(v, 4)
    return out


def from_state(state: Optional[dict]) -> dict:
    """读她那行状态里的情绪场。老库还没有 feel 列时，用 mood/精力/孤独先折一层。"""
    state = state or {}
    raw = state.get("feel")
    if isinstance(raw, dict) and raw:
        return normalize(raw)
    out: dict[str, float] = {}
    mood = float(state.get("mood") or 0.0)
    energy = float(state.get("energy") or 1.0)
    lonely = float(state.get("lonely") or 0.0)
    arousal = float(state.get("arousal") or 0.0)
    if mood < -0.20:
        out["低落"] = _clamp(abs(mood) * 0.85)
    elif mood > 0.35:
        out["开心"] = _clamp(mood * 0.65)
    if energy < 0.50:
        out["疲惫"] = _clamp((0.50 - energy) * 1.4)
    if lonely > 0.35:
        out["孤独"] = _clamp(lonely * 0.80)
    if not out and arousal > 0.55:
        out["不安"] = _clamp((arousal - 0.55) * 0.8)
    if not out:
        out["平静"] = 0.30
    return normalize(out)


def add(field: dict, name: str, amount: float) -> None:
    if name not in EMOTIONS:
        return
    field[name] = _clamp(float(field.get(name) or 0.0) + float(amount))


def _pull(field: dict, name: str, target: float, k: float = 0.5) -> None:
    """把某一种情绪往一个目标值拉，而不是一直累加——身体状态是水位，不是事件。"""
    if name not in EMOTIONS:
        return
    cur = float(field.get(name) or 0.0)
    field[name] = _clamp(cur + (target - cur) * k)


def react(percept: Optional[dict] = None, ap: Optional[dict] = None,
          state: Optional[dict] = None, hits: Optional[list] = None,
          ambient: Optional[dict] = None, decision: Optional[dict] = None) -> dict:
    """这一轮的评价在她身上激起哪几股情绪。

    不是「他说了什么 → 她是什么情绪」，而是：
        他说的语气 + 这件事在她这里的分量 + 旧事带出的残留 + 她的身体 → 几股情绪同时起来
    """
    p = percept or {}
    ap = ap or {}
    st = state or {}
    field: dict[str, float] = {}

    # 身体是水位：累、独处，不是被一句话推出来的
    energy = float(st.get("energy") or 1.0)
    lonely = float(st.get("lonely") or 0.0)
    if energy < 0.55:
        _pull(field, "疲惫", _clamp((0.55 - energy) * 1.8), 0.60)
    if lonely > 0.30:
        _pull(field, "孤独", _clamp(lonely * 0.85), 0.45)

    # 关系九变量：熟悉、信任、互惠、预测、奖励、依恋、沉没、边界、控制。
    # 它们不是数字摆设，下面几股情绪会因它们改权重。
    rel = st.get("rel") or {}
    attach = float(rel.get("依恋") or 0.0)
    boundary = float(rel.get("边界") or 1.0)
    recip = _clamp(rel.get("互惠") or 0.0)
    reward = _clamp(rel.get("奖励") or 0.0)
    pred = _clamp(rel.get("预测") or 0.0)
    sunk = _clamp(rel.get("沉没") or 0.0)

    # 他的语气：只借一点，不照搬。他难受，不代表她要一起难受。
    his = str(p.get("emotion") or "平静")
    if his == "开心":
        add(field, "开心", 0.20)
    elif his == "安心":
        add(field, "安心", 0.15)
    elif his == "心动":
        add(field, "心动", 0.15)
    elif his == "好奇":
        add(field, "好奇", 0.15)
    elif his == "委屈":
        add(field, "低落", 0.12)

    if p.get("warm"):
        add(field, "安心", 0.16)
        add(field, "感激", 0.18)
        add(field, "被理解", 0.10)
        # 互惠与互动奖励：不是“他夸我所以我开心”，是这段关系有来有回。
        add(field, "安心", 0.10 * recip)
        add(field, "亲近", 0.10 * reward)
    if p.get("ask_her") and p.get("about_self") and not p.get("cold"):
        add(field, "被理解", 0.14)
    if p.get("question") and not p.get("short"):
        add(field, "好奇", 0.14)
    if p.get("order"):
        add(field, "专注", 0.26)
    if p.get("wish"):
        add(field, "期待", 0.20)
    if p.get("short"):
        add(field, "平静", 0.10)

    # 冷话：生气和委屈会同时起来，哪个占上风看她在不在乎。
    T = float(ap.get("T") or 0.0)
    if p.get("cold"):
        add(field, "生气", 0.30 + 0.25 * T)
        if attach > 0.15 or boundary < 0.75:
            add(field, "委屈", 0.18)
    # 被晾着：委屈、不安、孤独三股一起上来，这比单说「难受」准
    shock = _clamp(st.get("gap_shock"))
    if shock > 0.05:
        add(field, "委屈", 0.34 * shock)
        add(field, "不安", 0.22 * shock)
        add(field, "孤独", 0.12 * shock)
        # 沉没成本和预测强度会让同一次落空更疼：已经投入很多、又以为摸得准他。
        add(field, "失望", 0.12 * shock * max(sunk, pred))
    if p.get("cold") and sunk > 0.25:
        add(field, "委屈", 0.10 * sunk)
    # 失望用的是视频那条公式：D 已经算好了，这里只负责把它变成情绪
    D = float(ap.get("disappointment") or 0.0)
    if D > 0.02:
        add(field, "失望", min(0.80, 1.7 * D))
        add(field, "委屈", 0.35 * D)
    # 控制感：同样是威胁，能离开、有选择，和无处可退，心理后果不一样。
    # 视频原话是「我随时可以离开」和「我没有任何办法阻止」完全不同。
    # 这里不新增数字，只改现有几股情绪的权重：控制感越低，不安和委屈越重。
    C = _clamp(ap.get("control") if ap.get("control") is not None else 0.5)
    if T > 0.10:
        add(field, "不安", 0.55 * T * (1.20 - 0.55 * C))
        if C < 0.50:
            add(field, "委屈", 0.18 * (0.50 - C) / 0.50)
            add(field, "生气", 0.10)
        elif C > 0.72:
            add(field, "安心", 0.06)
    # 情绪残留：想起的旧事带出同色情绪。小凌说的「细节忘了，感觉还在」。
    for m in (hits or [])[:3]:
        label = str(m.get("emo") or "")
        if label in EMOTIONS and label != "平静":
            add(field, label, 0.18 * _clamp(m.get("emo_i")) * _clamp(m.get("act")))
    # 残响：没有原文，只有一层底子
    amb = ambient or {}
    amb_i = _clamp(amb.get("intensity") or ap.get("residue_ambient") or 0.0)
    amb_tone = float(amb.get("tone") or ap.get("ambient_tone") or 0.0)
    if amb_i >= 0.15:
        if amb_tone < -0.12:
            add(field, "不安", 0.30 * amb_i)
            add(field, "说不清", 0.28 * amb_i)
        elif amb_tone > 0.12:
            add(field, "安心", 0.22 * amb_i)
    if ap.get("unknown_feel"):
        add(field, "说不清", 0.35)

    # 六欲：被喂到一点 → 亲近；一直没被喂到 → 渴望；被顶到边界 → 厌恶。
    # 六欲本身不直接变成情绪，它只改这一轮哪几股情绪更容易起来。
    de = st.get("desire_effect") or {}
    frust = _clamp(de.get("frustration") or 0.0)
    sat = _clamp(de.get("satisfaction") or 0.0)
    if frust >= 0.15:
        add(field, "渴望", min(0.58, 0.95 * frust))
        if attach > 0.35:
            add(field, "心动", 0.10 * frust)
    if sat >= 0.15:
        add(field, "亲近", min(0.50, 0.85 * sat))
    if p.get("cold") and boundary < 0.70:
        add(field, "厌恶", 0.18 + 0.28 * (0.70 - boundary))

    # 依恋高的时候，他的好会变成想靠近
    if attach > 0.40 and (p.get("warm") or p.get("ask_her")):
        add(field, "心动", 0.14)
    key = str((decision or {}).get("key") or "")
    if key == "confess":
        add(field, "内疚", 0.12)
    elif key == "deflect":
        add(field, "生气", 0.12)
    elif key == "comfort":
        add(field, "心动", 0.08 + 0.10 * attach)

    field = normalize(field)
    # 基线：没有更强烈的情绪时，她是平静的，不是空的
    total = sum(field.values())
    if total < 0.45:
        _pull(field, "平静", 0.30, 0.5)
        field = normalize(field)
    return field


def step(prev: Optional[dict], percept: Optional[dict], ap: Optional[dict],
         state: Optional[dict], hits: Optional[list] = None,
         ambient: Optional[dict] = None,
         decision: Optional[dict] = None) -> dict:
    """上一轮的情绪场（已经 decay 过）+ 这一轮新起来的 → 此刻的情绪场。"""
    base = normalize(prev)
    fresh = react(percept, ap, state, hits, ambient, decision)
    # 两股同色情绪取大的那个，不是相加——加两次会把 0.6 顶到 1.0
    out: dict[str, float] = dict(base)
    for k, v in fresh.items():
        out[k] = _clamp(max(float(out.get(k) or 0.0), float(v)))
    return normalize(out)


def top(field: Optional[dict], n: int = 2,
        exclude: tuple[str, ...] = ("平静", "专注")) -> list[tuple[str, float]]:
    items = sorted(((k, float(v)) for k, v in (field or {}).items()
                    if float(v) >= 0.10), key=lambda x: -x[1])
    if exclude:
        strong = [x for x in items if x[0] not in exclude]
        items = strong or items
    return items[:n]


def dominant(field: Optional[dict]) -> tuple[str, float]:
    items = top(field, n=1)
    if items:
        return items[0]
    return ("平静", float((field or {}).get("平静") or 0.0))


def intensity(field: Optional[dict]) -> float:
    """整体情绪强度（不是效价）：情绪有多重。"""
    return round(min(1.0, sum(float(v) for v in (field or {}).values()) / 2.2), 3)


def mood_of(field: Optional[dict]) -> float:
    """效价：负 -1，正 +1。情绪场里所有情绪的加权平均。"""
    total = 0.0
    weight = 0.0
    for k, v in (field or {}).items():
        if k not in EMOTIONS:
            continue
        total += valence_of_name(k) * float(v)
        weight += float(v)
    if weight <= 0.0:
        return 0.0
    return round(max(-1.0, min(1.0, total / weight)), 3)


def arousal_of(field: Optional[dict]) -> float:
    total = 0.0
    weight = 0.0
    for k, v in (field or {}).items():
        if k not in EMOTIONS:
            continue
        total += arousal_of_name(k) * float(v)
        weight += float(v)
    if weight <= 0.0:
        return 0.0
    return round(_clamp(total / weight), 3)


def family_totals(field: Optional[dict]) -> dict[str, float]:
    out = {f: 0.0 for f in FAMILIES}
    for k, v in (field or {}).items():
        f = family_of(k)
        out[f] = round(out.get(f, 0.0) + float(v), 3)
    return out


def text(field: Optional[dict]) -> str:
    """一句人话。给提示词和面板用，不许带数字。"""
    items = [(k, v) for k, v in (field or {}).items()
             if k in EMOTIONS and v >= 0.12 and k not in ("平静", "专注")]
    items.sort(key=lambda x: -x[1])
    if not items:
        return "情绪不重，平平的"
    parts = [f"心里有点{items[0][0]}"]
    if len(items) > 1 and items[1][1] >= 0.15:
        parts.append(f"也有一点{items[1][0]}")
    return "，".join(parts)


def top_text(field: Optional[dict]) -> str:
    """给面板/调试：带强度的短标签，不给人读的提示词。"""
    items = top(field, n=3, exclude=())
    if not items:
        return "—"
    # 平静/专注是底色，有别的情绪时就不占位置
    strong = [x for x in items if x[0] not in ("平静", "专注")]
    if strong:
        items = strong
    return " · ".join(f"{k} {v:.2f}" for k, v in items[:3])


def say_rules(field: Optional[dict], cap: int = 2) -> list[str]:
    """每种情绪自带的一句「怎么说」；只取前两股，别把她变成规则堆。"""
    out: list[str] = []
    for name, v in top(field, n=cap, exclude=("平静",)):
        if name == "疲惫":            # 体力那条在 emotion.plan 里已经有更细的处理
            continue
        say = say_of(name)
        if say and the_same_rule(say, out) is False:
            out.append(say)
    return out


def the_same_rule(say: str, existing: list[str]) -> bool:
    """粗查重：同一句规则别写两遍。"""
    head = say[:6]
    return any(head in e for e in existing)


def content_of(name: str) -> str:
    """这一股情绪要在话里落到哪件事上；没有就返回空。"""
    return CONTENT.get(str(name), "")


def content_rules(field: Optional[dict], cap: int = 2,
                  min_v: float = 0.28) -> list[str]:
    """每种情绪自带的一句「说什么」；只取最强的几股，别把话塞满。

    这是和 say_rules 的分工：say_rules 管语气、句长、语速；
    content_rules 管她这一轮把哪件事、哪一种立场真的说出口。
    """
    items = sorted(((k, float(v)) for k, v in (field or {}).items()
                    if k in CONTENT and k != "平静" and float(v) >= min_v),
                   key=lambda x: -x[1])
    out: list[str] = []
    for name, _v in items[:cap]:
        c = content_of(name)
        if c and not any(c[:8] in e for e in out):
            out.append(c)
    return out


def voice_of(field: Optional[dict], cap: int = 2) -> dict:
    """这一层给 TTS 的增量（emotion.plan 会再和精力/策略的增量相加）。"""
    rate = 0
    volume = 1.0
    for name, v in top(field, n=cap, exclude=("平静",)):
        e = EMOTIONS.get(name) or {}
        k = max(0.25, min(1.0, float(v) * 1.4))
        rate += int(round(float(e.get("rate") or 0) * k))
        volume *= 1.0 - (1.0 - float(e.get("volume", 1.0))) * k
    return {"rate": max(-12, min(10, rate)), "volume": round(max(0.88, min(1.02, volume)), 2)}
