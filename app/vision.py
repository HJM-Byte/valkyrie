"""视觉：摄像头采集 + 人脸检测（YuNet）+ 人脸识别（SFace）+ 人脸库。

设计约束：
  - 模型路径必须是 ASCII 绝对路径（OpenCV 的 C++ 层不认中文路径）。
  - 摄像头未插时不抛死，`Camera.open()` 返回 False，上层继续跑语音。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

from . import config as C


# --------------------------------------------------------------------------- #
# 中文路径读写
# --------------------------------------------------------------------------- #
def imread_unicode(path: Path | str, flags: int = cv2.IMREAD_COLOR) -> Optional[np.ndarray]:
    """OpenCV 的 imread 在 Windows 上不认中文路径；这里走 Path.read_bytes + imdecode。"""
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    if not data:
        return None
    return cv2.imdecode(np.frombuffer(data, dtype=np.uint8), flags)


def imwrite_unicode(path: Path | str, img: np.ndarray, ext: str = ".jpg",
                    quality: int = 95) -> bool:
    """写图也不走 cv2.imwrite：Path.write_bytes 才能安全处理中文路径。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    params = [cv2.IMWRITE_JPEG_QUALITY, int(quality)] if ext.lower() in (".jpg", ".jpeg") else []
    ok, buf = cv2.imencode(ext, img, params)
    if not ok:
        return False
    p.write_bytes(buf.tobytes())
    return True


# --------------------------------------------------------------------------- #
# 摄像头
# --------------------------------------------------------------------------- #
_BACKENDS = {
    "dshow": cv2.CAP_DSHOW,
    "msmf": cv2.CAP_MSMF,
    "any": cv2.CAP_ANY,
}


class Camera:
    def __init__(self, index: int = C.CAMERA_INDEX, backend: str = C.CAMERA_BACKEND,
                 width: int = C.CAMERA_WIDTH, height: int = C.CAMERA_HEIGHT,
                 fps: int = C.CAMERA_FPS) -> None:
        self.index = index
        self.backend = backend
        self.width = width
        self.height = height
        self.fps = fps
        self._cap: Optional[cv2.VideoCapture] = None

    @property
    def is_open(self) -> bool:
        return self._cap is not None and self._cap.isOpened()

    def open(self, timeout: float = 3.0) -> bool:
        """打开摄像头。带超时：Windows 上 DShow 打开不存在的设备会阻塞十几秒，
        而且卡住的 VideoCapture 线程会互相拖累，所以必须在独立线程里做。"""
        if self.is_open:
            return True
        import threading

        result: list = []

        def _do() -> None:
            try:
                cap = cv2.VideoCapture(self.index, _BACKENDS.get(self.backend, cv2.CAP_ANY))
                if not cap.isOpened():
                    cap.release()
                    result.append(None)
                    return
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
                cap.set(cv2.CAP_PROP_FPS, self.fps)
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                result.append(cap)
            except Exception:
                result.append(None)

        t = threading.Thread(target=_do, daemon=True)
        t.start()
        t.join(timeout)
        if result and result[0] is not None:
            self._cap = result[0]
            return True
        return False

    def read(self) -> Optional[np.ndarray]:
        if not self.is_open:
            return None
        ok, frame = self._cap.read()
        return frame if ok else None

    def close(self) -> None:
        if self._cap is not None:
            self._cap.release()
            self._cap = None

    def __enter__(self) -> "Camera":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def list_cameras(max_index: int = 4, timeout: float = 2.5) -> list[int]:
    """枚举可用的摄像头索引。

    Windows/DShow 打开一个不存在的设备会阻塞十几秒，这里用线程加超时兜住，
    超时即视为无设备——没插摄像头时不能把整个程序卡死。
    """
    import threading

    found: list[int] = []
    for i in range(max_index):
        hit: list[int] = []

        def probe(idx: int = i, out: list[int] = hit) -> None:
            cap = cv2.VideoCapture(idx, cv2.CAP_DSHOW)
            try:
                if cap.isOpened():
                    ok, _ = cap.read()
                    if ok:
                        out.append(idx)
            finally:
                cap.release()

        t = threading.Thread(target=probe, daemon=True)
        t.start()
        t.join(timeout)
        if hit:
            found.append(i)
    return found


# --------------------------------------------------------------------------- #
# 人脸检测 / 识别
# --------------------------------------------------------------------------- #
class FaceDetector:
    """YuNet：输入 BGR，输出 (N,15) 数组——前 4 列是 x,y,w,h，后 10 列是 5 个关键点。"""

    def __init__(self, model_path: Path = C.YUNET_MODEL,
                 w: int = C.DETECT_WIDTH, h: int = C.DETECT_HEIGHT) -> None:
        if not Path(model_path).exists():
            raise FileNotFoundError(f"YuNet 模型缺失: {model_path}")
        self._w, self._h = w, h
        self._det = cv2.FaceDetectorYN.create(
            str(model_path), "", (w, h),
            C.FACE_SCORE_THRESHOLD, C.FACE_NMS_THRESHOLD, C.FACE_TOP_K,
        )

    def detect(self, bgr: np.ndarray) -> np.ndarray:
        h, w = bgr.shape[:2]
        if (w, h) != (self._w, self._h):
            self._det.setInputSize((w, h))
            self._w, self._h = w, h
        _, faces = self._det.detect(bgr)
        if faces is None:
            return np.empty((0, 15), dtype=np.float32)
        return faces


