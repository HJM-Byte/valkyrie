"""身体 WebSocket 会话与设备表。

这里只放协议层的纯逻辑，不碰 UI/记忆/LLM：

- OTA 响应（只给 websocket + server_time，不给 mqtt/activation/firmware）
- 设备 hello 校验与服务器 hello
- 同一 Device-Id 重连时踢掉旧连接
- 每台设备的音频参数、收发统计

真正的 HTTP/WS 监听在 `app.xiaozhi_server`，真机联调入口在 `body_server.cmd`。
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, MutableMapping

PROTOCOL_VERSION = "1"
DEFAULT_AUDIO_PARAMS: dict[str, Any] = {
    "format": "opus",
    "sample_rate": 16000,
    "channels": 1,
    "frame_duration": 60,
}

OTA_PATH = "/xiaozhi/ota/"
WS_PATH = "/xiaozhi/v1/"


class BodyProtocolError(RuntimeError):
    """对端发来的协议字段不合法。"""


class BodyRegistryFull(BodyProtocolError):
    """超过服务端允许的并发设备数。"""


def now_ms() -> int:
    return int(time.time() * 1000)


def normalize_audio_params(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    params = dict(DEFAULT_AUDIO_PARAMS)
    if not raw:
        return params
    for key in ("format", "sample_rate", "channels", "frame_duration"):
        if key in raw and raw[key] is not None:
            params[key] = raw[key]
    try:
        params["sample_rate"] = int(params["sample_rate"])
        params["channels"] = int(params["channels"])
        params["frame_duration"] = int(params["frame_duration"])
    except (TypeError, ValueError) as exc:
        raise BodyProtocolError(f"audio_params 不是整数：{raw!r}") from exc
    fmt = str(params["format"]).lower()
    if fmt not in ("opus",):
        raise BodyProtocolError(f"不支持的音频格式：{fmt!r}（只支持 opus）")
    if params["sample_rate"] != 16000:
        raise BodyProtocolError(
            f"不支持的采样率：{params['sample_rate']}（身体链路固定 16000）"
        )
    if params["channels"] != 1:
        raise BodyProtocolError(f"只支持单声道，收到 {params['channels']} 声道")
    if params["frame_duration"] not in (20, 40, 60):
        raise BodyProtocolError(
            f"不支持的帧长：{params['frame_duration']}ms（20/40/60）"
        )
    params["format"] = "opus"
    return params


def parse_device_headers(headers: Mapping[str, str],
                         *,
                         fallback_id: str = "") -> dict[str, str]:
    """从 OTA/WS 请求头提取设备身份。

    固件实际发送的头（大小写不敏感）：
      Device-Id、Client-Id、Protocol-Version、Authorization、User-Agent
    """
    def get(name: str) -> str:
        # aiohttp 的 CIMultiDict 已经大小写不敏感；普通 dict 这里兜底。
        value = headers.get(name)
        if value is None:
            lower = name.lower()
            for key, item in headers.items():
                if str(key).lower() == lower:
                    return str(item)
            return ""
        return str(value)

    device_id = get("Device-Id").strip() or fallback_id
    if not device_id:
        # 不把连接直接拒掉：没有 Device-Id 时用远端地址做临时身份，便于本机调试。
        device_id = f"unknown-{get('X-Forwarded-For') or 'local'}"
    return {
        "device_id": device_id,
        "client_id": get("Client-Id").strip(),
        "protocol_version": get("Protocol-Version").strip() or PROTOCOL_VERSION,
        "activation_version": get("Activation-Version").strip(),
        "user_agent": get("User-Agent").strip(),
        "authorization": get("Authorization").strip(),
    }


def ota_payload(ws_url: str, token: str, *, timestamp_ms: int | None = None) -> dict[str, Any]:
    """OTA 响应体。

    关键约束：**只有 websocket 和 server_time 两节**。
    - 出现 mqtt 节会抢走协议选择；
    - 出现 activation（code/challenge）会走激活分支；
    - 出现 firmware 会让板子尝试升级，必须禁止。
    """
    return {
        "websocket": {"url": ws_url, "token": token},
        "server_time": {
            "timestamp": timestamp_ms if timestamp_ms is not None else now_ms(),
            "timezone_offset": 480,
        },
    }


def server_hello(session_id: str,
                 audio_params: Mapping[str, Any] | None = None) -> dict[str, Any]:
    params = normalize_audio_params(audio_params)
    return {
        "type": "hello",
        "transport": "websocket",
        "session_id": session_id,
        "audio_params": params,
        "version": 1,
    }


def validate_client_hello(msg: Mapping[str, Any]) -> dict[str, Any]:
    if msg.get("type") != "hello":
        raise BodyProtocolError(f"期望 hello，收到 {msg.get('type')!r}")
    transport = str(msg.get("transport") or "websocket").lower()
    if transport != "websocket":
        raise BodyProtocolError(f"不支持的 transport：{transport!r}")
    params = normalize_audio_params(msg.get("audio_params"))  # type: ignore[arg-type]
    return {
        "version": msg.get("version", 1),
        "features": msg.get("features") or {},
        "audio_params": params,
    }


@dataclass
class BodySession:
    """一条设备连接。所有发送都串行化，避免后台任务与主循环抢写。"""

    ws: Any
    device_id: str
    client_id: str = ""
    protocol_version: str = PROTOCOL_VERSION
    remote: str = ""
    session_id: str = ""
    audio_params: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_AUDIO_PARAMS))
    features: dict[str, Any] = field(default_factory=dict)
    connected_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    hello_received: bool = False
    replaced: bool = False
    processing: bool = False
    rx_text: int = 0
    rx_binary: int = 0
    rx_bytes: int = 0
    tx_text: int = 0
    tx_binary: int = 0
    tx_bytes: int = 0
    last_error: str = ""
    _send_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    _closed: bool = field(default=False, repr=False)

    @property
    def is_closed(self) -> bool:
        return self._closed or bool(getattr(self.ws, "closed", False))

    def touch(self) -> None:
        self.last_seen = time.time()

    async def send_json(self, obj: Mapping[str, Any]) -> bool:
        if self.is_closed:
            return False
        async with self._send_lock:
            try:
                await self.ws.send_json(dict(obj))
                self.tx_text += 1
                self.touch()
                return True
            except Exception as exc:
                self.last_error = f"send_json: {exc}"
                return False

    async def send_bytes(self, data: bytes) -> bool:
        if not data or self.is_closed:
            return False
        async with self._send_lock:
            try:
                await self.ws.send_bytes(data)
                self.tx_binary += 1
                self.tx_bytes += len(data)
                self.touch()
                return True
            except Exception as exc:
                self.last_error = f"send_bytes: {exc}"
                return False

    async def send_hello(self) -> bool:
        return await self.send_json(server_hello(self.session_id, self.audio_params))

    async def send_stt(self, text: str, *, is_final: bool = True) -> bool:
        return await self.send_json({"type": "stt", "text": text, "is_final": is_final})

    async def send_llm(self, text: str, *, emotion: str = "") -> bool:
        payload: dict[str, Any] = {"type": "llm", "text": text}
        if emotion:
            payload["emotion"] = emotion
        return await self.send_json(payload)

    async def send_emotion(self, emotion: str) -> bool:
        return await self.send_json({"type": "emotion", "emotion": emotion})

    async def send_tts_start(self, text: str = "") -> bool:
        return await self.send_json({"type": "tts", "state": "start", "text": text})

    async def send_tts_sentence(self, text: str) -> bool:
        return await self.send_json({"type": "tts", "state": "sentence_start", "text": text})

    async def send_tts_stop(self) -> bool:
        return await self.send_json({"type": "tts", "state": "stop"})

    async def close(self, *, code: int = 1000, reason: str = "") -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self.ws.close(code=code, message=reason.encode("utf-8", "replace"))
        except Exception as exc:
            self.last_error = f"close: {exc}"

    def stats(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "client_id": self.client_id,
            "session_id": self.session_id,
            "remote": self.remote,
            "connected_sec": round(time.time() - self.connected_at, 1),
            "idle_sec": round(time.time() - self.last_seen, 1),
            "hello_received": self.hello_received,
            "processing": self.processing,
            "audio_params": dict(self.audio_params),
            "rx_text": self.rx_text,
            "rx_binary": self.rx_binary,
            "rx_bytes": self.rx_bytes,
            "tx_text": self.tx_text,
            "tx_binary": self.tx_binary,
            "tx_bytes": self.tx_bytes,
            "last_error": self.last_error,
        }


class DeviceRegistry:
    """Device-Id -> 会话。同一设备新连接进来时返回旧会话，由调用方踢掉。"""

    def __init__(self, max_sessions: int = 3) -> None:
        if max_sessions <= 0:
            raise ValueError("max_sessions must be positive")
        self.max_sessions = int(max_sessions)
        self._sessions: MutableMapping[str, BodySession] = {}
        self._lock = asyncio.Lock()

    async def register(self, session: BodySession) -> BodySession | None:
        async with self._lock:
            old = self._sessions.get(session.device_id)
            if old is not None and old is not session:
                self._sessions[session.device_id] = session
                old.replaced = True
                return old
            if old is None and len(self._sessions) >= self.max_sessions:
                raise BodyRegistryFull(
                    f"并发设备已满（{self.max_sessions}），拒绝 {session.device_id}"
                )
            self._sessions[session.device_id] = session
            return None

    async def unregister(self, session: BodySession) -> None:
        async with self._lock:
            if self._sessions.get(session.device_id) is session:
                self._sessions.pop(session.device_id, None)

    async def get(self, device_id: str) -> BodySession | None:
        async with self._lock:
            return self._sessions.get(device_id)

    async def snapshot(self) -> list[dict[str, Any]]:
        async with self._lock:
            return [s.stats() for s in self._sessions.values()]

    async def count(self) -> int:
        async with self._lock:
            return len(self._sessions)
