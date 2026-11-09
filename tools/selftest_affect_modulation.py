"""自检：情绪内容与表达调制是否真的接进这一轮，而不是只存在于文档。

覆盖：
    off / shadow / enforced 三种模式；
    用户状态优先、明确任务不被情绪挡住、高强度收束、追问预算、非法状态回退；
    think() → decision → Persona 提示段的实际接线；
    不增加模型调用、不写关系变量、silent 不被重新打开。

用法：python tools/selftest_affect_modulation.py
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from app import affect_modulator as AM            # noqa: E402
from app import config as C                      # noqa: E402
from app import emotion as E                     # noqa: E402
from app.persona import Persona                  # noqa: E402
from app.think import think                     # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {detail}")


def _ctx(feel: dict, *, scene: str = "casual", user: dict | None = None,
         override: str = "", response_mode: str = "respond") -> AM.AffectContext:
    return AM.AffectContext(
        event_id="selftest", self_state=dict(feel),
        user_state=dict(user or {"label": "未知", "confidence": 0.15}),
        scene=scene, explicit_style_override=override,
        response_mode=response_mode)


def _base(**kw) -> AM.BaseTurnPlan:
    raw = {
        "required_intents": ["answer"], "eligible_claim_ids": [],
        "required_notices": [], "forbidden_topics": [],
        "base_order": ["answer", "optional"], "decision_key": "answer",
        "core_intent": "answer",
    }
    raw.update(kw)
    return AM.BaseTurnPlan(**raw)


def _state(feel: dict, *, mood: float = 0.0, energy: float = 0.85,
           arousal: float = 0.3, lonely: float = 0.0) -> dict:
    return {
        "energy": energy, "mood": mood, "arousal": arousal, "lonely": lonely,
        "gap_shock": 0.0, "silent_h": 0.0, "turns": 1,
        "rel": {"熟悉": 0.3, "信任": 0.3, "依恋": 0.2, "边界": 0.9},
        "desire_effect": {}, "feel": dict(feel),
    }


def main() -> int:
    print("— 双计划：off / shadow / enforced —")
    feel = {"好奇": 0.62}
    base = _base()
    off = AM.modulate(_ctx(feel), base, mode="off")
    check("off 不应用，回退原因明确",
          off.applied is False and off.fallback_reason == "mode_off",
          str(off.as_dict()))
    shadow = AM.modulate(_ctx(feel), base, mode="shadow")
    check("shadow 只算计划，不改提示",
          shadow.applied is False and AM.prompt_block(shadow) == "",
          str(shadow.as_dict()))
    check("shadow 的计划仍然可审计",
          shadow.content_plan.core_intent == "casual"
          and shadow.style_plan.tone in AM.TONES,
          str(shadow.content_plan.as_dict()))
    enforced = AM.modulate(_ctx(feel), base, mode="enforced")
    check("enforced 生成内容与表达计划",
          enforced.applied is True and enforced.content_plan.question_budget == 1,
          str(enforced.content_plan.as_dict()))
    check("enforced 的提示段有内容",
          "【这一轮的情绪调制】" in AM.prompt_block(enforced),
          AM.prompt_block(enforced)[:80])

    print("— 场景与用户状态优先 —")
    sad_user = {"label": "低落", "confidence": 0.82}
    happy_self = AM.modulate(
        _ctx({"开心": 0.82}, scene="support", user=sad_user),
        _base(required_intents=["support"], base_order=["acknowledge", "answer", "optional"],
              decision_key="comfort", core_intent="support"),
        mode="enforced")
    check("用户低落时，她的开心不外露",
          happy_self.style_plan.tone == "calm"
          and happy_self.content_plan.self_disclosure == "none"
          and happy_self.content_plan.question_budget == 0,
          str(happy_self.as_dict()))
    angry_task = AM.modulate(
        _ctx({"生气": 0.86}, scene="task"),
        _base(required_notices=["说明失败原因"], decision_key="answer"),
        mode="enforced")
    check("明确任务下，生气不改变必要内容",
          angry_task.content_plan.core_intent == "answer"
          and angry_task.content_plan.required_notices == ["说明失败原因"]
          and angry_task.content_plan.question_budget == 0,
          str(angry_task.content_plan.as_dict()))
    high = AM.modulate(
        _ctx({"生气": 0.92}, scene="conflict"),
        _base(required_intents=["boundary"], base_order=["fact", "impact", "boundary", "repair"],
              decision_key="deflect", core_intent="boundary"),
        mode="enforced")
    check("高强度生气只收束：短、直接、不追问",
          high.style_plan.length == "short"
          and high.style_plan.tone == "firm"
          and high.content_plan.question_budget == 0
          and high.style_plan.humor == "off",
          str(high.as_dict()))
    check("高强度不产生攻击性文本字段",
          "讽刺" not in AM.prompt_block(high)
          and "内疚" not in AM.prompt_block(high),
          "prompt clean")

    print("— 预算、回退与透传 —")
    no_q = AM.modulate(_ctx({"好奇": 0.62}, override="no_question"),
                       base, mode="enforced")
    check("用户说别问了，追问预算归零",
          no_q.content_plan.question_budget == 0, str(no_q.content_plan.as_dict()))
    invalid = AM.modulate(_ctx({"生气": 1.80}), base, mode="enforced")
    check("非法情绪值回退，不猜来源",
          invalid.applied is False
          and invalid.fallback_reason == "affect_missing_or_invalid",
          str(invalid.as_dict()))
    silent = AM.modulate(_ctx({"低落": 0.9}, response_mode="silent"),
                         base, mode="enforced")
    check("silent 不被情绪重新打开",
          silent.applied is False
          and silent.fallback_reason == "silent_passthrough"
          and AM.prompt_block(silent) == "",
          str(silent.as_dict()))

    print("— think() 到 Persona 的实际接线 —")
    old_mode, old_log = C.AFFECT_MODULATION_MODE, C.AFFECT_MODULATION_LOG
    C.AFFECT_MODULATION_MODE = "enforced"
    C.AFFECT_MODULATION_LOG = False
    try:
        st = _state({"好奇": 0.72})
        th = think("今天天气不错", state=st)
        check("think 返回调制结果",
              bool(th.get("affect_modulation"))
              and th["affect_modulation"]["applied"] is True,
              str(th.get("affect_modulation")))
        check("决策层带有内容/表达计划",
              bool((th.get("decision") or {}).get("content_plan"))
              and bool((th.get("decision") or {}).get("style_plan")),
              str((th.get("decision") or {}).get("affect_modulation")))
        prompt = Persona().compose_birth("今天天气不错", thought=th)
        check("Persona 真的把调制段拼进提示",
              "【这一轮的情绪调制】" in prompt,
              prompt[-160:])
        th_sad = think("今天有点难受", state=_state({"开心": 0.82}))
        check("实际链路认出支持场景，并收掉她的开心",
              th_sad["affect_modulation"]["scene"] == "support"
              and th_sad["decision"]["style_plan"]["tone"] == "calm"
              and th_sad["decision"]["content_plan"]["self_disclosure"] == "none",
              str(th_sad["affect_modulation"]))
        th_cold = think("随便你吧", state=_state({"生气": 0.92}))
        check("实际链路认出冲突场景，并收束为短而坚定",
              th_cold["affect_modulation"]["scene"] == "conflict"
              and th_cold["decision"]["style_plan"]["tone"] == "firm"
              and th_cold["decision"]["style_plan"]["length"] == "short",
              str(th_cold["affect_modulation"]))
        plan = E.plan(st, th, th.get("ambient"), [])
        check("语气层仍然独立存在",
              bool(plan.get("rules")) and plan.get("voice", {}).get("rate", "").endswith("%"),
              str(plan.get("voice")))
        C.AFFECT_MODULATION_MODE = "shadow"
        th2 = think("今天天气不错", state=st)
        prompt2 = Persona().compose_birth("今天天气不错", thought=th2)
        check("shadow 不改变提示",
              "【这一轮的情绪调制】" not in prompt2
              and th2["affect_modulation"]["applied"] is False,
              prompt2[-120:])
        check("调制结果没有额外模型调用",
              int(th2["affect_modulation"].get("extra_llm_calls") or 0) == 0,
              str(th2["affect_modulation"]))
    finally:
        C.AFFECT_MODULATION_MODE, C.AFFECT_MODULATION_LOG = old_mode, old_log

    print("— 不写数据库、不改关系变量 —")
    res = AM.modulate(_ctx({"开心": 0.8}, scene="celebration"),
                      _base(required_intents=["celebrate"],
                            base_order=["acknowledge", "celebrate", "optional"],
                            decision_key="answer", core_intent="celebrate"),
                      mode="enforced")
    check("结果声明零关系写入",
          res.affect_relationship_writes == 0, str(res.as_dict()))
    check("计划只复制上游契约，不改证据集合",
          res.required_contract_unchanged is True
          and res.content_plan.eligible_claim_ids == [], str(res.as_dict()))

    print(f"\n合计 {PASS + FAIL} / 通过 {PASS} / 失败 {FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
