"""采集日常物体照片（手机/椅子/凳子）：保存整帧，供物体分类与验收。

用法：
    C:\\robot\\venv\\Scripts\\python.exe tools\\collect_object_data.py ^
        --allow-camera --session 20261029_phone_a --label 手机 --per-label 30 --seconds 20
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import cv2

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config as C  # noqa: E402
from app.objects import ObjectDetector  # noqa: E402
from app.vision import Camera, imwrite_unicode  # noqa: E402
from tools.collect_expression_set import (  # noqa: E402
    _countdown, _preview, _show, _warmup,
)
from tools.collect_face_data import _now_iso, append_index  # noqa: E402

EXPECTED = {
    "手机": ("cell phone",),
    "椅子": ("chair",),
    "凳子": ("chair",),
}


def _brightness(frame) -> float:
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    return float(gray.mean())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="collect_object_data",
                                 description="采集日常物体整帧照片，默认不打开摄像头")
    ap.add_argument("--allow-camera", action="store_true")
    ap.add_argument("--session", default=None)
    ap.add_argument("--label", default="手机")
    ap.add_argument("--per-label", type=int, default=30)
    ap.add_argument("--seconds", type=float, default=20.0)
    ap.add_argument("--prep", type=float, default=5.0)
    ap.add_argument("--every", type=int, default=10,
                    help="每 N 帧存一张（默认 10，约 3 fps）")
    ap.add_argument("--camera-index", type=int, default=C.CAMERA_INDEX)
    ap.add_argument("--backend", default=C.CAMERA_BACKEND,
                    choices=["dshow", "msmf", "any"])
    ap.add_argument("--data-dir", default=str(C.DATA / "objects_local"))
    ap.add_argument("--min-object-score", type=float, default=0.25,
                    help="YOLO 检测到目标类才保存（防止拍成人像）")
    ap.add_argument("--skip-detection", action="store_true",
                    help="不做目标检测门槛，只按亮度存")
    args = ap.parse_args(argv)

    if C.VISION_LOCKED and not args.allow_camera:
        print("视觉仍锁定（VISION_LOCKED=True）。确认需要采集时加 --allow-camera。")
        return 2

    session = args.session or time.strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.data_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    index_path = out_dir / "objects_index.jsonl"
    label_dir = out_dir / "images" / args.label
    label_dir.mkdir(parents=True, exist_ok=True)

    cam = Camera(index=args.camera_index, backend=args.backend)
    warm = 0
    for attempt in range(1, 3):
        if cam.is_open:
            cam.close()
        if not cam.open(timeout=5.0):
            print(f"摄像头打不开（index={args.camera_index}, backend={args.backend}）。")
            return 3
        warm = _warmup(cam, timeout=6.0)
        if warm:
            break
        print(f"第 {attempt} 次预热读不到帧，重开摄像头……", flush=True)
        cam.close()
        time.sleep(1.5)
    if warm == 0:
        print("摄像头多次重开仍读不到帧，拔插 USB 后再试。")
        cam.close()
        return 3
    print(f"摄像头预热完成：读 {warm} 帧", flush=True)
    det = None
    if not args.skip_detection and args.label in EXPECTED:
        det = ObjectDetector(min_score=args.min_object_score)
        print(f"检测门槛：只存画面里能检出 {args.label} 的帧", flush=True)

    _preview()
    if not _countdown(cam, args.prep,
                      f"{args.label}：拿稳，正面/背面/侧面都露出来"):
        cam.close()
        return 0

    saved = rejected = seen = seq = 0
    started = time.time()
    try:
        while time.time() - started < args.seconds and saved < args.per_label:
            frame = cam.read()
            if frame is None:
                time.sleep(0.01)
                continue
            seen += 1
            if seen % max(1, args.every) != 0:
                continue
            if det is not None:
                ds = det.detect(frame)
                wanted = EXPECTED[args.label]
                hit = any(str(d.get("label")) in wanted for d in ds)
                if not hit:
                    rejected += 1
                    status = f"没检测到{args.label}，靠近镜头"
                    key = _show(frame, [
                        f"{args.label}  {saved}/{args.per_label}",
                        "把物体举到画面中间，占一半以上",
                        status,
                        "q/ESC = 停止",
                    ], (0, 200, 255))
                    if key in (27, ord("q")):
                        print("用户中止。")
                        break
                    continue
            b = _brightness(frame)
            if b < 20 or b > 238:
                rejected += 1
                status = f"reject 亮度 {b:.0f}"
            else:
                seq += 1
                path = label_dir / f"{session}_{seq:03d}.jpg"
                if not imwrite_unicode(path, frame):
                    print(f"写图失败: {path}")
                    break
                append_index(index_path, {
                    "file": str(path.relative_to(out_dir)),
                    "label": args.label,
                    "session": session,
                    "source": "camera_object",
                    "ts": _now_iso(),
                    "brightness": round(b, 1),
                    "shape": list(frame.shape[:2]),
                })
                saved += 1
                status = f"saved={saved} 亮度={b:.0f}"
            key = _show(frame, [
                f"{args.label}  {saved}/{args.per_label}",
                "缓慢转动 正面 / 背面 / 侧面",
                status,
                "q/ESC = 停止",
            ], (0, 255, 0))
            if key in (27, ord("q")):
                print("用户中止。")
                break
    finally:
        cam.close()
        if det is not None:
            det.close()
        try:
            cv2.destroyWindow("collect_expression_set")
        except Exception:
            pass

    print(f"\n=== 汇总 ===\n  标签: {args.label}\n  存: {saved}  拒绝: {rejected}  读帧: {seen}")
    print(f"  session={session}")
    if saved == 0:
        print("一张都没存：检查光线、镜头和物体是否在画面内。")
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
