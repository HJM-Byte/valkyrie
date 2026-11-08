"""身体链路音频：Opus 编解码与 16 kHz 重采样。

设备侧（xiaozhi 固件）的约定是 16 kHz、单声道、60 ms 一帧的裸 Opus 包；
PC 侧现有 TTS 可能是 24 kHz mp3 或 8 kHz wav，所以这里统一收口：

    TTS 文件 -> decode_audio_file() -> int16/16k PCM
    int16/16k PCM -> pcm16_to_opus() -> list[bytes]（每项一个 Opus 包）
    Opus 包 -> opus_frames_to_pcm16() -> int16/16k PCM（给 STT）

依赖 PyAV 19（自带 libopus）；不使用 PATH 里的 ffmpeg，也不依赖 scipy。
"""
from __future__ import annotations

import io
import wave
from pathlib import Path
from typing import Iterable, Sequence

import av
import numpy as np

OPUS_SAMPLE_RATE = 16000
OPUS_CHANNELS = 1
OPUS_FRAME_MS = 60
OPUS_FRAME_SAMPLES = OPUS_SAMPLE_RATE * OPUS_FRAME_MS // 1000   # 960
OPUS_BITRATE = 24000


class AudioCodecError(RuntimeError):
    """音频解码/编码失败。"""


def _as_pcm16(data: bytes | bytearray | memoryview | np.ndarray) -> np.ndarray:
    """统一成连续、native-endian 的 int16 单声道数组。"""
    if isinstance(data, np.ndarray):
        arr = np.asarray(data)
        if arr.dtype != np.int16:
            if arr.dtype.kind == "f":
                arr = np.clip(arr, -1.0, 1.0) * 32767.0
            arr = arr.astype(np.int16)
        arr = arr.reshape(-1)
        return np.ascontiguousarray(arr, dtype=np.int16)
    if isinstance(data, (bytearray, memoryview)):
        data = bytes(data)
    if not isinstance(data, bytes):
        raise TypeError(f"expected bytes/ndarray, got {type(data)!r}")
    if len(data) % 2:
        raise AudioCodecError(f"PCM16 字节数不是偶数：{len(data)}")
    arr = np.frombuffer(data, dtype="<i2").astype(np.int16, copy=True)
    return np.ascontiguousarray(arr)


def _frame_to_pcm16(frame: av.AudioFrame) -> np.ndarray:
    arr = frame.to_ndarray()
    if arr.ndim > 1:
        # PyAV 单声道常见形状是 (1, n)；多声道时这里只取第一声道，
        # 身体链路本来就是单声道，真出现多声道应当在上游先混音。
        arr = arr.reshape(-1) if arr.shape[0] == 1 else arr[0]
    return np.ascontiguousarray(arr.astype(np.int16, copy=False).reshape(-1))


def _collect(resampler: av.AudioResampler | None,
             frame: av.AudioFrame,
             out: list[np.ndarray]) -> None:
    if resampler is None:
        out.append(_frame_to_pcm16(frame))
        return
    for item in resampler.resample(frame):
        out.append(_frame_to_pcm16(item))


def pcm16_to_opus(pcm16: bytes | np.ndarray,
                  *,
                  frame_samples: int = OPUS_FRAME_SAMPLES,
                  bitrate: int = OPUS_BITRATE) -> list[bytes]:
    """把 16 kHz/单声道/int16 PCM 编成裸 Opus 包。

    最后一帧不足 60 ms 时补零，保证每个包时长一致；返回值每项可直接
    作为 WebSocket 二进制帧发送（version=1 的裸 Opus，不带 Ogg 头）。
    """
    if frame_samples <= 0:
        raise ValueError("frame_samples must be positive")
    arr = _as_pcm16(pcm16)
    if arr.size == 0:
        return []

    pad = (-arr.size) % frame_samples
    if pad:
        arr = np.pad(arr, (0, pad), mode="constant")

    ctx = av.CodecContext.create("libopus", "w")
    ctx.sample_rate = OPUS_SAMPLE_RATE
    ctx.layout = "mono"
    ctx.format = "s16"
    ctx.bit_rate = bitrate
    duration_ms = frame_samples * 1000.0 / OPUS_SAMPLE_RATE
    # libopus 的可选帧长是 2.5/5/10/20/40/60 ms；给 ffmpeg 的 option 用字符串。
    if duration_ms in (2.5, 5.0, 10.0, 20.0, 40.0, 60.0):
        ctx.options = {"frame_duration": f"{duration_ms:g}"}
    ctx.open()

    packets: list[bytes] = []
    for i in range(0, arr.size, frame_samples):
        chunk = arr[i:i + frame_samples]
        frame = av.AudioFrame(format="s16", layout="mono", samples=frame_samples)
        frame.sample_rate = OPUS_SAMPLE_RATE
        frame.pts = i
        frame.planes[0].update(chunk.tobytes())
        for packet in ctx.encode(frame):
            packets.append(bytes(packet))

    # 刷出编码器内部延迟；PyAV 不同小版本对 None 的接受度不同。
    try:
        for packet in ctx.encode(None):
            packets.append(bytes(packet))
    except (TypeError, ValueError):
        pass
    return packets


