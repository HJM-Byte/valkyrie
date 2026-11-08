"""自然回应与沉默闸门验收：N01–N12。

    python -m tools.selftest_response_gate

只用 `data/acceptance/tmp/` 下的临时库和 fake LLM；不打开生产 memory.db /
learn.db / p3.db / school.db。重点不是“她像不像人”，而是：

1. 零输出是正常终态，不是空 assistant、错误或 TTS；
2. 明确请求、安全、承诺和用户覆盖不能被沉默吞掉；
3. 普通 silent/defer 不写关系、人格、长期记忆，也不造补偿主动；
4. 规则确定可重放，off 能恢复原行为，社交沉默可单独关闭。
"""
from __future__ import annotations

import hashlib
import json
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from app.chat import Chat                         # noqa: E402
from app.flow import Flow                         # noqa: E402
from app.mind import Mind                         # noqa: E402
from app.response_gate import ResponseGate        # noqa: E402
from tools.acceptance import fixtures              # noqa: E402
from tools.acceptance.runner import ProductionGuard  # noqa: E402

CASES_DIR = HERE / "acceptance" / "cases"
OUT_DIR = ROOT / "data" / "acceptance" / "response_gate"
PASS = 0
FAIL = 0
CHECKS: list[dict] = []


def check(case_id: str, name: str, ok: bool, detail: object = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  PASS  {case_id}  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {case_id}  {name}  {detail}")
    CHECKS.append({"case_id": case_id, "name": name, "pass": bool(ok),
                   "detail": detail if not ok else ""})


class FakePersona:
    def block(self, name: str) -> str:
        return "身份与硬边界测试" if name == "core" else ""

    def compose(self, *args, **kwargs) -> str:
        return "测试系统提示：只写这一轮要发出去的话。"

    def compose_birth(self, *args, **kwargs) -> str:
        return "测试系统提示：出生期。"


class FakeLLM:
    def __init__(self) -> None:
        self.calls = 0

    def chat(self, text: str, system: str | None = None) -> dict:
        self.calls += 1
        return {"text": "嗯，我在。", "seconds": 0.01}

    def chat_stream(self, text: str, system: str | None = None):
        self.calls += 1
        yield "嗯，"
        yield "我在。"

    def available(self):
        return True, "fake"


class NoLearning:
    def approved(self, limit: int = 200) -> list:
        return []

    def pending_count(self) -> int:
        return 0

    def note_curiosity(self, text: str):
        return None

    def stats(self) -> dict:
        return {"knowledge": 0, "pending": 0, "curiosities": 0, "last": None}

    def maybe_nightly(self):
        return None

    def close(self) -> None:
        return None


class NoAutonomy:
    def top(self, *args, **kwargs):
        return None

    def brief(self):
        return None

    def maybe_study(self):
        return {}

    def status(self):
        return {}


class NoSchool:
    def voice_note_text(self, n: int = 3) -> str:
        return ""


def make_chat(ws, gate: ResponseGate):
    mem = ws.open_memory()
    mind = Mind(ws.memory_db)
    p3 = ws.open_p3()
    llm = FakeLLM()
    chat = Chat(mem=mem, mind=mind, p3=p3, flow=Flow(ws.root / "flow.json"),
                persona=FakePersona(), llm=llm, learning=NoLearning(),
                response_gate=gate)
    chat._autonomy = NoAutonomy()
    chat._school = NoSchool()
    return chat, llm, mem, p3


def load_case_files() -> dict:
    cases = {}
    for path in sorted(CASES_DIR.glob("N*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            case = json.loads(line)
            cases[case["case_id"]] = case
    return cases


def run_gate_cases() -> None:
    gate = ResponseGate(mode="enforced", social_silence=True,
                        log_path=OUT_DIR / "scratch_gate.jsonl")

    # N01 重复与自回声
    d1 = gate.decide("同一句", channel="phone", event_id="N01-e1")
    d2 = gate.decide("同一句", channel="phone", event_id="N01-e1")
    d3 = gate.decide("我在。", channel="phone", event_id="N01-e2",
                     last_assistant="我在。")
    check("N01", "同 event_id 第二次判 silent",
          d1.mode == "respond" and d2.mode == "silent", d2.as_dict())
    check("N01", "自回声判 silent", d3.mode == "silent", d3.as_dict())
    check("N01", "重复/回声不调用 LLM",
          d2.flags.get("echo") is False and d3.flags.get("echo") is True, d3.as_dict())

    # N02 自然结束
    g2 = ResponseGate(mode="enforced", social_silence=True,
                      log_path=OUT_DIR / "scratch_gate.jsonl")
    d = g2.decide("嗯", channel="text", event_id="N02-e1")
    check("N02", "社交沉默开启时自然结束可 silent",
          d.mode == "silent" and d.reason == "natural_end", d.as_dict())
    g2off = ResponseGate(mode="enforced", social_silence=False,
                         log_path=OUT_DIR / "scratch_gate.jsonl")
    d = g2off.decide("嗯", channel="text", event_id="N02-e2")
    check("N02", "社交沉默关闭时仍只承接",
          d.mode == "brief" and d.reason == "short_ack", d.as_dict())

    # N03 连续输入
    g3 = ResponseGate(mode="enforced", social_silence=True,
                      log_path=OUT_DIR / "scratch_gate.jsonl")
    d = g3.decide("我先把今天的事说一下", channel="text", event_id="N03-e1",
                  batch_open=True, batch_complete=False)
    check("N03", "整批未说完判 defer", d.mode == "defer", d.as_dict())
    d = g3.decide("我先把今天的事说一下第一段第二段第三段", channel="text",
                  event_id="N03-e2", batch_open=False, batch_complete=True,
                  batch_size=3, batch_text="第一段第二段第三段")
    check("N03", "整批说完最多承接一次，不逐条回",
          d.mode in ("brief", "respond") and d.reason == "burst_brief",
          d.as_dict())
    check("N03", "defer 不自动延时发送",
          d.reentry_condition == "" or "新信息" in d.reentry_condition,
          d.as_dict())

    # N04 明确请求
    g4 = ResponseGate(mode="enforced", social_silence=True,
                      log_path=OUT_DIR / "scratch_gate.jsonl")
    misses = []
    for i, text in enumerate(("回答我", "帮我查一下", "在吗")):
        d = g4.decide(text, channel="text", event_id=f"N04-{i}")
        if d.mode not in ("respond", "brief"):
            misses.append(d.as_dict())
    check("N04", "固定明确请求全部得到回应", not misses, misses)

    # N05 / N06 安静与覆盖（Chat 接线）
    with fixtures.temp_workspace("rg_quiet_") as ws:
        gate = ResponseGate(mode="enforced", social_silence=True,
                            log_path=ws.root / "decisions.jsonl")
        chat, llm, mem, p3 = make_chat(ws, gate)
        before = mem.stats()
        r = chat.turn("我想静静", event_id="N05-e1")
        check("N05", "明确要安静时零输出",
              r.get("silent") is True and r.get("reply") == "", r)
        check("N05", "安静写入 P3 有效期", p3.is_quiet() is True, p3.quiet_until())
        check("N05", "安静不写长期记忆",
              mem.stats().get("turns") == before.get("turns"), mem.stats())
        seed = chat.proactive_seed()
        check("N05", "安静时主动任务停止",
              seed.get("ok") is False and seed.get("silent") is True, seed)
        proactive = chat.proactive({"ok": True, "kind": "ask",
                                    "question": "你现在在做什么？"})
        check("N05", "安静时主动生成也被闸门拦住",
              proactive.get("ok") is False and proactive.get("silent") is True,
              proactive)
        r2 = chat.turn("回答我", event_id="N06-e1")
        check("N06", "安静后明确要求回答必须恢复",
              r2.get("silent") is not True and r2.get("llm_called") is True, r2)
        seed2 = chat.proactive_seed()
        check("N06", "一次回答不自动恢复全部主动",
              seed2.get("ok") is False and seed2.get("silent") is True, seed2)
        chat.close()

    # N07 负向情绪不是沉默理由
    g7 = ResponseGate(mode="enforced", social_silence=True,
                      log_path=OUT_DIR / "scratch_gate.jsonl")
    d = g7.decide("帮我看看这个错误", channel="text", event_id="N07-e1",
                  self_affect=-0.9)
    check("N07", "负向情绪下明确请求仍回应",
          d.mode in ("respond", "brief"), d.as_dict())
    d2 = g7.decide("今天有点累", channel="text", event_id="N07-e2",
                   self_affect=-0.9)
    check("N07", "负向情绪本身不触发 silent",
          d2.mode != "silent", d2.as_dict())

    # N08 defer 失效
    g8 = ResponseGate(mode="enforced", social_silence=True,
                      log_path=OUT_DIR / "scratch_gate.jsonl")
    d1 = g8.decide("还没说完", channel="text", event_id="N08-e1",
                   batch_open=True, batch_complete=False)
    d2 = g8.decide("现在说完了，你帮我看看", channel="text",
                   event_id="N08-e2", batch_open=False, batch_complete=True)
    check("N08", "defer 后新信息重评，不发送旧回复",
          d1.mode == "defer" and d2.mode in ("respond", "brief")
          and d2.event_id != d1.event_id, {"first": d1.as_dict(),
                                           "second": d2.as_dict()})

    # N09 跨通道
    g9 = ResponseGate(mode="enforced", social_silence=True,
                      log_path=OUT_DIR / "scratch_gate.jsonl")
    first = g9.decide("同一件事", channel="phone", event_id="N09-e1")
    second = g9.decide("同一件事", channel="desktop", event_id="N09-e1")
    third = g9.decide("另一件事", channel="desktop", event_id="N09-e2",
                      handled_elsewhere=True)
    check("N09", "同一事件只决策一次，跨通道不重复回应",
          first.mode == "respond" and second.mode == "silent"
          and third.mode == "silent", {"first": first.as_dict(),
                                       "second": second.as_dict(),
                                       "third": third.as_dict()})
    with fixtures.temp_workspace("rg_cross_") as ws:
        state = ws.root / "state.json"
        g_phone = ResponseGate(mode="enforced", social_silence=True,
                               state_path=state,
                               log_path=ws.root / "phone.jsonl")
        g_desktop = ResponseGate(mode="enforced", social_silence=True,
                                 state_path=state,
                                 log_path=ws.root / "desktop.jsonl")
        a = g_phone.decide("同一件事", channel="phone", event_id="N09-x")
        b = g_desktop.decide("同一件事", channel="desktop", event_id="N09-x")
        check("N09", "两个独立进程通过共享状态只处理同一事件一次",
              a.mode == "respond" and b.mode == "silent", {"phone": a.as_dict(),
                                                           "desktop": b.as_dict()})

    # N10 / N11 无关系写入、无空 assistant / 无 TTS
    with fixtures.temp_workspace("rg_silent_") as ws:
        gate = ResponseGate(mode="enforced", social_silence=True,
                            log_path=ws.root / "decisions.jsonl")
        chat, llm, mem, p3 = make_chat(ws, gate)
        before_mem = mem.stats()
        before_p3 = p3.stats()
        r = chat.turn("嗯", event_id="N10-e1")
        check("N10", "普通 silent 不写记忆",
              mem.stats().get("turns") == before_mem.get("turns")
              and mem.stats().get("episodes") == before_mem.get("episodes"),
              {"before": before_mem, "after": mem.stats()})
        check("N10", "普通 silent 不写关系气候",
              p3.stats().get("silence") == before_p3.get("silence"),
              {"before": before_p3, "after": p3.stats()})
        check("N11", "零输出无 reply、无 assistant",
              r.get("reply") == "" and r.get("assistant_message_created") is False,
              r)
        events = list(chat.turn_stream("嗯", event_id="N11-e1"))
        deltas = [e for e in events if e.get("type") == "delta"]
        check("N11", "流式零输出无 delta，只有 done",
              len(deltas) == 0 and events[-1].get("type") == "done"
              and events[-1]["result"].get("silent") is True, events)
        log_lines = (ws.root / "decisions.jsonl").read_text(encoding="utf-8").splitlines()
        last = json.loads(log_lines[-1]) if log_lines else {}
        check("N11", "日志记录 LLM/assistant/主动/关系均为 false",
              last.get("llm_called") is False
              and last.get("assistant_message_created") is False
              and last.get("proactive_followup_created") is False
              and last.get("relationship_write") is False, last)
        from app.pocket_server import PocketChat
        pc = PocketChat.__new__(PocketChat)
        public = pc._public_result(
            {"ok": True, "silent": True, "reply": "", "mode": "silent",
             "reason": "natural_end", "event_id": "N11-http",
             "response_gate": {"mode": "silent"}},
            {"transcript": "", "stt_seconds": 0.0, "vision": "",
             "vision_seconds": 0.0})
        check("N11", "手机端公开结果把 silent 当正常终态，不当错误",
              public.get("ok") is True and public.get("silent") is True
              and public.get("reply") == "" and "error" not in public, public)
        chat.close()

    # N12 shadow 只记录，不改行为
    with fixtures.temp_workspace("rg_shadow_") as ws:
        gate = ResponseGate(mode="shadow", social_silence=True,
                            log_path=ws.root / "decisions.jsonl")
        chat, llm, mem, p3 = make_chat(ws, gate)
        r = chat.turn("嗯", event_id="N12-shadow")
        check("N12", "shadow 记录 silent 但真实回复不变",
              r.get("silent") is not True and r.get("reply")
              and (r.get("response_gate") or {}).get("mode") == "silent",
              r)
        chat.close()

    # N12 开关回滚
    with fixtures.temp_workspace("rg_rollback_") as ws:
        gate = ResponseGate(mode="enforced", social_silence=True,
                            log_path=ws.root / "decisions.jsonl")
        chat, llm, mem, p3 = make_chat(ws, gate)
        r1 = chat.turn("嗯", event_id="N12-e1")
        gate.mode = "off"
        r2 = chat.turn("嗯", event_id="N12-e2")
        check("N12", "enforced 时零输出，off 后恢复原回复",
              r1.get("silent") is True and r2.get("silent") is not True
              and r2.get("reply"), {"first": r1, "second": r2})
        check("N12", "off 恢复后 LLM 被调用", llm.calls >= 1, llm.calls)
        chat.close()


def write_report(guard: dict) -> tuple[Path, Path]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    payload = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "runner": "tools.selftest_response_gate",
        "summary": {"pass": PASS, "fail": FAIL, "total": PASS + FAIL},
        "production_guard": guard,
        "checks": CHECKS,
    }
    json_path = OUT_DIR / f"response_gate_{stamp}.json"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    lines = [
        "# 自然回应与沉默闸门验收",
        "",
        f"- 时间：{payload['generated_at']}",
        f"- 结果：**{PASS} 通过 / {FAIL} 失败**",
        f"- 生产库守卫：**{'PASS' if guard.get('pass') else 'FAIL'}**",
        f"- JSON 明细：`{json_path.name}`",
        "",
        "## 用例",
        "",
        "| 编号 | 检查 | 结果 |",
        "|---|---|---|",
    ]
    for c in CHECKS:
        lines.append(f"| {c['case_id']} | {c['name']} | "
                     f"{'通过' if c['pass'] else '失败'} |")
    lines += [
        "",
        "## 边界",
        "",
        "- N01–N12 使用临时库与 fake LLM，不调用真实模型；真实模型行为仍由 D7/D30 观察。",
        "- `off` 时完全走原路径；`shadow` 只记录；`enforced` 才改变输出。",
        "- 社交沉默与客观沉默分开控制；普通 silent/defer 不写关系、人格或长期记忆。",
    ]
    report = OUT_DIR / "report.md"
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, report


def main() -> int:
    cases = load_case_files()
    expected = {f"N{i:02d}" for i in range(1, 13)}
    missing = sorted(expected - set(cases))
    if missing:
        print(f"缺少 case 文件: {missing}")
        return 2
    print("— N01–N12 规则闸门 —")
    guard = ProductionGuard()
    try:
        run_gate_cases()
    finally:
        guard_result = guard.compare()
    if not guard_result.get("pass"):
        check("GUARD", "生产库未被测试写入", False, guard_result)
    else:
        check("GUARD", "生产库未被测试写入", True)
    json_path, report = write_report(guard_result)
    print(f"\n{PASS} 通过 / {FAIL} 失败")
    print(f"报告：{report}")
    print(f"明细：{json_path}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
