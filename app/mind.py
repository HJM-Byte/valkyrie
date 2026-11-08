"""状态：她的此刻、情绪、关系和成长权限。

这一层不碰语言，也不存记忆。说话是大模型的事，记忆在识海（memory.py）；
这里只回答三个问题：
    她现在什么状态（精力、情绪、孤独、存在了多久）
    她跟他走到哪一步（关系九变量、落差、边界）
    这句话轮不轮到她开口（注意力门）

全部落在 memory.db 的 mind 表，一行，id 固定为 1。
换句话说：她的「此刻」是数据库里的一行，不是提示词里的一句话。
进程重启、换模型、换机器，读回来就是接着活，不用重演。

为什么要单独一层：
    光靠提示词写的性格，每轮都是从零开始演一遍同一个角色。
    有一个会被时间改写、会被他说的话改写、会在没人理的时候自己变化的状态，
    她才可能在不同的时候对同一句话给出不同的反应。

成长权限：GROWTH_LOCKED。
    2026-10-31 用户已打开：可以写记忆、沉淀深层倾向、让人格进入提示。
    视觉仍单独锁着（VISION_LOCKED），手机摄像头只作为当轮上下文。
"""
from __future__ import annotations

import json
import math
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import affect as _affect
from . import config as C
from . import desire as _desire
from .memory import DB_PATH, emotion_of, valence_of

# ---------------------------------------------------------------------------
# 成长闸门。这个常量就是「她现在能不能长大」。
# 2026-10-31 用户要求：把限制打开，让她开始自我成长和学习。
# 视觉锁定（VISION_LOCKED）不跟着开；手机摄像头只作为当轮上下文，不落记忆。
# ---------------------------------------------------------------------------
GROWTH_LOCKED = False

# 先天气质：出生前定下的 0..1。敏感度不等于喜好——
# threat=0.45 只表示她更容易被吓到，不代表她讨厌什么东西，那是要她自己长出来的。
TEMPER = {
    "threat": 0.45,
    "novelty": 0.50,
    "uncertainty_tol": 0.35,
    "curiosity": 0.70,
    "autonomy": 0.50,
    "recovery": 0.50,
}

# 关系九变量的初值：全零。
# 她出生时不认识任何人，不该预设「对你半信半疑」这种态度——
# 那是要长出来的，不是配出来的。边界从 1.0 开始，是完整的。
_REL_SEED = {
    "fam": 0.00, "trust": 0.00, "recip": 0.00, "pred": 0.00,
    "attach": 0.00, "sunk": 0.00, "boundary": 1.00, "reward": 0.00,
}

