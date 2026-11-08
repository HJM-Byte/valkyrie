"""A 轨：PC 端 xiaozhi 兼容服务端（OTA + WebSocket）。

只用于“不刷机、先让板子连到 PC”的路线。启动：

    cd robot
    python -m app.xiaozhi_server --host 0.0.0.0 --port 8768

它只做协议层：
- GET/POST /xiaozhi/ota/ 返回 websocket + server_time；
- /xiaozhi/v1/ 做 hello 握手、设备去重、收上行音频/文本、发下行文本；
- 不构造 Chat、不写 memory.db、不碰 sleep.json。

真身体必须由 desktop.py --body 启动，让服务与桌面共用同一个 Chat；
本进程只供假身体/协议调试用。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from aiohttp import WSMsgType, web

from . import body_audio, body_brain, body_capture, body_link, body_voice
from .body_link import (
    BodyRegistryFull,
    BodySession,
    DeviceRegistry,
    OTA_PATH,
    WS_PATH,
    normalize_audio_params,
    parse_device_headers,
    validate_client_hello,
)

log = logging.getLogger("valkyrie.body.xiaozhi")

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LOG = ROOT / "data" / "xiaozhi_server.log"

# 板子通常在 `tts stop` 后几十毫秒就发 `listen start mode=auto`。
# 这一小段只用来等板子把最后一个 Opus 尾音放完，不能再按整段音频时长
# 累加——旧实现把“发送耗时＋音频时长＋0.8s”叠在一起，导致下一轮
# listen start 被丢掉 10 秒以上，表现就是“显示聆听中但不再回答”。
LISTEN_TAIL_COOLDOWN_SEC = 0.25
# 推迟启动期间先缓存上行包，避免用户看到“聆听中”马上开口时丢首字。
LISTEN_PREROLL_PACKETS = 50

# 流式回答分句：先完整句，其次逗号；句号没到但已经很长时也切，
# 保证第一句能尽快进 TTS，而不是等整段回复生成完。
_SENTENCE_END = re.compile(r"[。！？!?；;\n]+")
_COMMA = re.compile(r"[，,、]")
_MAX_FIRST_CLAUSE = 60
_MIN_COMMA_CUT = 12
_MIN_CLAUSE_CHARS = 8


def split_speakable(buf: str) -> tuple[list[str], str]:
    """把流式文本切成可朗读的小句，返回 (完整句列表, 剩余尾巴)。"""
    out: list[str] = []
    while True:
        m = _SENTENCE_END.search(buf)
        if m is not None:
            cut = m.end()
            piece = buf[:cut].strip()
            rest = buf[cut:].strip()
            # “瓦尔基里。”这种两字短句单独合成会 2 秒才出声、后面还接不上，
            # 等下一句一起切；实在没有下一句才在 done 时由调用方兜底。
            if piece and len(piece) < _MIN_CLAUSE_CHARS and rest:
                m2 = _SENTENCE_END.search(buf, cut)
                if m2 is None:
                    break
                cut = m2.end()
                piece = buf[:cut].strip()
            if piece:
                out.append(piece)
            buf = buf[cut:]
            continue
        if len(buf) >= _MAX_FIRST_CLAUSE:
            m = _COMMA.search(buf)
            if m is not None and m.start() >= _MIN_COMMA_CUT:
                cut = m.end()
                piece = buf[:cut].strip()
                if piece:
                    out.append(piece)
                buf = buf[cut:]
                continue
        break
    return out, buf


@dataclass
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 8768
    token: str = "local-dev"
    public_host: str = ""
    strict_token: bool = False
    max_devices: int = 3
    hello_timeout_sec: float = 10.0
    idle_timeout_sec: float = 90.0
    inspect_audio: bool = False
    demo_reply: bool = False
    demo_audio: str = str(body_voice.DEFAULT_DEMO)
    chat: bool = False
    live_memory: bool = False
    no_write: bool = False
    brain_dir: str = ""
    capture_max_sec: float = 8.0
    record_only: bool = False
    record_dir: str = str(ROOT / "data" / "_tmp" / "denoise")
    record_seconds: float = 45.0
    debug: bool = False


class BodyServer:
    def __init__(self, cfg: ServerConfig) -> None:
        self.cfg = cfg
        self.registry = DeviceRegistry(max_sessions=cfg.max_devices)
        self.started_at = time.time()
        self.ota_requests = 0
        self.ws_connections = 0
        self.ws_rejected = 0
        self.last_ota: dict[str, Any] | None = None
        self.last_ws: dict[str, Any] | None = None
        self._session_seq = 0
        self._demo_sent: set[str] = set()
        self._demo_tasks: dict[str, asyncio.Task[Any]] = {}
        self.brain = (body_brain.BodyBrain(
                          work_dir=cfg.brain_dir or None,
                          live=cfg.live_memory,
                          no_write=cfg.no_write)
                      if cfg.chat else None)
        self._captures: dict[str, body_capture.Utterance] = {}
        self._capture_timers: dict[str, asyncio.Task[Any]] = {}
        self._pending_starts: dict[str, asyncio.Task[Any]] = {}
        self._pending_audio: dict[str, list[bytes]] = {}
        self._finishing: set[str] = set()
        self._response_tasks: dict[str, asyncio.Task[Any]] = {}
        self._listen_cooldown_until = 0.0
        self.last_turn: dict[str, Any] | None = None
        self._record_done = False
        self._tts_warm_task: Optional[asyncio.Task[Any]] = None

    # ---- 生命周期 --------------------------------------------------------
    async def on_startup(self, app: web.Application) -> None:
        if self.brain is None:
            return
        log.info("A4.2 加载身体大脑（首次约 2-5 秒）……")
        try:
            await self.brain.warm_async()
            log.info("A4.2 身体大脑就绪：%s", self.brain.root)
            # Breeze 的 TLS 连接空闲几秒就会被 Cloudflare 关掉；不保活的话
            # 每一句都要重新握手，首声会多 0.6–1.3 秒。这里在后台只建连、
            # 不请求合成，不产生云端费用。
            self._tts_warm_task = asyncio.create_task(self._tts_warm_loop())
        except Exception:
            log.exception("A4.2 身体大脑加载失败，服务继续但语音不会回答")

    async def _tts_warm_loop(self) -> None:
        while True:
            try:
                if self.brain is not None:
                    await self.brain.warm_tts_async()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.debug("Breeze 连接保活失败", exc_info=True)
            await asyncio.sleep(6.0)

    async def on_cleanup(self, app: web.Application) -> None:
        for session_id in list(self._captures):
            cap = self._captures.pop(session_id, None)
            if cap is not None:
                cap.done = True
            task = self._capture_timers.pop(session_id, None)
            if task is not None and not task.done():
                task.cancel()
        for task in list(self._pending_starts.values()):
            if not task.done():
                task.cancel()
        self._pending_starts.clear()
        self._pending_audio.clear()
        if self._tts_warm_task is not None:
            self._tts_warm_task.cancel()
            try:
                await self._tts_warm_task
            except (asyncio.CancelledError, Exception):
                pass
            self._tts_warm_task = None
        if self.brain is not None:
            await self.brain.close_async()

    # ---- URL / OTA -------------------------------------------------------
    def public_ws_url(self, request: web.Request) -> str:
        host = (self.cfg.public_host or request.host or "").strip()
        if "://" in host:
            scheme, rest = host.split("://", 1)
            host = rest.rstrip("/")
        else:
            scheme = ""
        if not host:
            host = f"127.0.0.1:{self.cfg.port}"
        elif not _has_port(host):
            host = f"{host}:{self.cfg.port}"
        base = f"ws://{host}" if scheme not in ("ws", "wss") else f"{scheme}://{host}"
        return base.rstrip("/") + WS_PATH

    def ota_response(self, request: web.Request) -> dict[str, Any]:
        return body_link.ota_payload(
            self.public_ws_url(request),
            self.cfg.token,
        )

    async def handle_ota(self, request: web.Request) -> web.Response:
        self.ota_requests += 1
        identity = parse_device_headers(request.headers)
        raw = b""
        try:
            raw = await request.read()
        except Exception:
            pass
        log.info(
            "OTA %s %s device=%s client=%s proto=%s ua=%r body=%r",
            request.method,
            request.path,
            identity["device_id"],
            identity["client_id"] or "-",
            identity["protocol_version"] or "-",
            identity["user_agent"][:120],
            raw[:300],
        )
        payload = self.ota_response(request)
        self.last_ota = {
            "at": time.time(),
            "method": request.method,
            "remote": request.remote or "",
            "device_id": identity["device_id"],
            "client_id": identity["client_id"],
            "protocol_version": identity["protocol_version"],
            "user_agent": identity["user_agent"],
            "ws_url": payload["websocket"]["url"],
            "body": raw[:300].decode("utf-8", "replace"),
        }
        log.info("OTA -> websocket=%s", payload["websocket"]["url"])
        return web.json_response(payload)

    # ---- WebSocket -------------------------------------------------------
    async def handle_ws(self, request: web.Request) -> web.StreamResponse:
        if self.cfg.strict_token:
            auth = request.headers.get("Authorization", "")
            if auth != f"Bearer {self.cfg.token}":
                log.warning("WS 拒绝：token 不匹配 %r", auth)
                return web.json_response({"error": "unauthorized"}, status=401)

        identity = parse_device_headers(request.headers)
        self.ws_connections += 1
        self._session_seq += 1
        # 心跳缩短到 15 秒：板子空闲时 WiFi 会进 LOW_POWER，30 秒一次
        # ping 隔得太久，空闲 RTT 会飙到 0.8–1 秒；15 秒能把它拉回活动态，
        # 又不至于像 5 秒那样明显吃电池。
        ws = web.WebSocketResponse(heartbeat=15, max_msg_size=2 * 1024 * 1024)
        await ws.prepare(request)

        session = BodySession(
            ws=ws,
            device_id=identity["device_id"],
            client_id=identity["client_id"],
            protocol_version=identity["protocol_version"],
            remote=request.remote or "",
            session_id=f"local-{int(time.time())}-{self._session_seq}",
        )
        try:
            replaced = await self.registry.register(session)
        except BodyRegistryFull as exc:
            self.ws_rejected += 1
            log.warning("WS 拒绝：%s", exc)
            await ws.close(code=1013, message=b"server full")
            return ws

        if replaced is not None:
            log.warning(
                "设备 %s 重连，踢掉旧会话 %s", session.device_id, replaced.session_id
            )
            await replaced.close(code=4001, reason="replaced by newer connection")
        self.last_ws = {
            "at": time.time(),
            "device_id": session.device_id,
            "client_id": session.client_id,
            "remote": session.remote,
            "session_id": session.session_id,
        }
        log.info(
            "WS 连接 device=%s client=%s remote=%s session=%s",
            session.device_id,
            session.client_id or "-",
            session.remote,
            session.session_id,
        )

        hello_done = False
        hello_deadline = time.monotonic() + self.cfg.hello_timeout_sec
        last_rx = time.monotonic()
        try:
            while not ws.closed:
                try:
                    msg = await asyncio.wait_for(ws.receive(), timeout=5.0)
                except asyncio.TimeoutError:
                    if not hello_done and time.monotonic() > hello_deadline:
                        log.warning("WS %s 未在 %.1fs 内发 hello，关闭",
                                    session.device_id, self.cfg.hello_timeout_sec)
                        await ws.close(code=4000, message=b"hello timeout")
                        break
                    if time.monotonic() - last_rx > self.cfg.idle_timeout_sec:
                        log.info("WS %s 空闲超时，关闭", session.device_id)
                        await ws.close(code=1000, message=b"idle timeout")
                        break
                    continue

                if msg.type == WSMsgType.TEXT:
                    last_rx = time.monotonic()
                    session.rx_text += 1
                    session.rx_bytes += len(msg.data)
                    session.touch()
                    try:
                        obj = json.loads(msg.data)
                    except json.JSONDecodeError:
                        log.warning("WS %s 文本不是 JSON: %r", session.device_id, msg.data[:200])
                        continue
                    if not isinstance(obj, dict):
                        log.warning("WS %s 文本 JSON 不是对象", session.device_id)
                        continue
                    hello_done = await self._handle_text(session, obj, hello_done)

                elif msg.type == WSMsgType.BINARY:
                    last_rx = time.monotonic()
                    session.rx_binary += 1
                    session.rx_bytes += len(msg.data)
                    session.touch()
                    if self.cfg.inspect_audio:
                        log.info("WS %s 上行 Opus %d 字节", session.device_id, len(msg.data))
                    if self.brain is not None or self.cfg.record_only:
                        self._feed_audio(session, msg.data)
                    # 真正的 Opus 解码由 body_capture/body_brain 在 A4.2 接入。

                elif msg.type == WSMsgType.ERROR:
                    log.warning("WS %s 错误：%s", session.device_id, ws.exception())
                    break

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            session.last_error = f"ws loop: {exc}"
            log.exception("WS %s 循环异常", session.device_id)
        finally:
            self._cancel_capture(session)
            self._abort_response(session)
            task = self._demo_tasks.pop(session.session_id, None)
            if task is not None and not task.done():
                task.cancel()
            await self.registry.unregister(session)
            stats = session.stats()
            log.info(
                "WS 断开 device=%s session=%s rx_text=%d rx_bin=%d rx_bytes=%d "
                "tx_text=%d tx_bin=%d last_error=%r",
                stats["device_id"], stats["session_id"], stats["rx_text"],
                stats["rx_binary"], stats["rx_bytes"], stats["tx_text"],
                stats["tx_binary"], stats["last_error"],
            )
            if not ws.closed:
                await ws.close()
        return ws

    async def _handle_text(self, session: BodySession, obj: dict[str, Any],
                           hello_done: bool) -> bool:
        typ = str(obj.get("type") or "")
        if typ == "hello":
            try:
                parsed = validate_client_hello(obj)
            except body_link.BodyProtocolError as exc:
                log.warning("WS %s hello 非法：%s", session.device_id, exc)
                await session.send_json({"type": "error", "message": str(exc)})
                await session.close(code=4000, reason="bad hello")
                return hello_done
            session.audio_params = parsed["audio_params"]
            session.features = parsed["features"]
            session.hello_received = True
            await session.send_hello()
            log.info(
                "hello device=%s version=%s audio=%s features=%s",
                session.device_id, parsed["version"],
                session.audio_params, session.features,
            )
            return True

        if not hello_done:
            log.warning("WS %s 在 hello 前收到 %r，忽略", session.device_id, typ)
            return hello_done

        if typ == "stt":
            log.info("STT device=%s text=%r final=%s",
                     session.device_id, str(obj.get("text", ""))[:300], obj.get("is_final"))
        elif typ == "llm":
            log.info("LLM device=%s text=%r", session.device_id, str(obj.get("text", ""))[:300])
        elif typ == "tts":
            log.info("TTS device=%s state=%s text=%r",
                     session.device_id, obj.get("state"), str(obj.get("text", ""))[:200])
        elif typ == "emotion":
            log.info("EMOTION device=%s emotion=%r", session.device_id, obj.get("emotion"))
        elif typ == "listen":
            state = str(obj.get("state") or "")
            log.info("LISTEN device=%s payload=%s",
                     session.device_id, json.dumps(obj, ensure_ascii=False)[:500])
            if self.brain is not None or self.cfg.record_only:
                if state == "start":
                    self._start_capture(session)
                elif state == "stop":
                    self._request_finish(session, "listen-stop")
                elif state == "abort":
                    self._cancel_capture(session)
            if self.cfg.demo_reply and state == "start":
                self._schedule_demo_reply(session)
        elif typ in ("abort", "mcp", "ping"):
            log.info("%s device=%s payload=%s",
                     typ.upper(), session.device_id,
                     json.dumps(obj, ensure_ascii=False)[:500])
            if typ == "abort":
                # 板子说话中再按 BOOT 发 abort；服务端要真的停 TTS。
                self._abort_response(session)
                self._cancel_capture(session)
        else:
            log.info("WS %s 未知文本 type=%r payload=%s",
                     session.device_id, typ, json.dumps(obj, ensure_ascii=False)[:500])
        return hello_done

    # ---- A4.2 上行录音 / STT / Chat / TTS ---------------------------------
    def _start_capture(self, session: BodySession) -> None:
        if session.is_closed:
            return
        if self.brain is None and not self.cfg.record_only:
            return
        if self.cfg.record_only and self._record_done:
            log.info("record-only 已完成，忽略后续 listen start device=%s",
                     session.device_id)
            return
        if session.processing or time.monotonic() < self._listen_cooldown_until:
            # 板子在 TTS 尾音/上一轮收尾期间就会自动发 listen start。
            # 不能丢：记下来，等 processing 结束、尾音冷却过去再开录音。
            self._schedule_pending_start(session)
            return
        self._start_capture_now(session)

    def _start_capture_now(self, session: BodySession) -> None:
        if session.is_closed:
            return
        if self.brain is None and not self.cfg.record_only:
            return
        old = self._captures.get(session.session_id)
        if old is not None:
            log.info("LISTEN start 重复，沿用当前录音")
            return
        cap = body_capture.Utterance(session_id=session.session_id, session=session)
        self._captures[session.session_id] = cap
        timer = asyncio.create_task(
            self._capture_timer(cap),
            name=f"body-capture-{session.session_id}",
        )
        self._capture_timers[session.session_id] = timer
        log.info("录音开始 device=%s session=%s record_only=%s",
                 session.device_id, session.session_id, self.cfg.record_only)

        # 推迟启动期间收到的包先喂给录音器，用户看到“聆听中”马上开口
        # 也不会丢首字。
        pre = self._pending_audio.pop(session.session_id, [])
        for packet in pre:
            try:
                endpoint = cap.feed(
                    packet, min_speech_frames=8, silence_frames=15)
            except Exception:
                log.exception("预滚音频处理异常 device=%s", session.device_id)
                break
            if endpoint and not self.cfg.record_only:
                self._finish_capture_soon(cap, "endpoint-preroll")
                break
        if pre:
            log.info("预滚音频已接入 device=%s packets=%d", session.device_id, len(pre))

    @staticmethod
    def _drop_pending_task(task: asyncio.Task[Any]) -> None:
        if task.done():
            try:
                task.exception()
            except (asyncio.CancelledError, Exception):
                pass

    def _schedule_pending_start(self, session: BodySession) -> None:
        if session.is_closed:
            return
        task = self._pending_starts.get(session.session_id)
        if task is not None and not task.done():
            return
        task = asyncio.create_task(
            self._wait_then_start(session),
            name=f"body-pending-listen-{session.session_id}",
        )
        self._pending_starts[session.session_id] = task
        task.add_done_callback(self._drop_pending_task)
        task.add_done_callback(
            lambda t: self._pending_starts.pop(session.session_id, None)
            if self._pending_starts.get(session.session_id) is t else None
        )
        log.info(
            "LISTEN start 推迟 device=%s processing=%s cooldown=%.2fs",
            session.device_id, session.processing,
            max(0.0, self._listen_cooldown_until - time.monotonic()),
        )

    async def _wait_then_start(self, session: BodySession) -> None:
        try:
            while not session.is_closed:
                wait = max(0.0, self._listen_cooldown_until - time.monotonic())
                if session.processing or wait > 0.0:
                    await asyncio.sleep(min(0.08, max(0.02, wait)))
                    continue
                break
            if session.is_closed:
                return
            if session.session_id in self._captures:
                return
            self._start_capture_now(session)
        except asyncio.CancelledError:
            raise

    async def _capture_timer(self, cap: body_capture.Utterance) -> None:
        if self.cfg.record_only:
            try:
                limit = max(0.5, self.cfg.record_seconds)
                while not cap.done:
                    await asyncio.sleep(0.2)
                    if self._captures.get(cap.session_id) is not cap:
                        return
                    if cap.elapsed() >= limit:
                        log.info("record-only 到达时长 session=%s %.1fs",
                                 cap.session_id, cap.elapsed())
                        self._finish_capture_soon(cap, "record-seconds")
                        return
            except asyncio.CancelledError:
                pass
            return
        try:
            while not cap.done:
                await asyncio.sleep(0.2)
                if self._captures.get(cap.session_id) is not cap:
                    return
                if not cap.heard_speech and cap.elapsed() >= 5.0:
                    self._finish_capture_soon(cap, "no-speech")
                    return
                if cap.elapsed() >= self.cfg.capture_max_sec:
                    self._finish_capture_soon(cap, "max-seconds")
                    return
        except asyncio.CancelledError:
            pass

    def _feed_audio(self, session: BodySession, data: bytes) -> None:
        cap = self._captures.get(session.session_id)
        if cap is None or cap.done:
            # 已经发了 listen start 但服务端还在等冷却/上一轮收尾：
            # 先把包存成预滚，真正开录音时补喂。
            if session.session_id in self._pending_starts:
                buf = self._pending_audio.setdefault(session.session_id, [])
                buf.append(data)
                if len(buf) > LISTEN_PREROLL_PACKETS:
                    del buf[:-LISTEN_PREROLL_PACKETS]
            return
        try:
            endpoint = cap.feed(
                data,
                min_speech_frames=8,
                silence_frames=15,
            )
        except Exception:
            log.exception("上行 Opus 处理异常 device=%s", session.device_id)
            return
        if endpoint and not self.cfg.record_only:
            log.info("端点检测：说完 device=%s %s", session.device_id, cap.stats())
            self._finish_capture_soon(cap, "endpoint")

    def _finish_capture_soon(self, cap: body_capture.Utterance, reason: str) -> None:
        if cap.done or cap.session_id in self._finishing:
            return
        self._finishing.add(cap.session_id)
        asyncio.create_task(
            self._finish_capture(cap, reason),
            name=f"body-finish-{cap.session_id}",
        )

    def _request_finish(self, session: BodySession, reason: str) -> None:
        cap = self._captures.get(session.session_id)
        if cap is None or cap.done:
            # 按键在录音还没真正开始时按了“结束聆听”：撤掉待启动。
            task = self._pending_starts.pop(session.session_id, None)
            if task is not None and not task.done():
                task.cancel()
            self._pending_audio.pop(session.session_id, None)
            return
        self._finish_capture_soon(cap, reason)

    def _cancel_capture(self, session: BodySession) -> None:
        task = self._pending_starts.pop(session.session_id, None)
        if task is not None and not task.done():
            task.cancel()
        self._pending_audio.pop(session.session_id, None)
        cap = self._captures.get(session.session_id)
        if cap is None:
            return
        if self.cfg.record_only and cap.packets and not self._record_done:
            # 板子断连/再按 abort 时，已经收到的音频不能丢。
            self._finish_capture_soon(cap, "record-cancel")
            return
        self._captures.pop(session.session_id, None)
        cap.done = True
        self._finishing.discard(session.session_id)
        timer = self._capture_timers.pop(session.session_id, None)
        if timer is not None and not timer.done():
            timer.cancel()
        log.info("录音取消 device=%s %s", session.device_id, cap.stats())

    async def _finish_capture(self, cap: body_capture.Utterance,
                              reason: str) -> None:
        if cap.done:
            self._finishing.discard(cap.session_id)
            return
        cap.done = True
        self._finishing.discard(cap.session_id)
        if self._captures.get(cap.session_id) is cap:
            self._captures.pop(cap.session_id, None)
        timer = self._capture_timers.pop(cap.session_id, None)
        if timer is not None and timer is not asyncio.current_task() and not timer.done():
            timer.cancel()
        if self.cfg.record_only:
            await self._save_recording(cap, reason)
            return
        session = cap.session
        if session is None or session.is_closed:
            log.info("录音结束但会话已断 device=%s reason=%s", cap.session_id, reason)
            return
        log.info("录音结束 device=%s reason=%s %s",
                 session.device_id, reason, cap.stats())
        if not cap.heard_speech:
            log.info("没有检测到人声，不发回答 device=%s", session.device_id)
            return
        try:
            pcm = await asyncio.to_thread(cap.pcm16)
        except Exception:
            log.exception("上行音频解码失败 device=%s", session.device_id)
            return
        duration = float(pcm.size) / body_audio.OPUS_SAMPLE_RATE
        if duration < 0.35:
            log.info("有效语音太短（%.2fs），不发回答", duration)
            return
        log.info("STT 开始 device=%s duration=%.2fs", session.device_id, duration)
        await self._respond_to_audio(session, pcm, cap)

    def _abort_response(self, session: BodySession) -> None:
        task = self._response_tasks.pop(session.session_id, None)
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
        session.processing = False
        # 板子那边可能已经切回待机；补一个 tts stop，避免它停在“说话中”。
        if not session.is_closed:
            asyncio.create_task(
                session.send_tts_stop(), name=f"body-abort-{session.session_id}")

    async def _respond_to_audio(self, session: BodySession,
                                pcm: Any, cap: body_capture.Utterance) -> None:
        assert self.brain is not None
        session.processing = True
        self._response_tasks[session.session_id] = asyncio.current_task()  # type: ignore[assignment]
        t0 = time.time()
        stt: dict[str, Any] = {}
        reply = ""
        sent = 0
        try:
            try:
                stt = await self.brain.transcribe_async(pcm)
            except Exception as exc:
                log.exception("STT 失败 device=%s", session.device_id)
                stt = {"text": "", "error": f"{type(exc).__name__}: {exc}"}
            text = str(stt.get("text") or "").strip()
            if not text:
                log.info("STT 空结果 device=%s %s", session.device_id, stt)
                await session.send_stt("", is_final=True)
                sent = await self._speak_fallback(session, "我没听清，再说一遍。")
                return
            await session.send_stt(text)
            try:
                stream = await self._stream_reply(session, text, t0)
            except Exception as exc:
                log.exception("Chat 失败 device=%s", session.device_id)
                sent = await self._speak_fallback(
                    session, "我这边卡了一下，再说一遍。")
                reply = ""
                self.last_turn = {
                    "at": time.time(), "ok": False, "stt": text,
                    "error": f"{type(exc).__name__}: {exc}",
                    "audio_sec": round(float(pcm.size) / body_audio.OPUS_SAMPLE_RATE, 2),
                }
                return
            reply = str(stream.get("reply") or "")
            answer = stream.get("answer") or {}
            sent = int(stream.get("sent") or 0)
            if sent:
                self._listen_cooldown_until = (
                    time.monotonic() + LISTEN_TAIL_COOLDOWN_SEC
                )
                log.info(
                    "A4.2 流式下行 device=%s stt=%r reply=%r opus=%d "
                    "backend=%s first_audio=%.2fs",
                    session.device_id, text, reply, sent,
                    stream.get("backend"), float(stream.get("first_audio_sec") or 0),
                )
            self.last_turn = {
                "at": time.time(),
                "ok": True,
                "stt": text,
                "reply": reply,
                "sent_opus": sent,
                "backend": stream.get("backend"),
                "audio_sec": round(float(pcm.size) / body_audio.OPUS_SAMPLE_RATE, 2),
                "stt_sec": round(float(stt.get("seconds") or 0), 2),
                "chat_sec": answer.get("chat_sec"),
                "synth_sec": stream.get("synth_sec"),
                "first_audio_sec": stream.get("first_audio_sec"),
                "total_sec": round(time.time() - t0, 2),
            }
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("A4.2 回答异常 device=%s", session.device_id)
        finally:
            self._response_tasks.pop(session.session_id, None)
            session.processing = False

    async def _stream_reply(self, session: BodySession, text: str,
                            t0: float) -> dict[str, Any]:
        """流式 Chat → 分句 TTS：第一句生成完就先合成、先出声。

        旧路径要等整段回复生成完再整段合成，25 秒里有一大半是“等”。
        这里第一句一到就开 TTS；后续句子在上一句播放时继续生成/合成。
        """
        assert self.brain is not None
        buf = ""
        full = ""
        answer: dict[str, Any] = {}
        tts_started = False
        llm_sent = False
        sent = 0
        synth_sec = 0.0
        backend = ""
        first_audio: Optional[float] = None

        # 合成与播放拆成两条有界队列：合成 worker 先把下一句做出来，
        # 播放 worker 只按顺序往下发。第一块 PCM 一到就编码、就发，
        # 不再等整句 mp3 落盘、整文件解码。
        raw_q: asyncio.Queue = asyncio.Queue(maxsize=8)
        ready_q: asyncio.Queue = asyncio.Queue(maxsize=8)

        async def enqueue(clause: str) -> None:
            nonlocal llm_sent
            clean = body_brain.clean_for_speech(clause)
            if not clean:
                return
            if not llm_sent:
                # 协议顺序仍是 llm 在 tts 前面；先发第一句，板子先有字。
                await session.send_llm(clean)
                llm_sent = True
            await raw_q.put(clean)

        async def synth_worker() -> None:
            nonlocal synth_sec
            while True:
                clean = await raw_q.get()
                if clean is None:
                    await ready_q.put(None)
                    return
                t_synth = time.time()
                got_chunks = False
                try:
                    async for chunk in self.brain.synth_stream_async(clean):
                        packets = chunk.get("packets") or []
                        if not packets:
                            continue
                        got_chunks = True
                        await ready_q.put((clean, chunk))
                except Exception:
                    log.exception("流式分句 TTS 失败 device=%s clause=%r",
                                  session.device_id, clean)
                    if got_chunks:
                        # 已经出过音，不能再用整段补，否则板子会听到重复半句。
                        continue
                    try:
                        audio = await self.brain.synth_async(clean)
                    except Exception:
                        log.exception("分句整段 TTS 兜底失败 device=%s clause=%r",
                                      session.device_id, clean)
                        audio = None
                    if audio and audio.get("packets"):
                        await ready_q.put((clean, audio))
                finally:
                    synth_sec += time.time() - t_synth

        async def speaker_worker() -> None:
            nonlocal tts_started, sent, backend, first_audio
            current_clause = ""
            while True:
                item = await ready_q.get()
                if item is None:
                    return
                clean, audio = item
                packets = audio.get("packets") or []
                if not packets:
                    continue
                backend = str(audio.get("backend") or backend)
                if not tts_started:
                    await session.send_tts_start(clean)
                    await session.send_tts_sentence(clean)
                    tts_started = True
                    current_clause = clean
                elif clean != current_clause:
                    await session.send_tts_sentence(clean)
                    current_clause = clean
                if sent == 0 and first_audio is None:
                    # 第一块已经拿到；先记时刻再发送，后面每块继续流。
                    first_audio = round(time.time() - t0, 2)
                    log.info(
                        "流式首块出声 device=%s first_audio=%.2fs "
                        "synth_chunk=%.2fs clause=%r",
                        session.device_id, first_audio,
                        float(audio.get("first_chunk_sec") or 0), clean)
                sent += await body_voice.send_packets(session, packets)

        synth_task = asyncio.create_task(synth_worker())
        speaker_task = asyncio.create_task(speaker_worker())
        try:
            async for ev in self.brain.answer_stream_async(text):
                typ = str(ev.get("type") or "")
                if typ == "delta":
                    piece = str(ev.get("text") or "")
                    if not piece:
                        continue
                    full += piece
                    buf += piece
                    ready, buf = split_speakable(buf)
                    for clause in ready:
                        await enqueue(clause)
                elif typ == "done":
                    answer = dict(ev.get("result") or {})
                    if buf.strip():
                        await enqueue(buf)
                        buf = ""
                elif typ == "error":
                    raise RuntimeError(str(ev.get("error") or "流式 Chat 失败"))
        finally:
            await raw_q.put(None)
            try:
                await synth_task
            except Exception:
                log.exception("TTS 合成 worker 退出异常 device=%s", session.device_id)
            try:
                await speaker_task
            except Exception:
                log.exception("TTS 播放 worker 退出异常 device=%s", session.device_id)
            # 已经开过 TTS 就必须收尾，否则板子会一直停在“说话中”。
            if tts_started:
                await session.send_tts_stop()

        reply = str(answer.get("reply") or "").strip()
        if not reply:
            reply = body_brain.clean_for_speech(full) or "我在。"
        if not llm_sent and reply:
            await session.send_llm(reply)
            llm_sent = True
        if not tts_started and reply:
            # 流式过程中一个完整句都没切出来（极少见），退回整段合成。
            try:
                audio = await self.brain.synth_async(reply)
                sent += await body_voice.send_audio(
                    session, text=reply, packets=audio.get("packets") or [])
                synth_sec += float(audio.get("synth_sec") or 0.0)
                backend = str(audio.get("backend") or backend)
                if sent and first_audio is None:
                    first_audio = round(time.time() - t0, 2)
            except Exception:
                log.exception("整段兜底 TTS 失败 device=%s", session.device_id)
        return {
            "reply": reply,
            "answer": answer,
            "sent": sent,
            "backend": backend,
            "synth_sec": round(synth_sec, 2),
            "first_audio_sec": first_audio,
        }

    async def _speak_fallback(self, session: BodySession, text: str) -> int:
        if self.brain is None:
            return 0
        try:
            audio = await self.brain.synth_async(text)
            sent = await body_voice.send_audio(
                session, text=text, packets=audio["packets"])
            self._listen_cooldown_until = (
                time.monotonic() + LISTEN_TAIL_COOLDOWN_SEC
            )
            return sent
        except Exception:
            log.exception("兜底 TTS 失败")
            return 0

    # ---- A4.1 demo downlink ---------------------------------------------
    def _schedule_demo_reply(self, session: BodySession) -> None:
        if session.session_id in self._demo_sent or session.is_closed:
            return
        if session.session_id in self._demo_tasks:
            return
        self._demo_sent.add(session.session_id)
        task = asyncio.create_task(
            self._demo_reply(session),
            name=f"body-demo-{session.session_id}",
        )
        self._demo_tasks[session.session_id] = task

        def _drop_done(t: asyncio.Task[Any]) -> None:
            if self._demo_tasks.get(session.session_id) is t:
                self._demo_tasks.pop(session.session_id, None)

        task.add_done_callback(_drop_done)

    async def _demo_reply(self, session: BodySession) -> None:
        try:
            sent = await body_voice.send_demo(
                session,
                text=body_voice.DEMO_TEXT,
                path=self.cfg.demo_audio,
            )
            log.info(
                "DEMO 下行 device=%s session=%s opus=%d tx_bin=%d",
                session.device_id, session.session_id, sent, session.tx_binary,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("DEMO 下行失败 device=%s", session.device_id)

    # ---- health ----------------------------------------------------------
    async def handle_health(self, request: web.Request) -> web.Response:
        return web.json_response({
            "ok": True,
            "server": "valkyrie-xiaozhi",
            "uptime_sec": round(time.time() - self.started_at, 1),
            "ota_requests": self.ota_requests,
            "ws_connections": self.ws_connections,
            "ws_rejected": self.ws_rejected,
            "sessions": await self.registry.snapshot(),
            "sessions_count": await self.registry.count(),
            "last_ota": self.last_ota,
            "last_ws": self.last_ws,
            "demo_reply": self.cfg.demo_reply,
            "demo_sent": len(self._demo_sent),
            "chat": self.cfg.chat,
            "brain": self.brain.health() if self.brain is not None else None,
            "tts": self.brain.tts_status() if self.brain is not None else None,
            "last_turn": self.last_turn,
            "captures": [c.stats() for c in self._captures.values()],
            "pending_starts": list(self._pending_starts),
            "pending_audio_packets": {
                sid: len(buf) for sid, buf in self._pending_audio.items() if buf
            },
            "token": self.cfg.token,
            "ws_url": f"ws://{self.cfg.host}:{self.cfg.port}{WS_PATH}",
        })

    async def handle_index(self, request: web.Request) -> web.Response:
        return web.Response(
            text=(
                "valkyrie body server\n"
                f"OTA: http://<PC-IP>:{self.cfg.port}{OTA_PATH}\n"
                f"WS : ws://<PC-IP>:{self.cfg.port}{WS_PATH}\n"
                f"health: http://127.0.0.1:{self.cfg.port}/health\n"
            ),
            content_type="text/plain; charset=utf-8",
        )


def _has_port(host: str) -> bool:
    # 只处理常见的 host / host:port；IPv6 明文场景不在 A 轨内。
    if host.startswith("["):
        return "]:" in host
    return host.rsplit(":", 1)[-1].isdigit() and host.count(":") == 1


def make_app(cfg: ServerConfig) -> web.Application:
    server = BodyServer(cfg)
    app = web.Application()
    app["body_server"] = server
    app.router.add_get("/", server.handle_index)
    app.router.add_get("/health", server.handle_health)
    app.router.add_route("GET", OTA_PATH, server.handle_ota)
    app.router.add_route("POST", OTA_PATH, server.handle_ota)
    app.router.add_get(WS_PATH, server.handle_ws)
    app.on_startup.append(server.on_startup)
    app.on_cleanup.append(server.on_cleanup)
    return app


def setup_logging(log_path: Path, debug: bool = False) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    level = logging.DEBUG if debug else logging.INFO
    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    handlers: list[logging.Handler] = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(log_path, encoding="utf-8"),
    ]
    logging.basicConfig(level=level, format=fmt, handlers=handlers, force=True)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="瓦尔基里身体 A 轨服务端（xiaozhi 兼容）")
    ap.add_argument("--host", default="0.0.0.0", help="监听地址")
    ap.add_argument("--port", type=int, default=8768, help="OTA + WS 共用端口")
    ap.add_argument("--token", default="local-dev", help="OTA 下发的 token")
    ap.add_argument("--public-host", default="", help="OTA 里回给板子的主机名/IP，默认用请求 Host")
    ap.add_argument("--strict-token", action="store_true", help="WS 强制校验 Bearer token")
    ap.add_argument("--max-devices", type=int, default=3)
    ap.add_argument("--hello-timeout", type=float, default=10.0)
    ap.add_argument("--idle-timeout", type=float, default=90.0)
    ap.add_argument("--inspect-audio", action="store_true", help="打印上行 Opus 包大小")
    ap.add_argument("--demo-reply", action="store_true",
                    help="A4.1：唤醒后推一句固定 TTS，验证板子喇叭")
    ap.add_argument("--demo-audio", default=str(body_voice.DEFAULT_DEMO),
                    help="A4.1 固定测试音频（mp3/wav）")
    ap.add_argument("--chat", action="store_true",
                    help="A4.2：接 STT -> Chat -> TTS（默认临时库，不写真实记忆）")
    ap.add_argument("--live-memory", action="store_true",
                    help="A4.3：接真实 memory.db / 人格 / 心流 / 休眠（配合 --chat）")
    ap.add_argument("--no-write", action="store_true",
                    help="验收期只读：板子可连，但不写记忆/心智/心流/学习；强制临时库")
    ap.add_argument("--brain-dir", default="",
                    help="临时大脑目录；--live-memory 时仅作 TTS 缓存目录")
    ap.add_argument("--capture-max", type=float, default=8.0,
                    help="单次录音最长秒数（默认 8）")
    ap.add_argument("--log", default=str(DEFAULT_LOG), help="日志路径")
    ap.add_argument("--debug", action="store_true")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    args = parse_args(argv)
    log_path = Path(args.log)
    setup_logging(log_path, args.debug)
    cfg = ServerConfig(
        host=args.host,
        port=args.port,
        token=args.token,
        public_host=args.public_host,
        strict_token=args.strict_token,
        max_devices=args.max_devices,
        hello_timeout_sec=args.hello_timeout,
        idle_timeout_sec=args.idle_timeout,
        inspect_audio=args.inspect_audio,
        demo_reply=args.demo_reply,
        demo_audio=args.demo_audio,
        chat=args.chat,
        live_memory=args.live_memory,
        no_write=args.no_write,
        brain_dir=args.brain_dir,
        capture_max_sec=args.capture_max,
        debug=args.debug,
    )
    log.info("启动：OTA http://%s:%d%s  WS ws://%s:%d%s",
             cfg.host, cfg.port, OTA_PATH, cfg.host, cfg.port, WS_PATH)
    log.info("A4.1 demo_reply=%s audio=%s", cfg.demo_reply, cfg.demo_audio)
    log.info("A4.2 chat=%s live_memory=%s no_write=%s capture_max=%.1fs",
             cfg.chat, cfg.live_memory, cfg.no_write, cfg.capture_max_sec)
    log.info("日志：%s", log_path)
    app = make_app(cfg)
    web.run_app(app, host=cfg.host, port=cfg.port, print=None, access_log=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
