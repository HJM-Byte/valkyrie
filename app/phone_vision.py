"""手机摄像头的眼睛：把一张照片交给视觉模型，换回一句可用的观察。

这张照片只在一次请求的内存里经过：不写盘、不进日志、不写记忆。
默认走阿里云百炼的 qwen3-vl-plus（OpenAI 兼容协议），模型名可用
`ROBOT_VISION_MODEL` 覆盖。没有 Key、模型不可用或图片坏掉时直接报错，
不拿假的「看到了」往下传。
"""
from __future__ import annotations

import base64
import time

import requests

from . import config as C
from .llm import _cloud_key


class VisionError(RuntimeError):
    pass


def _image_mime(raw: bytes) -> str:
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


class PhoneVision:
    def __init__(self, model: str = "", timeout: float = 0.0) -> None:
        self.model = (model or C.PHONE_VISION_MODEL).strip()
        self.timeout = float(timeout or C.PHONE_VISION_TIMEOUT)
        self.base = C.CLOUD_BASE.rstrip("/")

    def describe(self, image: bytes, question: str = "") -> dict:
        if not image:
            raise VisionError("没有收到照片")
        if len(image) > C.PHONE_VISION_MAX_BYTES:
            raise VisionError(f"照片太大，最多 {C.PHONE_VISION_MAX_BYTES // (1024 * 1024)}MB")
        key = _cloud_key()
        if not key:
            raise VisionError("没有视觉模型的密钥（DASHSCOPE_API_KEY）")

        q = (question or "").strip()[:500]
        prompt = (
            "你正通过一部手机的摄像头看东西。"
            f"用户的问题或嘱咐：{q or '（没有具体问题，只想让你看看眼前）'}\n"
            "只说你确实看到的：人物、动作、物体、环境、屏幕上的字。"
            "不要猜人物身份，不要补画面外的信息。"
            "先用一到两句直接回答/描述，最多 120 个汉字；看不清就直接说看不清。"
        )
        mime = _image_mime(image)
        b64 = base64.b64encode(image).decode("ascii")
        payload = {
            "model": self.model,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url",
                     "image_url": {"url": f"data:{mime};base64,{b64}"}},
                ],
            }],
            "stream": False,
            "temperature": 0.2,
            "max_tokens": 220,
        }
        t0 = time.time()
        try:
            r = requests.post(
                f"{self.base}/chat/completions",
                headers={"Authorization": f"Bearer {key}",
                         "Content-Type": "application/json"},
                json=payload, timeout=self.timeout,
            )
        except Exception as e:
            raise VisionError(f"看图请求失败: {type(e).__name__}: {e}") from e
        if r.status_code != 200:
            # 带服务端原话，但不要回显 key
            snippet = (r.text or "")[:300].replace(key, "***")
            raise VisionError(f"视觉模型 HTTP {r.status_code}: {snippet}")
        try:
            data = r.json()
            text = (data["choices"][0]["message"]["content"] or "").strip()
        except Exception as e:
            raise VisionError(f"视觉模型返回格式不对: {type(e).__name__}: {e}") from e
        if not text:
            raise VisionError("视觉模型没有返回内容")
        return {"text": text, "model": self.model,
                "seconds": round(time.time() - t0, 2), "mime": mime}
