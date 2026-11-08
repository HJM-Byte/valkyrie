"""A4.2/A4.3：身体语音接进真正的 Chat。

链路：

    上行 Opus -> STT -> Chat.turn() -> TTS -> Opus -> 下行

两种模式（A4.3 起）：
- live=False（默认，A4.2 隔离模式）：独立的临时 memory.db / mind / flow /
  learn.db，不写真实记忆、不消费真实 sleep.json；只有 PERSONA.md、whisper
  模型、TTS 声线是只读复用。
- live=True（A4.3）：真实 `Chat()`，接真实记忆、心流、人格与休眠记录。
  身体只是她的一个入口；`settle()` / 夜间窗口交给桌面端或手机端，
  不让第一句话同步等后台任务。

真正的协议/端点检测在 `app.xiaozhi_server`，这里只负责“声音 -> 她的回答 -> 声音”。
"""
from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any, AsyncIterator, Iterator, Optional

import numpy as np

from . import body_audio
from . import config as C
from . import dispatch as _dispatch_mod
from . import learning as _learning_mod
from .chat import Chat
from .dispatch import Dispatch
from .flow import Flow
from .learning import Learning
from .memory import Memory
from .mind import Mind
from .stt import STT
from .tts import TTS

log = logging.getLogger("valkyrie.body.brain")

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RUNTIME = ROOT / "data" / "body_chat"


class BodyBrainError(RuntimeError):
    """身体大脑的准备/回答失败。"""


class _SilentSchool:
    """A4.2 不接学校库；读真实 school.db 会带来无关副作用。"""

    def voice_note_text(self, limit: int = 3) -> str:
        return ""

    def close(self) -> None:
        pass


class _SilentAutonomy:
    """A4.2 不跑自主学习；避免后台任务在临时库/真实网络上乱写。"""

    def brief(self) -> None:
        return None

    def maybe_study(self) -> None:
        return None

    def status(self) -> dict[str, Any]:
        return {}


class _NoWriteMemory:
    """只读记忆代理：读全放行，写一律变 no-op。

    用于「板子可以连、但不许写记忆」的验收期。临时库本身也只是空壳；
    真实 memory.db 在构建前就不会被选中。
    """

    _BLOCK = {
        "add_turn", "add_episode", "add_fact", "note_correction",
        "link_supersede", "settle",
    }

    def __init__(self, inner: Memory) -> None:
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_blocked", {})

    def __getattr__(self, name: str):
        inner = object.__getattribute__(self, "_inner")
        attr = getattr(inner, name)
        if name not in self._BLOCK:
            return attr

        def _noop(*args, **kwargs):
            if name == "add_episode":
                return 0
            if name in ("note_correction", "settle"):
                return {}
            return None

        return _noop


class _NoWriteMind:
    """只读心智代理：tick/observe 不推进，只保留当前快照。"""

    _BLOCK = {"tick", "observe", "rest", "_write"}

    def __init__(self, inner: Mind) -> None:
        object.__setattr__(self, "_inner", inner)

    def __getattr__(self, name: str):
        inner = object.__getattribute__(self, "_inner")
        attr = getattr(inner, name)
        if name not in self._BLOCK:
            return attr
        return lambda *args, **kwargs: {}


class _NoWriteFlow:
    """只读心流代理：context 可读，advance 不落盘。"""

    _BLOCK = {"advance", "_save"}

    def __init__(self, inner: Flow) -> None:
        object.__setattr__(self, "_inner", inner)

    def __getattr__(self, name: str):
        inner = object.__getattribute__(self, "_inner")
        attr = getattr(inner, name)
        if name not in self._BLOCK:
            return attr
        return lambda *args, **kwargs: {}


