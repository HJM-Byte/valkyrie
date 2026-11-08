"""身体语音的上行录音缓冲与端点检测。

板子（LOOY / xiaozhi 固件）在唤醒后发：

    {"type": "listen", "state": "start", "mode": "auto"}
    [二进制 Opus 包，60 ms 一包]
    ...

这版固件不一定会发 `listen stop`（2026-11-02 实测 161 包后直接断开）。
所以服务端自己做端点：先等开口，听到人声后连续静音 1.2 s 就收尾；
没听到人声则 5 s 放弃，最长 8 s 强制收尾。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from . import body_audio

# 16-bit PCM 归一化后的 RMS 阈值。安静房底噪通常 < 0.004，正常说话 > 0.02。
SPEECH_RMS = 0.008


@dataclass
class Utterance:
    session_id: str
    session: Any = None
    started_at: float = field(default_factory=time.monotonic)
    packets: list[bytes] = field(default_factory=list)
    pcm_chunks: list[np.ndarray] = field(default_factory=list)
    decoder: body_audio.OpusStreamDecoder = field(
        default_factory=body_audio.OpusStreamDecoder)
    speech_frames: int = 0
    silence_frames: int = 0
    heard_speech: bool = False
    decode_errors: int = 0
    done: bool = False

    def feed(self, packet: bytes, *,
             min_speech_frames: int = 5,
             silence_frames: int = 10) -> bool:
        """收一个 Opus 包；返回 True 表示检测到说完。"""
        if self.done or not packet:
            return False
        self.packets.append(packet)
        try:
            chunk = self.decoder.decode(packet)
        except body_audio.AudioCodecError:
            self.decode_errors += 1
            return False
        if chunk.size == 0:
            return False
        self.pcm_chunks.append(chunk)

        audio = chunk.astype(np.float32) / 32768.0
        rms = float(np.sqrt(np.mean(audio * audio))) if audio.size else 0.0
        if rms >= SPEECH_RMS:
            self.speech_frames += 1
            self.silence_frames = 0
            self.heard_speech = True
        elif self.heard_speech:
            self.silence_frames += 1

        return (
            self.heard_speech
            and self.speech_frames >= min_speech_frames
            and self.silence_frames >= silence_frames
        )

    def pcm16(self) -> np.ndarray:
        """返回已解码的 16k PCM；流式解码为空时退回整段解码。"""
        if self.pcm_chunks:
            return np.ascontiguousarray(
                np.concatenate(self.pcm_chunks).astype(np.int16, copy=False))
        if not self.packets:
            return np.empty(0, dtype=np.int16)
        return body_audio.opus_frames_to_pcm16(self.packets)

    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    def stats(self) -> dict[str, Any]:
        return {
            "packets": len(self.packets),
            "speech_frames": self.speech_frames,
            "silence_frames": self.silence_frames,
            "heard_speech": self.heard_speech,
            "decode_errors": self.decode_errors,
            "elapsed_sec": round(self.elapsed(), 2),
        }