class Pcm16Resampler:
    """有状态的 PCM16 重采样器，给流式 TTS 逐块送样本。

    和 `resample_pcm16()` 的区别是保留 resampler 状态；每块单独调
    `resample_pcm16()` 会在块边界丢样本，长句会听出爆音/断续。
    """

    def __init__(self, src_rate: int = 24000,
                 dst_rate: int = OPUS_SAMPLE_RATE) -> None:
        if src_rate <= 0 or dst_rate <= 0:
            raise ValueError("sample rate must be positive")
        self.src_rate = int(src_rate)
        self.dst_rate = int(dst_rate)
        self._pts = 0
        self._resampler = None
        if self.src_rate != self.dst_rate:
            self._resampler = av.AudioResampler(
                format="s16", layout="mono", rate=self.dst_rate)

    def resample(self, pcm16: bytes | np.ndarray) -> np.ndarray:
        arr = _as_pcm16(pcm16)
        if arr.size == 0:
            return np.empty(0, dtype=np.int16)
        if self._resampler is None:
            return arr
        frame = av.AudioFrame(format="s16", layout="mono", samples=arr.size)
        frame.sample_rate = self.src_rate
        frame.pts = self._pts
        self._pts += arr.size
        frame.planes[0].update(arr.tobytes())
        chunks: list[np.ndarray] = []
        for item in self._resampler.resample(frame):
            chunks.append(_frame_to_pcm16(item))
        if not chunks:
            return np.empty(0, dtype=np.int16)
        return np.ascontiguousarray(
            np.concatenate(chunks).astype(np.int16, copy=False))

    def flush(self) -> np.ndarray:
        if self._resampler is None:
            return np.empty(0, dtype=np.int16)
        chunks: list[np.ndarray] = []
        try:
            for item in self._resampler.resample(None):
                chunks.append(_frame_to_pcm16(item))
        except (TypeError, ValueError):
            pass
        if not chunks:
            return np.empty(0, dtype=np.int16)
        return np.ascontiguousarray(
            np.concatenate(chunks).astype(np.int16, copy=False))


class OpusStreamEncoder:
    """有状态的 Opus 编码器：跨块保留不足 60ms 的尾巴。

    流式 TTS 每块 PCM 长度不固定，直接对每块调 `pcm16_to_opus()`
    会反复新建编码器、每块补零，既慢又会在块边界产生杂音。
    """

    def __init__(self, *,
                 frame_samples: int = OPUS_FRAME_SAMPLES,
                 bitrate: int = OPUS_BITRATE) -> None:
        if frame_samples <= 0:
            raise ValueError("frame_samples must be positive")
        self.frame_samples = int(frame_samples)
        self._pending: list[np.ndarray] = []
        self._pending_size = 0
        self._pts = 0
        self._ctx = av.CodecContext.create("libopus", "w")
        self._ctx.sample_rate = OPUS_SAMPLE_RATE
        self._ctx.layout = "mono"
        self._ctx.format = "s16"
        self._ctx.bit_rate = bitrate
        duration_ms = self.frame_samples * 1000.0 / OPUS_SAMPLE_RATE
        if duration_ms in (2.5, 5.0, 10.0, 20.0, 40.0, 60.0):
            self._ctx.options = {"frame_duration": f"{duration_ms:g}"}
        self._ctx.open()

    def _encode_frame(self, chunk: np.ndarray) -> list[bytes]:
        frame = av.AudioFrame(
            format="s16", layout="mono", samples=self.frame_samples)
        frame.sample_rate = OPUS_SAMPLE_RATE
        frame.pts = self._pts
        self._pts += self.frame_samples
        frame.planes[0].update(np.ascontiguousarray(
            chunk, dtype=np.int16).tobytes())
        return [bytes(packet) for packet in self._ctx.encode(frame)]

    def encode(self, pcm16: bytes | np.ndarray) -> list[bytes]:
        arr = _as_pcm16(pcm16)
        if arr.size == 0:
            return []
        self._pending.append(arr)
        self._pending_size += arr.size
        if self._pending_size < self.frame_samples:
            return []
        merged = np.concatenate(self._pending)
        self._pending = []
        self._pending_size = 0
        count = merged.size // self.frame_samples
        packets: list[bytes] = []
        for i in range(count):
            start = i * self.frame_samples
            packets.extend(self._encode_frame(
                merged[start:start + self.frame_samples]))
        rest = merged[count * self.frame_samples:]
        if rest.size:
            self._pending.append(rest)
            self._pending_size = rest.size
        return packets

    def flush(self) -> list[bytes]:
        packets: list[bytes] = []
        if self._pending:
            arr = np.concatenate(self._pending)
            pad = (-arr.size) % self.frame_samples
            if pad:
                arr = np.pad(arr, (0, pad), mode="constant")
            for i in range(0, arr.size, self.frame_samples):
                packets.extend(self._encode_frame(
                    arr[i:i + self.frame_samples]))
            self._pending = []
            self._pending_size = 0
        try:
            packets.extend(bytes(packet) for packet in self._ctx.encode(None))
        except (TypeError, ValueError):
            pass
        return packets


