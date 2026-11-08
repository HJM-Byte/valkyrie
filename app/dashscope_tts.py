"""百炼 TTS 兜底：Breeze 的 Cloudflare 线路不可达时，别让手机等 55 秒。

两条路：
1. `qwen-audio-3.1-tts-plus` + 声音复刻音色：用寒蝉余音/幽兰影的样本在百炼
   建的自定义音色，优先保声线；
2. `qwen3-tts-flash` + 内置 Cherry/Serena：复刻音色不可用时顶住，绝不静默
   回本地通用声。

两者都返回一个 OSS 音频 URL，下载后落成 mp3。Key 只从环境变量或
robot/.env 读，不写代码、不打印。
"""
from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

from . import config as C


class DashScopeTTSError(RuntimeError):
    pass


def _read_key() -> str:
    key = (os.environ.get(C.DASHSCOPE_KEY_ENV, "") or "").strip()
    if key:
        return key
    p = Path(C.PROJECT) / ".env"
    try:
        for line in p.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            if name.strip() == C.DASHSCOPE_KEY_ENV:
                return value.strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


class DashScopeTTS:
    def __init__(self, voice_id: str) -> None:
        self.voice_id = voice_id
        self.key = _read_key()
        self.clone_voice = C.QWEN_TTS_CLONE_VOICES.get(voice_id, "")
        self.builtin_voice = C.QWEN_TTS_BUILTIN_VOICES.get(voice_id, "Cherry")
        # 复刻音色第一次调用可能冷启动 20 秒；后台预热成功后才让它接手机。
        self.clone_ready = False
        self.last_backend = ""

    def available(self) -> bool:
        return bool(self.key)

    def warm(self) -> bool:
        """预热复刻音色；只发一个短音，成功后才让手机走 clone。"""
        if not self.available() or not self.clone_voice:
            return False
        out = Path(C.DATA) / "_tmp" / (
            "qwen_warm_" + self.voice_id.replace("/", "_") + ".mp3")
        try:
            self._speech_synth("嗯", out, C.QWEN_TTS_CLONE_MODEL,
                               self.clone_voice, C.QWEN_TTS_TIMEOUT)
            self.clone_ready = True
            return True
        except Exception:
            self.clone_ready = False
            return False
        finally:
            try:
                out.unlink(missing_ok=True)
            except OSError:
                pass

    def _post(self, url: str, body: dict, timeout: float) -> dict:
        req = urllib.request.Request(
            url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.key}",
                "Content-Type": "application/json",
                "User-Agent": "valkyrie-voice/2.1",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            detail = e.read(4096).decode("utf-8", "replace")
            raise DashScopeTTSError(
                f"HTTP {e.code}: {detail[:300]}") from e
        except Exception as e:
            raise DashScopeTTSError(
                f"{type(e).__name__}: {str(e)[:200]}") from e
        try:
            obj = json.loads(raw.decode("utf-8", "replace"))
        except Exception as e:
            raise DashScopeTTSError(
                f"非 JSON 响应: {raw[:200]!r}") from e
        if obj.get("code"):
            raise DashScopeTTSError(
                f"{obj.get('code')}: {obj.get('message') or ''}"[:400])
        return obj

    @staticmethod
    def _write_audio(obj: dict, out: Path, timeout: float) -> None:
        audio = ((obj.get("output") or {}).get("audio") or {})
        url = str(audio.get("url") or "").strip()
        data = str(audio.get("data") or "").strip()
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(out.suffix + ".tmp")
        try:
            if url:
                req = urllib.request.Request(
                    url, headers={"User-Agent": "valkyrie-voice/2.1"})
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    raw = resp.read()
            elif data:
                raw = base64.b64decode(data + "=" * ((4 - len(data) % 4) % 4))
            else:
                raise DashScopeTTSError("响应没有音频 URL 或 data")
            if not raw:
                raise DashScopeTTSError("音频为空")
            tmp.write_bytes(raw)
            tmp.replace(out)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def _speech_synth(self, text: str, out: Path, model: str,
                      voice: str, timeout: float) -> None:
        body = {
            "model": model,
            "input": {
                "text": text,
                "voice": voice,
                "format": "mp3",
                "sample_rate": 24000,
            },
        }
        obj = self._post(
            "https://dashscope.aliyuncs.com/api/v1/services/audio/tts/"
            "SpeechSynthesizer",
            body, timeout)
        self._write_audio(obj, out, timeout)

    def _qwen3_synth(self, text: str, out: Path, voice: str,
                     timeout: float) -> None:
        body = {
            "model": C.QWEN_TTS_BUILTIN_MODEL,
            "input": {
                "text": text,
                "voice": voice,
                "language_type": "Chinese",
            },
        }
        obj = self._post(
            "https://dashscope.aliyuncs.com/api/v1/services/aigc/"
            "multimodal-generation/generation",
            body, timeout)
        self._write_audio(obj, out, timeout)

    def synthesize(self, text: str, out: Path,
                   timeout: Optional[float] = None) -> Path:
        if not self.available():
            raise DashScopeTTSError(f"未设置 {C.DASHSCOPE_KEY_ENV}")
        timeout = float(timeout or C.QWEN_TTS_TIMEOUT)
        errors: list[str] = []
        if self.clone_voice and self.clone_ready:
            try:
                self._speech_synth(text, out, C.QWEN_TTS_CLONE_MODEL,
                                   self.clone_voice, timeout)
                self.last_backend = "qwen_clone"
                return out
            except Exception as e:
                self.clone_ready = False
                errors.append(f"clone: {type(e).__name__}: {str(e)[:180]}")
                try:
                    out.unlink(missing_ok=True)
                except OSError:
                    pass
        try:
            self._qwen3_synth(text, out, self.builtin_voice, timeout)
            self.last_backend = "qwen"
            return out
        except Exception as e:
            errors.append(f"qwen3: {type(e).__name__}: {str(e)[:180]}")
            try:
                out.unlink(missing_ok=True)
            except OSError:
                pass
        raise DashScopeTTSError("百炼 TTS 失败: " + " | ".join(errors[-2:]))
