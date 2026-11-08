"""摄像头到货预检：先验模型和数据目录，再接设备。

安全边界：
  - 默认不打开摄像头；
  - 只有显式 `--camera` 才会试探设备，而且**不会改** `VISION_LOCKED`；
  - 预检不写她的记忆、不消费休眠记录、不叫醒她。

用法（摄像头到货后）：
    C:\\robot\\venv\\Scripts\\python.exe tools\\vision_preflight.py --self-test
    C:\\robot\\venv\\Scripts\\python.exe tools\\vision_preflight.py --enumerate
    C:\\robot\\venv\\Scripts\\python.exe tools\\vision_preflight.py --camera --snapshot --expect-face
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config as C  # noqa: E402
from app.vision import (  # noqa: E402
    Camera, FaceDB, FaceDetector, FaceEmbedder, imread_unicode, imwrite_unicode,
    list_cameras,
)


def _json_default(obj):
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, np.generic):
        return obj.item()
    return str(obj)


def _quality(frame: np.ndarray) -> dict:
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    return {
        "brightness": round(float(gray.mean()), 2),
        "blur": round(float(cv2.Laplacian(gray, cv2.CV_64F).var()), 2),
    }


def static_report() -> dict:
    """不碰摄像头的静态检查。"""
    C.VISION_DATA.mkdir(parents=True, exist_ok=True)
    C.VISION_FACES.mkdir(parents=True, exist_ok=True)
    C.VISION_PROBE.mkdir(parents=True, exist_ok=True)
    checks: dict[str, dict] = {}

    def add(name: str, ok: bool, detail: str, required: bool = True) -> None:
        checks[name] = {"ok": bool(ok), "detail": detail, "required": bool(required)}

    add("vision_locked", C.VISION_LOCKED,
        "仍锁定：不会自动开摄像头；确认新设备后再手改 config.py",
        required=False)
    add("yunet_exists", Path(C.YUNET_MODEL).exists(), str(C.YUNET_MODEL))
    add("sface_exists", Path(C.SFACE_MODEL).exists(), str(C.SFACE_MODEL))
    add("expression_labels", len(C.EXPRESSION_LABELS) >= 2,
        f"{len(C.EXPRESSION_LABELS)} 类：{'、'.join(C.EXPRESSION_LABELS)}")
    add("data_dir", C.VISION_DATA.exists(), str(C.VISION_DATA))
    add("object_model", Path(C.OBJECT_MODEL).exists(), str(C.OBJECT_MODEL),
        required=False)
    try:
        from app.objects import ObjectDetector
        ObjectDetector()
        add("object_model_load", True, "YOLOv8 ONNX 可加载", required=False)
    except Exception as e:
        add("object_model_load", False, f"{type(e).__name__}: {e}", required=False)
    try:
        db = FaceDB()
        add("face_db", bool(db.entries), f"{len(db.entries)} 个身份 / {db.path}",
            required=False)
    except Exception as e:
        add("face_db", False, f"{type(e).__name__}: {e}", required=False)

    try:
        det = FaceDetector()
        emb = FaceEmbedder()
        add("yunet_load", True, "ok")
        add("sface_load", True, "ok")
    except Exception as e:
        add("yunet_load", False, f"{type(e).__name__}: {e}")
        add("sface_load", False, "未测（前置失败）")
        det = emb = None

    sample = C.MODELS / "samples" / "demo.jpg"
    if not sample.exists():
        sample = C.MODELS / "samples" / "selfie.jpg"
    if sample.exists():
        img = imread_unicode(sample)
        add("sample_read", img is not None, f"{sample.name} {None if img is None else img.shape}")
        if img is not None and det is not None and emb is not None:
            t0 = time.time()
            faces = det.detect(img)
            add("face_detect", len(faces) > 0,
                f"{len(faces)} 张脸 / {((time.time()-t0)*1000):.1f}ms")
            if len(faces) > 0:
                try:
                    aligned = emb.align(img, faces[0])
                    add("face_align", aligned.shape == (112, 112, 3), str(aligned.shape))
                except Exception as e:
                    add("face_align", False, f"{type(e).__name__}: {e}")
    else:
        add("sample_read", False, f"缺测试图：{sample}")

    add("training_torch", importlib.util.find_spec("torch") is not None,
        "torch 已装，可训深度 CNN" if importlib.util.find_spec("torch")
        else "未装 torch；先用无依赖 numpy 基线，真训 CNN 需你点头安装",
        required=False)
    add("expression_model", C.EXPRESSION_MODEL.exists(), str(C.EXPRESSION_MODEL),
        required=False)
    return {"checks": checks, "vision_locked": C.VISION_LOCKED,
            "data_dir": str(C.VISION_DATA), "model_dir": str(C.EXPRESSION_DIR)}


def probe_camera(index: int, backend: str, frames: int = 30,
                 snapshot: bool = False) -> dict:
    """显式打开一次摄像头；失败返回 ok=False。"""
    cam = Camera(index=index, backend=backend)
    out: dict = {"index": index, "backend": backend, "ok": False}
    t0 = time.time()
    try:
        if not cam.open(timeout=4.0):
            out["error"] = "open_failed_or_timeout"
            return out
        out["open_ms"] = round((time.time() - t0) * 1000, 1)
        first = None
        faces_total = 0
        detect_ms: list[float] = []
        frame_ms: list[float] = []
        quality = {}
        det = FaceDetector()
        for i in range(max(1, frames)):
            t_read = time.time()
            frame = cam.read()
            if frame is None:
                continue
            frame_ms.append((time.time() - t_read) * 1000)
            if first is None:
                first = frame
                quality = _quality(frame)
            t_det = time.time()
            faces = det.detect(frame)
            detect_ms.append((time.time() - t_det) * 1000)
            faces_total += len(faces)
        out.update({
            "ok": True,
            "width": int(first.shape[1]) if first is not None else 0,
            "height": int(first.shape[0]) if first is not None else 0,
            "frames_read": len(frame_ms),
            "read_ms_avg": round(float(np.mean(frame_ms)), 2) if frame_ms else None,
            "detect_ms_avg": round(float(np.mean(detect_ms)), 2) if detect_ms else None,
            "faces_total": faces_total,
            "faces_per_frame": round(faces_total / max(1, len(detect_ms)), 2),
            "quality": quality,
        })
        if first is not None and snapshot:
            C.VISION_PROBE.mkdir(parents=True, exist_ok=True)
            name = f"probe_{time.strftime('%Y%m%d_%H%M%S')}_{backend}_i{index}.jpg"
            path = C.VISION_PROBE / name
            out["snapshot"] = str(path) if imwrite_unicode(path, first) else ""
        return out
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
        return out
    finally:
        cam.close()


def camera_report(index: int, frames: int, snapshot: bool,
                  expect_face: bool) -> dict:
    backends: list[str] = []
    for b in (C.CAMERA_BACKEND, "msmf", "any"):
        if b not in backends:
            backends.append(b)
    attempts = [probe_camera(index, b, frames=frames, snapshot=snapshot)
                for b in backends]
    good = next((a for a in attempts if a.get("ok")), None)
    report = {"index": index, "attempts": attempts, "ok": bool(good)}
    if good:
        report["chosen"] = {"backend": good["backend"], "index": good["index"]}
        report["faces"] = good.get("faces_total", 0)
    if expect_face and good and int(good.get("faces_total") or 0) <= 0:
        report["ok"] = False
        report["reason"] = "camera_opened_but_no_face"
    return report


def _print_human(report: dict) -> None:
    checks = report.get("checks") or {}
    print("=" * 66)
    print("视觉预检（不自动解锁）")
    print("=" * 66)
    for name, item in checks.items():
        if item["ok"]:
            mark = "OK  "
        elif not item.get("required", True):
            mark = "WARN"
        else:
            mark = "FAIL"
        print(f"[{mark}] {name:18s} {item['detail']}")
    print(f"\nVISION_LOCKED = {report.get('vision_locked')}")
    print(f"数据目录       = {report.get('data_dir')}")
    print(f"表情模型目录   = {report.get('model_dir')}")
    if "attempts" in report:
        print("\n--- 摄像头尝试 ---")
        for a in report["attempts"]:
            print(f"  {a.get('backend'):5s} idx={a.get('index')} -> "
                  f"{'可用' if a.get('ok') else '不可用'}"
                  + (f" {a.get('width')}x{a.get('height')} "
                     f"faces={a.get('faces_total')} "
                     f"read={a.get('read_ms_avg')}ms det={a.get('detect_ms_avg')}ms"
                     if a.get("ok") else f" ({a.get('error')})"))
        if report.get("ok"):
            c = report.get("chosen") or {}
            print(f"  建议：CAMERA_INDEX={c.get('index')} / "
                  f"CAMERA_BACKEND={c.get('backend')}")
        else:
            print(f"  摄像头未通过：{report.get('reason', '没有可用后端')}")
    if report.get("ok") and report.get("faces", 1) > 0:
        print("\n下一步：确认画面/表情后再手改 app/config.py 的 VISION_LOCKED=False；"
              "预检不会替你解锁。")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="vision_preflight",
                                 description="摄像头到货预检（默认不打开摄像头）")
    ap.add_argument("--self-test", action="store_true", help="只做静态与样本图自检")
    ap.add_argument("--camera", action="store_true", help="显式试探摄像头（不自动解锁）")
    ap.add_argument("--enumerate", action="store_true", help="枚举摄像头索引")
    ap.add_argument("--index", type=int, default=C.CAMERA_INDEX)
    ap.add_argument("--frames", type=int, default=30)
    ap.add_argument("--snapshot", action="store_true", help="保存一帧到 data/vision/probe")
    ap.add_argument("--expect-face", action="store_true", help="画面里必须至少一张脸")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args(argv)

    static = static_report()
    if args.enumerate:
        print("枚举摄像头（DShow，最多 4 个索引）……", flush=True)
        cams = list_cameras(4, timeout=2.5)
        print(f"可用索引：{cams if cams else '无'}")
        if not args.camera:
            return 0

    if args.camera:
        print(f"显式试探摄像头：index={args.index}，每后端读 {args.frames} 帧……",
              flush=True)
        print("注意：--camera 会按索引打开设备；若这是旧的内置摄像头，"
              "先确认新摄像头已插或改用 --index。", flush=True)
        cam = camera_report(args.index, args.frames, args.snapshot, args.expect_face)
        report = {**static, **cam}
    else:
        report = static

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=_json_default))
    else:
        _print_human(report)

    if args.self_test:
        # vision_locked / torch / 已训练模型都是“可选状态”，不算失败
        required = [v for v in static["checks"].values()
                    if v.get("required", True)]
        return 0 if all(v["ok"] for v in required) else 2
    if not args.camera:
        return 0
    return 0 if report.get("ok") else 3


if __name__ == "__main__":
    raise SystemExit(main())
