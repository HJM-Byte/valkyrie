"""自检：自主学习——情绪/六欲/记忆 → 问题 → 只进待审区。

不联网、不碰真库：全部用临时目录和假材料。跑法：
    python -m tools.selftest_autonomy
"""
from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path

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


class FakeMem:
    def __init__(self, eps=None, amb=None):
        self.eps = eps or []
        self.amb = amb or {}

    def recent_emotional(self, n: int = 12):
        return self.eps[:n]

    def ambient(self):
        return self.amb


class FakeMind:
    def __init__(self, state):
        self.state = state

    def snapshot(self):
        return dict(self.state)


def main() -> int:
    from app import autonomy as A
    from app import learning as L
    from app import memory as M
    from app import persona as P

    print("— 纯函数：情绪长出问题 —")
    st = {"feel": {"委屈": 0.62, "不安": 0.34},
          "desire_effect": {"top": "意", "frustration": 0.10, "strength": 0.30},
          "turns": 6}
    got = A.intents(st, [], {}, [], [], now=1000.0)
    check("委屈能长出一个问题", bool(got))
    top = got[0] if got else {}
    check("最想弄明白的是委屈", top.get("question") == "人为什么会觉得委屈？", str(top))
    check("冲动过最低线", float(top.get("intensity") or 0) >= 0.26,
          str(top.get("intensity")))
    check("why 里点出情绪，不点记忆原文",
          "委屈" in str(top.get("why")) and "他" not in str(top.get("why")),
          str(top.get("why")))
    check("搜索只用通用关键词", top.get("query") == "委屈 情绪 期待",
          str(top.get("query")))

    pos = A.intents({"feel": {"安心": 0.70}}, [], {}, [], [], now=1000.0)
    check("正面情绪也会想弄明白",
          bool(pos) and "接住" in str(pos[0].get("question")), str(pos[:1]))

    calm = A.intents({"feel": {"平静": 0.30}}, [], {}, [], [], now=1000.0)
    check("平静时不硬造学习冲动", calm == [], str(calm))

    wronged_mem = [{"id": 9, "emo": "委屈", "emo_i": 0.80, "imp": 0.90,
                    "detail": 0.25, "stage": "meaning",
                    "imprint": "你开始不相信承诺", "imprint_a": 0.7}]
    boosted = A.intents(st, wronged_mem, {}, [], [], now=1000.0)
    old_top = next(x for x in got if x.get("question") == top.get("question"))
    new_top = next((x for x in boosted if x.get("question") == top.get("question")), None)
    check("记忆会抬高同一类问题",
          bool(new_top) and float(new_top["intensity"]) > float(old_top["intensity"])
          and 9 in new_top.get("memory_ids", []),
          str(new_top and (new_top["intensity"], new_top.get("memory_ids"))))

    print("— 纯函数：记忆与残响 —")
    mems = [{"id": 7, "emo": "低落", "emo_i": 0.60, "imp": 0.80,
             "detail": 0.20, "stage": "meaning", "imprint": "你开始不信承诺",
             "imprint_a": 0.6, "text": "这条原文只在本机判断，不该出现在搜索词里"},
            {"id": 8, "emo": "平静", "emo_i": 0.10, "imp": 0.30,
             "detail": 0.90, "text": "普通的一天"}]
    mg = A.intents({"feel": {"平静": 0.30}}, mems, {}, [], [], now=1000.0)
    mq = [x for x in mg if x.get("key") == "memory_residue"]
    check("记忆能自己长出一个问题", bool(mq), str([x.get("key") for x in mg]))
    if mq:
        check("记忆问题指向「记不清却还在影响」",
              "记不清" in mq[0]["question"] and "影响" in mq[0]["question"],
              mq[0]["question"])
        check("记忆只带 id，不带原文进来源",
              mq[0]["memory_ids"] == [7] and "原文" not in json.dumps(
                  mq[0]["sources"], ensure_ascii=False), str(mq[0]["sources"]))
        check("搜索词不含记忆原文",
              "这条原文" not in mq[0]["query"] and "开始不信" not in mq[0]["query"],
              mq[0]["query"])
    amb = A.intents({"feel": {"平静": 0.30}}, [], {"intensity": 0.60,
                                                   "unknown": 2, "sources": 3},
                    [], [], now=1000.0)
    check("残响也能长成问题",
          any(x.get("key") == "ambient" for x in amb), str(amb))

    print("— 纯函数：六欲 —")
    dg = A.intents({"feel": {"平静": 0.30},
                    "desire_effect": {"top": "意", "frustration": 0.45,
                                      "strength": 0.60}}, [], {}, [], [],
                   now=1000.0)
    check("想被听懂会变成「为什么想被听懂」",
          any("听懂" in x.get("question", "") for x in dg), str(dg))

    print("— 重复问题降权 —")
    q = top.get("question")
    old = [{"question": q, "ts": 1000.0 - 3600, "status": "studied"}]
    again = A.intents(st, [], {}, [], old, now=1000.0)
    same = next((x for x in again if x.get("question") == q), None)
    check("刚查过的问题仍在想，但先不重复抓",
          same and float(same.get("intensity") or 0) < float(top.get("intensity") or 0)
          and same.get("repeat_penalty") == 0.2, str(same))

    print("— 提示词：只说在琢磨什么 —")
    p = P.Persona.__new__(P.Persona)
    text = P._learning_block({"text": "【你自己在琢磨】\n- 你心里有个问题还没解开：为什么？"})
    check("persona 能插入自主学习块", "在琢磨" in text and "为什么" in text)
    check("没有学习块时不出空段", P._learning_block(None) == "")

    print("— 真实 Learning：入库只进待审 —")
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        L.CHANNELS_FILE = tmp / "channels.json"
        L.BOOKS = tmp / "books"
        L.BOOKS.mkdir(parents=True, exist_ok=True)
        L.FOOTPRINT = tmp / "footprint.jsonl"
        L.BILI_QUEUE = tmp / "bili_queue.json"
        L.DB_PATH = tmp / "learn.db"
        lr = L.Learning(db_path=L.DB_PATH)
        try:
            cfg = lr.config().get("autonomy") or {}
            check("配置里有自主学习一节", isinstance(cfg, dict) and "min_impulse" in cfg,
                  str(cfg))
            check("默认不自动调云端提炼", cfg.get("distill") is False)

            # 假搜索 + 假正文：不联网，验证入库链路
            lr.search = lambda q, limit=4, **k: [
                {"title": "情绪是怎么来的", "url": "https://example.com/1",
                 "snippet": "摘要"},
                {"title": "记忆与感觉", "url": "https://example.com/2",
                 "snippet": "摘要"},
            ]
            lr.fetch_article = lambda url, timeout=10.0: {
                "title": "情绪是怎么来的", "url": url,
                "text": "这是一篇足够长的假材料，用来验证自主学习的入库链路。" * 12,
                "chars": 300, "status": 200}
            lr.baike = lambda kw: None

            fm = FakeMem()
            fmi = FakeMind(st)
            au = A.Autonomy(learning=lr, mem=fm, mind=fmi)
            # 先清空待审区，避免受前面用例影响
            lr._db.execute("DELETE FROM candidates")
            lr._db.execute("DELETE FROM study_sessions")
            lr._db.commit()

            dry = au.maybe_study(force=True, dry_run=True, background=False)
            check("dry-run 只给问题与搜索词，不写库",
                  dry.get("ok") and dry.get("dry_run")
                  and lr.pending_count() == 0
                  and len(lr.recent_studies()) == 0, str(dry))

            r = au.maybe_study(force=True, dry_run=False, background=False)
            check("自主学习真的去查了", r.get("ok") is True and r.get("added") == 2,
                  str(r))
            check("材料只进待审区", lr.pending_count() == 2, str(lr.pending_count()))
            check("没有自动审核", len(lr.approved()) == 0)
            cand = lr.pending(1)[0]
            check("候选标了来源与原因",
                  cand.get("origin") == "self_study"
                  and "委屈" in (cand.get("drive_why") or ""),
                  str(cand))
            check("候选可信度按材料 Q=0.75",
                  abs(float(cand.get("q_recon") or 0) - 0.75) < 1e-6,
                  str(cand.get("q_recon")))
            check("自习记录已写", len(lr.recent_studies()) == 1,
                  str(lr.recent_studies()))
            check("自习记录带来源 JSON", isinstance(lr.last_study().get("sources"), dict))
            from app.mind import GROWTH_LOCKED
            _row = lr.show(cand["id"])
            _ev = str((_row or {}).get("text") or "")[:12]
            lr.set_points(cand["id"], [{"claim": "测试用知识点", "evidence": _ev}])
            approved = lr.approve(cand["id"])
            if GROWTH_LOCKED:
                check("待审区仍拒绝自动通过", approved.get("ok") is False)
            else:
                check("成长开启后可由用户审核通过",
                      approved.get("ok") is True
                      and approved.get("status") == "approved")

            print("— 闸门 —")
            # 待审上限
            cfg = lr.config()
            cfg["autonomy"]["max_pending"] = 1
            lr.save_config(cfg)
            gate = au.maybe_study(force=False, dry_run=False, background=False)
            check("待审区满了先审再学", gate.get("ok") is False
                  and "待审" in str(gate.get("reason")), str(gate))

            print("— 记忆只读接口 —")
            memdb = tmp / "memory.db"
            mem = M.Memory(memdb)
            try:
                mem.add_episode("他说晚点到，后来没来", kind="talk",
                                who="他", imp=0.8, emo="失望", emo_i=0.7,
                                detail=0.4, stage="meaning",
                                imprint="你开始不相信承诺", imprint_a=0.6)
                eps = mem.recent_emotional(5)
                check("recent_emotional 能读出情绪重量",
                      len(eps) == 1 and eps[0]["emo"] == "失望"
                      and eps[0]["emo_i"] == 0.7, str(eps))
                check("recent_emotional 不把原文当搜索词",
                      eps[0]["text"].startswith("他说晚点到"))
            finally:
                mem.close()
        finally:
            lr.close()

    print(f"\n{OK} 通过 / {FAIL} 失败")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
