"""一次引导采集 8 类表情：摄像头只开一次，按顺序提示，逐类存对齐人脸。

这是 `collect_face_data.py` 的“连续版”：适合人坐在镜头前一次拍完一轮，
不用为每个标签重新开摄像头。数据格式、索引、质量门槛完全沿用单类采集。

安全边界：
  - 默认拒绝打开摄像头；显式 `--allow-camera` 才采集；
  - 不改 `VISION_LOCKED`，不写她的记忆，不叫醒她；
  - 只保存对齐后的 112x112 人脸小图，不保存整帧；
  - 按 session 分组，训练时同一 session 不会同时进训练/验证。

用法：
    C:\\robot\\venv\\Scripts\\python.exe tools\\collect_expression_set.py --guide
    C:\\robot\\venv\\Scripts\\python.exe tools\\collect_expression_set.py ^
        --allow-camera --session 20261029_light1 --per-label 40 --seconds 12
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config as C  # noqa: E402
from app.vision import Camera, FaceDetector, FaceEmbedder  # noqa: E402
from tools.collect_face_data import (  # noqa: E402
    _now_iso, _quality, accept_quality, append_index, pick_face, save_crop,
)

# OpenCV putText 不能可靠显示中文；窗口里用 ASCII，控制台保留中文。
ASCII = {
    "中性": "NEUTRAL",
    "开心": "HAPPY",
    "难过": "SAD",
    "生气": "ANGRY",
    "惊讶": "SURPRISED",
    "害怕": "AFRAID",
    "厌恶": "DISGUST",
    "疲惫": "TIRED",
}

# OpenCV 的 putText 不能写中文；窗口提示用 Pillow 画。
try:
    _FONT = ImageFont.truetype(r"C:\Windows\Fonts\msyh.ttc", 28)
except Exception:
    _FONT = None


def print_guide() -> None:
    print("""一轮连续采集（约 2–3 分钟）：
  1. 灯在脸前方，不要背光；距离摄像头 40–70cm；把 C920 的镜头盖打开。
  2. 窗口会先倒数 3 秒，然后显示要做的表情；保持自然，不要挤眉弄眼。
  3. 每个表情做 2–3 组，组间回中性 2 秒；窗口显示 saved 计数。
  4. 按 q 或 Esc 随时中止；已保存的数据保留。
  5. 只存对齐人脸小图，不存整帧；换光线/换人请换一个 --session。
  6. 至少留一个完整 session 不参与训练，做验收。