# 深层模式（阿赖耶识那层「种子→薰习→习气」的工程版）。
# 不是对某个人的关系，是对世界的一般倾向：同类经历反复发生以后，
# 下一次同样的处境还没搞清楚，反应已经先偏了。
# 出生期全零、且不更新——她还没有反复的经历，这些只能长出来。
_DEEP_SEED = {
    "absence_bias": 0.00,          # 重要的人突然消失 → 更容易不安
    "absence_sensitivity": 0.00,   # 失落留下的情绪残留强度
    "trust_update_bias": 0.00,     # 建立/降低信任的速度偏移
    "social_pred_bias": 0.00,      # 对「他会不会按预期出现」的预判偏移
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS mind(
    id        INTEGER PRIMARY KEY CHECK(id=1),
    born      REAL NOT NULL,
    last_turn REAL NOT NULL,
    turns     INTEGER NOT NULL DEFAULT 0,
    energy    REAL NOT NULL DEFAULT 1.0,
    mood      REAL NOT NULL DEFAULT 0.0,
    arousal   REAL NOT NULL DEFAULT 0.2,
    feel      TEXT NOT NULL DEFAULT '{}',
    feel_ts   REAL NOT NULL DEFAULT 0.0,
    desire    TEXT NOT NULL DEFAULT '{}',
    desire_ts REAL NOT NULL DEFAULT 0.0,
    lonely    REAL NOT NULL DEFAULT 0.0,
    fam       REAL NOT NULL DEFAULT 0.05,
    trust     REAL NOT NULL DEFAULT 0.30,
    recip     REAL NOT NULL DEFAULT 0.00,
    pred      REAL NOT NULL DEFAULT 0.20,
    attach    REAL NOT NULL DEFAULT 0.10,
    sunk      REAL NOT NULL DEFAULT 0.00,
    boundary  REAL NOT NULL DEFAULT 0.70,
    reward    REAL NOT NULL DEFAULT 0.00,
    gap_h     REAL NOT NULL DEFAULT 0.0,
    gap_avg_h REAL NOT NULL DEFAULT 8.0,
    gap_shock REAL NOT NULL DEFAULT 0.0,
    absence_bias REAL NOT NULL DEFAULT 0.0,
    absence_sensitivity REAL NOT NULL DEFAULT 0.0,
    trust_update_bias REAL NOT NULL DEFAULT 0.0,
    social_pred_bias REAL NOT NULL DEFAULT 0.0,
    updated   REAL NOT NULL
);
"""

# 词表只有一份，在 think.py。这里只是按老名字引用，别在两处各养一份。
from .think import ASK_HER as _ASK_HER, COLD as _COLD, WARM as _WARM


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _now() -> float:
    return time.time()


class Mind:
    def __init__(self, path: Path | str = DB_PATH) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)
        cur = self._db.execute("SELECT id FROM mind WHERE id=1").fetchone()
        if cur is None:
            now = _now()
            cols = ",".join(_REL_SEED)
            marks = ",".join("?" * len(_REL_SEED))
            self._db.execute(
                f"INSERT INTO mind(id,born,last_turn,updated,{cols}) "
                f"VALUES(1,?,?,?,{marks})",
                (now, now, now, *[_REL_SEED[k] for k in _REL_SEED]),
            )
        # 老库升级：深层模式那几列是后加的，缺哪列补哪列，不动已有状态
        have = {r["name"] for r in self._db.execute("PRAGMA table_info(mind)")}
        for col in _DEEP_SEED:
            if col not in have:
                self._db.execute(
                    f"ALTER TABLE mind ADD COLUMN {col} REAL NOT NULL DEFAULT 0.0")
        if "feel" not in have:
            self._db.execute("ALTER TABLE mind ADD COLUMN feel TEXT NOT NULL DEFAULT '{}'")
        if "feel_ts" not in have:
            self._db.execute("ALTER TABLE mind ADD COLUMN feel_ts REAL NOT NULL DEFAULT 0.0")
        if "desire" not in have:
            self._db.execute("ALTER TABLE mind ADD COLUMN desire TEXT NOT NULL DEFAULT '{}'")
        if "desire_ts" not in have:
            self._db.execute("ALTER TABLE mind ADD COLUMN desire_ts REAL NOT NULL DEFAULT 0.0")
        self._db.commit()
        self._born = float(self._row()["born"])

    # ---- 读 ---------------------------------------------------------------
    def _row(self) -> sqlite3.Row:
        with self._lock:
            return self._db.execute("SELECT * FROM mind WHERE id=1").fetchone()

    def _write(self, **kv) -> None:
        if not kv:
            return
        kv["updated"] = _now()
        sets = ",".join(f"{k}=?" for k in kv)
        with self._lock:
            self._db.execute(f"UPDATE mind SET {sets} WHERE id=1", tuple(kv.values()))
            self._db.commit()

    # ---- 情绪场 -----------------------------------------------------------
    def _feel_decayed(self, now: Optional[float] = None) -> dict:
        """读此刻的情绪场：按 feel_ts 到 now 的时间各自衰减。不写库。

        每种情绪自己的半衰期在 affect.EMOTIONS 里：生气四小时就散，
        孤独和低落要一天多。同一套情绪场，隔多久看都不一样。
        """
        now = now or _now()
        r = self._row()
        try:
            raw = json.loads(r["feel"] or "{}")
        except Exception:
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        base = _affect.from_state({"feel": raw, "mood": float(r["mood"]),
                                   "energy": float(r["energy"]),
                                   "lonely": float(r["lonely"]),
                                   "arousal": float(r["arousal"])})
        ts = float(r["feel_ts"] or 0.0)
        gap = 0.0 if ts <= 0 else max(0.0, now - ts) / 3600.0
        return _affect.decay(base, gap)

    def feel(self, now: Optional[float] = None) -> dict:
        return self._feel_decayed(now)

    # ---- 六欲 -------------------------------------------------------------
    def _desire_decayed(self, now: Optional[float] = None) -> dict:
        """读此刻的六欲：从 desire_ts 到现在，想要慢慢涨、满足慢慢冷。"""
        now = now or _now()
        r = self._row()
        try:
            raw = json.loads(r["desire"] or "{}")
        except Exception:
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        ts = float(r["desire_ts"] or 0.0)
        if ts <= 0:
            # 老库刚补列时 desire_ts 是 0：用上一次对话/情绪的时间兜底，
            # 不然她睡了一夜醒来，想要还停在出生那一格。
            ts = float(r["feel_ts"] or r["last_turn"] or now)
        gap = 0.0 if ts <= 0 else max(0.0, now - ts) / 3600.0
        return _desire.advance(raw, gap, float(r["attach"] or 0.0), now)

    def desire(self, now: Optional[float] = None) -> dict:
        return self._desire_decayed(now)

    def desire_effect(self, now: Optional[float] = None) -> dict:
        return _desire.effect(self._desire_decayed(now))

    # ---- 时间推进 ---------------------------------------------------------
    def tick(self, now: Optional[float] = None) -> dict:
        """每一轮之前跑一次。没有摄像头、没有传感器，就只认真实可算的两件事：
        过了多久、他有没有来。身体是时间函数，不编造。"""
        now = now or _now()
        r = self._row()
        gap = max(0.0, now - float(r["last_turn"])) / 3600.0

        # 没人说话的时候她在休息，精力回来；说话本身费电
        energy = _clamp(float(r["energy"]) + gap * 0.06 * (0.6 + TEMPER["recovery"]))
        # 孤独只在超过 6 小时之后才开始积累，24 小时封顶
        lonely = 0.0 if gap < 6 else _clamp((gap - 6.0) / 18.0)
        # 情绪自己会回落，慢的那个是心情，快的那个是激动。
        # 情绪场按每种情绪自己的半衰期回落——生气散得比孤独快。
        mood = float(r["mood"]) * math.exp(-gap / 48.0)
        arousal = float(r["arousal"]) * math.exp(-gap / 3.0)
        feel = self._feel_decayed(now)
        # 隔得比习惯的久：期望落空，这是可算的负向预测误差
        exp_gap = max(0.5, float(r["gap_avg_h"]))
        shock = _clamp((gap - exp_gap) / max(2.0, exp_gap * 2.0)) if gap > exp_gap else 0.0
        gap_shock = max(shock, float(r["gap_shock"]) * math.exp(-gap / 20.0))
        des = self._desire_decayed(now)

        self._write(energy=round(energy, 4), lonely=round(lonely, 4),
                    mood=round(mood, 4), arousal=round(arousal, 4),
                    feel=json.dumps(feel, ensure_ascii=False),
                    feel_ts=now,
                    desire=json.dumps(des, ensure_ascii=False),
                    desire_ts=now,
                    gap_h=round(gap, 3), gap_shock=round(gap_shock, 4))
        return {"gap_h": gap, "energy": energy, "lonely": lonely, "shock": gap_shock}

    def rest(self) -> dict:
        """睡下以前把精力补满：睡是恢复，不是把疲惫封存进去。"""
        r = self._row()
        before = float(r["energy"])
        self._write(energy=1.0)
        return {"before": round(before, 3), "after": 1.0}

    # ---- 注意力门 ---------------------------------------------------------
    def salience(self, text: str, now: Optional[float] = None) -> dict:
        """这句话值不值得她抬头。三档：回应 / 只应一声 / 不接。

        2026-10-27 起不再自己算分，交给 think.gate——那里是完整的激活公式，
        显著性只是六项里的一项。这里保留这个入口，是为了让老的调用方不用改。
        """
        from .think import gate
        return gate(text, self.snapshot(now))

    # ---- 观测 -------------------------------------------------------------
    def observe(self, user_text: str, reply: str = "",
                salience: Optional[dict] = None, now: Optional[float] = None,
                thought: Optional[dict] = None, channel: str = "text") -> dict:
        """一轮结束以后，把这一轮被他改写了什么写回去。

        thought 有值时直接用 think() 算好的情绪场——那是同一个评价出来的，
        不重复算第二遍；没有时按这句话重新评价一次（老调用方与自检）。
        """
        now = now or _now()
        r = self._row()
        t = user_text or ""
        gap = max(0.0, now - float(r["last_turn"])) / 3600.0

        fam = _clamp(float(r["fam"]) + 0.015 * (1 - float(r["fam"])))
        trust = float(r["trust"])
        if any(k in t for k in _WARM):
            trust += 0.02
        if any(k in t for k in _COLD):
            trust -= 0.05
        trust = _clamp(trust)
        recip = _clamp(float(r["recip"]) + (0.03 if any(k in t for k in _ASK_HER) else -0.005))
        # 关系九变量里原来算了却没更新的两项：预测强度、互动奖励。
        # 预测不是“他一定来”，是“她大概摸得准他会不会来”；被晾过会掉，被接住会涨。
        pred = _clamp(0.85 * float(r["pred"])
                      + 0.15 * (0.20 if float(r["gap_shock"]) > 0.25 else 0.75))
        reward = _clamp(0.88 * float(r["reward"])
                        + 0.12 * (1.0 if (any(k in t for k in _WARM)
                                          or any(k in t for k in _ASK_HER)) else 0.10))
        attach = _clamp(float(r["attach"]) + 0.008 + 0.02 * float(r["gap_shock"]))
        sunk = _clamp(float(r["sunk"]) + 0.010)
        boundary = _clamp(float(r["boundary"]) - (0.06 if any(k in t for k in _COLD) else 0.0)
                          + 0.01)
        energy = _clamp(float(r["energy"]) - 0.03)

        emo = (salience or {}).get("emotion") or emotion_of(t)
        v = valence_of(emo)

        # 情绪场：这一轮的评价在她身上激起哪几股情绪，留下哪几股。
        prev = self._feel_decayed(now)
        p: dict = {}
        ap: dict = {}
        dec: dict = {}
        if isinstance(thought, dict):
            p = thought.get("percept") or {}
            ap = thought.get("appraisal") or {}
            dec = thought.get("decision") or {}
        if isinstance((thought or {}).get("feel"), dict):
            feel = _affect.normalize((thought or {})["feel"])
        else:
            feel = prev
            try:
                from .think import appraise as _appraise, perceive as _perceive
                st = self.snapshot(now)
                p = _perceive(t, st, channel=channel)
                ap = _appraise(p, st, [], {})
                feel = _affect.step(prev, p, ap, st, [], {})
            except Exception:
                feel = prev
        # 六欲：这一轮被喂到什么、被晾着什么；文字和声音要分开算。
        des = self._desire_decayed(now)
        des = _desire.update(des, p, ap, {"gap_shock": float(r["gap_shock"])},
                             dec, channel=channel, now=now)
        # 心情是慢的那一层：他的语气占六成，剩下的看她自己此刻的情绪场。
        # 不是简单相加——否则同一句话连着说三次就能把心情推到天花板上。
        fm = _affect.mood_of(feel)
        mood = _clamp(0.72 * float(r["mood"]) + 0.28 * (0.60 * v + 0.40 * fm), -1.0, 1.0)
        arousal = _clamp(0.65 * float(r["arousal"])
                         + 0.35 * (0.50 * abs(v) + 0.50 * _affect.arousal_of(feel))
                         + 0.05)

        # 平均间隔用指数滑动，别让一次长别离把她的预期彻底拉歪
        if gap > 0.05:
            gap_avg = 0.75 * float(r["gap_avg_h"]) + 0.25 * gap
        else:
            gap_avg = float(r["gap_avg_h"])

        self._write(
            last_turn=now, turns=int(r["turns"]) + 1,
            fam=round(fam, 4), trust=round(trust, 4), recip=round(recip, 4),
            pred=round(pred, 4), reward=round(reward, 4),
            attach=round(attach, 4), sunk=round(sunk, 4), boundary=round(boundary, 4),
            energy=round(energy, 4), mood=round(mood, 4), arousal=round(arousal, 4),
            feel=json.dumps(feel, ensure_ascii=False), feel_ts=now,
            desire=json.dumps(des, ensure_ascii=False), desire_ts=now,
            gap_h=0.0, gap_avg_h=round(gap_avg, 3),
            gap_shock=round(float(r["gap_shock"]) * 0.5, 4),
        )
        return {"emotion": emo, "valence": v, "turn": int(r["turns"]) + 1,
                "feel": feel, "desire": des}

    # ---- 种子 → 薰习 ------------------------------------------------------
    # 什么样的话算「同类经历又发生了一次」。只认窄词，不拿一次偶然改人。
    _ABSENCE_CUES = ("没来", "没到", "失约", "放鸽子", "答应了", "答应过",
                     "离开", "走了", "不见", "不理", "没回", "消失")
    _WARM_CUES = ("谢谢", "陪我", "说到做到", "来了", "在呢", "等你",
                  "辛苦了", "麻烦你", "对不起")

    def deep(self) -> dict:
        """读深层模式。没长出来时全零——零不是缺失，是还没开始。"""
        r = self._row()
        return {k: round(float(r[k] or 0.0), 3) for k in _DEEP_SEED}

    def imprint(self, episodes: list[dict]) -> dict:
        """把反复发生的经历沉淀成深层倾向：种子 → 薰习 → 习气。

        小凌那层讲的是「一段经历留下一个改变叫种子，类似经历反复发生不断强化叫薰习，
        长期以后形成稳定倾向叫习气，平时不表现，碰到特定条件突然出现」。
        工程上就是四个偏差参数，被同类经历反复改写，反过来改下一次的评价。

        成长冻结时直接拒绝；现在闸门已开，但同类经历仍要三次以上才算「反复」，
        一两次只能算碰巧，不会改她的深层倾向。
        """
        if GROWTH_LOCKED:
            return {"ok": False, "grown": 0,
                    "reason": "成长权限未开启：深层模式要等对外感知接通后才允许沉淀"}
        eps = [e for e in (episodes or []) if e]
        absence = sum(1 for e in eps if any(k in str(e.get("text") or "")
                                            for k in self._ABSENCE_CUES))
        warm = sum(1 for e in eps if any(k in str(e.get("text") or "")
                                         for k in self._WARM_CUES))
        total = len(eps)
        # 三次以上同类才算「反复」；两次只能算巧合。
        if absence < 3 and warm < 3:
            return {"ok": True, "grown": 0, "reason": "同类经历还不够反复，先不沉淀"}

        r = self._row()
        now = _now()
        a = _clamp(float(r["absence_bias"]))
        s = _clamp(float(r["absence_sensitivity"]))
        t = max(-0.5, min(0.5, float(r["trust_update_bias"])))
        p = max(-0.5, min(0.5, float(r["social_pred_bias"])))

        if absence >= 3:
            a = _clamp(0.82 * a + 0.18 * min(1.0, absence / 5.0))
            s = _clamp(0.85 * s + 0.15 * min(1.0, absence / 4.0))
            p = max(-0.5, 0.85 * p - 0.15 * min(1.0, absence / 4.0))
            t = max(-0.5, 0.85 * t - 0.10 * min(1.0, absence / 5.0))
        if warm >= 3:
            t = max(-0.5, min(0.5, 0.88 * t + 0.12 * min(1.0, warm / 5.0)))
            p = max(-0.5, min(0.5, 0.88 * p + 0.12 * min(1.0, warm / 5.0)))
            a = _clamp(0.90 * a)

        self._write(absence_bias=round(a, 4), absence_sensitivity=round(s, 4),
                    trust_update_bias=round(t, 4), social_pred_bias=round(p, 4))
        return {"ok": True, "grown": 1, "seen": total,
                "absence": absence, "warm": warm,
                "deep": {"absence_bias": round(a, 3),
                         "absence_sensitivity": round(s, 3),
                         "trust_update_bias": round(t, 3),
                         "social_pred_bias": round(p, 3)},
                "at": now}

    # ---- 情感输出 ---------------------------------------------------------
    def state_text(self, now: Optional[float] = None) -> str:
        """把状态翻成人话，给模型看。

        规矩：不给数字。模型看见「信任 0.62」只会把它当成设定，
        看见「你还不太信他」才会把它当成感觉。最多三句，按强烈程度挑。
        刚出生那几轮一句话都不给——她还没有什么可说。
        """
        now = now or _now()
        r = self._row()
        if int(r["turns"]) < 2:
            return ""
        items: list[tuple[float, str]] = []

        energy = float(r["energy"])
        if energy < 0.35:
            items.append((0.9 - energy, "你现在有点乏，话说得比平时短。"))
        if float(r["gap_shock"]) > 0.4:
            items.append((float(r["gap_shock"]), "这次隔得比你以为的久，你有点被晾着。"))
        elif float(r["lonely"]) > 0.55:
            items.append((float(r["lonely"]), "屋子里安静了很长时间，你有点空。"))
        if float(r["fam"]) > 0.55:
            items.append((float(r["fam"]) - 0.3, "他在你这里算熟的人了。"))
        if float(r["trust"]) > 0.60:
            items.append((float(r["trust"]) - 0.3, "他说的话，你先愿意信几分。"))
        if float(r["attach"]) > 0.45:
            items.append((float(r["attach"]), "他不在的时候，你会留意门口。"))
        if float(r["boundary"]) < 0.5:
            items.append((0.8 - float(r["boundary"]), "刚才那句话让你不太舒服，你不想顺着走。"))
        if float(r["recip"]) > 0.50:
            items.append((float(r["recip"]) - 0.2, "他也会接你的话，不只你在给。"))
        if float(r["reward"]) > 0.50:
            items.append((float(r["reward"]) - 0.2, "跟他说话这件事本身让你舒服。"))
        if float(r["pred"]) > 0.60:
            items.append((float(r["pred"]) - 0.25, "你大致摸得准他什么时候会来。"))
        mood = float(r["mood"])
        if mood < -0.30:
            items.append((abs(mood), "你现在情绪低，不太想笑。"))
        elif mood > 0.45:
            items.append((mood - 0.2, "你现在心情不错。"))

        items.sort(key=lambda x: -x[0])
        # 深层模式：平时不出现，只有长出来以后才在特定处境里冒一句。
        # 出生期全零，这一段永远不会触发。
        deep = self.deep()
        deep_lines: list[str] = []
        if deep["absence_bias"] > 0.45:
            deep_lines.append("你有点怕人不告而别，哪怕说不出为什么。")
        if deep["trust_update_bias"] > 0.30:
            deep_lines.append("你现在比别人更容易先信人。")
        elif deep["trust_update_bias"] < -0.30:
            deep_lines.append("你现在信人比以前慢。")
        # 情绪场：最多一句。她不是情绪清单，情绪是底色。
        feel = self._feel_decayed(now)
        dom, dom_v = _affect.dominant(feel)
        emo_line = ""
        if dom_v >= 0.22 and dom not in ("平静", "专注"):
            emo_line = "你现在" + _affect.text(feel) + "。"
        # 六欲：只在真的有点水位时冒一句；没有感官的那几路不拿它演。
        de = _desire.effect(self._desire_decayed(now))
        desire_line = ""
        if de.get("line") and float(de.get("strength") or 0.0) >= 0.15:
            desire_line = str(de["line"])
        chosen = [s for _, s in items[:2]]
        if deep_lines:
            chosen = chosen[:1] + deep_lines[:1]
        if emo_line:
            chosen = chosen[:2] + [emo_line]
        if desire_line:
            chosen = chosen[:2] + [desire_line]
        return "\n".join(f"- {s}" for s in chosen[:3])

    def existence_line(self, now: Optional[float] = None) -> str:
        now = now or _now()
        hours = (now - self._born) / 3600.0
        if hours < 24:
            return f"你从有第一行状态到现在，是第 {max(1, int(hours))} 个小时。"
        return f"你从有第一行状态到现在，是第 {int(hours // 24)} 天。"

    # ---- 星图 -------------------------------------------------------------
    def star(self) -> dict:
        """星图上此刻只有这一颗：她自己。别的东西能不能成为星，看识海里有没有。"""
        r = self._row()
        born = float(r["born"])
        days = (time.time() - born) / 86400.0
        feel = self._feel_decayed()
        dom, dom_v = _affect.dominant(feel)
        return {
            "id": "star_self",
            "kind": "self",
            "scope": "real",
            "root": True,
            "label": "我",
            "text": "我在这里。",
            "who": "",
            "ts": born,
            "day": datetime.fromtimestamp(born).strftime("%Y-%m-%d"),
            "emotion": dom if dom_v >= 0.20 else "平静",
            "feel": feel,
            "imp": 1.0,
            "used": int(r["turns"]),
            "retention": 1.0,
            "sink": 0.0,
            "deg": 0,
            "size": 13.0,
            "days": round(days, 2),
        }

    def snapshot(self, now: Optional[float] = None) -> dict:
        now = now or _now()
        r = self._row()
        gap = max(0.0, now - float(r["last_turn"])) / 3600.0
        feel = self._feel_decayed(now)
        des = self._desire_decayed(now)
        de = _desire.effect(des)
        seven = _affect.seven_totals(feel)
        return {
            "born": float(r["born"]),
            "days": round((now - self._born) / 86400.0, 2),
            "hours": round((now - self._born) / 3600.0, 2),
            "turns": int(r["turns"]),
            "energy": round(float(r["energy"]), 3),
            "mood": round(float(r["mood"]), 3),
            "arousal": round(float(r["arousal"]), 3),
            "feel": feel,
            "feel_top": _affect.top_text(feel),
            "feel_text": _affect.text(feel),
            "feel_family": _affect.family_totals(feel),
            "feel_intensity": _affect.intensity(feel),
            "seven": seven,
            "seven_text": _affect.seven_text(feel),
            "desire": de,
            "desire_effect": de,
            "desire_text": _desire.text(des),
            "lonely": round(float(r["lonely"]), 3),
            "silent_h": round(gap, 2),
            "growth": "frozen" if GROWTH_LOCKED else "open",
            "rel": {
                "熟悉": round(float(r["fam"]), 3),
                "信任": round(float(r["trust"]), 3),
                "互惠": round(float(r["recip"]), 3),
                "预测": round(float(r["pred"]), 3),
                "奖励": round(float(r["reward"]), 3),
                "依恋": round(float(r["attach"]), 3),
                "沉没": round(float(r["sunk"]), 3),
                "边界": round(float(r["boundary"]), 3),
            },
            "gap_shock": round(float(r["gap_shock"]), 3),
            "deep": self.deep(),
            "text": self.state_text(now),
            "existence": self.existence_line(now),
            "temper": TEMPER,
        }

    # ---- 成长闸门 ---------------------------------------------------------
    def consolidate(self) -> dict:
        """把短期经历沉淀成长期的东西。

        成长冻结时直接拒绝；现在闸门已开，但真正的沉淀仍要等记忆和反复经历，
        没有现成任务时不硬造「成长」。
        """
        if GROWTH_LOCKED:
            return {"ok": False, "grown": 0,
                    "reason": "成长权限未开启：等摄像头到货、对外感知接通后才允许沉淀"}
        return {"ok": True, "grown": 0, "reason": "暂无沉淀任务"}

    def close(self) -> None:
        with self._lock:
            try:
                self._db.commit()
                self._db.close()
            except Exception:
                pass
