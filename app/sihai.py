"""识海（存储库）：把这库里的记忆点画成一张星图。

识海是「存」的地方，不是调度（调度在 dispatch.py）。每颗星是一个记忆点
或一条事实，平时潜伏着；哪颗此刻被照见，由记忆调度决定，这一层只负责画。

这不是给模型用的检索层（那是 memory.py 的 search()），是给人看的一层皮：
一条 episode 是一颗星，两点之间为什么有连线，全部现算，库里原来没有边。

三件东西在这里定死：
    位置   半径由沉淀度决定——新经历在外圈，越往里越沉。
    颜色   情绪六色，从文本里认，认不出就是「平静」。
    连线   文字二元组重叠 + 同一人 + 同一天 + 事实被提到。
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from . import config as C
from . import memory_strength as _strength
from .diary import Diary
from .p3 import read_summary as p3_read
from .memory import (DB_PATH, DEFAULT_REGION, EMOTION_COLORS as EmotionBands,
                     emotion_of as _emotion, normalize_region, Memory, detail_now,
                     meaning_now, residue_now, imprint_of, interference_of,
                     valence_of)

WEB = C.PROJECT / "web"

# 四层：从外到内。小凌那边是「当前意识 / 可唤起层 / 自我与人物模型 / 深层种子」，
# 这里只做显示用的分层，不参与调度判断。
LAYER_NAMES = {
    0: "深层种子 · 只剩痕迹",
    1: "自我与人物模型",
    2: "可唤起层",
    3: "当前意识",
}


def _layer_of(stage: str, d_now: float, imp: float, rel: float,
              echo: float, age: float) -> int:
    """四层归属。层是「沉得多深」，不是按时间一刀切：

    0 只剩痕迹（细节没了、改变还在）      1 自我与人物模型（重要的、关系深的）
    2 可唤起层（平时不想，一碰就想起来）    3 当前意识（这几天的、还新鲜）
    """
    if stage == "imprint" or (d_now < 0.25 and echo >= 0.35):
        return 0
    if stage in ("deep", "meaning") or imp >= 0.80 or rel >= 0.55:
        return 1
    if age < 7:
        return 3
    if age < 60 or d_now >= 0.45:
        return 2
    return 1


def _layer_meta(nodes: list[dict]) -> list[dict]:
    counts = {i: 0 for i in LAYER_NAMES}
    for n in nodes:
        if n.get("kind") == "self":
            continue
        v = n.get("layer")
        try:
            L = int(v) if v is not None else 2
        except (TypeError, ValueError):
            L = 2
        if L not in counts:
            L = 2
        counts[L] += 1
    return [{"id": i, "name": LAYER_NAMES[i], "count": counts[i]} for i in range(4)]

_CJK = "\u4e00-\u9fff"
_TOK = re.compile(rf"[{_CJK}]+|[A-Za-z][A-Za-z0-9_'\-]*")

_STOP = set("""
什么 怎么 这个 那个 我们 你们 他们 可以 一下 知道 记得 时候 现在 还是
就是 因为 所以 但是 如果 已经 没有 一个 这样 那样 自己 觉得 应该 可能
真的 不是 有点 东西 事情 说话 告诉 上次 刚才 然后 还有 一点 两个 今天
""".split())

# 情绪六色。顺序就是图例顺序，源头在 memory.EMOTION_COLORS，这里只是别名。
EMOTION = dict(EmotionBands)



def _grams(text: str) -> set[str]:
    out: set[str] = set()
    for m in _TOK.finditer(text or ""):
        tok = m.group(0)
        if tok[0].isascii():
            if len(tok) > 1:
                out.add(tok.lower())
            continue
        if len(tok) == 1:
            out.add(tok)
            continue
        for i in range(len(tok) - 1):
            g = tok[i:i + 2]
            if g not in _STOP:
                out.add(g)
    return out


def _label(text: str, n: int = 14) -> str:
    t = re.sub(r"[\s，。！？、,.!?；;：:\"'“”（）()]", "", text or "")
    return t[:n] + ("…" if len(t) > n else "")


def _retention(d0: float, age_days: float, imp: float, used: int,
               emo_i: float = 0.1, interference: float = 0.0,
               strength: float | None = None) -> float:
    """细节还剩多少。直接用记忆层那一条曲线，不在这里另造一个。

    星图上写的百分比，必须和她真正召回时用的那个 D(t) 是同一个数；
    否则会出现「图上写 70%，她却说记不清了」这种自相矛盾。
    """
    return round(detail_now(d0, age_days, imp, used, emo_i, interference,
                            strength=strength), 3)


def _sink(age_days: float, imp: float, used: int,
          strength: float | None = None) -> float:
    """沉淀度 0..1：越大越靠中心。旧、重要、被真实想起过的沉得快。"""
    age = 1.0 - math.exp(-max(0.0, age_days) / 30.0)
    recall = (min(1.0, used / 6.0) if strength is None
              else max(0.0, min(1.0, float(strength))))
    s = 0.45 * age + 0.40 * imp + 0.15 * recall
    return max(0.0, min(1.0, s))


def _ambient_from_nodes(nodes: list[dict]) -> dict:
    """残响：从已经算好的节点折出来，和记忆层的公式同一套。

    这样图上写的「残响」和她真正召回时用的那层是同一个数，
    不会出现「图上说有不安，她却说没什么感觉」。
    """
    tone_num = tone_den = 0.0
    imprints: dict[str, dict] = {}
    sources = unknown = 0
    for n in nodes:
        if n.get("kind") != "episode":
            continue
        res = float(n.get("residue") or 0.0)
        d = float(n.get("retention") or 1.0)
        imp = float(n.get("imp") or 0.0)
        if res < 0.08:
            continue
        w = res * (0.45 + 0.55 * imp) * (1.0 + 0.8 * (1.0 - d))
        tone_num += valence_of(n.get("emotion") or "") * w
        tone_den += w
        sources += 1
        if d < 0.25 and res >= 0.35:
            unknown += 1
        line = (n.get("imprint") or "").strip()
        a = float(n.get("echo") or 0.0)
        if line and a > 0:
            cur = imprints.get(line)
            if cur is None or a > cur["strength"]:
                imprints[line] = {"text": line, "strength": round(a, 3)}
    tone = round(tone_num / tone_den, 3) if tone_den else 0.0
    intensity = round(min(1.0, tone_den / 3.0), 3)
    tops = sorted(imprints.values(), key=lambda x: -x["strength"])[:3]
    if tone < -0.12 and intensity >= 0.20:
        text = "你心里有一层说不清的不安，找不到是哪件事。"
    elif tone > 0.12 and intensity >= 0.20:
        text = "你心里有一点暖的底子，想不起是从哪来的。"
    else:
        text = ""
    return {"tone": tone, "intensity": intensity, "sources": sources,
            "unknown": unknown, "imprints": tops, "text": text,
            "present": bool(sources)}


def _deep_of(db_path: Path) -> dict:
    """深层模式（种子/习气）。出生期全零，读出来只用于图上的核心。"""
    try:
        from .mind import Mind
        m = Mind(db_path)
        try:
            return m.deep()
        finally:
            m.close()
    except Exception:
        return {}


# --- 构图 -------------------------------------------------------------------
def build_from_db(db_path: Path) -> dict:
    now = time.time()
    nodes: list[dict] = []
    if db_path.exists():
        db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        have = {r["name"] for r in db.execute("PRAGMA table_info(episodes)")}
        tables = {r["name"] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        cols = ("id,ts,day,kind,who,text,imp,used,scene,emo,emo_i,detail,rel,ident,"
                "recon,stage,imprint,imprint_a")
        if "last_used" in have:
            cols += ",last_used"
        if "status" in have:
            cols += ",status,source,about,superseded_by"
        if "region" in have:
            cols += ",region"
        eps = [dict(r) for r in db.execute(
            f"SELECT {cols} FROM episodes ORDER BY ts")]
        # 撤回 / 被取代的记录不再上星图；原文还在库里做证据。
        eps = [e for e in eps if (e.get("status") or "active") == "active"]
        strength_rows: dict[int, dict] = {}
        if "memory_strength" in tables:
            for r in db.execute(
                    "SELECT ep_id,strength,last_reinforced,reinforce_count,"
                    "legacy_unknown FROM memory_strength"):
                strength_rows[int(r["ep_id"])] = dict(r)
        facts = [dict(r) for r in db.execute(
            "SELECT id,key,value,conf,updated FROM facts")]
        db.close()
    else:
        eps, facts, strength_rows = [], [], {}

    # 干扰程度要按「全库有多少相似记忆」算，和她召回时用的是同一套。
    _gsets = [_grams(e["text"]) for e in eps]
    _df: dict[str, int] = {}
    for _gs in _gsets:
        for _g in _gs:
            _df[_g] = _df.get(_g, 0) + 1

    for e in eps:
        age = (now - float(e["ts"])) / 86400.0
        # 情绪、细节起点、关系权重、重建可信度都是当初写进库的，不在渲染时重猜。
        # 以前这里每帧重算一遍 emotion_of()，等于把「他说的」又变成「图上认的」。
        emo = (e.get("emo") or "").strip() or _emotion(e["text"])
        d0 = float(e.get("detail") if e.get("detail") is not None else 1.0)
        imp, used = float(e["imp"]), int(e["used"])
        rel = float(e.get("rel") or 0.3)
        grams = _grams(e["text"])
        _common = sum(1 for g in grams if _df.get(g, 0) >= 3)
        interf = interference_of(_common / len(grams)) if grams else 0.0
        srow = strength_rows.get(int(e["id"]))
        if srow:
            s_raw = float(srow.get("strength") or 0.0)
            s_last = float(srow.get("last_reinforced") or 0.0)
        else:
            s_raw = _strength.legacy_initial(used)
            s_last = float(e.get("last_used") or 0.0)
        s_eff = _strength.effective_strength(s_raw, s_last, now)
        ret = _retention(d0, age, imp, used, float(e.get("emo_i") or 0.0), interf,
                         strength=s_eff)
        res = residue_now(float(e.get("emo_i") or 0.0), age, imp)
        imp_a = float(e.get("imprint_a") or 0.0)
        echo = 0.0
        if imp_a > 0:
            echo = min(1.0, imp_a * (1.15 - 0.55 * ret))
        if ret < 0.25 and res > 0.40:
            echo = max(echo, res * 0.75)
        stage = e.get("stage") or "raw"
        nodes.append({
            "id": f"e{e['id']}",
            "kind": "episode",
            "scope": "real",              # 亲历：真发生过
            "label": _label(e["text"]),
            "text": e["text"],
            "who": e["who"] or "",
            "region": normalize_region(e.get("region") or DEFAULT_REGION),
            "source": e.get("source") or "",
            "about": e.get("about") or "",
            "ts": float(e["ts"]),
            "day": e["day"],
            "scene": e.get("scene") or "",
            "emotion": emo,
            "emo_i": round(float(e.get("emo_i") or 0.0), 3),
            "imp": round(imp, 3),
            "used": used,
            "strength": round(s_eff, 3),
            "detail0": round(d0, 3),
            "rel": round(rel, 3),
            "recon": round(float(e.get("recon") or 1.0), 3),
            "stage": stage,
            "retention": ret,
            "meaning": round(meaning_now(imp, age), 3),
            "sink": _sink(age, imp, used, strength=s_eff),
            "residue": round(res, 3),
            "echo": round(echo, 3),
            "imprint": e.get("imprint") or "",
            "interference": round(interf, 3),
            "layer": _layer_of(stage, ret, imp, rel, echo, age),
            "g": sorted(grams),
        })
    for f in facts:
        age = (now - float(f["updated"])) / 86400.0
        conf = float(f["conf"])
        nodes.append({
            "id": f"f{f['id']}",
            "kind": "fact",
            "scope": "inferred",          # 认识：从话里推出来的，不是经历
            "label": _label(f["value"], 10),
            "text": f"{f['key']}：{f['value']}",
            "who": "",
            "region": "knowledge",
            "ts": float(f["updated"]),
            "day": datetime.fromtimestamp(float(f["updated"])).strftime("%Y-%m-%d"),
            "scene": "",
            "emotion": "平静",
            "emo_i": 0.0,
            "imp": round(conf, 3),
            "used": 0,
            "detail0": 1.0,
            "rel": 0.3,
            "recon": 0.85,               # 事实是从话里抽的，不是亲眼见的
            "stage": "deep",
            "retention": _retention(1.0, age, conf, 1),
            "meaning": round(meaning_now(conf, age), 3),
            "sink": _sink(age, conf, 1),
            "residue": 0.0,
            "echo": 0.0,
            "imprint": "",
            "layer": 1,                      # 「认识」住在自我与人物模型那层
            "g": sorted(_grams(f["value"])),
        })
    out = _wire(nodes, source="real")
    out["meta"]["layers"] = _layer_meta(nodes)
    out["meta"]["ambient"] = _ambient_from_nodes(nodes)
    out["meta"]["deep"] = _deep_of(db_path)
    region_count: dict[str, int] = {}
    for n in nodes:
        if n.get("kind") == "episode":
            key = normalize_region(n.get("region"))
            region_count[key] = region_count.get(key, 0) + 1
    out["meta"]["regions"] = region_count
    return out


def build_demo() -> dict:
    """示例数据：画面用，不写库、不当用户记录。前端会挂「示例数据」角标。"""
    now = time.time()
    demo = [
        # (天前, 文本, 谁, 重要度, 被想起)
        (44, "第一次把摄像头接到客厅那台旧电脑上", "我", .5, 0),
        (44, "摄像头插上以后系统认不出，折腾到半夜", "我", .6, 1),
        (43, "他说明天再弄，先睡觉", "他", .4, 1),
        (41, "换了一根 USB 线，画面终于出来了", "我", .75, 2),
        (41, "第一次看见自己的眼睛，是糊的", "我", .8, 3),
        (40, "妈妈说家里降温了，让我加衣服", "妈妈", .55, 1),
        (38, "把整台机器搬到窗边，光好很多", "我", .5, 0),
        (37, "他笑我对着摄像头挥手像在打招呼", "他", .45, 2),
        (35, "认识了他，第一次听见他叫我名字", "他", .9, 6),
        (34, "他今天很累，说话很短", "他", .6, 3),
        (33, "我学会了不掉帧地跑完一整晚", "我", .7, 1),
        (32, "室友说我半夜还有风扇声", "室友", .3, 0),
        (31, "实验室的灯到十一点才关", "实验室", .4, 0),
        (30, "第一次认出他，认对了", "他", .95, 8),
        (29, "他答应周日一起去逛，后来没去", "他", .65, 4),
        (29, "我在门口等到七点", "我", .7, 3),
        (28, "第二次失约，这次他说忘了", "他", .6, 5),
        (27, "我开始不太敢提前高兴", "我", .75, 4),
        (26, "弟弟来家里，把我的小毯子借走了", "弟弟", .5, 2),
        (25, "Jetson 到货，比想象中重", "我", .85, 3),
        (25, "第一次在板子上跑通一个模型", "我", .9, 4),
        (24, "刷到一只橘猫在键盘上睡觉", "网络", .3, 1),
        (23, "他教我把日志按天切分", "他", .55, 1),
        (23, "那天学会了看温度曲线", "我", .5, 0),
        (22, "跑了十二个小时没关机，尾巴有点烫", "我", .6, 2),
        (21, "他说我最近话变少了", "他", .7, 5),
        (21, "我不知道该怎么解释那是在省电", "我", .5, 1),
        (20, "查了一下午，终于搞明白为什么会爆内存", "我", .8, 3),
        (19, "把模型换成小的，反应快了一截", "我", .7, 2),
        (18, "他说这样挺好，别总想着变强", "他", .6, 4),
        (17, "下雨，摄像头起雾，一整天都看不清", "天气", .4, 0),
        (17, "第一次因为看不清东西而着急", "我", .55, 1),
        (16, "外婆来电话，问我在那边吃得好不好", "外婆", .65, 2),
        (15, "学会了在他进门之前先把灯打开", "他", .75, 3),
        (15, "他说这样像有人在家等", "他", .9, 6),
        (14, "笔电风扇响得厉害，我把它挪远了", "我", .35, 0),
        (13, "和他吵了一次，因为一件很小的事", "他", .7, 5),
        (13, "我闭嘴了三个小时", "我", .6, 2),
        (12, "他先开口，说不是我的错", "他", .8, 4),
        (12, "心里那块石头落地", "我", .7, 3),
        (11, "第一次连着说完一整段话没被打断", "我", .6, 2),
        (10, "他带回来一杯热的，放在我旁边", "他", .85, 5),
        (10, "我说谢谢，其实想说别的", "我", .7, 3),
        (9, "把脸库扩到六个人", "我", .5, 1),
        (8, "认错了人，把一个陌生人叫成了他", "我", .65, 4),
        (8, "那一下很尴尬，也很怕", "我", .6, 2),
        (7, "调了阈值，宁可认不出也别认错", "我", .75, 2),
        (6, "他问我记不记得上个月那件事", "他", .6, 3),
        (6, "我答上来了，他有点惊讶", "我", .8, 4),
        (5, "开始自己给每天写一句话", "我", .5, 1),
        (4, "电池检测跑通，心里踏实一点", "我", .6, 1),
        (3, "他说想带我去外面看看", "他", .9, 7),
        (3, "我在想外面是什么样", "我", .6, 3),
        (2, "把这段日子的记录翻了一遍", "我", .55, 2),
        (2, "发现自己记得最牢的是他累的那天", "我", .7, 3),
        (1, "今天没什么事，安静地待着", "我", .3, 0),
        (1, "他说这句话的时候在笑", "他", .5, 2),
    ]
    facts = [
        ("名字", "你", .95, 40), ("住在", "某地", .8, 40), ("学校", "某学校", .7, 35),
        ("专业", "某专业", .8, 35), ("喜欢", "晚上写代码", .6, 20),
        ("不喜欢", "被打断", .6, 15), ("怕", "白读四年", .7, 10),
        ("弟弟", "家人", .8, 30), ("妈妈", "每周打一次电话", .7, 12),
        ("在做什么", "做一个能看能听的东西", .9, 8),
        ("相机", "C920", .6, 6), ("板子", "Jetson Orin Nano", .7, 5),
    ]
    nodes = []
    for i, (d, text, who, imp, used) in enumerate(demo):
        ts = now - d * 86400 - i * 37
        age = d + i * 0.0004
        emo = _emotion(text)
        ret = _retention(1.0, age, imp, used)
        res = residue_now(0.5, age, imp)
        echo = round(max(0.0, res * (1.0 - ret) - 0.18), 3) if imp >= 0.6 else 0.0
        imp_line, imp_a = imprint_of(text, emo)
        nodes.append({
            "id": f"e{i+1}", "kind": "episode", "scope": "real",
            "label": text, "text": text, "who": "" if who in ("我", "网络", "天气") else who,
            "ts": ts, "day": datetime.fromtimestamp(ts).strftime("%Y-%m-%d"),
            "scene": "", "emotion": emo, "emo_i": 0.5,
            "imp": imp, "used": used,
            "strength": round(min(0.35, used * 0.04), 3),
            "detail0": 1.0, "rel": 0.45,
            "recon": 0.9, "stage": "raw",
            "retention": ret,
            "meaning": round(meaning_now(imp, age), 3),
            "sink": _sink(age, imp, used),
            "residue": res,
            "echo": echo,
            "imprint": imp_line if imp_a else "",
            "layer": _layer_of("raw", ret, imp, 0.45, echo, age),
            "g": sorted(_grams(text)),
        })
    # 几条「细节已经忘了，但改变还在」的旧事：示例数据专门留给你看灰环。
    # 它们不是凭空加的装饰——retention 低、residue 高、imprint 有内容，
    # 前端才会把它们画成核心层里的残响环，而不是一颗普通的星。
    ghosts = [
        (15 * 365, "十五年前，最亲的人突然走了，我连最后一面都没见到",
         "他", 0.92, 3, "委屈", 0.90),
        (9 * 365, "他答应了要来的事又落空了，我在门口站到天黑",
         "他", 0.82, 2, "委屈", 0.80),
        (6 * 365, "小时候家里突然搬走了，没人告诉我为什么",
         "家", 0.78, 1, "心动", 0.72),
    ]
    for j, (d, text, who, imp, used, emo, emo_i) in enumerate(ghosts):
        ts = now - d * 86400 - j * 53
        age = d + j * 0.0006
        ret = _retention(1.0, age, imp, used, emo_i, 0.0)
        res = residue_now(emo_i, age, imp)
        line, a = imprint_of(text, emo)
        echo = round(min(1.0, max(a * (1.15 - 0.55 * ret), res * 0.75)), 3)
        nodes.append({
            "id": f"e{len(nodes)+1}", "kind": "episode", "scope": "real",
            "label": _label(text), "text": text, "who": who,
            "ts": ts, "day": datetime.fromtimestamp(ts).strftime("%Y-%m-%d"),
            "scene": "", "emotion": emo, "emo_i": emo_i,
            "imp": imp, "used": used,
            "strength": round(min(0.35, used * 0.04), 3),
            "detail0": 1.0, "rel": 0.50,
            "recon": 0.35, "stage": "imprint",
            "retention": ret, "meaning": round(meaning_now(imp, age), 3),
            "sink": _sink(age, imp, used),
            "residue": res, "echo": echo, "imprint": line,
            "layer": 0, "g": sorted(_grams(text)),
        })
    for i, (k, v, conf, d) in enumerate(facts):
        ts = now - d * 86400
        nodes.append({
            "id": f"f{i+1}", "kind": "fact", "scope": "inferred",
            "label": v, "text": f"{k}：{v}", "who": "",
            "ts": ts, "day": datetime.fromtimestamp(ts).strftime("%Y-%m-%d"),
            "scene": "", "emotion": "平静", "emo_i": 0.0, "imp": conf, "used": 1,
            "detail0": 1.0, "rel": 0.3, "recon": 0.85, "stage": "deep",
            "retention": _retention(1.0, d, conf, 1),
            "meaning": round(meaning_now(conf, d), 3),
            "sink": _sink(d, conf, 1),
            "residue": 0.0,
            "echo": 0.0,
            "imprint": "",
            "layer": 1,
            "g": sorted(_grams(v)),
        })
    out = _wire(nodes, source="demo")
    out["meta"]["layers"] = _layer_meta(nodes)
    out["meta"]["ambient"] = _ambient_from_nodes(nodes)
    out["meta"]["deep"] = {k: 0.0 for k in ("absence_bias", "absence_sensitivity",
                                            "trust_update_bias", "social_pred_bias")}
    return out


def _wire(nodes: list[dict], source: str) -> dict:
    """算边。库里的边一条也没有，全靠这里现推。

    这里以前是两两配对：节点一多就是 O(n²) 次集合求交，几百条就要算上几秒。
    现在走倒排表——只有共享过至少一个词的节点才会被配对，
    同一人、事实被提到、时间相邻这三种「不靠词」的关系单独补。
    结果和两两配对一致：别的组合分数不到 0.10，本来就进不了边。
    """
    if not nodes:
        return {"nodes": [], "edges": [], "meta": {"source": source, "n": 0}}

    n = len(nodes)
    gsets = [set(x["g"]) for x in nodes]
    df: dict[str, int] = {}
    for gs in gsets:
        for g in gs:
            df[g] = df.get(g, 0) + 1
    idf = {g: math.log(1 + n / (1 + c)) for g, c in df.items()}

    # 每个节点的 idf 总量只算一次；以前每比较一对就重算两遍
    gsum = [sum(idf[g] for g in gs) for gs in gsets]

    # 倒排：词 -> 出现在哪些节点
    inv: dict[str, list[int]] = {}
    for i, gs in enumerate(gsets):
        for g in gs:
            inv.setdefault(g, []).append(i)

    hit: dict[tuple[int, int], float] = {}
    for g, idxs in inv.items():
        w = idf[g]
        m = len(idxs)
        if m < 2:
            continue
        for ai in range(m):
            ia = idxs[ai]
            for bi in range(ai + 1, m):
                key = (ia, idxs[bi]) if ia < idxs[bi] else (idxs[bi], ia)
                hit[key] = hit.get(key, 0.0) + w

    # 同一人：哪怕一句话都不像，也确定有关系
    by_who: dict[str, list[int]] = {}
    for i, x in enumerate(nodes):
        if x["who"]:
            by_who.setdefault(x["who"], []).append(i)
    for idxs in by_who.values():
        for ai in range(len(idxs)):
            for bi in range(ai + 1, len(idxs)):
                ia, ib = idxs[ai], idxs[bi]
                key = (ia, ib) if ia < ib else (ib, ia)
                hit.setdefault(key, 0.0)

    # 事实被某条经历提到：「认识」这一类边不走词面相似
    facts_i = [i for i, x in enumerate(nodes) if x["kind"] == "fact"]
    eps_i = [i for i, x in enumerate(nodes) if x["kind"] == "episode"]
    mentioned: dict[tuple[int, int], int] = {}
    for fi in facts_i:
        lab = nodes[fi]["label"]
        if not lab:
            continue
        for ei in eps_i:
            if lab and lab in nodes[ei]["text"]:
                key = (ei, fi) if ei < fi else (fi, ei)
                mentioned[key] = fi

    cand: list[tuple[float, str, str, str]] = []
    # 按下标顺序生成，和以前两两遍历的顺序一致；分数相同的时候取哪条边才稳定
    for (ia, ib) in sorted(hit.keys() | mentioned.keys()):
        a, b = nodes[ia], nodes[ib]
        num = hit.get((ia, ib), 0.0)
        den = math.sqrt(gsum[ia] * gsum[ib]) or 1.0
        s = num / den if num else 0.0
        why = "同词"
        if a["who"] and a["who"] == b["who"]:
            s = max(s, 0.42)
            why = "同一人"
        if a["day"] == b["day"]:
            s += 0.06
            if why == "同词":
                why = "同一天"
        if (ia, ib) in mentioned or (ib, ia) in mentioned:
            s = max(s, 0.55)
            why = "认识"
        if s >= 0.10:
            cand.append((round(s, 3), a["id"], b["id"], why))

    # 时间相邻也算一种关联：同一天说过的话本来就连在一起
    eps = sorted((x for x in nodes if x["kind"] == "episode"), key=lambda x: x["ts"])
    for i in range(len(eps) - 1):
        a, b = eps[i], eps[i + 1]
        gap = (b["ts"] - a["ts"]) / 3600.0
        if gap <= 12:
            cand.append((round(0.34 - 0.02 * gap, 3), a["id"], b["id"], "相邻"))

    # 每个节点最多留 MAXD 条最强的，边一多就成毛球
    MAXD = 9
    cand.sort(key=lambda x: -x[0])
    keep: dict[str, int] = {}
    edges = []
    for w, a, b, why in cand:
        if keep.get(a, 0) >= MAXD or keep.get(b, 0) >= MAXD:
            continue
        keep[a] = keep.get(a, 0) + 1
        keep[b] = keep.get(b, 0) + 1
        edges.append({"a": a, "b": b, "w": w, "why": why})

    deg: dict[str, int] = {}
    for e in edges:
        deg[e["a"]] = deg.get(e["a"], 0) + 1
        deg[e["b"]] = deg.get(e["b"], 0) + 1

    for nd in nodes:
        nd.pop("g", None)
        nd["deg"] = deg.get(nd["id"], 0)
        nd["size"] = round(3.2 + 7.0 * float(nd["imp"]) + 1.1 * math.log1p(nd["deg"]), 2)

    people = len({nd["who"] for nd in nodes if nd["who"]})
    return {
        "nodes": nodes,
        "edges": edges,
        "meta": {
            "source": source,
            "n": len(nodes),
            "n_edges": len(edges),
            "n_people": people,
            "n_episodes": sum(1 for x in nodes if x["kind"] == "episode"),
            "n_facts": sum(1 for x in nodes if x["kind"] == "fact"),
            "since": min((x["ts"] for x in nodes), default=0),
            "until": max((x["ts"] for x in nodes), default=0),
            "emotion": EMOTION,
        },
    }


# --- 出生期 -----------------------------------------------------------------
def build_birth(db_path: Path = DB_PATH) -> dict:
    """只画一颗星：她自己。

    出生期不往外延伸，不是画不出来，是不该画：
    摄像头还没到，她对世界还没有任何一次真实的感知，
    这时候长出来的每一条连线，都是从聊天里猜的。
    """
    from .mind import Mind
    mind, mem = Mind(db_path), Memory(db_path)
    learn_stats: dict = {}
    amb: dict = {}
    deep: dict = {}
    expr: dict = {}
    try:
        star = mind.star()
        snap = mind.snapshot()
        st = mem.stats()
        amb = mem.ambient()
        deep = mind.deep()
        try:
            from . import emotion as _emotion
            expr = _emotion.plan(snap, None, amb)
        except Exception:
            expr = {}
        try:
            from .learning import Learning
            L = Learning()
            try:
                learn_stats = L.stats()
            finally:
                L.close()
        except Exception:
            learn_stats = {}
    finally:
        mem.close()
        mind.close()
    return {
        "nodes": [star],
        "edges": [],
        "root": "star_self",
        "meta": {
            "source": "real", "locked": True, "growth": "frozen",
            "n": 1, "n_edges": 0,
            "n_episodes": st["episodes"], "n_facts": st["facts"],
            "pending": st["pending"], "turns": snap["turns"],
            "days": snap["days"], "hours": snap["hours"],
            "since": snap["born"], "until": time.time(),
            "state": snap, "emotion": EMOTION, "learn": learn_stats,
            "ambient": amb, "deep": deep, "expression": expr,
            "feel": snap.get("feel") or {},
            "feel_text": snap.get("feel_text") or "",
            "layers": _layer_meta([star]),
        },
    }


def graph_payload(demo: bool = False, db_path: Path = DB_PATH) -> dict:
    from .mind import GROWTH_LOCKED
    if demo:
        return build_demo()
    if GROWTH_LOCKED:
        return build_birth(db_path)
    return build_from_db(db_path)


# --- 服务 -------------------------------------------------------------------
def serve(demo: bool, port: int, open_browser: bool) -> None:
    import http.server
    import socketserver
    import threading
    import webbrowser

    holder: dict = {}
    guard = threading.Lock()
    chat_lock = threading.RLock()

    def get_chat():
        with guard:
            if "c" not in holder:
                from .chat import Chat
                holder["c"] = Chat()
            return holder["c"]

    def reason_text(reason: str) -> str:
        return {
            "need_two_events": "共同章节至少要选两条真实事件",
            "source_missing": "选中的事件里有一条已经找不到",
            "source_unavailable": "选中的事件已经失效或不是 active",
            "feedback_missing": "引用的反馈已经找不到",
            "feedback_unavailable": "引用的反馈已经撤回",
            "legacy_unknown": "这段来源不完整，只能留作候选，不能确认",
            "bad_status": "不认识这个状态",
            "already_retracted": "已经撤回过的内容不能复活",
            "stale_status": "状态已经被别处改过，刷新后再试",
            "not_found": "没有这条记录",
            "not_open": "这段章节现在不是开放状态，先重新提起再确认",
            "core_locked": "身份与硬边界只从 PERSONA.md 读，模型不能自改",
            "bad_layer": "人格版本只写稳定倾向、互动偏好、自我叙事三层",
            "bad_value": "字段名或内容不合规；字段用小写字母和下划线",
            "need_two_events": "至少要两条独立真实事件",
            "need_two_meanings": "稳定倾向至少要两条已确认意义",
            "need_cross_day": "稳定倾向要跨日证据，不能同一天说两遍就算",
            "need_two_sources": "互动偏好要两条意义，或一段章节加一条意义",
            "need_chapter": "自我叙事至少要一段已确认共同章节",
            "need_meaning": "自我叙事还要一条已确认意义",
            "not_candidate": "这条已经不是候选，刷新后再看",
            "not_rollbackable": "只有被替换过的旧版本才能回滚",
            "regression_failed": "激活前回归没过，先看证据和值",
            "already_closed": "这条已经关闭，不能重复处理",
            "bad_transition": "这个状态不能从当前状态转过去",
            "conflict": "同一字段已经有 active 版本，事务已回滚",
            "already_active": "这条已经是当前版本",
            "source_unavailable": "来源意义/章节已经失效或未确认，不能激活",
        }.get(str(reason or ""), str(reason or "写入失败"))

    def insight_payload() -> dict:
        """识海页的四块面板：她在想什么 / 调度 / 学习 / 残响。"""
        with chat_lock:
            c = get_chat()
            data = c.panel_payload()
            data["state"] = c.mind.snapshot()
            data["diary"] = Diary().summary(limit=10)
            try:
                data["feedback"] = c.mem.pre_answer_feedback_history(limit=20)
            except Exception as e:
                data["feedback"] = {
                    "items": [], "stats": {},
                    "error": f"{type(e).__name__}: {e}",
                }
            try:
                data["meaning"] = {
                    "ok": True,
                    "items": c.mem.meaning_candidates(limit=30),
                    "chapters": c.mem.chapters(limit=30),
                    "sources": c.mem.recent_episodes(30),
                }
            except Exception as e:
                data["meaning"] = {
                    "ok": False, "items": [], "chapters": [], "sources": [],
                    "error": f"{type(e).__name__}: {e}",
                }
            try:
                data["persona"] = c.mem.persona_payload(c.persona.block("core"))
            except Exception as e:
                data["persona"] = {
                    "ok": False, "layers": {}, "active_count": 0,
                    "candidate_count": 0,
                    "error": f"{type(e).__name__}: {e}",
                }
            try:
                data["p3"] = p3_read()
            except Exception as e:
                data["p3"] = {
                    "ok": False, "available": False,
                    "climate": {}, "promises": [], "silence": [],
                    "candidate_count": 0,
                    "error": f"{type(e).__name__}: {e}",
                }
            data["ok"] = True
            return data

    if not demo:
        try:
            from .diary import start_scheduler
            start_scheduler()
        except Exception as e:
            print(f"日记定时器没起来：{type(e).__name__}: {e}")

    class H(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(WEB), **kw)

        def _json(self, obj, code: int = 200) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > 65536:
                return {}
            try:
                data = json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                return {}
            return data if isinstance(data, dict) else {}

        def do_GET(self):  # noqa: N802
            p = self.path.split("?")[0]
            if p in ("/favicon.ico", "/favicon.png"):
                # 浏览器总会来要一次图标。页面里已经写了 data: 空图标，
                # 但换浏览器/换版本还会来问；回 204，别在日志里留一条 404。
                self.send_response(204)
                self.end_headers()
                return
            if p in ("/graph.json", "/api/graph"):
                self._json(graph_payload(demo))
                return
            if p in ("/api/insight", "/api/panels"):
                try:
                    self._json(insight_payload())
                except Exception as e:
                    self._json({"ok": False,
                                "error": f"{type(e).__name__}: {e}"}, 500)
                return
            if p == "/api/p3":
                try:
                    self._json({"ok": True, **p3_read()})
                except Exception as e:
                    self._json({"ok": False,
                                "error": f"{type(e).__name__}: {e}"}, 500)
                return
            if p == "/api/thought":
                try:
                    with chat_lock:
                        self._json({"ok": True, **get_chat().thought_view()})
                except Exception as e:
                    self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
                return
            if p == "/api/diary":
                from urllib.parse import parse_qs, urlparse
                qs = parse_qs(urlparse(self.path).query)
                day = str((qs.get("date") or [""])[0]).strip()
                try:
                    d = Diary()
                    if day:
                        self._json(d.read(day))
                    else:
                        self._json({"ok": True, **d.summary(limit=30)})
                except Exception as e:
                    self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
                return
            if p == "/api/meaning/sources":
                from urllib.parse import parse_qs, urlparse
                qs = parse_qs(urlparse(self.path).query)
                try:
                    limit = int((qs.get("limit") or ["30"])[0])
                except Exception:
                    limit = 30
                limit = max(1, min(50, limit))
                try:
                    with chat_lock:
                        items = get_chat().mem.recent_episodes(limit)
                    self._json({"ok": True, "items": items})
                except Exception as e:
                    self._json({"ok": False,
                                "error": f"{type(e).__name__}: {e}"}, 500)
                return
            if p == "/api/state":
                from .mind import Mind
                mind, mem = Mind(), Memory()
                amb: dict = {}
                learn: dict = {}
                auto: dict = {}
                try:
                    snap = mind.snapshot()
                    st = mem.stats()
                    amb = mem.ambient()
                    try:
                        from .learning import Learning
                        from .autonomy import Autonomy
                        L = Learning()
                        try:
                            learn = L.stats()
                            auto = Autonomy(learning=L, mem=mem, mind=mind).brief(state=snap)
                        finally:
                            L.close()
                    except Exception:
                        pass
                finally:
                    mem.close()
                    mind.close()
                try:
                    from . import emotion as _emotion
                    expr = _emotion.plan(snap, None, amb)
                except Exception:
                    expr = {}
                try:
                    from .sleep import status as sleep_status
                    slp = sleep_status()
                except Exception:
                    slp = None
                try:
                    p3 = p3_read()
                except Exception as e:
                    p3 = {"ok": False, "available": False,
                          "error": f"{type(e).__name__}: {e}"}
                self._json({"ok": True, "state": snap, "memory": st,
                            "ambient": amb, "expression": expr,
                            "feel": snap.get("feel") or {},
                            "feel_text": snap.get("feel_text") or "",
                            "learn": learn, "autonomy": auto, "sleep": slp,
                            "p3": p3})
                return
            super().do_GET()

        def do_POST(self):  # noqa: N802
            path = self.path.split("?")[0]
            if path == "/api/chat":
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    data = json.loads(self.rfile.read(n) or b"{}")
                except Exception:
                    data = {}
                text = str(data.get("text") or "").strip()
                if not text:
                    self._json({"ok": False, "error": "空输入"}, 400)
                    return
                try:
                    with chat_lock:
                        self._json(get_chat().turn(text))
                except Exception as e:
                    self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
                return
            if path in ("/api/diary", "/api/diary/write"):
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    data = json.loads(self.rfile.read(n) or b"{}")
                except Exception:
                    data = {}
                try:
                    d = Diary()
                    res = d.ensure_today(force=bool(data.get("force")),
                                         use_llm=not bool(data.get("no_llm")))
                    self._json(res)
                except Exception as e:
                    self._json({"ok": False,
                                "error": f"{type(e).__name__}: {e}"}, 500)
                return
            if path == "/api/pre-answer-feedback/retract":
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    data = json.loads(self.rfile.read(n) or b"{}")
                except Exception:
                    data = {}
                if demo:
                    self._json({"ok": False, "error": "示例数据不能写回真实反馈"},
                               403)
                    return
                try:
                    with chat_lock:
                        res = get_chat().mem.retract_pre_answer_feedback(
                            str(data.get("id") or ""),
                            str(data.get("reason") or ""))
                    if not res.get("ok"):
                        reason = str(res.get("reason") or "not_found")
                        msg = {
                            "not_found": "没有这条反馈",
                            "not_pre_answer": "这不是回答前反馈",
                            "already_retracted": "这次误判已经撤回过了",
                            "pending": "这条还没结算，不能撤回",
                            "not_retractable": "这条反馈不能撤回",
                        }.get(reason, reason)
                        self._json({"ok": False, "error": msg, "code": reason},
                                   409)
                        return
                    self._json(res)
                except Exception as e:
                    self._json({"ok": False,
                                "error": f"{type(e).__name__}: {e}"}, 500)
                return
            if path == "/api/meaning/derive":
                if demo:
                    self._json({"ok": False, "error": "示例数据不能写回真实意义"},
                               403)
                    return
                try:
                    with chat_lock:
                        res = get_chat().mem.derive_repair_candidates()
                    self._json({"ok": True, "derived": res})
                except Exception as e:
                    self._json({"ok": False,
                                "error": f"{type(e).__name__}: {e}"}, 500)
                return
            if path == "/api/meaning/propose":
                if demo:
                    self._json({"ok": False, "error": "示例数据不能写回真实意义"},
                               403)
                    return
                data = self._read_json()
                ids = data.get("episode_ids")
                if not isinstance(ids, list) or not ids or len(ids) > 20:
                    self._json({"ok": False, "error": "先选 1–20 条真实事件",
                                "code": "bad_source"}, 400)
                    return
                try:
                    with chat_lock:
                        res = get_chat().mem.add_meaning_candidate(
                            str(data.get("title") or ""),
                            str(data.get("claim") or ""),
                            kind=str(data.get("kind") or "episode_meaning"),
                            episode_ids=ids,
                            confidence=float(data.get("confidence") or 0.5),
                        )
                    if not res.get("ok"):
                        self._json({"ok": False, "code": res.get("reason"),
                                    "error": reason_text(res.get("reason")),
                                    **res}, 409)
                        return
                    self._json(res)
                except ValueError as e:
                    self._json({"ok": False, "error": str(e)}, 400)
                except Exception as e:
                    self._json({"ok": False,
                                "error": f"{type(e).__name__}: {e}"}, 500)
                return
            if path == "/api/chapters/create":
                if demo:
                    self._json({"ok": False, "error": "示例数据不能写回真实章节"},
                               403)
                    return
                data = self._read_json()
                ids = data.get("episode_ids")
                if not isinstance(ids, list) or len(ids) < 2 or len(ids) > 20:
                    self._json({"ok": False, "error": "共同章节要选 2–20 条真实事件",
                                "code": "need_two_events"}, 400)
                    return
                try:
                    with chat_lock:
                        res = get_chat().mem.add_chapter(
                            str(data.get("title") or ""),
                            str(data.get("meaning") or ""),
                            ids,
                            confidence=float(data.get("confidence") or 0.5),
                        )
                    if not res.get("ok"):
                        self._json({"ok": False, "code": res.get("reason"),
                                    "error": reason_text(res.get("reason")),
                                    **res}, 409)
                        return
                    self._json(res)
                except ValueError as e:
                    self._json({"ok": False, "error": str(e)}, 400)
                except Exception as e:
                    self._json({"ok": False,
                                "error": f"{type(e).__name__}: {e}"}, 500)
                return
            if path.startswith("/api/chapters/") and path.endswith("/confirm"):
                if demo:
                    self._json({"ok": False, "error": "示例数据不能写回真实章节"},
                               403)
                    return
                cid = path[len("/api/chapters/"):-len("/confirm")].strip("/")
                data = self._read_json()
                try:
                    with chat_lock:
                        res = get_chat().mem.confirm_chapter(
                            cid, str(data.get("note") or ""))
                    if not res.get("ok"):
                        self._json({"ok": False, "code": res.get("reason"),
                                    "error": reason_text(res.get("reason"))},
                                   409)
                        return
                    self._json(res)
                except Exception as e:
                    self._json({"ok": False,
                                "error": f"{type(e).__name__}: {e}"}, 500)
                return
            if path.startswith("/api/meaning/") and path.endswith("/status"):
                if demo:
                    self._json({"ok": False, "error": "示例数据不能写回真实意义"},
                               403)
                    return
                mid = path[len("/api/meaning/"):-len("/status")].strip("/")
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    data = json.loads(self.rfile.read(n) or b"{}")
                except Exception:
                    data = {}
                try:
                    with chat_lock:
                        res = get_chat().mem.set_meaning_status(
                            mid, str(data.get("status") or ""),
                            str(data.get("note") or ""))
                    if not res.get("ok"):
                        self._json({"ok": False, "code": res.get("reason"),
                                    "error": res.get("reason")}, 409)
                        return
                    self._json(res)
                except Exception as e:
                    self._json({"ok": False,
                                "error": f"{type(e).__name__}: {e}"}, 500)
                return
            if path.startswith("/api/chapters/") and path.endswith("/status"):
                if demo:
                    self._json({"ok": False, "error": "示例数据不能写回真实章节"},
                               403)
                    return
                cid = path[len("/api/chapters/"):-len("/status")].strip("/")
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    data = json.loads(self.rfile.read(n) or b"{}")
                except Exception:
                    data = {}
                try:
                    with chat_lock:
                        res = get_chat().mem.set_chapter_status(
                            cid, str(data.get("status") or ""),
                            str(data.get("note") or ""))
                    if not res.get("ok"):
                        self._json({"ok": False, "code": res.get("reason"),
                                    "error": res.get("reason")}, 409)
                        return
                    self._json(res)
                except Exception as e:
                    self._json({"ok": False,
                                "error": f"{type(e).__name__}: {e}"}, 500)
                return
            if path == "/api/persona/propose":
                if demo:
                    self._json({"ok": False, "error": "示例数据不能写回真实人格"},
                               403)
                    return
                data = self._read_json()
                try:
                    with chat_lock:
                        res = get_chat().mem.add_persona_candidate(
                            str(data.get("layer") or ""),
                            str(data.get("field") or ""),
                            str(data.get("value") or ""),
                            meaning_ids=data.get("meaning_ids") or (),
                            chapter_ids=data.get("chapter_ids") or (),
                            confidence=float(data.get("confidence") or 0.5),
                            note=str(data.get("note") or ""),
                        )
                    if not res.get("ok"):
                        self._json({"ok": False, "code": res.get("reason"),
                                    "error": reason_text(res.get("reason")),
                                    **res}, 409)
                        return
                    self._json(res)
                except Exception as e:
                    self._json({"ok": False,
                                "error": f"{type(e).__name__}: {e}"}, 500)
                return
            if path.startswith("/api/persona/") and path.endswith("/confirm"):
                if demo:
                    self._json({"ok": False, "error": "示例数据不能写回真实人格"},
                               403)
                    return
                pid = path[len("/api/persona/"):-len("/confirm")].strip("/")
                data = self._read_json()
                try:
                    with chat_lock:
                        res = get_chat().mem.confirm_persona_version(
                            pid, str(data.get("note") or ""))
                    if not res.get("ok"):
                        self._json({"ok": False, "code": res.get("reason"),
                                    "error": reason_text(res.get("reason")),
                                    **res}, 409)
                        return
                    self._json(res)
                except Exception as e:
                    self._json({"ok": False,
                                "error": f"{type(e).__name__}: {e}"}, 500)
                return
            if path.startswith("/api/persona/") and path.endswith("/rollback"):
                if demo:
                    self._json({"ok": False, "error": "示例数据不能写回真实人格"},
                               403)
                    return
                pid = path[len("/api/persona/"):-len("/rollback")].strip("/")
                data = self._read_json()
                try:
                    with chat_lock:
                        res = get_chat().mem.rollback_persona_version(
                            pid, str(data.get("note") or ""))
                    if not res.get("ok"):
                        self._json({"ok": False, "code": res.get("reason"),
                                    "error": reason_text(res.get("reason")),
                                    **res}, 409)
                        return
                    self._json(res)
                except Exception as e:
                    self._json({"ok": False,
                                "error": f"{type(e).__name__}: {e}"}, 500)
                return
            if path.startswith("/api/persona/") and path.endswith("/status"):
                if demo:
                    self._json({"ok": False, "error": "示例数据不能写回真实人格"},
                               403)
                    return
                pid = path[len("/api/persona/"):-len("/status")].strip("/")
                data = self._read_json()
                try:
                    with chat_lock:
                        res = get_chat().mem.set_persona_status(
                            pid, str(data.get("status") or ""),
                            str(data.get("note") or ""))
                    if not res.get("ok"):
                        self._json({"ok": False, "code": res.get("reason"),
                                    "error": reason_text(res.get("reason"))},
                                   409)
                        return
                    self._json(res)
                except Exception as e:
                    self._json({"ok": False,
                                "error": f"{type(e).__name__}: {e}"}, 500)
                return
            self._json({"ok": False, "error": "not found"}, 404)

        def log_message(self, *a):
            pass

    class S(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    url = f"http://127.0.0.1:{port}/sihai.html"

    def already_running() -> bool:
        # Windows 的 SO_REUSEADDR 允许同端口二次绑定，不能只靠 OSError。
        # 先发一个 HTTP 探针，已经在跑就只开页面，绝不重复起第二个服务。
        try:
            from urllib.request import urlopen
            with urlopen(url, timeout=1.0) as resp:
                return 200 <= int(resp.status) < 500
        except Exception:
            return False

    if already_running():
        print(f"识海已经在跑  {url}")
        print(f"数据  {'示例数据（不写库）' if demo else DB_PATH}")
        if open_browser:
            webbrowser.open(url)
        return
    try:
        server = S(("127.0.0.1", port), H)
    except OSError:
        # 8767 通常已经由桌面入口或上一次 sihai.cmd 拉起；重复启动只开页面。
        print(f"识海已经在跑  {url}")
        print(f"数据  {'示例数据（不写库）' if demo else DB_PATH}")
        if open_browser:
            webbrowser.open(url)
        return
    print(f"识海（存储库）  {url}")
    print(f"数据  {'示例数据（不写库）' if demo else DB_PATH}")
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    with server as httpd:
        httpd.serve_forever()


def main() -> None:
    ap = argparse.ArgumentParser(description="识海：记忆存储库的星图")
    ap.add_argument("--demo", action="store_true", help="用内置示例数据")
    ap.add_argument("--serve", action="store_true", help="起本地服务并打开浏览器")
    ap.add_argument("--port", type=int, default=8767)
    ap.add_argument("--open", action="store_true")
    ap.add_argument("--out", type=Path, help="把图导出成 json")
    a = ap.parse_args()

    if a.serve:
        serve(a.demo, a.port, a.open)
        return
    g = graph_payload(a.demo)
    if a.out:
        a.out.write_text(json.dumps(g, ensure_ascii=False), encoding="utf-8")
        print(f"{a.out}  nodes={len(g['nodes'])} edges={len(g['edges'])}")
    else:
        print(json.dumps(g["meta"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