标签顺序：""" + "、".join(C.EXPRESSION_LABELS))


def _preview(size: tuple[int, int] = (960, 540)) -> None:
    try:
        cv2.namedWindow("collect_expression_set", cv2.WINDOW_NORMAL)
        cv2.resizeWindow("collect_expression_set", *size)
    except Exception:
        pass


def _show(frame: np.ndarray, lines: list[str], color=(0, 255, 0)) -> int:
    """在预览窗口写几行中文提示，返回按键。"""
    show = cv2.resize(frame, (960, 540))
    if _FONT is not None:
        rgb = cv2.cvtColor(show, cv2.COLOR_BGR2RGB)
        img = Image.fromarray(rgb)
        draw = ImageDraw.Draw(img)
        y = 18
        for line in lines:
            draw.text((18, y), line, font=_FONT,
                      fill=(color[2], color[1], color[0]))
            y += 38
        show = cv2.cvtColor(np.asarray(img), cv2.COLOR_RGB2BGR)
    else:
        y = 34
        for line in lines:
            cv2.putText(show, line, (14, y), cv2.FONT_HERSHEY_SIMPLEX, 0.85,
                        color, 2, cv2.LINE_AA)
            y += 34
    try:
        cv2.imshow("collect_expression_set", show)
        return cv2.waitKey(1) & 0xFF
    except Exception:
        return 255


def _countdown(cam: Camera, seconds: int, label: str) -> bool:
    """倒数；返回 False 表示用户中止。"""
    end = time.time() + max(1, seconds)
    while time.time() < end:
        frame = cam.read()
        if frame is None:
            time.sleep(0.01)
            continue
        left = max(0, int(round(end - time.time())))
        key = _show(frame, [f"准备：{label}", f"{left} 秒后开始",
                            "q/ESC = 停止"], (0, 220, 255))
        if key in (27, ord("q")):
            return False
    return True


def _relax(cam: Camera, seconds: float = 1.5) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        frame = cam.read()
        if frame is None:
            time.sleep(0.01)
            continue
        key = _show(frame, ["放松一下", "回到中性表情"])
        if key in (27, ord("q")):
            return False
    return True


def _warmup(cam: Camera, wanted: int = 20, timeout: float = 8.0) -> int:
    """刚打开/上一进程刚退出时，MSMF 可能连续返回 None；先读几帧再开始采集。"""
    start = time.time()
    got = 0
    while got < wanted and time.time() - start < timeout:
        frame = cam.read()
        if frame is not None:
            got += 1
        else:
            time.sleep(0.03)
    return got


def capture_set(args: argparse.Namespace) -> int:
    if C.VISION_LOCKED and not args.allow_camera:
        print("视觉仍锁定（VISION_LOCKED=True）。")
        print("确认是新 C920 后，加 --allow-camera；这个参数不会改 config.py。")
        return 2
    labels = [x.strip() for x in args.labels.split(",") if x.strip()]
    bad = [x for x in labels if x not in C.EXPRESSION_LABELS]
    if bad:
        print(f"非法标签：{bad}；可选：{'、'.join(C.EXPRESSION_LABELS)}")
        return 2
    if not labels:
        print("没有标签。")
        return 2

    out_dir = Path(args.data_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    index_path = out_dir / "faces_index.jsonl"
    session = args.session or time.strftime("%Y%m%d_%H%M%S")
    cam = Camera(index=args.camera_index, backend=args.backend)
    warm = 0
    for attempt in range(1, 3):
        if cam.is_open:
            cam.close()
        if not cam.open(timeout=5.0):
            print(f"摄像头打不开（index={args.camera_index}, backend={args.backend}）。")
            print("先跑 tools/vision_preflight.py --camera --index 0，确认 msmf 可用。")
            return 3
        warm = _warmup(cam, timeout=6.0)
        if warm:
            break
        print(f"第 {attempt} 次预热读不到帧，重开摄像头……", flush=True)
        cam.close()
        time.sleep(1.5)
    if warm == 0:
        print("摄像头已打开，但多次重开仍读不到任何帧。")
        print("拔插一次 USB，再跑 preflight --camera；仍失败就换一个 USB 口。")
        cam.close()
        return 3
    print(f"摄像头预热完成：读 {warm} 帧", flush=True)

    det = FaceDetector()
    emb = FaceEmbedder()
    preview = not args.no_preview
    if preview:
        _preview()
    total = 0
    counts: dict[str, int] = {}
    started = time.time()
    try:
        for i, label in enumerate(labels, 1):
            print(f"\n[{i}/{len(labels)}] {label}（{ASCII.get(label, label)}）"
                  f" 倒数 {args.prep:g} 秒，采 {args.seconds:g} 秒，"
                  f"最多 {args.per_label} 张")
            if not _countdown(cam, args.prep, label):
                break
            saved = rejected = seen = 0
            seq = 0
            label_start = time.time()
            while time.time() - label_start < args.seconds and saved < args.per_label:
                frame = cam.read()
                if frame is None:
                    time.sleep(0.01)
                    continue
                seen += 1
                if seen % max(1, args.every) != 0:
                    continue
                faces = det.detect(frame)
                row = pick_face(faces)
                status = "no face"
                if row is not None:
                    try:
                        aligned = emb.align(frame, row)
                        q = _quality(aligned)
                        ok, why = accept_quality(q)
                        if ok:
                            seq += 1
                            path = save_crop(aligned, label, session, out_dir, seq)
                            append_index(index_path, {
                                "file": str(path.relative_to(out_dir)),
                                "label": label,
                                "session": session,
                                "source": "camera",
                                "ts": _now_iso(),
                                "score": round(float(row[-1]), 4),
                                "quality": q,
                            })
                            saved += 1
                            total += 1
                            status = f"saved={saved} q={q['brightness']}/{q['blur']}"
                        else:
                            rejected += 1
                            status = f"reject {why}"
                    except Exception as e:
                        rejected += 1
                        status = f"error {type(e).__name__}"
                if preview:
                    key = _show(frame, [f"{label}  {saved}/{args.per_label}",
                                        status, "q/ESC = 停止"])
                    if key in (27, ord("q")):
                        print("用户中止。")
                        raise SystemExit(0)
                if saved and saved % 10 == 0:
                    print(f"  {label}: 已存 {saved}，拒绝 {rejected}")
            counts[label] = counts.get(label, 0) + saved
            print(f"  {label}: 存 {saved}，拒绝 {rejected}，读 {seen} 帧")
            if i < len(labels) and not _relax(cam):
                break
    finally:
        cam.close()
        if preview:
            try:
                cv2.destroyWindow("collect_expression_set")
            except Exception:
                pass

    print("\n=== 本轮汇总 ===")
    for label in labels:
        print(f"  {label:2s}: {counts.get(label, 0)}")
    print(f"  合计: {total}；session={session}；用时 {time.time()-started:.1f}s")
    if total == 0:
        print("一张都没存：检查光线、距离、C920 镜头盖和人脸分数。")
        return 4
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="collect_expression_set",
                                 description="连续采集 8 类表情（默认不打开摄像头）")
    ap.add_argument("--allow-camera", action="store_true",
                    help="确认新 C920 已接后，显式允许本次打开摄像头")
    ap.add_argument("--camera-index", type=int, default=C.CAMERA_INDEX)
    ap.add_argument("--backend", default=C.CAMERA_BACKEND,
                    choices=["dshow", "msmf", "any"])
    ap.add_argument("--data-dir", default=str(C.VISION_DATA))
    ap.add_argument("--session", default=None,
                    help="同一人/同一光线一轮的名字；默认时间戳")
    ap.add_argument("--labels", default=",".join(C.EXPRESSION_LABELS),
                    help="逗号分隔；默认全部 8 类")
    ap.add_argument("--per-label", type=int, default=40)
    ap.add_argument("--seconds", type=float, default=10.0)
    ap.add_argument("--prep", type=float, default=3.0,
                    help="每类开始前倒数秒数（默认 3；新环境可设 10-12）")
    ap.add_argument("--every", type=int, default=2, help="每 N 帧取一帧")
    ap.add_argument("--no-preview", action="store_true")
    ap.add_argument("--guide", action="store_true")
    args = ap.parse_args(argv)
    if args.guide:
        print_guide()
        return 0
    return capture_set(args)


if __name__ == "__main__":
    raise SystemExit(main())