def clean_for_speech(text: str, limit: int = 220) -> str:
    """把模型回复收拾成适合朗读的一句话，顺手去掉 markdown/表情。"""
    t = str(text or "")
    t = re.sub(r"```.*?```", "", t, flags=re.S)
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", t)
    t = re.sub(r"^\s*[#>*\-•]+\s*", "", t, flags=re.M)
    t = re.sub(r"[*_~>#]+", "", t)
    t = re.sub(r"[\U0001F000-\U0001FAFF\u2600-\u27BF]", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    t = t.strip("「」“”\"'‘’ \t\r\n")
    if not t:
        return ""
    if len(t) > limit:
        cut = t[:limit]
        cut_at = max(cut.rfind(c) for c in "。！？!?；;")
        t = cut[:cut_at + 1] if cut_at >= 40 else cut
    return t.strip()


class BodyBrain:
    """一个进程内复用的身体大脑：STT/TTS/Chat 各一份。"""

    def __init__(self, work_dir: Optional[Path | str] = None,
                 *, warm_tts: bool = True, live: bool = False,
                 no_write: bool = False) -> None:
        if work_dir is None:
            stamp = time.strftime("%Y%m%d_%H%M%S")
            work_dir = DEFAULT_RUNTIME / stamp
        self.root = Path(work_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self.warm_tts = bool(warm_tts)
        self.live = bool(live)
        self.no_write = bool(no_write)
        self._prepare_lock = threading.Lock()
        self._stt_lock = threading.Lock()
        self._tts_lock = threading.Lock()
        self._chat_lock = threading.Lock()
        self._stt: Optional[STT] = None
        self._tts: Optional[TTS] = None
        self._chat: Optional[Chat] = None
        self._ready = False
        self.last_turn: dict[str, Any] = {}

    @property
    def ready(self) -> bool:
        return self._ready

    @property
    def mode(self) -> str:
        if self.no_write:
            return "readonly"
        return "live" if self.live else "temp"

    @property
    def run_id(self) -> str:
        return self.root.name

    # ---- 准备：只加载一次 -------------------------------------------------
    def prepare(self) -> None:
        with self._prepare_lock:
            if self._ready:
                return
            t0 = time.time()
            self.root.mkdir(parents=True, exist_ok=True)
            log.info("身体大脑准备中：%s", self.root)
            self._stt = STT()
            # 身体只走用户指定的 Breeze 寒蝉余音；Breeze 不可用时由 TTS
            # 报错（不静默降级到 edge/本地通用声）。
            self._tts = TTS(cache_dir=self.root / "tts",
                            breeze_voice=C.BREEZE_VOICE_ID,
                            strict_breeze=True)
            if self.warm_tts:
                try:
                    self._tts.warm()
                    # 真发一次极短合成，把 Breeze 的模型连接也预热；
                    # 否则用户第一句话可能替服务端吃一次 5–10 秒冷启动。
                    self._tts.synth("嗯。")
                except Exception:
                    pass
            self._chat = self._build_chat()
            self._ready = True
            log.info(
                "身体大脑就绪：模式=%s whisper=%.1fs 库=%s 缓存=%s",
                self.mode, self._stt.load_seconds,
                getattr(self._chat.mem, "path", "?"), self.root,
            )
            _ = t0

    def _build_chat(self) -> Chat:
        if self.live and not self.no_write:
            return self._build_live_chat()
        chat = self._build_temp_chat()
        if self.no_write:
            self._apply_no_write(chat)
        return chat

    def _apply_no_write(self, chat: Chat) -> None:
        """验收期闸门：切掉所有会改状态的入口。

        记忆、心智、心流、学习好奇、调度足迹全部只读；对话本身照常走
        LLM/TTS。临时目录只有空库结构与 TTS 缓存，不会出现记忆条目。
        """
        real_mem = chat.mem
        chat.mem = _NoWriteMemory(real_mem)          # type: ignore[assignment]
        chat.mind = _NoWriteMind(chat.mind)          # type: ignore[assignment]
        chat.flow = _NoWriteFlow(chat.flow)          # type: ignore[assignment]
        if getattr(chat.dispatch, "mem", None) is real_mem:
            chat.dispatch.mem = chat.mem             # type: ignore[assignment]
        chat.dispatch._footprint = lambda *a, **k: None   # type: ignore[assignment]
        chat.dispatch.note_curiosity = lambda *a, **k: None  # type: ignore[assignment]
        log.info("只读闸门已开：记忆/心智/心流/学习写入全部屏蔽")

    def _tune_body_llm(self, chat: Chat) -> None:
        """身体通道单独关思考：不改变手机/桌面的 Chat 行为。"""
        try:
            llm = chat.llm
            llm.enable_thinking = False
            llm.max_tokens = min(int(getattr(llm, "max_tokens", 300)), 180)
            log.info("身体 LLM：thinking=off max_tokens=%s", llm.max_tokens)
        except Exception:
            log.exception("身体 LLM 关闭 thinking 失败，沿用默认")

    def _build_live_chat(self) -> Chat:
        """真实 Chat：接真实 memory.db / flow / learn / sleep / 人格。

        A4.3：身体只是她的一个入口，不单独跑记忆生命周期。
        `settle()` 与夜间学习窗口由桌面端/手机端负责；这里让第一句话
        不必同步等后台任务。休眠记录仍由真实 `_claim_wake` 认领。
        两个进程同时写真实库时给 SQLite 留 8 秒忙等，避免瞬时锁直接报错。
        """
        chat = Chat()
        chat._housekept = True
        self._tune_body_llm(chat)
        try:
            chat.mem._db.execute("PRAGMA busy_timeout=8000")
        except Exception:
            pass
        return chat

    def _build_temp_chat(self) -> Chat:
        """建一个完全落在临时目录里的 Chat，不碰真实数据。"""
        # Dispatch 的足迹 / Learning 的书籍与足迹都是模块级路径，必须先改。
        _dispatch_mod.FOOTPRINT = self.root / "sihai_footprint.jsonl"
        _learning_mod.ROOT = self.root / "learning"
        _learning_mod.DB_PATH = _learning_mod.ROOT / "learn.db"
        _learning_mod.BOOKS = _learning_mod.ROOT / "books"
        _learning_mod.CHANNELS_FILE = _learning_mod.ROOT / "channels.json"
        _learning_mod.FOOTPRINT = _learning_mod.ROOT / "footprint.jsonl"
        _learning_mod.BILI_QUEUE = _learning_mod.ROOT / "bili_queue.json"
        _learning_mod.BOOKS.mkdir(parents=True, exist_ok=True)

        mem = Memory(self.root / "memory.db")
        mind = Mind(self.root / "memory.db")
        flow = Flow(self.root / "flow.json")
        learning = Learning(self.root / "learning" / "learn.db")
        chat = Chat(mem=mem, mind=mind, flow=flow)
        chat.dispatch = Dispatch(mem=mem, learning=learning)
        chat._school = _SilentSchool()          # type: ignore[assignment]
        chat._autonomy = _SilentAutonomy()      # type: ignore[assignment]
        # 第一轮不做记忆生命周期/夜间窗口，避免后台任务碰真实文件。
        chat._housekept = True
        self._tune_body_llm(chat)
        # 身体语音不消费真实 sleep.json；醒来提示留到以后接真实库时再开。
        chat._claim_wake = lambda: None         # type: ignore[assignment]
        chat._awake = None
        return chat

    # ---- 三段同步接口（在线程里跑，别堵事件循环） --------------------------
    def transcribe_sync(self, pcm16: np.ndarray) -> dict[str, Any]:
        self.prepare()
        assert self._stt is not None
        with self._stt_lock:
            result = self._stt.transcribe(np.asarray(pcm16, dtype=np.int16))
        log.info(
            "STT text=%r lang=%s prob=%.2f audio=%.2fs stt=%.2fs filtered=%r",
            result.get("text"), result.get("lang"), float(result.get("lang_prob") or 0),
            float(result.get("audio_sec") or 0), float(result.get("seconds") or 0),
            result.get("filtered"),
        )
        return result

    def answer_sync(self, text: str) -> dict[str, Any]:
        self.prepare()
        assert self._chat is not None
        t0 = time.time()
        with self._chat_lock:
            result = self._chat.turn(text, channel="body")
        if not result.get("ok"):
            raise BodyBrainError(str(result.get("error") or "Chat.turn 失败"))
        raw = str(result.get("reply") or "")
        reply = clean_for_speech(raw)
        if not reply:
            reply = "我在。"
        log.info("CHAT 模式=%s user=%r reply=%r chat=%.2fs",
                 self.mode, text, reply, time.time() - t0)
        return {
            "ok": True,
            "reply": reply,
            "raw_reply": raw,
            "chat_sec": round(time.time() - t0, 2),
            "chat_turn": result,
        }

    def synth_sync(self, text: str) -> dict[str, Any]:
        self.prepare()
        assert self._tts is not None
        t0 = time.time()
        with self._tts_lock:
            path = self._tts.synth(text)
            pcm = body_audio.decode_audio_file(path)
            packets = body_audio.pcm16_to_opus(pcm)
            backend = self._tts.last_backend
        log.info("TTS 完成 chars=%d synth=%.2fs backend=%s packets=%d",
                 len(text), time.time() - t0, backend, len(packets))
        if not packets:
            raise BodyBrainError(f"TTS 没有产出 Opus：{path}")
        return {
            "ok": True,
            "text": text,
            "path": str(path),
            "backend": backend,
            "packets": packets,
            "seconds": round(len(packets) * body_audio.OPUS_FRAME_MS / 1000.0, 2),
            "synth_sec": round(time.time() - t0, 2),
        }

    def synth_stream_sync(self, text: str) -> Iterator[dict[str, Any]]:
        """逐块把 Breeze PCM 变成 Opus，第一帧就 yield 给服务端。

        之前是 `synth_sync()` 等整段 mp3 + 整文件解码 + 整段编码；现在
        TTS 一发首块 PCM 就重采样/编码并交给 WebSocket，首声不再等整句
        在云端合成完。
        """
        self.prepare()
        assert self._tts is not None
        t0 = time.time()
        with self._tts_lock:
            resampler = body_audio.Pcm16Resampler(24000)
            encoder = body_audio.OpusStreamEncoder()
            packets_sent = 0
            first_packet_sec: Optional[float] = None
            try:
                for pcm_chunk in self._tts.synth_stream_pcm(text):
                    pcm16 = resampler.resample(pcm_chunk)
                    packets = encoder.encode(pcm16)
                    if not packets:
                        continue
                    if first_packet_sec is None:
                        first_packet_sec = round(time.time() - t0, 2)
                    packets_sent += len(packets)
                    yield {
                        "packets": packets,
                        "backend": self._tts.last_backend or "breeze",
                        "first_chunk_sec": first_packet_sec,
                    }
                tail = resampler.flush()
                packets = encoder.encode(tail) + encoder.flush()
                if packets:
                    if first_packet_sec is None:
                        first_packet_sec = round(time.time() - t0, 2)
                    packets_sent += len(packets)
                    yield {
                        "packets": packets,
                        "backend": self._tts.last_backend or "breeze",
                        "first_chunk_sec": first_packet_sec,
                    }
            except Exception:
                log.exception(
                    "TTS 流式合成失败 chars=%d sent=%d", len(text), packets_sent)
                raise
            if packets_sent <= 0:
                raise BodyBrainError("TTS 流式合成没有产出 Opus")
            log.info(
                "TTS 流式完成 chars=%d first=%.2fs total=%.2fs packets=%d",
                len(text), float(first_packet_sec or 0),
                time.time() - t0, packets_sent)

    async def synth_stream_async(
            self, text: str) -> AsyncIterator[dict[str, Any]]:
        """把同步流式生成器桥接成异步迭代器。"""
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

        def put(ev: dict[str, Any]) -> None:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, ev)
            except RuntimeError:
                pass

        def run() -> None:
            try:
                for chunk in self.synth_stream_sync(text):
                    put(chunk)
            except Exception as exc:
                put({"error": f"{type(exc).__name__}: {exc}"})
            finally:
                put({"eof": True})

        task = asyncio.create_task(
            asyncio.to_thread(run), name="body-tts-stream")
        try:
            while True:
                ev = await queue.get()
                if ev.get("eof"):
                    return
                if ev.get("error"):
                    raise BodyBrainError(str(ev["error"]))
                yield ev
        finally:
            if task.done():
                try:
                    task.exception()
                except Exception:
                    pass

    # ---- 流式回答：先出第一句，边生成边合成 ------------------------------
    def answer_stream_sync(self, text: str):
        """逐段吐出 Chat 的流式回答；done 事件带清洗后的完整回复。

        和 `answer_sync` 共用 `_chat_lock`，保证同一进程里不会有两轮
        同时写同一份 Chat/记忆。
        """
        self.prepare()
        assert self._chat is not None
        t0 = time.time()
        first_delta = False
        with self._chat_lock:
            try:
                for ev in self._chat.turn_stream(text, channel="body"):
                    typ = str(ev.get("type") or "")
                    if typ == "done":
                        result = dict(ev.get("result") or {})
                        raw = str(result.get("reply") or "")
                        clean = clean_for_speech(raw)
                        if not clean:
                            clean = "我在。"
                        result["reply"] = clean
                        result["raw_reply"] = raw
                        result["chat_sec"] = round(time.time() - t0, 2)
                        yield {"type": "done", "result": result}
                    else:
                        if typ == "delta" and not first_delta:
                            first_delta = True
                            log.info("流式首字 %.2fs", time.time() - t0)
                        yield ev
            except Exception as exc:
                log.exception("流式 Chat 失败")
                yield {"type": "error",
                       "error": f"{type(exc).__name__}: {exc}"}

    async def answer_stream_async(self, text: str) -> AsyncIterator[dict[str, Any]]:
        """把线程里的同步生成器桥接成异步迭代器，供服务端边收边合成。"""
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

        def put(ev: dict[str, Any]) -> None:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, ev)
            except RuntimeError:
                pass

        def run() -> None:
            try:
                for ev in self.answer_stream_sync(text):
                    put(ev)
            except Exception as exc:
                put({"type": "error",
                     "error": f"{type(exc).__name__}: {exc}"})
            finally:
                put({"type": "eof"})

        task = asyncio.create_task(asyncio.to_thread(run),
                                   name="body-chat-stream")
        try:
            while True:
                ev = await queue.get()
                if ev.get("type") == "eof":
                    break
                yield ev
        finally:
            # 消费者提前退出时不强杀线程（Chat 内部还握着记忆/锁），
            # 让生成器自然跑完；服务端正常路径都会消费到 eof。
            if task.done():
                try:
                    task.exception()
                except Exception:
                    pass

    # ---- 异步包装 ---------------------------------------------------------
    async def warm_async(self) -> None:
        await asyncio.to_thread(self.prepare)

    def warm_tts_sync(self) -> bool:
        """保持 Breeze 的 TLS 连接热着，第一句话少付一次握手。"""
        self.prepare()
        if self._tts is None:
            return False
        # 和真正的合成共用一把锁，避免保活线程把正在用的连接换掉。
        with self._tts_lock:
            return self._tts.warm()

    async def warm_tts_async(self) -> bool:
        return await asyncio.to_thread(self.warm_tts_sync)

    def tts_status(self) -> dict[str, object]:
        # 只读状态，不能为了报健康去等合成锁。
        if self._tts is None:
            return {"ready": False}
        return self._tts.breeze_status()

    async def transcribe_async(self, pcm16: np.ndarray) -> dict[str, Any]:
        return await asyncio.to_thread(self.transcribe_sync, pcm16)

    async def answer_async(self, text: str) -> dict[str, Any]:
        return await asyncio.to_thread(self.answer_sync, text)

    async def synth_async(self, text: str) -> dict[str, Any]:
        return await asyncio.to_thread(self.synth_sync, text)

    def close(self) -> None:
        if self._chat is not None:
            try:
                self._chat.close()
            except Exception:
                pass
            self._chat = None
        if self._tts is not None:
            try:
                self._tts.close()
            except Exception:
                pass
            self._tts = None
        self._ready = False

    async def close_async(self) -> None:
        await asyncio.to_thread(self.close)

    def health(self) -> dict[str, Any]:
        info: dict[str, Any] = {
            "ready": self._ready,
            "mode": self.mode,
            "write_blocked": self.no_write,
            "run_id": self.run_id,
            "dir": str(self.root),
            "last_turn": dict(self.last_turn),
        }
        if self._chat is not None:
            info["memory_path"] = str(getattr(self._chat.mem, "path", ""))
            try:
                info["memory"] = self._chat.mem.stats()
            except Exception:
                pass
        return info
