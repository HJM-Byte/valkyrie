"""身体下行音频：TTS 文件 -> 16k PCM -> 60 ms 裸 Opus 包 -> 板子。

A4.1 先验证“PC -> 板子喇叭”；A4.2 起由 BodyBrain 把 Chat 回复交给这里下发。
设计约束：
- 不在事件循环里做 mp3 解码/Opus 编码，全部在线程或启动时完成；
- 同一个音频文件只编码一次，按路径 + mtime 缓存；
- 按 60 ms 节奏推包，避免一次性灌爆板子下行缓冲。
"""
from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path
from typing import Sequence

from . import body_audio

log = logging.getLogger("valkyrie.body.voice")

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DEMO = ROOT / "data" / "body_demo.mp3"
DEMO_TEXT = "我在，听见了。"

_cache_lock = threading.Lock()
_cache: dict[str, tuple[tuple[str, int], list[bytes]]] = {}


def _file_key(path: Path) -> tuple[str, int]:
    try:
        st = path.stat()
        return (str(path), int(st.st_mtime_ns))
    except OSError:
        return (str(path), 0)


def _synth_demo(path: Path) -> None:
    """没有固定音频时，用项目 TTS 合成一句；失败就抛给调用方。"""
    from .tts import TTS

    path.parent.mkdir(parents=True, exist_ok=True)
    tts = TTS()
    try:
        produced = tts.synth(DEMO_TEXT)
        if produced.resolve() != path.resolve():
            path.write_bytes(produced.read_bytes())
    finally:
        try:
            tts.close()
        except Exception:
            pass


def load_packets(path: str | Path = DEFAULT_DEMO) -> list[bytes]:
    """读取任意 mp3/wav 并编码成 Opus 包；同一文件只编码一次。"""
    p = Path(path)
    key = _file_key(p)
    cache_key = str(p.resolve()) if p.exists() else str(p)
    with _cache_lock:
        hit = _cache.get(cache_key)
        if hit is not None and hit[0] == key:
            return list(hit[1])

        if not p.is_file() or p.stat().st_size <= 0:
            log.warning("身体音频不存在，尝试现合成：%s", p)
            _synth_demo(p)
        pcm = body_audio.decode_audio_file(p)
        if pcm.size == 0:
            raise body_audio.AudioCodecError(f"测试音频没有 PCM：{p}")
        packets = body_audio.pcm16_to_opus(pcm)
        if not packets:
            raise body_audio.AudioCodecError(f"测试音频编码后为空：{p}")
        _cache[cache_key] = (_file_key(p), packets)
        log.info(
            "身体音频就绪：%s，PCM %.2fs，Opus %d 包",
            p, pcm.size / body_audio.OPUS_SAMPLE_RATE, len(packets),
        )
        return list(packets)


def load_demo_packets(path: str | Path = DEFAULT_DEMO) -> list[bytes]:
    return load_packets(path)


async def send_packets(session, packets: Sequence[bytes],
                       frame_ms: int = body_audio.OPUS_FRAME_MS) -> int:
    """只推 Opus 包，不发 tts start/stop；供分句流式回答复用同一条 TTS。"""
    sent = 0
    for pkt in packets:
        if session.is_closed:
            break
        if await session.send_bytes(pkt):
            sent += 1
        else:
            break
        # 板子按 60 ms 包播放；按同样节奏推，避免一次性灌爆下行缓冲。
        await asyncio.sleep(frame_ms / 1000.0)
    return sent


async def send_audio(session, *, text: str,
                     packets: Sequence[bytes] | None = None,
                     path: str | Path | None = None,
                     frame_ms: int = body_audio.OPUS_FRAME_MS) -> int:
    """给一条会话推 TTS；返回发出的 Opus 包数。"""
    if packets is not None:
        pkts = list(packets)
    else:
        if path is None:
            raise ValueError("send_audio 需要 packets 或 path")
        pkts = await asyncio.to_thread(load_packets, path)
    if not pkts:
        return 0
    await session.send_tts_start(text)
    await session.send_tts_sentence(text)
    sent = await send_packets(session, pkts, frame_ms=frame_ms)
    await session.send_tts_stop()
    return sent


async def send_demo(session, *, text: str = DEMO_TEXT,
                    packets: Sequence[bytes] | None = None,
                    path: str | Path = DEFAULT_DEMO,
                    frame_ms: int = body_audio.OPUS_FRAME_MS) -> int:
    """给一条会话推固定 TTS；保留给 A4.1 自检/回退。"""
    return await send_audio(session, text=text, packets=packets,
                            path=path, frame_ms=frame_ms)
