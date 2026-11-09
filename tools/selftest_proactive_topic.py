"""自检：主动话题的种子、计划、过滤、审计与 Chat 接线。

不联网、不调用真实模型、不写生产库。所有 Chat 集成都使用 fake provider 和
临时目录，覆盖 T01-T16 的核心断言。
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from app import config as C                      # noqa: E402
from app import proactive_topic as PT            # noqa: E402
from app.chat import Chat                        # noqa: E402

PASS = FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"PASS  {name}")
    else:
        FAIL += 1
        print(f"FAIL  {name}  {detail}")


def _seed(source_type: str, *, now: float, evidence: str = "eligible",
          hint: str = "一件具体的事", **kw) -> PT.TopicSeed:
    return PT.make_seed(
        source_type=source_type, source_refs=kw.pop("refs", ["ref:1"]),
        content_hint=hint, now=now, evidence_state=evidence,
        claim_ids=kw.pop("claim_ids", ["k:1"] if evidence == "eligible"
                         and source_type == "learning_result" else []),
        **kw)


class FakeLearning:
    def __init__(self, *, broken: bool = False) -> None:
        self.broken = broken
        self.calls = 0

    def approved(self, limit: int = 8):
        self.calls += 1
        if self.broken:
            raise RuntimeError("学习库不可用")
        return [{"id": "k1", "points": [
            {"claim": "记忆不是一次写死的，会随着召回和遗忘变化。"}]}]

    def recent_studies(self, limit: int = 5):
        if self.broken:
            raise RuntimeError("学习库不可用")
        return []


class FakeAutonomy:
    def __init__(self, learning=None) -> None:
        self.learning = learning or FakeLearning()

    def brief(self):
        return {}


class FakeMem:
    def __init__(self, *, broken: bool = False, user_text: str = "今天有点累") -> None:
        self.broken = broken
        self.user_text = user_text
        self.calls = 0

    def recent_episodes(self, n=5):
        self.calls += 1
        if self.broken:
            raise RuntimeError("记忆库不可用")
        now = time.time()
        return [{"id": 11, "ts": now - 2 * 3600, "status": "active",
                 "text": "那次我们一起把顺序问题理顺了。"},
                {"id": 12, "ts": now - 3 * 3600, "status": "active",
                 "text": "他说过不想把情绪当成任务。"}][:n]

    def recent_turns(self, n=10):
        if self.broken:
            raise RuntimeError("记忆库不可用")
        return [{"ts": time.time() - 60, "role": "user", "text": self.user_text}]


class FakeMind:
    def __init__(self, feel=None) -> None:
        self.feel = feel or {"好奇": 0.62}

    def snapshot(self, now=None):
        return {"feel": dict(self.feel), "energy": 0.8, "lonely": 0.1,
                "silent_h": 0.2}

    def tick(self):
        return None


class FakeP3:
    def due_promises(self, now=None):
        return []


class FakePersona:
    def compose(self, *args, **kwargs):
        return "BASE"


class FakeLLM:
    def __init__(self, text: str = "我把这件事想清楚了，先这么记着。") -> None:
        self.text = text
        self.system = ""

    def chat(self, prompt, system=""):
        self.system = system
        return {"text": self.text}


class FakeGate:
    mode = "off"


class TwoStepGate:
    """第一次放行，生成后第二次要求安静；用于验证发送前重检。"""

    mode = "enforced"
    calls = 0
    logged = 0

    def __init__(self) -> None:
        self.calls = 0
        self.logged = 0
        self.event_ids: list[str] = []

    def is_quiet(self, now=None):
        return False

    def is_paused(self, now=None):
        return False

    def decide(self, *a, **k):
        self.calls += 1
        self.event_ids.append(str(k.get("event_id") or ""))
        if self.calls >= 2:
            d = SimpleNamespace(mode="pause_stated", reason="user_quiet",
                                event_id="proactive:test")
            d.as_dict = lambda: {"mode": d.mode, "reason": d.reason}
            return d
        d = SimpleNamespace(mode="respond", reason="ok",
                            event_id="proactive:test")
        d.as_dict = lambda: {"mode": d.mode, "reason": d.reason}
        return d

    def log(self, *a, **k):
        self.logged += 1


class FakeChat:
    def __init__(self, *, learning=None, mem=None, mind=None) -> None:
        self.mem = mem or FakeMem()
        self.mind = mind or FakeMind()
        self.autonomy = FakeAutonomy(learning=learning or FakeLearning())
        self.p3 = FakeP3()
        self.response_gate = FakeGate()
        self.persona = FakePersona()
        self._proactive_llm = FakeLLM()
        self._last_proactive = ""
        self._last_proactive_ts = 0.0

    def _topic_style_block(self, plan, snap):
        return ""


def _baseline_no_value() -> dict:
    return {"ok": False, "silent": True, "reason": "此刻没有想主动说的事",
            "reason_code": "no_value"}


def main() -> int:
    old_mode = getattr(C, "PROACTIVE_TOPIC_MODE", "off")
    old_log = getattr(C, "PROACTIVE_TOPIC_LOG", True)
    old_dir = getattr(C, "PROACTIVE_TOPIC_DIR", None)
    tmp = Path(tempfile.mkdtemp(prefix="proactive_topic_"))
    try:
        now = time.time()

        print("— T01/T07 配置、模式与种子过滤 —")
        check("默认意图白名单是 share/continue/react/care/play",
              PT.enabled_intents() == ("share", "continue", "react", "care", "play"),
              str(PT.enabled_intents()))
        check("未知意图被丢弃",
              PT.enabled_intents("share,bogus,care") == ("share", "care"),
              str(PT.enabled_intents("share,bogus,care")))
        check("未知模式回退 off", PT.mode("BOGUS") == "off")
        good = _seed("learning_result", now=now)
        check("合法种子通过", PT.validate_seed(good, now).get("ok") is True)
        expired = PT.make_seed(source_type="shared_event", source_refs=["mem:1"],
                               content_hint="旧事", now=now - 10 * 3600,
                               ttl_hours=0.05)
        check("过期种子不通过",
              PT.validate_seed(expired, now).get("reason") == "expired")
        forbidden = PT.make_seed(source_type="shared_event",
                                 source_refs=["mem:2"], content_hint="敏感",
                                 now=now, sensitivity="forbidden")
        check("敏感未授权种子不通过",
              PT.validate_seed(forbidden, now).get("reason") == "forbidden_sensitivity")
        bad_ev = PT.make_seed(source_type="shared_event", source_refs=["mem:3"],
                              content_hint="背景", now=now,
                              evidence_state="invalid")
        check("invalid 证据不通过",
              PT.validate_seed(bad_ev, now).get("reason") == "invalid_evidence")

        print("— T03 八类意图映射 —")
        cases = [
            ("learning_result", "share", "none", 0),
            ("shared_event", "continue", "open", 0),
            ("shared_event", "react", "none", 0),
            ("observation", "care", "none", 0),
            ("shared_event", "invite", "open", 1),
            ("imagination", "play", "open", 0),
            ("commitment", "remind", "requested", 1),
            ("observation", "check_in", "open", 1),
        ]
        for source, intent, reply, qb in cases:
            s = _seed(source, now=now,
                      hint="一起把这件事往前推" if source != "commitment" else "明天提醒他")
            p = PT.plan_for_seed(s, intent, now)
            ok = (p.intent == intent and p.reply_expectation == reply
                  and p.question_budget == qb and PT.validate_plan(p, now)["ok"])
            check(f"{intent} 的回复期待/问题预算正确", ok, str(p))
        check("普通分享计划不要求回答",
              PT.plan_for_seed(_seed("learning_result", now=now), "share",
                               now).reply_expectation == "none")
        check("提醒计划是 requested，不是社交提问",
              PT.plan_for_seed(_seed("commitment", now=now), "remind",
                               now).reply_expectation == "requested")

        print("— T04 非问题式表达 —")
        share = PT.plan_for_seed(_seed("learning_result", now=now), "share", now)
        text = PT.prompt_directive(share)
        check("分享指令明确不要提问", "不要提问" in text)
        check("分享指令不要求回应", "说完就结束" in text)
        care = PT.plan_for_seed(_seed("observation", now=now), "care", now)
        check("关心指令不要求汇报", "不要要求他汇报" in PT.prompt_directive(care)
              or "不要要求他回应" in PT.prompt_directive(care))
        invite = PT.plan_for_seed(_seed("shared_event", now=now), "invite", now)
        check("邀请最多一个问题", "最多只问一个" in PT.prompt_directive(invite))
        check("contextual_only 不许说成事实",
              "只能当背景" in PT.prompt_directive(
                  PT.plan_for_seed(_seed("shared_event", now=now,
                                         evidence="contextual_only"),
                                   "react", now)))

        print("— T07/T08 排序、去重与过期 —")
        seeds = [_seed("learning_result", now=now, hint="学习"),
                 _seed("shared_event", now=now, hint="共同经历")]
        a = PT.plan_candidates(seeds=seeds, now=now, mode_name="enforced")
        b = PT.plan_candidates(seeds=seeds, now=now, mode_name="enforced")
        check("候选确定性可重放",
              [x.plan_id for x in a] == [x.plan_id for x in b],
              str([x.plan_id for x in a]))
        check("off 不产生候选",
              PT.plan_candidates(seeds=seeds, now=now, mode_name="off") == [])
        check("已发送过的 novelty 被过滤",
              PT.plan_candidates(seeds=a[:1], now=now, mode_name="enforced",
                                 recent_novelty=[a[0].novelty_key]) == [])
        check("过期种子不进入候选",
              PT.plan_candidates(seeds=[expired], now=now,
                                 mode_name="enforced") == [])

        print("— T02 shadow 日志无副作用 —")
        C.PROACTIVE_TOPIC_LOG = True
        C.PROACTIVE_TOPIC_DIR = tmp / "logs"
        PT.reset_for_tests()
        shadow_plan = PT.plan_for_seed(_seed("learning_result", now=now),
                                      "share", now)
        row = PT.log_plan(shadow_plan, mode_name="shadow",
                          candidate_added_to_real_set=False,
                          visible_message_created=False)
        check("shadow 日志写入",
              row is not None and row["mode"] == "shadow",
              str(row))
        check("shadow 不增加真实模型调用",
              row is not None and row["real_llm_calls_added"] == 0)
        check("shadow 不加入真实候选集",
              row is not None and row["candidate_added_to_real_set"] is False)
        check("shadow 不产生可见消息",
              row is not None and row["visible_message_created"] is False)
        check("日志不复制完整内容目标",
              row is not None and "content_goal" not in row
              and "content_goal_sha" in row, str(row))
        PT.reset_for_tests()
        dedup = tmp / "dedup.jsonl"
        PT.log_shadow_once(shadow_plan, path=dedup)
        PT.log_shadow_once(shadow_plan, path=dedup)
        lines = [x for x in dedup.read_text(encoding="utf-8").splitlines()
                 if x.strip()]
        check("shadow 同一计划不每 20 秒重复刷日志", len(lines) == 1, str(len(lines)))

        print("— T09/T12/T13 只读提供者与故障隔离 —")
        fake = FakeChat()
        snap = fake.mind.snapshot(now)
        seeds2, errors = PT.collect_seeds(
            mem=fake.mem, mind=fake.mind, autonomy=fake.autonomy,
            p3=fake.p3, now=now)
        types = {s.source_type for s in seeds2}
        check("采集到学习成果", "learning_result" in types, str(types))
        check("采集到共同经历", "shared_event" in types, str(types))
        check("采集到互动观察", "observation" in types, str(types))
        check("采集到情绪态度", "affect_state" in types, str(types))
        check("学习成果带合格 claim",
              any(s.claim_ids for s in seeds2 if s.source_type == "learning_result"))
        check("共同经历只标 contextual_only",
              all(s.evidence_state == "contextual_only"
                  for s in seeds2 if s.source_type == "shared_event"))
        check("提供者故障不影响其他来源",
              PT.collect_seeds(mem=fake.mem, mind=fake.mind,
                               autonomy=FakeAutonomy(FakeLearning(broken=True)),
                               p3=fake.p3, now=now)[0] != [])
        _, errors2 = PT.collect_seeds(mem=fake.mem, mind=fake.mind,
                                      autonomy=FakeAutonomy(FakeLearning(broken=True)),
                                      p3=fake.p3, now=now)
        check("提供者故障被记录", "learning" in errors2, str(errors2))
        all_broken, _ = PT.collect_seeds(
            mem=FakeMem(broken=True), mind=fake.mind,
            autonomy=FakeAutonomy(FakeLearning(broken=True)),
            p3=fake.p3, now=now)
        check("全部来源失败时返回空候选", all_broken == [], str(all_broken))

        print("— T01/T02/T06 Chat 接线：off/shadow/enforced —")
        calls = {"off": 0}
        class CountingLearning(FakeLearning):
            def approved(self, limit=8):
                calls["off"] += 1
                return super().approved(limit)
        fake_off = FakeChat(learning=CountingLearning())
        C.PROACTIVE_TOPIC_MODE = "off"
        base = _baseline_no_value()
        got = Chat._proactive_topic_seed(fake_off, snap, base, now)
        check("off 原样返回基线", got is base or got == base, str(got))
        check("off 不读取新提供者", calls["off"] == 0, str(calls))
        C.PROACTIVE_TOPIC_MODE = "shadow"
        PT.reset_for_tests()
        got = Chat._proactive_topic_seed(fake, snap, base, now)
        check("shadow 原样返回基线", got == base, str(got))
        shadow_log = C.PROACTIVE_TOPIC_DIR / "events.jsonl"
        check("shadow 接线确实写了计划日志",
              shadow_log.exists()
              and '"mode": "shadow"' in shadow_log.read_text(encoding="utf-8"),
              str(shadow_log))
        C.PROACTIVE_TOPIC_MODE = "enforced"
        got = Chat._proactive_topic_seed(fake, snap, base, now)
        check("enforced 返回 topic 种子",
              got.get("ok") is True and got.get("kind") == "topic", str(got)[:160])
        check("enforced 有完整 plan",
              isinstance(got.get("plan"), dict)
              and got["plan"].get("plan_id"), str(got)[:160])
        check("enforced 有 seed 去重键",
              str(got.get("seed") or "").startswith("topic:"), str(got.get("seed")))
        check("promise 基线不被新话题抢走",
              Chat._proactive_topic_seed(
                  fake, snap, {"ok": True, "kind": "promise",
                               "seed": "promise:p1"}, now).get("kind") == "promise")
        check("新话题候选失败时回退基线",
              Chat._proactive_topic_seed(
                  FakeChat(learning=FakeLearning(broken=True),
                           mem=FakeMem(broken=True),
                           mind=FakeMind(feel={"平静": 0.8})),
                  snap, base, now) == base)

        print("— T04/T05 Chat.proactive：指令、问号与空输出 —")
        topic_seed = {"ok": True, "kind": "topic", "plan": share.as_dict(),
                      "style_block": "", "seed": "topic:" + share.novelty_key}
        cap = FakeLLM("我把这件事想清楚了。")
        fake._proactive_llm = cap
        res = Chat.proactive(fake, topic_seed)
        check("topic 生成成功", res.get("ok") is True, str(res))
        check("topic 指令进入 system prompt",
              "主动意图" in cap.system and "不要提问" in cap.system,
              cap.system[:120])
        cap_bad = FakeLLM("这样安排你觉得好吗？")
        fake._proactive_llm = cap_bad
        res_bad = Chat.proactive(fake, topic_seed)
        check("问题预算 0 时拒绝问号收尾",
              res_bad.get("ok") is False
              and res_bad.get("reason") == "question_budget_exceeded",
              str(res_bad))
        check("失败计划短期不再重试",
              PT.plan_candidates(seeds=[_seed("learning_result", now=now)],
                                 now=now, mode_name="enforced") == [])
        gate = TwoStepGate()
        fake.response_gate = gate
        fake._proactive_llm = FakeLLM("我把这件事想清楚了。")
        fake._last_proactive = ""
        res_gate = Chat.proactive(fake, topic_seed)
        check("发送前重检安静状态，生成后撤回",
              res_gate.get("ok") is False and res_gate.get("silent") is True
              and gate.logged >= 1, str(res_gate))
        check("两次闸门事件 id 不同，不会被 duplicate_event 吞掉",
              len(gate.event_ids) >= 2 and gate.event_ids[0] != gate.event_ids[1],
              str(gate.event_ids))
        fake.response_gate = FakeGate()
        cap_empty = FakeLLM("")
        fake._proactive_llm = cap_empty
        res_empty = Chat.proactive(fake, topic_seed)
        check("topic 空输出不退回旧问候",
              res_empty.get("ok") is False
              and res_empty.get("reason") == "topic_generation_failed",
              str(res_empty))

        print("— T15 资源边界 —")
        many = [_seed("learning_result", now=now, refs=[f"k:{i}"],
                      hint=f"第{i}条") for i in range(20)]
        got_many = PT.plan_candidates(seeds=many, now=now, mode_name="enforced")
        check("候选数有上限", 0 < len(got_many) <= PT.max_candidates(),
              str(len(got_many)))
    finally:
        C.PROACTIVE_TOPIC_MODE = old_mode
        C.PROACTIVE_TOPIC_LOG = old_log
        if old_dir is not None:
            C.PROACTIVE_TOPIC_DIR = old_dir
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{PASS} 通过 / {FAIL} 失败")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
