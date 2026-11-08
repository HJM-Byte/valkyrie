"""下位机链路：USB CDC 串口 + 定长帧协议（CRC16）。

帧格式：AA 55 | type(1) | len(2, LE) | payload(len) | crc16(2, LE)
CRC16 用 CCITT-FALSE（init 0xFFFF，poly 0x1021），与常见 MCU 库一致。

没有硬件时用 MockLink，接口相同，方便 PC 阶段把整条链路跑通。
"""
from __future__ import annotations

import struct
import threading
import time
from typing import Callable, Optional

from . import config as C
from .state import State, LED_HINT


# ---- 消息类型 --------------------------------------------------------------
MSG_PING = 0x01
MSG_PONG = 0x02
MSG_STATE = 0x10
MSG_BUTTON = 0x11
MSG_BATTERY = 0x12
MSG_CMD_LED = 0x20
MSG_CMD_SERVO = 0x21
MSG_CMD_MOTOR = 0x22

HEADER = b"\xAA\x55"


# ---- CRC -------------------------------------------------------------------
def crc16_ccitt(data: bytes, init: int = 0xFFFF, poly: int = 0x1021) -> int:
    crc = init
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ poly) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def encode(msg_type: int, payload: bytes = b"") -> bytes:
    body = bytes([msg_type]) + struct.pack("<H", len(payload)) + payload
    return HEADER + body + struct.pack("<H", crc16_ccitt(body))


class FrameParser:
    """流式解析：喂字节，吐出 (type, payload) 列表。"""

    def __init__(self) -> None:
        self._buf = bytearray()
        self.errors = 0

    def feed(self, data: bytes) -> list[tuple[int, bytes]]:
        self._buf.extend(data)
        out: list[tuple[int, bytes]] = []
        while True:
            i = self._buf.find(HEADER)
            if i < 0:
                if len(self._buf) > 2:
                    del self._buf[:-1]
                return out
            if i > 0:
                del self._buf[:i]
            if len(self._buf) < 6:
                return out
            msg_type = self._buf[2]
            length = struct.unpack_from("<H", self._buf, 3)[0]
            if length > 512:                     # 明显异常，丢一个字节重新同步
                self.errors += 1
                del self._buf[0]
                continue
            total = 5 + length + 2
            if len(self._buf) < total:
                return out
            payload = bytes(self._buf[5:5 + length])
            crc_recv = struct.unpack_from("<H", self._buf, 5 + length)[0]
            crc_calc = crc16_ccitt(bytes(self._buf[2:5 + length]))
            if crc_recv != crc_calc:
                self.errors += 1
                del self._buf[0]
                continue
            out.append((msg_type, payload))
            del self._buf[:total]


# ---- 链路基类 --------------------------------------------------------------
class BaseLink:
    def __init__(self) -> None:
        self.on_button: Optional[Callable[[int], None]] = None
        self.on_battery: Optional[Callable[[float, int], None]] = None
        self.on_raw: Optional[Callable[[int, bytes], None]] = None

    def start(self) -> bool:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

    def send(self, msg_type: int, payload: bytes = b"") -> bool:
        raise NotImplementedError

    def set_state(self, state: State) -> None:
        sid = list(State).index(state)
        self.send(MSG_STATE, struct.pack("<BB", sid, 0))

    def request_battery(self) -> None:
        self.send(MSG_PING)


# ---- 真串口 ----------------------------------------------------------------
class SerialLink(BaseLink):
    def __init__(self, port: str, baud: int = C.SERIAL_BAUD) -> None:
        super().__init__()
        self.port = port
        self.baud = baud
        self._ser = None
        self._parser = FrameParser()
        self._alive = False
        self._thr: Optional[threading.Thread] = None

    def start(self) -> bool:
        try:
            import serial
            self._ser = serial.Serial(self.port, self.baud, timeout=C.SERIAL_TIMEOUT)
        except Exception:
            return False
        self._alive = True
        self._thr = threading.Thread(target=self._loop, daemon=True)
        self._thr.start()
        return True

    def _loop(self) -> None:
        while self._alive and self._ser is not None:
            try:
                chunk = self._ser.read(256)
            except Exception:
                break
            if not chunk:
                continue
            for msg_type, payload in self._parser.feed(chunk):
                self._dispatch(msg_type, payload)

    def _dispatch(self, msg_type: int, payload: bytes) -> None:
        if self.on_raw:
            try:
                self.on_raw(msg_type, payload)
            except Exception:
                pass
        if msg_type == MSG_BUTTON and payload:
            if self.on_button:
                self.on_button(payload[0])
        elif msg_type == MSG_BATTERY and len(payload) >= 3:
            mv, pct = struct.unpack_from("<HB", payload)
            if self.on_battery:
                self.on_battery(mv / 1000.0, pct)

    def send(self, msg_type: int, payload: bytes = b"") -> bool:
        if self._ser is None:
            return False
        try:
            self._ser.write(encode(msg_type, payload))
            return True
        except Exception:
            return False

    def stop(self) -> None:
        self._alive = False
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:
                pass
            self._ser = None


# ---- 无硬件模拟 ------------------------------------------------------------
class MockLink(BaseLink):
    """没有 ESP32 时用：记录发出的帧，可以脚本化注入按键。"""

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[tuple[int, bytes]] = []

    def start(self) -> bool:
        return True

    def stop(self) -> None:
        pass

    def send(self, msg_type: int, payload: bytes = b"") -> bool:
        self.sent.append((msg_type, payload))
        return True

    def inject_button(self, key_id: int = 1) -> None:
        if self.on_button:
            self.on_button(key_id)

    def inject_battery(self, volt: float, pct: int) -> None:
        if self.on_battery:
            self.on_battery(volt, pct)


# ---- 端口枚举 --------------------------------------------------------------
def list_ports() -> list[dict]:
    try:
        from serial.tools import list_ports as lp
        return [{"device": p.device, "desc": p.description, "hwid": p.hwid}
                for p in lp.comports()]
    except Exception:
        return []


def guess_esp32_port() -> Optional[str]:
    """常见 ESP32-S3 USB CDC 的 VID:PID 是 303A:1001 / 303A:0002。"""
    for p in list_ports():
        hwid = (p.get("hwid") or "").upper()
        if "303A" in hwid:
            return p["device"]
    ports = list_ports()
    return ports[0]["device"] if len(ports) == 1 else None
