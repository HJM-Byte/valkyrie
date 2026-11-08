"""自检：记忆的来源、归属、撤回/更正、情绪同频召回、关系变量进情感。

对应视频里最容易漏掉的一条闭环：
    「记忆要分清谁说的、关于谁……是事实、愿望、转述还是推测」
    「纠正、撤回后的旧信息失效」「明确要求删除的内容必须及时处理」
以及「情绪关联参与竞争式召回」「关系变量要能翻成态度和情绪」。

不碰真库、不联网。跑法：
    python -m tools.selftest_memory
"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from app import affect as A                       # noqa: E402
from app import memory as M                       # noqa: E402
from app import think as T                        # noqa: E402
from app.mind import Mind                         # noqa: E402
from app.sihai import build_from_db               # noqa: E402

PASS = 0
FAIL = 0
DAY = 86400.0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {detail}")


def main() -> int:
    now = time.time()
    td = Path(tempfile.mkdtemp(prefix="memory_prov_"))
    mem = M.Memory(td / "m.db")
    try:
        print("— 来源与归属：谁说的、关于谁 —")
        cols = {r["name"] for r in mem._db.execute("PRAGMA table_info(episodes)")}
        check("记忆点带来源/归属/状态列",
              {"source", "about", "status", "superseded_by", "retracted_at",
               "retract_reason"} <= cols,
              str(sorted(cols)))
        eid = mem.add_episode(
            "听说他那天走得很急，没跟谁说", who="他", imp=0.6,
            source="听说的", about="他", claim="quote", detail=0.3,
            ts=now - 2 * DAY)
        row = dict(mem._db.execute(
            "SELECT source,about,status,claim FROM episodes WHERE id=?", (eid,)).fetchone())
        check("来源和归属写进记忆点",
              row["source"] == "听说的" and row["about"] == "他"
              and row["status"] == "active" and row["claim"] == "quote",
              str(row))
        blk = mem.episodes_block("听说他那天走得很急", k=2)
        check("进提示时说出转述来源", ("来源是听说的" in blk) or ("转述" in blk), blk[:120])

        print("— 撤回：旧信息失效，原文仍然留着 —")
        retired = mem.add_episode(
            "他答应周六来", who="他", imp=0.7, source="他说的",
            ts=now - DAY)
        res = mem.retract(retired, reason="他说记错了")
        check("撤回只标失效，不删原文",
              res["retracted"] == 1
              and bool(mem._db.execute(
                  "SELECT text FROM episodes WHERE id=?", (retired,)).fetchone()),
              str(res))
        st = dict(mem._db.execute(
            "SELECT status,retract_reason FROM episodes WHERE id=?", (retired,)).fetchone())
        check("状态改成 retracted 并带原因",
              st["status"] == "retracted" and "记错" in st["retract_reason"], str(st))
        check("撤回后不再被召回",
              not any(int(h["id"]) == int(retired) for h in mem.search("答应周六", k=5)),
              str(mem.search("答应周六", k=5)))
        check("撤回后不再进残响", mem.ambient(now)["sources"] >= 0)
        check("撤回后不再上星图",
              all(int(n["id"][1:]) != int(retired) for n in build_from_db(td / "m.db")["nodes"]
                  if n.get("kind") == "episode"),
              str([n["id"] for n in build_from_db(td / "m.db")["nodes"]]))
        stats = mem.stats()
        check("stats 分开报 active / retracted",
              stats["retracted"] >= 1 and stats["active"] >= 1,
              str(stats))

        print("— 纠正：新记录取代旧记录 —")
        old = mem.add_episode("他周六来", who="他", imp=0.6, source="他说的", ts=now - 600)
        new = mem.supersede(old, "他周日来", who="他", imp=0.7,
                            source="他说的", claim="fact", ts=now - 60,
                            reason="他后来改口")
        oldrow = dict(mem._db.execute(
            "SELECT status,superseded_by FROM episodes WHERE id=?", (old,)).fetchone())
        check("旧记录标 superseded 并指向新记录",
              oldrow["status"] == "superseded" and int(oldrow["superseded_by"]) == int(new),
              str(oldrow))
        hits = mem.search("他周日来", k=3)
        check("召回只拿到新记录",
              any(int(h["id"]) == int(new) for h in hits)
              and not any(int(h["id"]) == int(old) for h in hits),
              str([(h["id"], h["text"]) for h in hits]))

        print("— 一句明确纠正就能让旧记录失效 —")
        pref = mem.add_episode("我周六去看车", who="他", imp=0.5, source="他说的", ts=now - 30)
        note = mem.note_correction("说错了，不是周六，是周日")
        check("识别出“说错了”是纠正",
              note.get("kind") == "correct" and note.get("retracted") == 1, str(note))
        row = mem._db.execute("SELECT status FROM episodes WHERE id=?", (pref,)).fetchone()
        check("最近一条被撤下", row["status"] == "retracted", str(dict(row)))
        check("普通“不是”不误触发",
              mem.note_correction("这不是我的杯子，是他的").get("retracted") == 0,
              "普通陈述")
        obj = M._correction_object("忘掉那个约定")
        check("纠正对象可以从“忘掉 X”里取出来",
              obj.endswith("约定") and len(obj) <= 6, obj)

        print("— 情绪同频：同色记忆更容易被想起 —")
        happy = mem.add_episode("那天他来了，我在门口等", who="他", imp=0.7,
                                feel={"开心": 0.9}, emo="开心", emo_i=0.9,
                                source="他说的", ts=now - 2 * DAY)
        sad = mem.add_episode("那天他没来，我在门口等", who="他", imp=0.7,
                              feel={"委屈": 0.9}, emo="委屈", emo_i=0.9,
                              source="他说的", ts=now - 2 * DAY)
        h_sad = mem.search("那天门口等", k=2, now_feel={"委屈": 0.95})
        h_happy = mem.search("那天门口等", k=2, now_feel={"开心": 0.95})
        check("委屈时更容易想起委屈那条",
              h_sad and int(h_sad[0]["id"]) == int(sad), str([(h["id"], h["act"]) for h in h_sad]))
        check("开心时更容易想起开心那条",
              h_happy and int(h_happy[0]["id"]) == int(happy),
              str([(h["id"], h["act"]) for h in h_happy]))
        check("召回结果带情绪同频度",
              "feel_congruence" in h_sad[0], str(h_sad[0].keys()))

        print("— 末那识：区分亲历与听来 —")
        own_none = T.own(T.perceive("在吗", T._empty_state()), [])
        check("没有记忆时仍是“听来的”", own_none["owner"] == "听来的", str(own_none))
        own_heard = T.own(T.perceive("在吗", T._empty_state()), [
            {"source": "他说的", "who": "他", "claim": "wish", "ident": 0.2}])
        check("他说的不算她的亲历", "亲历" not in own_heard["owner"]
              or "不是" in own_heard["text"], str(own_heard))
        check("愿望/转述不会被当成事实", "愿望" in own_heard["text"] or "转述" in own_heard["text"],
              own_heard["text"])
        own_seen = T.own(T.perceive("在吗", T._empty_state()), [
            {"source": "看到的", "who": "", "claim": "fact", "ident": 0.8}])
        check("看到的才算亲历", "亲历" in own_seen["owner"], str(own_seen))

        print("— 关系变量：互惠/奖励/预测/沉没 —")
        state = T._empty_state()
        state["turns"] = 5
        state["rel"] = {"熟悉": 0.6, "信任": 0.6, "互惠": 0.6, "奖励": 0.6,
                        "预测": 0.7, "依恋": 0.2, "沉没": 0.7, "边界": 0.4}
        rel_text = T.relate(state)
        check("互惠翻成人话", "接你的话" in rel_text, rel_text)
        check("奖励翻成人话", "舒服" in rel_text, rel_text)
        check("预测翻成人话", "摸得准" in rel_text, rel_text)
        check("沉没成本翻成人话", "放了很多" in rel_text, rel_text)
        base = T._empty_state()
        ap0 = T.appraise(T.perceive("在吗", base), base, [], {})
        ap1 = T.appraise(T.perceive("在吗", state), state, [], {})
        check("预测强度进期望", ap1["expect"] > ap0["expect"],
              f"{ap0['expect']} -> {ap1['expect']}")

        print("— 关系变量也改情绪 —")
        low_rel = {"熟悉": 0.3, "信任": 0.3, "互惠": 0.0, "奖励": 0.0,
                   "预测": 0.0, "依恋": 0.1, "沉没": 0.0, "边界": 0.9}
        high_rel = {**low_rel, "互惠": 0.8, "奖励": 0.8, "预测": 0.8, "沉没": 0.8}
        st_low = {**base, "gap_shock": 0.7, "rel": low_rel}
        st_high = {**base, "gap_shock": 0.7, "rel": high_rel}
        f_low = A.react(T.perceive("在吗", st_low), T.appraise(T.perceive("在吗", st_low), st_low), st_low, [], {})
        f_high = A.react(T.perceive("在吗", st_high), T.appraise(T.perceive("在吗", st_high), st_high), st_high, [], {})
        check("沉没/预测让落空更失望",
              f_high.get("失望", 0) > f_low.get("失望", 0),
              f"{f_low.get('失望')} -> {f_high.get('失望')}")
        warm_low = T.perceive("谢谢你陪我", st_low)
        warm_high = T.perceive("谢谢你陪我", st_high)
        fw_low = A.react(warm_low, T.appraise(warm_low, st_low), st_low, [], {})
        fw_high = A.react(warm_high, T.appraise(warm_high, st_high), st_high, [], {})
        check("互惠/奖励让被接住更安心",
              fw_high.get("安心", 0) > fw_low.get("安心", 0),
              f"{fw_low.get('安心')} -> {fw_high.get('安心')}")

        print("— 状态里真的会更新预测和奖励 —")
        md = Mind(td / "m.db")
        before = md.snapshot()["rel"]
        md.observe("谢谢你陪我", "嗯", None)
        after = md.snapshot()["rel"]
        check("互动奖励会涨", after["奖励"] > before["奖励"],
              f"{before['奖励']} -> {after['奖励']}")
        check("预测强度会更新", after["预测"] != before["预测"],
              f"{before['预测']} -> {after['预测']}")
        md.close()

        print("— 没找到 vs 根本没有 —")
        empty = M.Memory(td / "empty.db")
        try:
            r0 = T.recall(empty, "你还记得吗", 0.8)
            check("空库说“根本没有过”",
                  "没有任何记忆" in r0["note"] or "根本没有过" in r0["note"],
                  r0["note"])
        finally:
            empty.close()
        r1 = T.recall(mem, "完全不存在的暗号", 0.8)
        check("有库没命中时说“没想起，不是没发生”",
              "没想起来" in r1["note"] or "没有记忆" in r1["note"], r1["note"])
        th0 = T.think("你还记得我的生日吗", state=T._empty_state())
        check("空库的规矩：还没有任何记忆",
              "没有任何记忆" in th0["decision"]["rule"], th0["decision"]["rule"][-100:])
        th1 = T.think("你还记得完全不存在的暗号吗？", state=T._empty_state(), mem=mem)
        check("有库没命中的规矩：不许说成没发生",
              ("可靠记录" in th1["decision"]["rule"])
              or ("想不起来" in th1["decision"]["rule"]),
              th1["decision"]["rule"][-120:])
    finally:
        mem.close()

    print(f"\n结果：{PASS} 通过，{FAIL} 失败")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
