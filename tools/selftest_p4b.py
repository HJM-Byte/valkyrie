"""P4-B 自检：意义候选、共同章节与修复经验。

    python -m tools.selftest_p4b

只使用 data/acceptance/tmp/ 下的临时库。这里验证的是“派生层不覆盖事实、
不自动确认、来源失效会级联”，不是验证自动写出一段感人故事。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from app.memory import Memory                       # noqa: E402
from tools.acceptance import fixtures               # noqa: E402

PASS = 0
FAIL = 0
DAY = 86400.0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def main() -> int:
    now = time.time()
    with fixtures.temp_workspace("p4b_") as ws:
        mem = ws.open_memory()
        try:
            print("— 意义候选：有来源、候选态、不自动确认 —")
            check("schema 升到 8", mem.stats().get("schema_version") == 8,
                  str(mem.stats()))
            tables = {r["name"] for r in mem._db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            check("意义与章节四张表都在",
                  {"meaning_candidates", "chapters", "chapter_events",
                   "chapter_confirmations"} <= tables, str(sorted(tables)))

            e1 = mem.add_episode("他今天说项目卡住了，我先问他是哪一步卡住",
                                 who="我", source="我说的", imp=0.65,
                                 ts=now - 2 * DAY)
            e2 = mem.add_episode("后来他愿意继续说，我们一起把问题拆开了",
                                 who="我", source="我说的", imp=0.75,
                                 ts=now - DAY)
            e3 = mem.add_episode("他没有继续说，我也不追问", who="我",
                                 source="我说的", imp=0.45, ts=now - 3 * DAY)
            before_eps = mem.stats().get("episodes")
            before_facts = mem.stats().get("facts")

            res = mem.add_meaning_candidate(
                "先确认再猜，比直接给结论更稳",
                "他卡住时，先问清是哪一步，再继续；这不是替他做决定，是少猜一层。",
                episode_ids=[e1, e2], confidence=0.61)
            check("意义候选写入成功", res.get("ok") and res.get("created"), str(res))
            mid = str(res.get("id") or "")
            items = mem.meaning_candidates()
            check("候选默认不是 confirmed",
                  len(items) == 1 and items[0]["status"] == "candidate",
                  str(items))
            check("候选带两条真实来源",
                  items[0]["source_episode_ids"] == [e1, e2], str(items[0]))
            check("候选未确认时 usable=false",
                  items[0]["usable"] is False, str(items[0]))
            check("候选不改 episodes 数量", mem.stats().get("episodes") == before_eps,
                  str(mem.stats()))
            check("候选不改 facts 数量", mem.stats().get("facts") == before_facts,
                  str(mem.stats()))

            dup = mem.add_meaning_candidate(
                "重复标题", "重复正文", episode_ids=[e1, e2])
            check("同一组来源不会重复派生",
                  dup.get("ok") and dup.get("duplicate") and dup.get("id") == mid,
                  str(dup))

            missing = mem.add_meaning_candidate(
                "缺来源", "这条不该写进去", episode_ids=[999999])
            check("缺事件来源被拒", missing.get("ok") is False
                  and missing.get("reason") == "source_missing", str(missing))

            legacy_e = mem.add_episode("一条没有来源的旧式记录", who="", imp=0.5,
                                       ts=now - 5 * DAY)
            before_eps = mem.stats().get("episodes")
            before_facts = mem.stats().get("facts")
            legacy_res = mem.add_meaning_candidate(
                "旧式理解", "没有来源就不确认", episode_ids=[legacy_e])
            legacy_id = str(legacy_res.get("id") or "")
            check("无来源候选带 legacy_unknown",
                  legacy_res.get("ok") and legacy_res.get("legacy_unknown") == 1,
                  str(legacy_res))
            legacy_confirm = mem.set_meaning_status(legacy_id, "confirmed")
            check("无来源候选不能确认",
                  legacy_confirm.get("ok") is False
                  and legacy_confirm.get("reason") == "legacy_unknown",
                  str(legacy_confirm))

            print("— 确认 / 否定 / 撤回都不改事实 —")
            conf = mem.set_meaning_status(mid, "confirmed", "他确认这个理解")
            check("有来源候选可以确认", conf.get("ok") and conf.get("status") == "confirmed",
                  str(conf))
            items = mem.meaning_candidates("confirmed")
            check("确认后可进入叙事", bool(items) and items[0]["usable"] is True,
                  str(items))
            disputed = mem.set_meaning_status(mid, "disputed", "他说不是这样")
            check("用户可否定派生意义",
                  disputed.get("ok") and disputed.get("status") == "disputed",
                  str(disputed))
            check("否定后 usable=false",
                  not mem.meaning_candidates("disputed")[0]["usable"], "")
            retracted = mem.set_meaning_status(mid, "retracted", "不要这段理解")
            check("派生意义可撤回", retracted.get("ok"), str(retracted))
            again = mem.set_meaning_status(mid, "confirmed")
            check("已撤回的意义不能复活",
                  again.get("ok") is False and again.get("reason") == "already_retracted",
                  str(again))
            check("全过程 episodes 原文未被改写",
                  mem.stats().get("episodes") == before_eps,
                  str(mem.stats()))

            print("— 共同章节：至少两条事件 —")
            mid2_res = mem.add_meaning_candidate(
                "确认能让他继续说",
                "先确认他的处境，他才更愿意把问题说下去；这条仍只是候选解释。",
                kind="chapter_meaning", episode_ids=[e1, e2], confidence=0.55)
            mid2 = str(mid2_res.get("id") or "")
            one = mem.add_chapter("只一条", "这不能成章", [e1])
            check("一条事件不能成章",
                  one.get("ok") is False and one.get("reason") == "need_two_events",
                  str(one))
            ch = mem.add_chapter(
                "从卡住到一起拆问题",
                "他卡住时先问清哪一步，后来愿意继续说；这段经历里，确认比猜测有用。",
                [e1, e2], confidence=0.58)
            check("两条真实事件可以成章", ch.get("ok") and ch.get("created"), str(ch))
            cid = str(ch.get("id") or "")
            chapters = mem.chapters()
            check("章节引用两条事件",
                  bool(chapters) and chapters[0]["event_ids"] == [e1, e2], str(chapters))
            check("章节默认是 open", chapters[0]["status"] == "open", str(chapters[0]))
            check("open 不等于用户确认，不能直接进叙事",
                  chapters[0]["usable"] is False and chapters[0]["confirmed"] is False,
                  str(chapters[0]))
            conf_ch = mem.confirm_chapter(cid, "用户确认这段")
            check("用户确认章节", conf_ch.get("ok") and conf_ch.get("confirmed"),
                  str(conf_ch))
            chapters = mem.chapters()
            check("确认后章节才能进入叙事",
                  chapters[0]["usable"] is True and chapters[0]["confirmed"] is True,
                  str(chapters[0]))
            conf2 = mem.set_meaning_status(mid2, "confirmed", "先按这个理解")
            check("第二条意义可确认", conf2.get("ok"), str(conf2))
            block = mem.meaning_block("项目卡住了，我想先确认")
            check("确认后的意义才进提示",
                  "确认能让他继续说" in block, block)
            check("提示里带共同章节",
                  "从卡住到一起拆问题" in block, block)
            mem.set_meaning_status(mid2, "disputed", "不是这样")
            block2 = mem.meaning_block("项目卡住了，我想先确认")
            check("否定后不再带这条意义",
                  "确认能让他继续说" not in block2, block2)
            closed = mem.set_chapter_status(cid, "closed", "这段已经收束")
            check("章节可关闭", closed.get("ok") and closed.get("status") == "closed",
                  str(closed))
            check("关闭后不再进入叙事",
                  not mem.chapters()[0]["usable"], str(mem.chapters()[0]))
            reopened = mem.set_chapter_status(cid, "open")
            check("用户重新提起时可以重开",
                  reopened.get("ok") and reopened.get("status") == "open", str(reopened))

            print("— 来源事件失效：级联检查 —")
            mem.retract(e1, reason="用户纠正：不是他卡住，是我误会了")
            m_after = next((x for x in mem.meaning_candidates() if x["id"] == mid2), {})
            c_after = next((x for x in mem.chapters() if x["id"] == cid), {})
            check("来源事件撤回后意义进入 disputed",
                  m_after.get("status") == "disputed", str(m_after))
            check("来源事件撤回后章节进入 disputed",
                  c_after.get("status") == "disputed", str(c_after))
            check("级联不改事件原文",
                  mem.stats().get("episodes") == before_eps, str(mem.stats()))

            print("— 修复经验只从真实反馈派生 —")
            before_repair = len(mem.meaning_candidates())
            mem.offer_pre_answer("understand", 1, "先接住他的感受",
                                 note="他听起来有点累")
            mem.resolve_pre_answer_feedback("不是这样，我没累")
            mem.offer_pre_answer("understand", 2, "先接住他的感受",
                                 note="他听起来有点累")
            mem.resolve_pre_answer_feedback("谢谢，这次说对了")
            derived = mem.derive_repair_candidates()
            check("派生出一条修复候选", len(derived) == 1 and derived[0].get("created"),
                  str(derived))
            check("修复候选只增一条",
                  len(mem.meaning_candidates()) == before_repair + 1,
                  str(len(mem.meaning_candidates())))
            repair = next(x for x in mem.meaning_candidates()
                          if x["kind"] == "repair")
            check("修复候选引用两条反馈",
                  len(repair["source_feedback_ids"]) == 2, str(repair))
            check("修复候选不是 confirmed",
                  repair["status"] == "candidate", str(repair))
            dup2 = mem.derive_repair_candidates()
            check("重复派生不制造第二条",
                  len(mem.meaning_candidates()) == before_repair + 1, str(dup2))
            first_fb = repair["source_feedback_ids"][0]
            mem.retract_pre_answer_feedback(first_fb, "这条判错了")
            repair2 = next(x for x in mem.meaning_candidates()
                           if x["kind"] == "repair")
            check("反馈撤回后修复候选进入 disputed",
                  repair2["status"] == "disputed", str(repair2))

            print("— 删除事件：级联撤回派生层 —")
            mem.forget("一起把问题拆开")
            m3 = next((x for x in mem.meaning_candidates() if x["id"] == mid2), {})
            c3 = next((x for x in mem.chapters() if x["id"] == cid), None)
            check("删除来源后意义进入 retracted",
                  m3.get("status") == "retracted", str(m3))
            check("删除来源后章节进入 retracted",
                  c3 is not None and c3.get("status") == "retracted", str(c3))
            join = mem._db.execute(
                "SELECT COUNT(*) c FROM chapter_events WHERE chapter_id=? AND ep_id=?",
                (cid, e2)).fetchone()["c"]
            check("删除来源后章节引用已清理", int(join) == 0, str(join))
            check("stats 报出意义/章节计数",
                  mem.stats().get("meaning_candidates", 0) >= 3
                  and mem.stats().get("chapters", 0) >= 1, str(mem.stats()))
        finally:
            mem.close()

    print(f"\n{PASS} 通过 / {FAIL} 失败")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
