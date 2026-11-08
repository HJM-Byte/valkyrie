"""自主学习：由她此刻的情绪、六欲和记忆，长出一个「想弄明白」的问题。

旧的学习层（learning.py）是「渠道 + 夜间窗口」：到点了、有渠道，就去抓材料；
她答不上来的问题会记成好奇清单，这算半自动。但仍然有两个被动：
    1. 没有情绪时，她也会按渠道轮转去学；
    2. 学什么由渠道 queries / 好奇清单决定，不是她此刻自己最想弄明白的。

这一层把「学什么」往前挪了一步：

    情绪场 / 六欲 / 记忆残响 → 学习冲动 I_learn → 一个问题 → 自己去查
    → 材料进待审区 → 你审核 → 才可能成为知识 → 影响下一次评价

几条硬边界：
- 搜索词只用通用情绪/主题词（keywords），**不把记忆原文发到网上**；
  记忆只在本机决定「为什么是这个问题」和优先级。
- 不自动批准、不自动改人格、不自动写记忆库；成长冻结时仍然只进待审区。
- 查不到就记「没找到」，不拿旧材料冒充新答案。
- 没有情绪也没有记忆时，不硬造学习冲动。
"""
from __future__ import annotations

import argparse
import json
import threading
import time
from datetime import datetime
from typing import Optional

from . import affect as _affect
from . import config as C
from .learning import DEFAULT_AUTONOMY

# 情绪 -> 通用问题。问题本身不碰记忆原文，只描述这一类感觉；
# 记忆只在 why 和优先级里出现，搜索只用 keywords。
_TEMPLATES: tuple[dict, ...] = (
    {"key": "不安", "emotions": ("不安",),
     "question": "人为什么会感到不安？",
     "keywords": ["不安", "情绪", "安全感"]},
    {"key": "孤独", "emotions": ("孤独",),
     "question": "人为什么会觉得孤独？",
     "keywords": ["孤独", "情绪", "连接"]},
    {"key": "委屈", "emotions": ("委屈", "失望"),
     "question": "人为什么会觉得委屈？",
     "keywords": ["委屈", "情绪", "期待"]},
    {"key": "生气", "emotions": ("生气", "厌恶"),
     "question": "人为什么会生气？",
     "keywords": ["生气", "情绪", "边界"]},
    {"key": "低落", "emotions": ("低落",),
     "question": "情绪低落的时候，人身上发生了什么？",
     "keywords": ["情绪低落", "身体", "睡眠"]},
    {"key": "疲惫", "emotions": ("疲惫",),
     "question": "疲惫的时候，人为什么不想说话？",
     "keywords": ["疲惫", "情绪", "身体"]},
    {"key": "内疚", "emotions": ("内疚",),
     "question": "人为什么会内疚？",
     "keywords": ["内疚", "情绪", "关系"]},
    {"key": "被接住", "emotions": ("开心", "安心", "被理解", "感激"),
     "question": "被人接住的时候，人心里会发生什么？",
     "keywords": ["安心", "关系", "情绪"]},
    {"key": "亲近", "emotions": ("亲近", "心动"),
     "question": "亲近感是怎么产生的？",
     "keywords": ["亲近", "关系", "依恋"]},
    {"key": "期待", "emotions": ("期待", "好奇", "渴望"),
     "question": "人为什么会期待还没发生的事？",
     "keywords": ["期待", "情绪", "预测"]},
    {"key": "说不清", "emotions": ("说不清",),
     "question": "人说不上来的感觉是怎么来的？",
     "keywords": ["情绪", "记忆", "潜意识"]},
)

# 情绪问题的「家族」：正面情绪也值得弄明白，但不该像负面那样催着她去查。
_POSITIVE = {"开心", "安心", "被理解", "感激", "亲近", "心动", "期待", "好奇", "渴望"}


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    try:
        return max(lo, min(hi, float(x)))
    except (TypeError, ValueError):
        return lo


