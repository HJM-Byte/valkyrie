"""主流水线：把视觉、语音、对话、下位机串成一条命。

六个状态怎么走：
    IDLE --(听到人声 / 按键)--> LISTENING --(静音收尾)--> THINKING
    THINKING --(拿到回复)--> SPEAKING --(播完)--> IDLE
任何一步出错 -> ERROR，然后自动回 IDLE。

没有摄像头、没有串口、没有 ollama 时全部降级，不阻塞语音主链路。
"""
from __future__ import annotations

import threading
import time
from typing import Optional

from . import config as C
from .audio import Endpointer, Mic, record_utterance
from .llm import LLM, LLMError
from .response_gate import ResponseGate
from .state import LED_HINT, Machine, State
from .think import think


class Pipeline:
    def __init__(self, use_camera: bool = True, use_link: bool = True,
                 use_llm: bool = True, use_tts: bool = True) -> None:
        self.machine = Machine(State.BOOTING)
        self.mic = Mic()
        self.mic_ok = False
        self.ep = Endpointer()
        self.llm = LLM() if use_llm else None
        self.tts = None
        self.stt = None
        self.vision = None
        self.camera_ok = False
        self.link = None
        self.last_faces: list[dict] = []
        self.last_objects: list[dict] = []
        self._alive = False
        self._thr_vision: Optional[threading.Thread] = None
        self._use_camera = use_camera
        self._use_link = use_link
        self._use_tts = use_tts
        self.log: list[str] = []
        self.response_gate = ResponseGate(state_path=C.RESPONSE_GATE_STATE)
        self._last_reply = ""

        # 她三件套：记忆、状态、人设。缺一件就不接，语音照跑，只是没有她。
        self.mem = self.mind = self.persona = None
        self.flow = None
        self.learning = None
        self.autonomy = None
        try:
            from .memory import Memory
            from .mind import Mind
            from .persona import Persona
            from .flow import Flow
            from .learning import Learning
            from .autonomy import Autonomy
            self.mem, self.mind, self.persona = Memory(), Mind(), Persona()
            self.flow = Flow()
            self.learning = Learning()
            self.autonomy = Autonomy(learning=self.learning, mem=self.mem,
                                     mind=self.mind)
        except Exception as e:
            print(f"[记忆] 初始化失败: {type(e).__name__}: {e}", flush=True)

    # ---- 启动 -------------------------------------------------------------
    def _say(self, msg: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        self.log.append(line)
        print(line, flush=True)

    def start(self) -> bool:
        self.machine.to(State.BOOTING, "boot")

        self.mic_ok = bool(self.mic.open())
        if not self.mic_ok:
            self._say(f"麦克风打不开: {self.mic.last_error or '未知错误'}")
            self._say("先跑 run.cmd --mic 看设备；权限页可跑 run.cmd --mic-settings")
        else:
            st = self.mic.status()
            self._say(f"麦克风就绪: {self.mic.device_label()} "
                      f"（采集 {st['capture_rate']}Hz -> 16k）")

        if self._use_camera:
            try:
                from .vision import Vision
                self.vision = Vision()
                self.camera_ok = self.vision.camera.open()
                self._say(f"摄像头 {'就绪' if self.camera_ok else '未连接（视觉关闭）'}")
            except Exception as e:
                self._say(f"视觉初始化失败: {type(e).__name__}: {e}")
                self.vision = None

        if self._use_link:
            from .link import MockLink, SerialLink, guess_esp32_port
            port = guess_esp32_port()
            self.link = SerialLink(port) if port else MockLink()
            if not self.link.start():
                self.link = MockLink()
            self.link.on_button = self._on_button
            self._say(f"下位机链路: {type(self.link).__name__}")
            self.machine.on_change(lambda o, n, r: self.link.set_state(n))

        if self._use_tts:
            try:
                from .tts import TTS
                self.tts = TTS()
                self._say("TTS 就绪")
            except Exception as e:
                self._say(f"TTS 初始化失败: {e}")

        try:
            from .stt import STT
            t0 = time.time()
            self.stt = STT()
            self._say(f"STT 就绪（加载 {time.time()-t0:.2f}s）")
        except Exception as e:
            self._say(f"STT 加载失败: {type(e).__name__}: {e}")

        if self.llm is not None:
            ok, why = self.llm.available()
            self._say(f"大模型 {self.llm.model}: {why}")

        if self.mem is not None and self.mind is not None:
            st, sn = self.mem.stats(), self.mind.snapshot()
            self._say(f"记忆库: 存在 {sn['hours']:.1f}h，说过 {sn['turns']} 轮，"
                      f"{st['episodes']} 条经历（待沉淀 {st['pending']}），"
                      f"成长{'冻结' if sn['growth'] == 'frozen' else '已开'}")

        if self.vision is not None and self.camera_ok:
            self._alive = True
            self._thr_vision = threading.Thread(target=self._vision_loop, daemon=True)
            self._thr_vision.start()

        if self.mic_ok:
            self.machine.to(State.IDLE, "ready")
            self._say(f"就绪，状态={self.machine.state.value} 灯={LED_HINT[self.machine.state]}")
        else:
            self.machine.to(State.ERROR, "no mic")
            self._say("语音输入不可用；文字入口 chat.cmd 仍可用")
        return self.mic_ok

    def _on_button(self, key: int) -> None:
        self._say(f"按键 {key}")

    # ---- 视觉线程 ---------------------------------------------------------
    def _vision_loop(self) -> None:
        while self._alive:
            try:
                frame = self.vision.grab()
                if frame is not None:
                    scene = self.vision.analyze_scene(frame)
                    self.last_faces = scene.get("faces", [])
                    self.last_objects = scene.get("objects", [])
            except Exception:
                pass
            time.sleep(0.5)

    # ---- 一轮对话 ---------------------------------------------------------
    def _scene_hint(self) -> str:
        parts = []
        if self.last_faces:
            names = [f.get("name") for f in self.last_faces if f.get("name")]
            n = len(self.last_faces)
            if names:
                parts.append(f"画面里有 {n} 张脸，认出：{'、'.join(names)}")
            else:
                parts.append(f"画面里有 {n} 张脸，都不认识")
        if self.last_objects:
            from .objects import object_summary
            got = object_summary(self.last_objects, max_items=3)
            if got:
                parts.append(f"看到 {got}")
        return f"（{'；'.join(parts)}）" if parts else ""

    def one_turn(self) -> bool:
        if not self.mic_ok:
            self._say("麦克风未就绪，跳过这一轮")
            time.sleep(0.5)
            return False
        self.machine.to(State.LISTENING, "listen")
        pcm = record_utterance(self.mic, self.ep)
        if pcm is None:
            self.machine.to(State.IDLE, "silence")
            return False

        self.machine.to(State.THINKING, "stt")
        text = ""
        if self.stt is not None:
            try:
                r = self.stt.transcribe(pcm)
                text = r["text"]
                self._say(f"你说: {text}  ({r['seconds']:.2f}s)")
            except Exception as e:
                self._say(f"STT 失败: {type(e).__name__}: {e}")
                self.machine.to(State.ERROR, "stt")
                return False
        else:
            self._say("STT 未加载，跳过识别")
            self.machine.to(State.IDLE, "no stt")
            return False

        if not text.strip():
            self._say("没听清")
            self.machine.to(State.IDLE, "empty")
            return False

        # 自然回应闸门：语音入口也先判客观沉默；默认 off 时完全不动原路径。
        gate = self.response_gate
        gd = gate.decide(text, channel="voice",
                         last_assistant=self._last_reply, batch_complete=True)
        if gate.mode == "enforced" and gd.mode in ("silent", "defer"):
            gate.log(gd, llm_called=False, assistant_message_created=False)
            self._say(f"这句没接（{gd.reason}）")
            if self.mem is not None:
                try:
                    self.mem.add_turn("user", text)
                except Exception:
                    pass
            self.machine.to(State.IDLE, "gate")
            return False
        response_mode = gd.mode if gate.mode == "enforced" else "respond"

        # 先过注意力门。不是每句话都值得她抬头——见人就答是机器最像机器的地方。
        th = None
        feedback = None
        sal = {"mode": "respond", "score": 0.5, "emotion": ""}
        if self.mind is not None:
            try:
                self.mind.tick()
                if self.mem is not None:
                    try:
                        feedback = self.mem.resolve_pre_answer_feedback(text)
                    except Exception:
                        feedback = None
                th = think(text, mind=self.mind, mem=self.mem, channel="voice",
                           feedback=feedback, reinforce=True)
                sal = th["gate"]
            except Exception as e:
                self._say(f"状态更新失败: {type(e).__name__}: {e}")
            if sal["mode"] == "quiet":
                self._say(f"这句没接（注意力 {sal['score']:.2f}）")
                if self.mem is not None:
                    self.mem.add_turn("user", text)
                self.machine.to(State.IDLE, "quiet")
                return False

        prompt = None
        # 心流：上一轮没说完的念头能不能接上；接上就只往前接一小步。
        if th is not None and self.flow is not None:
            try:
                fl = self.flow.context(text, (th.get("memory") or {}).get("hits"),
                                       event_a=float(sal.get("score") or 0.0))
                th["flow"] = fl
                if fl.get("continue"):
                    th["awareness_text"] = ((th.get("awareness_text") or "").rstrip()
                                            + "\n- " + str(fl.get("block") or "")).strip()
                    if fl.get("rule"):
                        th["decision"]["rule"] = ((th["decision"].get("rule") or "")
                                                  + " " + str(fl["rule"])).strip()
                    self._say(f"心流 接上一轮（强度 {float(fl.get('strength') or 0):.2f}）")
                elif fl.get("interrupt"):
                    self._say("心流 被打断，先接眼前这句")
            except Exception as e:
                self._say(f"心流判断失败: {type(e).__name__}: {e}")

        if self.persona is not None:
            try:
                from .mind import GROWTH_LOCKED
                from . import emotion as _emotion
                # 情感输出：语气与语速也来自这一轮的状态，不只是文字内容
                plan = _emotion.plan(self.mind.snapshot() if self.mind else {},
                                     th, th.get("ambient"),
                                     (th.get("memory") or {}).get("hits"))
                th["emotion_plan"] = plan
                learn = None
                if self.autonomy is not None:
                    try:
                        learn = self.autonomy.brief()
                        th["learning"] = learn
                    except Exception:
                        learn = None
                if GROWTH_LOCKED:
                    prompt = self.persona.compose_birth(
                        text, mind=self.mind, salience=sal, thought=th,
                        expression=plan, learning=learn, channel="voice")
                else:
                    prompt = self.persona.compose(
                        text, mem=self.mem, mind=self.mind, salience=sal,
                        thought=th,
                        faces=self.last_faces, camera_ok=self.camera_ok,
                        expression=plan, learning=learn,
                    )
            except Exception as e:
                self._say(f"提示词组装失败: {type(e).__name__}: {e}")

        if prompt and response_mode == "brief":
            prompt += ("\n\n【这一轮只要承接】\n只回一句，最多二十五个字。"
                       "不展开新话题，不追问，不总结。")
        elif prompt and response_mode == "pause_stated":
            prompt += ("\n\n【先停一下】\n只说明一次为什么现在不继续，"
                       "并给出一个可理解的恢复条件。不指责、不追问。")

        reply = ""
        if self.llm is not None:
            try:
                r = self.llm.chat(text + self._scene_hint(), system=prompt)
                reply = r["text"]
                self._last_reply = str(reply or "").strip()
                self._say(f"回答: {reply}  ({r['seconds']:.2f}s)")
            except LLMError as e:
                self._say(f"LLM 失败: {e}")
                self.machine.to(State.ERROR, "llm")
                return False

        if reply and self.tts is not None:
            self.machine.to(State.SPEAKING, "tts")
            try:
                from . import emotion as _emotion
                v = _emotion.voice_of(th.get("emotion_plan") if th else None)
                r = self.tts.speak(reply, volume=v["volume"], rate=v["rate"])
                self._say(f"播报 {r['total_sec']:.2f}s（{v['rate']} / 音量 {v['volume']:.2f}）")
            except Exception as e:
                self._say(f"TTS 失败: {type(e).__name__}: {e}")

        if gate.mode != "off":
            gate.log(gd, llm_called=True,
                     assistant_message_created=bool(reply))
        self._flow_advance(text, th)
        self._remember(text, reply, sal, th)
        self._maybe_self_study()
        self.machine.to(State.IDLE, "done")
        return True

    def _maybe_self_study(self) -> None:
        """语音这条路也接自主学习：她情绪/记忆变了，才由她自己决定查不查。

        和文字入口一样，不阻塞；材料只进待审区。
        """
        if self.autonomy is None:
            return
        try:
            r = self.autonomy.maybe_study()
            if r.get("started"):
                q = (r.get("intent") or {}).get("question") or ""
                self._say(f"自习开跑：{q}（材料只进待审区）")
        except Exception as e:
            self._say(f"自主学习失败（不影响对话）: {type(e).__name__}: {e}")

    def _flow_advance(self, text: str, th: Optional[dict]) -> None:
        """一轮说完，把心流往前推：答上了算进展，答不上把念头挂到下一轮。"""
        if self.flow is None or th is None:
            return
        try:
            key = str((th.get("decision") or {}).get("key") or "")
            prog = {"answer": 0.50, "attend": 0.45, "comfort": 0.40,
                    "act": 0.45, "confess": 0.15, "deflect": 0.10}.get(key, 0.0)
            pend = ("他问的那件事我还没答上来：" + text.strip()[:24]
                    if key == "confess" else "")
            fl = th.get("flow") or {}
            self.flow.advance(text, progress=prog, pending=pend,
                              interrupt=bool(fl.get("interrupt")),
                              recalled=(th.get("memory") or {}).get("hits"))
        except Exception as e:
            self._say(f"心流推进失败: {type(e).__name__}: {e}")

    def _remember(self, text: str, reply: str, sal: dict,
                  th: Optional[dict] = None) -> None:
        """一轮说完，先把这一轮写回她的状态；记忆要等成长权限打开。
        写不进去只记日志，不能因为记忆出错把对话搞崩。"""
        if self.mind is None:
            return
        try:
            from .mind import GROWTH_LOCKED
            if GROWTH_LOCKED or self.mem is None:
                # 出生期：她只有当前意识，不落任何东西
                self.mind.observe(text, reply, sal, thought=th, channel="voice")
                return
            from .memory import absorb, emotion_of, importance_of
            turn_id = self.mem.add_turn("user", text)
            if reply:
                self.mem.add_turn("assistant", reply)
            pre = (th or {}).get("pre_answer") or {}
            if turn_id and pre.get("applied"):
                try:
                    self.mem.offer_pre_answer(
                        str(pre.get("strategy") or ""), turn_id,
                        str(pre.get("directive") or pre.get("note") or ""),
                        note=str(pre.get("note") or ""),
                    )
                except Exception:
                    pass
            emo = sal.get("emotion") or emotion_of(text)
            corrected = self.mem.note_correction(text, turn_id=turn_id)
            new_id = self.mem.add_episode(
                text, kind="talk", who="他", imp=importance_of(text, emo),
                scene="当面", emo=emo, emo_i=0.5 if emo != "平静" else 0.1,
                feel=(th or {}).get("feel"),
                rel=0.45, ident=0.30, stage="raw",
                source="他当面说的", about="他",
            )
            if corrected.get("retracted") and new_id:
                self.mem.link_supersede(corrected.get("ids") or [], new_id)
            got = absorb(self.mem, text)
            if got:
                self._say(f"记住: {'；'.join(got)}")
            self.mind.observe(text, reply, sal, thought=th, channel="voice")
        except Exception as e:
            self._say(f"记忆写入失败: {type(e).__name__}: {e}")

    # ---- 主循环 -----------------------------------------------------------
    def run(self, max_turns: Optional[int] = None) -> None:
        turns = 0
        try:
            while max_turns is None or turns < max_turns:
                if self.machine.state in (State.ERROR, State.BOOTING):
                    time.sleep(0.3)
                    self.machine.to(State.IDLE, "recover")
                if self.one_turn():
                    turns += 1
        except KeyboardInterrupt:
            self._say("用户中断")
        finally:
            self.stop()

    def stop(self) -> None:
        self._alive = False
        try:
            self.response_gate.shutdown()
        except Exception:
            pass
        self.mic.close()
        if self.vision is not None:
            try:
                self.vision.camera.close()
            except Exception:
                pass
        if self.link is not None:
            self.link.stop()
        if self.tts is not None:
            self.tts.stop()
        if self.learning is not None:
            try:
                self.learning.close()
            except Exception:
                pass