def opus_frames_to_pcm16(frames: Iterable[bytes],
                         *,
                         sample_rate: int = OPUS_SAMPLE_RATE) -> np.ndarray:
    """把裸 Opus 包还原成 int16 PCM。

    libopus 解码器通常输出 48 kHz；这里统一重采样到 sample_rate（默认 16k）。
    缺少解码尾帧不会报错，语音场景最多损失几毫秒。
    """
    decoder = av.CodecContext.create("libopus", "r")
    decoder.sample_rate = 48000
    decoder.layout = "mono"
    decoder.format = "s16"
    decoder.open()

    resampler = None
    if sample_rate != 48000:
        resampler = av.AudioResampler(format="s16", layout="mono", rate=sample_rate)

    chunks: list[np.ndarray] = []
    for raw in frames:
        if not raw:
            continue
        try:
            decoded = decoder.decode(av.Packet(raw))
        except Exception as exc:  # 坏包不应该让整个会话线程崩溃
            raise AudioCodecError(f"Opus 解码失败（{len(raw)} 字节）：{exc}") from exc
        for frame in decoded:
            _collect(resampler, frame, chunks)

    # 解码器尾部（如有）没有时间戳，丢掉也不会影响 STT。
    if chunks:
        pcm = np.concatenate(chunks).astype(np.int16, copy=False)
    else:
        pcm = np.empty(0, dtype=np.int16)
    return np.ascontiguousarray(pcm)


class OpusStreamDecoder:
    """逐包解码器：给服务端在收到每个 Opus 包时立刻算能量/做端点检测。

    和 `opus_frames_to_pcm16` 的差别是它保留解码器状态，不会每包重建；
    输出仍是 16 kHz、单声道、int16，可直接拼给 STT。
    """

    def __init__(self, sample_rate: int = OPUS_SAMPLE_RATE) -> None:
        self.sample_rate = int(sample_rate)
        self._decoder = av.CodecContext.create("libopus", "r")
        self._decoder.sample_rate = 48000
        self._decoder.layout = "mono"
        self._decoder.format = "s16"
        self._decoder.open()
        self._resampler = None
        if self.sample_rate != 48000:
            self._resampler = av.AudioResampler(
                format="s16", layout="mono", rate=self.sample_rate)

    def decode(self, packet: bytes) -> np.ndarray:
        if not packet:
            return np.empty(0, dtype=np.int16)
        chunks: list[np.ndarray] = []
        try:
            frames = self._decoder.decode(av.Packet(packet))
        except Exception as exc:
            raise AudioCodecError(
                f"Opus 流解码失败（{len(packet)} 字节）：{exc}") from exc
        for frame in frames:
            _collect(self._resampler, frame, chunks)
        if not chunks:
            return np.empty(0, dtype=np.int16)
        return np.ascontiguousarray(np.concatenate(chunks).astype(np.int16, copy=False))

    def flush(self) -> np.ndarray:
        """刷出解码器/重采样器里剩余帧；没有就返回空数组。"""
        chunks: list[np.ndarray] = []
        try:
            frames = self._decoder.decode(None)
        except (TypeError, ValueError):
            frames = []
        for frame in frames:
            _collect(self._resampler, frame, chunks)
        if self._resampler is not None:
            try:
                for frame in self._resampler.resample(None):
                    chunks.append(_frame_to_pcm16(frame))
            except (TypeError, ValueError):
                pass
        if not chunks:
            return np.empty(0, dtype=np.int16)
        return np.ascontiguousarray(np.concatenate(chunks).astype(np.int16, copy=False))