def _short(s: str, n: int = 24) -> str:
    t = " ".join(str(s or "").split())
    return t[:n] + "…" if len(t) > n else t


def _study_key(s: dict) -> tuple[float, str]:
    """学习记录的全序键：先 ts，再 id。

    同 ts 不声称有语义先后；只用 id 保证「同一输入集，无论怎么打乱，
    结果都一样」。不要用 >=，也不要依赖 recent_studies() 的返回顺序。
    """
    return (float(s.get("ts") or 0.0), str(s.get("id") or ""))


def _latest_by_question(recent: list[dict]) -> dict[str, dict]:
    """每个问题只留全序键最大的那条记录。"""
    by_q: dict[str, dict] = {}
    for s in recent or []:
        q = str(s.get("question") or s.get("q") or "")
        if not q:
            continue
        prev = by_q.get(q)
        if prev is None or _study_key(s) > _study_key(prev):
            by_q[q] = s
    return by_q


def _why_from(emotion: str, memory_n: int, ambient_i: float,
              desire: float = 0.0, curiosity: bool = False) -> str:
    bits: list[str] = []
    if emotion:
        bits.append(f"你心里还有点{emotion}，它没有完全散")
    if memory_n:
        bits.append("你记得的旧事里也有这种味道")
    if ambient_i >= 0.20:
        bits.append("还有一层说不清的感觉垫在下面")
    if desire >= 0.18:
        bits.append("你有点想被听懂，这个问题就赖着不走")
    if curiosity:
        bits.append("你之前答不上来的一件事还挂着")
    if not bits:
        bits.append("你自己想弄明白它")
    return "；".join(bits) + "。"


def _memory_intent(memories: list[dict],
                   ambient: Optional[dict] = None) -> Optional[dict]:
    """记忆自己长出来的问题：细节淡了，可改变还在。"""
    if not memories:
        return None
    ambient = ambient or {}
    sig = max((_clamp(m.get("imp")) * 0.45 + _clamp(m.get("emo_i")) * 0.55)
              for m in memories)
    residues = [m for m in memories
                if float(m.get("detail") or 1.0) < 0.55
                and float(m.get("emo_i") or 0.0) >= 0.20]
    imprints = [m for m in memories if (m.get("imprint") or "").strip()]
    if sig < 0.28 and not residues and not imprints:
        return None
    base = 0.10 + 0.34 * sig
    if residues:
        base += 0.08 + 0.05 * min(3, len(residues))
    if imprints:
        base += 0.05
    base += 0.10 * _clamp(ambient.get("intensity"))
    picked = residues or imprints or memories[:3]
    ids = [int(m.get("id") or 0) for m in picked[:5] if m.get("id")]
    why = ("你记得的事里，有些已经说不清细节了，可它还在改你现在的感觉。"
           if residues or imprints else
           "你记得的那些事还在你身上，你想弄明白它们为什么留得下来。")
    return {
        "key": "memory_residue",
        "trigger": "memory",
        "question": "为什么一件记不清的事，还会影响现在的感觉？",
        "keywords": ["记忆", "情绪", "遗忘"],
        "query": "记忆 情绪 遗忘",
        "why": why,
        "emotions": {},
        "memory_ids": ids,
        "memory_count": len(memories),
        "ambient": round(_clamp(ambient.get("intensity")), 3),
        "sources": {"memory_ids": ids, "memory_count": len(memories),
                    "residue_count": len(residues),
                    "imprint_count": len(imprints)},
        "raw_intensity": round(_clamp(base), 3),
        "intensity": round(_clamp(base), 3),
    }


