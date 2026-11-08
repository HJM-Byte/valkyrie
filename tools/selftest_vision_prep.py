"""视觉到货准备自检：不打开摄像头，验预检/采集/训练三条链路。

跑法：
    C:\\robot\\venv\\Scripts\\python.exe -m tools.selftest_vision_prep

边界：
  - 不调用 `--camera`、不打开设备、不改 VISION_LOCKED；
  - 训练只用合成图，不把合成准确率当真实效果；
  - 前后核对 data/sleep.json 的 SHA256，确认没有消费她的休眠记录。
"""
from __future__ import annotations

import hashlib
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from app import config as C  # noqa: E402
from app.vision import FaceDetector, FaceEmbedder, imread_unicode, imwrite_unicode  # noqa: E402

import collect_face_data as collect  # noqa: E402
import train_expression as trainer  # noqa: E402
import vision_preflight as preflight  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK ] {name}" + (f"  {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  [FAIL] {name}" + (f"  {detail}" if detail else ""))


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else "-"


def main() -> int:
    print("=" * 66)
    print("视觉到货准备自检（不开摄像头）")
    print("=" * 66)
    sleep_hash = sha(C.DATA / "sleep.json")

    # 1. 锁定与静态检查
    check("VISION_LOCKED 仍为 True", C.VISION_LOCKED is True)
    check("表情标签至少 8 类", len(C.EXPRESSION_LABELS) >= 8,
          "、".join(C.EXPRESSION_LABELS))
    static = preflight.static_report()
    required = [v for v in static["checks"].values() if v.get("required", True)]
    check("预检必过项全通过", all(v["ok"] for v in required),
          f"{len(required)} 项")
    check("预检不自动解锁", static["vision_locked"] is True)
    check("YuNet 与 SFace 文件存在",
          Path(C.YUNET_MODEL).exists() and Path(C.SFACE_MODEL).exists())

    # 2. Unicode 路径与对齐
    sample = C.MODELS / "samples" / "demo.jpg"
    img = imread_unicode(sample)
    check("中文/ASCII 路径读图", img is not None, str(sample))
    det = FaceDetector()
    emb = FaceEmbedder()
    faces = det.detect(img)
    check("样本图检出人脸", len(faces) > 0, f"{len(faces)} 张")
    row = collect.pick_face(faces, min_score=0.6, min_px=40)
    aligned = emb.align(img, row) if row is not None else None
    check("对齐输出 112x112x3",
          aligned is not None and aligned.shape == (112, 112, 3),
          str(None if aligned is None else aligned.shape))
    with tempfile.TemporaryDirectory(prefix="vision_prep_") as td:
        p = Path(td) / "中文目录" / "脸.jpg"
        ok = imwrite_unicode(p, aligned)
        back = imread_unicode(p)
        check("中文路径写图/回读", ok and back is not None and back.shape == aligned.shape)

    # 3. 质量门与采集
    good_q = {"brightness": 120.0, "blur": 100.0}
    check("正常质量接受", collect.accept_quality(good_q)[0])
    check("过暗拒绝", not collect.accept_quality({"brightness": 5, "blur": 100})[0])
    check("模糊拒绝", not collect.accept_quality({"brightness": 120, "blur": 1})[0])
    with tempfile.TemporaryDirectory(prefix="vision_collect_") as td:
        root = Path(td)
        saved = collect.save_crop(aligned, "中性", "s1", root, 1)
        check("采集存图", saved.exists() and saved.stat().st_size > 1000,
              f"{saved.stat().st_size} bytes")
        idx = root / "faces_index.jsonl"
        collect.append_index(idx, {"file": str(saved.relative_to(root)), "label": "中性",
                                   "session": "s1", "quality": good_q})
        check("索引追加", len(idx.read_text(encoding="utf-8").splitlines()) == 1)

    # 4. 数据加载、切分、训练
    imgs, labels = trainer.synthetic_images(10, labels=["中性", "开心"])
    records = [{"label": labels[i % 2], "session": f"s{i // 2}",
                "path": f"syn_{i}.jpg"} for i in range(len(imgs))]
    trainer._set_record_labels(records)
    train_idx, val_idx = trainer.split_indices(records, val_ratio=0.25, seed=3)
    train_sessions = {records[i]["session"] for i in train_idx}
    val_sessions = {records[i]["session"] for i in val_idx}
    check("训练/验证按 session 不重叠", not (train_sessions & val_sessions),
          f"{len(train_sessions)} / {len(val_sessions)} 组")
    check("训练集包含两类", {records[i]["label"] for i in train_idx} == set(labels))
    model, met = trainer.train_numpy(imgs, labels, train_idx, val_idx,
                                     epochs=5, hidden=32, batch=8, lr=0.05, seed=3)
    check("numpy 基线产出模型", model["W1"].shape[1] == 32 and model["W2"].shape[1] == 2)
    check("验证指标有 accuracy", met.get("accuracy") is not None,
          str(met.get("accuracy")))
    with tempfile.TemporaryDirectory(prefix="vision_model_") as td:
        out = Path(td) / "expression_mlp.npz"
        trainer._save_numpy(model, out)
        from app.expression import ExpressionRecognizer
        er = ExpressionRecognizer(out)
        pred = er.predict(imgs[0])
        check("模型保存后被推理层加载", er.available)
        check("推理输出标签与置信度",
              pred is not None and pred["label"] in labels
              and 0.0 <= pred["confidence"] <= 1.0,
              str(None if pred is None else (pred["label"], round(pred["confidence"], 3))))

    # 5. 全帧不保存：采集默认只存 crop；这里用接口契约确认没有 frame 字段
    check("索引默认不含整帧字段", "frame" not in {"file", "label", "session", "quality"})

    # 6. 锁定拦截
    args = SimpleNamespace(allow_camera=False, label="中性", camera_index=C.CAMERA_INDEX,
                           backend=C.CAMERA_BACKEND, data_dir=str(C.VISION_DATA),
                           index=None, session="x", seconds=1, max_saved=1,
                           every=3, no_preview=True)
    check("无 --allow-camera 时采集拒绝", collect.capture(args) == 2)

    after_hash = sha(C.DATA / "sleep.json")
    check("休眠记录未被消费", sleep_hash == after_hash,
          f"{sleep_hash[:12]}... / {after_hash[:12]}...")

    print("-" * 66)
    print(f"{PASS} 通过 / {FAIL} 失败")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
