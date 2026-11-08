"""自检：三类情感对象只读分离 + 回答前分寸（P3 4.4/4.5）。

    python -m tools.selftest_appraisal

不碰真库、不联网，只调用纯函数和 think()。重点不是“像不像人”，
而是三件事不能混：用户情绪假设、她的状态、关系评价；以及回答前的
理解 / 澄清 / 暂停 / 降压力只改表达，不改检索、人格和长期关系。
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from app.appraisal import evaluate, is_valid, render  # noqa: E402
from app.persona import Persona  # noqa: E402
from app.pre_answer_feedback import classify_response  # noqa: E402
from app.think import think  # noqa: E402

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


def state() -> dict:
    return {
        "energy": 0.55, "mood": -0.25, "arousal": 0.45,
        "lonely": 0.20, "silent_h": 0.10, "gap_shock": 0.0,
        "feel": {"低落": 0.50, "平静": 0.30},
        "rel": {"熟悉": 0.40, "信任": 0.30, "边界": 0.80},
        "desire_effect": {}, "temper": {},
    }


def main() -> int:
    st = state()
    before = copy.deepcopy(st)
    now = 1000.0

    r = evaluate("我今天有点难受，心里堵", st, now=now)
    u = r["user_emotion"]
    check("明确表达 → 低落", u["label"] == "低落", u)
    check("明确表达带证据", bool(u["evidence"]), u)
    check("用户情绪不是事实", u["is_fact"] is False, u)
    check("允许用户否定", u["allows_user_denial"] is True, u)

    r2 = evaluate("我没事", st, now=now)
    check("“没事”不自动等于平静", r2["user_emotion"]["label"] != "平静",
          r2["user_emotion"])
    check("“没事”标记歧义", r2["user_emotion"]["ambiguous"] is True,
          r2["user_emotion"])
    check("歧义走尊重不确定",
          r2["regulation"]["strategy"] == "respect_ambiguous",
          r2["regulation"])

    r3 = evaluate("我不难过", st, now=now)
    check("否定句保持未知", r3["user_emotion"]["label"] == "未知",
          r3["user_emotion"])
    check("否定句有标记", r3["user_emotion"]["negated"] is True,
          r3["user_emotion"])

    r4 = evaluate("我有点累", st, now=now)
    check("疲惫能识别", r4["user_emotion"]["label"] == "疲惫", r4["user_emotion"])

    r4b = evaluate("我不累", st, now=now)
    check("“不累”标记否定", r4b["user_emotion"]["negated"] is True,
          r4b["user_emotion"])
    check("“不累”不反推成疲惫", r4b["user_emotion"]["label"] == "未知",
          r4b["user_emotion"])

    r4c = evaluate("我没事，就是有点累", st, now=now)
    check("歧义词和明确词同句时明确词优先",
          r4c["user_emotion"]["label"] == "疲惫", r4c["user_emotion"])

    r4d = evaluate("麻烦你帮我看看这个", st, now=now)
    check("“麻烦你”不被当成烦躁",
          r4d["user_emotion"]["label"] == "未知", r4d["user_emotion"])
    check("“麻烦你”可作暖意候选",
          r4d["relationship_appraisal"]["label"] == "偏暖",
          r4d["relationship_appraisal"])

    r4e = evaluate("这是长期积累的结果", st, now=now)
    check("“积累”不触发疲惫",
          r4e["user_emotion"]["label"] == "未知", r4e["user_emotion"])

    r5 = evaluate("谢谢你陪我", st, now=now)
    rel = r5["relationship_appraisal"]
    check("暖意只形成候选", rel["candidate"] is True, rel)
    check("暖意不永久改关系", rel["permanent_change"] is False, rel)
    check("暖意不改九变量",
          rel["changes_relationship_variables"] is False, rel)
    check("暖意不反推用户情绪", r5["user_emotion"]["label"] == "未知",
          r5["user_emotion"])

    r6 = evaluate("随便你", st, now=now)
    check("冷意识别", r6["relationship_appraisal"]["label"] == "偏冷",
          r6["relationship_appraisal"])
    check("冷意不升级亲密",
          r6["regulation"]["strategy"] == "hold_boundary", r6["regulation"])

    r7 = evaluate("我想静静", st, now=now, user_ttl_s=90,
                  relationship_ttl_s=180)
    check("暂停信号优先", r7["regulation"]["strategy"] == "pause",
          r7["regulation"])
    check("用户情绪有独立有效期",
          r7["user_emotion"]["valid_until"] == now + 90, r7["user_emotion"])
    check("关系评价有独立有效期",
          r7["relationship_appraisal"]["valid_until"] == now + 180,
          r7["relationship_appraisal"])
    check("有效期外失效",
          not is_valid(r7["user_emotion"], now + 91)
          and not is_valid(r7["relationship_appraisal"], now + 181), {})

    th = think("我有点累", state=st, now=now)
    three = th.get("appraisal_three") or {}
    check("think 带回三类评价", bool(three), list(th))
    check("think 的用户情绪是疲惫",
          (three.get("user_emotion") or {}).get("label") == "疲惫",
          three.get("user_emotion"))
    check("她的状态单独一类",
          (three.get("self_state") or {}).get("is_user_emotion") is False,
          three.get("self_state"))
    check("评价声明只读", three.get("read_only") is True, three.get("read_only"))
    check("评价不写库", three.get("writes") == [], three.get("writes"))
    check("调用后 state 不变", st == before, st)
    check("render 不抛异常", bool(render(three)), render(three))

    # P3 4.5：回答前的分寸。evaluate 只给建议，think 才接线。
    pause = evaluate("我想静静", st, now=now)
    check("evaluate 只给暂停建议，不自己应用",
          pause["regulation"]["strategy"] == "pause"
          and pause["regulation"]["applied"] is False, pause["regulation"])
    check("暂停建议允许不接、不追问",
          "不追问" in pause["regulation"]["directive"]
          and "允许不接" in pause["regulation"]["directive"],
          pause["regulation"])

    th_pause = think("我想静静", state=st, now=now)
    pp = th_pause.get("pre_answer") or {}
    check("think 把暂停接到回答前", pp.get("applied") is True, pp)
    check("暂停限一句且不许追问",
          pp.get("allow_question") is False and pp.get("max_chars") <= 18, pp)
    check("暂停写进 decision 和 summary",
          th_pause["decision"].get("pre_answer", {}).get("strategy") == "pause"
          and th_pause["summary"].get("pre_answer") == "pause",
          th_pause.get("summary"))
    check("暂停只改表达，不改检索/人格/关系",
          pp.get("change_scope") == ["expression"]
          and pp.get("changes_retrieval") is False
          and pp.get("changes_personality") is False
          and pp.get("changes_relationship_variables") is False, pp)

    th_amb = think("我没事", state=st, now=now)
    pp_amb = th_amb.get("pre_answer") or {}
    check("歧义允许一次轻量确认",
          pp_amb.get("strategy") == "respect_ambiguous"
          and pp_amb.get("clarify_allowed") is True
          and pp_amb.get("allow_question") is True, pp_amb)

    th_task = think("我好累，帮我把这个做完", state=st, now=now)
    pp_task = th_task.get("pre_answer") or {}
    check("累了还要办事：先降任务压力",
          pp_task.get("strategy") == "understand"
          and pp_task.get("task_pressure") == "low"
          and "最小的一步" in pp_task.get("directive", ""), pp_task)

    th_cold = think("随便你", state=st, now=now)
    pp_cold = th_cold.get("pre_answer") or {}
    check("冷话不升级亲密、不追问关系",
          pp_cold.get("strategy") == "hold_boundary"
          and "不升级亲密" in pp_cold.get("directive", "")
          and "不追问关系" in pp_cold.get("directive", ""), pp_cold)

    th_neutral = think("今天天气不错", state=st, now=now)
    check("普通陈述不额外加回答前约束",
          (th_neutral.get("pre_answer") or {}).get("applied") is False,
          th_neutral.get("pre_answer"))

    persona = Persona()
    prompt = persona.compose("我想静静", thought=th_pause, channel="text")
    check("暂停指令进提示词", "【回答前】" in prompt
          and pp.get("directive") in prompt and "最多 18 字" in prompt,
          prompt[-300:])
    prompt_neutral = persona.compose("今天天气不错", thought=th_neutral,
                                     channel="text")
    check("普通陈述不加回答前块", "【回答前】" not in prompt_neutral,
          prompt_neutral[-200:])
    check("多次 think 后 state 仍不变", st == before, st)

    # P3 4.6：上一轮反馈只调下一次表达。
    check("拒绝分类优先",
          classify_response("别问了，我不想说", "respect_ambiguous", 3)["action"]
          == "rejected",
          classify_response("别问了，我不想说", "respect_ambiguous", 3))
    check("接受分类", classify_response("嗯，谢谢", "pause", 3)["action"]
          == "accepted", {})
    check("没回应分类",
          classify_response("今天天气不错", "understand", 3)["action"]
          == "no_response", {})
    feedback = {"last_strategy": "respect_ambiguous", "last_action": "rejected",
                "stats": {"count": 1, "rejected": 1}}
    rf = evaluate("我没事", st, now=now, feedback=feedback)
    check("上次澄清被拒绝后不再追问",
          rf["regulation"]["clarify_allowed"] is False
          and rf["regulation"]["allow_question"] is False, rf["regulation"])
    check("反馈标出已生效",
          rf["regulation"]["feedback_informed"] is True
          and rf["regulation"]["feedback_action"] == "rejected",
          rf["regulation"])
    check("反馈仍只改表达",
          rf["regulation"]["changes_retrieval"] is False
          and rf["regulation"]["changes_personality"] is False
          and rf["regulation"]["changes_relationship_variables"] is False,
          rf["regulation"])
    check("反馈不写库", rf["writes"] == [], rf["writes"])
    th_fb = think("我没事", state=st, now=now, feedback=feedback)
    check("think 接上反馈后的回答前策略",
          (th_fb.get("pre_answer") or {}).get("clarify_allowed") is False
          and (th_fb.get("pre_answer") or {}).get("feedback_informed") is True,
          th_fb.get("pre_answer"))
    check("反馈后 state 仍不变", st == before, st)

    print(f"\n{PASS} 通过 / {FAIL} 失败")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
