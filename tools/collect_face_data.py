"""采集表情训练数据：摄像头 → 最大人脸 → 对齐 112x112 → 按表情分类存盘。

默认拒绝打开摄像头（`VISION_LOCKED=True`），显式加 `--allow-camera` 才采集；
这个开关只影响本次采集，不会改 `config.py` 的锁。

只保存对齐后的人脸小图，默认不保存整帧；索引写 `data/vision/faces_index.jsonl`。
采集协议见 `docs/视觉与表情训练.md`。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config as C  # noqa: E402
from app.vision import (  # noqa: E402
    Camera, FaceDetector, FaceEmbedder, imread_unicode, imwrite_unicode,
)


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _quality(aligned: np.ndarray) -> dict:
    gray = cv2.cvtColor(aligned, cv2.COLOR_BGR2GRAY)
    return {
        "brightness": round(float(gray.mean()), 1),
        "blur": round(float(cv2.Laplacian(gray, cv2.CV_64F).var()), 1),
        "clip_hi": round(float((gray >= 245).mean()), 4),
    }


def pick_face(faces: np.ndarray, min_score: float = C.VISION_MIN_FACE_SCORE,
              min_px: int = C.VISION_MIN_FACE_PX) -> np.ndarray | None:
    """从 YuNet 结果里挑面积最大且过门槛的一张。"""
    best = None
    best_area = -1.0
    if faces is None:
        return None
    for row in faces:
        x, y, w, h = [float(v) for v in row[:4]]
        score = float(row[-1])
        if score < min_score or min(w, h) < min_px:
            continue
        area = w * h
        if area > best_area:
            best, best_area = row, area
    return best


def accept_quality(q: dict, min_blur: float = 25.0,
                   min_brightness: float = 35.0,
                   max_brightness: float = 230.0,
                   max_clip_hi: float = 0.08) -> tuple[bool, str]:
    if not (min_brightness <= q["brightness"] <= max_brightness):
        return False, "亮度不合适"
    if q.get("clip_hi", 0.0) > max_clip_hi:
        return False, "过曝"
    if q["blur"] < min_blur:
        return False, "太糊"
    return True, "ok"


def append_index(index_path: Path, record: dict) -> None:
    index_path.parent.mkdir(parents=True, exist_ok=True)
    with index_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def save_crop(aligned: np.ndarray, label: str, session: str,
              out_dir: Path, seq: int) -> Path:
    label_dir = out_dir / "faces" / label
    label_dir.mkdir(parents=True, exist_ok=True)
    path = label_dir / f"{session}_{seq:05d}.jpg"
    if not imwrite_unicode(path, aligned):
        raise OSError(f"写图失败: {path}")
    return path


def print_guide() -> None:
    print("""采集协议（一轮 3–5 分钟）：
  1. 灯放在脸前方，不要背光；距离摄像头 40–70cm。
  2. 每个表情分 2–3 组，每组保持 3–5 秒，自然做，不挤。
  3. 每组之间回到中性 2 秒，避免上一个表情挂在脸上。
  4. 一个 session 只拍一个人、一个光线环境；换场景换 session。
  5. 每类先收 40–60 张，8 类共 300–500 张再开训；少于 8 张/类不训。
  6. 只存人脸小图；不要把整帧、聊天记录或别人的脸混进来。

