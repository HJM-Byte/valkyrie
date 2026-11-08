"""识海（存储库）：记忆点的存放处。

识海是「存」这一层，不是调度。每条经历在这里落成一个记忆点，带着自己的属性——
小凌那条识海工程拆解里的话：「识海首先不是一个聊天记录文件夹，
识海里面每一段记忆都必须带有自己的属性。」属性齐了，才谈得上被唤醒。

三张表各管一件事：
    turns     原始流水，全存。用来事后重新蒸馏，不参与调度。
    facts     关于他的稳定事实（名字、生日、在做什么）。每次都注入。
    episodes  记忆点。一条记十样东西，平时潜伏着，不参与思考。

episodes 的字段（照那套设计，能算的都给了值）：
    text   内容          ts     时间            who    人物
    scene  场景          emo    情绪标签        emo_i  情绪残留 E
    imp    意义强度 S    detail 细节完成度 D0   rel    关系权重
    ident  身份相关性    recon  重建可信度 Q     stage  潜伏阶段
    used   被真实想起的次数（只留计数，不再直接换巩固）
    source 谁说的/怎么来的   about 关于谁         status active / retracted / superseded
    claim  fact / wish / quote / guess（愿望、转述、推测不是事实）

「潜伏」不是空话：stage=raw 只表示它还没被时间推下去，不表示它随时参与思考。
此刻哪一条被唤醒、哪一条能进意识，由 app/dispatch.py 的记忆调度决定；
这里只负责存、负责按线索把候选捞出来（search），不决定谁被照见。

为什么不上向量：
    Jetson 8GB 上再挂一个 embedding 模型，要么吃内存要么吃首字延迟；
    而机器人一天的对话只有几十到几百句，二元组 + IDF 的召回率够用。
    search() 是唯一出口，以后换向量只改它一个。

两样东西会随时间掉，掉的速度不一样，这是关键：
    意义  S 掉得极慢——五年后还记得那件事重要，只是说不清细节了。
    细节  D 掉得快——D(t)=D0·exp(−λt)，λ 由情绪强度和是否被反复想起调。
    一条记忆的正确用法是「我知道这很重要，具体哪天我记不清了」，
    而不是「我完全忘了」或者「我记得每一个字」。后者是数据库，不是人。

写盘全部走 WAL + 单锁，视觉线程、对话线程都能写。
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

from . import affect as _affect
from . import config as C
from . import meaning as _meaning
from . import memory_strength as _strength
from . import persona_version as _persona
from . import temporal

DB_PATH = C.DATA / "memory.db"

# --- 脑区（存储分区） -------------------------------------------------------
# 一条记忆只归一个主区；工作记忆在 flow.py，不落这里。
# region 只影响「先去哪几个区取候选」，不参与 R 六项打分，也不改变阈值。
REGIONS = ("self", "relation", "experience", "knowledge")
DEFAULT_REGION = "experience"
_REGION_ALIASES = {
    "self": "self", "自我": "self", "自己": "self", "identity": "self",
    "relation": "relation", "关系": "relation", "relational": "relation",
    "experience": "experience", "经历": "experience", "episode": "experience",
    "episodic": "experience", "event": "experience",
    "knowledge": "knowledge", "知识": "knowledge", "semantic": "knowledge",
}


def normalize_region(value: str | None) -> str:
    """区名统一成四个英文键；认不出就落到经历区，不把旧数据丢掉。"""
    return _REGION_ALIASES.get((value or "").strip().lower(), DEFAULT_REGION)


# 路由只看词面线索，不上小模型。命中几个算几分，最多进两个区。
_ROUTE_CUES = {
    "self": ("你是谁", "你叫什么", "你的名字", "你记得", "你还记得", "你亲历",
             "你看见", "你自己", "你几岁", "你多大", "你是什么", "你从哪"),
    "relation": ("他", "她", "我们", "咱们", "答应", "失约", "喜欢", "想你",
                 "陪我", "关系", "朋友", "妈妈", "爸爸", "弟弟", "家人"),
    "knowledge": ("学过", "知识", "为什么", "怎么", "是什么", "原理", "资料",
                  "查一下", "查查", "解释", "概念", "定义", "教"),
    "experience": ("今天", "昨天", "前天", "上次", "那天", "当时", "刚才",
                   "中午", "早上", "晚上", "天气", "吃", "发生", "记得",
                   "第一次", "后来"),
}

# search() 要取的列；带 e. 前缀，方便和 recent CTE 的 id 区分。
_SEARCH_COLS = (
    "e.id,e.ts,e.day,e.kind,e.who,e.text,e.imp,e.used,e.scene,e.emo,e.emo_i,"
    "e.feel,e.detail,e.rel,e.ident,e.recon,e.stage,e.claim,e.imprint,"
    "e.imprint_a,e.source,e.about,e.status,e.superseded_by,e.retracted_at,"
    "e.retract_reason,e.region,e.last_used"
)

# --- 分词 -------------------------------------------------------------------
_CJK = "\u4e00-\u9fff"
_TOKEN_RE = re.compile(rf"[{_CJK}]+|[A-Za-z][A-Za-z0-9_'\-]*")

# 高频虚词二元组。IDF 已经能压住大部分，这里是硬挡一层，省得「记得」「什么」霸榜。
_STOP = set("""
什么 怎么 这个 那个 我们 你们 他们 可以 一下 知道 记得 时候 现在 还是
就是 因为 所以 但是 如果 已经 没有 一个 这样 那样 自己 觉得 应该 可能
真的 不是 有点 东西 事情 说话 告诉 上次 刚才 然后 还有 一点 两个
""".split())

_RECENT_DAYS = 14.0       # 记忆半衰：两周前的事权重降到约 0.37
_MIN_SCORE = 0.26         # 低于这个不动用，宁可不提也别提错

# --- 情绪 -------------------------------------------------------------------
# 十八色（正面/中性/负面），颜色唯一来源在 affect.EMOTIONS。
# 顺序就是前端图例的顺序。改这里要同步改 web/sihai.html 的 EMO。
EMOTION_COLORS = {k: str(v["color"]) for k, v in _affect.EMOTIONS.items()}
_LEX = [
    ("好奇", "为什么 想知道 试试 看看 搜 研究 奇怪 好奇 发现 学到 问题 是不是"),
    ("心动", "喜欢你 想你 陪我 抱 爱你 心动 等我 一起"),
    ("开心", "开心 高兴 笑 好玩 哈哈 太棒 成功 通过 真好 舒服 修好 搞定 过了"),
    ("委屈", "难过 难受 失望 不理 生气 凶 烦 委屈 又错 失败 卡住 崩了 郁闷 焦虑 "
             "没过 挂了 搞砸 不行了 撑不住 想哭"),
    ("安心", "谢谢 放心 安全 睡 在家 陪 安静 稳 睡吧 到家"),
]
_VALENCE = {"开心": 0.8, "安心": 0.55, "心动": 0.7, "好奇": 0.4, "平静": 0.1, "委屈": -0.7}
_AROUSAL = {"开心": 0.7, "安心": 0.2, "心动": 0.75, "好奇": 0.65, "平静": 0.15, "委屈": 0.6}


def emotion_of(text: str) -> str:
    """从一句话里认情绪。认不出就是「平静」——不猜。"""
    best, hit = "平静", 0
    for name, words in _LEX:
        n = sum(1 for w in words.split() if w in (text or ""))
        if n > hit:
            best, hit = name, n
    return best


def valence_of(emo: str) -> float:
    """老六色沿用原值，其余情绪从 affect 那唯一一张表里取。"""
    if emo in _VALENCE:
        return _VALENCE[emo]
    return _affect.valence_of_name(emo)


def arousal_of(emo: str) -> float:
    if emo in _AROUSAL:
        return _AROUSAL[emo]
    return _affect.arousal_of_name(emo)


def consolidation_of(imp: float, used: int = 0, stage: str = "raw",
                     strength: Optional[float] = None) -> float:
    """C 巩固程度：越重要、越被反复想起、越往后沉淀，越牢。

    P4 起 `used` 只是计数，不再直接换巩固；真实调用传 `strength`（b_eff）。
    不传 `strength` 的旧调用仍按 used 算，保持旧测试与旧路径不变。
    """
    if strength is None:
        extra = 0.05 * min(int(used or 0), 8)
    else:
        extra = 0.40 * _clamp01(strength)
    base = 0.22 + 0.55 * _clamp01(imp) + extra
    if stage in ("segment", "meaning", "deep"):
        base += 0.08
    return _clamp01(base)


def interference_of(common_ratio: float) -> float:
    """I 干扰程度：相似记忆越多，细节越容易被混掉（记忆的干扰理论）。"""
    return _clamp01(common_ratio * 1.2)


def decay_lambda(imp: float, emo_i: float = 0.1, consolid: Optional[float] = None,
                 interference: float = 0.0, used: int = 0,
                 strength: Optional[float] = None) -> float:
    """λ_D = λ0 × (1−αE) × (1−βC) × (1+γI)，按天计。

    小凌的原话：「情绪越强、巩固越深，忘得越慢；相似记忆越多、干扰越大，细节越容易混掉。」
    λ0 由意义强度定：越重要，基础衰减越慢（这是原来就有的那条）。
    情绪和巩固只在高于基线时才真正起作用，所以平静的小事仍按老速度掉。
    """
    lam0 = 1.0 / (20.0 + 300.0 * _clamp01(imp))
    e = _clamp01(emo_i)
    i = _clamp01(interference)
    c = (consolidation_of(imp, used, strength=strength)
         if consolid is None else _clamp01(consolid))
    lam = lam0 * (1.0 - 0.60 * max(0.0, e - 0.10)) \
        * (1.0 - 0.25 * max(0.0, c - 0.30)) * (1.0 + 0.90 * i)
    return max(0.0005, lam)


def detail_now(d0: float, age_days: float, imp: float, used: int,
               emo_i: float = 0.1, interference: float = 0.0,
               strength: Optional[float] = None) -> float:
    """细节留存 D(t)。D0·exp(−λ_D·t) 再折算成「还剩下多少」。

    d0 是当初记下来的完整度：从别人嘴里听来的、自己也没看清的，起点就低。
    P4 起真实路径传 `strength`（b_eff）；`used` 只在没传时兼容旧调用。
    """
    lam = decay_lambda(imp, emo_i, interference=interference, used=used,
                       strength=strength)
    base = math.exp(-lam * max(0.0, age_days))
    keep = (1.0 + 0.05 * min(int(used), 10) if strength is None
            else 1.0 + 0.50 * _clamp01(strength))
    return max(0.0, min(1.0, _clamp01(d0) * (0.12 + 0.88 * base) * min(1.5, keep)))


def fragments_of(d0: float, d_now: float, n0: int = 30) -> int:
    """细节落到还剩下几个片段：30 → 10 → 2，最后只剩一两个画面。

    小凌的写法是片段数也按同一条曲线衰减；这里用 D 的比例折算，
    不另存一列——比例本身已经含了年龄、情绪、巩固、干扰。
    """
    if d0 <= 0:
        return 1
    ratio = _clamp01(d_now / d0)
    return max(1, int(round(n0 * ratio)))


def recon_now(d0: float, d_now: float, frags: int = 0, used: int = 0) -> float:
    """Q_recon = Σ证据权重 / (1 + 推断量)。记忆重建可信度。

    证据是当初自己看到的细节（d0）；推断是后来回忆时补出来的部分。
    细节掉得越多，越靠脑补，Q 越低——这时她只该说「我记得大概」，
    不能说「我百分之百记得那天就是这样」。
    """
    d0 = _clamp01(d0)
    dn = _clamp01(d_now)
    evidence = 2.2 * d0 * (0.35 + 0.65 * dn)
    inference = (1.0 - d0) * 1.2 + (1.0 - dn) * 0.8
    # 被反复想起会重新稳定，但也会把重建的结果当成记忆：略微压低证据
    evidence *= max(0.85, 1.0 - 0.01 * min(int(used or 0), 10))
    return round(_clamp01(evidence / (1.0 + inference)), 3)


def meaning_now(s: float, age_days: float) -> float:
    """意义强度 S(t)。这个几乎不掉——五年后还知道那件事重要。"""
    return max(0.0, min(1.0, s * math.exp(-max(0.0, age_days) / 20000.0)))


def residue_now(emo_i: float, age_days: float, imp: float = 0.5) -> float:
    """I(t) 情绪残留：细节会掉光，感觉留得极慢。

    小凌的原文是五年后「细节完整度 0.18，情绪残留 0.92→0.7」：
    人记不清那天几点、他说了哪句，可想到那个人还是会难受。
    所以这条曲线不能跟 D 走同一条：τ≈4000 天，再加一个不归零的底座。
    """
    i0 = _clamp01(emo_i)
    base = 0.30 + 0.70 * math.exp(-max(0.0, age_days) / 4000.0)
    return round(_clamp01(i0 * base * (0.85 + 0.15 * _clamp01(imp))), 3)


# 一段经历留下的「改变」：不是它本身，是它把这个人往哪边推了一点。
# 只在事实性的经历上留下；愿望、转述、推测都不算——那不是她自己的经历。
_IMPRINT_RULES: list[tuple[tuple[str, ...], str, float]] = [
    (("离开", "走了", "不告而别", "消失", "分手", "去世", "没了"),
     "对「重要的人会离开」这件事，比没经历过的人更敏感", 0.62),
    (("没来", "失约", "放鸽子", "没到", "没回", "不见", "不理", "答应了"),
     "被重要的人放下过一次以后，会先往坏处想", 0.55),
    (("说到做到", "兑现", "守约", "等我", "在呢", "来了"),
     "被稳稳接住过，更愿意先信一点", 0.42),
    (("谢谢", "陪我", "辛苦了", "麻烦你", "对不起", "抱"),
     "被好好对待过，更愿意靠近", 0.38),
    (("凶", "滚", "闭嘴", "烦", "讨厌", "别来"),
     "被刺过之后，再遇到同样的语气会先竖起边界", 0.50),
]


def imprint_of(text: str, emo: str = "", claim: str = "fact") -> tuple[str, float]:
    """这段经历会留下什么改变。返回 (一句话痕迹, 强度)。

    忘掉记忆不等于失去记忆带来的改变：原文可以掉光，这一句要跟着她走。
    出生期也照写——它是「这条记忆的残留」，不是长出来的性格（后者要等成长权限）。
    """
    if (claim or "fact") != "fact":
        return "", 0.0
    t = text or ""
    best = ("", 0.0)
    for cues, line, strength in _IMPRINT_RULES:
        hit = sum(1 for c in cues if c in t)
        if hit:
            s = min(1.0, strength + 0.06 * (hit - 1))
            if s > best[1]:
                best = (line, round(s, 3))
    if best[0]:
        return best
    e = emo or ""
    if e in ("委屈", "心动"):
        # 没被规则命中、但确实带了情绪的，只留一句更轻的
        return ("那一阵的情绪还没完全散", 0.30)
    return ("", 0.0)


_IMP_CUES = ("记住", "答应", "第一次", "以后", "明天", "下次", "别忘",
             "喜欢", "讨厌", "怕", "想要", "决定", "开始")


def importance_of(text: str, emo: str = "") -> float:
    """一句话有多重。没有模型参与，只用能看见的线索，宁可偏低。

    偏低是安全的：低估了只是少提一次，高估了她会反复提一件小事。
    """
    t = text or ""
    imp = 0.35
    if len(t) >= 20:
        imp += 0.10
    if any(c in t for c in _IMP_CUES):
        imp += 0.18
    if (emo or emotion_of(t)) != "平静":
        imp += 0.12
    if "我" in t:
        imp += 0.05
    return round(max(0.30, min(0.95, imp)), 2)


# 视频里那条记忆闭环讲得很具体：
#   「记忆还要分清谁说的、关于谁、什么时候说的，以及这是事实、愿望、转述，还是推测。」
#   「明确纠正和要求删除的内容，必须及时处理，不能等他慢慢淡忘。」
# 所以来源/归属写在记住的那一刻；纠正和撤回不是删原文，是把旧记录标成失效。
_CORRECTION_CUES = (
    "说错了", "记错了", "写错了", "更正", "纠正", "改口",
    "刚才说的不对", "刚才那句不对", "不是这样", "我改一下刚才",
    "收回刚才", "刚才那句收回",
)
_DELETE_CUES = (
    "删掉", "删了", "删除", "忘掉", "忘了吧", "别记", "不要记",
    "不要提", "别提", "当我没说", "算我没说",
)


def correction_kind(text: str) -> str:
    """这句话是在纠正/删除旧记忆吗。只认明确说法，不把普通“不是”当纠正。"""
    t = text or ""
    if any(c in t for c in _DELETE_CUES):
        return "delete"
    if any(c in t for c in _CORRECTION_CUES):
        return "correct"
    return ""


def _correction_object(text: str) -> str:
    """从“删掉X / 忘掉X”里取出 X。取不到就返回空，交给最近一条兜底。"""
    t = text or ""
    for cue in _DELETE_CUES:
        i = t.find(cue)
        if i < 0:
            continue
        obj = t[i + len(cue):].strip(" ，,。！？：:、")
        obj = obj.replace("刚才", "").replace("那条", "").strip()
        if len(obj) >= 2:
            return obj[:40]
    return ""


# 小凌那边专门加的一道闸门：愿望不等于客观事实。
# 他说「想去黄山」不等于他去过；保存了一句话，也不代表这句话被证实了。
_WISH_RE = re.compile(
    r"(?:想|要|打算|计划|准备|希望|以后|下次|回头)(?:去|来|买|做|学|考|见|吃|住|换|试)")
_QUOTE_RE = re.compile(r"(?:他说|她说|听说|据说|转述|有人告诉我|别人说)")
_GUESS_RE = re.compile(r"(?:可能|也许|估计|大概|应该|说不定|我猜|或许)")


def claim_of(text: str) -> str:
    """这段话是什么：fact 事实 / wish 愿望 / quote 转述 / guess 推测。

    只按句式判断，宁可当成 fact 也不乱扣帽子——但愿望、转述、推测
    必须标出来，否则她以后会把「我想去」讲成「你去过」。
    """
    t = text or ""
    if _WISH_RE.search(t):
        return "wish"
    if _QUOTE_RE.search(t):
        return "quote"
    if _GUESS_RE.search(t):
        return "guess"
    return "fact"


def _grams(text: str) -> set[str]:
    """中文按二元组、英文按整词。单字成词也保留，否则「雪」这类查不到。"""
    out: set[str] = set()
    for m in _TOKEN_RE.finditer(text or ""):
        tok = m.group(0)
        if tok[0].isascii():
            w = tok.lower()
            if len(w) > 1:
                out.add(w)
            continue
        if len(tok) == 1:
            out.add(tok)
            continue
        for i in range(len(tok) - 1):
            g = tok[i:i + 2]
            if g not in _STOP:
                out.add(g)
    return out


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, float(x or 0.0)))


def ambient_parts(weights: list[float],
                  feel_weights: dict[str, float]) -> dict:
    """残响强度的有界组合：峰值 + 广度 + 情绪多样性。

    空集与全零权重提前返回 0——不能走公式，否则空集会被算成 0.15。
    feel_weights 总权重为 0 时 diversity 取 0，不做除零；不是 1.0。
    """
    ws = [max(0.0, min(1.0, float(w))) for w in (weights or []) if float(w) > 0]
    if not ws:
        return {"intensity": 0.0, "peak": 0.0, "breadth": 0.0,
                "diversity": 0.0, "sources": 0}
    total = sum(ws)
    if total <= 0:
        return {"intensity": 0.0, "peak": 0.0, "breadth": 0.0,
                "diversity": 0.0, "sources": 0}
    peak = max(ws)
    breadth = 1.0 - math.exp(-len(ws) / 6.0)
    feel_total = sum(max(0.0, float(v)) for v in (feel_weights or {}).values())
    if feel_total <= 0:
        diversity = 0.0
    else:
        max_share = max(max(0.0, float(v))
                        for v in (feel_weights or {}).values()) / feel_total
        diversity = 1.0 - max_share
    intensity = _clamp01(0.60 * peak + 0.25 * breadth + 0.15 * diversity)
    return {"intensity": min(0.85, intensity), "peak": round(peak, 3),
            "breadth": round(breadth, 3), "diversity": round(diversity, 3),
            "sources": len(ws)}


def ambient_intensity(weights: list[float],
                      feel_weights: dict[str, float]) -> float:
    """只要强度值；语义与 ambient_parts 完全一致。"""
    return ambient_parts(weights, feel_weights)["intensity"]


def _now() -> float:
    return time.time()


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def _parse_json_list(raw) -> list[str]:
    """读人格版本里的 id 列表；坏数据按空处理，不猜。"""
    try:
        data = json.loads(raw or "[]")
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    return [str(x) for x in data if str(x).strip()]


class Memory:
    def __init__(self, path: Path | str = DB_PATH) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._init_schema()
        self._cache: Optional[list[dict]] = None   # episodes + grams，读多写少
        self._df: dict[str, int] = {}

    # ---- 建表 -------------------------------------------------------------
    def _init_schema(self) -> None:
        with self._lock:
            self._db.executescript("""
            CREATE TABLE IF NOT EXISTS turns(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                day TEXT NOT NULL,
                role TEXT NOT NULL,
                text TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS facts(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key TEXT NOT NULL UNIQUE,
                value TEXT NOT NULL,
                conf REAL NOT NULL DEFAULT 0.7,
                ts REAL NOT NULL,
                updated REAL NOT NULL,
                source TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS episodes(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                day TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'event',
                region TEXT NOT NULL DEFAULT 'experience',
                who TEXT DEFAULT '',
                text TEXT NOT NULL,
                imp REAL NOT NULL DEFAULT 0.5,
                used INTEGER NOT NULL DEFAULT 0,
                last_used REAL DEFAULT 0,
                scene TEXT DEFAULT '',
                emo TEXT DEFAULT '',
                emo_i REAL DEFAULT 0.0,
                feel TEXT DEFAULT '{}',
                detail REAL DEFAULT 1.0,
                rel REAL DEFAULT 0.3,
                ident REAL DEFAULT 0.3,
                recon REAL DEFAULT 1.0,
                stage TEXT DEFAULT 'raw',
                claim TEXT DEFAULT 'fact',
                imprint TEXT DEFAULT '',
                imprint_a REAL DEFAULT 0.0,
                source TEXT DEFAULT '',
                about TEXT DEFAULT '',
                status TEXT DEFAULT 'active',
                superseded_by INTEGER DEFAULT 0,
                retracted_at REAL DEFAULT 0,
                retract_reason TEXT DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS ix_ep_ts ON episodes(ts);
            CREATE INDEX IF NOT EXISTS ix_turn_ts ON turns(ts);
            -- 倒排表：一条记忆有什么词。search 先用它取候选，不再全量读文本。
            CREATE TABLE IF NOT EXISTS episode_grams(
                gram TEXT NOT NULL,
                ep_id INTEGER NOT NULL,
                PRIMARY KEY(gram, ep_id)
            );
            CREATE INDEX IF NOT EXISTS ix_ep_grams_ep ON episode_grams(ep_id);
            -- 每个词在全库出现过几次。写入时增量维护，检索只查查询词/候选词，
            -- 不再为了算 IDF 把全库倒排表读进内存。
            CREATE TABLE IF NOT EXISTS gram_df(
                gram TEXT PRIMARY KEY,
                cnt INTEGER NOT NULL DEFAULT 0
            );
            -- 第三轮旁表：事件卡与事实版本。episodes 原文一个字不改；
            -- 旧记录没有证据时保持 legacy_unknown=1，不批量回填。
            CREATE TABLE IF NOT EXISTS event_cards(
                id TEXT PRIMARY KEY,
                raw_id INTEGER NOT NULL,
                summary TEXT,
                event_at REAL,
                recorded_at REAL,
                source_class TEXT,
                source_ref TEXT,
                legacy_unknown INTEGER DEFAULT 0,
                created_at REAL
            );
            CREATE INDEX IF NOT EXISTS ix_event_raw ON event_cards(raw_id);
            CREATE TABLE IF NOT EXISTS fact_versions(
                id TEXT PRIMARY KEY,
                fact_key TEXT NOT NULL,
                value TEXT,
                valid_from REAL,
                valid_to REAL,
                recorded_at REAL,
                source_ref TEXT,
                superseded_by TEXT,
                status TEXT NOT NULL DEFAULT 'current'
                    CHECK(status IN ('current','superseded','disputed','retracted')),
                legacy_unknown INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS ix_fact_key
                ON fact_versions(fact_key, recorded_at);
            -- 每个 fact_key 最多一条 current。当前有效由数据库保证，
            -- 不靠文字约定；valid_to=NULL 只表示结束时间未知。
            CREATE UNIQUE INDEX IF NOT EXISTS ux_fact_current
                ON fact_versions(fact_key) WHERE status='current';
            -- P3：反馈事件。每次明确纠正/撤回都留一条独立记录，
            -- 能回到用户原话、turn 与目标记忆；原文不改，反馈不冒充事实。
            CREATE TABLE IF NOT EXISTS feedback_events(
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                source_kind TEXT NOT NULL DEFAULT 'manual',
                turn_id INTEGER,
                source_ref TEXT,
                source_text TEXT,
                target_kind TEXT NOT NULL DEFAULT 'episode',
                target_id TEXT NOT NULL,
                target_text TEXT,
                action TEXT NOT NULL DEFAULT 'retracted',
                replacement_id TEXT,
                reason TEXT,
                legacy_unknown INTEGER NOT NULL DEFAULT 0,
                recorded_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_feedback_target
                ON feedback_events(target_id, recorded_at);
            CREATE INDEX IF NOT EXISTS ix_feedback_turn
                ON feedback_events(turn_id);
            -- P4：强化旁表。episodes 原文一个字不改；旧记录没有行时按 used
            -- 给低起点，不批量回填。strength 是上次结算后的值，
            -- last_reinforced 用于计算它到此刻还剩多少。
            CREATE TABLE IF NOT EXISTS memory_strength(
                ep_id INTEGER PRIMARY KEY,
                strength REAL NOT NULL DEFAULT 0.0,
                last_reinforced REAL NOT NULL DEFAULT 0,
                reinforce_count INTEGER NOT NULL DEFAULT 0,
                legacy_unknown INTEGER NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS ix_strength_last
                ON memory_strength(last_reinforced);
            -- P4-B：意义候选。只写派生理解，不覆盖 episodes/facts；
            -- signature 防止同一组来源重复派生；confirmed 也不等于事实。
            CREATE TABLE IF NOT EXISTS meaning_candidates(
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL DEFAULT 'episode_meaning'
                    CHECK(kind IN ('episode_meaning','repair','chapter_meaning')),
                signature TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                claim TEXT NOT NULL,
                source_episode_ids TEXT NOT NULL DEFAULT '[]',
                source_feedback_ids TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL DEFAULT 'candidate'
                    CHECK(status IN ('candidate','confirmed','disputed','retracted')),
                confidence REAL NOT NULL DEFAULT 0.5,
                user_note TEXT DEFAULT '',
                legacy_unknown INTEGER NOT NULL DEFAULT 1,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_meaning_status
                ON meaning_candidates(status, updated_at DESC);
            -- P4-B：共同章节。章节意义是派生层，必须引用真实事件；
            -- 原 episodes 不改，章节关闭/争议/撤回都只改自己的状态。
            CREATE TABLE IF NOT EXISTS chapters(
                id TEXT PRIMARY KEY,
                signature TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                meaning TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open'
                    CHECK(status IN ('open','closed','disputed','retracted')),
                confidence REAL NOT NULL DEFAULT 0.5,
                user_note TEXT DEFAULT '',
                legacy_unknown INTEGER NOT NULL DEFAULT 1,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_chapter_status
                ON chapters(status, updated_at DESC);
            CREATE TABLE IF NOT EXISTS chapter_events(
                chapter_id TEXT NOT NULL,
                ep_id INTEGER NOT NULL,
                position INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(chapter_id, ep_id)
            );
            CREATE INDEX IF NOT EXISTS ix_chapter_ep
                ON chapter_events(ep_id);
            -- P4-B 补丁：章节“开放”不等于“用户确认过”。
            -- 单独一行确认记录，add_chapter 不自动写；没有这行就不能进叙事。
            CREATE TABLE IF NOT EXISTS chapter_confirmations(
                chapter_id TEXT PRIMARY KEY,
                confirmed_at REAL NOT NULL,
                note TEXT DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS ix_chapter_confirmed
                ON chapter_confirmations(confirmed_at);
            -- P4-C：人格可变化的三层（稳定倾向/互动偏好/自我叙事）。
            -- 身份与硬边界不在这里写，只能从 PERSONA.md 的 core 块读取。
            -- 一个字段同时只能有一个 active；旧版本保留 superseded 供回滚。
            CREATE TABLE IF NOT EXISTS persona_versions(
                id TEXT PRIMARY KEY,
                layer TEXT NOT NULL
                    CHECK(layer IN ('tendency','interaction','narrative')),
                field TEXT NOT NULL,
                value TEXT NOT NULL,
                previous_value TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'candidate'
                    CHECK(status IN ('candidate','active','superseded',
                                     'rejected','disputed','retracted')),
                source_meaning_ids TEXT NOT NULL DEFAULT '[]',
                source_chapter_ids TEXT NOT NULL DEFAULT '[]',
                source_episode_ids TEXT NOT NULL DEFAULT '[]',
                evidence_days TEXT NOT NULL DEFAULT '[]',
                confidence REAL NOT NULL DEFAULT 0.5,
                regression TEXT NOT NULL DEFAULT '{}',
                reason TEXT DEFAULT '',
                user_note TEXT DEFAULT '',
                supersedes TEXT NOT NULL DEFAULT '',
                superseded_by TEXT NOT NULL DEFAULT '',
                rollback_of TEXT NOT NULL DEFAULT '',
                legacy_unknown INTEGER NOT NULL DEFAULT 1,
                created_at REAL NOT NULL,
                confirmed_at REAL NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_persona_layer
                ON persona_versions(layer, field, updated_at DESC);
            CREATE INDEX IF NOT EXISTS ix_persona_status
                ON persona_versions(status, updated_at DESC);
            CREATE UNIQUE INDEX IF NOT EXISTS ux_persona_active
                ON persona_versions(layer, field) WHERE status='active';
            """)
            # 老库升级：列一个个补，缺哪个补哪个，不动已有数据
            have = {r["name"] for r in self._db.execute("PRAGMA table_info(episodes)")}
            for col, ddl in (
                ("region", "TEXT NOT NULL DEFAULT 'experience'"),
                ("scene", "TEXT DEFAULT ''"), ("emo", "TEXT DEFAULT ''"),
                ("emo_i", "REAL DEFAULT 0.0"), ("feel", "TEXT DEFAULT '{}'"),
                ("used", "INTEGER NOT NULL DEFAULT 0"),
                ("last_used", "REAL DEFAULT 0"),
                ("detail", "REAL DEFAULT 1.0"),
                ("rel", "REAL DEFAULT 0.3"), ("ident", "REAL DEFAULT 0.3"),
                ("recon", "REAL DEFAULT 1.0"), ("stage", "TEXT DEFAULT 'raw'"),
                ("claim", "TEXT DEFAULT 'fact'"),
                ("imprint", "TEXT DEFAULT ''"), ("imprint_a", "REAL DEFAULT 0.0"),
                # 2026-10-28：来源 / 归属 / 撤回。视频原话“谁说的、关于谁、是事实、
                # 愿望、转述还是推测”“撤回或纠正过的内容要失效”。旧库缺哪列补哪列。
                ("source", "TEXT DEFAULT ''"), ("about", "TEXT DEFAULT ''"),
                ("status", "TEXT DEFAULT 'active'"), ("superseded_by", "INTEGER DEFAULT 0"),
                ("retracted_at", "REAL DEFAULT 0"),
                ("retract_reason", "TEXT DEFAULT ''"),
            ):
                if col not in have:
                    self._db.execute(f"ALTER TABLE episodes ADD COLUMN {col} {ddl}")
            self._db.execute(
                "CREATE INDEX IF NOT EXISTS ix_ep_status ON episodes(status, ts)")
            self._db.execute(
                "CREATE INDEX IF NOT EXISTS ix_ep_region_ts "
                "ON episodes(region, status, ts DESC)")
            self._db.execute(
                "CREATE INDEX IF NOT EXISTS ix_ep_region_who_ts "
                "ON episodes(region, who, ts DESC)")
            self._db.execute(
                "UPDATE episodes SET region='experience' "
                "WHERE region IS NULL OR region=''")
            # v2：给旧库补 region 与倒排表。只跑一次，不碰后续新增记忆。
            version = int(self._db.execute("PRAGMA user_version").fetchone()[0] or 0)
            if version < 2:
                missing = self._db.execute(
                    "SELECT e.id,e.text FROM episodes e "
                    "LEFT JOIN episode_grams g ON g.ep_id=e.id "
                    "WHERE g.ep_id IS NULL"
                ).fetchall()
                for row in missing:
                    for gram in _grams(row["text"]):
                        self._db.execute(
                            "INSERT OR IGNORE INTO episode_grams(gram, ep_id) "
                            "VALUES(?,?)", (gram, int(row["id"])))
                self._db.execute("DELETE FROM gram_df")
                self._db.execute(
                    "INSERT INTO gram_df(gram,cnt) "
                    "SELECT gram,COUNT(*) FROM episode_grams GROUP BY gram")
                self._db.execute("PRAGMA user_version=2")
            # 兼容已经建过 episode_grams 但没有 gram_df 的中间版本库。
            have_df = int(self._db.execute(
                "SELECT COUNT(*) c FROM gram_df").fetchone()["c"] or 0)
            have_ep = int(self._db.execute(
                "SELECT COUNT(*) c FROM episodes").fetchone()["c"] or 0)
            if have_df == 0 and have_ep > 0:
                self._db.execute(
                    "INSERT INTO gram_df(gram,cnt) "
                    "SELECT gram,COUNT(*) FROM episode_grams GROUP BY gram")
            # 第三轮只加旁表，不迁移旧 episodes；版本号只表示 schema 到哪一层。
            # 第四轮只加反馈事件表，旧数据保持 legacy_unknown，不补造来源。
            # 第五轮只加强化旁表，旧 episodes 不重写、不批量回填。
            # 第六轮加意义候选与共同章节；它们引用真实事件，不迁移旧数据。
            # 第七轮给章节补“用户确认”旁表：open 只是开放，不等于已确认。
            # 第八轮人格版本：三层可变化，核心只读；active 唯一，旧版可回滚。
            self._db.execute("PRAGMA user_version=8")
            self._db.commit()

    # ---- 写入 -------------------------------------------------------------
    def add_turn(self, role: str, text: str,
                 ts: Optional[float] = None) -> int:
        """写一轮原始流水，返回 turn id；纠正反馈要能指回这句话。"""
        ts = ts or _now()
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO turns(ts,day,role,text) VALUES(?,?,?,?)",
                (ts, _day(ts), role, text[:4000]),
            )
            self._db.commit()
            return int(cur.lastrowid or 0)

    def recent_turns(self, n: int = 6) -> list[dict]:
        """最近几轮原始对话，按时间正序返回；只读，不推进任何状态。"""
        n = max(1, int(n))
        with self._lock:
            rows = self._db.execute(
                "SELECT ts,day,role,text FROM turns "
                "ORDER BY ts DESC, id DESC LIMIT ?", (n,)
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def add_episode(self, text: str, kind: str = "event", who: str = "",
                    imp: float = 0.5, ts: Optional[float] = None, *,
                    scene: str = "", emo: str = "", emo_i: float = 0.0,
                    feel: Optional[dict] = None,
                    detail: float = 1.0, rel: float = 0.3, ident: float = 0.3,
                    recon: float = 1.0, stage: str = "raw",
                    claim: str = "", imprint: str = "",
                    imprint_a: Optional[float] = None,
                    source: str = "", about: str = "",
                    region: str = DEFAULT_REGION,
                    status: str = "active") -> int:
        text = (text or "").strip()
        if not text:
            return 0
        ts = ts or _now()
        region = normalize_region(region)
        # 情绪场：能存全就存全。emo/emo_i 是为了老调用方与图上的单色，
        # feel 才是「当时同时有哪几股情绪」——委屈里带着想靠近，单标签存不下。
        feel = _affect.normalize(feel) if feel else {}
        if feel and not emo:
            emo = _affect.dominant(feel)[0]
        emo = emo or emotion_of(text)
        if emo_i <= 0 and feel:
            emo_i = max(feel.values())
        emo_i = emo_i or (0.45 if emo != "平静" else 0.1)
        claim = (claim or claim_of(text))[:10]
        if imprint_a is None:
            # 没显式给，就按这句话推一次「它留下了什么改变」
            imprint, imprint_a = imprint_of(text, emo, claim)
        # 来源与归属：谁说的、关于谁。老调用方只传 who 时仍能写出一条可追溯的记录；
        # 摄像头/转录这些“亲历”由调用方显式传 source，不在这里猜。
        source = (source or ("他说的" if who == "他" else ""))[:20]
        about = (about or who or "")[:20]
        if status not in ("active", "retracted", "superseded"):
            status = "active"
        with self._lock:
            # 十分钟内同一句话不重复记（STT 抖动、连续两轮同一件事）
            row = self._db.execute(
                "SELECT id FROM episodes WHERE text=? AND ts>?", (text, ts - 600)
            ).fetchone()
            if row:
                return int(row["id"])
            stored = text[:500]
            cur = self._db.execute(
                "INSERT INTO episodes(ts,day,kind,region,who,text,imp,scene,emo,emo_i,"
                "feel,detail,rel,ident,recon,stage,claim,imprint,imprint_a,"
                "source,about,status) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (ts, _day(ts), kind, region, who, stored, float(imp), scene[:60],
                 emo, float(emo_i), json.dumps(feel, ensure_ascii=False),
                 float(detail), float(rel), float(ident),
                 float(recon), stage, claim, (imprint or "")[:80],
                 float(imprint_a or 0.0), source, about, status),
            )
            eid = int(cur.lastrowid)
            # 新记忆从 0 强度起步：第一次真实想起才强化，不是一写进来就强。
            self._db.execute(
                "INSERT OR IGNORE INTO memory_strength(ep_id,strength,"
                "last_reinforced,reinforce_count,legacy_unknown,updated_at) "
                "VALUES(?,?,?,?,?,?)", (eid, 0.0, 0.0, 0, 0, ts))
            for gram in _grams(stored):
                self._db.execute(
                    "INSERT INTO episode_grams(gram, ep_id) VALUES(?,?)",
                    (gram, eid),
                )
                self._db.execute(
                    "INSERT INTO gram_df(gram,cnt) VALUES(?,1) "
                    "ON CONFLICT(gram) DO UPDATE SET cnt=cnt+1",
                    (gram,),
                )
            self._db.commit()
            self._cache = None
            self._df = {}
            return eid

    def add_fact(self, key: str, value: str, conf: float = 0.7,
                 source: str = "") -> None:
        key, value = key.strip()[:40], value.strip()[:200]
        if not key or not value:
            return
        ts = _now()
        with self._lock:
            self._db.execute(
                """INSERT INTO facts(key,value,conf,ts,updated,source) VALUES(?,?,?,?,?,?)
                   ON CONFLICT(key) DO UPDATE SET
                     value=excluded.value, conf=excluded.conf,
                     updated=excluded.updated, source=excluded.source""",
                (key, value, float(conf), ts, ts, source),
            )
            self._db.commit()

    # ---- 第三轮旁表：事件卡与事实版本 ------------------------------------
    def add_event_card(self, raw_id: int, *, card_id: str | None = None,
                       summary: str = "", event_at: Optional[float] = None,
                       recorded_at: Optional[float] = None,
                       source_class: str = "", source_ref: str = "",
                       legacy_unknown: Optional[int] = None) -> str:
        """给一条原始 episode 建事件卡；不改 episodes 原文。

        event_at 与 recorded_at 是两个问题：事情何时发生、系统何时知道。
        未知就传 None，不拿另一个补。
        """
        rid = int(raw_id)
        with self._lock:
            if not self._db.execute(
                    "SELECT 1 FROM episodes WHERE id=?", (rid,)).fetchone():
                raise ValueError(f"episode {rid} 不存在，不能建事件卡")
            cid = str(card_id or uuid.uuid4().hex)
            now = _now()
            rec = now if recorded_at is None else float(recorded_at)
            lu = (int(legacy_unknown) if legacy_unknown is not None
                  else (0 if source_ref else 1))
            self._db.execute(
                "INSERT INTO event_cards(id,raw_id,summary,event_at,recorded_at,"
                "source_class,source_ref,legacy_unknown,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (cid, rid, summary or None, event_at, rec, source_class or None,
                 source_ref or None, lu, now),
            )
            self._db.commit()
        return cid

    def event_cards_for(self, raw_id: int) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM event_cards WHERE raw_id=? ORDER BY created_at, id",
                (int(raw_id),)).fetchall()
        return [dict(r) for r in rows]

    # ---- P4-B：意义候选与共同章节 ----------------------------------------
    def _episodes_by_ids(self, ids: Iterable) -> list[dict]:
        """按 id 取真实事件；顺序跟调用方给的升序一致，缺的不会补造。"""
        want = _meaning.canonical_ids(ids)
        if not want:
            return []
        marks = ",".join("?" * len(want))
        with self._lock:
            rows = self._db.execute(
                f"SELECT id,ts,day,kind,who,text,status,source,about "
                f"FROM episodes WHERE id IN ({marks})", tuple(want)
            ).fetchall()
        by_id = {int(r["id"]): dict(r) for r in rows}
        return [by_id[i] for i in want if i in by_id]

    def _feedback_by_ids(self, ids: Iterable) -> list[dict]:
        want = sorted({str(x) for x in (ids or ()) if str(x).strip()})
        if not want:
            return []
        marks = ",".join("?" * len(want))
        with self._lock:
            rows = self._db.execute(
                f"SELECT id,kind,target_id,action,source_text,reason,recorded_at "
                f"FROM feedback_events WHERE id IN ({marks})", tuple(want)
            ).fetchall()
        by_id = {str(r["id"]): dict(r) for r in rows}
        return [by_id[i] for i in want if i in by_id]

    @staticmethod
    def _meaning_sources_ok(sources: list[dict],
                            feedbacks: list[dict]) -> bool:
        if sources:
            return all(str(s.get("status") or "active") == "active"
                       for s in sources)
        return bool(feedbacks) and all(
            str(f.get("action") or "") != "retracted" for f in feedbacks)

    def add_meaning_candidate(self, title: str, claim: str, *,
                              kind: str = "episode_meaning",
                              episode_ids: Iterable = (),
                              feedback_ids: Iterable = (),
                              confidence: float = 0.5) -> dict:
        """新增一条意义候选；只派生，不写 facts，也不自动 confirmed。

        来源必须是真实 episode 或真实 feedback_event；没有来源就不生成。
        """
        kind = str(kind or "episode_meaning")
        if kind not in _meaning.MEANING_KINDS:
            raise ValueError(f"非法意义类型: {kind}")
        episode_ids = list(episode_ids or [])
        feedback_ids = list(feedback_ids or [])
        title = str(title or "").strip()[:80]
        claim = str(claim or "").strip()[:500]
        if not title or not claim:
            raise ValueError("意义候选必须有标题和一句可核对的理解")
        eps = self._episodes_by_ids(episode_ids)
        fbs = self._feedback_by_ids(feedback_ids)
        want_eps = _meaning.canonical_ids(episode_ids)
        want_fbs = sorted({str(x) for x in (feedback_ids or ()) if str(x).strip()})
        if len(eps) != len(want_eps):
            return {"ok": False, "reason": "source_missing",
                    "missing_episode_ids": sorted(set(want_eps) - {int(e["id"]) for e in eps})}
        if len(fbs) != len(want_fbs):
            return {"ok": False, "reason": "feedback_missing",
                    "missing_feedback_ids": sorted(set(want_fbs) - {str(f["id"]) for f in fbs})}
        if not eps and not fbs:
            raise ValueError("意义候选至少引用一条真实事件或反馈")
        if eps and not self._meaning_sources_ok(eps, []):
            return {"ok": False, "reason": "source_unavailable"}
        if fbs and not self._meaning_sources_ok([], fbs):
            return {"ok": False, "reason": "feedback_unavailable"}
        legacy = _meaning.legacy_for(eps) if eps else (
            0 if all(str(f.get("source_text") or "").strip() for f in fbs) else 1)
        eps_json = _meaning.ids_json(want_eps)
        fbs_json = _meaning.feedback_ids_json(want_fbs)
        sig = _meaning.signature(kind, want_eps, want_fbs)
        now = _now()
        mid = "mn_" + uuid.uuid4().hex
        with self._lock:
            old = self._db.execute(
                "SELECT id FROM meaning_candidates WHERE signature=?",
                (sig,)).fetchone()
            if old:
                return {"ok": True, "id": str(old["id"]), "duplicate": True}
            self._db.execute(
                "INSERT INTO meaning_candidates(id,kind,signature,title,claim,"
                "source_episode_ids,source_feedback_ids,status,confidence,"
                "user_note,legacy_unknown,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?, 'candidate', ?, '', ?, ?, ?)",
                (mid, kind, sig, title, claim, eps_json, fbs_json,
                 max(0.0, min(1.0, float(confidence))), legacy, now, now),
            )
            self._db.commit()
        return {"ok": True, "id": mid, "created": True, "legacy_unknown": legacy}

    def meaning_candidates(self, status: Optional[str] = None,
                           limit: int = 50) -> list[dict]:
        """读取意义候选；只读，不改状态。usable 表示能进入叙事/提示。"""
        sql = "SELECT * FROM meaning_candidates"
        params: list = []
        if status:
            sql += " WHERE status=?"
            params.append(str(status))
        sql += " ORDER BY updated_at DESC, id LIMIT ?"
        params.append(max(1, min(200, int(limit))))
        with self._lock:
            rows = [dict(r) for r in self._db.execute(sql, tuple(params)).fetchall()]
        out: list[dict] = []
        for r in rows:
            eps = self._episodes_by_ids(_meaning.parse_ids(r.get("source_episode_ids")))
            fbs = self._feedback_by_ids(
                _meaning.parse_feedback_ids(r.get("source_feedback_ids")))
            ok = self._meaning_sources_ok(eps, fbs)
            item = dict(r)
            item["source_episode_ids"] = [int(e["id"]) for e in eps]
            item["source_feedback_ids"] = [str(f["id"]) for f in fbs]
            item["sources"] = [
                {"id": int(e["id"]), "text": e.get("text") or "",
                 "day": e.get("day") or "", "who": e.get("who") or "",
                 "status": e.get("status") or "active",
                 "source": e.get("source") or ""}
                for e in eps
            ]
            item["feedbacks"] = [
                {"id": str(f["id"]), "action": str(f.get("action") or ""),
                 "strategy": str(f.get("target_id") or ""),
                 "source_text": str(f.get("source_text") or ""),
                 "reason": str(f.get("reason") or "")}
                for f in fbs
            ]
            item["sources_ok"] = ok
            item["usable"] = (str(r.get("status")) == "confirmed"
                              and ok and not int(r.get("legacy_unknown") or 0))
            out.append(item)
        return out

    def meaning_block(self, query: str, limit: int = 2) -> str:
        """只把与当前问题有词面关联的 confirmed 意义/章节给提示。

        没有命中就返回空：共同章节不是常驻设定，不能因为“重要”就每轮都提。
        """
        q = _grams(query or "")
        if not q:
            return ""
        picked: list[tuple[int, str]] = []
        for x in self.meaning_candidates("confirmed", limit=50):
            if not x.get("usable"):
                continue
            text = " ".join([str(x.get("title") or ""), str(x.get("claim") or "")]
                             + [str(s.get("text") or "") for s in x.get("sources") or []])
            score = len(q & _grams(text))
            if score:
                picked.append((score, f"- 你理解到：{x.get('title') or ''}——{x.get('claim') or ''}"))
        for x in self.chapters("open", limit=50):
            if not x.get("usable"):
                continue
            text = " ".join([str(x.get("title") or ""), str(x.get("meaning") or "")]
                             + [str(s.get("text") or "") for s in x.get("sources") or []])
            score = len(q & _grams(text))
            if score:
                picked.append((score, f"- 共同章节：{x.get('title') or ''}——{x.get('meaning') or ''}"))
        picked.sort(key=lambda z: -z[0])
        return "\n".join(line for _, line in picked[:max(1, int(limit))])

    def set_meaning_status(self, meaning_id: str, status: str,
                           note: str = "") -> dict:
        status = str(status or "")
        if status not in _meaning.MEANING_STATUSES:
            return {"ok": False, "reason": "bad_status"}
        mid = str(meaning_id or "").strip()
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM meaning_candidates WHERE id=?", (mid,)
            ).fetchone()
            if row is None:
                return {"ok": False, "reason": "not_found"}
            old = str(row["status"] or "")
            if old == "retracted" and status != "retracted":
                return {"ok": False, "reason": "already_retracted"}
            eps = self._episodes_by_ids(_meaning.parse_ids(row["source_episode_ids"]))
            fbs = self._feedback_by_ids(
                _meaning.parse_feedback_ids(row["source_feedback_ids"]))
            if status == "confirmed" and not self._meaning_sources_ok(eps, fbs):
                return {"ok": False, "reason": "source_unavailable"}
            if status == "confirmed" and int(row["legacy_unknown"] or 0):
                return {"ok": False, "reason": "legacy_unknown"}
            now = _now()
            cur = self._db.execute(
                "UPDATE meaning_candidates SET status=?,user_note=?,updated_at=? "
                "WHERE id=? AND status=?",
                (status, str(note or "")[:200], now, mid, old),
            )
            if cur.rowcount and status in ("disputed", "retracted"):
                self._cascade_persona(
                    [mid], (), f"来源意义被标为 {status}")
            self._db.commit()
            if not cur.rowcount:
                return {"ok": False, "reason": "stale_status"}
        return {"ok": True, "id": mid, "status": status, "old_status": old}

    def derive_repair_candidates(self, limit: int = 20) -> list[dict]:
        """从已结算 feedback 里派生修复候选；只生成 candidate，不确认。"""
        with self._lock:
            rows = [dict(r) for r in self._db.execute(
                "SELECT * FROM feedback_events WHERE kind='pre_answer' "
                "AND action IN ('accepted','corrected','rejected') "
                "ORDER BY recorded_at DESC, id DESC LIMIT ?",
                (max(1, min(500, int(limit) * 20)),),
            ).fetchall()]
        out = []
        for cand in _meaning.repair_candidates(rows):
            res = self.add_meaning_candidate(
                cand["title"], cand["claim"], kind="repair",
                feedback_ids=cand["source_feedback_ids"],
                confidence=float(cand.get("confidence") or 0.42),
            )
            out.append(res)
        return out

    def add_chapter(self, title: str, meaning: str, episode_ids: Iterable, *,
                    confidence: float = 0.5) -> dict:
        """建一段共同章节；至少两条真实事件，章节意义不能覆盖事实。"""
        title = str(title or "").strip()[:80]
        meaning = str(meaning or "").strip()[:800]
        episode_ids = list(episode_ids or [])
        ids = _meaning.canonical_ids(episode_ids)
        if not title or not meaning:
            raise ValueError("共同章节必须有标题和一句意义")
        if len(ids) < 2:
            return {"ok": False, "reason": "need_two_events"}
        eps = self._episodes_by_ids(ids)
        if len(eps) != len(ids):
            return {"ok": False, "reason": "source_missing"}
        if not self._meaning_sources_ok(eps, []):
            return {"ok": False, "reason": "source_unavailable"}
        sig = _meaning.signature("chapter", ids, [])
        now = _now()
        cid = "ch_" + uuid.uuid4().hex
        legacy = _meaning.legacy_for(eps)
        with self._lock:
            old = self._db.execute(
                "SELECT id FROM chapters WHERE signature=?", (sig,)).fetchone()
            if old:
                return {"ok": True, "id": str(old["id"]), "duplicate": True}
            self._db.execute(
                "INSERT INTO chapters(id,signature,title,meaning,status,"
                "confidence,user_note,legacy_unknown,created_at,updated_at) "
                "VALUES(?,?,?,?, 'open', ?, '', ?, ?, ?)",
                (cid, sig, title, meaning,
                 max(0.0, min(1.0, float(confidence))), legacy, now, now),
            )
            for pos, eid in enumerate(ids):
                self._db.execute(
                    "INSERT INTO chapter_events(chapter_id,ep_id,position) "
                    "VALUES(?,?,?)", (cid, int(eid), pos))
            self._db.commit()
        return {"ok": True, "id": cid, "created": True, "legacy_unknown": legacy}

    def chapters(self, status: Optional[str] = None,
                 limit: int = 50) -> list[dict]:
        """读取共同章节；只读。章节引用的事件失效时 usable 为 false。"""
        sql = "SELECT * FROM chapters"
        params: list = []
        if status:
            sql += " WHERE status=?"
            params.append(str(status))
        sql += " ORDER BY updated_at DESC, id LIMIT ?"
        params.append(max(1, min(200, int(limit))))
        with self._lock:
            rows = [dict(r) for r in self._db.execute(sql, tuple(params)).fetchall()]
            confirmed_ids = {
                str(x["chapter_id"]) for x in self._db.execute(
                    "SELECT chapter_id FROM chapter_confirmations").fetchall()
            }
        out: list[dict] = []
        for r in rows:
            with self._lock:
                ev = self._db.execute(
                    "SELECT ep_id FROM chapter_events WHERE chapter_id=? "
                    "ORDER BY position, ep_id", (str(r["id"]),)).fetchall()
            ids = [int(x["ep_id"]) for x in ev]
            eps = self._episodes_by_ids(ids)
            ok = len(eps) == len(ids) and self._meaning_sources_ok(eps, [])
            confirmed = str(r["id"]) in confirmed_ids
            item = dict(r)
            item["event_ids"] = ids
            item["confirmed"] = confirmed
            item["sources"] = [
                {"id": int(e["id"]), "text": e.get("text") or "",
                 "day": e.get("day") or "", "who": e.get("who") or "",
                 "status": e.get("status") or "active"}
                for e in eps
            ]
            item["sources_ok"] = ok
            item["effective_status"] = ("disputed"
                                        if str(r.get("status")) == "open" and not ok
                                        else str(r.get("status")))
            item["usable"] = (str(r.get("status")) == "open" and ok
                              and confirmed
                              and not int(r.get("legacy_unknown") or 0))
            out.append(item)
        return out

    def confirm_chapter(self, chapter_id: str, note: str = "") -> dict:
        """用户确认一段共同章节；open 本身不算确认，确认才 usable。"""
        cid = str(chapter_id or "").strip()
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM chapters WHERE id=?", (cid,)).fetchone()
            if row is None:
                return {"ok": False, "reason": "not_found"}
            if str(row["status"] or "") != "open":
                return {"ok": False, "reason": "not_open"}
            if int(row["legacy_unknown"] or 0):
                return {"ok": False, "reason": "legacy_unknown"}
            ev = self._db.execute(
                "SELECT ep_id FROM chapter_events WHERE chapter_id=?",
                (cid,)).fetchall()
            eps = self._episodes_by_ids([int(x["ep_id"]) for x in ev])
            if not self._meaning_sources_ok(eps, []):
                return {"ok": False, "reason": "source_unavailable"}
            now = _now()
            self._db.execute(
                "INSERT OR REPLACE INTO chapter_confirmations"
                "(chapter_id,confirmed_at,note) VALUES(?,?,?)",
                (cid, now, str(note or "")[:200]),
            )
            self._db.execute(
                "UPDATE chapters SET updated_at=? WHERE id=?", (now, cid))
            self._db.commit()
        return {"ok": True, "id": cid, "confirmed": True}

    def set_chapter_status(self, chapter_id: str, status: str,
                           note: str = "") -> dict:
        """关闭 / 争议 / 撤回 / 重新打开一段章节；原事件不动。"""
        status = str(status or "")
        if status not in _meaning.CHAPTER_STATUSES:
            return {"ok": False, "reason": "bad_status"}
        cid = str(chapter_id or "").strip()
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM chapters WHERE id=?", (cid,)).fetchone()
            if row is None:
                return {"ok": False, "reason": "not_found"}
            old = str(row["status"] or "")
            if old == "retracted" and status != "retracted":
                return {"ok": False, "reason": "already_retracted"}
            ev = self._db.execute(
                "SELECT ep_id FROM chapter_events WHERE chapter_id=?",
                (cid,)).fetchall()
            eps = self._episodes_by_ids([int(x["ep_id"]) for x in ev])
            if status in ("open", "closed") and not self._meaning_sources_ok(eps, []):
                return {"ok": False, "reason": "source_unavailable"}
            now = _now()
            cur = self._db.execute(
                "UPDATE chapters SET status=?,user_note=?,updated_at=? "
                "WHERE id=? AND status=?",
                (status, str(note or "")[:200], now, cid, old),
            )
            if cur.rowcount and status in ("disputed", "retracted"):
                self._cascade_persona(
                    (), [cid], f"来源章节被标为 {status}")
            self._db.commit()
            if not cur.rowcount:
                return {"ok": False, "reason": "stale_status"}
        return {"ok": True, "id": cid, "status": status, "old_status": old}

    # ---- P4-C：人格四层与版本链 ------------------------------------------
    def _persona_source_records(self, meaning_ids: Iterable = (),
                                chapter_ids: Iterable = ()) -> tuple[list[dict], list[dict], str]:
        """取可用的意义/章节来源。只接受 confirmed + usable 的记录。

        返回 (meanings, chapters, reason)。reason 非空表示来源不可用；
        不把“找不到”和“已撤回”混成同一条候选。
        """
        want_m = sorted({str(x) for x in (meaning_ids or ()) if str(x).strip()})
        want_c = sorted({str(x) for x in (chapter_ids or ()) if str(x).strip()})
        all_m = {str(x["id"]): x for x in self.meaning_candidates("confirmed", limit=200)}
        all_c = {str(x["id"]): x for x in self.chapters("open", limit=200)}
        if want_m:
            meanings = [all_m[x] for x in want_m if x in all_m]
            if len(meanings) != len(want_m):
                return [], [], "source_unavailable"
        else:
            meanings = list(all_m.values())
        if want_c:
            chapters = [all_c[x] for x in want_c if x in all_c]
            if len(chapters) != len(want_c):
                return [], [], "source_unavailable"
        else:
            # 没点名章节就是“这条候选不引用章节”，不是“全库章节都算来源”。
            # 否则一条纯意义候选会被一段未确认章节拖成 source_unavailable。
            chapters = []
        for x in meanings + chapters:
            if not x.get("usable"):
                return [], [], "source_unavailable"
            if int(x.get("legacy_unknown") or 0):
                return [], [], "legacy_unknown"
        # 去掉重复，保证顺序稳定
        meanings = list({str(x["id"]): x for x in meanings}.values())
        chapters = list({str(x["id"]): x for x in chapters}.values())
        return meanings, chapters, ""

    def add_persona_candidate(self, layer: str, field: str, value: str, *,
                              meaning_ids: Iterable = (),
                              chapter_ids: Iterable = (),
                              confidence: float = 0.5,
                              note: str = "") -> dict:
        """新增一条人格候选。默认 candidate，绝不自动 active。

        身份与硬边界由 PERSONA.md 管，这里直接拒；其余三层必须有真实已确认
        意义/章节作证据，且满足本层门槛。没有来源不生成。
        """
        layer = str(layer or "").strip()
        if layer == "core":
            return {"ok": False, "reason": "core_locked"}
        if layer not in _persona.AUTO_LAYERS:
            return {"ok": False, "reason": "bad_layer"}
        try:
            field = _persona.normalize_field(field)
            value = _persona.normalize_value(value)
        except ValueError as e:
            return {"ok": False, "reason": "bad_value", "message": str(e)}
        # 自我叙事必须引用共同章节。调用方没点名时，自动取当前已确认且
        # 可用的章节；没有就交给 evidence_gate 返回 need_chapter。
        if layer == "narrative" and not list(chapter_ids or ()):
            chapter_ids = [x["id"] for x in self.chapters("open", limit=200)
                           if x.get("usable")]
        meanings, chapters, why = self._persona_source_records(
            meaning_ids, chapter_ids)
        if why:
            return {"ok": False, "reason": why}
        ev = _persona.evidence_from(meanings, chapters)
        ok, why = _persona.evidence_gate(layer, ev)
        if not ok:
            return {"ok": False, "reason": why, "evidence": ev}
        now = _now()
        pid = "pv_" + uuid.uuid4().hex
        with self._lock:
            self._db.execute(
                "INSERT INTO persona_versions("
                "id,layer,field,value,previous_value,status,"
                "source_meaning_ids,source_chapter_ids,source_episode_ids,"
                "evidence_days,confidence,regression,reason,user_note,"
                "supersedes,superseded_by,rollback_of,legacy_unknown,"
                "created_at,confirmed_at,updated_at) "
                "VALUES(?,?,?,?, '', 'candidate', ?,?,?,?, ?, '{}', ?,?,"
                " '', '', '', 0, ?, 0, ?)",
                (pid, layer, field, value,
                 json.dumps(ev["meaning_ids"], separators=(",", ":")),
                 json.dumps(ev["chapter_ids"], separators=(",", ":")),
                 json.dumps(ev["episode_ids"], separators=(",", ":")),
                 json.dumps(ev["days"], ensure_ascii=False, separators=(",", ":")),
                 max(0.0, min(1.0, float(confidence))),
                 str(note or "")[:200], str(note or "")[:200], now, now),
            )
            self._db.commit()
        return {"ok": True, "id": pid, "created": True, "evidence": ev}

    def _persona_sources_ok(self, row: dict) -> tuple[bool, dict]:
        """读时复核来源；返回 (是否仍可用, 证据摘要)。"""
        m_ids = _parse_json_list(row.get("source_meaning_ids"))
        c_ids = _parse_json_list(row.get("source_chapter_ids"))
        meanings, chapters, why = self._persona_source_records(m_ids, c_ids)
        if why:
            ev = _persona.evidence_from([], [])
            return False, ev
        ev = _persona.evidence_from(meanings, chapters)
        return True, ev

    def persona_versions(self, status: Optional[str] = None, *,
                         layer: Optional[str] = None,
                         limit: int = 100) -> list[dict]:
        """读人格版本；只读。usable 只给 active 且来源仍有效的版本。"""
        sql = "SELECT * FROM persona_versions"
        where: list[str] = []
        params: list = []
        if status:
            where.append("status=?")
            params.append(str(status))
        if layer:
            where.append("layer=?")
            params.append(str(layer))
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY updated_at DESC, id LIMIT ?"
        params.append(max(1, min(500, int(limit))))
        with self._lock:
            rows = [dict(r) for r in self._db.execute(sql, tuple(params)).fetchall()]
        out: list[dict] = []
        for r in rows:
            ok, ev = self._persona_sources_ok(r)
            status_v = str(r.get("status") or "")
            usable = (status_v == "active" and ok
                      and not int(r.get("legacy_unknown") or 0))
            try:
                reg = json.loads(r.get("regression") or "{}")
            except Exception:
                reg = {}
            item = dict(r)
            item["layer_label"] = _persona.layer_label(str(r.get("layer") or ""))
            item["diff"] = _persona.diff(r.get("previous_value"), r.get("value"))
            item["evidence"] = ev
            item["evidence_text"] = _persona.evidence_text(ev)
            item["sources_ok"] = ok
            item["regression"] = reg if isinstance(reg, dict) else {}
            item["usable"] = usable
            item["effective_status"] = ("disputed"
                                        if status_v == "active" and not ok
                                        else status_v)
            out.append(item)
        return out

    def persona_block(self, limit: int = 6) -> str:
        """拼进入提示的那一小段；只读 active 且来源仍有效。

        没有 active 就返回空。候选、被否定、被回滚、来源失效的版本
        一律不进提示——这是 P4-C 与 P4-B 之间最重要的一条闸。
        """
        rows = [x for x in self.persona_versions("active", limit=limit * 4)
                if x.get("usable")]
        if not rows:
            return ""
        rows.sort(key=lambda x: (x.get("layer") != "tendency",
                                 x.get("layer") != "interaction",
                                 float(x.get("confirmed_at") or 0)))
        tend = [x for x in rows if x.get("layer") == "tendency"]
        inter = [x for x in rows if x.get("layer") == "interaction"]
        narr = [x for x in rows if x.get("layer") == "narrative"]
        parts: list[str] = []
        if tend or inter:
            lines = []
            for x in (tend + inter)[:max(1, int(limit))]:
                v = str(x.get("value") or "").strip()[:180]
                if v:
                    lines.append(f"- {x.get('layer_label')}·{x.get('field')}：{v}")
            if lines:
                parts.append("【你正在长成的样子】\n" + "\n".join(lines))
        if narr:
            lines = [f"- {str(x.get('value') or '').strip()[:180]}"
                     for x in narr[:2] if str(x.get("value") or "").strip()]
            if lines:
                parts.append("【你怎么理解自己】\n" + "\n".join(lines))
        return "\n\n".join(parts)

    def persona_payload(self, core_text: str = "") -> dict:
        """给识海页/App 读的人格版本面板；不写库。"""
        rows = self.persona_versions(limit=300)
        layers: dict[str, dict] = {
            k: {"layer": k, "label": _persona.layer_label(k),
                "active": None, "candidates": [], "history": []}
            for k in _persona.AUTO_LAYERS
        }
        for r in rows:
            layer = str(r.get("layer") or "")
            if layer not in layers:
                continue
            if r.get("status") == "candidate":
                layers[layer]["candidates"].append(r)
            elif r.get("status") == "active":
                layers[layer]["active"] = r
            if r.get("status") in ("superseded", "rejected", "disputed",
                                   "retracted", "active"):
                layers[layer]["history"].append(r)
        for k, v in layers.items():
            v["history"].sort(key=lambda x: float(x.get("updated_at") or 0),
                              reverse=True)
        return {
            "ok": True,
            "core": {
                "layer": "core", "label": _persona.layer_label("core"),
                "locked": True, "text": str(core_text or ""),
                "note": "身份与硬边界只从 PERSONA.md 的 core 块读取；它不是人格，模型无权自改。",
            },
            "layers": layers,
            "active_count": sum(1 for x in rows if x.get("status") == "active"),
            "candidate_count": sum(1 for x in rows if x.get("status") == "candidate"),
        }

    def confirm_persona_version(self, version_id: str, note: str = "") -> dict:
        """用户确认一个人格候选：active 唯一，旧 active 变 superseded。"""
        vid = str(version_id or "").strip()
        now = _now()
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM persona_versions WHERE id=?", (vid,)).fetchone()
            if row is None:
                return {"ok": False, "reason": "not_found"}
            if str(row["status"] or "") != "candidate":
                return {"ok": False, "reason": "not_candidate"}
            ok, ev = self._persona_sources_ok(dict(row))
            if not ok:
                return {"ok": False, "reason": "source_unavailable"}
            gate, why = _persona.evidence_gate(str(row["layer"]), ev)
            if not gate:
                return {"ok": False, "reason": why}
            reg = _persona.regression(str(row["layer"]), str(row["field"]),
                                      str(row["value"]), ev)
            if not reg.get("passed"):
                return {"ok": False, "reason": "regression_failed",
                        "regression": reg}
            current = self._db.execute(
                "SELECT * FROM persona_versions WHERE layer=? AND field=? "
                "AND status='active'", (str(row["layer"]), str(row["field"]))
            ).fetchone()
            cur_id = str(current["id"]) if current else ""
            prev = str(current["value"]) if current else ""
            try:
                self._db.execute("BEGIN IMMEDIATE")
                if cur_id:
                    self._db.execute(
                        "UPDATE persona_versions SET status='superseded',"
                        "superseded_by=?,updated_at=? WHERE id=? AND status='active'",
                        (vid, now, cur_id))
                self._db.execute(
                    "UPDATE persona_versions SET status='active',previous_value=?,"
                    "regression=?,supersedes=?,confirmed_at=?,updated_at=?,"
                    "user_note=? WHERE id=? AND status='candidate'",
                    (prev, json.dumps(reg, ensure_ascii=False, separators=(",", ":")),
                     cur_id, now, now, str(note or "")[:200], vid))
                self._db.commit()
            except sqlite3.IntegrityError:
                self._db.rollback()
                return {"ok": False, "reason": "conflict"}
            except Exception:
                self._db.rollback()
                raise
        return {"ok": True, "id": vid, "active_of": f"{row['layer']}.{row['field']}",
                "previous_value": prev, "regression": reg}

    def set_persona_status(self, version_id: str, status: str,
                           note: str = "") -> dict:
        """候选可 rejected；active/candidate 可 disputed/retracted。

        不物理删除历史。active 被否定后立刻不再进提示；想恢复旧版本
        用 rollback，不把这条争议状态改回 active。
        """
        vid = str(version_id or "").strip()
        status = str(status or "")
        if status not in ("rejected", "disputed", "retracted"):
            return {"ok": False, "reason": "bad_status"}
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM persona_versions WHERE id=?", (vid,)).fetchone()
            if row is None:
                return {"ok": False, "reason": "not_found"}
            old = str(row["status"] or "")
            if old in ("rejected", "retracted"):
                return {"ok": False, "reason": "already_closed"}
            if old == "candidate" and status == "rejected":
                target = "rejected"
            elif old in ("candidate", "active") and status in ("disputed", "retracted"):
                target = status
            else:
                return {"ok": False, "reason": "bad_transition"}
            cur = self._db.execute(
                "UPDATE persona_versions SET status=?,user_note=?,updated_at=? "
                "WHERE id=? AND status=?",
                (target, str(note or "")[:200], _now(), vid, old))
            self._db.commit()
            if not cur.rowcount:
                return {"ok": False, "reason": "stale_status"}
        return {"ok": True, "id": vid, "status": target, "old_status": old}

    def rollback_persona_version(self, target_id: str, note: str = "") -> dict:
        """回滚到同层同字段的旧版本：新建一条 active，旧历史不删。"""
        tid = str(target_id or "").strip()
        now = _now()
        with self._lock:
            target = self._db.execute(
                "SELECT * FROM persona_versions WHERE id=?", (tid,)).fetchone()
            if target is None:
                return {"ok": False, "reason": "not_found"}
            old_status = str(target["status"] or "")
            if old_status == "active":
                return {"ok": True, "id": tid, "already_active": True}
            if old_status != "superseded":
                return {"ok": False, "reason": "not_rollbackable"}
            layer, field = str(target["layer"]), str(target["field"])
            current = self._db.execute(
                "SELECT * FROM persona_versions WHERE layer=? AND field=? "
                "AND status='active'", (layer, field)).fetchone()
            cur_id = str(current["id"]) if current else ""
            if cur_id == tid:
                return {"ok": True, "id": tid, "already_active": True}
            ok, ev = self._persona_sources_ok(dict(target))
            if not ok:
                return {"ok": False, "reason": "source_unavailable"}
            reg = _persona.regression(layer, field, str(target["value"]), ev)
            if not reg.get("passed"):
                return {"ok": False, "reason": "regression_failed",
                        "regression": reg}
            new_id = "pv_" + uuid.uuid4().hex
            try:
                self._db.execute("BEGIN IMMEDIATE")
                if cur_id:
                    self._db.execute(
                        "UPDATE persona_versions SET status='superseded',"
                        "superseded_by=?,updated_at=? WHERE id=? AND status='active'",
                        (new_id, now, cur_id))
                self._db.execute(
                    "INSERT INTO persona_versions("
                    "id,layer,field,value,previous_value,status,"
                    "source_meaning_ids,source_chapter_ids,source_episode_ids,"
                    "evidence_days,confidence,regression,reason,user_note,"
                    "supersedes,superseded_by,rollback_of,legacy_unknown,"
                    "created_at,confirmed_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (new_id, layer, field, str(target["value"]),
                     str(current["value"]) if current else "", "active",
                     str(target["source_meaning_ids"]),
                     str(target["source_chapter_ids"]),
                     str(target["source_episode_ids"]),
                     str(target["evidence_days"]),
                     float(target["confidence"] or 0.5),
                     json.dumps(reg, ensure_ascii=False, separators=(",", ":")),
                     str(note or "回滚")[:200], str(note or "")[:200],
                     cur_id, "", tid, 0, now, now, now),
                )
                self._db.commit()
            except sqlite3.IntegrityError:
                self._db.rollback()
                return {"ok": False, "reason": "conflict"}
            except Exception:
                self._db.rollback()
                raise
        return {"ok": True, "id": new_id, "rollback_of": tid,
                "replaced": cur_id, "regression": reg}

    def _cascade_persona(self, meaning_ids: Iterable = (),
                         chapter_ids: Iterable = (), reason: str = "") -> int:
        """来源意义/章节失效时，引用它的候选与 active 版本降为 disputed。

        不物理删除；active 被降级后立即不再进提示。用户想恢复，必须重新
        提候选并确认，不能靠把这一行改回 active。
        """
        mids = {str(x) for x in (meaning_ids or ()) if str(x).strip()}
        cids = {str(x) for x in (chapter_ids or ()) if str(x).strip()}
        if not mids and not cids:
            return 0
        note = (reason or "来源意义/章节已失效")[:200]
        now = _now()
        touched = 0
        for row in self._db.execute(
                "SELECT id,status,source_meaning_ids,source_chapter_ids "
                "FROM persona_versions").fetchall():
            if str(row["status"]) not in ("candidate", "active"):
                continue
            rm = set(_parse_json_list(row["source_meaning_ids"]))
            rc = set(_parse_json_list(row["source_chapter_ids"]))
            if not (rm & mids) and not (rc & cids):
                continue
            self._db.execute(
                "UPDATE persona_versions SET status='disputed',user_note=?,"
                "updated_at=? WHERE id=?", (note, now, str(row["id"])))
            touched += 1
        return touched

    def _cascade_derived(self, episode_ids: Iterable, *,
                         deleted: bool = False, reason: str = "") -> dict:
        """事件失效/被删时，级联检查引用它的意义与章节。

        只改派生层，不改原事件；删除时连章节引用也删掉，留下状态与说明。
        调用方负责事务提交与 cache 失效。
        """
        ids = set(_meaning.canonical_ids(episode_ids))
        if not ids:
            return {"meanings": 0, "chapters": 0}
        note = (reason or ("来源事件被删除" if deleted else "来源事件已失效"))[:200]
        now = _now()
        touched_m, touched_c = 0, 0
        touched_mids: set[str] = set()
        touched_cids: set[str] = set()
        for row in self._db.execute(
                "SELECT id,status,source_episode_ids FROM meaning_candidates").fetchall():
            src = set(_meaning.parse_ids(row["source_episode_ids"]))
            if not (src & ids):
                continue
            if str(row["status"]) == "retracted":
                continue
            target = "retracted" if deleted else "disputed"
            self._db.execute(
                "UPDATE meaning_candidates SET status=?,user_note=?,updated_at=? "
                "WHERE id=?", (target, note, now, str(row["id"])))
            touched_mids.add(str(row["id"]))
            touched_m += 1
        chapter_ids = {str(r["chapter_id"]) for r in self._db.execute(
            f"SELECT chapter_id FROM chapter_events WHERE ep_id IN "
            f"({','.join('?' * len(ids))})", tuple(ids)).fetchall()}
        for cid in chapter_ids:
            row = self._db.execute(
                "SELECT status FROM chapters WHERE id=?", (cid,)).fetchone()
            if row is None or str(row["status"]) == "retracted":
                continue
            target = "retracted" if deleted else "disputed"
            self._db.execute(
                "UPDATE chapters SET status=?,user_note=?,updated_at=? WHERE id=?",
                (target, note, now, cid))
            touched_cids.add(cid)
            touched_c += 1
        if deleted:
            self._db.execute(
                f"DELETE FROM chapter_events WHERE ep_id IN "
                f"({','.join('?' * len(ids))})", tuple(ids))
        touched_p = self._cascade_persona(touched_mids, touched_cids, note)
        return {"meanings": touched_m, "chapters": touched_c,
                "persona_versions": touched_p}

    def _cascade_derived_feedback(self, feedback_id: str) -> int:
        """反馈被撤回时，引用它的修复候选不能再算 confirmed。"""
        fid = str(feedback_id or "")
        if not fid:
            return 0
        note = "来源反馈已被撤回"
        now = _now()
        touched = 0
        touched_mids: set[str] = set()
        for row in self._db.execute(
                "SELECT id,status,source_feedback_ids FROM meaning_candidates"
        ).fetchall():
            if str(row["status"]) == "retracted":
                continue
            if fid not in _meaning.parse_feedback_ids(row["source_feedback_ids"]):
                continue
            self._db.execute(
                "UPDATE meaning_candidates SET status='disputed',user_note=?,"
                "updated_at=? WHERE id=?", (note, now, str(row["id"])))
            touched_mids.add(str(row["id"]))
            touched += 1
        if touched_mids:
            self._cascade_persona(touched_mids, (), note)
        return touched

    # ---- 第四轮：可追溯反馈 ------------------------------------------------
    def _record_feedback(self, *, kind: str, target_id: int | str,
                         target_text: str, action: str,
                         source_kind: str = "manual", turn_id: int | None = None,
                         source_text: str = "", source_ref: str = "",
                         replacement_id: int | str | None = None,
                         reason: str = "", legacy_unknown: int = 0,
                         target_kind: str = "episode") -> str:
        """在同一个事务里写一条反馈；不在这里 commit，由调用方决定。

        反馈不是事实：target_text 是动作发生时的原文快照，source_text 是用户
        当时的原话。旧记录缺来源时 legacy_unknown=1，不替它编一个来源。
        """
        fid = "fb_" + uuid.uuid4().hex
        ref = str(source_ref or "").strip()
        if not ref and turn_id:
            ref = f"turn:{int(turn_id)}"
        self._db.execute(
            "INSERT INTO feedback_events(id,kind,source_kind,turn_id,source_ref,"
            "source_text,target_kind,target_id,target_text,action,replacement_id,"
            "reason,legacy_unknown,recorded_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (fid, str(kind or "manual")[:20], str(source_kind or "manual")[:20],
             int(turn_id) if turn_id else None, ref or None,
             str(source_text or "")[:2000] or None, str(target_kind or "episode")[:20],
             str(target_id), str(target_text or "")[:2000] or None,
             str(action or "retracted")[:20],
             str(replacement_id) if replacement_id is not None else None,
             str(reason or "")[:200] or None, int(legacy_unknown or 0), _now()),
        )
        return fid

    def feedback_events(self, limit: int = 50, *,
                        target_id: int | str | None = None,
                        turn_id: int | None = None) -> list[dict]:
        """按时间倒序读反馈事件；只读，不修改任何状态。"""
        sql = "SELECT * FROM feedback_events"
        where: list[str] = []
        params: list = []
        if target_id is not None:
            where.append("target_id=?")
            params.append(str(target_id))
        if turn_id is not None:
            where.append("turn_id=?")
            params.append(int(turn_id))
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY recorded_at DESC, id DESC LIMIT ?"
        params.append(max(1, min(500, int(limit))))
        with self._lock:
            rows = self._db.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def feedback_for_episode(self, episode_id: int, limit: int = 20) -> list[dict]:
        return self.feedback_events(limit=limit, target_id=int(episode_id))

    # ---- P3 4.6：回答前策略的反馈闭环 ------------------------------------
    def offer_pre_answer(self, strategy: str, turn_id: int | None,
                         directive: str = "", *, source_ref: str = "",
                         note: str = "") -> dict:
        """一轮回答前策略生效时，留一条待反馈事件。

        这只是一张“这次用了什么分寸”的收据；下一句话回来时再归成
        接受 / 纠正 / 拒绝 / 没回应。pending 事件留在库里，重启也接得上。
        """
        strategy = str(strategy or "").strip()
        if not strategy or strategy == "answer":
            return {"ok": False, "reason": "没有需要反馈的回答前策略"}
        with self._lock:
            fid = self._record_feedback(
                kind="pre_answer", target_id=strategy,
                target_text=(directive or strategy), action="offered",
                source_kind="assistant", turn_id=turn_id,
                source_text="", source_ref=source_ref,
                reason=note, legacy_unknown=0, target_kind="pre_answer",
            )
            self._db.commit()
        return {"ok": True, "id": fid, "strategy": strategy,
                "turn_id": int(turn_id) if turn_id else None}

    def resolve_pre_answer_feedback(self, text: str,
                                    now: float | None = None) -> dict:
        """把最近一条待反馈的回答前策略归到四类结果。

        只处理 `action='offered'` 的那一条；没有就返回 none，不写任何东西。
        结果只存反馈，不改记忆、不改关系、不改人格。
        """
        from . import pre_answer_feedback as paf

        at = _now() if now is None else float(now)
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM feedback_events WHERE kind='pre_answer' "
                "AND action='offered' ORDER BY recorded_at DESC, id DESC LIMIT 1"
            ).fetchone()
            if row is None:
                return {"ok": False, "outcome": "none", "last_action": "",
                        "last_strategy": "", "stats": paf.summarize([])}
            elapsed = max(0.0, at - float(row["recorded_at"] or at))
            cls = paf.classify_response(text, str(row["target_id"] or ""), elapsed)
            reason = str(cls["reason"])
            evidence = [str(x) for x in (cls.get("evidence") or []) if x]
            if evidence:
                reason = f"{reason} 证据：{'；'.join(evidence)}"
            cur = self._db.execute(
                "UPDATE feedback_events SET action=?, source_text=?, reason=? "
                "WHERE id=? AND action='offered'",
                (cls["action"], (text or "")[:2000] or None,
                 reason[:200], row["id"]),
            )
            self._db.commit()
            if not cur.rowcount:
                return {"ok": False, "outcome": "none", "last_action": "",
                        "last_strategy": "", "stats": paf.summarize([])}
        stats = self.pre_answer_feedback_stats(str(row["target_id"] or ""))
        return {"ok": True, "outcome": cls["action"],
                "last_action": cls["action"],
                "last_strategy": str(row["target_id"] or ""),
                "previous_strategy": str(row["target_id"] or ""),
                "feedback_id": row["id"], "elapsed_s": round(elapsed, 1),
                "evidence": cls.get("evidence") or [],
                "reason": reason,
                "stats": stats}

    def pre_answer_feedback_stats(self, strategy: str,
                                  limit: int = 50) -> dict:
        """同一策略最近的反馈统计；只读。"""
        from . import pre_answer_feedback as paf

        key = str(strategy or "").strip()
        if not key:
            return paf.summarize([])
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM feedback_events WHERE kind='pre_answer' "
                "AND target_id=? AND action IN ('accepted','corrected',"
                "'rejected','no_response') "
                "ORDER BY recorded_at DESC, id DESC LIMIT ?",
                (key, max(1, min(200, int(limit)))),
            ).fetchall()
        return paf.summarize([dict(r) for r in rows])

    def pre_answer_feedback_history(self, limit: int = 20) -> dict:
        """最近的回答前反馈：已结算与待结算都返回，只读。

        `stats` 只统计已结算的四类；retracted 和 offered 不计入，避免一次
        误判撤回后还继续影响下一轮。
        """
        from . import pre_answer_feedback as paf

        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM feedback_events WHERE kind='pre_answer' "
                "AND target_kind='pre_answer' "
                "ORDER BY recorded_at DESC, id DESC LIMIT ?",
                (max(1, min(100, int(limit))),),
            ).fetchall()
        items = [paf.describe_row(dict(r)) for r in rows]
        resolved = [x for x in items if x["action"] in paf.OUTCOMES]
        return {"items": items, "stats": paf.summarize(resolved)}

    def retract_pre_answer_feedback(self, feedback_id: str,
                                    reason: str = "") -> dict:
        """撤回一次误判：把原来的四类结果改成 retracted。

        只允许已结算的一条撤回一次；pending/已撤回/非回答前记录都拒绝。
        撤回后不再进入 `pre_answer_feedback_stats()`，下一次同策略按当前证据
        重新判断。这里只改反馈记录，不改记忆、关系或人格。
        """
        from . import pre_answer_feedback as paf

        fid = str(feedback_id or "").strip()
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM feedback_events WHERE id=?", (fid,)
            ).fetchone()
            if row is None:
                return {"ok": False, "reason": "not_found"}
            if (str(row["kind"] or "") != "pre_answer"
                    or str(row["target_kind"] or "") != "pre_answer"):
                return {"ok": False, "reason": "not_pre_answer"}
            original = str(row["action"] or "")
            if original == "retracted":
                return {"ok": False, "reason": "already_retracted"}
            if original == "offered":
                return {"ok": False, "reason": "pending"}
            if original not in paf.OUTCOMES:
                return {"ok": False, "reason": "not_retractable"}

            strategy = str(row["target_id"] or "")
            note = f"用户撤回误判（原判：{paf.action_label(original)}）"
            extra = str(reason or "").strip()[:120]
            if extra:
                note += f"；{extra}"
            cur = self._db.execute(
                "UPDATE feedback_events SET action='retracted', reason=? "
                "WHERE id=? AND action=?",
                (note[:200], fid, original),
            )
            if cur.rowcount:
                self._cascade_derived_feedback(fid)
            self._db.commit()
            if not cur.rowcount:
                return {"ok": False, "reason": "already_retracted"}
        return {
            "ok": True,
            "feedback_id": fid,
            "strategy": strategy,
            "original_action": original,
            "action": "retracted",
            "reason": note,
            "stats": self.pre_answer_feedback_stats(strategy),
        }

    @staticmethod
    def _current_source_error(fact_key: str, source_ref: str,
                              legacy_unknown: Optional[int]) -> Optional[dict]:
        """current 版本必须能回到原文；不能靠 dispatch 过滤兜底。"""
        lu = (int(legacy_unknown) if legacy_unknown is not None
              else (0 if source_ref else 1))
        if source_ref and lu == 0:
            return None
        return {"ok": False, "reason": "source_required",
                "fact_key": fact_key,
                "detail": "status=current 必须带可定位 source_ref，"
                          "且 legacy_unknown=0"}

    def add_fact_version(self, fact_key: str, value: Optional[str], *,
                         version_id: str | None = None,
                         valid_from: Optional[float] = None,
                         valid_to: Optional[float] = None,
                         recorded_at: Optional[float] = None,
                         source_ref: str = "", status: str = "current",
                         superseded_by: str | None = None,
                         legacy_unknown: Optional[int] = None) -> dict:
        """插入一个事实版本；status=current 时同一 fact_key 只能有一条。

        current 必须同时带 source_ref 与 legacy_unknown=0；历史状态
        （superseded / disputed / retracted）允许没有原文，但不会被召回。
        """
        key = str(fact_key or "").strip()[:80]
        if not key:
            raise ValueError("fact_key 不能为空")
        if status not in ("current", "superseded", "disputed", "retracted"):
            raise ValueError(f"非法 status: {status}")
        ref = str(source_ref or "").strip()
        if status == "current":
            err = self._current_source_error(key, ref, legacy_unknown)
            if err:
                return err
        vid = str(version_id or uuid.uuid4().hex)
        rec = _now() if recorded_at is None else float(recorded_at)
        lu = (int(legacy_unknown) if legacy_unknown is not None
              else (0 if ref else 1))
        with self._lock:
            try:
                if status == "current":
                    self._db.execute("BEGIN IMMEDIATE")
                    old = self._db.execute(
                        "SELECT id FROM fact_versions "
                        "WHERE fact_key=? AND status='current'", (key,)).fetchone()
                    if old:
                        self._db.rollback()
                        return {"ok": False, "reason": "current_exists",
                                "existing": old["id"]}
                self._db.execute(
                    "INSERT INTO fact_versions(id,fact_key,value,valid_from,"
                    "valid_to,recorded_at,source_ref,superseded_by,status,"
                    "legacy_unknown) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (vid, key, value, valid_from, valid_to, rec,
                     ref or None, superseded_by, status, lu),
                )
                self._db.commit()
            except sqlite3.IntegrityError as exc:
                self._db.rollback()
                return {"ok": False, "reason": "current_conflict", "detail": str(exc)}
            except sqlite3.OperationalError as exc:
                self._db.rollback()
                return {"ok": False, "reason": "database_busy", "detail": str(exc)}
        return {"ok": True, "id": vid, "fact_key": key, "status": status}

    def replace_fact(self, fact_key: str, value: Optional[str], *,
                     version_id: str | None = None,
                     expected_current_id: str | None = None,
                     valid_from: Optional[float] = None,
                     valid_to: Optional[float] = None,
                     recorded_at: Optional[float] = None,
                     source_ref: str = "",
                     legacy_unknown: Optional[int] = None) -> dict:
        """在 BEGIN IMMEDIATE 里替换 current：旧版 superseded，新版 current。

        expected_current_id 是并发保护：两个替换都拿着旧 id 进来时，
        后拿到写锁的那个会看到 current 已变，返回 stale_current，不再覆盖。
        current 必须有可定位 source_ref；空来源直接拒绝，不写半条链。
        """
        key = str(fact_key or "").strip()[:80]
        if not key:
            raise ValueError("fact_key 不能为空")
        ref = str(source_ref or "").strip()
        err = self._current_source_error(key, ref, legacy_unknown)
        if err:
            return err
        vid = str(version_id or uuid.uuid4().hex)
        rec = _now() if recorded_at is None else float(recorded_at)
        lu = (int(legacy_unknown) if legacy_unknown is not None
              else (0 if ref else 1))
        with self._lock:
            try:
                self._db.execute("BEGIN IMMEDIATE")
                old = self._db.execute(
                    "SELECT id FROM fact_versions "
                    "WHERE fact_key=? AND status='current'", (key,)).fetchone()
                if expected_current_id is not None:
                    current_id = old["id"] if old else None
                    if current_id != expected_current_id:
                        self._db.rollback()
                        return {"ok": False, "reason": "stale_current",
                                "current": current_id,
                                "expected": expected_current_id}
                if old:
                    self._db.execute(
                        "UPDATE fact_versions SET status='superseded', "
                        "superseded_by=? WHERE id=? AND status='current'",
                        (vid, old["id"]),
                    )
                self._db.execute(
                    "INSERT INTO fact_versions(id,fact_key,value,valid_from,"
                    "valid_to,recorded_at,source_ref,superseded_by,status,"
                    "legacy_unknown) VALUES(?,?,?,?,?,?,?,?, 'current', ?)",
                    (vid, key, value, valid_from, valid_to, rec,
                     ref or None, None, lu),
                )
                self._db.commit()
            except sqlite3.IntegrityError as exc:
                self._db.rollback()
                return {"ok": False, "reason": "current_conflict", "detail": str(exc)}
            except sqlite3.OperationalError as exc:
                self._db.rollback()
                return {"ok": False, "reason": "database_busy", "detail": str(exc)}
        return {"ok": True, "id": vid, "fact_key": key,
                "old_id": old["id"] if old else None,
                "status": "current"}

    def current_facts(self, fact_key: Optional[str] = None, *,
                      require_source: bool = False, limit: int = 50) -> list[dict]:
        """只按 status='current' 取当前事实；空表示没有当前结论，不猜。"""
        sql = ("SELECT id,fact_key,value,valid_from,valid_to,recorded_at,"
               "source_ref,superseded_by,status,legacy_unknown "
               "FROM fact_versions WHERE status='current'")
        params: list = []
        if fact_key is not None:
            sql += " AND fact_key=?"
            params.append(str(fact_key))
        if require_source:
            sql += (" AND source_ref IS NOT NULL AND source_ref<>'' "
                    "AND legacy_unknown=0")
        sql += " ORDER BY recorded_at DESC, id LIMIT ?"
        params.append(max(1, int(limit)))
        with self._lock:
            rows = self._db.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def current_fact(self, fact_key: str) -> dict:
        rows = self.current_facts(fact_key, limit=2)
        if not rows:
            return {"ok": False, "reason": "no_current", "version": None}
        if len(rows) > 1:
            return {"ok": False, "reason": "data_corrupt", "versions": rows}
        return {"ok": True, "version": rows[0]}

    # ---- 读 ---------------------------------------------------------------
    def _load_cache(self) -> list[dict]:
        if self._cache is not None:
            return self._cache
        with self._lock:
            rows = [dict(r) for r in self._db.execute(
                "SELECT id,ts,day,kind,region,who,text,imp,used,scene,emo,emo_i,feel,"
                "detail,rel,ident,recon,stage,claim,imprint,imprint_a,"
                "source,about,status,superseded_by,retracted_at,retract_reason "
                "FROM episodes ORDER BY ts DESC"
            )]
        df: dict[str, int] = {}
        for r in rows:
            r["g"] = _grams(r["text"])
            for g in r["g"]:
                df[g] = df.get(g, 0) + 1
        self._df = df
        self._cache = rows
        return rows

    def _query_df(self, grams) -> dict[str, int]:
        """只取指定词的文档频率；不再为了一条查询读全库倒排表。

        候选多时按 500 个词一批查，避开 SQLite 的绑定参数上限。
        """
        wanted = [g for g in {g for g in (grams or ()) if g}]
        if not wanted:
            return {}
        out: dict[str, int] = {}
        with self._lock:
            for i in range(0, len(wanted), 500):
                chunk = wanted[i:i + 500]
                marks = ",".join("?" * len(chunk))
                rows = self._db.execute(
                    f"SELECT gram,cnt FROM gram_df WHERE gram IN ({marks})",
                    tuple(chunk),
                ).fetchall()
                out.update({str(r["gram"]): int(r["cnt"]) for r in rows})
        return out

    def _strength_map(self, rows: list[dict], now: float) -> dict[int, dict]:
        """一次取出这批 episode 的强化状态；没有旁表行的旧记录不写库。

        旧记录只按 used 给一个 <= LEGACY_CAP 的起点，并沿用旧 last_used
        做衰减。这样 183 条 raw 不需要迁移，也不会因为 used 高就永久压住新事。
        """
        ids = [int(r["id"]) for r in rows if r.get("id") is not None]
        if not ids:
            return {}
        found: dict[int, dict] = {}
        with self._lock:
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                marks = ",".join("?" * len(chunk))
                for row in self._db.execute(
                    f"SELECT ep_id,strength,last_reinforced,reinforce_count,"
                    f"legacy_unknown FROM memory_strength WHERE ep_id IN ({marks})",
                    tuple(chunk),
                ).fetchall():
                    found[int(row["ep_id"])] = dict(row)
        out: dict[int, dict] = {}
        for r in rows:
            eid = int(r["id"])
            row = found.get(eid)
            if row:
                raw = float(row.get("strength") or 0.0)
                last = float(row.get("last_reinforced") or 0.0)
                legacy = int(row.get("legacy_unknown") or 0)
                count = int(row.get("reinforce_count") or 0)
            else:
                raw = _strength.legacy_initial(int(r.get("used") or 0))
                last = float(r.get("last_used") or 0.0)
                legacy = 1
                count = 0
            out[eid] = {
                "raw": raw, "last": last, "legacy": legacy, "count": count,
                "effective": round(_strength.effective_strength(raw, last, now), 6),
            }
        return out

    def strength_of(self, ep_id: int, now: Optional[float] = None) -> dict:
        """一条记忆此刻的强化强度；只读，不结算。"""
        now = now or _now()
        with self._lock:
            row = self._db.execute(
                "SELECT id,ts,used,last_used FROM episodes WHERE id=?",
                (int(ep_id),)).fetchone()
        if not row:
            return {"ok": False, "reason": "not_found"}
        sm = self._strength_map([dict(row)], now)[int(ep_id)]
        return {"ok": True, "ep_id": int(ep_id), **sm,
                "explain": _strength.explain(sm["raw"], sm["last"], now)}

    def _reinforce_hits(self, hits: list[dict], now: float) -> int:
        """真实 recall 结算：used 加一，强度按年龄/上次强化结算。

        只在 `search(reinforce=True)` 里调用。只读预览、打开识海、写日记不进来。
        """
        if not hits:
            return 0
        sm = self._strength_map(hits, now)
        changed = 0
        with self._lock:
            for h in hits:
                eid = int(h["id"])
                cur = sm.get(eid) or {}
                raw = float(cur.get("raw") or 0.0)
                last = float(cur.get("last") or 0.0)
                step = _strength.next_strength(float(h["ts"]), raw, last, now)
                after = float(step["after"])
                count = int(cur.get("count") or 0) + 1
                legacy = int(cur.get("legacy") or 0)
                self._db.execute(
                    "INSERT INTO memory_strength(ep_id,strength,last_reinforced,"
                    "reinforce_count,legacy_unknown,updated_at) "
                    "VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(ep_id) DO UPDATE SET "
                    "strength=excluded.strength,last_reinforced=excluded.last_reinforced,"
                    "reinforce_count=memory_strength.reinforce_count+1,"
                    "updated_at=excluded.updated_at",
                    (eid, after, now, count, legacy, now),
                )
                self._db.execute(
                    "UPDATE episodes SET used=used+1,last_used=? WHERE id=?",
                    (now, eid))
                # 回忆不是免费的：细节不足的记忆每被想起一次，都是重新拼一次。
                # P4 不因强化把 recon 抬高；这条旧规则原样保留。
                if float(h.get("q_recon") or 1.0) < 0.55:
                    self._db.execute(
                        "UPDATE episodes SET recon=MAX(0.25, recon-0.01) WHERE id=?",
                        (eid,))
                h["strength"] = round(after, 3)
                h["strength_before"] = round(float(cur.get("effective") or 0.0), 3)
                h["strength_next"] = round(after, 3)
                changed += 1
            if changed:
                self._db.commit()
        return changed

    def _active_regions(self) -> dict[str, int]:
        """当前有可用记忆的区。空区不参与路由，也不拖慢候选查询。"""
        with self._lock:
            rows = self._db.execute(
                "SELECT region, COUNT(*) c FROM episodes "
                "WHERE status='active' AND stage!='imprint' GROUP BY region"
            ).fetchall()
        out: dict[str, int] = {}
        for r in rows:
            region = normalize_region(r["region"])
            out[region] = out.get(region, 0) + int(r["c"])
        return out

    def route_regions(self, query: str, max_regions: int = 2) -> list[str]:
        """规则路由：这一句先去哪几个区找。

        - 只有一个区有记忆时直接进它，不因为一句「今天」就漏掉；
        - 多个区时按词面线索打分，最多进两个；
        - 一个线索都没命中时返回空列表，交给 search() 回退最近 20 条，
          不回退全表。工作记忆在 flow，不在这里。
        """
        q = (query or "").strip()
        active = self._active_regions()
        if not active:
            return []
        if len(active) == 1:
            return list(active)[:max(1, int(max_regions))]
        scores = {region: 0 for region in active}
        for region, cues in _ROUTE_CUES.items():
            if region not in scores:
                continue
            scores[region] = sum(1 for cue in cues if cue in q)
        ranked = sorted(((score, region) for region, score in scores.items() if score > 0),
                        key=lambda item: (-item[0], REGIONS.index(item[1])))
        return [region for _, region in ranked[:max(1, int(max_regions))]]

    def _rows_for_search(self, qgrams, max_scan: int,
                         regions: Optional[list[str]],
                         candidate_limit: Optional[int]) -> list[dict]:
        """取候选记忆行。这里是「先取候选、再算 R」的唯一入口。

        regions=None  ：老接口，等价于旧版扫描最近 max_scan 条，不变行为。
        regions=[]    ：路由没命中，回退最近 20 条（仍受 active/stage 约束）。
        regions=[...] ：只在这些区里取，候选上限默认 200。
        """
        if max_scan <= 0:
            return []
        limit = int(candidate_limit) if candidate_limit else 0
        with self._lock:
            if regions is not None and not regions:
                rows = self._db.execute(
                    f"SELECT {_SEARCH_COLS} FROM episodes e "
                    "WHERE e.status='active' AND e.stage!='imprint' "
                    "ORDER BY e.ts DESC LIMIT 20"
                ).fetchall()
            else:
                grams = sorted(qgrams)
                if not grams:
                    return []
                qg_values = ",".join("(?)" for _ in grams)
                region_sql = ""
                params: list = [int(max_scan), *grams]
                if regions:
                    region_sql = " AND e.region IN (" + ",".join("?" * len(regions)) + ")"
                    params.extend(regions)
                limit_sql = ""
                if limit > 0:
                    limit_sql = " LIMIT ?"
                    params.append(limit)
                ids = [int(r["id"]) for r in self._db.execute(
                    f"""
                    WITH recent AS (
                        SELECT id FROM episodes ORDER BY ts DESC LIMIT ?
                    ),
                    qg(gram) AS (VALUES {qg_values}),
                    hits AS (
                        SELECT g.ep_id, COUNT(*) n
                        FROM episode_grams g
                        JOIN qg ON qg.gram=g.gram
                        JOIN recent rr ON rr.id=g.ep_id
                        GROUP BY g.ep_id
                    )
                    SELECT e.id FROM episodes e
                    JOIN recent r ON r.id=e.id
                    LEFT JOIN hits h ON h.ep_id=e.id
                    WHERE e.status='active' AND e.stage!='imprint'
                      {region_sql}
                      AND (COALESCE(h.n,0) > 0 OR e.imp >= 0.75)
                    ORDER BY COALESCE(h.n,0) DESC, e.ts DESC{limit_sql}
                    """,
                    params,
                )]
                if not ids:
                    return []
                marks = ",".join("?" * len(ids))
                rows = self._db.execute(
                    f"SELECT {_SEARCH_COLS} FROM episodes e WHERE e.id IN ({marks})",
                    ids,
                ).fetchall()
                by_id = {int(r["id"]): dict(r) for r in rows}
                return [by_id[i] for i in ids if i in by_id]
        return [dict(r) for r in rows]

    def facts_block(self, limit: int = 14) -> str:
        """当前事实进提示：有版本链的 key 只认 status='current'。

        旧 facts 表没有版本链，仍按原样保留；但一个 key 只要在
        fact_versions 里出现过，就绝不再回落旧表——否则 superseded /
        disputed / retracted 会被旧值重新召回。
        """
        limit = max(1, int(limit))
        current = self.current_facts(limit=limit)
        with self._lock:
            versioned = {r["fact_key"] for r in self._db.execute(
                "SELECT DISTINCT fact_key FROM fact_versions")}
            if versioned:
                marks = ",".join("?" * len(versioned))
                legacy = self._db.execute(
                    f"SELECT key,value,conf FROM facts WHERE conf>=0.55 "
                    f"AND key NOT IN ({marks}) "
                    f"ORDER BY conf DESC, updated DESC LIMIT ?",
                    (*sorted(versioned), limit),
                ).fetchall()
            else:
                legacy = self._db.execute(
                    "SELECT key,value,conf FROM facts WHERE conf>=0.55 "
                    "ORDER BY conf DESC, updated DESC LIMIT ?", (limit,)
                ).fetchall()
        lines = [f"- {r['fact_key']}：{r['value']}" for r in current
                 if r.get("value") is not None]
        lines += [f"- {r['key']}：{r['value']}" for r in legacy]
        return "\n".join(lines[:limit])

    def search(self, query: str, k: int = 4, max_scan: int = 4000,
               focus: float = 0.6, now_emo: str = "",
               now_feel: Optional[dict] = None,
               regions: Optional[Iterable[str]] = None,
               candidate_limit: Optional[int] = None,
               reinforce: bool = False) -> list[dict]:
        """竞争式召回。返回按激活值排序的 episodes，空列表表示这次没什么好提的。

        `reinforce=False` 是默认：只读预览、打开识海、写日记、调试都不改变
        她的记忆。只有真实对话 turn 才传 `True`，由 `Memory` 结算 used 与强度。

        激活值不是单纯的「像不像」，是五项相乘：
            内容匹配 × 人物关系权重 × 情绪关联 × 时间因素 × 当前注意资源
        再加上意义强度那一项兜底——很重要的事，哪怕这次没提到关键词，也该浮上来。

        focus 是注意力余量（0..1）。她累的时候只有最要紧的几件事能被想起来，
        这也顺便省了算力：同样的查询，累的时候召回更少，而不是更快。

        检索顺序改成两步，但 R 六项公式、权重、阈值一个没动：
            1. 先用 episode_grams 倒排表和 region 取出候选行；
            2. 只对候选行跑原来的 R 打分。
        regions=None 是旧接口，仍然等价于扫最近 max_scan 条；给路由用时传
        route_regions() 的结果，最多 200 条候选，没命中就回退最近 20 条。
        """
        qg = _grams(query)
        if not qg:
            return []
        with self._lock:
            n = int(self._db.execute(
                "SELECT COUNT(*) c FROM episodes").fetchone()["c"] or 0)
        if n <= 0:
            return []
        # 只查查询词的词频；候选取出来以后，再补候选词自己的词频给干扰项。
        df_q = self._query_df(qg)
        qidf = {g: math.log(1 + n / (1 + df_q.get(g, 0))) for g in qg}
        qnorm = sum(qidf.values()) or 1.0
        now = _now()
        focus = max(0.0, min(1.0, focus))
        att = 0.70 + 0.60 * focus

        regions_arg: Optional[list[str]] = None
        if regions is not None:
            if isinstance(regions, str):
                raw_regions = [regions]
            else:
                raw_regions = list(regions)
            regions_arg = [normalize_region(r) for r in raw_regions]
        if regions_arg is None:
            cap = candidate_limit
        elif not regions_arg:
            cap = 20                 # 路由没命中：最近 20 条，不回退全表
        else:
            cap = int(candidate_limit or 200)
        rows = self._rows_for_search(qg, max_scan, regions_arg, cap)
        if not rows:
            return []
        sm = self._strength_map(rows, now)
        for r in rows:
            r["g"] = _grams(r["text"])
        # 干扰项需要候选自己的词频；只查这批候选的并集，不读全库。
        all_grams: set[str] = set()
        for r in rows:
            all_grams.update(r["g"])
        df = self._query_df(all_grams)
        scored = []
        for r in rows:
            # 已经撤回 / 被纠正取代的旧记录不再参与召回。原文还留在库里做证据，
            # 但不许它再从记忆里冒出来——视频里那句“纠正、撤回后的旧信息失效”。
            if (r.get("status") or "active") != "active":
                continue
            # 只剩痕迹的记忆不再作为「文字」被想起：她说不出经过，只会带着那股劲。
            # 它仍然活着——通过 ambient() 影响此刻，见下面的 residue。
            if (r.get("stage") or "raw") == "imprint":
                continue
            hit = qg & r["g"]
            age = max(0.0, now - r["ts"]) / 86400.0
            s_now = meaning_now(float(r["imp"]), age)
            rec = math.exp(-age / _RECENT_DAYS)
            if hit:
                cov = len(hit) / len(qg)                       # 覆盖了多少查询词
                idfw = sum(qidf[g] for g in hit) / qnorm        # 命中的词有多值钱
            else:
                cov = idfw = 0.0
            emo_i = float(r.get("emo_i") or 0.0)
            interf = self._interference(r["g"], df=df)
            # 小凌的 R：意义、情绪残留、当前线索、最近发生、被回忆次数、干扰，
            # 六项一起决定「现在有多容易被想起来」。原来的五项是它的简化版。
            base = 1.05 * cov + 0.45 * idfw + 0.30 * s_now + 0.22 * rec
            base += 0.10 * emo_i - 0.12 * interf
            # P4：可及性不再看 used 的次方，而看随时间衰减的 b_eff。
            # 旧记录没有旁表时，_strength_map 只按 used 给 <=0.35 的起点；
            # 新记录从 0 起步，第一次真实想起才加一点。
            b_eff = float(sm.get(int(r["id"]), {}).get("effective") or 0.0)
            access = _clamp01(b_eff)
            base += 0.06 * access
            if base < 0.22:
                continue
            # 没命中关键词的东西，只有特别重要的才允许自己浮上来。
            # 不设这一条，问「今天几号」都会被「他答应带我去看海」挤进来。
            if not hit and float(r["imp"]) < 0.75:
                continue
            rel = float(r.get("rel") or 0.3)
            # 情绪关联不是“标签相同 +1、不同 +0.6”这么粗：记忆里存的是一小片情绪场，
            # 此刻也是一小片。两片重叠越多，越容易被同一股情绪勾起来（视频的“情绪关联”）。
            feel_same = 0.0
            try:
                rec_feel = json.loads(r.get("feel") or "{}")
            except Exception:
                rec_feel = {}
            if now_feel and isinstance(rec_feel, dict) and rec_feel:
                denom = max(1e-6, sum(float(v) for v in rec_feel.values()))
                overlap = sum(min(float(v), float(now_feel.get(k, 0.0)))
                              for k, v in rec_feel.items() if k in now_feel)
                feel_same = min(1.0, overlap / denom)
            elif now_emo:
                # 老记忆还没有 feel 列时，退回单标签比较；不能因为缺新字段就把旧事压低。
                feel_same = 1.0 if r.get("emo") == now_emo else 0.0
            else:
                feel_same = 0.6
            mod = (0.75 + 0.50 * rel) * (0.85 + 0.30 * emo_i * (0.35 + 0.65 * feel_same)) * att
            act = base * mod
            if act >= _MIN_SCORE:
                scored.append((act, r, interf, access, feel_same))
        scored.sort(key=lambda x: -x[0])
        out = []
        for act, r, interf, access, feel_same in scored[:k]:
            age = max(0.0, now - r["ts"]) / 86400.0
            d0 = float(r.get("detail") or 1.0)
            d_now = detail_now(d0, age, float(r["imp"]),
                               int(r["used"] or 0), float(r.get("emo_i") or 0.0),
                               interf, strength=access)
            frags = fragments_of(d0, d_now)
            q = recon_now(d0, d_now, frags, int(r["used"] or 0))
            s_now = meaning_now(float(r["imp"]), age)
            R = _clamp01(0.55 * min(1.0, act / 1.8) + 0.25 * s_now
                         + 0.10 * float(r.get("emo_i") or 0.0) + 0.10 * access - 0.15 * interf)
            out.append({
                **r, "region": normalize_region(r.get("region")),
                "act": round(act, 3), "R": round(R, 3),
                "detail_now": round(d_now, 3), "meaning_now": round(s_now, 3),
                "fragments": frags, "q_recon": q, "interference": round(interf, 3),
                "feel_congruence": round(float(feel_same), 3),
                "strength": round(access, 3),
                "strength_before": round(access, 3),
                "residue": residue_now(float(r.get("emo_i") or 0.0), age, float(r["imp"])),
                "imprint": r.get("imprint") or "",
                "imprint_a": round(float(r.get("imprint_a") or 0.0), 3),
            })
        # 只有真实 turn 才结算。只读预览不进这里，`used` 和强度一个字节都不动。
        if out and reinforce:
            self._reinforce_hits(out, now)
        return out

    def neighbors(self, ep_id: int | str, limit: int = 2,
                  now: Optional[float] = None) -> list[dict]:
        """顺着一条记忆看见相邻的几条：同一人 / 同一场景 / 共用词。

        小凌那句「讲起一件会勾起另一件」在工程上就是这一步。
        只读：不加 used、不改 recon——扩散不是回忆，不收费，也不留下痕迹。
        """
        try:
            eid = int(str(ep_id).replace("mem:", "").strip())
        except (TypeError, ValueError):
            return []
        rows = self._load_cache()
        me = next((r for r in rows if int(r["id"]) == eid), None)
        if me is None or not me.get("g") or (me.get("status") or "active") != "active":
            return []
        now = now or _now()
        out: list[dict] = []
        for r in rows:
            if int(r["id"]) == eid or not r.get("g"):
                continue
            if (r.get("status") or "active") != "active":
                continue
            shared = me["g"] & r["g"]
            same_who = bool(me.get("who")) and me.get("who") == r.get("who")
            if not shared and not same_who:
                continue
            age = max(0.0, now - float(r["ts"])) / 86400.0
            imp = float(r["imp"])
            s = 0.45 * (len(shared) / max(1, len(me["g"]))) + 0.22 * meaning_now(imp, age)
            why = "共用词"
            if same_who:
                s = max(s, 0.50)
                why = "同一个人"
            if me.get("scene") and me.get("scene") == r.get("scene"):
                s += 0.12
                why = "同一场景"
            if me.get("day") == r.get("day"):
                s += 0.06
            out.append({"id": int(r["id"]), "text": r["text"],
                        "who": r.get("who") or "",
                        "emo": r.get("emo") or "", "emo_i": float(r.get("emo_i") or 0.0),
                        "imp": imp, "rel": float(r.get("rel") or 0.3),
                        "ident": float(r.get("ident") or 0.3),
                        "day": r.get("day"), "scene": r.get("scene") or "",
                        "spread": round(min(1.0, s), 3), "why": why})
        out.sort(key=lambda x: -x["spread"])
        return out[:limit]

    def ambient(self, now: Optional[float] = None, limit: int = 400) -> dict:
        """残响：此刻还在改她、但她已经想不起是哪件事的那些东西。

        「忘掉经历不等于抹掉经历造成的改变」——原文掉光以后，
        情绪残留（I）和留下的痕迹（imprint）还在参与评价。
        这些不是被想起的内容，所以不进「你记得」，只进语气和那点说不清的感觉。
        """
        now = now or _now()
        rows = self._load_cache()[:limit]
        tone_num = tone_den = 0.0
        imprints: dict[str, dict] = {}
        feel_acc: dict[str, float] = {}
        weights: list[float] = []
        sources = 0
        unknown = 0
        seen_ids: set[int] = set()
        for r in rows:
            if (r.get("status") or "active") != "active":
                continue
            # 同一 episode.id 只算一次：重复副本不放大残响。
            try:
                eid = int(r.get("id") or 0)
            except (TypeError, ValueError):
                eid = 0
            if eid:
                if eid in seen_ids:
                    continue
                seen_ids.add(eid)
            age = max(0.0, now - float(r["ts"])) / 86400.0
            imp = float(r["imp"])
            d0 = float(r.get("detail") or 1.0)
            emo_i = float(r.get("emo_i") or 0.0)
            interf = self._interference(r.get("g") or set())
            d_now = detail_now(d0, age, imp, int(r["used"] or 0), emo_i, interf)
            res = residue_now(emo_i, age, imp)
            if res < 0.08:
                continue
            # 越是想不起来（细节低），越是以「说不清」的方式影响她
            w = res * (0.45 + 0.55 * imp) * (1.0 + 0.8 * (1.0 - d_now))
            weights.append(w)
            tone_num += valence_of(r.get("emo") or "") * w
            tone_den += w
            sources += 1
            # 情绪残留不是一根标尺：当年是委屈、是生气还是被接住过，
            # 留下的颜色不一样。这里按同一份权重把它们叠成一小片情绪场。
            try:
                rec_feel = json.loads(r.get("feel") or "{}")
            except Exception:
                rec_feel = {}
            if not isinstance(rec_feel, dict):
                rec_feel = {}
            if not rec_feel:
                lab = str(r.get("emo") or "")
                rec_feel = {lab: emo_i} if lab else {}
            for k, v in rec_feel.items():
                if k in _affect.EMOTIONS:
                    feel_acc[k] = feel_acc.get(k, 0.0) + float(v) * w
            if d_now < 0.25 and res >= 0.35:
                unknown += 1
            line = (r.get("imprint") or "").strip()
            a = float(r.get("imprint_a") or 0.0)
            if line and a > 0 and d_now < 0.75:
                strength = a * (0.35 + 0.65 * (1.0 - d_now))
                cur = imprints.get(line)
                if cur is None or strength > cur["strength"]:
                    imprints[line] = {"text": line, "strength": round(strength, 3)}
        tone = round(tone_num / tone_den, 3) if tone_den else 0.0
        # 强度不再拿权重和除以常数：1 条和 30 条不会都顶到 1.0。
        parts = ambient_parts(weights, feel_acc)
        intensity = round(float(parts["intensity"]), 3)
        # 归一化到 0..1：残响只需要「哪几股感觉在」，不需要原始权重
        feel: dict[str, float] = {}
        if feel_acc and intensity > 0:
            top_v = max(feel_acc.values())
            for k, v in feel_acc.items():
                feel[k] = round(_clamp01(v / top_v) * _clamp01(intensity + 0.2), 3)
            feel = _affect.normalize(feel)
        tops = sorted(imprints.values(), key=lambda x: -x["strength"])[:3]
        if tone < -0.12 and intensity >= 0.20:
            text = "你心里有一层说不清的不安，找不到是哪件事。"
        elif tone > 0.12 and intensity >= 0.20:
            text = "你心里有一点暖的底子，想不起是从哪来的。"
        else:
            text = ""
        return {"tone": tone, "intensity": intensity, "sources": sources,
                "unknown": unknown, "imprints": tops, "text": text,
                "feel": feel, "feel_text": _affect.text(feel) if feel else "",
                "present": bool(weights)}

    def _interference(self, grams: set[str],
                      df: Optional[dict[str, int]] = None) -> float:
        """I 干扰：这条记忆和库里多少东西共用同一批词。

        共用得越多，越容易在回忆时串味。search 传候选词频进来；
        ambient/settle 仍用 _load_cache 算出的全库词频，不改旧路径。
        """
        if not grams:
            return 0.0
        freq = self._df if df is None else df
        common = sum(1 for g in grams if freq.get(g, 0) >= 3)
        return interference_of(common / len(grams))

    def episodes_block(self, query: str, k: int = 4, focus: float = 0.6,
                       now_emo: str = "", now_feel: Optional[dict] = None) -> str:
        hits = self.search(query, k=k, focus=focus, now_emo=now_emo, now_feel=now_feel)
        if not hits:
            return ""
        lines = []
        now_dt = temporal.now_local()
        for h in hits:
            bits: list[str] = []
            # Q_recon 低 = 这段记忆现在有一半是重新拼出来的。她自己不该装作记得每个字，
            # 只说「我记得你那天离开了，后来具体怎样我只能拼个大概」。
            q = float(h.get("q_recon") or 1.0)
            if q < 0.60:
                bits.append("后来具体只能拼个大概，我不确定")
            elif h["detail_now"] < 0.5 or float(h.get("recon") or 1.0) < 0.6:
                bits.append("细节记不太清了")
            frags = int(h.get("fragments") or 0)
            if 0 < frags <= 8:
                bits.append(f"大约只剩 {frags} 个片段")
            stage = h.get("stage") or "raw"
            if stage == "meaning":
                bits.append("只记得当时的感觉")
            elif stage == "segment":
                bits.append("只剩几个片段")
            # 忘掉记忆不等于失去记忆带来的改变：细节快没了、情绪还在的，
            # 要点明「留下的改变」，而不是让她以为这件事已经过去了。
            res_i = float(h.get("residue") or 0.0)
            if h["detail_now"] < 0.28 and res_i >= 0.40:
                bits.append("细节几乎没了，但它还留在你身上")
            imp_txt = h.get("imprint") or ""
            if imp_txt and h["detail_now"] < 0.55 and float(h.get("imprint_a") or 0) >= 0.35:
                bits.append(f"它留下的是：{imp_txt}")
            # 记忆的归属：这句话是什么性质，决定她能不能拿它当事实说
            claim = h.get("claim") or "fact"
            if claim == "wish":
                bits.append("这是他的愿望，不是已经发生的事")
            elif claim == "quote":
                bits.append("这是转述，不是她亲眼所见")
            elif claim == "guess":
                bits.append("这是当时的推测，未必准")
            # 来源与归属：她得知道这句话是谁说的、关于谁，不能把“他说的”过成“我经历的”。
            src = str(h.get("source") or "").strip()
            about = str(h.get("about") or "").strip()
            if src and src not in ("他说的",):
                bits.append(f"来源是{src}")
            if about and about not in ("他", ""):
                bits.append(f"关于{about}")
            hedge = "（" + "；".join(bits) + "）" if bits else ""
            stamp = (temporal.day_stamp(float(h["ts"]), now_dt)
                     if h.get("ts") else h.get("day", ""))
            lines.append(f"- {stamp}：{h['text']}{hedge}")
        return "\n".join(lines)

    def recent_episodes(self, n: int = 3) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT id,ts,day,kind,who,text,imp,source,about,status "
                "FROM episodes WHERE status='active' ORDER BY ts DESC LIMIT ?",
                (n,),
            ).fetchall()
        return [dict(r) for r in rows]

    def recent_emotional(self, n: int = 12) -> list[dict]:
        """最近这些记忆里，哪些还带着情绪重量。只读，供本机判断用。

        它和 search() 不一样：不按关键词找、不加 used、不改 recon——
        自主学习只用它决定「我最近在琢磨什么」，不把它当回忆播放。
        返回的 text 只在本机判断里用；调用方不得把原文发到网上，
        发出去的搜索词只允许是情绪/通用主题词（见 app/autonomy.py）。
        """
        rows = self._load_cache()[:max(1, int(n))]
        out: list[dict] = []
        for r in rows:
            if (r.get("status") or "active") != "active":
                continue
            out.append({
                "id": int(r["id"]),
                "day": r.get("day") or "",
                "ts": float(r.get("ts") or 0.0),
                "text": r.get("text") or "",
                "emo": r.get("emo") or "",
                "emo_i": float(r.get("emo_i") or 0.0),
                "imp": float(r.get("imp") or 0.0),
                "detail": float(r.get("detail") or 1.0),
                "stage": r.get("stage") or "raw",
                "claim": r.get("claim") or "fact",
                "imprint": r.get("imprint") or "",
                "imprint_a": float(r.get("imprint_a") or 0.0),
            })
        return out

    def stats(self) -> dict:
        with self._lock:
            t = self._db.execute("SELECT COUNT(*) c FROM turns").fetchone()["c"]
            f = self._db.execute("SELECT COUNT(*) c FROM facts").fetchone()["c"]
            e = self._db.execute("SELECT COUNT(*) c FROM episodes").fetchone()["c"]
            fv = self._db.execute(
                "SELECT COUNT(*) c FROM fact_versions").fetchone()["c"]
            ec = self._db.execute(
                "SELECT COUNT(*) c FROM event_cards").fetchone()["c"]
            fb = self._db.execute(
                "SELECT COUNT(*) c FROM feedback_events").fetchone()["c"]
            ms = self._db.execute(
                "SELECT COUNT(*) c FROM memory_strength").fetchone()["c"]
            mc = self._db.execute(
                "SELECT COUNT(*) c FROM meaning_candidates").fetchone()["c"]
            ch = self._db.execute(
                "SELECT COUNT(*) c FROM chapters").fetchone()["c"]
            cm = self._db.execute(
                "SELECT COUNT(*) c FROM meaning_candidates "
                "WHERE status='confirmed'").fetchone()["c"]
            co = self._db.execute(
                "SELECT COUNT(*) c FROM chapters WHERE status='open'").fetchone()["c"]
            pv = self._db.execute(
                "SELECT COUNT(*) c FROM persona_versions").fetchone()["c"]
            pa = self._db.execute(
                "SELECT COUNT(*) c FROM persona_versions "
                "WHERE status='active'").fetchone()["c"]
            pc = self._db.execute(
                "SELECT COUNT(*) c FROM persona_versions "
                "WHERE status='candidate'").fetchone()["c"]
            first = self._db.execute("SELECT MIN(ts) m FROM episodes").fetchone()["m"]
            active = self._db.execute(
                "SELECT COUNT(*) c FROM episodes WHERE status='active'").fetchone()["c"]
            retracted = self._db.execute(
                "SELECT COUNT(*) c FROM episodes WHERE status='retracted'").fetchone()["c"]
            superseded = self._db.execute(
                "SELECT COUNT(*) c FROM episodes WHERE status='superseded'").fetchone()["c"]
            pending = self._db.execute(
                "SELECT COUNT(*) c FROM episodes WHERE stage='raw' AND status='active'"
            ).fetchone()["c"]
            deep = self._db.execute(
                "SELECT COUNT(*) c FROM episodes WHERE stage IN ('segment','meaning','deep') "
                "AND status='active'").fetchone()["c"]
            stages = {r["stage"]: int(r["c"]) for r in self._db.execute(
                "SELECT stage, COUNT(*) c FROM episodes WHERE status='active' GROUP BY stage")}
            imprint = int(stages.get("imprint", 0))
            regions: dict[str, int] = {}
            for r in self._db.execute(
                    "SELECT region, COUNT(*) c FROM episodes GROUP BY region"):
                key = normalize_region(r["region"])
                regions[key] = regions.get(key, 0) + int(r["c"])
            grams = int(self._db.execute(
                "SELECT COUNT(*) c FROM episode_grams").fetchone()["c"] or 0)
            version = int(self._db.execute("PRAGMA user_version").fetchone()[0] or 0)
        return {"turns": t, "facts": f, "episodes": e, "active": active,
                "fact_versions": fv, "event_cards": ec, "feedback_events": fb,
                "memory_strength": ms,
                "meaning_candidates": mc, "chapters": ch,
                "confirmed_meanings": cm, "open_chapters": co,
                "persona_versions": pv, "active_personas": pa,
                "candidate_personas": pc,
                "retracted": retracted, "superseded": superseded,
                "pending": pending, "deep": deep,
                "imprint": imprint, "stages": stages,
                "regions": regions, "grams": grams, "schema_version": version,
                "since": _day(first) if first else None,
                "db_kb": round(self.path.stat().st_size / 1024, 1) if self.path.exists() else 0}

    def settle(self, keep_days: float = 3.0, now: Optional[float] = None) -> dict:
        """记忆生命周期：完整经历 → 情景记忆 → 意义与情绪残留。

        小凌的原话是「忘记经历不等于抹掉经历造成的改变」——所以这里只推进
        stage 标记，不删原文、不改内容。原文留着是为了可核对，不是为了天天
        复述；stage 决定她提这件事时该留多少余地（见 episodes_block）。
        """
        now = now or _now()
        moved = {"segment": 0, "meaning": 0, "imprint": 0}
        gram_of = {r["id"]: r["g"] for r in self._load_cache()}
        with self._lock:
            rows = [dict(r) for r in self._db.execute(
                "SELECT id,ts,imp,used,last_used,detail,emo_i,text,emo,claim,"
                "imprint,imprint_a FROM episodes WHERE stage='raw' AND status='active'"
            ).fetchall()]
            sm = self._strength_map(rows, now)
            for r in rows:
                age = max(0.0, now - float(r["ts"])) / 86400.0
                if age < keep_days:        # 场景缓冲：最近几天的还算「正在经历」
                    continue
                interf = self._interference(gram_of.get(int(r["id"]), set()))
                b_eff = float((sm.get(int(r["id"])) or {}).get("effective") or 0.0)
                d = detail_now(float(r["detail"] or 1.0), age,
                               float(r["imp"]), int(r["used"] or 0),
                               float(r["emo_i"] or 0.0), interf, strength=b_eff)
                # 细节掉到几乎没有、又过了很久，就只剩「留下的改变」——
                # 原文不删，只退出被想起的资格（ambient 仍然带着它）。
                if age >= 365 * 6 and d < 0.155:
                    target = "imprint"
                elif age >= 60 and d < 0.35:
                    target = "meaning"
                elif d < 0.55:
                    target = "segment"
                else:
                    continue
                if not (r["imprint"] or "").strip():
                    line, a = imprint_of(r["text"] or "", r["emo"] or "",
                                         r["claim"] or "fact")
                    if line:
                        self._db.execute(
                            "UPDATE episodes SET imprint=?,imprint_a=? WHERE id=?",
                            (line[:80], float(a), r["id"]))
                self._db.execute("UPDATE episodes SET stage=? WHERE id=?",
                                 (target, r["id"]))
                moved[target] += 1
            self._db.commit()
            if sum(moved.values()):
                self._cache = None
            counts = {}
            for st in ("raw", "segment", "meaning", "imprint"):
                counts[st] = int(self._db.execute(
                    "SELECT COUNT(*) c FROM episodes WHERE stage=?", (st,)
                ).fetchone()["c"])
        return {**counts, "moved": sum(moved.values()), "moved_detail": moved}

    # ---- 撤回 / 纠正 ------------------------------------------------------
    def retract(self, target: int | str, reason: str = "", limit: int = 10, *,
                kind: str = "manual", source_kind: str = "manual",
                turn_id: int | None = None, source_text: str = "",
                source_ref: str = "") -> dict:
        """把一条或几条记忆标成失效，不删原文。

        视频原话是「纠正、撤回后的旧信息失效」「明确纠正和要求删除的内容必须及时处理」。
        所以这里不是 DELETE：原文留着可核对，但不再参与召回、扩散、残响和星图。
        第四轮起，每次撤回同时写一条 feedback_event，能回到原话/ turn / 目标；
        事件与撤回在同一个事务里，写不进事件就不撤回。
        """
        if isinstance(target, int):
            rows = self._db.execute(
                "SELECT id,text FROM episodes WHERE id=? AND status='active'",
                (int(target),)).fetchall()
        else:
            needle = (target or "").strip()
            if not needle:
                return {"ok": False, "retracted": 0, "ids": [], "reason": "没有指定要撤回的内容"}
            rows = self._db.execute(
                "SELECT id,text FROM episodes WHERE status='active' AND text LIKE ? "
                "ORDER BY ts DESC LIMIT ?", (f"%{needle}%", int(limit))).fetchall()
        ids = [int(r["id"]) for r in rows]
        texts = [r["text"] for r in rows]
        if not ids:
            return {"ok": True, "retracted": 0, "ids": [], "reason": "没有找到可撤回的记录"}
        now = _now()
        feedback_ids: list[str] = []
        with self._lock:
            try:
                for eid in ids:
                    self._db.execute(
                        "UPDATE episodes SET status='retracted', retracted_at=?, "
                        "retract_reason=? WHERE id=?",
                        (now, (reason or "用户纠正/要求删除")[:80], eid))
                # 派生意义与章节先级联检查：原事件失效，章节不能再当成仍然有效。
                self._cascade_derived(ids, deleted=False,
                                      reason="来源事件已被撤回")
                for eid, text in zip(ids, texts):
                    feedback_ids.append(self._record_feedback(
                        kind=kind, target_id=eid, target_text=text,
                        action="retracted", source_kind=source_kind,
                        turn_id=turn_id, source_text=source_text,
                        source_ref=source_ref, reason=reason,
                        legacy_unknown=0,
                    ))
                self._db.commit()
                self._cache = None
            except sqlite3.Error as exc:
                self._db.rollback()
                return {"ok": False, "retracted": 0, "ids": [],
                        "reason": f"feedback_failed: {exc}"}
        return {"ok": True, "retracted": len(ids), "ids": ids,
                "texts": texts, "reason": reason,
                "feedback_ids": feedback_ids}

    def supersede(self, old_id: int, new_text: str, reason: str = "", *,
                  feedback_kind: str = "", source_kind: str = "manual",
                  source_text: str = "", source_ref: str = "",
                  turn_id: int | None = None, **kwargs) -> int:
        """用一条新记录取代旧记录：旧记录标 superseded，新记录照常 active。

        纠正不是抹掉历史，而是让“我现在依据哪一版”有据可查。
        传 feedback_kind 时，同一事务里写一条 feedback_event，指向新旧两条。
        """
        new_id = self.add_episode(new_text, **kwargs)
        if new_id:
            with self._lock:
                try:
                    old = self._db.execute(
                        "SELECT text FROM episodes WHERE id=?", (int(old_id),)
                    ).fetchone()
                    cur = self._db.execute(
                        "UPDATE episodes SET status='superseded', superseded_by=?, "
                        "retracted_at=?, retract_reason=? WHERE id=? AND status='active'",
                        (int(new_id), _now(), (reason or "被更正的记录取代")[:80],
                         int(old_id)))
                    if cur.rowcount:
                        self._cascade_derived([int(old_id)], deleted=False,
                                              reason="来源事件已被更正取代")
                    if feedback_kind and cur.rowcount:
                        self._record_feedback(
                            kind=feedback_kind, target_id=int(old_id),
                            target_text=(old["text"] if old else ""),
                            action="superseded", source_kind=source_kind,
                            turn_id=turn_id, source_text=source_text,
                            source_ref=source_ref, replacement_id=int(new_id),
                            reason=reason, legacy_unknown=0,
                        )
                    self._db.commit()
                    self._cache = None
                except sqlite3.Error:
                    self._db.rollback()
                    return 0
        return new_id

    def link_supersede(self, old_ids: list[int], new_id: int,
                       reason: str = "") -> int:
        """把已经撤回的旧记录再连到“取代它的新记录”上，方便核对因果。"""
        ids = [int(x) for x in (old_ids or []) if int(x) > 0]
        if not ids or not new_id:
            return 0
        with self._lock:
            marks = ",".join("?" * len(ids))
            self._db.execute(
                f"UPDATE episodes SET status='superseded', superseded_by=?, "
                f"retracted_at=?, retract_reason=? WHERE id IN ({marks})",
                (int(new_id), _now(), (reason or "被新的更正记录取代")[:80], *ids))
            self._cascade_derived(ids, deleted=False,
                                  reason="来源事件已被新的更正记录取代")
            # 同一批反馈事件补上“被谁取代”；不新增第二条事件，避免一次纠正记两笔。
            self._db.execute(
                f"UPDATE feedback_events SET replacement_id=?, action='superseded' "
                f"WHERE target_id IN ({marks}) AND replacement_id IS NULL",
                (str(int(new_id)), *[str(i) for i in ids]))
            self._db.commit()
            self._cache = None
        return len(ids)

    def note_correction(self, text: str, limit: int = 1, *,
                        turn_id: int | None = None,
                        source_ref: str = "") -> dict:
        """从一句用户话里识别纠正/删除，把目标标记失效。

        只在有明确说法时动手：说错了 / 更正 / 删掉 / 忘掉 / 别提……
        普通陈述里的“不是”不触发。取不到对象时只撤回最近一条 active 记录；
        这不是完美语义理解，但足够把“说错了”这类明确纠正立刻落地。
        第四轮起会带上 turn_id，反馈事件能指回这句话本身。
        """
        kind = correction_kind(text)
        if not kind:
            return {"ok": False, "kind": "", "retracted": 0, "ids": [],
                    "feedback_ids": []}
        rows = self._db.execute(
            "SELECT id,text FROM episodes WHERE status='active' ORDER BY ts DESC LIMIT 6"
        ).fetchall()
        if not rows:
            return {"ok": True, "kind": kind, "retracted": 0, "ids": [],
                    "feedback_ids": [], "reason": "库里没有可撤回的记录"}
        target = None
        if kind == "delete":
            obj = _correction_object(text)
            if obj:
                for r in rows:
                    if obj in (r["text"] or ""):
                        target = r
                        break
        if target is None:
            target = rows[0]
        res = self.retract(
            int(target["id"]),
            reason=("用户明确要求删除" if kind == "delete"
                    else "用户说之前记错了，要求更正"),
            kind=kind, source_kind="user_turn", turn_id=turn_id,
            source_text=text, source_ref=source_ref,
        )
        return {"ok": bool(res.get("ok")), "kind": kind,
                "retracted": int(res.get("retracted") or 0),
                "ids": res.get("ids") or [],
                "feedback_ids": res.get("feedback_ids") or [],
                "target": target["text"][:60],
                "reason": res.get("reason", "")}

    # ---- 删 ---------------------------------------------------------------
    def forget(self, needle: str) -> dict:
        """按子串删 facts 和 episodes。返回删了几条。"""
        needle = needle.strip()
        if not needle:
            return {"facts": 0, "episodes": 0}
        with self._lock:
            f = self._db.execute(
                "DELETE FROM facts WHERE key LIKE ? OR value LIKE ?",
                (f"%{needle}%", f"%{needle}%"),
            ).rowcount
            dead = self._db.execute(
                "SELECT gram,COUNT(*) n FROM episode_grams WHERE ep_id IN "
                "(SELECT id FROM episodes WHERE text LIKE ?) GROUP BY gram",
                (f"%{needle}%",),
            ).fetchall()
            for row in dead:
                self._db.execute(
                    "UPDATE gram_df SET cnt=MAX(0,cnt-?) WHERE gram=?",
                    (int(row["n"]), row["gram"]),
                )
                self._db.execute("DELETE FROM gram_df WHERE cnt<=0")
            self._db.execute(
                "DELETE FROM episode_grams WHERE ep_id IN "
                "(SELECT id FROM episodes WHERE text LIKE ?)",
                (f"%{needle}%",),
            )
            self._db.execute(
                "DELETE FROM memory_strength WHERE ep_id IN "
                "(SELECT id FROM episodes WHERE text LIKE ?)",
                (f"%{needle}%",),
            )
            dead_ids = [int(r["id"]) for r in self._db.execute(
                "SELECT id FROM episodes WHERE text LIKE ?", (f"%{needle}%",)
            ).fetchall()]
            if dead_ids:
                self._cascade_derived(dead_ids, deleted=True,
                                      reason="来源事件被删除")
            e = self._db.execute(
                "DELETE FROM episodes WHERE text LIKE ?", (f"%{needle}%",)
            ).rowcount
            self._db.commit()
            self._cache = None
            self._df = {}
        return {"facts": f, "episodes": e}

    def close(self) -> None:
        with self._lock:
            try:
                self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                self._db.close()
            except Exception:
                pass


# --- 从未话里抽事实 ---------------------------------------------------------
# 只认窄句式。宁可漏，不要记错：记错一条「他讨厌猫」，后面每次都要带着走。
_PATTERNS: list[tuple[str, str, float]] = [
    (r"我(?:的名字)?叫\s*([\u4e00-\u9fffA-Za-z]{2,8})", "名字", 0.95),
    (r"我今年\s*(\d{1,2})\s*(?:岁|周岁)", "年龄", 0.9),
    (r"我(?:的)?生日(?:是)?\s*([0-9一二三四五六七八九十]{1,4}\s*月\s*[0-9一二三四五六七八九十]{1,4}\s*[日号])", "生日", 0.9),
    (r"我(?:住在|家在)\s*([\u4e00-\u9fff]{2,12})", "住在", 0.75),
    (r"我(?:在|读)([\u4e00-\u9fff]{2,10}(?:大学|学院|学校))(?:读|上学|念书)?", "学校", 0.7),
    (r"我(?:学的是|读的是|专业是)\s*([\u4e00-\u9fff]{2,10})", "专业", 0.8),
    (r"我(?:不喜欢|讨厌|受不了)\s*([^，。！？,.!?]{1,16})", "不喜欢", 0.8),
    (r"我(?:喜欢|最爱|爱吃)\s*([^，。！？,.!?]{1,16})", "喜欢", 0.8),
    (r"我(?:怕|害怕|最怕)\s*([^，。！？,.!?]{1,16})", "怕", 0.85),
]


def extract_facts(text: str) -> list[tuple[str, str, float]]:
    out = []
    for pat, key, conf in _PATTERNS:
        m = re.search(pat, text or "")
        if m:
            val = m.group(1).strip()
            if val and val not in ("你", "他", "她"):
                out.append((key, val, conf))
    return out


def absorb(mem: Memory, user_text: str) -> list[str]:
    """把一轮用户话里的可记内容写进库。返回写进去的 key，方便她回一句「记住了」。"""
    got = []
    for key, val, conf in extract_facts(user_text):
        mem.add_fact(key, val, conf=conf, source="对话")
        got.append(f"{key}：{val}")

    t = (user_text or "").strip()
    # 明确让他记：优先级最高，整句留原话
    for cue in ("记住", "记一下", "别忘了"):
        if cue in t:
            i = t.find(cue)
            body = t[i + len(cue):].lstrip("，,：: 我你")
            if body:
                mem.add_episode(body, kind="note", imp=0.95,
                                claim=claim_of(body),
                                source="他要求记住的", about="他")
                got.append(f"记下：{body[:24]}")
            break
    return got