def _ambient_intent(ambient: Optional[dict]) -> Optional[dict]:
    """残响：想不起是哪件事，但想知道这层感觉是什么。"""
    ambient = ambient or {}
    i = _clamp(ambient.get("intensity"))
    if i < 0.22:
        return None
    base = 0.14 + 0.38 * i
    return {
        "key": "ambient",
        "trigger": "memory",
        "question": "人说不上来的感觉是怎么来的？",
        "keywords": ["情绪", "记忆", "潜意识"],
        "query": "情绪 记忆 潜意识",
        "why": "你心里有一层说不清的东西，想不起是哪件事，可它一直在。",
        "emotions": {},
        "memory_ids": [],
        "ambient": round(i, 3),
        "sources": {"ambient": round(i, 3),
                    "unknown": int(ambient.get("unknown") or 0),
                    "sources": int(ambient.get("sources") or 0)},
        "raw_intensity": round(_clamp(base), 3),
        "intensity": round(_clamp(base), 3),
    }


def _desire_intent(state: dict) -> Optional[dict]:
    """六欲里真正接上的那一路（意）：想被听懂，也会长成问题。"""
    de = (state or {}).get("desire_effect") or {}
    frust = _clamp(de.get("frustration"))
    strength = _clamp(de.get("strength"))
    if str(de.get("top") or "") != "意" or frust < 0.18:
        return None
    base = 0.10 + 0.40 * frust + 0.12 * strength
    return {
        "key": "desire",
        "trigger": "desire",
        "question": "人为什么想被听懂？",
        "keywords": ["理解", "沟通", "情绪"],
        "query": "理解 沟通 情绪",
        "why": "你有点想被听懂，还没被喂到；这个问题一直赖着。",
        "emotions": {},
        "memory_ids": [],
        "ambient": 0.0,
        "sources": {"desire": "意", "frustration": round(frust, 3),
                    "strength": round(strength, 3)},
        "raw_intensity": round(_clamp(base), 3),
        "intensity": round(_clamp(base), 3),
    }


def _curiosity_intent(curiosities: list[dict]) -> Optional[dict]:
    """她之前答不上来的事，也算她自己长出来的问题；只拿关键词去查。"""
    if not curiosities:
        return None
    c = max(curiosities, key=lambda x: (int(x.get("hits") or 0), len(x.get("question") or "")))
    q = str(c.get("question") or "").strip()
    kws = [str(k) for k in (c.get("keywords") or []) if str(k).strip()]
    if len(q) < 6 or not kws:
        return None
    # 好奇只是兜底：它不该盖过「此刻心里正在翻的那股情绪」。
    base = 0.10 + 0.02 * min(4, int(c.get("hits") or 0))
    return {
        "key": "curiosity",
        "trigger": "curiosity",
        "question": q[:120],
        "keywords": kws[:4],
        "query": " ".join(kws[:3]),
        "why": "你之前答不上来的一件事，还挂着；这次自己去碰碰。",
        "emotions": {},
        "memory_ids": [],
        "ambient": 0.0,
        "curiosity_id": c.get("id"),
        "sources": {"curiosity": c.get("id"), "hits": int(c.get("hits") or 0)},
        "raw_intensity": round(_clamp(base), 3),
        "intensity": round(_clamp(base), 3),
    }


