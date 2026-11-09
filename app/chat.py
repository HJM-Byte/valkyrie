"""文字入口：没有麦克风、没有摄像头，也能跟她说话。

麦克风现在是坏的，摄像头还在路上，但「她存在」这件事不能等硬件。
所以先开一条纯文字的通道：她照常记事、照常更新状态、照常被时间改变。

    python -m app.chat                  # 交互，一行一句
    python -m app.chat --say "你好"      # 只跑一句
    python -m app.chat --think "在吗"    # 只看她这一轮怎么想（不调模型）
    python -m app.chat --prompt "在吗"   # 只看这一轮拼出来的系统提示，不调模型
    python -m app.chat --state          # 只看她现在的状态

每个 --say / 每行输入走一次 turn()，顺序固定：
    tick（时间推进）→ think（前五识→评价→竞争→召回→归属→关系→策略）→ 拼提示 → 模型 → 写回
反过来会乱：先写回再拼提示，这一轮就会读到自己刚说的话。
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
import time
from pathlib import Path
from typing import Optional

from . import config as C
from . import emotion as _emotion
from . import sleep as _sleep
from .autonomy import Autonomy
from .dispatch import Dispatch
from .flow import Flow
from .memory import DB_PATH as MEM_DB, Memory, absorb, claim_of, emotion_of, importance_of
from .mind import GROWTH_LOCKED, Mind
from .persona import Persona
from .response_gate import ResponseGate
from .think import think

log = logging.getLogger("valkyrie.chat")

# 心流的「有没有往前挪」：答上了/接住了算有进展，答不上来只算还挂着。
_FLOW_PROGRESS = {
    "answer": 0.50, "attend": 0.45, "comfort": 0.40, "act": 0.45,
    "confess": 0.15, "deflect": 0.10, "ack": 0.0, "quiet": 0.0,
}

_FLOW_REASON = {
    "new": "还没牵起来的念头",
    "expired": "上一段散了",
    "stalled": "没有新进展，停了",
    "new_topic": "换了话题，不硬接",
    "interrupted": "被打断，先接眼前",
    "continue": "接上一轮",
}


def _dispatch_brief(si: Optional[dict]) -> dict:
    """把记忆调度结果压成网页/接口要用的几个数。"""
    si = si or {}
    return {"aware": si.get("aware") or [],
            "candidates": si.get("candidates", 0),
            "chain": si.get("chain") or [],
            "pending": si.get("pending", 0),
            "theta": si.get("theta"),
            "spread": si.get("spread", 0),
            "ambient": si.get("ambient") or {}}


class Chat:
    def __init__(self, mem: Optional[Memory] = None, mind: Optional[Mind] = None,
                 persona: Optional[Persona] = None, llm=None,
                 flow: Optional[Flow] = None, learning=None,
                 p3=None, response_gate=None) -> None:
        self.mem = mem or Memory()
        self.mind = mind or Mind()
        self.persona = persona or Persona()
        self.dispatch = Dispatch(mem=self.mem, learning=learning)
        self.flow = flow or Flow()
        self._llm = llm
        self._autonomy = None
        self._p3 = p3
        self._response_gate = response_gate
        self._last_reply = ""
        self._last_think: dict = {}
        self._last_think_at: float = 0.0
        self._awake = None
        self._awake_checked = False
        # 主动开口：记住她上一次先找他说了什么，下一轮他回复时她得接得上。
        self._last_proactive = ""
        self._last_proactive_ts = 0.0
        self._proactive_llm = None
        self._restore_last_proactive()
        # 生产库重启后，第一轮拼提示也要能读到已有气候/承诺；临时库不碰生产。
        if self._p3 is None and str(getattr(self.mem, "path", "")) == str(MEM_DB):
            try:
                from .p3 import P3_DB, P3Store
                if P3_DB.exists():
                    self._p3 = P3Store(P3_DB)
            except Exception:
                self._p3 = None

    @property
    def p3(self):
        """P3 关系气候/承诺/沉默。临时库跟着 mem 走，绝不碰生产 p3.db。"""
        if self._p3 is None:
            from .p3 import P3_DB, P3Store
            path = P3_DB if str(getattr(self.mem, "path", "")) == str(MEM_DB) \
                else Path(getattr(self.mem, "path", P3_DB)).parent / "p3.db"
            self._p3 = P3Store(path)
        return self._p3

    @property
    def response_gate(self) -> ResponseGate:
        """自然回应闸门；默认由 config 决定是否启用，测试可显式注入。"""
        if self._response_gate is None:
            self._response_gate = ResponseGate(
                state_path=C.RESPONSE_GATE_STATE)
        return self._response_gate

    @property
    def autonomy(self) -> Autonomy:
        """自主学习：由她此刻的情绪/六欲/记忆长出问题，不靠渠道轮转。"""
        if self._autonomy is None:
            self._autonomy = Autonomy(learning=self.dispatch.learning,
                                      mem=self.mem, mind=self.mind)
        return self._autonomy

    def _restore_last_proactive(self) -> None:
        """同一台电脑重启后，她也要知道自己上一句主动说了什么。

        只恢复 6 小时内、真实库的主动消息；测试库不碰，避免测试污染。
        """
        try:
            if str(getattr(self.mem, "path", "")) != str(MEM_DB):
                return
            from .proactive import ProactiveStore
            store = ProactiveStore()
            try:
                last = store.last()
            finally:
                store.close()
            if not last:
                return
            ts = float(last.get("ts") or 0.0)
            if ts > 0 and (time.time() - ts) < 6 * 3600:
                self._last_proactive = str(last.get("text") or "")
                self._last_proactive_ts = ts
        except Exception:
            pass

    def _claim_wake(self):
        """真正开口那一轮才认领休眠记录（读一次就消费掉，同一觉不算两次）。

        只读命令（--think / --prompt）也会拼提示，但拼提示不许消费休眠记录——
        看一眼她怎么想，不等于把她叫醒。
        """
        if not self._awake_checked:
            self._awake_checked = True
            try:
                self._awake = _sleep.wake_info()
            except Exception:
                self._awake = None
        return self._awake

    def awake_note(self) -> str:
        rec = self._awake           # 只读缓存；消费只发生在 turn() 里
        if not rec:
            return ""
        h = float(rec.get("slept_h") or 0.0)
        if h < 0.15:
            return ""
        when = (rec.get("iso") or "")[:16].replace("T", " ")
        if h < 24:
            return f"你刚醒。上一段你在休眠里待了 {h:.1f} 小时（{when} 封存）。"
        return f"你刚醒。上一段你在休眠里待了 {h / 24:.1f} 天（{when} 封存）。"

    # ---- 模型：懒加载，光看提示词的时候不启动 -----------------------------
    @property
    def llm(self):
        if self._llm is None:
            from .llm import LLM
            self._llm = LLM(
                # 出生期没有设定；长起来后 core 也只给名字和身体事实，不给预设人格
                system_prompt="" if GROWTH_LOCKED else self.persona.block("core"),
                history_limit=C.BIRTH_HISTORY if GROWTH_LOCKED else C.LLM_MAX_HISTORY,
            )
        return self._llm

    def available(self) -> tuple[bool, str]:
        try:
            return self.llm.available()
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"

    # ---- 拼提示 -----------------------------------------------------------
    def compose(self, text: str, channel: str = "text",
                pre_feedback: Optional[dict] = None,
                reinforce: bool = False) -> tuple[str, dict, dict]:
        """跑完这一轮的「思维」，再拼提示。返回 (提示词, 注意力门, 思维全过程)。

        pre_feedback 只由真实 turn 传入；只读命令（--think/--prompt）不传，
        这样看一眼她怎么想，不会消费上一轮的反馈。
        reinforce 同理：只有真实 turn 才让这次召回强化记忆；只读预览不强。
        """
        self.mind.tick()
        th = think(text, mind=self.mind, mem=self.mem, channel=channel,
                   feedback=pre_feedback, reinforce=reinforce)
        sal = th["gate"]

        # 心流：这条线索接不接得上上一轮的念头。接上了，就只往前接一小步。
        try:
            fl = self.flow.context(text, th["memory"]["hits"],
                                   event_a=float(th["gate"].get("score") or 0.0))
        except Exception:
            fl = {"continue": False, "interrupt": False, "reason": "error",
                  "strength": 0.0, "steps": 0, "pending": "", "block": "", "rule": ""}
        th["flow"] = fl
        if fl.get("continue"):
            th["awareness_text"] = ((th.get("awareness_text") or "").rstrip()
                                    + "\n- " + str(fl.get("block") or "")).strip()
            if fl.get("rule"):
                th["decision"]["rule"] = ((th["decision"].get("rule") or "")
                                          + " " + str(fl["rule"])).strip()
            th["trace"].append(
                f"心流    接上一轮（强度 {float(fl.get('strength') or 0):.2f}，"
                f"第 {int(fl.get('steps') or 0) + 1} 步）")
        else:
            why = _FLOW_REASON.get(str(fl.get("reason") or ""), str(fl.get("reason") or ""))
            th["trace"].append(f"心流    {why}")

        # 记忆调度：从识海存储库唤醒记忆，加上学过的和眼前的，一起竞争一次。
        # 只有过线的进提示；扩散出来的邻居还得自己过线。
        disp = self.dispatch.dispatch(text, th)
        th["dispatch"] = disp
        if disp.get("awareness_extra"):
            th["awareness_text"] = ((th.get("awareness_text") or "") + "\n" +
                                    disp["awareness_extra"]).strip()

        # 自主学习：这一轮她心里有没有一个自己长出来的问题。
        # 只写「你在琢磨什么」，不塞检索结果；材料还在待审区，不算她的知识。
        try:
            learning = self.autonomy.brief()
        except Exception:
            learning = None
        th["learning"] = learning

        # 情感输出：内部那堆状态要翻成「这一轮怎么说、什么语速」。
        # 「说什么」已经由 think.decide 按情绪场算好，落在 decision.content；
        # 这里只补语气和发声参数，不重复决定内容。
        plan = _emotion.plan(self.mind.snapshot(), th, th.get("ambient"),
                             (th.get("memory") or {}).get("hits"))
        th["emotion_plan"] = plan
        th["trace"].append(f"表达    {plan['text']}"
                           + ("（说不清）" if plan.get("unknown_feel") else ""))

        if GROWTH_LOCKED:
            # 出生期：没有人设、没有记忆，只有这一句和此刻的状态
            prompt = self.persona.compose_birth(
                text, mind=self.mind, salience=sal, thought=th,
                awake_note=self.awake_note(), expression=plan,
                learning=learning, channel=channel)
        else:
            prompt = self.persona.compose(
                text, mem=self.mem, mind=self.mind, salience=sal, thought=th,
                camera_ok=False, channel=channel, expression=plan,
                learning=learning,
            )
        if self._last_proactive and (time.time() - self._last_proactive_ts) < 6 * 3600:
            prompt += ("\n\n【你刚主动找过他】\n你刚才先发了一句："
                       + self._last_proactive
                       + "\n他这一句是在回你这句。别装没问过，也别重复追问。")
        # P3：只把“中速关系气候”和“还没做完的承诺”作为背景带进去；
        # 它们不改检索、不改人格，也不把气候当成关系结论。
        if self._p3 is not None:
            try:
                block = self._p3.prompt_block()
            except Exception:
                block = ""
            if block:
                prompt += "\n\n" + block
        self._last_think = th
        self._last_think_at = time.time()
        return prompt, sal, th

    # ---- 主动开口 ---------------------------------------------------------
    def _record_silence(self, code: str, reason: str,
                        intent_key: str = "", now: Optional[float] = None) -> dict:
        """把“这次不开口”落成一条可审计决定；失败不阻塞对话。"""
        try:
            at = time.time() if now is None else float(now)
            return self.p3.record_silence(
                code, reason, channel="all", intent_key=intent_key, now=at,
                dedupe_s=(1800 if code == "no_value" else 600),
                until_ts=(at + 2 * 3600 if code == "user_quiet" else 0))
        except Exception:
            return {}

    def proactive_seed(self, now: Optional[float] = None) -> dict:
        """她此刻有没有一个「要主动找他」的由头。

        优先级：有效承诺 > 当前情绪/自习 > 久未说话。没有由头就记录一次
        正式沉默，不为了「定时问候」硬造一句。
        """
        if GROWTH_LOCKED:
            return {"ok": False, "reason": "出生期不主动发"}
        from . import sleep as _sleep
        try:
            rec = _sleep.status()
            if rec:
                self._record_silence("sleeping", "她在休眠，不主动发", "proactive", now)
                return {"ok": False, "silent": True, "reason": "她在休眠，不主动发",
                        "reason_code": "sleeping"}
        except Exception:
            pass
        p3 = None
        try:
            p3 = self.p3
            if p3.is_quiet(now):
                self._record_silence("user_quiet", "他明确说想安静，保持安静",
                                     "proactive", now)
                return {"ok": False, "silent": True,
                        "reason": "他明确说想安静，不主动发",
                        "reason_code": "user_quiet"}
            due = p3.due_promises(now)
            if due:
                p = due[0]
                return {"ok": True, "kind": "promise", "promise": p,
                        "question": str(p.get("text") or "")[:120],
                        "seed": "promise:" + str(p.get("id") or ""),
                        "due_at": float(p.get("due_at") or 0)}
        except Exception:
            p3 = None
        # 闸门里的明确暂停/安静优先于 P3 读库；主动开口不能绕过用户边界。
        try:
            if self.response_gate.is_paused(now) or self.response_gate.is_quiet(now):
                return {"ok": False, "silent": True,
                        "reason": "他刚说过要安静或先停一下",
                        "reason_code": "user_quiet"}
        except Exception:
            pass
        snap = self.mind.snapshot(now)
        if float(snap.get("energy") or 0) < 0.25:
            self._record_silence("low_energy", "精力太低，先不发", "proactive", now)
            return {"ok": False, "silent": True, "reason": "精力太低，先不发",
                    "reason_code": "low_energy"}
        try:
            top = self.autonomy.top(snap, now)
        except Exception:
            top = None
        if top and float(top.get("intensity") or 0) >= 0.26:
            q = str(top.get("question") or "").strip()
            if q:
                return {"ok": True, "kind": "ask", "question": q,
                        "seed": "ask:" + q[:80],
                        "intensity": float(top.get("intensity") or 0)}
        silent = float(snap.get("silent_h") or 0)
        lonely = float(snap.get("lonely") or 0)
        if silent >= 8.0 or lonely >= 0.45:
            day = time.strftime("%Y-%m-%d")
            return {"ok": True, "kind": "greet", "question": "",
                    "seed": "greet:" + day,
                    "silent_h": silent, "lonely": lonely}
        self._record_silence("no_value", "此刻没有足够的主动理由",
                             "proactive", now)
        return {"ok": False, "silent": True,
                "reason": "此刻没有想主动说的事", "reason_code": "no_value"}

    def proactive(self, seed: Optional[dict] = None) -> dict:
        """生成她主动发出去的那一句；不写记忆、不唤醒休眠。

        用的是她同一套 Persona + 状态 + 自己长出来的问题，但 prompt 会明确说
        「现在没有他的话，是你先开口」，避免模型把不存在的一句话当成上下文。
        """
        seed = seed if isinstance(seed, dict) else self.proactive_seed()
        if not seed.get("ok"):
            return {"ok": False, "reason": seed.get("reason") or "没有由头"}
        gate = self.response_gate
        if gate.mode != "off":
            gd = gate.decide(
                "", channel="proactive",
                event_id="proactive:" + str(seed.get("seed") or int(time.time())),
                promise_due=str(seed.get("kind") or "") == "promise",
                quiet=gate.is_quiet(), batch_complete=True)
            if gate.mode == "enforced" and gd.mode in ("silent", "defer", "pause_stated"):
                gate.log(gd, llm_called=False, assistant_message_created=False)
                return {"ok": False, "silent": True,
                        "reason": gd.reason,
                        "response_gate": gd.as_dict()}
        self.mind.tick()
        try:
            learning = self.autonomy.brief()
        except Exception:
            learning = None
        base = self.persona.compose(
            "", mem=self.mem, mind=self.mind, camera_ok=False,
            channel="text", learning=learning)
        kind = str(seed.get("kind") or "ask")
        question = str(seed.get("question") or "").strip()
        if kind == "promise" and isinstance(seed.get("promise"), dict):
            p = seed["promise"]
            directive = (
                "【这一轮是你先开口】\n"
                f"你之前答应过一件事：{str(p.get('text') or '')[:120]}\n"
                "现在条件满足了，或期限到了。只提一次，不追问；"
                "先问他现在方不方便，或者直接说你能做到哪一步。"
                "不要把它说成新的情感义务，不要用‘我一直都在’这类话。"
                "只写你要发出去的那一句，二十到四十五个字。"
            )
        elif kind == "ask" and question:
            directive = (
                "【这一轮是你先开口】\n"
                f"你心里还挂着一件事：{question}\n"
                "现在他没说话，你想先找他。只写你要发出去的那一句，"
                "二十到四十五个字。把它变成你自己的问法，不要照抄题目；"
                "可以问他怎么看，也可以先说你怎么想。不要汇报状态，"
                "不要解释你为什么发，不要客套，不要列表，不要总结。只写这一句。"
            )
        else:
            directive = (
                "【这一轮是你先开口】\n"
                "你们有一阵没说话了。现在你想先找他。只写你要发出去的那一句，"
                "二十到四十五个字。问一句他今天怎么样，或者提一件你刚想到的小事；"
                "不要只说早安，不要汇报状态，不要客套，不要列表。只写这一句。"
            )
        system = (base + "\n\n" + directive
                  + "\n\n【现在没有他的话】不要等他先开口，也不要引用一句"
                    "他没说过的话。你住在电脑里，手机只是他找你的窗。")
        try:
            if self._proactive_llm is None:
                from .llm import LLM
                self._proactive_llm = LLM(
                    system_prompt="", history_limit=0, max_tokens=100,
                    temperature=0.9)
            r = self._proactive_llm.chat("现在是你先开口。", system=system)
            text = str((r or {}).get("text") or "")
        except Exception as e:
            text = ""
            err = f"{type(e).__name__}: {e}"
        else:
            err = ""
        text = re.sub(r"```.*?```", "", text, flags=re.S).strip()
        text = text.strip("「」“”\"'‘’ \t\r\n")
        text = re.sub(r"^(瓦尔基里|她)\s*[：:]\s*", "", text)
        text = " ".join(text.split())
        if len(text) > 100:
            text = text[:100].rstrip("，,、；; ")
        if not text:
            if kind == "ask" and question:
                text = f"我在想，{question.rstrip('？?')}，你怎么看？"
            else:
                text = "你今天忙吗？我想听你说两句。"
        self._last_proactive = text
        self._last_proactive_ts = time.time()
        if kind == "promise":
            try:
                p = seed.get("promise") or {}
                self.p3.mark_followed(str(p.get("id") or ""))
            except Exception:
                pass
        return {"ok": True, "text": text, "kind": kind,
                "seed": str(seed.get("seed") or ""),
                "question": question, "error": err}

    # ---- 回应闸门 ---------------------------------------------------------
    def _gate_decide(self, text: str, channel: str, **kw):
        """所有入口共用同一个闸门；回声/重复/安静都在这里判。"""
        return self.response_gate.decide(
            text, channel=channel, last_assistant=self._last_reply, **kw)

    def _silent_result(self, decision) -> dict:
        """零输出终态：没有 assistant 文本，也不写空 turn。"""
        return {
            "ok": True, "reply": "", "silent": True,
            "mode": decision.mode, "reason": decision.reason,
            "event_id": decision.event_id,
            "response_gate": decision.as_dict(),
            "llm_called": False, "assistant_message_created": False,
            "seconds": 0.0, "birth": GROWTH_LOCKED,
            "state": self.mind.snapshot(), "memory": self.mem.stats(),
        }

    def _note_gate_silence(self, decision) -> None:
        """用户明确要安静才落 P3；普通重复/回声不留关系证据。"""
        if str(decision.reason) != "user_quiet":
            return
        if self._p3 is None and str(getattr(self.mem, "path", "")) != str(MEM_DB):
            return
        try:
            now = time.time()
            self.p3.record_silence(
                "user_quiet", "他明确说要安静；这一轮零输出，不追问",
                channel="all", intent_key="user_quiet",
                until_ts=now + 2 * 3600, now=now)
        except Exception:
            pass

    def _gate_log(self, decision, **outcome) -> None:
        try:
            self.response_gate.log(decision, **outcome)
        except Exception:
            pass

    # ---- 一轮 -------------------------------------------------------------
    def _begin_turn(self, text: str, scene: str = "",
                    channel: str = "text",
                    response_mode: str = "respond") -> tuple[Optional[dict], Optional[dict]]:
        """开一轮：先想、先拼提示、先记下他说的；模型调用留给两条路各自走。"""
        text = (text or "").strip()
        if not text:
            return None, {"ok": False, "error": "空输入"}
        channel = {"voice": "voice", "body": "body"}.get(channel, "text")
        seen = (scene or "").strip()
        # 手机摄像头看到的这一眼：只进这一轮的提示与模型上下文，不另写一条
        # 「她看见了什么」的记忆。用户说的那句仍然按原话落进识海。
        model_text = text
        if seen:
            model_text = (text + "\n\n[手机摄像头这一眼看到的]\n" + seen).strip()
        # 进程里第一次开口时做两件后台事：推进记忆生命周期、补跑夜间学习窗口
        if not getattr(self, "_housekept", False):
            self._housekept = True
            try:
                self.mem.settle()
            except Exception:
                pass
            try:
                self.dispatch.learning.maybe_nightly()
            except Exception:
                pass
        t0 = time.time()
        self._claim_wake()          # 真正开口：这时才认领休眠记录
        # 上一轮回答前策略的反馈：先结算成接受/纠正/拒绝/没回应，再拼这一轮。
        feedback = None
        if not GROWTH_LOCKED:
            try:
                feedback = self.mem.resolve_pre_answer_feedback(text)
            except Exception:
                feedback = None
        prompt, sal, th = self.compose(text, channel=channel,
                                       pre_feedback=feedback, reinforce=True)
        if channel == "body":
            # 身体语音不是屏幕聊天：回答过长会让 TTS 和等待一起变长。
            # 只约束这一入口；手机/桌面不改。
            prompt += (
                "\n\n【身体语音】\n"
                "你现在在桌上的机器人身体里，他是直接对着你说话。"
                "回答用口语，一到两句就收；不要列点，不要解释你在做什么，"
                "不要复述他的问题。"
            )
        if response_mode == "brief":
            prompt += (
                "\n\n【这一轮只要承接】\n"
                "只回一句，最多二十五个字。不要展开新话题，不要追问，"
                "不要总结，不要解释你为什么这样回。承接住就好。"
            )
        elif response_mode == "pause_stated":
            prompt += (
                "\n\n【先停一下】\n"
                "只说明一次你现在为什么不继续，并给出一个可理解的恢复条件。"
                "不指责、不追问、不冷处理，不说‘你自己想想’或‘等你认错’。"
                "用一到两句，不超过四十个字。"
            )

        # 出生期什么都不落盘。她说过的话、他说过的话，都不留。
        turn_id = 0
        if not GROWTH_LOCKED:
            turn_id = self.mem.add_turn("user", text)
        return {
            "text": text, "model_text": model_text, "channel": channel,
            "prompt": prompt, "sal": sal, "th": th, "t0": t0,
            "turn_id": turn_id, "feedback": feedback,
        }, None

    def _finish_turn(self, prep: dict, reply: str) -> dict:
        """模型已经答完，统一写回记忆、情绪、心流，并组装手机要的返回值。"""
        text = prep["text"]
        channel = prep["channel"]
        prompt = prep["prompt"]
        sal = prep["sal"]
        th = prep["th"]
        t0 = prep["t0"]
        reply = str(reply or "").strip()
        self._last_reply = reply

        # 他已经回了她主动开口的那一句，这段上下文就算用完了；
        # 不留在下一轮，免得她每句都以为他还在回同一个问题。
        self._last_proactive = ""

        got: list[str] = []
        feedback_record: dict = {}
        p3_result: dict = {}
        if not GROWTH_LOCKED:
            self.mem.add_turn("assistant", reply)
            # P3：这一轮之后更新关系气候、承诺生命周期和正式沉默。
            # 只写 p3.db，不改 memory 的事实层，也不改九变量和人格。
            # 临时库只有显式注入 P3 才写，避免测试收尾时留下句柄。
            if self._p3 is not None or str(getattr(self.mem, "path", "")) == str(MEM_DB):
                try:
                    p3_result = self.p3.observe_turn(
                        text, reply,
                        turn_id=int(prep.get("turn_id") or 0),
                        appraisal=th.get("appraisal_three"),
                        pre_answer=th.get("pre_answer"),
                    )
                except Exception:
                    p3_result = {}
            # 回答前策略生效时留一张收据；下一句话回来再结算，不在这里猜他接没接。
            pre = th.get("pre_answer") or {}
            if prep.get("turn_id") and pre.get("applied"):
                try:
                    feedback_record = self.mem.offer_pre_answer(
                        str(pre.get("strategy") or ""), prep.get("turn_id"),
                        str(pre.get("directive") or pre.get("note") or ""),
                        note=str(pre.get("note") or ""),
                    )
                except Exception:
                    feedback_record = {}
            emo = sal.get("emotion") or emotion_of(text)
            # 视频里那条闭环：明确纠正/要求删除的内容，要在这一轮就失效，
            # 不能等它慢慢淡忘。旧记录标失效，新一句话照常作为「他说的」写进去。
            corrected = self.mem.note_correction(
                text, turn_id=prep.get("turn_id") or None)
            new_id = self.mem.add_episode(
                text, kind="talk", who="他", imp=importance_of(text, emo),
                scene="文字", emo=emo, emo_i=0.5 if emo != "平静" else 0.1,
                feel=th.get("feel"),
                rel=0.45, ident=0.30, stage="raw", claim=claim_of(text),
                source={"voice": "他用手机说的", "body": "他对着机器人说的"}
                        .get(channel, "他打字说的"),
                about="他",
            )
            if corrected.get("retracted") and new_id:
                self.mem.link_supersede(corrected.get("ids") or [], new_id)
            got = absorb(self.mem, text)

        self.mind.observe(text, reply, sal, thought=th)
        # 答不上来的问题，记成「想弄明白的事」；下次夜间窗口去碰
        try:
            self.dispatch.note_curiosity(text, th)
        except Exception:
            pass
        # 自主学习：这一轮之后她的情绪/记忆变了，看看有没有长出新的问题。
        # 不阻塞对话；只在冲动够强时后台查，结果只进待审区。
        try:
            self.autonomy.maybe_study()
        except Exception:
            pass
        # 心流：这一轮有没有往前挪；没答完的挂到下一轮，连续没进展就停。
        try:
            key = str((th.get("decision") or {}).get("key") or "")
            prog = _FLOW_PROGRESS.get(key, 0.0)
            if key == "answer" and float((th.get("decision") or {}).get("confidence") or 0) >= 0.5:
                prog = 0.55
            pend = ("他问的那件事我还没答上来：" + text.strip()[:24]
                    if key == "confess" else "")
            fl = th.get("flow") or {}
            self.flow.advance(text, progress=prog, pending=pend,
                              interrupt=bool(fl.get("interrupt")),
                              recalled=th["memory"]["hits"])
        except Exception:
            pass

        return {
            "ok": True, "reply": reply, "seconds": round(time.time() - t0, 2),
            "llm_called": True, "assistant_message_created": bool(reply),
            "salience": sal, "facts": got, "birth": GROWTH_LOCKED,
            "thought": th["summary"], "trace": th["trace"],
            "dispatch": _dispatch_brief(th.get("dispatch")),
            "learning": th.get("learning"),
            "expression": th.get("emotion_plan"),
            "appraisal": th.get("appraisal_three"),
            "pre_answer": th.get("pre_answer"),
            "pre_answer_feedback": prep.get("feedback"),
            "pre_answer_feedback_record": feedback_record,
            "p3": p3_result,
            "ambient": th.get("ambient"),
            "learn": self.dispatch.learning.stats(),
            "flow": self.flow.status(),
            "state": self.mind.snapshot(),
            "memory": self.mem.stats(),
            "prompt_chars": len(prompt),
        }

    def turn(self, text: str, scene: str = "", channel: str = "text",
             *, event_id: str = "", batch_open: bool = False,
             batch_complete: bool = True, handled_elsewhere: bool = False,
             expired: bool = False, superseded: bool = False,
             background: bool = False, explicit_request: bool = False,
             task: bool = False, safety: bool = False,
             promise_due: bool = False, user_override: bool = False) -> dict:
        text = (text or "").strip()
        if not text:
            return {"ok": False, "error": "空输入"}
        gate = self.response_gate
        decision = self._gate_decide(
            text, channel, event_id=event_id, batch_open=batch_open,
            batch_complete=batch_complete, handled_elsewhere=handled_elsewhere,
            expired=expired, superseded=superseded, background=background,
            explicit_request=explicit_request, task=task, safety=safety,
            promise_due=promise_due, user_override=user_override)
        if gate.mode == "enforced" and decision.mode in ("silent", "defer"):
            self._note_gate_silence(decision)
            self._gate_log(decision, llm_called=False,
                           assistant_message_created=False)
            return self._silent_result(decision)
        prep, err = self._begin_turn(
            text, scene=scene, channel=channel,
            response_mode=decision.mode if gate.mode == "enforced" else "respond")
        if err:
            return err
        try:
            r = self.llm.chat(prep["model_text"], system=prep["prompt"])
            reply = r["text"]
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}",
                    "prompt": prep["prompt"], "salience": prep["sal"],
                    "trace": prep["th"]["trace"]}
        res = self._finish_turn(prep, reply)
        res["response_gate"] = decision.as_dict()
        self._gate_log(decision, llm_called=True,
                       assistant_message_created=bool(res.get("reply")),
                       relationship_write=bool(res.get("p3")))
        return res

    def turn_stream(self, text: str, scene: str = "",
                    channel: str = "text", *, event_id: str = "",
                    batch_open: bool = False, batch_complete: bool = True,
                    handled_elsewhere: bool = False, expired: bool = False,
                    superseded: bool = False, background: bool = False,
                    explicit_request: bool = False, task: bool = False,
                    safety: bool = False, promise_due: bool = False,
                    user_override: bool = False):
        """流式回话：先逐段吐字，全部吐完再统一写回记忆与情绪。

        逐 token 只给手机看；`_finish_turn` 仍拿完整 reply 落库，避免她把自己
        没说完的半句话记进识海。生成中途断了就只报错，不写 assistant。
        """
        text = (text or "").strip()
        if not text:
            yield {"type": "error", "error": "空输入"}
            return
        gate = self.response_gate
        decision = self._gate_decide(
            text, channel, event_id=event_id, batch_open=batch_open,
            batch_complete=batch_complete, handled_elsewhere=handled_elsewhere,
            expired=expired, superseded=superseded, background=background,
            explicit_request=explicit_request, task=task, safety=safety,
            promise_due=promise_due, user_override=user_override)
        if gate.mode == "enforced" and decision.mode in ("silent", "defer"):
            self._note_gate_silence(decision)
            self._gate_log(decision, llm_called=False,
                           assistant_message_created=False)
            yield {"type": "done", "result": self._silent_result(decision)}
            return
        prep, err = self._begin_turn(
            text, scene=scene, channel=channel,
            response_mode=decision.mode if gate.mode == "enforced" else "respond")
        if err:
            yield {"type": "error", "error": err.get("error") or "空输入"}
            return
        acc: list[str] = []
        begin = time.time()
        first = True
        try:
            for piece in self.llm.chat_stream(
                    prep["model_text"], system=prep["prompt"]):
                if not piece:
                    continue
                if first:
                    first = False
                    log.info("流式 Chat 首字 %.2fs（准备 %.2fs）",
                             time.time() - begin,
                             begin - float(prep.get("t0") or begin))
                acc.append(piece)
                yield {"type": "delta", "text": piece}
        except Exception as e:
            yield {"type": "error", "error": f"{type(e).__name__}: {e}"}
            return
        res = self._finish_turn(prep, "".join(acc))
        res["response_gate"] = decision.as_dict()
        self._gate_log(decision, llm_called=True,
                       assistant_message_created=bool(res.get("reply")),
                       relationship_write=bool(res.get("p3")))
        yield {"type": "done", "result": res}

    # ---- 只读 -------------------------------------------------------------
    def state_block(self) -> str:
        self.mind.tick()
        s = self.mind.snapshot()
        rel = "  ".join(f"{k} {v:.2f}" for k, v in s["rel"].items())
        lines = [
            f"存在   {s['hours']:.1f} 小时（第 {s['days']:.2f} 天）",
            f"对话   {s['turns']} 轮",
            f"精力   {s['energy']:.2f}    心情 {s['mood']:+.2f}    "
            f"激动 {s['arousal']:.2f}    安静了 {s['silent_h']:.1f}h",
            f"关系   {rel}",
            f"七情   {s.get('seven_text') or '—'}",
            f"六欲   {s.get('desire_text') or '—'}",
            f"成长   {'冻结' if s['growth'] == 'frozen' else '已开启'}",
            "—— 她现在的感觉 ——",
            s["text"] or "（没什么特别的感觉）",
        ]
        st = self.mem.stats()
        lines.append(f"记忆   {st['episodes']} 条经历，其中待沉淀 {st['pending']} 条")
        try:
            amb = self.mem.ambient()
            if amb.get("present"):
                lines.append(f"残响   {amb.get('text') or '—'}"
                             f"（{amb.get('sources')} 处 · 强度 {amb.get('intensity'):.2f}"
                             f" · 说不清 {amb.get('unknown')} 处）")
                for im in (amb.get("imprints") or [])[:2]:
                    lines.append(f"       留下的是：{im['text']}（{im['strength']:.2f}）")
            ex = _emotion.plan(s, {}, amb)
            lines.append(f"表达   {ex['text']}")
        except Exception:
            pass
        try:
            si = self.dispatch.stats()
            lines.append(f"识海   记忆点 {st['episodes']} 条 · 知识 {si['knowledge']} 条 · "
                         f"待审 {si['pending']} 条 · 好奇 {si['curiosities']} 件")
            last = si.get("last") or {}
            if last.get("chain"):
                lines.append(f"调度   " + " → ".join(last["chain"]))
        except Exception:
            pass
        try:
            au = self.autonomy.status()
            top = au.get("intent")
            if top:
                lines.append(f"自习   她自己想弄明白：{top.get('question')}"
                             f"（冲动 {float(top.get('intensity') or 0):.2f}）")
            last = au.get("last")
            if last:
                when = time.strftime("%m-%d %H:%M",
                                     time.localtime(float(last.get("ts") or 0)))
                lines.append(f"       上次自习 {when} · {last.get('status')} · "
                             f"加了 {int(last.get('added') or 0)} 条待审")
        except Exception:
            pass
        sl = _sleep.status()
        if sl:
            lines.append(f"休眠   上次 {str(sl.get('iso') or '')[:16]} 封存，"
                         f"已 {sl['slept_h']} 小时")
        try:
            fl = self.flow.status()
            if fl.get("open"):
                lines.append(f"心流   接着上一轮（强度 {fl['strength']:.2f}，"
                             f"{fl['steps']} 步，挂着「{fl['pending']}」）")
            elif fl.get("last_closed"):
                lines.append(f"心流   没有在想的（上次：{fl['last_closed'].get('reason')}）")
        except Exception:
            pass
        return "\n".join(lines)

    # ---- 给识海页的"她此刻在想什么" ---------------------------------------
    def thought_view(self) -> dict:
        """把上一轮的思维链和此刻的挂念摊开给用户看。

        这不是模型的隐藏思维；是 `think()` 已经算出来的感知、唤醒、竞争、
        策略、心流和残响。没开口时给一份只读的静止状态，不硬跑一轮 think，
        免得为了"看一眼"消耗记忆或叫醒她。
        """
        th = self._last_think or {}
        if th:
            summary = th.get("summary") or {}
            gate = th.get("gate") or {}
            return {
                "ok": True, "idle": False,
                "line": summary.get("line") or "她刚刚想过一轮，但没有留下摘要。",
                "trace": list(th.get("trace") or []),
                "awareness": str(th.get("awareness_text") or ""),
                "summary": summary,
                "strategy": summary.get("strategy_name") or "",
                "score": summary.get("score", gate.get("score")),
                "gate": gate,
                "feel_text": str(th.get("feel_text") or ""),
                "appraisal": th.get("appraisal_three"),
                "pre_answer": th.get("pre_answer"),
                "at": self._last_think_at,
            }
        # 没有上一轮：用当前状态 + 心流 + 残响 + 自主学习拼一句真的"此刻"。
        try:
            snap = self.mind.snapshot()
        except Exception:
            snap = {}
        try:
            fl = self.flow.status()
        except Exception:
            fl = {}
        try:
            amb = self.mem.ambient()
        except Exception:
            amb = {}
        try:
            au = self.autonomy.status()
        except Exception:
            au = {}
        bits: list[str] = []
        if fl.get("open"):
            pending = str(fl.get("pending") or "").strip()
            if pending:
                bits.append(f"她还在想上一轮没说完的「{pending}」")
            else:
                bits.append("她心里还有一个没说完的念头，但还没落到一句话")
        intent = (au or {}).get("intent") or {}
        if intent.get("question"):
            bits.append(f"她在琢磨「{str(intent['question'])[:28]}」")
        if amb.get("present") and amb.get("text"):
            bits.append(str(amb["text"]).strip().rstrip("。；;"))
        if not bits:
            bits.append("她没有在追什么，只是在这里，听着周围的声音")
        line = "；".join(bits).strip("。；;，, ") + "。"
        fp = str(fl.get("pending") or "").strip()
        trace = [
            f"状态    精力 {float(snap.get('energy') or 0):.2f} · "
            f"心情 {float(snap.get('mood') or 0):+.2f} · "
            f"安静 {float(snap.get('silent_h') or 0):.1f}h",
            "心流    " + (f"开着 · {fp}" if fl.get("open") and fp
                           else ("开着" if fl.get("open") else "没有在接的念头")),
        ]
        if amb.get("present"):
            trace.append(f"残响    {amb.get('text') or '有一层说不清的感觉还在'}"
                         f"（{amb.get('sources', 0)} 处 · 强度 {float(amb.get('intensity') or 0):.2f}）")
        return {"ok": True, "idle": True, "line": line, "trace": trace,
                "awareness": "", "summary": {}, "strategy": "",
                "score": None, "gate": {}, "feel_text": "",
                "at": time.time()}

    def panel_payload(self) -> dict:
        """识海页右侧四块面板的只读数据。"""
        try:
            amb = self.mem.ambient()
        except Exception:
            amb = {}
        try:
            dispatch = self.dispatch.stats()
        except Exception:
            dispatch = {}
        try:
            learn = self.dispatch.learning.stats()
        except Exception:
            learn = {}
        try:
            pending = self.dispatch.learning.pending(limit=8)
        except Exception:
            pending = []
        try:
            flow = self.flow.status()
        except Exception:
            flow = {}
        return {
            "thought": self.thought_view(),
            "dispatch": dispatch,
            "learning": learn,
            "pending": pending,
            "echo": amb,
            "flow": flow,
            "memory": self.mem.stats(),
        }

    def close(self) -> None:
        if self._response_gate is not None:
            try:
                self._response_gate.shutdown()
            except Exception:
                pass
        if self._p3 is not None:
            try:
                self._p3.close()
            except Exception:
                pass
        try:
            self.dispatch.close()
        finally:
            try:
                self.mem.close()
            finally:
                self.mind.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="app.chat", description="文字对话入口")
    ap.add_argument("--say", type=str, default=None, help="只说一句就退出")
    ap.add_argument("--think", type=str, default=None, help="只看她这一轮怎么想，不调模型")
    ap.add_argument("--prompt", type=str, default=None, help="只打印系统提示，不调模型")
    ap.add_argument("--trace", action="store_true", help="连同 --say 一起打印思维链")
    ap.add_argument("--state", action="store_true", help="只打印状态")
    ap.add_argument("--no-llm", action="store_true", help="只看提示词")
    ap.add_argument("--response-gate", choices=("off", "shadow", "enforced"),
                    default=None, help="自然回应闸门")
    ap.add_argument("--social-silence", choices=("off", "on"), default=None,
                    help="社交性沉默")
    a = ap.parse_args(argv)
    if a.response_gate:
        C.RESPONSE_GATE_MODE = a.response_gate
    if a.social_silence:
        C.SOCIAL_SILENCE = a.social_silence == "on"

    c = Chat()
    try:
        if a.state:
            print(c.state_block())
            return 0
        if a.think:
            prompt, sal, th = c.compose(a.think)
            print("—— 思维 ——")
            print("\n".join(th["trace"]))
            si = th.get("dispatch") or {}
            if si:
                print("\n—— 记忆调度 ——")
                print(" → ".join(si.get("chain") or []))
                print(f"候选 {si.get('candidates', 0)} 个 · θ={si.get('theta')} · "
                      f"待审 {si.get('pending', 0)} 条没资格进来")
                for x in si.get("aware") or []:
                    print(f"  照见 {x['A']:.3f}  [{x['kind']}] {x['label']}")
                for x in (si.get("subliminal") or [])[:3]:
                    print(f"  存在 {x['A']:.3f}  [{x['kind']}] {x['label']}")
                if si.get("awareness_extra"):
                    print("\n—— 会写进提示的 ——\n" + si["awareness_extra"])
            print("\n—— 提示词 ——")
            print(prompt)
            return 0
        if a.prompt:
            prompt, sal, th = c.compose(a.prompt)
            print(f"—— 注意力门：{sal['mode']}  score={sal['score']}  "
                  f"情绪={sal['emotion']} ——\n")
            print(prompt)
            return 0
        if a.say:
            r = c.turn(a.say)
            if not r["ok"]:
                print(f"[失败] {r['error']}", file=sys.stderr)
                return 2
            if a.trace:
                print("—— 思维 ——")
                print("\n".join(r.get("trace") or []))
                print("\n—— 回答 ——")
            if r.get("silent"):
                print(f"（她这次没接：{r.get('reason') or r.get('mode') or 'silent'}）")
            else:
                print(r["reply"])
            return 0

        ok, why = c.available()
        if not ok:
            print(f"（大模型没起来：{why}）\n"
                  f"先看看她会怎么想：python -m app.chat --prompt \"在吗\"\n", file=sys.stderr)
        print("跟她说话，空行退出。输入 /state 看状态。", file=sys.stderr)
        while True:
            try:
                line = input("你 > ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not line:
                break
            if line == "/state":
                print(c.state_block())
                continue
            r = c.turn(line)
            if not r["ok"]:
                print(f"[失败] {r['error']}", file=sys.stderr)
                continue
            if r.get("silent"):
                print(f"她 > （没接：{r.get('reason') or r.get('mode') or 'silent'}）")
            else:
                print(f"她 > {r['reply']}")
        return 0
    finally:
        c.close()


if __name__ == "__main__":
    raise SystemExit(main())
