"""手机/背景二分类：用本地两轮手机采集 + 空场景负样本训练小分类头。

数据契约：data/objects_local/objects_index.jsonl，label 为 手机/背景。
训练：phone_a 的手机框裁剪；验证：phone_c 的手机框裁剪。
负样本：背景帧随机裁剪，按文件序号 70/30 切训练/验证。
"""
from __future__ import annotations

import argparse
import json
import random
import time
from collections import defaultdict
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
import sys
sys.path.insert(0, str(ROOT))

from app import config as C  # noqa: E402
from app.objects import ObjectDetector  # noqa: E402
from app.vision import imread_unicode  # noqa: E402

SIZE = 112


def load_index(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    return rows


def square_crop(img: np.ndarray, box: tuple[int, int, int, int], margin: float = 0.15) -> np.ndarray | None:
    x, y, w, h = box
    pad = int(max(w, h) * margin)
    x1, y1 = max(0, x - pad), max(0, y - pad)
    x2, y2 = min(img.shape[1], x + w + pad), min(img.shape[0], y + h + pad)
    if x2 <= x1 or y2 <= y1:
        return None
    # 先按短边扩成正方形，避免把手机拉扁
    cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
    side = max(x2 - x1, y2 - y1)
    sx1, sy1 = max(0, cx - side // 2), max(0, cy - side // 2)
    sx2, sy2 = min(img.shape[1], sx1 + side), min(img.shape[0], sy1 + side)
    if sx2 <= sx1 or sy2 <= sy1:
        return None
    crop = img[sy1:sy2, sx1:sx2]
    return cv2.resize(crop, (SIZE, SIZE), interpolation=cv2.INTER_AREA)


def random_crop(img: np.ndarray, rng: random.Random) -> np.ndarray | None:
    h, w = img.shape[:2]
    side = int(min(h, w) * rng.uniform(0.25, 0.55))
    if side < 40:
        return None
    x = rng.randint(0, max(0, w - side))
    y = rng.randint(0, max(0, h - side))
    return cv2.resize(img[y:y + side, x:x + side], (SIZE, SIZE), interpolation=cv2.INTER_AREA)


def prep(img: np.ndarray) -> np.ndarray:
    rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
    return ((rgb - 0.5) / 0.5).transpose(2, 0, 1)


def main() -> int:
    import torch
    from torch import nn
    from torch.utils.data import DataLoader, TensorDataset

    ap = argparse.ArgumentParser()
    ap.add_argument("--index", default=str(ROOT / "data" / "objects_local" / "objects_index.jsonl"))
    ap.add_argument("--phone-train", default="20261029_phone_a")
    ap.add_argument("--phone-val", default="20261029_phone_c")
    ap.add_argument("--bg-session", default="20261029_bg_a")
    ap.add_argument("--out", default=str(C.MODELS / "objects" / "phone_classifier.pt"))
    ap.add_argument("--epochs", type=int, default=35)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    rows = load_index(Path(args.index))
    det = ObjectDetector()
    rng = random.Random(args.seed)
    torch.manual_seed(args.seed)

    def phone_crops(session: str) -> list[np.ndarray]:
        out = []
        for r in rows:
            if r.get("session") != session or r.get("label") != "手机":
                continue
            img = imread_unicode(ROOT / "data" / "objects_local" / r["file"])
            if img is None:
                continue
            ds = [d for d in det.detect(img) if d["label"] == "cell phone"]
            if not ds:
                continue
            top = max(ds, key=lambda d: d["score"])
            c = square_crop(img, top["box"])
            if c is not None:
                out.append(c)
        return out

    def bg_crops() -> list[np.ndarray]:
        out = []
        for r in rows:
            if r.get("session") != args.bg_session or r.get("label") != "背景":
                continue
            img = imread_unicode(ROOT / "data" / "objects_local" / r["file"])
            if img is None:
                continue
            for _ in range(3):
                c = random_crop(img, rng)
                if c is not None:
                    out.append(c)
        return out

    p_train, p_val = phone_crops(args.phone_train), phone_crops(args.phone_val)
    b_all = bg_crops()
    rng.shuffle(b_all)
    n_b_train = int(len(b_all) * 0.7)
    b_train, b_val = b_all[:n_b_train], b_all[n_b_train:]
    print(f"手机训练 {len(p_train)}、手机验证 {len(p_val)}；背景训练 {len(b_train)}、背景验证 {len(b_val)}")

    def tensor(items: list[np.ndarray]) -> torch.Tensor:
        return torch.tensor(np.stack([prep(x) for x in items]), dtype=torch.float32)

    x_train = torch.cat([tensor(p_train), tensor(b_train)])
    y_train = torch.cat([torch.ones(len(p_train)), torch.zeros(len(b_train))]).long()
    x_val = torch.cat([tensor(p_val), tensor(b_val)])
    y_val = torch.cat([torch.ones(len(p_val)), torch.zeros(len(b_val))]).long()

    net = nn.Sequential(
        nn.Conv2d(3, 16, 3, padding=1), nn.BatchNorm2d(16), nn.ReLU(), nn.MaxPool2d(2),
        nn.Conv2d(16, 32, 3, padding=1), nn.BatchNorm2d(32), nn.ReLU(), nn.MaxPool2d(2),
        nn.Conv2d(32, 64, 3, padding=1), nn.BatchNorm2d(64), nn.ReLU(),
        nn.AdaptiveAvgPool2d(1), nn.Flatten(), nn.Dropout(0.2), nn.Linear(64, 2),
    )
    opt = torch.optim.AdamW(net.parameters(), lr=1e-3, weight_decay=1e-4)
    loss_fn = nn.CrossEntropyLoss()
    train_loader = DataLoader(TensorDataset(x_train, y_train), batch_size=8, shuffle=True)
    t0 = time.time()
    for epoch in range(1, args.epochs + 1):
        net.train()
        for xb, yb in train_loader:
            opt.zero_grad()
            loss = loss_fn(net(xb), yb)
            loss.backward()
            opt.step()
        if epoch % 5 == 0 or epoch == args.epochs:
            net.eval()
            with torch.no_grad():
                pred = net(x_val).argmax(1)
                acc = float((pred == y_val).float().mean())
            print(f"epoch {epoch:02d} loss={loss.item():.4f} val_acc={acc:.3f}")

    net.eval()
    with torch.no_grad():
        probs = net(x_val).softmax(1)[:, 1]
        pred = (probs >= 0.5).long()
        tp = int(((pred == 1) & (y_val == 1)).sum())
        fn = int(((pred == 0) & (y_val == 1)).sum())
        fp = int(((pred == 1) & (y_val == 0)).sum())
        tn = int(((pred == 0) & (y_val == 0)).sum())
    recall = tp / max(1, tp + fn)
    precision = tp / max(1, tp + fp)
    acc = (tp + tn) / max(1, len(y_val))
    result = {"phone_recall": round(recall, 4), "phone_precision": round(precision, 4),
              "background_recall": round(tn / max(1, tn + fp), 4), "accuracy": round(acc, 4),
              "tp": tp, "fn": fn, "fp": fp, "tn": tn,
              "train_phone": len(p_train), "val_phone": len(p_val),
              "train_bg": len(b_train), "val_bg": len(b_val), "elapsed_s": round(time.time() - t0, 1)}
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    torch.save({"state_dict": net.cpu().state_dict(), "labels": ["background", "phone"],
                "input_size": SIZE, "metrics": result}, out)
    report = out.with_suffix(".json")
    report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print("结果", json.dumps(result, ensure_ascii=False))
    print("模型", out)
    det.close()
    return 0 if recall >= 0.8 and precision >= 0.8 else 4


if __name__ == "__main__":
    raise SystemExit(main())