标签：""" + "、".join(C.EXPRESSION_LABELS))


def self_test() -> int:
    """不碰摄像头：用自带样本图验 检测→对齐→存图→索引 往返。"""
    print("=" * 62)
    print("采集链路自检（不打开摄像头）")
    print("=" * 62)
    sample = C.MODELS / "samples" / "demo.jpg"
    if not sample.exists():
        sample = C.MODELS / "samples" / "selfie.jpg"
    img = imread_unicode(sample)
    if img is None:
        print(f"!! 测试图读不出: {sample}")
        return 2
    det = FaceDetector()
    faces = det.detect(img)
    print(f"  {sample.name}: {img.shape[1]}x{img.shape[0]}，检出 {len(faces)} 张脸")
    row = pick_face(faces, min_score=0.6, min_px=40)
    if row is None:
        print("!! 没有可用的脸")
        return 3
    emb = FaceEmbedder()
    aligned = emb.align(img, row)
    q = _quality(aligned)
    print(f"  对齐 {aligned.shape}，亮度 {q['brightness']}，清晰度 {q['blur']}")
    with tempfile.TemporaryDirectory(prefix="vision_selftest_") as td:
        root = Path(td)
        p = save_crop(aligned, "中性", "selftest", root, 1)
        back = imread_unicode(p)
        print(f"  存图 {p.name} {p.stat().st_size} bytes，回读 "
              f"{'OK' if back is not None and back.shape == aligned.shape else 'FAIL'}")
        rec = {"file": str(p.relative_to(root)), "label": "中性",
               "session": "selftest", "source": "selftest", "ts": _now_iso(),
               "score": float(row[-1]), "quality": q}
        append_index(root / "faces_index.jsonl", rec)
        lines = (root / "faces_index.jsonl").read_text(encoding="utf-8").splitlines()
        print(f"  索引 {len(lines)} 行")
        ok = (back is not None and back.shape == aligned.shape and len(lines) == 1)
    print("=== 采集链路通过 ===" if ok else "=== 采集链路失败 ===")
    return 0 if ok else 4


def capture(args: argparse.Namespace) -> int:
    if C.VISION_LOCKED and not args.allow_camera:
        print("视觉仍锁定（VISION_LOCKED=True）。")
        print("确认插的是新摄像头后，再加 --allow-camera；这个参数不会改 config.py。")
        return 2
    if args.label not in C.EXPRESSION_LABELS:
        print(f"标签必须是：{'、'.join(C.EXPRESSION_LABELS)}")
        return 2
    out_dir = Path(args.data_dir)
    index_path = Path(args.index) if args.index else out_dir / "faces_index.jsonl"
    session = args.session or time.strftime("%Y%m%d_%H%M%S")
    cam = Camera(index=args.camera_index, backend=args.backend)
    if not cam.open(timeout=4.0):
        print(f"摄像头打不开（index={args.camera_index}, backend={args.backend}）；"
              "先跑 tools/vision_preflight.py --camera")
        return 3
    det = FaceDetector()
    emb = FaceEmbedder()
    preview = not args.no_preview
    if preview:
        try:
            cv2.namedWindow("collect_face_data", cv2.WINDOW_NORMAL)
        except Exception as e:
            print(f"预览不可用，转无窗口采集: {type(e).__name__}: {e}")
            preview = False
    saved = rejected = seen = 0
    started = time.time()
    seq = 0
    print(f"开始采集：标签={args.label} session={session}；"
          f"q/ESC 结束，保存 {args.max_saved} 张或 {args.seconds or '不限'} 秒")
    try:
        while True:
            if args.seconds and time.time() - started >= args.seconds:
                break
            if args.max_saved and saved >= args.max_saved:
                break
            frame = cam.read()
            if frame is None:
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
                        path = save_crop(aligned, args.label, session, out_dir, seq)
                        append_index(index_path, {
                            "file": str(path.relative_to(out_dir)),
                            "label": args.label,
                            "session": session,
                            "source": "camera",
                            "ts": _now_iso(),
                            "score": round(float(row[-1]), 4),
                            "quality": q,
                        })
                        saved += 1
                        status = f"saved {saved} ({q['brightness']}/{q['blur']})"
                    else:
                        rejected += 1
                        status = f"reject: {why}"
                except Exception as e:
                    rejected += 1
                    status = f"error: {type(e).__name__}"
            if preview:
                try:
                    show = frame.copy()
                    cv2.putText(show, f"{args.label}  saved={saved}  {status}",
                                (12, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7,
                                (0, 255, 0), 2, cv2.LINE_AA)
                    cv2.imshow("collect_face_data", show)
                    key = cv2.waitKey(1) & 0xFF
                    if key in (27, ord("q")):
                        break
                except Exception:
                    preview = False
            if saved and saved % 10 == 0:
                print(f"  已存 {saved} 张（拒绝 {rejected}）")
    finally:
        cam.close()
        if preview:
            try:
                cv2.destroyWindow("collect_face_data")
            except Exception:
                pass
    print(f"结束：保存 {saved} 张，拒绝 {rejected}，索引 {index_path}")
    if saved == 0:
        print("一张都没存：检查光线、距离、人脸分数与清晰度。")
        return 4
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="collect_face_data",
                                 description="表情数据采集（默认不打开摄像头）")
    ap.add_argument("--label", choices=list(C.EXPRESSION_LABELS),
                    help="这一轮采集的表情标签")
    ap.add_argument("--allow-camera", action="store_true",
                    help="确认新摄像头已接后，显式允许本次打开摄像头")
    ap.add_argument("--camera-index", type=int, default=C.CAMERA_INDEX)
    ap.add_argument("--backend", default=C.CAMERA_BACKEND,
                    choices=["dshow", "msmf", "any"])
    ap.add_argument("--data-dir", default=str(C.VISION_DATA))
    ap.add_argument("--index", default=None, help="索引路径（默认 data-dir/faces_index.jsonl）")
    ap.add_argument("--session", default=None, help="同一环境/同一人一轮的名字")
    ap.add_argument("--seconds", type=float, default=0,
                    help="采集时长，0=由 max-saved 或手动结束")
    ap.add_argument("--max-saved", type=int, default=60)
    ap.add_argument("--every", type=int, default=3, help="每 N 帧取一帧")
    ap.add_argument("--no-preview", action="store_true")
    ap.add_argument("--guide", action="store_true", help="打印采集协议")
    ap.add_argument("--self-test", action="store_true", help="不打开摄像头，验链路")
    args = ap.parse_args(argv)

    if args.guide:
        print_guide()
        return 0
    if args.self_test:
        return self_test()
    if not args.label:
        ap.error("采集需要 --label（或用 --self-test）")
    return capture(args)


if __name__ == "__main__":
    raise SystemExit(main())