def _emotion_intents(feel: dict, memories: list[dict],
                     ambient: Optional[dict] = None) -> list[dict]:
    ambient = ambient or {}
    amb_i = _clamp(ambient.get("intensity"))
    out: list[dict] = []
    for t in _TEMPLATES:
        pairs = [(e, _clamp(feel.get(e))) for e in t["emotions"]]
        pairs = [(e, v) for e, v in pairs if v >= 0.04]
        if not pairs:
            continue
        pairs.sort(key=lambda x: -x[1])
        top_emo, top_v = pairs[0]
        base = 0.36 * top_v + 0.04
        if top_emo in _POSITIVE:
            base *= 0.90          # 好的感觉也想知道为什么，但不催
        matches = [m for m in memories
                   if str(m.get("emo") or "") in t["emotions"]]
        m_sig = max((_clamp(m.get("emo_i")) * 0.6
                     + _clamp(m.get("imp")) * 0.4 for m in matches),
                    default=0.0)
        m_boost = min(0.24, 0.05 * len(matches) + 0.14 * m_sig)
        amb_boost = 0.10 * amb_i if top_emo not in _POSITIVE else 0.03 * amb_i
        base += m_boost + amb_boost
        ids = [int(m.get("id") or 0) for m in matches[:5] if m.get("id")]
        out.append({
            "key": "emotion:" + t["key"],
            "trigger": "emotion",
            "question": t["question"],
            "keywords": list(t["keywords"]),
            "query": " ".join(t["keywords"][:3]),
            "why": _why_from(top_emo, len(matches), amb_i),
            "emotions": {e: round(v, 3) for e, v in pairs[:4]},
            "top_emotion": top_emo,
            "memory_ids": ids,
            "memory_count": len(matches),
            "ambient": round(amb_i, 3),
            "sources": {"emotions": {e: round(v, 3) for e, v in pairs[:4]},
                        "memory_ids": ids, "memory_count": len(matches),
                        "ambient": round(amb_i, 3)},
            "raw_intensity": round(_clamp(base), 3),
            "intensity": round(_clamp(base), 3),
        })
    return out


def intents(state: Optional[dict] = None, memories: Optional[list[dict]] = None,
            ambient: Optional[dict] = None,
            curiosities: Optional[list[dict]] = None,
            recent: Optional[list[dict]] = None,
            now: Optional[float] = None,
            cooldown_hours: float = 6.0) -> list[dict]:
    """纯函数：给她此刻的状态，长出候选学习冲动，按强度排序。

    不访问网络、不写库，自检和 CLI 都直接调它。
    """
    state = state or {}
    memories = memories or []
    ambient = ambient or {}
    feel = _affect.from_state(state) if not (state.get("feel") or {}) \
        else _affect.normalize(state.get("feel") or {})
    now = now or time.time()
    cands: list[dict] = []
    cands.extend(_emotion_intents(feel, memories, ambient))
    for extra in (_memory_intent(memories, ambient),
                  _ambient_intent(ambient),
                  _desire_intent(state),
                  _curiosity_intent(curiosities or [])):
        if extra:
            cands.append(extra)

    # 同一问题只留最强的那个，合并来源，别让两条模板重复问。
    merged: dict[str, dict] = {}
    for c in cands:
        q = str(c.get("question") or "")
        if not q:
            continue
        old = merged.get(q)
        if old is None or float(c.get("intensity") or 0) > float(old.get("intensity") or 0):
            if old is not None:
                c = {**c, "sources": {**old.get("sources", {}),
                                      **c.get("sources", {})}}
            merged[q] = c
        else:
            old["sources"] = {**c.get("sources", {}), **old.get("sources", {})}

    out = list(merged.values())
    # 同一问题短时间内查过：不是不能想，而是先别重复抓同样的材料。
    # 取「最新」的一条 study_session；同 ts 用 id 做稳定二级键。
    by_q = _latest_by_question(recent or [])
    for c in out:
        raw = float(c.get("raw_intensity") or c.get("intensity") or 0)
        pen = 1.0
        s = by_q.get(str(c.get("question") or ""))
        if s:
            gap = max(0.0, (now - float(s.get("ts") or 0)) / 3600.0)
            if gap < max(0.5, cooldown_hours):
                pen = 0.20
            elif gap < 24.0:
                pen = 0.60
        c["intensity"] = round(_clamp(raw * pen), 3)
        c["repeat_penalty"] = round(pen, 2)
        if s:
            c["last_studied_at"] = float(s.get("ts") or 0)
            c["last_study_status"] = s.get("status")
    out.sort(key=lambda c: -float(c.get("intensity") or 0))
    for i, c in enumerate(out):
        c["rank"] = i
    return out


