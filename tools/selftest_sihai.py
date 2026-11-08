"""自检：识海（存储库）+ 记忆调度 + 休眠补精力。

    识海    存：记忆点带属性、潜伏、只推 stage 不删原文
    调度    唤醒 → 竞争 → 扩散 → 星光 → 心流，一条链跑完
    休眠    封存之后补精力，不改封存时刻

不碰真库、不联网（Learning 用桩，休眠文件指到临时目录）。
跑法：python -m tools.selftest_sihai
"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

OK = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


class _Learn:
    """学习层桩：不建库、不联网。"""

    def __init__(self, approved=None, pending: int = 0) -> None:
        self._approved = list(approved or [])
        self._pending = pending

    def approved(self, limit: int = 200):
        return self._approved[:limit]

    def pending_count(self) -> int:
        return self._pending

    def stats(self) -> dict:
        return {"approved": len(self._approved), "pending": self._pending,
                "curiosities": 0}

    def close(self) -> None:
        pass


def main() -> int:
    from app import memory as M
    from app import mind as MI
    from app import sleep as S
    from app.dispatch import Dispatch
    from app.think import _empty_state, think

    print("— 识海：存的是带属性的记忆点，不是聊天记录 —")
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        mem = M.Memory(path=tdp / "m.db")
        try:
            now = time.time()
            mem.add_episode("他答应周末一起去看海", kind="talk", who="他", imp=0.95,
                            ts=now - 3600, scene="家里", emo="开心", emo_i=0.9,
                            detail=1.0, rel=0.9, ident=0.5, claim="wish")
            mem.add_episode("那天我们在海边待到很晚", kind="talk", who="他", imp=0.8,
                            ts=now - 3000, scene="海边", emo="开心", emo_i=0.8,
                            detail=0.8, rel=0.8, ident=0.4)
            mem.add_episode("今天中午吃了面", imp=0.2, ts=now - 1800, scene="厨房")
            mem.add_fact("名字", "测试用户", conf=0.95)
            stats = mem.stats()
            check("三条记忆点都在", stats["episodes"] == 3, str(stats["episodes"]))
            check("事实单独存", stats["facts"] == 1, str(stats["facts"]))
            check("潜伏阶段分开记（三条 raw）",
                  stats["stages"].get("raw") == 3, str(stats.get("stages")))
            row = dict(mem._db.execute(
                "SELECT * FROM episodes WHERE text='他答应周末一起去看海'").fetchone())
            args = ("who", "scene", "emo", "imp", "rel", "ident", "detail", "recon", "claim")
            check("记忆点带着属性（人物/场景/情绪/意义/关系/身份/细节/可信度/性质）",
                  row["who"] == "他" and row["scene"] == "家里" and row["emo"] == "开心"
                  and abs(float(row["imp"]) - 0.95) < 1e-6 and float(row["rel"]) > 0.8
                  and float(row["ident"]) == 0.5 and float(row["detail"]) == 1.0
                  and float(row["recon"]) == 1.0 and row["claim"] == "wish",
                  str({k: row[k] for k in args}))
            check("愿望不被记成已发生的事实", row["claim"] == "wish", row["claim"])
            nb = mem.neighbors(f"mem:{row['id']}", limit=2)
            check("顺着一条记忆能找到相邻的（扩散的前提）",
                  bool(nb) and all(n["why"] in ("同一个人", "同一场景", "共用词") for n in nb),
                  str([(n["text"], n["why"], n["spread"]) for n in nb]))
            old_ts = now - 200 * 86400
            mem.add_episode("很久以前一件小事", imp=0.30, detail=1.0, ts=old_ts)
            mem.settle(keep_days=0)
            old_row = dict(mem._db.execute(
                "SELECT text,stage FROM episodes WHERE text='很久以前一件小事'").fetchone())
            check("生命周期只推 stage，不删原文",
                  old_row["text"] == "很久以前一件小事" and old_row["stage"] != "raw",
                  str(old_row))
        finally:
            mem.close()

    print("— 记忆调度：唤醒 → 竞争 → 扩散 → 星光 → 心流 —")
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        mem = M.Memory(path=tdp / "m.db")
        try:
            now = time.time()
            mem.add_episode("他答应周末一起去看海", kind="talk", who="他", imp=0.95,
                            ts=now - 3600, scene="家里", emo="开心", emo_i=0.9,
                            detail=1.0, rel=0.9, ident=0.5)
            # 同一个人，但和这句话没有词面重合、重要度也不够自己浮上来——
            # 只有被上一条「勾起」才可能出现。
            mem.add_episode("那天他走的时候没回头", kind="talk", who="他", imp=0.5,
                            ts=now - 30000, scene="门口", emo="委屈", emo_i=0.6,
                            detail=0.8, rel=0.6, ident=0.3)
            mem.add_episode("今天中午吃了面", imp=0.2, ts=now - 1800)
            st = _empty_state()
            th = think("他答应周末一起去看海", state=st, mem=mem)
            si = Dispatch(mem=mem, learning=_Learn(pending=2))
            cands = si.candidates("他答应周末一起去看海", th)
            disp = si.dispatch("他答应周末一起去看海", th, footprint=False)
            check("唤醒：想起了相关记忆",
                  bool(th["memory"]["hits"]) and float(th["memory"]["activation"]) > 0,
                  str(th["memory"].get("note")))
            check("唤醒的记忆带着 R / Q_recon / 细节",
                  {"R", "q_recon", "detail_now"} <= set(th["memory"]["hits"][0]),
                  str(sorted(th["memory"]["hits"][0].keys()))[:140])
            check("竞争：候选里有记忆点",
                  any(c["kind"] == "memory" for c in cands),
                  str([(c["kind"], c.get("origin")) for c in cands]))
            check("扩散：被照见的记忆勾起了相邻的一条",
                  disp["spread"] >= 1 and
                  any(c.get("origin") == "spread" for c in cands),
                  str([(c.get("origin"), c.get("label")) for c in cands]))
            check("星光：四步链条齐全",
                  len(disp["chain"]) == 4 and "唤醒" in disp["chain"][0]
                  and "照见" in disp["chain"][2] and "心流" in disp["chain"][3],
                  str(disp["chain"]))
            check("阈值：照见的都在 θ 之上，存在的都在 0.14–θ",
                  all(c["A"] >= disp["theta"] for c in disp["aware"])
                  and all(0.14 <= c["A"] < disp["theta"] for c in disp["subliminal"]),
                  f"{disp['aware']} | {disp['subliminal']}")
            check("待审知识没资格进来", disp["pending"] == 2, str(disp["pending"]))
            check("候选里没有待审材料",
                  all("还没审" not in (c.get("label") or "") for c in cands))
            # 心流也来竞争
            th_flow = {"flow": {"continue": True, "pending": "记忆怎么保存", "strength": 0.6}}
            check("心流：没想完的念头也进候选",
                  any(c["kind"] == "flow"
                      for c in si.candidates("那记忆呢", th_flow)))
            # 已审核知识能进调度
            approved = [{
                "id": "k1", "title": "记忆的保存", "url": "u",
                "text": "记忆会随时间掉细节，情绪强的掉得慢",
                "points": [{"claim": "记忆的细节会随时间变淡", "evidence": "细节会掉"}],
                "q_recon": 0.8,
            }]
            si2 = Dispatch(mem=mem, learning=_Learn(approved=approved))
            disp2 = si2.dispatch("记忆是怎么保存的", {}, footprint=False)
            kinds2 = {c["kind"] for c in disp2["aware"] + disp2["subliminal"]}
            check("已审核的知识参与调度", "knowledge" in kinds2, str(kinds2))
            check("知识被照见时会写进提示的原句",
                  "记忆的细节" in (disp2.get("awareness_extra") or "")
                  or disp2.get("awareness_extra") is not None,
                  (disp2.get("awareness_extra") or "")[:80])
        finally:
            mem.close()

    print("— 休眠：封存以后补精力，不改封存时刻 —")
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        saved = (S.SLEEP_FILE, S.SLEEP_LOG)
        S.SLEEP_FILE, S.SLEEP_LOG = tdp / "sleep.json", tdp / "sleep_log.jsonl"
        mem = M.Memory(path=tdp / "m.db")
        mind = MI.Mind(path=tdp / "m.db")
        try:
            mind._write(energy=0.35)
            rec = S.hibernate(mind, mem, reason="自检", wake_when="新摄像头到货后")
            ts0 = float(rec["ts"])
            check("封存的是当下的精力（不是补过的）",
                  abs(float(rec["state"]["energy"]) - 0.35) < 0.02,
                  str(rec["state"]["energy"]))
            r = S.topup(mind=mind, mem=mem)
            check("补精力成功", bool(r.get("ok")) and r["rest"]["after"] == 1.0,
                  str(r.get("rest")))
            rec2 = S.status()
            check("封存时刻没被改掉", abs(float(rec2["ts"]) - ts0) < 1e-6,
                  f"{rec2['ts']} vs {ts0}")
            check("快照里的精力已补满", float(rec2["state"]["energy"]) == 1.0,
                  str(rec2["state"]["energy"]))
            check("叫醒条件没被覆盖", rec2.get("wake_when") == "新摄像头到货后",
                  str(rec2.get("wake_when")))
            check("休眠记录还在（没被当成醒来消费）", S.SLEEP_FILE.exists())
            S.SLEEP_FILE.unlink()
            r2 = S.topup(mind=mind, mem=mem)
            check("醒着的时候补精力会被拒绝", r2.get("ok") is False, str(r2))
        finally:
            S.SLEEP_FILE, S.SLEEP_LOG = saved
            mem.close()
            mind.close()

    print(f"\n{OK} 通过 / {FAIL} 失败")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
