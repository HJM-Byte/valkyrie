"""语音识别：faster-whisper（CTranslate2，int8，CPU 可跑）。

模型目录 models\\faster-whisper-small。首次加载约 1-3 秒，之后常驻。
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

import numpy as np

from . import config as C


class STT:
    # whisper 对静音/噪声会编出训练语料里的字幕文本，必须挡掉
    HALLUCINATION_PATTERNS = (
        "字幕", "字幕组", "订阅", "点赞", "感谢观看", "谢谢观看",
        "明镜与点点", "amara.org", "please subscribe", "thank you for watching",
        "中文字幕", "翻译", "校对",
    )

    def __init__(self, model_dir: Path = C.WHISPER_DIR,
                 device: str = "cpu", compute_type: str = C.WHISPER_COMPUTE,
                 language: str = C.WHISPER_LANG) -> None:
        from faster_whisper import WhisperModel

        if not Path(model_dir).exists():
            raise FileNotFoundError(f"whisper 模型缺失: {model_dir}")
        self.model_dir = Path(model_dir)
        self.language = language
        t0 = time.time()
        self.model = WhisperModel(str(model_dir), device=device, compute_type=compute_type)
        self.load_seconds = time.time() - t0
        self.last_seconds = 0.0

    def _is_hallucination(self, text: str) -> bool:
        low = text.lower()
        return any(p.lower() in low for p in self.HALLUCINATION_PATTERNS)

    def transcribe(self, pcm16: np.ndarray, language: Optional[str] = None,
                   beam_size: int = C.WHISPER_BEAM) -> dict:
        audio = (pcm16.astype(np.float32) / 32768.0).clip(-1.0, 1.0)

        # 太安静直接判空，别让模型去编
        rms = float(np.sqrt(np.mean(audio ** 2))) if audio.size else 0.0
        if rms < 0.004:
            self.last_seconds = 0.0
            return {"text": "", "lang": language or self.language, "lang_prob": 0.0,
                    "seconds": 0.0, "audio_sec": len(pcm16) / C.SAMPLE_RATE,
                    "filtered": "silence"}

        t0 = time.time()
        segments, info = self.model.transcribe(
            audio,
            language=language or self.language,
            beam_size=beam_size,
            vad_filter=False,        # 上游已经做过 VAD，这里不再切
            condition_on_previous_text=False,
            no_speech_threshold=0.6,
            log_prob_threshold=-1.0,
        )
        text = "".join(seg.text for seg in segments).strip()
        self.last_seconds = time.time() - t0

        filtered = None
        if text and self._is_hallucination(text):
            filtered = "hallucination"
            text = ""

        return {
            "text": text,
            "lang": info.language,
            "lang_prob": float(info.language_probability),
            "seconds": self.last_seconds,
            "audio_sec": len(pcm16) / C.SAMPLE_RATE,
            "filtered": filtered,
        }