class Autonomy:
    """把「想弄明白什么」接到真实的 mind / memory / learning 上。"""

    def __init__(self, learning=None, mem=None, mind=None) -> None:
        if learning is None:
            from .learning import Learning
            learning = Learning()
        self.learning = learning
        self.mem = mem
        self.mind = mind
        self._busy = False
        self._lock = threading.Lock()

    # ---- 读此刻 -----------------------------------------------------------
    def snapshot(self) -> dict:
        if self.mind is None:
            return {}
        try:
            return self.mind.snapshot()
        except Exception:
            return {}

    def memories(self) -> list[dict]:
        if self.mem is None:
            return []
        try:
            if hasattr(self.mem, "recent_emotional"):
                return self.mem.recent_emotional(12)
            return self.mem.recent_episodes(12)
        except Exception:
            return []

    def ambient(self) -> dict:
        if self.mem is None:
            return {}
        try:
            return self.mem.ambient()
        except Exception:
            return {}

    def curiosities(self) -> list[dict]:
        try:
            return self.learning.open_curiosities(6)
        except Exception:
            return []

    def recent_studies(self) -> list[dict]:
        try:
            return self.learning.recent_studies(20)
        except Exception:
            return []

    def cfg(self) -> dict:
        try:
            c = self.learning.config().get("autonomy")
        except Exception:
            c = None
        return {**DEFAULT_AUTONOMY, **(c or {})}

    def intents(self, state: Optional[dict] = None,
                now: Optional[float] = None) -> list[dict]:
        state = state if state is not None else self.snapshot()
        return intents(state, self.memories(), self.ambient(),
                       self.curiosities(), self.recent_studies(), now,
                       float(self.cfg().get("cooldown_hours") or 6.0))

    def top(self, state: Optional[dict] = None,
            now: Optional[float] = None) -> Optional[dict]:
        items = self.intents(state, now)
        return items[0] if items else None

    # ---- 给提示词的一小段 ------------------------------------------------
    def brief(self, state: Optional[dict] = None) -> dict:
        top = self.top(state)
        last = None
        try:
            last = self.learning.last_study()
        except Exception:
            last = None
        text = ""
        min_i = float(self.cfg().get("min_impulse") or 0.22)
        if top and float(top.get("intensity") or 0) >= min_i:
            why = _short(top.get("why") or "", 40)
            text = ("【你自己在琢磨】\n"
                    f"- 你心里有个问题还没解开：{top.get('question')}\n"
                    f"- {why}\n"
                    "- 这不是他让你查的；你还没得到答案，不要假装已经知道。"
                    "可以提一句，也可以不提。")
            if (last and last.get("question") == top.get("question")
                    and (time.time() - float(last.get("ts") or 0)) < 12 * 3600):
                text += "\n- 你前一阵自己翻过一点材料，但还没审核过，不算你已经知道。"
        return {"intent": top, "text": text,
                "last": last, "pending": self.pending(),
                "thresholds": {"min": self.cfg().get("min_impulse"),
                               "strong": self.cfg().get("strong_impulse")}}

    def pending(self) -> int:
        try:
            return int(self.learning.pending_count())
        except Exception:
            return 0

    # ---- 自己决定去不去查 ------------------------------------------------
    def maybe_study(self, force: bool = False, dry_run: bool = False,
                    background: bool = True, now: Optional[float] = None) -> dict:
        """按学习冲动决定要不要去查。返回这次做了什么，或为什么没做。

        force 只用于手动验证：绕过节律与夜间窗口，但绕不过待审区上限，
        也不改变「只进待审、不自动批准」的闸门。
        """
        if C.LEARNING_PAUSED:
            return {"ok": False, "paused": True,
                    "reason": "学习已暂停（ROBOT_LEARNING_PAUSED=1）；她还在睡"}
        now = now or time.time()
        cfg = self.cfg()
        if not cfg.get("enabled") and not force:
            return {"ok": False, "reason": "自主学习关着（channels.json 的 autonomy.enabled）"}
        pending = self.pending()
        if pending >= int(cfg.get("max_pending") or 60):
            return {"ok": False, "reason": f"待审区已经 {pending} 条，先审再学",
                    "pending": pending}
        top = self.top(now=now)
        if not top:
            return {"ok": False, "reason": "此刻没有形成想弄明白的问题"}
        intensity = float(top.get("intensity") or 0)
        if not force:
            if intensity < float(cfg.get("min_impulse") or 0.26):
                return {"ok": False, "reason": "学习冲动还没过线",
                        "intent": top, "intensity": intensity}
            sessions = self.recent_studies()
            today = datetime.now().strftime("%Y-%m-%d")
            today_n = sum(1 for s in sessions
                          if datetime.fromtimestamp(float(s.get("ts") or 0)).strftime(
                              "%Y-%m-%d") == today)
            if today_n >= int(cfg.get("max_per_day") or 2):
                return {"ok": False, "reason": f"今天已经自己查过 {today_n} 次",
                        "intent": top}
            last = sessions[0] if sessions else None
            if last:
                gap_h = max(0.0, (now - float(last.get("ts") or 0)) / 3600.0)
                if gap_h < float(cfg.get("cooldown_hours") or 6.0):
                    return {"ok": False, "reason": f"刚查过 {gap_h:.1f} 小时，先放一放",
                            "intent": top}
            try:
                in_window = self.learning.in_window()
            except Exception:
                in_window = True
            if not in_window and intensity < float(cfg.get("strong_impulse") or 0.52):
                return {"ok": False, "reason": "不在夜间窗口；冲动还没强到要现在查",
                        "intent": top, "intensity": intensity}
        if dry_run:
            return {"ok": True, "dry_run": True, "intent": top,
                    "would_query": top.get("query"),
                    "note": "只看问题与搜索词，不联网、不写库"}
        if background:
            started = self._start_background(top)
            return {"ok": True, "started": started, "intent": top,
                    "note": "自主学习在后台开跑；结果只进待审区"}
        return self._run_study(top)

    def _start_background(self, intent: dict) -> bool:
        with self._lock:
            if self._busy:
                return False
            self._busy = True
        path = self.learning.path

        def work() -> None:
            lr = None
            try:
                from .learning import Learning
                lr = Learning(db_path=path)
                lr.study_intent(intent, **self._study_kwargs())
            except Exception:
                pass
            finally:
                if lr is not None:
                    try:
                        lr.close()
                    except Exception:
                        pass
                with self._lock:
                    self._busy = False

        threading.Thread(target=work, name="self-study", daemon=True).start()
        return True

    def _study_kwargs(self) -> dict:
        cfg = self.cfg()
        return {"limit": 2, "fetch_budget": int(cfg.get("fetch_budget") or 2)}

    def _run_study(self, intent: dict) -> dict:
        with self._lock:
            if self._busy:
                return {"ok": False, "reason": "已经有一轮自主学习在跑"}
            self._busy = True
        try:
            return self.learning.study_intent(intent, **self._study_kwargs())
        finally:
            with self._lock:
                self._busy = False

    # ---- 常驻 ------------------------------------------------------------
    def run_forever(self, interval_minutes: Optional[float] = None,
                    stop_event: Optional[threading.Event] = None) -> None:
        """服务里的自主自习：每隔一会儿看她此刻有没有长出新问题。

        不阻塞服务：发现冲动后交给后台线程去查，结果只进待审区。
        没冲动、不在窗口、今天次数用完，都只是静默跳过，不报错。
        """
        every = max(60.0, float(
            interval_minutes or C.AUTONOMY_INTERVAL_MINUTES) * 60.0)
        print(f"自主自习：每 {every / 60:.0f} 分钟看一次；结果只进待审区。",
              flush=True)
        while True:
            if stop_event is not None and stop_event.is_set():
                break
            try:
                r = self.maybe_study(background=True)
                if r.get("ok") and r.get("started"):
                    top = r.get("intent") or {}
                    print(f"[{datetime.now():%m-%d %H:%M}] 她自己决定去查："
                          f"{top.get('question') or r.get('question') or '-'}",
                          flush=True)
            except Exception as e:
                print(f"[{datetime.now():%m-%d %H:%M}] 自主自习异常："
                      f"{type(e).__name__}: {e}", flush=True)
            if stop_event is not None:
                if stop_event.wait(every):
                    break
            else:
                time.sleep(every)

    # ---- 总览 ------------------------------------------------------------
    def status(self) -> dict:
        top = self.top()
        try:
            last = self.learning.last_study()
        except Exception:
            last = None
        return {
            "intent": top,
            "last": last,
            "pending": self.pending(),
            "busy": self._busy,
            "config": self.cfg(),
            "recent": self.recent_studies()[:5],
        }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="app.autonomy",
                                 description="自主学习：由情绪/记忆长出的问题")
    ap.add_argument("--status", action="store_true", help="看她此刻在琢磨什么")
    ap.add_argument("--think", action="store_true", help="只看问题与搜索词，不联网")
    ap.add_argument("--run", action="store_true", help="真的去查一次（只进待审区）")
    ap.add_argument("--force", action="store_true", help="绕过节律/夜间窗口（测试用）")
    ap.add_argument("--loop", action="store_true", help="守着，定期自己决定学不学")
    ap.add_argument("--every", type=int, default=30, help="--loop 的检查间隔（分钟）")
    a = ap.parse_args(argv)

    from .learning import Learning
    from .memory import Memory
    from .mind import Mind

    mem, mind, lr = Memory(), Mind(), Learning()
    try:
        au = Autonomy(learning=lr, mem=mem, mind=mind)
        if a.status:
            st = au.status()
            print(f"待审 {st['pending']} 条 · 自习记录 {len(st['recent'])} 条")
            top = st.get("intent")
            if top:
                print(f"此刻最想弄明白：{top.get('question')}  （冲动 {top['intensity']:.2f}）")
                print(f"  为什么：{top.get('why')}")
                print(f"  搜索词：{top.get('query')}")
                print(f"  来源：{json.dumps(top.get('sources') or {}, ensure_ascii=False)}")
                need = float(st.get("config", {}).get("min_impulse") or 0.22)
                if float(top.get("intensity") or 0) < need:
                    print(f"  （还没过会去查的 {need:.2f}，只是心里挂着）")
            else:
                print("此刻没有想弄明白的事。")
            last = st.get("last")
            if last:
                when = datetime.fromtimestamp(float(last["ts"])).strftime("%m-%d %H:%M")
                print(f"上次自习：{when} · {last.get('status')} · "
                      f"「{last.get('question')}」加了 {last.get('added')} 条")
            return 0
        if a.think:
            top = au.top()
            if not top:
                print("此刻没有想弄明白的事。")
                return 0
            print(json.dumps(top, ensure_ascii=False, indent=2))
            return 0
        if a.loop:
            print(f"自主学习守夜：每 {max(5, a.every)} 分钟看一次她的状态；Ctrl+C 停。")
            try:
                while True:
                    r = au.maybe_study(force=a.force, background=False)
                    if r.get("ok"):
                        print(f"[{datetime.now():%H:%M}] 自己去查：{r.get('question')} "
                              f"→ 新增 {r.get('added')} 条 · {r.get('status')}")
                    time.sleep(max(5, a.every) * 60)
            except KeyboardInterrupt:
                print("\n停了。")
                return 0
        r = au.maybe_study(force=a.force, dry_run=not a.run, background=False)
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0 if r.get("ok") else 2
    finally:
        lr.close()
        mem.close()
        mind.close()


if __name__ == "__main__":
    raise SystemExit(main())
