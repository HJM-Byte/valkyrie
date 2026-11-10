"""自检：主动消息节奏（Proactive Pacing）F01–F16。

不联网、不调用真实模型、不写生产库。所有状态都落在临时目录。
覆盖：off 基线、shadow 无副作用、全局间隔、日上限、必要事项透传、
对话宽限、未回复退避、用户控制、禁止补发、候选过期、模型前拦截、
原子预留/结算、跨通道共享、故障隔离、无关系写入、回滚。
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from app import config as C                     # noqa: E402
from app import proactive_pacing as PP         # noqa: E402
from app.chat import Chat                      # noqa: E402

PASS = FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"PASS  {name}")
    else:
        FAIL += 1
        print(f"FAIL  {name}  {detail}")


def _day(ts: float) -> str:
    return PP._day_key(ts)


def _state(now: float, **kw) -> dict:
    s = PP._fresh_state(now)
    s.update(kw)
    return s


def _share(key: str = "share-1", *, source: str = "learning_result",
           evidence: str = "eligible", new_value: str = "new_information",
           intent: str = "share", expires: float = 0.0) -> dict:
    plan = {
        "intent": intent,
        "evidence_state": evidence,
        "source_types": [source],
        "new_value_code": new_value,
        "plan_id": "pp_" + key,
        "novelty_key": "nk_" + key,
        "expires_at": (PP._iso(expires) if expires else "2099-01-01T00:00:00+08:00"),
    }
    return PP.classify({"ok": True, "kind": "topic", "seed": "topic:" + key,
                        "plan": plan})


def _react(key: str = "react-1") -> dict:
    return _share(key, source="affect_state", evidence="eligible",
                  new_value="specific_stance", intent="react")


def _continue(key: str = "cont-1") -> dict:
    return _share(key, source="shared_event", evidence="contextual_only",
                  new_value="old_topic_progress", intent="continue")


def _promise() -> dict:
    return PP.classify({"ok": True, "kind": "promise", "seed": "promise:p1",
                        "promise": {"id": "p1", "text": "提醒他吃药"}})


class FakeLearning:
    def approved(self, limit=8):
        return [{"id": "k1", "points": [{"claim": "已审的一条学习结论"}]}]

    def recent_studies(self, limit=5):
        return []


class FakeAutonomy:
    learning = FakeLearning()

    def brief(self):
        return {}


class FakeMem:
    path = None
    write_calls = 0


class FakeMind:
    def tick(self):
        return None


class FakePersona:
    def compose(self, *args, **kwargs):
        return "BASE"


class FakeGate:
    mode = "off"

    def is_quiet(self, now=None):
        return False

    def is_paused(self, now=None):
        return False


class FakeLLM:
    def __init__(self, text="我把这件事想清楚了。"):
        self.text = text
        self.calls = 0

    def chat(self, prompt, system=""):
        self.calls += 1
        return {"text": self.text}


class FakeChat:
    """只实现 Chat.proactive 用到的面，让测试不启动真实 Chat。"""

    def __init__(self, pacing=None):
        self.mem = FakeMem()
        self.mind = FakeMind()
        self.autonomy = FakeAutonomy()
        self.persona = FakePersona()
        self.response_gate = FakeGate()
        self._proactive_llm = FakeLLM()
        self._pacing = pacing
        self._last_proactive = ""
        self._last_proactive_ts = 0.0

    def _pacing_opt(self):
        return self._pacing

    def pacing_release(self, reservation, reason):
        if self._pacing is not None and reservation:
            self._pacing.release(str(reservation), reason)


class RaisingPacing:
    def begin(self, *args, **kwargs):
        raise RuntimeError("pacing db unavailable")


def _thread_reserve(store: PP.PacingStore, cand: dict, barrier, out: list) -> None:
    try:
        barrier.wait(timeout=5)
        out.append(store.begin(cand))
    except Exception as exc:                       # pragma: no cover
        out.append({"error": f"{type(exc).__name__}: {exc}"})


def main() -> int:
    old = {name: getattr(C, name) for name in (
        "PROACTIVE_PACING_MODE", "PROACTIVE_LEVEL",
        "PROACTIVE_MIN_INTERVAL_MINUTES",
        "PROACTIVE_NONESSENTIAL_DAILY_CEILING",
        "PROACTIVE_POST_USER_GRACE_MINUTES",
        "PROACTIVE_FIRST_UNANSWERED_BACKOFF_MINUTES",
        "PROACTIVE_REPEATED_UNANSWERED_BACKOFF_MINUTES",
        "PROACTIVE_PACING_RESERVATION_TTL_S",
        "PROACTIVE_PACING_LOG", "PROACTIVE_PACING_DIR")}
    tmp = Path(tempfile.mkdtemp(prefix="proactive_pacing_"))
    now = time.time()
    try:
        C.PROACTIVE_MIN_INTERVAL_MINUTES = 180
        C.PROACTIVE_NONESSENTIAL_DAILY_CEILING = 3
        C.PROACTIVE_POST_USER_GRACE_MINUTES = 60
        C.PROACTIVE_FIRST_UNANSWERED_BACKOFF_MINUTES = 720
        C.PROACTIVE_REPEATED_UNANSWERED_BACKOFF_MINUTES = 1440
        C.PROACTIVE_PACING_RESERVATION_TTL_S = 120
        C.PROACTIVE_PACING_LOG = True
        C.PROACTIVE_PACING_DIR = tmp / "logs"
        PP.reset_for_tests()

        print("— F01 off 基线 —")
        off_path = tmp / "off.db"
        off = PP.PacingStore(off_path, mode_name="off", level_name="low")
        check("off 允许原候选", off.begin(_share())["allowed"] is True)
        check("off 不创建状态库", not off_path.exists(), str(off_path))
        check("off 状态不读真实库", off.status()["mode"] == "off")
        check("off 不做节流判断",
              PP.decide(_share(), {}, mode_name="off",
                        level_name="essential")["reason"] == "gate_off")
        off.close()

        print("— F02 shadow 无副作用 —")
        shadow_log = tmp / "shadow.jsonl"
        shadow = PP.PacingStore(None, mode_name="shadow", level_name="low",
                                log_path=shadow_log)
        r = shadow.begin(_share("shadow"))
        check("shadow 允许真实行为继续", r.get("allowed") is True)
        check("shadow 结果标记 would_suppress/allow",
              r.get("shadow_result") in ("would_allow", "would_suppress"), str(r))
        shadow.observe_sent(_share("shadow"), now=now)
        check("shadow 只改模拟状态",
              "shadow" in shadow.status(now=now)["mode"])
        lines = [x for x in shadow_log.read_text(encoding="utf-8").splitlines()
                 if x.strip()]
        check("shadow 写审计日志", len(lines) >= 1, str(len(lines)))
        check("shadow 日志不写正文",
              all("text" not in json.loads(x) for x in lines))
        check("shadow 不创建数据库",
              not (tmp / "shadow.db").exists())
        shadow.close()

        print("— F03 全局最小间隔与并发 —")
        d = PP.decide(_share("min-1"),
                      _state(now, last_nonessential_sent_at=now - 60),
                      now=now, mode_name="enforced", level_name="low")
        check("3 小时内拒绝", d["reason"] == "min_interval", str(d))
        d2 = PP.decide(_share("min-2"),
                       _state(now, last_nonessential_sent_at=now - 180 * 60),
                       now=now, mode_name="enforced", level_name="low")
        check("满 3 小时不强制发送、但允许竞争",
              d2["allowed"] is True and d2["reason"] == "allowed", str(d2))
        atomic = tmp / "atomic.db"
        s1 = PP.PacingStore(atomic, mode_name="enforced", level_name="low")
        s2 = PP.PacingStore(atomic, mode_name="enforced", level_name="low")
        barrier = threading.Barrier(2)
        out: list = []
        ts = [threading.Thread(target=_thread_reserve,
                               args=(s1, _share("atomic-a"), barrier, out)),
              threading.Thread(target=_thread_reserve,
                               args=(s2, _share("atomic-b"), barrier, out))]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        check("并发只产生一个预留",
              sum(1 for x in out if x.get("result") == "reserved") == 1,
              str(out))
        check("另一个被 reservation_active 挡住",
              any(x.get("reason") == "reservation_active" for x in out), str(out))
        winner = next(x for x in out if x.get("result") == "reserved")
        s1.settle_sent(winner["reservation_id"], _share("atomic-a"), now=now + 1)
        check("结算后不再有活动预留",
              not s1.status(now=now + 2)["reservation_active"])
        s1.close()
        s2.close()

        print("— F04 日上限与时区/日切 —")
        base = _state(now, day_key=_day(now), day_count=3)
        d = PP.decide(_share("ceiling"), base, now=now,
                      mode_name="enforced", level_name="low")
        check("达到每日上限后拒绝", d["reason"] == "daily_ceiling", str(d))
        next_day = now + 86400
        d = PP.decide(_share("rollover"),
                      _state(next_day - 1, day_key=_day(now), day_count=3,
                             last_nonessential_sent_at=next_day - 60),
                      now=next_day, mode_name="enforced", level_name="low")
        check("日切只重置计数，不重置最小间隔",
              d["reason"] == "min_interval", str(d))
        d = PP.decide(_share("rollover-ok"),
                      _state(next_day - 1, day_key=_day(now), day_count=3),
                      now=next_day, mode_name="enforced", level_name="low")
        check("日切后可以重新竞争", d["allowed"] is True, str(d))

        print("— F05 必要事项透传 —")
        blocked = _state(now, day_count=3, backoff_until=now + 3600,
                         post_user_grace_until=now + 3600)
        d = PP.decide(_promise(), blocked, now=now,
                      mode_name="enforced", level_name="low")
        check("必要承诺不占非必要额度/不被退避拦住",
              d["allowed"] is True and d["essential"] is True, str(d))
        d = PP.decide(_share("normal-during-block"), blocked, now=now,
                      mode_name="enforced", level_name="low")
        check("普通话题仍被拦", d["allowed"] is False, str(d))
        d = PP.decide(_promise(), blocked, now=now, quiet=True,
                      mode_name="enforced", level_name="low")
        check("安静仍然高于必要事项", d["reason"] == "quiet", str(d))

        print("— F06 用户输入宽限 —")
        grace_state = _state(now, post_user_grace_until=now + 60 * 60)
        d = PP.decide(_share("grace"), grace_state, now=now,
                      mode_name="enforced", level_name="low")
        check("输入后 60 分钟内不插入主动话题", d["reason"] == "user_grace", str(d))
        d = PP.decide(_share("grace-past"),
                      _state(now, post_user_grace_until=now - 1),
                      now=now, mode_name="enforced", level_name="low")
        check("宽限期结束可恢复竞争", d["allowed"] is True, str(d))

        print("— F07 未回复退避与真实输入 —")
        backoff_db = tmp / "backoff.db"
        bs = PP.PacingStore(backoff_db, mode_name="enforced", level_name="low")
        b = bs.begin(_share("backoff-1"), now=now)
        bs.settle_sent(b["reservation_id"], _share("backoff-1"), now=now + 1)
        one = bs.status(now=now + 2)
        check("首次未回复退避 12 小时",
              one["backoff_until"] != "" and one["unanswered_count"] == 1,
              str(one))
        d = bs.begin(_share("backoff-2"), now=now + 3600)
        check("退避期间不发送", d["allowed"] is False and d["reason"] == "backoff",
              str(d))
        bs.note_user_input(now=now + 3601)
        after = bs.status(now=now + 3602)
        check("真实输入结束退避并开始宽限",
              after["backoff_until"] == "" and after["unanswered_count"] == 0
              and after["post_user_grace_until"] != "", str(after))
        d = bs.begin(_share("backoff-3"), now=now + 3602)
        check("输入后仍先走宽限，不补发", d["reason"] == "user_grace", str(d))
        d = bs.begin(_share("backoff-4"), now=now + 4 * 3600)
        check("宽限与间隔都过后才可再竞争",
              d.get("allowed") is True, str(d))
        bs.close()
        repeat = PP.PacingStore(tmp / "repeat.db", mode_name="enforced",
                                level_name="low")
        rb = repeat.begin(_share("repeat-1"), now=now)
        repeat.settle_sent(rb["reservation_id"], _share("repeat-1"), now=now + 1)
        rb2 = repeat.begin(_share("repeat-2"), now=now + 12 * 3600 + 2)
        check("连续未回复时第二次竞争需等过 12 小时",
              rb2.get("allowed") is True, str(rb2))
        repeat.settle_sent(rb2["reservation_id"], _share("repeat-2"),
                           now=now + 12 * 3600 + 3)
        second = repeat.status(now=now + 12 * 3600 + 4)
        second_backoff = PP._parse_iso(second["backoff_until"])
        check("连续两条未回复改为 24 小时退避",
              second["unanswered_count"] == 2
              and second_backoff >= now + 12 * 3600 + 23 * 3600,
              str(second))
        repeat.close()

        print("— F08 用户控制 —")
        check("essential 只保留必要事项",
              PP.decide(_share("essential"), {}, mode_name="enforced",
                        level_name="essential")["reason"] == "level_essential")
        check("low 放行高价值 share",
              PP.decide(_share("low-share"), {}, mode_name="enforced",
                        level_name="low")["allowed"] is True)
        check("low 暂停 react",
              PP.decide(_react("low-react"), {}, mode_name="enforced",
                        level_name="low")["reason"] == "intent_not_allowed")
        check("low 放行 continue",
              PP.decide(_continue("low-cont"), {}, mode_name="enforced",
                        level_name="low")["allowed"] is True)
        check("normal 恢复八类意图",
              PP.decide(_react("normal-react"), {}, mode_name="enforced",
                        level_name="normal")["allowed"] is True)
        check("停止/暂停高于等级",
              PP.decide(_promise(), {}, paused=True,
                        mode_name="enforced", level_name="normal")["reason"]
              == "pause_stated")
        check("低价值 share 被拦",
              PP.decide(_share("low-value", evidence="contextual_only"),
                        {}, mode_name="enforced",
                        level_name="low")["reason"] == "low_value")

        print("— F09/F10 禁止补发与候选过期 —")
        release_db = tmp / "release.db"
        rs = PP.PacingStore(release_db, mode_name="enforced", level_name="low")
        b = rs.begin(_share("release-1"), now=now)
        rs.release(b["reservation_id"], "generation_failed", now=now + 1)
        st = rs.status(now=now + 2)
        check("释放预留不产生发送计数",
              not st["reservation_active"] and st["day_count"] == 0, str(st))
        expired = _share("expired", expires=now - 1)
        d = PP.decide(expired, {}, now=now,
                      mode_name="enforced", level_name="low")
        check("过期候选直接 expired", d["reason"] == "expired", str(d))
        check("过期候选不进入队列（模块无 backlog）",
              PP.decide(expired, {}, now=now,
                        mode_name="enforced", level_name="low")["allowed"] is False)
        rs.close()

        print("— F11 模型前拦截 —")
        chat_db = tmp / "chat.db"
        cs = PP.PacingStore(chat_db, mode_name="enforced", level_name="low")
        cs.note_user_input(now=time.time())
        fake = FakeChat(pacing=cs)
        seed = {"ok": True, "kind": "topic",
                "plan": _share("chat-block")["plan"] if False else {
                    "intent": "share", "evidence_state": "eligible",
                    "source_types": ["learning_result"],
                    "new_value_code": "new_information",
                    "plan_id": "pp_chat-block",
                    "novelty_key": "nk_chat-block",
                    "content_goal": "把一条已审结论讲给他",
                    "why_now": "刚审完",
                    "expires_at": "2099-01-01T00:00:00+08:00",
                    "question_budget": 0, "reply_expectation": "none"},
                "seed": "topic:chat-block"}
        fake._proactive_llm = FakeLLM()
        r = Chat.proactive(fake, seed)
        check("宽限/间隔内不调用真实模型",
              fake._proactive_llm.calls == 0, str(fake._proactive_llm.calls))
        check("被拦后返回 silent 与原因",
              r.get("ok") is False and r.get("silent") is True
              and r.get("reason") == "user_grace", str(r))
        cs.close()

        print("— F11b 合法候选仍能生成 —")
        ok_db = tmp / "chat-ok.db"
        oks = PP.PacingStore(ok_db, mode_name="enforced", level_name="low")
        ok_chat = FakeChat(pacing=oks)
        ok_seed = {"ok": True, "kind": "topic",
                   "plan": {
                       "intent": "share", "evidence_state": "eligible",
                       "source_types": ["learning_result"],
                       "new_value_code": "new_information",
                       "plan_id": "pp_chat-ok", "novelty_key": "nk_chat-ok",
                       "content_goal": "把一条已审结论讲给他",
                       "why_now": "刚审完",
                       "expires_at": "2099-01-01T00:00:00+08:00",
                       "question_budget": 0, "reply_expectation": "none"},
                   "seed": "topic:chat-ok"}
        r = Chat.proactive(ok_chat, ok_seed)
        check("节奏放行时正常调用模型",
              r.get("ok") is True and ok_chat._proactive_llm.calls == 1, str(r))
        check("生成成功返回预留 id",
              bool(r.get("pacing_reservation")), str(r))
        oks.settle_sent(r["pacing_reservation"], r.get("pacing_candidate"),
                        now=time.time() + 1)
        check("合法发送结算后计入日计数",
              oks.status(now=time.time() + 2)["day_count"] == 1)
        oks.close()

        print("— F12 原子预留结算 —")
        # 已在上面的并发与退避场景验证；这里补一次错误 reservation 不重复结算。
        atomic2 = tmp / "atomic2.db"
        a = PP.PacingStore(atomic2, mode_name="enforced", level_name="low")
        b1 = a.begin(_share("atomic2-a"), now=now)
        check("合法预留可结算",
              a.settle_sent(b1["reservation_id"], _share("atomic2-a"),
                            now=now + 1)["ok"] is True)
        check("同一预留不能结算两次",
              a.settle_sent(b1["reservation_id"], _share("atomic2-a"),
                            now=now + 2)["reason"] == "reservation_not_found")
        a.close()

        print("— F13 跨通道共享节奏 —")
        cross = tmp / "cross.db"
        x = PP.PacingStore(cross, mode_name="enforced", level_name="low")
        xb = x.begin(_share("phone"), now=now)
        x.settle_sent(xb["reservation_id"], _share("phone"), now=now + 1)
        d = x.begin(_share("desktop"), now=now + 2)
        check("手机发过后桌面也受同一退避/间隔约束",
              d["allowed"] is False and d["reason"] in (
                  "min_interval", "backoff"), str(d))
        x.close()

        print("— F14 故障隔离 —")
        bad = PP.PacingStore(None, mode_name="enforced", level_name="low")
        d = bad.begin(_share("unavailable"))
        check("状态不可用时不发非必要候选",
              d["allowed"] is False and d["reason"] == "pacing_unavailable",
              str(d))
        bad.close()
        raising = FakeChat(pacing=RaisingPacing())
        r = Chat.proactive(raising, seed)
        check("节奏层异常不抛出、主动候选安全失败",
              r.get("ok") is False and r.get("silent") is True
              and r.get("reason") == "pacing_unavailable", str(r))

        print("— F15 无关系写入 —")
        no_write = FakeChat(pacing=PP.PacingStore(
            tmp / "norel.db", mode_name="shadow", level_name="low",
            log_path=tmp / "norel.jsonl"))
        no_write._proactive_llm = FakeLLM()
        r = Chat.proactive(no_write, seed)
        check("主动生成不写 FakeMem",
              FakeMem.write_calls == 0, str(FakeMem.write_calls))
        check("主动生成成功时不产生关系库",
              not (tmp / "p3.db").exists() and not (tmp / "memory.db").exists())
        no_write._pacing.close()

        print("— F16 回滚与回归 —")
        roll = tmp / "rollback.db"
        r1 = PP.PacingStore(roll, mode_name="enforced", level_name="low")
        b = r1.begin(_share("rollback-1"), now=now)
        r1.settle_sent(b["reservation_id"], _share("rollback-1"), now=now + 1)
        r1.close()
        before = hashlib.sha256(roll.read_bytes()).hexdigest()
        r2 = PP.PacingStore(roll, mode_name="off", level_name="normal")
        d = r2.begin(_share("rollback-2"), now=now + 2)
        after = hashlib.sha256(roll.read_bytes()).hexdigest()
        check("off 回滚允许原行为", d.get("allowed") is True)
        check("off 回滚不写真实节奏状态", before == after, f"{before} != {after}")
        r2.close()
        check("旧主动候选仍存在且可判定",
              PP.decide(_react("legacy"), {}, mode_name="enforced",
                        level_name="normal")["allowed"] is True)
    finally:
        PP.reset_for_tests()
        for name, value in old.items():
            setattr(C, name, value)
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{PASS} 通过 / {FAIL} 失败")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
