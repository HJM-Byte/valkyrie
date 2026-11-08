"""P3 残余自检：关系气候、未竟承诺、正式沉默。

    python -m tools.selftest_p3

只用 `data/acceptance/tmp/` 下的临时库；不打开生产 memory.db / p3.db。
重点不是“像不像人”，而是三件事不能混：

1. 关系气候是中速判断：单次互动不改变它，必须有多次事件、跨日与反证；
2. 承诺有候选/有效/暂停/完成/取消/过期出口，候选不得反复提醒；
3. 沉默是一条可审计决定，同一意图只留一次，不把“没生成消息”当沉默。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from app import sleep as _sleep  # noqa: E402
from app.chat import Chat  # noqa: E402
from app.flow import Flow  # noqa: E402
from app.mind import Mind  # noqa: E402
from app.p3 import P3Store, detect_promise, read_summary  # noqa: E402
from tools.acceptance import fixtures  # noqa: E402

PASS = 0
FAIL = 0


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
    old_sleep = _sleep.status
    _sleep.status = lambda: None
    try:
        # ---- 关系气候：单次不算，多次跨日才形成，反证不丢 -----------------
        with fixtures.temp_workspace("p3_climate_") as ws:
            p3 = ws.open_p3()
            missing = read_summary(ws.root / "missing.db", now=now)
            check("库不存在时只读摘要为空", missing.get("available") is False, missing)
            check("只读摘要不创建 p3.db",
                  not (ws.root / "missing.db").exists(), ws.root)

            p3.add_climate_event("warm", source_text="谢谢", ts=now - 3600)
            one = p3.climate(now)
            check("单次暖意不形成关系气候",
                  one["label"] == "还看不出来", one)
            check("单次事件仍保留证据",
                  one["evidence_count"] == 1, one)

            p3.add_climate_event("cold", source_text="随便你", ts=now - 86400)
            p3.add_climate_event("warm", source_text="陪我", ts=now - 7200)
            p3.add_climate_event("warm", source_text="谢谢你", ts=now - 1800)
            climate = p3.climate(now)
            check("多次跨日事件形成偏暖",
                  climate["label"] == "偏暖", climate)
            check("气候保留反向证据", len(climate["counter_evidence"]) >= 1,
                  climate)
            check("气候不是关系变量写入",
                  climate["changes_relationship_variables"] is False, climate)

            try:
                p3.add_climate_event("bad", source_text="x", ts=now)
                bad_ok = False
            except ValueError:
                bad_ok = True
            check("非法气候信号被拒绝", bad_ok)

            other = p3.add_climate_event("cold", source_text="别烦我", ts=now - 60)
            check("气候事件带来源", bool(other.get("id")), other)
            summary = read_summary(ws.p3_db, now=now)
            check("只读摘要能读到气候", summary["climate"]["label"] == "偏暖",
                  summary["climate"])

        # ---- 承诺：候选不触发、接受后才有效、跟进只一次、终态不复活 --------
        with fixtures.temp_workspace("p3_promise_") as ws:
            p3 = ws.open_p3()
            res = p3.add_candidate(
                "我明天帮你查一下那篇论文。", source_text="我明天帮你查",
                source_turn_id=7, due_at=now + 100)
            p = res["promise"]
            check("承诺先进入候选", p["state"] == "candidate", p)
            check("候选不会被当成到期承诺",
                  p3.due_promises(now + 101) == [], p3.promises())
            accepted = p3.accept(p["id"], "好")
            check("用户接受后才进入有效",
                  accepted.get("promise", {}).get("state") == "active", accepted)
            due = p3.due_promises(now + 101)
            check("有效承诺到期可被取到", len(due) == 1, due)
            first = p3.mark_followed(p["id"], now + 101)
            check("跟进一次成功", first.get("ok") is True, first)
            second = p3.mark_followed(p["id"], now + 102)
            check("同一承诺不会反复跟进",
                  second.get("ok") is False
                  and second.get("reason") == "already_followed", second)
            check("跟进后不再重复到期",
                  p3.due_promises(now + 103) == [], p3.promises())
            done = p3.complete(p["id"], "已经查到了，发给他了")
            check("承诺可以带完成证据结束",
                  done.get("promise", {}).get("state") == "done"
                  and "查到" in done["promise"]["completion_evidence"], done)
            check("完成是终态，不能复活",
                  p3.accept(p["id"], "好").get("reason") == "bad_transition",
                  p3.get(p["id"]))

            c2 = p3.add_candidate("我今晚提醒你吃药。", due_at=now + 3600)["promise"]
            p3.accept(c2["id"], "好")
            cancelled = p3.cancel(c2["id"], "不用了")
            check("用户取消后停止跟进",
                  cancelled.get("promise", {}).get("state") == "cancelled", cancelled)
            check("取消后不进入到期", p3.due_promises(now + 4000) == [], p3.promises())

            check("没有具体动作的承诺不被识别",
                  detect_promise("我会一直陪着你。").get("ok") is False, {})
            found = detect_promise("我明天帮你查一下。")
            check("有时间有动作的承诺被识别", found.get("ok") is True, found)
            check("识别出的承诺带期限", float(found.get("due_at") or 0) > 0, found)

            # observe_turn：她的回复提出候选，他下一句接受。
            obs = p3.observe_turn(
                "谢谢", "好，我明天帮你查一下那篇论文。", turn_id=9,
                appraisal={"relationship_appraisal": {"label": "偏暖"}}, now=now)
            check("一轮之后记录气候事件",
                  (obs.get("climate") or {}).get("signal") == "warm", obs)
            cand = obs.get("promise") or {}
            check("一轮之后能形成承诺候选",
                  cand.get("state") == "candidate", obs)
            if cand.get("id"):
                p3.observe_turn("好", "", turn_id=10, now=now + 60)
                check("下一句接受后承诺生效",
                      p3.get(cand["id"])["state"] == "active", p3.get(cand["id"]))
                p3.observe_turn("不用了，先别提醒", "", turn_id=11, now=now + 120)
                check("明确取消后承诺终止",
                      p3.get(cand["id"])["state"] == "cancelled", p3.get(cand["id"]))

            # 安静：沉默是记录，不是“没有生成消息”。
            quiet = p3.observe_turn(
                "我想静静", "", turn_id=12,
                pre_answer={"strategy": "pause"}, now=now + 200)
            check("明确要安静会记录沉默", quiet.get("quiet") is True, quiet)
            check("安静有有效期", p3.is_quiet(now + 300) is True, p3.quiet_until(now))
            check("安静到期后自动解除", p3.is_quiet(now + 9000) is False, p3.quiet_until(now))
            check("同一意图可跨端查询沉默",
                  p3.silent_for("conversation", now + 300) is True, {})
            check("沉默记录带原因码",
                  (quiet.get("silence") or {}).get("decision", {}).get("reason_code")
                  == "user_quiet", quiet.get("silence"))

    finally:
        _sleep.status = old_sleep

    # ---- Chat 接线：安静优先，承诺优先于普通好奇 -------------------------
    with fixtures.temp_workspace("p3_chat_") as ws:
        mem = ws.open_memory()
        mind = Mind(ws.memory_db)
        p3 = ws.open_p3()
        c = Chat(mem=mem, mind=mind, p3=p3, flow=Flow(ws.root / "flow.json"))

        class NoAutonomy:
            def top(self, *a, **kw):
                return None

            def brief(self):
                return None

        c._autonomy = NoAutonomy()
        for signal, ago in (("warm", 3 * 86400), ("cold", 2 * 86400),
                            ("warm", 86400), ("repair", 3600)):
            p3.add_climate_event(signal, source_text="P3 Chat 接线",
                                 ts=now - ago)
        p = p3.add_candidate("我明天帮你查一下那篇论文。",
                             due_at=now + 100)["promise"]
        p3.accept(p["id"], "好")
        seed = c.proactive_seed(now + 200)
        check("有效承诺优先成为主动由头",
              seed.get("ok") is True and seed.get("kind") == "promise", seed)
        p3.record_silence("user_quiet", "他要求安静", channel="all",
                          intent_key="proactive", until_ts=now + 3600, now=now + 300)
        quiet_seed = c.proactive_seed(now + 400)
        check("用户要求安静时承诺也让路",
              quiet_seed.get("ok") is False and quiet_seed.get("silent") is True,
              quiet_seed)
        p3.mark_followed(p["id"], now + 500)
        later = c.proactive_seed(now + 4000)
        check("跟进后不再重复同一承诺",
              later.get("ok") is False and later.get("reason_code") == "no_value",
              later)
        check("沉默决定写进 p3 库",
              p3.stats().get("silence", 0) >= 1, p3.stats())

        block = p3.prompt_block(now + 200)
        check("提示块包含关系气候与未完成事项",
              "【关系气候】" in block and "【还没做完的事】" in block, block)

        p3.close()
        mind.close()
        mem.close()

    print(f"\n{PASS} 通过 / {FAIL} 失败")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
