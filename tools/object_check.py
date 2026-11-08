"""不打开摄像头，检查日常物体检测：图片 -> 检测 -> 标注图。

用法：
    python tools/object_check.py --image data/objects_web/chair/aeron_chair.jpg
    python tools/object_check.py --dir data/objects_web --out robot/web/objects_web.jpg
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.vision import imread_unicode, imwrite_unicode  # noqa: E402
from app.objects import ObjectDetector, object_summary  # noqa: E402


def annotate(img: np.ndarray, dets: list[dict]) -> np.ndarray:
    out = img.copy()
    for d in dets:
        x, y, w, h = d["box"]
        color = (0, 200, 0)
        cv2.rectangle(out, (x, y), (x + w, y + h), color, 2)
        text = f"{d['label']} {d['score']:.2f}"
        cv2.putText(out, text, (x, max(18, y - 6)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 1, cv2.LINE_AA)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", default=None)
    ap.add_argument("--dir", default=None)
    ap.add_argument("--out", default=str(ROOT / "web" / "objects_check.jpg"))
    ap.add_argument("--min-score", type=float, default=0.35)
    args = ap.parse_args()
    paths: list[Path] = []
    if args.image:
        paths = [Path(args.image)]
    elif args.dir:
        paths = sorted(Path(args.dir).rglob("*.jpg")) + sorted(Path(args.dir).rglob("*.png"))
    else:
        ap.error("--image 或 --dir 至少一个")
    det = ObjectDetector(min_score=args.min_score)
    rows = []
    for p in paths:
        img = imread_unicode(p)
        if img is None:
            print(f"跳过（读不出）: {p}")
            continue
        dets = det.detect(img)
        print(f"{p}: {object_summary(dets) or '无检测'}")
        for d in dets:
            print(f"  {d['label']} {d['score']:.2f} {d['box']}")
        rows.append((p, annotate(img, dets)))
    if not rows:
        return 2
    # 简单拼贴，避免一次打开多个窗口
    cell_w = 640
    thumbs = []
    for p, im in rows:
        h, w = im.shape[:2]
        scale = min(1.0, cell_w / max(1, w))
        thumbs.append(cv2.resize(im, (int(w * scale), int(h * scale))))
    out_h = sum(x.shape[0] for x in thumbs)
    out = np.zeros((out_h, cell_w, 3), dtype=np.uint8)
    y = 0
    for im in thumbs:
        out[y:y + im.shape[0], :im.shape[1]] = im
        y += im.shape[0]
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    imwrite_unicode(out_path, out)
    print(f"标注图: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
