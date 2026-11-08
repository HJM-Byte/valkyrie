"""语音合成：BreezeBlue 音色优先，失败退 edge-tts，再退本地 VITS。

优先级与原因：
- BreezeBlue 的可选音色（寒蝉余音 voc_wkzpcy2xxwch、幽兰影
  voc_dcccp6b9p466）由手机端选择；它是云端接口，Key 优先读环境变量
  BREEZE_API_KEY；8766 后台服务没有终端环境时，读 data/breeze_key.txt
  （只本机当前用户可读写）。
- edge-tts 中文自然、免费，作为没有 Key 或 Breeze 故障时的在线兜底。
  本机到 speech.platform.bing.com 的 WebSocket 会优先走 IPv6，而这条路径
  被系统拒绝（WinError 1236），所以 edge 必须强制走 IPv4。
- 本地 VITS（sherpa-onnx，int8，约 40MB）不联网，保证断网时也能说话；
  代价是通用女声、8k，不是人格声线。
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
import socket
import time
import wave
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Iterator, Optional

import aiohttp
import numpy as np

from . import config as C
from .breeze_transport import BreezeError, BreezeTransport
from .dashscope_tts import DashScopeTTS, DashScopeTTSError


class TTS:
    # 一次 Breeze 失败后所有音色实例一起降温。每个手机音色都会 new 一个
    # TTS；只按实例冷却会让“寒蝉”刚失败、“幽兰影”又重走一遍死节点。
    _breeze_down_until = 0.0

    @staticmethod
    def _read_key_file() -> str:
        """Breeze Key 的本机落点：data/breeze_key.txt（只认第一行非注释）。

        8766 的手机服务由独立进程/看护拉起，拿不到桌面终端里临时 export 的
        Key；文件只在本机可读，便于重启后仍保持寒蝉余音。环境变量优先，
        文件只作为没有环境变量时的补充。
        """
        p = Path(C.DATA) / "breeze_key.txt"
        try:
            for line in p.read_text(encoding="utf-8-sig").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    return line
        except OSError:
            pass
        return ""

    def __init__(self, voice: str = C.TTS_VOICE, rate: str = C.TTS_RATE,
                 cache_dir: Optional[Path] = None,
                 local_dir: Optional[Path] = None,
                 breeze_voice: Optional[str] = None,
                 strict_breeze: bool = False) -> None:
        self.voice = voice
        self.rate = rate
        # 身体通道用：只允许用户指定的 Breeze 原声，Breeze 不可用时宁可
        # 不出声，也不静默换成 edge/本地通用女声。
        self.strict_breeze = bool(strict_breeze)
        # 放工作区 data\_tmp：run.cmd 会把 TEMP 指到仓库内的临时目录，但写入失败时
        # tempfile 会退到当前目录，把 wav 丢在项目根；固定到工作区更干净。
        self.cache_dir = Path(cache_dir) if cache_dir else Path(C.DATA / "_tmp")
        self.local_dir = Path(local_dir) if local_dir else Path(C.TTS_LOCAL_DIR)
        self._mixer_ready = False
        self._local = None
        self.last_backend = ""
        # edge 被拦时不要每句话都等一次超时；2 分钟内直接走本地。
        self._edge_blocked_until = 0.0
        self.breeze_key = (os.environ.get(C.BREEZE_API_KEY_ENV, "").strip()
                           or self._read_key_file())
        # 手机端给每个音色建一个 TTS 实例；不传时沿用全局默认。
        self.breeze_voice = (breeze_voice or C.BREEZE_VOICE_ID).strip() \
            or C.BREEZE_VOICE_ID
        self._breeze_ips = [x.strip() for x in C.BREEZE_FORCE_IPS.split(",")
                            if x.strip()]
        # Breeze 失败后短冷却；网络抖动恢复后要尽快回到寒蝉余音。
        self._breeze_blocked_until = 0.0
        self._breeze_transport: Optional[BreezeTransport] = None
        # 百炼复刻只作为显式兜底，默认关闭；用户要的是 Breeze 原声，
        # 不能再在断线时悄悄换成“像但不同源”的复刻声线。
        self._qwen_enabled = bool(getattr(C, "TTS_QWEN_FALLBACK", False))
        self._dashscope = DashScopeTTS(self.breeze_voice)

    def breeze_available(self) -> bool:
        return bool(self.breeze_key and self.breeze_voice)

    def _cache_path(self, tag: str, suffix: str) -> Path:
        """缓存名用稳定哈希；内置 hash() 每个进程不同，Breeze 会重复付费合成。"""
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha1(tag.encode("utf-8")).hexdigest()[:12]
        return self.cache_dir / f"tts_{digest}{suffix}"

    # ---- BreezeBlue 寒蝉余音 ----------------------------------------------
    def _breeze_client(self) -> BreezeTransport:
        if self._breeze_transport is None:
            self._breeze_transport = BreezeTransport(
                self.breeze_key,
                self.breeze_voice,
                host=C.BREEZE_HOST,
                ips=self._breeze_ips,
                connect_timeout=C.BREEZE_CONNECT_TIMEOUT,
                header_timeout=C.BREEZE_HEADER_TIMEOUT,
                read_timeout=C.BREEZE_READ_TIMEOUT,
                total_timeout=C.BREEZE_TOTAL_TIMEOUT,
                max_seconds=C.BREEZE_MAX_SECONDS,
            )
        return self._breeze_transport

    def warm(self) -> bool:
        """预热 Breeze 的 TLS 连接；第一句话可省掉约 1.3 秒握手。"""
        if not self.breeze_available() or time.time() < self._breeze_blocked_until:
            return False
        try:
            return self._breeze_client().prewarm()
        except Exception:
            return False

    def breeze_status(self) -> dict[str, object]:
        """给 /health 看 Breeze 连接是否还热着，别只报“服务活着”。"""
        tr = self._breeze_transport
        idle = getattr(tr, "_idle", None)
        return {
            "ready": self.breeze_available(),
            "last_ip": getattr(tr, "last_ip", "") if tr is not None else "",
            "idle": bool(idle),
            "last_backend": self.last_backend,
            "blocked_sec": round(max(
                0.0, self._breeze_blocked_until - time.time()), 1),
        }

    def close(self) -> None:
        if self._breeze_transport is not None:
            try:
                self._breeze_transport.close()
            except Exception:
                pass
            self._breeze_transport = None

    def _synth_breeze(self, text: str, rate: Optional[str] = None) -> Path:
        # 缓存键把演绎指令和模型也算进去，避免改 prompt 后还播旧音频。
        tag = ("breeze|" + self.breeze_voice + "|" + C.BREEZE_MODEL_ID
               + "|" + C.BREEZE_INSTRUCTIONS + "|" + text)
        out = self._cache_path(tag, ".mp3")
        if out.exists() and out.stat().st_size > 0:
            return out
        if not self.breeze_available():
            raise RuntimeError("未设置 BREEZE_API_KEY")
        try:
            self._breeze_client().synthesize(
                text,
                out,
                model_id=C.BREEZE_MODEL_ID,
                instructions=C.BREEZE_INSTRUCTIONS,
            )
        except BreezeError as e:
            raise RuntimeError(f"BreezeBlue: {e}") from e
        if not out.exists() or out.stat().st_size <= 0:
            raise RuntimeError("BreezeBlue 没有生成音频文件")
        return out

    def synth_stream_pcm(self, text: str) -> Iterator[bytes]:
        """strict_breeze 的逐块 24k PCM：板子和手机共用。

        先查完整 mp3 缓存；命中时直接解码成 PCM 分块返回，避免为了
        流式把已经付过费的句子再合成一遍。未命中才走 Breeze 流。
        """
        if not text.strip():
            raise ValueError("空文本")
        if not self.strict_breeze:
            raise RuntimeError("synth_stream_pcm 只用于 strict_breeze 身体通道")
        tag = ("breeze|" + self.breeze_voice + "|" + C.BREEZE_MODEL_ID
               + "|" + C.BREEZE_INSTRUCTIONS + "|" + text)
        cached = self._cache_path(tag, ".mp3")
        cached_wav = self._cache_path(tag, ".wav")
        for candidate in (cached_wav, cached):
            if candidate.exists() and candidate.stat().st_size > 0:
                try:
                    from . import body_audio
                    pcm = body_audio.decode_audio_file(
                        candidate, sample_rate=24000)
                    if pcm.size:
                        self.last_backend = "breeze"
                        self._breeze_blocked_until = 0.0
                        TTS._breeze_down_until = 0.0
                        step = 2048
                        for i in range(0, pcm.size, step):
                            yield pcm[i:i + step].tobytes()
                        return
                except Exception:
                    # 缓存坏了就当没命中，继续走云端；不能因为一个坏文件失声。
                    pass
        breeze_down = max(self._breeze_blocked_until, TTS._breeze_down_until)
        if not self.breeze_available() or time.time() < breeze_down:
            raise RuntimeError("身体指定 Breeze 音色不可用（未配置或冷却中）")
        pcm_parts: list[bytes] = []
        try:
            first = True
            for chunk in self._breeze_client().iter_pcm(
                    text,
                    model_id=C.BREEZE_MODEL_ID,
                    instructions=C.BREEZE_INSTRUCTIONS):
                if first:
                    first = False
                    self.last_backend = "breeze"
                    self._breeze_blocked_until = 0.0
                    TTS._breeze_down_until = 0.0
                pcm_parts.append(chunk)
                yield chunk
        except Exception as e:
            body_cooldown = float(getattr(
                C, "BREEZE_BODY_COOLDOWN_SECONDS", 3.0))
            TTS._breeze_down_until = time.time() + body_cooldown
            self._breeze_blocked_until = TTS._breeze_down_until
            raise RuntimeError(
                f"身体指定 Breeze 流式合成失败: "
                f"{type(e).__name__}: {e}") from e
        if pcm_parts:
            # 流式也要落缓存；下一轮同一句不再付费、不再等云端。
            try:
                from . import body_audio
                body_audio.write_wav(
                    cached_wav, b"".join(pcm_parts), sample_rate=24000)
            except Exception:
                pass
        self.last_backend = "breeze"
        self._breeze_blocked_until = 0.0
        TTS._breeze_down_until = 0.0

    # ---- 百炼兜底 ---------------------------------------------------------
    def _synth_qwen(self, text: str,
                    rate: Optional[str] = None) -> tuple[Path, str]:
        """复刻音色优先；复刻未就绪时用内置 Cherry/Serena。"""
        tag = ("qwen|" + self.breeze_voice + "|" + text)
        out = self._cache_path(tag, ".mp3")
        if out.exists() and out.stat().st_size > 0:
            return out, ("qwen_clone" if self._dashscope.clone_ready
                         else "qwen")
        if not self._dashscope.available():
            raise RuntimeError(f"未设置 {C.DASHSCOPE_KEY_ENV}")
        try:
            self._dashscope.synthesize(text, out)
        except DashScopeTTSError as e:
            raise RuntimeError(f"百炼 TTS: {e}") from e
        return out, (self._dashscope.last_backend or "qwen")

    def warm_fallback(self) -> bool:
        """后台预热复刻音色；默认关闭，只有显式打开才走百炼。"""
        if not self._qwen_enabled or not self._dashscope.available():
            return False
        return self._dashscope.warm()

    # ---- 在线 edge -------------------------------------------------------
    async def _synth_edge_async(self, text: str, out_path: Path,
                                rate: Optional[str] = None) -> None:
        import edge_tts
        # 本机 DNS 先返回 IPv6，而 speech.platform.bing.com 的 IPv6 路径会被
        # 系统直接中止（1236），默认连接必然落到本地兜底。强制 IPv4 后
        # 同一句话通常 1–2 秒内就能拿到 Xiaoxiao 的 24k 音频。
        connector = aiohttp.TCPConnector(family=socket.AF_INET, force_close=True)
        try:
            comm = edge_tts.Communicate(
                text, self.voice, rate=rate or self.rate, connector=connector)
            await comm.save(str(out_path))
        finally:
            await connector.close()

    def _synth_edge(self, text: str, rate: Optional[str] = None) -> Path:
        tag = "edge|" + (rate or self.rate or "") + "|" + text
        out = self._cache_path(tag, ".mp3")
        if not out.exists() or out.stat().st_size == 0:
            try:
                asyncio.run(self._synth_edge_async(text, out, rate))
            except Exception:
                try:
                    out.unlink(missing_ok=True)
                except Exception:
                    pass
                raise
        return out

    # ---- 离线本地 VITS ---------------------------------------------------
    def _local_paths(self) -> tuple[Path, Path, Path]:
        return (
            self.local_dir / "vits-aishell3.int8.onnx",
            self.local_dir / "tokens.txt",
            self.local_dir / "lexicon.txt",
        )

    def local_available(self) -> bool:
        if not C.TTS_LOCAL_ENABLED:
            return False
        model, tokens, lexicon = self._local_paths()
        return all(p.exists() and p.stat().st_size > 0
                   for p in (model, tokens, lexicon))

    def _local_engine(self):
        if self._local is not None:
            return self._local
        if not self.local_available():
            raise FileNotFoundError(f"本地声线模型缺失: {self.local_dir}")
        import sherpa_onnx
        model, tokens, lexicon = self._local_paths()
        cfg = sherpa_onnx.OfflineTtsConfig(
            model=sherpa_onnx.OfflineTtsModelConfig(
                vits=sherpa_onnx.OfflineTtsVitsModelConfig(
                    model=str(model),
                    tokens=str(tokens),
                    lexicon=str(lexicon),
                ),
                num_threads=2,
                provider="cpu",
            ),
            max_num_sentences=1,
        )
        self._local = sherpa_onnx.OfflineTts(cfg)
        return self._local

    @staticmethod
    def _speed_of(rate: Optional[str]) -> float:
        """把 edge 的 +8% / -15% 换成 VITS 的 speed 倍数。"""
        if not rate:
            return 1.0
        m = re.search(r"([+-]?\d+(?:\.\d+)?)\s*%", str(rate))
        if not m:
            return 1.0
        speed = 1.0 + float(m.group(1)) / 100.0
        return max(0.55, min(1.55, speed))

    def _synth_local(self, text: str, rate: Optional[str] = None) -> Path:
        tag = "local|" + (rate or self.rate or "") + "|" + text
        out = self._cache_path(tag, ".wav")
        if out.exists() and out.stat().st_size > 44:
            return out

        engine = self._local_engine()
        audio = engine.generate(text, sid=C.TTS_LOCAL_SID,
                                speed=self._speed_of(rate))
        samples = np.asarray(audio.samples, dtype=np.float32)
        if samples.size == 0:
            raise RuntimeError("本地 TTS 没有生成音频")
        pcm = np.clip(samples, -1.0, 1.0)
        pcm = (pcm * 32767.0).astype(np.int16)
        tmp = out.with_suffix(".wav.tmp")
        with wave.open(str(tmp), "wb") as f:
            f.setnchannels(1)
            f.setsampwidth(2)
            f.setframerate(int(audio.sample_rate))
            f.writeframes(pcm.tobytes())
        tmp.replace(out)
        return out

    # ---- 统一入口 ---------------------------------------------------------
    def _race_breeze_qwen(self, text: str,
                          rate: Optional[str]) -> Optional[Path]:
        """Breeze 与百炼同时跑，谁先出音用谁。

        Breeze 正常时 1.8–2.6 秒，保住用户选的音色；Cloudflare 断时
        百炼复刻 ~2 秒先回来，手机不再等 55 秒。输掉的那条线程自己收尾。
        """
        ex = ThreadPoolExecutor(max_workers=2)
        fb = ex.submit(self._synth_breeze, text, rate)
        fq = ex.submit(self._synth_qwen, text, rate)

        def _breeze_done(f) -> None:
            try:
                f.result()
                TTS._breeze_down_until = 0.0
            except Exception:
                TTS._breeze_down_until = (
                    time.time() + C.BREEZE_COOLDOWN_SECONDS)

        fb.add_done_callback(_breeze_done)
        pending = {fb, fq}
        errors: list[str] = []
        try:
            while pending:
                done, pending = wait(
                    pending, return_when=FIRST_COMPLETED, timeout=10.0)
                if not done:
                    errors.append("竞速超时")
                    break
                for f in done:
                    try:
                        res = f.result()
                    except Exception as e:
                        errors.append(f"{type(e).__name__}: {str(e)[:160]}")
                        continue
                    if f is fb:
                        self.last_backend = "breeze"
                        self._breeze_blocked_until = 0.0
                        return res
                    path, backend = res
                    self.last_backend = backend
                    return path
        finally:
            ex.shutdown(wait=False)
        if errors:
            print(f"[TTS] Breeze/百炼竞速失败: {' | '.join(errors[-3:])}",
                  flush=True)
        return None

    def synth(self, text: str, rate: Optional[str] = None) -> Path:
        if not text.strip():
            raise ValueError("空文本")
        breeze_down = max(self._breeze_blocked_until, TTS._breeze_down_until)
        breeze_ready = (self.breeze_available()
                        and time.time() >= breeze_down)
        qwen_ready = self._qwen_enabled and self._dashscope.available()

        # 身体通道：只认 Breeze 原声。失败就报错，不往 edge/本地掉。
        if self.strict_breeze:
            if not breeze_ready:
                raise RuntimeError("身体指定 Breeze 音色不可用（未配置或冷却中）")
            try:
                path = self._synth_breeze(text, rate=rate)
            except Exception as e:
                # 手机共用的 10 秒冷却会让板子下一轮直接“冷却中”失声；
                # 身体通道只压 3 秒，给下一句一次重新握手的机会。
                body_cooldown = float(getattr(
                    C, "BREEZE_BODY_COOLDOWN_SECONDS", 3.0))
                TTS._breeze_down_until = time.time() + body_cooldown
                self._breeze_blocked_until = TTS._breeze_down_until
                raise RuntimeError(
                    f"身体指定 Breeze 音色合成失败: {type(e).__name__}: {e}"
                ) from e
            self.last_backend = "breeze"
            self._breeze_blocked_until = 0.0
            TTS._breeze_down_until = 0.0
            return path

        path = None
        qwen_error = None
        if breeze_ready and qwen_ready:
            path = self._race_breeze_qwen(text, rate)
            if path is not None:
                return path
        elif qwen_ready:
            try:
                path, backend = self._synth_qwen(text, rate=rate)
                self.last_backend = backend
                return path
            except Exception as e:
                qwen_error = e
                print(f"[TTS] 百炼失败，切 edge/本地: "
                      f"{type(e).__name__}: {e}", flush=True)
        elif breeze_ready:
            try:
                path = self._synth_breeze(text, rate=rate)
                self.last_backend = "breeze"
                self._breeze_blocked_until = 0.0
                TTS._breeze_down_until = 0.0
                return path
            except Exception as e:
                TTS._breeze_down_until = (
                    time.time() + C.BREEZE_COOLDOWN_SECONDS)
                self._breeze_blocked_until = TTS._breeze_down_until
                print(f"[TTS] BreezeBlue 失败，切 edge/本地: "
                      f"{type(e).__name__}: {e}", flush=True)

        edge_error = None
        if time.time() >= self._edge_blocked_until:
            try:
                path = self._synth_edge(text, rate=rate)
                self.last_backend = "edge"
                self._edge_blocked_until = 0.0
                return path
            except Exception as e:
                edge_error = e
                # 网络侧拦截通常不会秒恢复，先冷却 2 分钟，避免每句都卡 5 秒
                self._edge_blocked_until = time.time() + 120.0
                print(f"[TTS] edge 失败，切本地离线声线: "
                      f"{type(e).__name__}: {e}", flush=True)

        if not self.local_available():
            raise RuntimeError(
                "BreezeBlue/百炼/edge-tts 合成失败，本地声线也不可用: "
                f"{qwen_error or edge_error}")
        path = self._synth_local(text, rate=rate)
        self.last_backend = "local"
        return path

    # ---- 播放 -------------------------------------------------------------
    def _ensure_mixer(self) -> bool:
        if self._mixer_ready:
            return True
        try:
            import pygame
            pygame.mixer.init(frequency=24000, size=-16, channels=1, buffer=512)
            self._mixer_ready = True
            return True
        except Exception:
            return False

    def play_file(self, path: Path, volume: float = 1.0) -> float:
        import pygame
        if self._ensure_mixer():
            try:
                pygame.mixer.music.load(str(path))
                pygame.mixer.music.set_volume(volume)
                t0 = time.time()
                pygame.mixer.music.play()
                while pygame.mixer.music.get_busy():
                    pygame.time.wait(20)
                return time.time() - t0
            except Exception:
                # 8k 本地 wav 在部分 SDL_mixer 版本上加载失败，退到 sounddevice
                try:
                    pygame.mixer.music.stop()
                except Exception:
                    pass
        try:
            import soundfile as sf
            import sounddevice as sd
            data, sr = sf.read(str(path), dtype="float32", always_2d=True)
            mono = data[:, 0] * max(0.0, min(1.0, volume))
            t0 = time.time()
            sd.play(mono, samplerate=int(sr), blocking=True)
            return time.time() - t0
        except Exception:
            return 0.0

    def speak(self, text: str, volume: float = 1.0,
              rate: Optional[str] = None) -> dict:
        t0 = time.time()
        path = self.synth(text, rate=rate)
        t_synth = time.time() - t0
        t_play = self.play_file(path, volume=volume)
        return {"synth_sec": t_synth, "play_sec": t_play,
                "total_sec": time.time() - t0, "file": str(path),
                "rate": rate or self.rate, "volume": volume,
                "backend": self.last_backend}

    def stop(self) -> None:
        try:
            import pygame
            if pygame.mixer.get_init():
                pygame.mixer.music.stop()
        except Exception:
            pass


async def list_voices(language_prefix: str = "zh-") -> list[str]:
    import edge_tts
    voices = await edge_tts.list_voices()
    return [v["ShortName"] for v in voices if v.get("ShortName", "").startswith(language_prefix)]
