"""表情识别推理（先接无依赖基线，不阻塞摄像头接入）。

训练脚本 `tools/train_expression.py` 默认产出 `expression_mlp.npz`：
输入 112x112 对齐脸 → 灰度 32x32 → 单隐层 MLP → 8 类表情。
这个文件只做前向，不训练、不下载、不打开摄像头。

换深度模型时保持同一个 `.npz` 契约即可（W1/b1/W2/b2 + labels + input_size），
`Vision.analyze()` 不需要改。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

from . import config as C


def _softmax(logits: np.ndarray) -> np.ndarray:
    z = logits - np.max(logits, axis=-1, keepdims=True)
    e = np.exp(z)
    return e / np.maximum(e.sum(axis=-1, keepdims=True), 1e-9)


class ExpressionRecognizer:
    """加载 `expression_mlp.npz`，对一张对齐人脸做表情分类。"""

    def __init__(self, model_path: Optional[Path] = None) -> None:
        self.path = Path(model_path) if model_path else C.EXPRESSION_MODEL
        self.labels: list[str] = list(C.EXPRESSION_LABELS)
        self.input_size: int = int(C.EXPRESSION_INPUT)
        self._W1: Optional[np.ndarray] = None
        self._b1: Optional[np.ndarray] = None
        self._W2: Optional[np.ndarray] = None
        self._b2: Optional[np.ndarray] = None
        self._loaded = False
        self._load()

    @property
    def available(self) -> bool:
        return self._loaded

    def _load(self) -> None:
        if self.path.suffix.lower() != ".npz" or not self.path.exists():
            return
        try:
            with np.load(self.path, allow_pickle=False) as z:
                self._W1 = np.asarray(z["W1"], dtype=np.float32)
                self._b1 = np.asarray(z["b1"], dtype=np.float32)
                self._W2 = np.asarray(z["W2"], dtype=np.float32)
                self._b2 = np.asarray(z["b2"], dtype=np.float32)
                if "input_size" in z.files:
                    self.input_size = int(np.asarray(z["input_size"]).reshape(-1)[0])
                if "labels" in z.files:
                    self.labels = [str(x) for x in np.asarray(z["labels"]).tolist()]
            if (self._W1.ndim != 2 or self._W2.ndim != 2
                    or self._W2.shape[1] != len(self.labels)
                    or self._W2.shape[0] != self._W1.shape[1]
                    or self._W1.shape[0] != self.input_size * self.input_size):
                raise ValueError("模型维度与 labels/input_size 不一致")
            self._loaded = True
        except Exception:
            self._loaded = False
            self._W1 = self._b1 = self._W2 = self._b2 = None

    def _features(self, face_bgr: np.ndarray) -> np.ndarray:
        if face_bgr is None or face_bgr.size == 0:
            raise ValueError("空人脸图")
        if face_bgr.ndim == 3:
            gray = cv2.cvtColor(face_bgr, cv2.COLOR_BGR2GRAY)
        else:
            gray = face_bgr
        gray = cv2.resize(gray, (self.input_size, self.input_size),
                          interpolation=cv2.INTER_AREA)
        x = gray.astype(np.float32) / 255.0 - 0.5
        return x.reshape(-1)

    def predict(self, aligned_face_bgr: np.ndarray) -> Optional[dict]:
        """返回 {label, confidence, scores}；模型缺失或图坏了返回 None。"""
        if not self._loaded:
            return None
        try:
            x = self._features(aligned_face_bgr)
            h = np.maximum(x @ self._W1 + self._b1, 0.0)
            probs = _softmax(h @ self._W2 + self._b2)
            idx = int(np.argmax(probs))
            return {
                "label": self.labels[idx],
                "confidence": float(probs[idx]),
                "scores": {name: float(p) for name, p in zip(self.labels, probs)},
            }
        except Exception:
            return None

    def describe(self) -> dict:
        return {
            "path": str(self.path),
            "available": self._loaded,
            "labels": self.labels,
            "input_size": self.input_size,
        }


def selfcheck() -> dict:
    """不打开摄像头，只报告模型文件与推理是否可加载。"""
    er = ExpressionRecognizer()
    return er.describe()


def main() -> int:
    print(json.dumps(selfcheck(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
