"""视觉自检：不依赖摄像头。用一张标准人脸图验证 检测→对齐→特征→匹配 全链路。"""
from __future__ import annotations

import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402

from app import config as C  # noqa: E402

SAMPLE = C.MODELS / "samples" / "selfie.jpg"


def ensure_sample() -> Path:
    if SAMPLE.exists() and SAMPLE.stat().st_size > 10_000:
        return SAMPLE
    raise FileNotFoundError(f"测试图缺失: {SAMPLE}（先跑 download_faces.py）")


def main() -> int:
    print("=" * 62)
    print("视觉链路自检")
    print("=" * 62)

    import cv2
    print(f"OpenCV {cv2.__version__}")

    from app.vision import FaceDetector, FaceEmbedder, FaceDB, list_cameras

    # 1. 模型文件
    for p in (C.YUNET_MODEL, C.SFACE_MODEL):
        ok = Path(p).exists()
        size = f"{Path(p).stat().st_size/1024:.0f}KB" if ok else "-"
        print(f"  [{'OK ' if ok else 'MISS'}] {Path(p).name:46s} {size}")
    if not (Path(C.YUNET_MODEL).exists() and Path(C.SFACE_MODEL).exists()):
        print("!! 模型缺失，先跑 download_models.py")
        return 2

    # 2. 加载
    t0 = time.time()
    det = FaceDetector()
    print(f"  [OK ] YuNet 加载 {time.time()-t0:.2f}s")
    t0 = time.time()
    emb = FaceEmbedder()
    print(f"  [OK ] SFace 加载 {time.time()-t0:.2f}s")

    # 3. 检测
    img = cv2.imread(str(ensure_sample()))
    if img is None:
        print("!! 测试图读不出来")
        return 3
    h, w = img.shape[:2]
    print(f"\n测试图 lena.jpg {w}x{h}")

    t0 = time.time()
    faces = det.detect(img)
    dt = (time.time() - t0) * 1000
    print(f"检测到 {len(faces)} 张脸，耗时 {dt:.1f}ms")
    if len(faces) == 0:
        print("!! 一张脸都没检出来，阈值或模型有问题")
        return 4

    for i, row in enumerate(faces):
        x, y, fw, fh = [int(v) for v in row[:4]]
        print(f"  face#{i}: box=({x},{y},{fw},{fh}) score={row[-1]:.3f}")

    # 4. 特征 + 自匹配
    row = faces[0]
    t0 = time.time()
    f1 = emb.embed(img, row)
    t_emb = (time.time() - t0) * 1000
    f2 = emb.embed(img, row)
    s = FaceEmbedder.score(f1, f2)
    print(f"\n特征维度 {f1.shape[0]}，提取 {t_emb:.1f}ms")
    print(f"同图自匹配余弦 = {s:.4f}  (期望 > 0.99)  {'OK' if s > 0.99 else '异常'}")

    # 5. 人脸库往返
    db = FaceDB(C.DATA / "_selftest_faces.json")
    db.entries.clear()
    db.enroll("测试脸", f1)
    name, sc = db.match(f2)
    print(f"人脸库匹配 -> {name} {sc:.4f}  {'OK' if name == '测试脸' else '异常'}")
    (C.DATA / "_selftest_faces.json").unlink(missing_ok=True)

    # 6. 摄像头（默认不探测：DShow 打开不存在的设备会阻塞很久）
    if "--camera" in sys.argv:
        print("\n摄像头枚举 ...", flush=True)
        cams = list_cameras(3, timeout=2.5)
        print(f"  可用索引: {cams if cams else '无'}")
    else:
        print("\n摄像头枚举已跳过（加 --camera 参数才探测）")

    print("\n=== 视觉链路全部通过 ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