class FaceEmbedder:
    """SFace：把人脸裁正后压成 128 维特征，用于认人。"""

    def __init__(self, model_path: Path = C.SFACE_MODEL) -> None:
        if not Path(model_path).exists():
            raise FileNotFoundError(f"SFace 模型缺失: {model_path}")
        self._rec = cv2.FaceRecognizerSF.create(str(model_path), "")

    def align(self, bgr: np.ndarray, face_row: np.ndarray) -> np.ndarray:
        """对齐裁脸，返回 112x112 BGR。表情模型和 SFace 共用同一张对齐图。"""
        return self._rec.alignCrop(bgr, face_row)

    def embed_aligned(self, aligned: np.ndarray) -> np.ndarray:
        feat = self._rec.feature(aligned)
        return np.asarray(feat, dtype=np.float32).flatten()

    def embed(self, bgr: np.ndarray, face_row: np.ndarray) -> np.ndarray:
        return self.embed_aligned(self.align(bgr, face_row))

    @staticmethod
    def score(a: np.ndarray, b: np.ndarray) -> float:
        """余弦相似度，SFace 官方阈值 0.363。"""
        a = a.flatten().astype(np.float32)
        b = b.flatten().astype(np.float32)
        na, nb = np.linalg.norm(a), np.linalg.norm(b)
        if na == 0 or nb == 0:
            return 0.0
        return float(np.dot(a, b) / (na * nb))


class FaceDB:
    """极简人脸库：JSON 存 姓名 -> 特征向量。实际用起来再换 sqlite。"""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path else (C.DATA / "faces.json")
        self.entries: dict[str, np.ndarray] = {}
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self.entries = {k: np.array(v, dtype=np.float32) for k, v in raw.items()}
        except Exception:
            self.entries = {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        raw = {k: v.tolist() for k, v in self.entries.items()}
        self.path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    def enroll(self, name: str, feat: np.ndarray) -> None:
        self.entries[name] = feat.astype(np.float32).flatten()
        self.save()

    def match(self, feat: np.ndarray,
              threshold: float = C.FACE_MATCH_THRESHOLD) -> tuple[Optional[str], float]:
        best_name, best_score = None, -1.0
        for name, ref in self.entries.items():
            s = FaceEmbedder.score(feat, ref)
            if s > best_score:
                best_name, best_score = name, s
        if best_name is not None and best_score >= threshold:
            return best_name, best_score
        return None, best_score


# --------------------------------------------------------------------------- #
# 高层封装：一帧进，一张人脸表出
# --------------------------------------------------------------------------- #
class Vision:
    def __init__(self, camera: Optional[Camera] = None,
                 detector: Optional[FaceDetector] = None,
                 embedder: Optional[FaceEmbedder] = None,
                 db: Optional[FaceDB] = None) -> None:
        self.camera = camera or Camera()
        self.detector = detector or FaceDetector()
        self.embedder = embedder or FaceEmbedder()
        self.db = db or FaceDB()
        self._last_gray: Optional[np.ndarray] = None
        self._objects = None
        self.expression = None
        try:
            from .expression import ExpressionRecognizer
            er = ExpressionRecognizer()
            if er.available:
                self.expression = er
        except Exception:
            self.expression = None

    def grab(self) -> Optional[np.ndarray]:
        return self.camera.read()

    def analyze(self, bgr: np.ndarray) -> list[dict]:
        """返回 [{'box':(x,y,w,h), 'score':float, 'name':str|None, 'match':float}]"""
        faces = self.detector.detect(bgr)
        out = []
        for row in faces:
            x, y, w, h = [float(v) for v in row[:4]]
            res = {"box": (int(x), int(y), int(w), int(h)),
                   "score": float(row[-1]), "name": None, "match": -1.0}
            try:
                aligned = self.embedder.align(bgr, row)
                feat = self.embedder.embed_aligned(aligned)
                name, sc = self.db.match(feat)
                res["name"], res["match"] = name, sc
                res["_feat"] = feat
                if self.expression is not None:
                    got = self.expression.predict(aligned)
                    if got:
                        res["expression"] = got
            except Exception:
                pass
            out.append(res)
        return out

    def detect_objects(self, bgr: np.ndarray) -> list[dict]:
        """日常物体检测；模型加载失败时返回空表，不影响人脸链路。"""
        if self._objects is None:
            try:
                from .objects import ObjectDetector
                self._objects = ObjectDetector()
            except Exception:
                self._objects = False
        if not self._objects:
            return []
        try:
            return self._objects.detect(bgr)
        except Exception:
            return []

    def analyze_scene(self, bgr: np.ndarray) -> dict:
        """一帧同时返回 faces 与 objects，给网页/对话层留统一入口。"""
        return {"faces": self.analyze(bgr), "objects": self.detect_objects(bgr)}


def selfcheck() -> dict:
    """不依赖摄像头，检查模型文件与 OpenCV 能力。"""
    info = {
        "cv2": cv2.__version__,
        "yunet_model": str(C.YUNET_MODEL),
        "yunet_exists": Path(C.YUNET_MODEL).exists(),
        "sface_model": str(C.SFACE_MODEL),
        "sface_exists": Path(C.SFACE_MODEL).exists(),
    }
    try:
        FaceDetector()
        info["yunet_load"] = "ok"
    except Exception as e:
        info["yunet_load"] = f"{type(e).__name__}: {e}"
    try:
        FaceEmbedder()
        info["sface_load"] = "ok"
    except Exception as e:
        info["sface_load"] = f"{type(e).__name__}: {e}"
    return info
