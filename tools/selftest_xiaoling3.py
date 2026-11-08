"""自检：三读六视频之后补的四组机制。

    1 遗忘公式化：λ_D = λ0·(1−αE)·(1−βC)·(1+γI)、片段数、Q_recon 重建可信度
    2 心流：有限工作上下文 / 继续要有来处 / 停止要有理由 / 允许被打断
    3 情绪：情绪残留、心情同频、深层不安放大威胁
    4 种子：深层模式被反复经历改写，出生期锁死

不碰真库、不联网（Learning 用桩，Flow 用临时文件）。
跑法：python -m tools.selftest_xiaoling3
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


class _DummyLearning:
    """记忆调度里的学习层桩：不建库、不联网。"""

    def approved(self, limit: int = 200):
        return []

    def pending_count(self) -> int:
        return 0


def main() -> int:
    from app import memory as M
    from app import mind as MI
    from app.flow import Flow
    from app.chat import Chat
    from app.dispatch import Dispatch
    from app.think import _empty_state, think

    print("— 遗忘：意义和细节分开掉 —")
    # 同样三年前、同样重要，情绪强的那条细节掉得慢（λ 被 E 压下来）
    d_strong = M.detail_now(1.0, 3 * 365, imp=0.8, used=0, emo_i=0.9)
    d_plain = M.detail_now(1.0, 3 * 365, imp=0.8, used=0, emo_i=0.1)
    check("情绪强的记忆细节掉得更慢", d_strong > d_plain,
          f"{d_strong:.3f} vs {d_plain:.3f}")
    # 干扰越大，细节掉得越快
    d_noise = M.detail_now(1.0, 3 * 365, imp=0.8, used=0, emo_i=0.9, interference=0.8)
    check("相似记忆多时细节更容易混掉", d_noise < d_strong,
          f"{d_noise:.3f} vs {d_strong:.3f}")
    # 意义几乎不掉：五年后仍然记得重要（同一时刻细节已经快没了）
    s5 = M.meaning_now(0.9, 5 * 365)
    d5 = M.detail_now(1.0, 5 * 365, imp=0.9, used=0)
    check("五年后意义还在、细节已掉光（S 和 D 分开）", s5 > 0.80 and s5 > d5 * 4,
          f"S={s5:.3f} D={d5:.3f}")
    # 片段数：30 个起步，掉到只剩几个，但永远不少于 1
    f_fresh = M.fragments_of(1.0, 1.0)
    f_old = M.fragments_of(1.0, 0.13)
    check("细节完整时是 30 个片段", f_fresh == 30, str(f_fresh))
    check("细节掉光后只剩几个片段且不少于 1", 1 <= f_old <= 6, str(f_old))

    print("— 重建可信度 Q_recon —")
    check("刚发生的事 Q_recon 接近 1", M.recon_now(1.0, 1.0) >= 0.95,
          str(M.recon_now(1.0, 1.0)))
    check("细节掉光的旧事 Q_recon 低于 0.6", M.recon_now(1.0, 0.13) < 0.6,
          str(M.recon_now(1.0, 0.13)))
    check("从别人嘴里听来的起点就低", M.recon_now(0.5, 0.4) < M.recon_now(1.0, 0.8),
          f"{M.recon_now(0.5, 0.4)} vs {M.recon_now(1.0, 0.8)}")

    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        mem = M.Memory(path=tdp / "m.db")
        try:
            old = time.time() - 4 * 365 * 86400
            mem.add_episode("那天他离开的时候，我送他到门口", imp=0.95, emo="委屈",
                            emo_i=0.9, detail=0.9, ts=old)
            # 听来的、当时就没看清的：起点低，重建可信度应该明显更低
            mem.add_episode("听说他那天走得很急，没跟谁说", imp=0.7, emo="委屈",
                            emo_i=0.7, detail=0.3, ts=old + 3600)
            mem.add_episode("今天中午吃了面", imp=0.2, ts=time.time() - 3600)
            hit = mem.search("那天他离开", k=1)
            check("召回带出片段数与 Q_recon",
                  hit and "fragments" in hit[0] and "q_recon" in hit[0],
                  str(hit[:1]))
            check("旧事 Q_recon 已降下来", float(hit[0]["q_recon"]) < 0.8,
                  str(hit[0]["q_recon"]))
            blk = mem.episodes_block("他离开那天", k=2)
            check("旧事进提示时说话留余地",
                  ("拼个大概" in blk) or ("细节记不太清了" in blk) or ("只剩" in blk),
                  blk[:100])
            blk2 = mem.episodes_block("听说他那天走得很急", k=2)
            check("低起点/重建记忆会明说「拼个大概」",
                  "拼个大概" in blk2 or "不确定" in blk2, blk2[:100])
            # 回忆不是免费的：细节不足的记忆被反复想起，可信度会再降一点
            before = mem._db.execute(
                "SELECT recon FROM episodes WHERE text LIKE '%听说他那天%'"
            ).fetchone()["recon"]
            for _ in range(3):
                mem.search("听说他那天走得很急", k=1, reinforce=True)
            after = mem._db.execute(
                "SELECT recon FROM episodes WHERE text LIKE '%听说他那天%'"
            ).fetchone()["recon"]
            check("反复回忆会让重建可信度继续下降（原文不动）", after < before,
                  f"{before} → {after}")
        finally:
            mem.close()

    print("— 心流：接得上、停得下、打断得了 —")
    with tempfile.TemporaryDirectory() as td:
        fp = Path(td) / "flow.json"
        f = Flow(path=fp)
        c0 = f.context("我在想记忆到底是怎么保存的")
        check("还没有念头时不硬接", c0["continue"] is False and c0["reason"] == "new",
              str(c0["reason"]))
        a1 = f.advance("我在想记忆到底是怎么保存的", progress=0.5, pending="记忆到底怎么保存")
        check("有进展就牵起一条思绪", a1["strength"] >= 0.25, str(a1))
        c1 = f.context("那它和遗忘是什么关系")
        check("相关的下一句能接上", c1["continue"] is True and c1["block"],
              f"{c1['reason']} {c1['strength']}")
        check("继续时给了只接一层的规矩", "一小步" in (c1.get("rule") or ""),
              c1.get("rule", "")[:40])
        c2 = f.context("今天天气挺好的")
        check("无关的话不硬接", c2["continue"] is False, c2["reason"])
        c3 = f.context("那它和遗忘是什么关系", event_a=0.95)
        check("更重要的事出现时允许被打断", c3["interrupt"] is True, str(c3["reason"]))
        a3 = f.advance("那它和遗忘是什么关系", progress=0.4,
                       pending="遗忘和保存的关系", interrupt=True)
        check("被打断后记住了这次接的急事", a3.get("closed") == "interrupted",
              str(a3))
        # 停滞：连续两轮没有进展就停
        f2 = Flow(path=Path(td) / "flow2.json")
        f2.advance("想一件没想明白的事", progress=0.5, pending="没想明白的事")
        f2.advance("嗯", progress=0.0)
        closed = f2.advance("嗯", progress=0.0)
        check("连续两轮没进展就停", closed.get("closed") == "stalled", str(closed))
        check("停了以后状态是关着的", f2.status()["open"] is False)
        # 过期
        f3 = Flow(path=Path(td) / "flow3.json")
        t0 = time.time()
        f3.advance("在想一个问题", progress=0.5, pending="问题", now=t0)
        c4 = f3.context("接着刚才那个问题", now=t0 + 7 * 3600)
        check("隔了 6 小时以上就散了", c4["reason"] == "expired", c4["reason"])

    print("— 情绪：残留、同频、深层不安 —")
    st_calm = _empty_state()
    hot = [{"id": 1, "text": "那天他离开", "emo": "委屈", "emo_i": 0.9,
            "act": 1.6, "q_recon": 0.8}]
    # 直接比较 appraise 的两条路：有情绪残留 vs 没有
    from app.think import appraise, perceive
    p = perceive("我今天有点难受", st_calm)
    ap_plain = appraise(p, st_calm, [])
    ap_hot = appraise(p, st_calm, hot)
    check("想起旧事会抬高情绪强度（情绪残留）", ap_hot["E"] > ap_plain["E"],
          f"{ap_hot['E']} vs {ap_plain['E']}")
    st_low = {**st_calm, "mood": -0.4}
    st_high = {**st_calm, "mood": 0.4}
    ap_low = appraise(p, st_low, [])
    ap_high = appraise(p, st_high, [])
    check("心情低时同样的话更沉（情绪同频）", ap_low["E"] > ap_high["E"],
          f"{ap_low['E']} vs {ap_high['E']}")
    # 深层 absence_bias 放大威胁与失望
    st_gap = {**st_calm, "gap_shock": 0.7,
              "deep": {"absence_bias": 0.7, "absence_sensitivity": 0.6,
                       "social_pred_bias": -0.2, "trust_update_bias": 0.0}}
    ap_deep = appraise(p, st_gap, [])
    ap_nogap = appraise(p, st_gap | {"deep": {}}, [])
    check("深层不安放大「又被晾着」的威胁", ap_deep["T"] > ap_nogap["T"],
          f"{ap_deep['T']} vs {ap_nogap['T']}")
    check("深层不安也放大失望", ap_deep["disappointment"] > ap_nogap["disappointment"],
          f"{ap_deep['disappointment']} vs {ap_nogap['disappointment']}")
    # 低 Q_recon 的记忆 → 提示里要求留余地
    class _MemStub:
        def search(self, *a, **kw):
            return [{"id": 1, "text": "听说他那天走了", "emo": "委屈", "emo_i": 0.6,
                     "act": 0.9, "q_recon": 0.2, "detail_now": 0.1, "recon": 0.5,
                     "stage": "segment", "claim": "quote", "day": "2023-01-01"}]

    thq = think("你还记得他离开那天吗", state=st_calm, mem=_MemStub())
    check("想起的事细节不可靠时，规矩要求说个大概",
          "大概" in thq["decision"]["rule"], thq["decision"]["rule"][:80])

    print("— 种子：反复经历改的是深层倾向 —")
    with tempfile.TemporaryDirectory() as td:
        mind = MI.Mind(path=Path(td) / "m.db")
        try:
            _seed = mind.imprint([{"text": "他说来没来"}, {"text": "又没来"}])
            if MI.GROWTH_LOCKED:
                check("出生期不允许沉淀深层模式", _seed["ok"] is False)
            else:
                check("成长开启后允许沉淀深层模式", _seed["ok"] is True)
            saved = MI.GROWTH_LOCKED
            MI.GROWTH_LOCKED = False
            try:
                mind.observe("你好", "在")
                mind.observe("在吗", "嗯")
                eps = [{"text": "他说好要来，没来"}, {"text": "又失约了"},
                       {"text": "答应了却没出现"}]
                r = {}
                for _ in range(12):      # 反复发生才沉淀；一两次只算巧合
                    r = mind.imprint(eps)
                check("反复失约后长出深层不安", r["ok"] and "deep" in r, str(r)[:80])
                deep = mind.deep()
                check("absence_bias 被抬起来", deep["absence_bias"] > 0, str(deep))
                text = mind.state_text()
                check("长出来以后状态里会说出这层感觉",
                      "不告而别" in text, text[:80])
                snap = mind.snapshot()
                check("快照里带深层模式四个量",
                      set(snap["deep"]) >= {"absence_bias", "trust_update_bias",
                                            "social_pred_bias", "absence_sensitivity"})
            finally:
                MI.GROWTH_LOCKED = saved
        finally:
            mind.close()

    print("— 接进对话：提示里真的出现了心流 —")
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        # 自检不许碰她真正的休眠标记：拼提示会读 sleep，但绝不该消费
        import app.sleep as _sleepmod
        saved_sleep = (_sleepmod.SLEEP_FILE, _sleepmod.SLEEP_LOG)
        _sleepmod.SLEEP_FILE = tdp / "sleep.json"
        _sleepmod.SLEEP_LOG = tdp / "sleep_log.jsonl"
        # 调度足迹也不许写进真实文件
        from app import dispatch as _dispmod
        saved_fp = _dispmod.FOOTPRINT
        _dispmod.FOOTPRINT = tdp / "footprint.jsonl"
        mem = M.Memory(path=tdp / "m.db")
        mind = MI.Mind(path=tdp / "m.db")
        try:
            c = Chat(mem=mem, mind=mind)
            c.dispatch = Dispatch(mem=mem, learning=_DummyLearning())
            c.flow = Flow(path=tdp / "flow.json")
            c.flow.advance("我在想记忆到底是怎么保存的", progress=0.5,
                           pending="记忆到底怎么保存")
            prompt, sal, th = c.compose("那遗忘和记忆是什么关系")
            check("这一轮接上了心流", th["flow"]["continue"] is True,
                  str(th["flow"].get("reason")))
            check("提示里带上了「接着想」的要求",
                  "接着" in prompt or "上一轮" in prompt, prompt[-160:])
            check("思维链里看得到心流那一行",
                  any("心流" in line for line in th["trace"]), str(th["trace"][-2:]))
            check("记忆调度候选里有多出来的「没想完的念头」",
                  any(x.get("kind") == "flow" for x in c.dispatch.candidates("那遗忘是什么", th)),
                  str([x.get("kind") for x in c.dispatch.candidates("那遗忘是什么", th)]))
        finally:
            _sleepmod.SLEEP_FILE, _sleepmod.SLEEP_LOG = saved_sleep
            _dispmod.FOOTPRINT = saved_fp
            mem.close()
            mind.close()

    print(f"\n{OK} 通过 / {FAIL} 失败")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