def decode_audio_file(path: str | Path,
                      *,
                      sample_rate: int = OPUS_SAMPLE_RATE) -> np.ndarray:
    """把 mp3/wav 等 TTS 输出解码成 int16/单声道/sample_rate PCM。"""
    p = Path(path)
    if not p.is_file():
        raise AudioCodecError(f"音频文件不存在：{p}")
    chunks: list[np.ndarray] = []
    try:
        container = av.open(str(p))
    except Exception as exc:
        raise AudioCodecError(f"打不开音频文件 {p}：{exc}") from exc
    try:
        if not container.streams.audio:
            raise AudioCodecError(f"文件没有音频流：{p}")
        stream = container.streams.audio[0]
        resampler = av.AudioResampler(format="s16", layout="mono", rate=sample_rate)
        for frame in container.decode(stream):
            _collect(resampler, frame, chunks)
        try:
            for frame in resampler.resample(None):
                chunks.append(_frame_to_pcm16(frame))
        except (TypeError, ValueError):
            pass
    finally:
        container.close()
    if not chunks:
        return np.empty(0, dtype=np.int16)
    return np.ascontiguousarray(np.concatenate(chunks).astype(np.int16, copy=False))


def resample_pcm16(pcm16: bytes | np.ndarray,
                   src_rate: int,
                   dst_rate: int = OPUS_SAMPLE_RATE) -> np.ndarray:
    """原始 PCM16 重采样；src_rate == dst_rate 时只做类型/形状归一。"""
    arr = _as_pcm16(pcm16)
    if src_rate <= 0 or dst_rate <= 0:
        raise ValueError("sample rate must be positive")
    if src_rate == dst_rate or arr.size == 0:
        return arr
    frame = av.AudioFrame(format="s16", layout="mono", samples=arr.size)
    frame.sample_rate = src_rate
    frame.pts = 0
    frame.planes[0].update(arr.tobytes())
    resampler = av.AudioResampler(format="s16", layout="mono", rate=dst_rate)
    chunks: list[np.ndarray] = []
    for item in resampler.resample(frame):
        chunks.append(_frame_to_pcm16(item))
    try:
        for item in resampler.resample(None):
            chunks.append(_frame_to_pcm16(item))
    except (TypeError, ValueError):
        pass
    if not chunks:
        return np.empty(0, dtype=np.int16)
    return np.ascontiguousarray(np.concatenate(chunks).astype(np.int16, copy=False))


def pcm16_duration(pcm16: bytes | np.ndarray,
                   sample_rate: int = OPUS_SAMPLE_RATE) -> float:
    """PCM16 时长（秒）。"""
    return _as_pcm16(pcm16).size / float(sample_rate)


def sine_pcm16(freq: float = 440.0,
               seconds: float = 1.0,
               *,
               sample_rate: int = OPUS_SAMPLE_RATE,
               amplitude: float = 0.2) -> np.ndarray:
    """生成正弦波，给自检/假身体用。"""
    n = max(0, int(round(seconds * sample_rate)))
    t = np.arange(n, dtype=np.float64) / float(sample_rate)
    return (np.sin(2.0 * np.pi * float(freq) * t) * float(amplitude) * 32767.0).astype(np.int16)


def wav_bytes(pcm16: bytes | np.ndarray,
              *,
              sample_rate: int = OPUS_SAMPLE_RATE) -> bytes:
    """把 PCM16 包成 wav 字节，方便落盘或调试。"""
    arr = _as_pcm16(pcm16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(int(sample_rate))
        wf.writeframes(arr.tobytes())
    return buf.getvalue()


def write_wav(path: str | Path,
              pcm16: bytes | np.ndarray,
              *,
              sample_rate: int = OPUS_SAMPLE_RATE) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(wav_bytes(pcm16, sample_rate=sample_rate))
    return p
