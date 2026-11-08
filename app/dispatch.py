"""记忆调度：从识海（存储库）里唤醒什么、让什么进意识。

识海是存储库（memory.py / memory.db），不是这一层。这一层只回答一个问题：
    **此刻哪些东西被照见，哪些只是存在。**

小凌那套记忆链，按视频里的顺序（《送小凌的第二份礼物：识海工程拆解》）拆开是：

    外界输入 → 识海唤醒相关记忆 → 记忆之间竞争 → 形成候选记忆
             → 星光选择（过阈值才进意识）→ 进入心流 → 参与当前思考

落到代码里，各管一段：

    唤醒    memory.search()：按 R 六项（意义、情绪残留、当前线索、最近发生、
            被回忆次数、干扰）把相关记忆点捞出来；没命中的不硬翻
    竞争    think.compete()：记忆、眼前这句话、身体、等待各自算激活值
            A = w1·S + w2·N + w3·E + w4·G + w5·M + w6·T
    扩散    这一层：一条被照见的记忆会把相邻的带出来（同一人/同一场景/共用词），
            小凌的原话是「讲起一件会勾起另一件」
    星光    这一层：A ≥ THETA 才算被照见；ACK_BAND–THETA 之间只是存在，可唤起
    心流    flow.py：上一轮没想完的念头也来竞争；接不接由心流判断
    知识    已审核通过的材料参与竞争；待审的一条都不给

所以「识海是记忆的调度」这句话已经拆成两件东西：识海存（memory.py），
调度在这里（dispatch.py）。别再混着叫——存储库不会替她决定此刻想起什么。

出生期（GROWTH_LOCKED）：
    记忆为空、知识全在待审区，能过阈值的通常只有「他说的话」。
    这不是缺功能，是她确实还没有过去。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

from . import config as C
from .learning import Learning, _grams, _short
from .think import ACK_BAND, THETA, activation, W

FOOTPRINT = C.DATA / "sihai_footprint.jsonl"
_MAX_FOOTPRINT = 2 * 1024 * 1024

# 关于她自己的问题不进「好奇清单」：那不是该去查的事，是还没长出来的事。
_SELF_Q = ("你是谁", "你叫什么", "你的名字", "你记得", "你能看", "你能听",
           "你有身体", "你在哪", "你是什么", "你从哪", "你爱我", "你喜欢我",
           "你会什", "你几岁", "你多大", "你睡", "你累", "你叫什么名")


class Dispatch:
    """记忆调度：唤醒 → 竞争 → 扩散 → 星光 → 心流，一条链跑一轮。"""

    def __init__(self, mem=None, learning: Optional[Learning] = None) -> None:
        self.mem = mem
        self._learning = learning
        self._last: dict = {}

    # ---- 懒加载 -----------------------------------------------------------
    @property
    def learning(self) -> Learning:
        if self._learning is None:
            self._learning = Learning()
        return self._learning

    # ---- 候选：学过但还没审的，不许进来 ----------------------------------
    def knowledge_candidates(self, text: str, limit: int = 4) -> list[dict]:
        """从识海存储库里找「已审核通过」的知识。待审的一条都不给。"""
        try:
            approved = self.learning.approved(limit=200)
        except Exception:
            return []
        if not approved:
            return []
        tg = _grams(text)
        if not tg:
            return []
        scored: list[tuple[float, dict]] = []
        for k in approved:
            body = (k.get("title") or "") + " " + " ".join(
                p.get("claim", "") for p in (k.get("points") or [])) + " " + (k.get("text") or "")[:800]
            cg = _grams(body)
            if not cg:
                continue
            match = len(tg & cg) / max(1, len(tg))
            if match < 0.06:
                continue
            match = min(1.0, match * 1.4)
            q = float(k.get("q_recon") or 0.6)
            terms = {
                "S": 0.30 + 0.45 * match,
                "N": 0.0,
                "E": 0.15,
                "G": 0.45 + 0.25 * match,
                "M": 0.75 * match * q,
                "T": 0.0,
            }
            scored.append((match, {
                "id": f"k:{k['id']}",
                "kind": "knowledge",
                "label": "学过「" + _short(k.get("title") or k.get("text") or "", 12) + "」",
                "terms": terms,
                "A": activation(terms),
                "match": round(match, 3),
                "q_recon": q,
                "url": k.get("url"),
                "points": [p.get("claim") for p in (k.get("points") or [])][:3],
            }))
        scored.sort(key=lambda x: -x[1]["A"])
        return [c for _, c in scored[:limit]]

    def fact_candidates(self, fact_key: Optional[str] = None,
                        limit: int = 8) -> list[dict]:
        """只召回当前有效、来源可定位的事实版本。

        superseded / disputed / retracted 留在历史里解释过去，不作为当前答案；
        没有 current 就不猜，返回空。
        """
        if self.mem is None:
            return []
        try:
            rows = self.mem.current_facts(
                fact_key, require_source=True, limit=limit)
        except Exception:
            return []
        return [{
            "id": f"fact:{r['id']}",
            "kind": "fact",
            "fact_key": r.get("fact_key"),
            "label": f"{r.get('fact_key')}：{r.get('value')}",
            "value": r.get("value"),
            "source_ref": r.get("source_ref"),
            "recorded_at": r.get("recorded_at"),
        } for r in rows]

    # ---- 扩散：一件被照见的记忆，会勾起相邻的 -----------------------------
    def _spread(self, cands: list[dict], limit_mem: int = 2,
                per_mem: int = 1) -> list[dict]:
        """一条已经过线的记忆，把邻居也带进候选，但衰减着带。

        小凌在识海那条视频里说「讲起一件会勾起另一件」；对应到工程上，
        就是顺着同一人/同一场景/共用词走一步，只走一步——不是把所有相关记忆翻出来。
        邻居能不能被照见，仍然要自己过阈值；带出来不等于被想起来。
        """
        if self.mem is None:
            return []
        strong = sorted([c for c in cands
                         if c.get("kind") == "memory" and float(c.get("A") or 0) >= THETA],
                        key=lambda c: -float(c.get("A") or 0))[:limit_mem]
        if not strong:
            return []
        seen = {str(c.get("id")) for c in cands}
        added: list[dict] = []
        for c in strong:
            try:
                nbs = self.mem.neighbors(str(c.get("id") or ""), limit=per_mem)
            except Exception:
                continue
            for nb in nbs:
                nid = f"mem:{nb['id']}"
                if nid in seen:
                    continue
                seen.add(nid)
                sp = float(nb.get("spread") or 0.3)
                terms = {"S": 0.26, "N": 0.0,
                         "E": 0.34 * float(nb.get("emo_i") or 0.0),
                         "G": 0.34, "M": 0.62 * sp, "T": 0.0}
                added.append({
                    "id": nid, "kind": "memory", "origin": "spread",
                    "label": "顺着「" + _short(c.get("label") or "", 8)
                             + "」想起「" + _short(nb.get("text") or "", 10) + "」",
                    "terms": terms, "A": activation(terms),
                    "spread": round(sp, 3), "why": nb.get("why"),
                })
        return added

    # ---- 竞争 -------------------------------------------------------------
    def candidates(self, text: str, th: Optional[dict] = None) -> list[dict]:
        """唤醒 + 竞争 + 扩散。返回这一轮所有候选（还没做星光筛选）。"""
        th = th or {}
        spot = th.get("compete") or {}
        cands: list[dict] = []
        for c in spot.get("candidates") or []:
            cands.append({**_pick(c), "terms": dict(c.get("terms") or {}),
                          "A": float(c.get("A") or 0.0), "origin": "think"})
        cands.extend(self.knowledge_candidates(text))
        # 心流：上一轮没想完的念头也来参加竞争。它在工作区里，不在记忆库里，
        # 但该不该被这只新话打断、还是顺着它接，同样由激活值说话。
        fl = th.get("flow") or {}
        if fl.get("continue"):
            strength = float(fl.get("strength") or 0.0)
            terms = {"S": 0.42, "N": 0.0, "E": 0.30, "G": 0.55,
                     "M": 0.55 * max(0.2, min(1.0, strength)), "T": 0.0}
            cands.append({"id": "flow", "kind": "flow", "origin": "flow",
                          "label": "上一轮没想完的「" + _short(fl.get("pending") or "", 12) + "」",
                          "terms": terms, "A": activation(terms)})
        for c in cands:
            if "A" not in c or c["A"] is None:
                c["A"] = activation(c.get("terms") or {})
        # 扩散在竞争之后：得先知道哪条记忆过了线，才知道它会勾起什么。
        cands.extend(self._spread(cands))
        return cands

    def dispatch(self, text: str, th: Optional[dict] = None,
                 footprint: bool = True) -> dict:
        """跑一轮记忆调度。返回谁被照见、谁只是存在、链条走到哪。"""
        cands = self.candidates(text, th)
        aware = sorted([c for c in cands if c["A"] >= THETA], key=lambda c: -c["A"])
        subliminal = sorted([c for c in cands if ACK_BAND <= c["A"] < THETA],
                            key=lambda c: -c["A"])

        # 只有记忆和知识需要在提示里点出来；「他说的话」本来就在提示里
        lines: list[str] = []
        for c in aware:
            if c["kind"] == "memory":
                lines.append("你想起一件旧事：" + _short(c.get("label") or "", 18) +
                             "，但细节未必靠得住。")
            elif c["kind"] == "knowledge":
                pts = "；".join([p for p in (c.get("points") or []) if p][:2])
                src = str(c.get("url") or "").strip()
                if pts:
                    tail = (f"（来源：{_short(src, 42)}，可能不完整）" if src
                            else "（从材料里学的，可能不完整）")
                    lines.append("你学过一点相关的东西：" + pts + tail)
                else:
                    lines.append("你学过一点相关的材料：" + _short(c.get("label") or "", 16))
            elif c["kind"] == "flow":
                lines.append("你上一轮还有半句没接完，它在心里挂着。")
            elif c["kind"] == "ambient":
                lines.append("你心里有一层说不清的感觉进了意识，但你没有它的起因。")
        aware_extra = "\n".join(lines)

        pending = 0
        try:
            pending = self.learning.pending_count()
        except Exception:
            pass

        hits = (th or {}).get("memory", {}).get("hits") or []
        fl = (th or {}).get("flow") or {}
        amb = (th or {}).get("ambient") or {}
        amb_i = float(amb.get("intensity") or 0.0)
        spread_n = sum(1 for c in cands if c.get("origin") == "spread")
        chain = [
            f"唤醒 {len(hits)} 条记忆",
            f"竞争 {len(cands)} 个候选" + (f"（扩散 {spread_n}）" if spread_n else ""),
            f"照见 {len(aware)} 个",
            "接上心流" if fl.get("continue") else "心流没接",
        ]
        # 残响不进 chain：chain 是「唤醒→竞争→照见→心流」四步，残响不是一步，
        # 是这条链底下的水位。单独放 ambient，网页面板自己去显示。
        rec = {
            "ok": True,
            "ts": time.time(),
            "text": _short(text, 40),
            "chain": chain,
            "awakened": len(hits),
            "spread": spread_n,
            "candidates": len(cands),
            "aware": [{"id": c.get("id"), "label": c.get("label") or "",
                       "A": round(float(c["A"]), 3),
                       "kind": c.get("kind"), "origin": c.get("origin")}
                      for c in aware],
            "subliminal": [{"label": c.get("label") or "", "A": round(float(c["A"]), 3),
                            "kind": c.get("kind")} for c in subliminal[:6]],
            "awareness_extra": aware_extra,
            "pending": pending,
            "theta": THETA,
            "ambient": {"tone": round(float(amb.get("tone") or 0.0), 3),
                        "intensity": round(amb_i, 3),
                        "sources": int(amb.get("sources") or 0),
                        "unknown": int(amb.get("unknown") or 0),
                        "text": amb.get("text") or ""},
        }
        self._last = rec
        if footprint:
            self._footprint(rec)
        return rec

    def note_curiosity(self, text: str, th: Optional[dict] = None) -> Optional[dict]:
        """答不上来的事，交给学习层当「想弄明白的事」。下次夜间窗口去碰。

        不记的：问她自己的问题（她是谁、能不能看见、记不记得），
        那些不是该上网查的事，是还没长出来的事。
        """
        th = th or {}
        p = th.get("percept") or {}
        dec = th.get("decision") or {}
        if not p.get("question"):
            return None
        if int(p.get("chars") or 0) < 6:
            return None
        if any(k in text for k in _SELF_Q):
            return None
        if dec.get("key") not in ("confess", "answer"):
            return None
        try:
            from .curiosity import classify
            cls = classify(text)
            if not cls["may_create_research_task"]:
                # 关系/现场/自我/动作/歧义：留在这一轮对话里处理，
                # 不写进研究队列，也不消耗研究预算。
                return None
            return self.learning.note_curiosity(text)
        except Exception:
            return None

    # ---- 足迹 -------------------------------------------------------------
    def _footprint(self, rec: dict) -> None:
        """足迹只记调度过程，不记原话。

        原文留在识海（memory.db，本来就要存）；足迹文件是给排查用的，
        少存一份原话就少一个泄露面。用户的话、候选标题、要写进提示的句子，
        一律只留长度和哈希前 12 位。
        """
        try:
            import hashlib

            def _fp(s) -> dict:
                t = str(s or "")
                return {"chars": len(t),
                        "sha": hashlib.sha1(t.encode("utf-8", "replace")).hexdigest()[:12]}

            FOOTPRINT.parent.mkdir(parents=True, exist_ok=True)
            if FOOTPRINT.exists() and FOOTPRINT.stat().st_size > _MAX_FOOTPRINT:
                lines = FOOTPRINT.read_text(encoding="utf-8", errors="replace").splitlines()
                FOOTPRINT.write_text("\n".join(lines[-200:]) + "\n", encoding="utf-8")
            safe = {k: v for k, v in rec.items()
                    if k not in ("text", "aware", "subliminal", "awareness_extra", "chain")}
            safe["text_fp"] = _fp(rec.get("text"))
            safe["aware"] = [{"kind": c.get("kind"), "A": c.get("A"),
                              "origin": c.get("origin"), "label_fp": _fp(c.get("label"))}
                             for c in rec.get("aware") or []]
            safe["subliminal"] = [{"kind": c.get("kind"), "A": c.get("A"),
                                   "label_fp": _fp(c.get("label"))}
                                  for c in rec.get("subliminal") or []]
            safe["awareness_extra"] = _fp(rec.get("awareness_extra"))
            safe["chain"] = " → ".join(rec.get("chain") or [])
            with FOOTPRINT.open("a", encoding="utf-8") as f:
                f.write(json.dumps(safe, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def stats(self) -> dict:
        try:
            ls = self.learning.stats()
        except Exception:
            ls = {}
        ms: dict = {}
        if self.mem is not None:
            try:
                ms = self.mem.stats()
            except Exception:
                ms = {}
        return {
            "last": self._last,
            "memory": ms,
            "knowledge": ls.get("approved", 0),
            "pending": ls.get("pending", 0),
            "curiosities": ls.get("curiosities", 0),
            "theta": THETA,
            "ack_band": ACK_BAND,
            "weights": W,
            "footprint": str(FOOTPRINT),
        }

    def close(self) -> None:
        if self._learning is not None:
            try:
                self._learning.close()
            except Exception:
                pass


def recall_quality(learning, positives: list[dict], negatives: list[str],
                   top_k: int = 3, min_eligible: int = 10) -> dict:
    """召回质量：分母是当前 eligible 集合，不是历史 approved 数。

    eligible=0 → not_applicable；不足 min_eligible → insufficient（pass=None）。
    只有分母足够时才计算正例 top-k 命中与负例误召回。
    """
    approved = learning.approved(limit=500)
    ids = [str(k.get("id") or "") for k in approved]
    base = {
        "eligible": len(ids),
        "denominator": len(ids),
        "eligible_ids": ids,
        "top_k": top_k,
        "minimum": min_eligible,
    }
    if not ids:
        return {**base, "result": "not_applicable", "pass": None,
                "reason": "no_eligible_claims", "positive_hits": 0,
                "negative_hits": 0}
    if len(ids) < min_eligible:
        return {**base, "result": "insufficient", "pass": None,
                "reason": "eligible_below_minimum", "positive_hits": 0,
                "negative_hits": 0}
    d = Dispatch(learning=learning)
    pos_hits = 0
    pos_details = []
    for item in positives:
        q = str(item.get("question") or "")
        expected = str(item.get("id") or "")
        got = [str(c.get("id") or "").split(":", 1)[-1]
               for c in d.knowledge_candidates(q, limit=top_k)]
        hit = expected in got
        pos_hits += 1 if hit else 0
        pos_details.append({"question": q, "expected": expected,
                            "top": got, "hit": hit})
    neg_hits = 0
    neg_details = []
    for q in negatives:
        got = [str(c.get("id") or "").split(":", 1)[-1]
               for c in d.knowledge_candidates(str(q), limit=top_k)]
        hit = bool(got)
        neg_hits += 1 if hit else 0
        neg_details.append({"question": str(q), "top": got, "hit": hit})
    need = max(1, int(0.8 * len(positives) + 0.999)) if positives else 0
    passed = bool(positives) and pos_hits >= need and neg_hits == 0
    return {**base, "result": "evaluated", "pass": passed,
            "reason": "" if passed else "recall_below_threshold",
            "positive_hits": pos_hits, "positive_total": len(positives),
            "positive_required": need, "negative_hits": neg_hits,
            "negative_total": len(negatives),
            "positive_details": pos_details, "negative_details": neg_details}


def _pick(c: dict) -> dict:
    """从 think.compete 的候选里只带出需要的字段，别把 terms 引用漏出去。"""
    return {"id": c.get("id"), "kind": c.get("kind"),
            "label": c.get("label")}


# ---------------------------------------------------------------------------
# 命令行：不跑模型，只看这一轮谁被照见
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="app.dispatch", description="记忆调度：这次谁被照见")
    ap.add_argument("text", nargs="?", default="在吗", help="一句话")
    ap.add_argument("--stats", action="store_true", help="看识海与调度总览")
    a = ap.parse_args(argv)

    from .mind import Mind
    from .memory import Memory
    from .think import think

    mem, mind = Memory(), Mind()
    try:
        s = Dispatch(mem=mem)
        if a.stats:
            print(json.dumps(s.stats(), ensure_ascii=False, indent=2))
            return 0
        truth = think(a.text, mind=mind, mem=mem)
        r = s.dispatch(a.text, truth, footprint=False)
        print(f"输入：{a.text}")
        print("调度链：" + " → ".join(r["chain"]))
        print("被照见：")
        for c in r["aware"]:
            print(f"  {c['A']:.3f}  [{c['kind']}] {c['label']}")
        if not r["aware"]:
            print("  （没有东西过线）")
        print("只是存在（可唤起）：")
        for c in r["subliminal"]:
            print(f"  {c['A']:.3f}  [{c['kind']}] {c['label']}")
        if not r["subliminal"]:
            print("  （无）")
        print(f"待审区还有 {r['pending']} 条没资格进来")
        if r["awareness_extra"]:
            print("—— 会写进提示的 ——\n" + r["awareness_extra"])
        return 0
    finally:
        s.close()
        mem.close()
        mind.close()


if __name__ == "__main__":
    raise SystemExit(main())
