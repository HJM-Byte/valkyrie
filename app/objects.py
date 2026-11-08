"""日常物体检测：YOLOv8n/s ONNX（COCO 80 类），不依赖 torch。

模型来自公开预训练权重；本模块只做推理，不训练、不改表情模型。
输入 BGR（OpenCV 习惯），输出统一的 {label, name_zh, score, box}。
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import cv2
import numpy as np

from . import config as C

# COCO 2017 的 80 类，顺序必须与模型输出一致。
COCO_NAMES = [
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck",
    "boat", "traffic light", "fire hydrant", "stop sign", "parking meter", "bench",
    "bird", "cat", "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra",
    "giraffe", "backpack", "umbrella", "handbag", "tie", "suitcase", "frisbee",
    "skis", "snowboard", "sports ball", "kite", "baseball bat", "baseball glove",
    "skateboard", "surfboard", "tennis racket", "bottle", "wine glass", "cup",
    "fork", "knife", "spoon", "bowl", "banana", "apple", "sandwich", "orange",
    "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair", "couch",
    "potted plant", "bed", "dining table", "toilet", "tv", "laptop", "mouse",
    "remote", "keyboard", "cell phone", "microwave", "oven", "toaster", "sink",
    "refrigerator", "book", "clock", "vase", "scissors", "teddy bear",
    "hair drier", "toothbrush",
]

ZH = {
    "person": "人", "bicycle": "自行车", "car": "汽车", "motorcycle": "摩托车",
    "airplane": "飞机", "bus": "公交车", "train": "火车", "truck": "卡车",
    "boat": "船", "traffic light": "交通灯", "fire hydrant": "消防栓",
    "stop sign": "停止标志", "parking meter": "停车计时器", "bench": "长椅",
    "bird": "鸟", "cat": "猫", "dog": "狗", "horse": "马", "sheep": "羊",
    "cow": "牛", "elephant": "大象", "bear": "熊", "zebra": "斑马",
    "giraffe": "长颈鹿", "backpack": "背包", "umbrella": "雨伞", "handbag": "手提包",
    "tie": "领带", "suitcase": "行李箱", "frisbee": "飞盘", "skis": "滑雪板",
    "snowboard": "单板滑雪板", "sports ball": "球", "kite": "风筝",
    "baseball bat": "棒球棒", "baseball glove": "棒球手套", "skateboard": "滑板",
    "surfboard": "冲浪板", "tennis racket": "网球拍", "bottle": "瓶子",
    "wine glass": "酒杯", "cup": "杯子", "fork": "叉子", "knife": "刀",
    "spoon": "勺子", "bowl": "碗", "banana": "香蕉", "apple": "苹果",
    "sandwich": "三明治", "orange": "橙子", "broccoli": "西兰花",
    "carrot": "胡萝卜", "hot dog": "热狗", "pizza": "披萨", "donut": "甜甜圈",
    "cake": "蛋糕", "chair": "椅子", "couch": "沙发", "potted plant": "盆栽",
    "bed": "床", "dining table": "餐桌", "toilet": "马桶", "tv": "电视",
    "laptop": "笔记本电脑", "mouse": "鼠标", "remote": "遥控器",
    "keyboard": "键盘", "cell phone": "手机", "microwave": "微波炉",
    "oven": "烤箱", "toaster": "烤面包机", "sink": "水槽",
    "refrigerator": "冰箱", "book": "书", "clock": "时钟", "vase": "花瓶",
    "scissors": "剪刀", "teddy bear": "泰迪熊", "hair drier": "吹风机",
    "toothbrush": "牙刷",
}

# 凳子不是 COCO 独立类；检测到 chair 时把它作为可能的细分类提示。
ALIASES = {"chair": ("椅子", "凳子")}


class ObjectDetector:
    """YOLOv8 ONNX 推理器。模型路径在 config.OBJECT_MODEL。"""

    def __init__(self, model_path: Path | str = C.OBJECT_MODEL,
                 min_score: float = C.OBJECT_MIN_SCORE,
                 iou: float = C.OBJECT_NMS_IOU) -> None:
        path = Path(model_path)
        if not path.exists():
            raise FileNotFoundError(f"物体模型缺失: {path}")
        import onnxruntime as ort
        self._session = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
        inp = self._session.get_inputs()[0]
        self.input_name = inp.name
        h, w = int(inp.shape[2]), int(inp.shape[3])
        self.input_size = (w, h)
        self.min_score = float(min_score)
        self.iou = float(iou)

    @staticmethod
    def _letterbox(bgr: np.ndarray, size: tuple[int, int]) -> tuple[np.ndarray, float, int, int]:
        h, w = bgr.shape[:2]
        tw, th = size
        r = min(tw / w, th / h)
        nw, nh = int(round(w * r)), int(round(h * r))
        resized = cv2.resize(bgr, (nw, nh), interpolation=cv2.INTER_LINEAR)
        canvas = np.full((th, tw, 3), 114, dtype=np.uint8)
        dw, dh = (tw - nw) // 2, (th - nh) // 2
        canvas[dh:dh + nh, dw:dw + nw] = resized
        return canvas, r, dw, dh

    def detect(self, bgr: np.ndarray) -> list[dict]:
        if bgr is None or bgr.size == 0:
            return []
        canvas, r, dw, dh = self._letterbox(bgr, self.input_size)
        blob = cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        blob = np.transpose(blob, (2, 0, 1))[None]
        out = self._session.run(None, {self.input_name: blob})[0]
        # YOLOv8: [1, 4+classes, N]
        pred = np.asarray(out, dtype=np.float32)
        if pred.ndim != 3:
            return []
        pred = pred[0].T  # [N, 4+classes]
        if pred.shape[1] < 5:
            return []
        boxes_cxcywh = pred[:, :4]
        scores = pred[:, 4:]
        class_ids = scores.argmax(axis=1)
        conf = scores.max(axis=1)
        keep = conf >= self.min_score
        if not np.any(keep):
            return []
        b = boxes_cxcywh[keep]
        c = conf[keep]
        ids = class_ids[keep]
        # 还原到原图
        x = (b[:, 0] - b[:, 2] / 2 - dw) / max(1e-6, r)
        y = (b[:, 1] - b[:, 3] / 2 - dh) / max(1e-6, r)
        w = b[:, 2] / max(1e-6, r)
        h = b[:, 3] / max(1e-6, r)
        rects = [[float(v) for v in xywh] for xywh in zip(x, y, w, h)]
        idx = cv2.dnn.NMSBoxes(rects, [float(v) for v in c], self.min_score, self.iou)
        if idx is None or len(idx) == 0:
            return []
        result = []
        h_img, w_img = bgr.shape[:2]
        for i in np.asarray(idx).reshape(-1):
            i = int(i)
            x1 = int(max(0, min(w_img - 1, x[i])))
            y1 = int(max(0, min(h_img - 1, y[i])))
            x2 = int(max(0, min(w_img, x[i] + w[i])))
            y2 = int(max(0, min(h_img, y[i] + h[i])))
            if x2 <= x1 or y2 <= y1:
                continue
            cid = int(ids[i])
            en = COCO_NAMES[cid] if 0 <= cid < len(COCO_NAMES) else str(cid)
            result.append({
                "label": en,
                "name_zh": ZH.get(en, en),
                "aliases": list(ALIASES.get(en, ())),
                "score": round(float(c[i]), 4),
                "box": (x1, y1, x2 - x1, y2 - y1),
            })
        result.sort(key=lambda z: z["score"], reverse=True)
        return result

    def close(self) -> None:
        self._session = None


def object_summary(detections: list[dict], max_items: int = 4) -> str:
    """把检测结果压成一句人话；没有就返回空串。"""
    if not detections:
        return ""
    parts = []
    for d in detections[:max_items]:
        name = d.get("name_zh") or d.get("label")
        alias = d.get("aliases") or []
        if alias:
            name = "/".join([name] + [a for a in alias if a != name])
        parts.append(f"{name}{round(float(d.get('score', 0)) * 100)}%")
    return "、".join(parts)
