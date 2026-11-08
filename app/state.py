"""状态机。六个状态，是整机的骨架：ESP32 的灯和按键都挂在这上面。"""
from __future__ import annotations

from enum import Enum
from typing import Callable


class State(Enum):
    BOOTING = "booting"      # 启动自检
    IDLE = "idle"            # 待机，等人说话 / 按键
    LISTENING = "listening"  # 正在录音
    THINKING = "thinking"    # STT + LLM
    SPEAKING = "speaking"    # 播报
    ERROR = "error"          # 故障


# 下位机指示灯约定（写给 ESP32 侧的固件用）
LED_HINT = {
    State.BOOTING: "white_breath",
    State.IDLE: "blue_dim",
    State.LISTENING: "blue_breath",
    State.THINKING: "cyan_spin",
    State.SPEAKING: "green_solid",
    State.ERROR: "red_blink",
}


class Machine:
    def __init__(self, initial: State = State.BOOTING) -> None:
        self._state = initial
        self._listeners: list[Callable[[State, State, str], None]] = []

    @property
    def state(self) -> State:
        return self._state

    def on_change(self, fn: Callable[[State, State, str], None]) -> None:
        self._listeners.append(fn)

    def to(self, new: State, reason: str = "") -> None:
        if new is self._state:
            return
        old, self._state = self._state, new
        for fn in self._listeners:
            try:
                fn(old, new, reason)
            except Exception:
                pass
