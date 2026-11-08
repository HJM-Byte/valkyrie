"""表情图像模型训练：有 torch 走小 CNN，没有就用无依赖 numpy 基线。

先采数据，再训练：
    python tools\\collect_face_data.py --label 开心 --allow-camera
    python tools\\train_expression.py --dry-run
    python tools\\train_expression.py --backend auto

数据契约：`data/vision/faces_index.jsonl` + `data/vision/faces/<label>/*.jpg`
模型契约：`models\\expression\\expression_mlp.npz`（W1/b1/W2/b2 +
labels + input_size），推理在 `app/expression.py`，不依赖训练脚本。

注意：没有真实人脸数据时不要拿合成图冒充模型效果；`--self-test` 只验代码链路。
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import random
import sys
import tempfile
import time
from collections import Counter, defaultdict
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config as C  # noqa: E402
from app.vision import imread_unicode  # noqa: E402


# --------------------------------------------------------------------------- #
# 数据
# --------------------------------------------------------------------------- #
def load_records(data_dir: Path, index_path: Path | None = None) -> list[dict]:
    root = Path(data_dir)
    index = Path(index_path) if index_path else root / "faces_index.jsonl"
    records: list[dict] = []
    seen: set[str] = set()
    if index.exists():
        for line in index.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            rel = rec.get("file")
            label = rec.get("label")
            if not rel or not label:
                continue
            path = root / rel
            if not path.exists() or str(path) in seen:
                continue
            seen.add(str(path))
            rec["path"] = str(path)
            rec["label"] = str(label)
            rec["session"] = str(rec.get("session") or "unknown")
            records.append(rec)
    else:
        faces_dir = root / "faces"
        for p in sorted(faces_dir.glob("*/*.jpg")):
            if str(p) in seen:
                continue
            seen.add(str(p))
            records.append({
                "path": str(p),
                "file": str(p.relative_to(root)),
                "label": p.parent.name,
                "session": p.stem.split("_")[0] or "unknown",
                "source": "scan",
            })
    return records


def load_images(records: list[dict], size: int = 112) -> tuple[np.ndarray, list[dict]]:
    imgs: list[np.ndarray] = []
    kept: list[dict] = []
    for rec in records:
        img = imread_unicode(rec["path"])
        if img is None or img.size == 0:
            continue
        if img.shape[1] != size or img.shape[0] != size:
            img = cv2.resize(img, (size, size), interpolation=cv2.INTER_AREA)
        imgs.append(img)
        kept.append(rec)
    if not imgs:
        return np.empty((0, size, size, 3), dtype=np.uint8), []
    return np.stack(imgs).astype(np.uint8), kept


def split_indices(records: list[dict], val_ratio: float = 0.2,
                  seed: int = 42,
                  group_by_session: bool = True) -> tuple[list[int], list[int]]:
    """优先按 session 分组切，避免同一段视频的相邻帧同时进训练和验证。"""
    n = len(records)
    if n == 0:
        return [], []
    rng = random.Random(seed)
    groups: dict[str, list[int]] = defaultdict(list)
    for i, rec in enumerate(records):
        groups[str(rec.get("session") or f"single_{i}")].append(i)
    keys = list(groups)
    # 有两个以上 session 才能按 session 切；只有 1 个 session 才退回按类随机切。
    # 之前写成 >=3，导致 2 个 session（训练/验证各一）也会按类随机切，相邻帧泄漏。
    if group_by_session and len(keys) >= 2 and val_ratio > 0:
        # 先拿较小的 session 做验证，避免验证集比训练集还大；
        # 大小相同才用随机数打破平局。
        keys.sort(key=lambda k: (len(groups[k]), rng.random()))
        target = max(1, int(n * val_ratio))
        train: list[int] = []
        val: list[int] = []
        for k in keys:
            if len(val) < target:
                val.extend(groups[k])
            else:
                train.extend(groups[k])
        if not train:
            train, val = val, []
    else:
        by_label: dict[str, list[int]] = defaultdict(list)
        for i, rec in enumerate(records):
            by_label[str(rec["label"])].append(i)
        train, val = [], []
        for idxs in by_label.values():
            idxs = idxs[:]
            rng.shuffle(idxs)
            k = max(1, int(len(idxs) * val_ratio)) if val_ratio > 0 and len(idxs) >= 3 else 0
            val.extend(idxs[:k])
            train.extend(idxs[k:])
        if not train:
            train, val = val, []
    # 每个类在训练集里至少留一条；否则这个类没法学
    train_labels = {str(records[i]["label"]) for i in train}
    all_labels = {str(r["label"]) for r in records}
    for label in sorted(all_labels - train_labels):
        for j, i in enumerate(val):
            if str(records[i]["label"]) == label:
                train.append(val.pop(j))
                break
    return sorted(train), sorted(val)


def class_counts(records: list[dict]) -> dict[str, int]:
    return dict(sorted(Counter(str(r["label"]) for r in records).items()))


def _features(images: np.ndarray, size: int = 32) -> np.ndarray:
    out = np.empty((len(images), size * size), dtype=np.float32)
    for i, img in enumerate(images):
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
        gray = cv2.resize(gray, (size, size), interpolation=cv2.INTER_AREA)
        out[i] = ((gray.astype(np.float32) / 255.0) - 0.5).reshape(-1)
    return out


def _softmax(logits: np.ndarray) -> np.ndarray:
    z = logits - np.max(logits, axis=1, keepdims=True)
    e = np.exp(z)
    return e / np.maximum(e.sum(axis=1, keepdims=True), 1e-9)


def _metrics(y_true: list[int], y_pred: list[int], labels: list[str]) -> dict:
    k = len(labels)
    conf = [[0] * k for _ in range(k)]
    for t, p in zip(y_true, y_pred):
        conf[int(t)][int(p)] += 1
    total = max(1, len(y_true))
    correct = sum(conf[i][i] for i in range(k))
    per_class = {}
    for i, name in enumerate(labels):
        tp = conf[i][i]
        fp = sum(conf[j][i] for j in range(k) if j != i)
        fn = sum(conf[i][j] for j in range(k) if j != i)
        precision = tp / max(1, tp + fp)
        recall = tp / max(1, tp + fn)
        support = sum(conf[i])
        per_class[name] = {
            "precision": round(float(precision), 4),
            "recall": round(float(recall), 4),
            "support": support,
            "f1": round(float(2 * precision * recall / max(1e-9, precision + recall)), 4),
        }
    return {
        "accuracy": round(float(correct / total), 4),
        "correct": correct,
        "total": len(y_true),
        "confusion": conf,
        "per_class": per_class,
    }


# --------------------------------------------------------------------------- #
# numpy 基线
# --------------------------------------------------------------------------- #
def train_numpy(images: np.ndarray, labels: list[str], train_idx: list[int],
                val_idx: list[int], epochs: int = 50, hidden: int = 128,
                batch: int = 32, lr: float = 0.03, seed: int = 42,
                input_size: int = 32) -> tuple[dict, dict]:
    rng = np.random.default_rng(seed)
    label_to_id = {lab: i for i, lab in enumerate(labels)}
    y_all = np.array([label_to_id[str(r["label"])] for r in _RECORD_LABELS], dtype=np.int64)
    idx = np.array(train_idx, dtype=np.int64)
    X = _features(images, input_size)
    y = y_all[idx]
    if len(idx) == 0:
        raise ValueError("训练集为空")
    dim, n_class = X.shape[1], len(labels)
    counts = np.bincount(y, minlength=n_class).astype(np.float32)
    weight = (len(y) / (n_class * np.maximum(counts, 1))).astype(np.float32)
    weight = weight / max(1e-9, weight.mean())

    W1 = (rng.standard_normal((dim, hidden)) * math.sqrt(2.0 / dim)).astype(np.float32)
    b1 = np.zeros(hidden, dtype=np.float32)
    W2 = (rng.standard_normal((hidden, n_class)) * math.sqrt(2.0 / hidden)).astype(np.float32)
    b2 = np.zeros(n_class, dtype=np.float32)
    state = [W1, b1, W2, b2]
    m = [np.zeros_like(x) for x in state]
    v = [np.zeros_like(x) for x in state]
    beta1, beta2, eps = 0.9, 0.999, 1e-8
    step = 0
    best_acc, best_state = -1.0, [x.copy() for x in state]
    history = []
    Xt = X[idx]
    for epoch in range(1, epochs + 1):
        perm = rng.permutation(len(idx))
        for start in range(0, len(perm), batch):
            take = perm[start:start + batch]
            xb, yb = Xt[take], y[take]
            z1 = xb @ W1 + b1
            h = np.maximum(z1, 0.0)
            probs = _softmax(h @ W2 + b2)
            # 加权交叉熵；dlogits 已除以 batch
            onehot = np.zeros_like(probs)
            onehot[np.arange(len(yb)), yb] = 1.0
            dlogits = (probs - onehot) * weight[yb, None] / len(xb)
            gW2 = h.T @ dlogits
            gb2 = dlogits.sum(axis=0)
            dh = dlogits @ W2.T
            dh[z1 <= 0] = 0.0
            gW1 = xb.T @ dh
            gb1 = dh.sum(axis=0)
            grads = [gW1, gb1, gW2, gb2]
            step += 1
            for i in range(4):
                m[i] = beta1 * m[i] + (1 - beta1) * grads[i]
                v[i] = beta2 * v[i] + (1 - beta2) * (grads[i] ** 2)
                mh = m[i] / (1 - beta1 ** step)
                vh = v[i] / (1 - beta2 ** step)
                state[i] -= lr * mh / (np.sqrt(vh) + eps)
        # 验证
        if val_idx:
            Xv = X[np.array(val_idx, dtype=np.int64)]
            pv = _softmax(np.maximum(Xv @ state[0] + state[1], 0.0) @ state[2] + state[3])
            pred = pv.argmax(axis=1)
            true = y_all[np.array(val_idx, dtype=np.int64)]
            acc = float((pred == true).mean())
        else:
            acc = float("nan")
        history.append(acc)
        if not math.isnan(acc) and acc >= best_acc:
            best_acc = acc
            best_state = [x.copy() for x in state]
    if math.isnan(best_acc):
        best_state = [x.copy() for x in state]
    model = {
        "W1": best_state[0], "b1": best_state[1],
        "W2": best_state[2], "b2": best_state[3],
        "labels": np.array(labels), "input_size": int(input_size),
    }
    # 指标
    if val_idx:
        Xv = X[np.array(val_idx, dtype=np.int64)]
        pred = _softmax(np.maximum(Xv @ model["W1"] + model["b1"], 0.0)
                        @ model["W2"] + model["b2"]).argmax(axis=1)
        met = _metrics(y_all[np.array(val_idx, dtype=np.int64)].tolist(),
                       pred.tolist(), labels)
    else:
        met = {"accuracy": None, "total": 0, "confusion": [], "per_class": {}}
    met["history"] = [None if math.isnan(x) else round(x, 4) for x in history]
    return model, met


# --------------------------------------------------------------------------- #
# torch 小 CNN（可选）
# --------------------------------------------------------------------------- #
def torch_available() -> bool:
    return importlib.util.find_spec("torch") is not None


def _preprocess(img: np.ndarray, mode: str) -> np.ndarray:
    """跨环境时先把颜色/曝光差异压掉；RGB 保留原样，gray/clahe 转三通道灰度。"""
    if mode == "rgb":
        return img
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    if mode == "gray":
        gray = cv2.equalizeHist(gray)
    elif mode == "clahe":
        gray = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(gray)
    return cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)


def train_torch(images: np.ndarray, labels: list[str], train_idx: list[int],
                val_idx: list[int], epochs: int = 40, batch: int = 32,
                lr: float = 1e-3, seed: int = 42,
                preprocess: str = "rgb",
                sad_weight: float = 1.0) -> tuple[dict, dict]:
    import torch
    from torch import nn
    from torch.utils.data import DataLoader, Dataset

    torch.manual_seed(seed)
    torch.set_num_threads(max(1, min(4, (torch.get_num_threads() or 4))))
    rng = random.Random(seed)
    y_all = np.array([labels.index(str(r["label"])) for r in _RECORD_LABELS], dtype=np.int64)

    class DS(Dataset):
        def __init__(self, idxs: list[int], training: bool) -> None:
            self.idxs = idxs
            self.training = training

        def __len__(self) -> int:
            return len(self.idxs)

        def __getitem__(self, i: int):
            idx = self.idxs[i]
            img = images[idx].copy()
            if self.training:
                if rng.random() < 0.5:
                    img = cv2.flip(img, 1)
                if rng.random() < 0.6:
                    alpha = 1.0 + rng.uniform(-0.25, 0.25)
                    beta = rng.uniform(-20, 20)
                    img = np.clip(img.astype(np.float32) * alpha + beta, 0, 255).astype(np.uint8)
                if rng.random() < 0.35:
                    ang = rng.uniform(-8, 8)
                    h, w = img.shape[:2]
                    m = cv2.getRotationMatrix2D((w / 2, h / 2), ang, 1.0)
                    img = cv2.warpAffine(img, m, (w, h), borderMode=cv2.BORDER_REFLECT)
            img = _preprocess(img, preprocess)
            rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
            rgb = (rgb - 0.5) / 0.5
            x = torch.from_numpy(rgb.transpose(2, 0, 1)).float()
            return x, int(y_all[idx])

    train_ds = DS(train_idx, True)
    val_ds = DS(val_idx, False)
    train_loader = DataLoader(train_ds, batch_size=batch, shuffle=True, num_workers=0)
    val_loader = DataLoader(val_ds, batch_size=batch, shuffle=False, num_workers=0)

    class TinyCNN(nn.Module):
        def __init__(self, n_class: int) -> None:
            super().__init__()
            self.net = nn.Sequential(
                nn.Conv2d(3, 32, 3, padding=1), nn.BatchNorm2d(32), nn.ReLU(),
                nn.MaxPool2d(2),
                nn.Conv2d(32, 64, 3, padding=1), nn.BatchNorm2d(64), nn.ReLU(),
                nn.MaxPool2d(2),
                nn.Conv2d(64, 128, 3, padding=1), nn.BatchNorm2d(128), nn.ReLU(),
                nn.AdaptiveAvgPool2d(1), nn.Flatten(), nn.Dropout(0.25),
                nn.Linear(128, n_class),
            )

        def forward(self, x):
            return self.net(x)

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model = TinyCNN(len(labels)).to(device)
    counts = np.bincount(y_all[np.array(train_idx, dtype=np.int64)], minlength=len(labels))
    w = (len(train_idx) / (len(labels) * np.maximum(counts, 1))).astype(np.float32)
    if sad_weight != 1.0 and "难过" in labels:
        w[labels.index("难过")] *= float(sad_weight)
    criterion = nn.CrossEntropyLoss(weight=torch.tensor(w, dtype=torch.float32).to(device))
    opt = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=1e-4)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=max(1, epochs))
    best_acc, best_state = -1.0, None
    history = []
    for epoch in range(1, epochs + 1):
        model.train()
        total = 0
        for xb, yb in train_loader:
            xb, yb = xb.to(device), yb.to(device)
            opt.zero_grad()
            loss = criterion(model(xb), yb)
            loss.backward()
            opt.step()
            total += int(yb.numel())
        sched.step()
        model.eval()
        correct = 0
        n = 0
        with torch.no_grad():
            for xb, yb in val_loader:
                xb, yb = xb.to(device), yb.to(device)
                pred = model(xb).argmax(dim=1)
                correct += int((pred == yb).sum())
                n += int(yb.numel())
        acc = correct / max(1, n) if n else float("nan")
        history.append(acc)
        if n and acc >= best_acc:
            best_acc = acc
            best_state = {k: v.detach().cpu().clone() for k, v in model.state_dict().items()}
    if best_state is not None:
        model.load_state_dict(best_state)
    # 指标
    model.eval()
    preds, trues = [], []
    with torch.no_grad():
        for xb, yb in val_loader:
            p = model(xb.to(device)).argmax(dim=1).cpu().tolist()
            preds.extend(p)
            trues.extend(yb.tolist())
    met = _metrics(trues, preds, labels) if trues else {"accuracy": None, "total": 0,
                                                       "confusion": [], "per_class": {}}
    met["history"] = [None if math.isnan(x) else round(x, 4) for x in history]
    bundle = {
        "state_dict": model.cpu().state_dict(),
        "labels": labels,
        "input_size": 112,
        "arch": "tinycnn",
    }
    return bundle, met


# --------------------------------------------------------------------------- #
# 合成数据（只给 --self-test，不冒充真实效果）
# --------------------------------------------------------------------------- #
def synthetic_images(n_per: int = 12, size: int = 112,
                     labels: list[str] | None = None,
                     seed: int = 7) -> tuple[np.ndarray, list[str]]:
    labels = labels or ["中性", "开心"]
    rng = np.random.default_rng(seed)
    imgs = []
    for i in range(len(labels)):
        for _ in range(n_per):
            img = np.full((size, size, 3), 120 + i * 20, dtype=np.uint8)
            center = (size // 2 + int(rng.integers(-6, 7)), size // 2 + int(rng.integers(-6, 7)))
            color = (40, 40, 230) if i == 0 else (230, 120, 40)
            cv2.circle(img, center, size // 5, color, -1)
            img = np.clip(img.astype(np.int16) + rng.integers(-12, 13, img.shape), 0, 255).astype(np.uint8)
            imgs.append(img)
    return np.stack(imgs), labels


_RECORD_LABELS: list[dict] = []  # 由 main 设置；numpy 训练用它取标签


def _set_record_labels(records: list[dict]) -> None:
    global _RECORD_LABELS
    _RECORD_LABELS = records


def _save_numpy(model: dict, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(out_path,
                        W1=model["W1"], b1=model["b1"],
                        W2=model["W2"], b2=model["b2"],
                        labels=model["labels"], input_size=np.array(model["input_size"]))


def _self_test() -> int:
    """合成数据只验训练循环和保存/加载，不作为真实准确率。"""
    print("=" * 62)
    print("训练链路自检（合成图，不打开摄像头）")
    print("=" * 62)
    imgs, labels = synthetic_images(8, labels=["中性", "开心"])
    records = []
    for i, _ in enumerate(imgs):
        records.append({"label": labels[i % len(labels)], "session": f"s{i // 2}",
                        "path": f"synthetic_{i}.jpg"})
    _set_record_labels(records)
    train_idx, val_idx = split_indices(records, val_ratio=0.25, seed=3)
    print(f"  合成样本 {len(records)} 张，训练 {len(train_idx)} / 验证 {len(val_idx)}")
    model, met = train_numpy(imgs, labels, train_idx, val_idx,
                             epochs=8, hidden=32, batch=8, lr=0.05, seed=3)
    with tempfile.TemporaryDirectory(prefix="expr_train_") as td:
        out = Path(td) / "expression_mlp.npz"
        _save_numpy(model, out)
        from app.expression import ExpressionRecognizer
        er = ExpressionRecognizer(out)
        pred = er.predict(imgs[0]) if er.available else None
        print(f"  模型 {out.name} {out.stat().st_size} bytes；加载 "
              f"{'OK' if er.available else 'FAIL'}；预测 "
              f"{(pred or {}).get('label')} / {(pred or {}).get('confidence')}")
        ok = er.available and pred is not None and 0 <= pred["confidence"] <= 1
    print(f"  验证准确率 {met.get('accuracy')}（合成图，仅验链路）")
    print("=== 训练链路通过 ===" if ok else "=== 训练链路失败 ===")
    return 0 if ok else 4


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="train_expression",
                                 description="表情图像模型训练（无数据时只验链路）")
    ap.add_argument("--data", default=str(C.VISION_DATA))
    ap.add_argument("--out", default=str(C.EXPRESSION_DIR))
    ap.add_argument("--backend", choices=["auto", "torch", "numpy"], default="auto")
    ap.add_argument("--epochs", type=int, default=60)
    ap.add_argument("--batch", type=int, default=32)
    ap.add_argument("--lr", type=float, default=None)
    ap.add_argument("--hidden", type=int, default=128)
    ap.add_argument("--val-ratio", type=float, default=0.2)
    ap.add_argument("--random-split", action="store_true",
                    help="仅诊断用：按类随机切，不按 session；相邻帧会泄漏，不能当验收")
    ap.add_argument("--preprocess", choices=["rgb", "gray", "clahe"], default="rgb",
                    help="输入归一化：rgb 原样；gray 灰度直方图均衡；clahe 灰度自适应均衡")
    ap.add_argument("--sad-weight", type=float, default=1.0,
                    help="难过类损失权重倍数（默认 1；用来解决难过被中性吞掉）")
    ap.add_argument("--holdout-session", default=None,
                    help="指定一个 session 完全留给验证；其余 session 全进训练")
    ap.add_argument("--only-sessions", default=None,
                    help="只使用这些 session（逗号分隔），用于四类小实验")
    ap.add_argument("--only-labels", default=None,
                    help="只使用这些标签（逗号分隔），用于三类兜底实验")
    ap.add_argument("--min-per-class", type=int, default=8)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--dry-run", action="store_true", help="只统计与切分，不训练")
    ap.add_argument("--self-test", action="store_true", help="合成图验训练链路")
    ap.add_argument("--require-torch", action="store_true")
    args = ap.parse_args(argv)

    if args.self_test:
        return _self_test()
    if args.require_torch and not torch_available():
        print("要求 torch，但当前 venv 没装；见 docs/视觉与表情训练.md 的安装命令。")
        return 2

    records = load_records(Path(args.data))
    if args.only_sessions:
        wanted = {x.strip() for x in args.only_sessions.split(",") if x.strip()}
        records = [r for r in records if str(r.get("session")) in wanted]
    if args.only_labels:
        wanted_labels = {x.strip() for x in args.only_labels.split(",") if x.strip()}
        records = [r for r in records if str(r.get("label")) in wanted_labels]
    if not records:
        print(f"没有数据：{Path(args.data) / 'faces_index.jsonl'}")
        print("先跑采集：tools/collect_face_data.py --label 开心 --allow-camera")
        return 2
    _set_record_labels(records)
    counts = class_counts(records)
    labels = [x for x in C.EXPRESSION_LABELS if x in counts] + \
             sorted(x for x in counts if x not in C.EXPRESSION_LABELS)
    print(f"数据 {len(records)} 张 / {len(labels)} 类：{counts}")
    if len(labels) < 2:
        print("至少需要 2 类才能训练。")
        return 2
    too_few = {k: v for k, v in counts.items() if v < args.min_per_class}
    if too_few:
        print(f"每类至少 {args.min_per_class} 张；还差：{too_few}")
        return 2
    imgs, kept = load_images(records)
    records = kept
    _set_record_labels(records)
    if args.holdout_session:
        val_idx = [i for i, r in enumerate(records)
                   if str(r.get("session")) == args.holdout_session]
        train_idx = [i for i, r in enumerate(records)
                     if str(r.get("session")) != args.holdout_session]
        if not val_idx or not train_idx:
            print(f"holdout session 没有数据或训练集为空：{args.holdout_session}")
            return 2
        mode = f"holdout={args.holdout_session}"
    else:
        train_idx, val_idx = split_indices(records, args.val_ratio, args.seed,
                                           group_by_session=not args.random_split)
        mode = "随机切分（诊断，有泄漏）" if args.random_split else "按 session 分组切分"
    print(f"训练 {len(train_idx)} / 验证 {len(val_idx)}（{mode}）")
    if args.dry_run:
        return 0

    backend = args.backend
    if backend == "auto":
        backend = "torch" if torch_available() else "numpy"
    print(f"后端：{backend}")
    t0 = time.time()
    if backend == "torch":
        model, met = train_torch(imgs, labels, train_idx, val_idx,
                                 epochs=args.epochs, batch=args.batch,
                                 lr=args.lr or 1e-3, seed=args.seed,
                                 preprocess=args.preprocess,
                                 sad_weight=args.sad_weight)
        out = Path(args.out) / "expression_torch.pt"
        out.parent.mkdir(parents=True, exist_ok=True)
        import torch
        torch.save(model, out)
        notes = "torch 小 CNN；推理接入需导出 ONNX 或直接在 torch 环境加载"
    else:
        model, met = train_numpy(imgs, labels, train_idx, val_idx,
                                 epochs=args.epochs, hidden=args.hidden,
                                 batch=args.batch, lr=args.lr or 0.03, seed=args.seed)
        out = Path(args.out) / "expression_mlp.npz"
        _save_numpy(model, out)
        notes = "无依赖 numpy 基线；已可直接被 app/expression.py 加载"
    elapsed = round(time.time() - t0, 2)
    report = {
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "backend": backend,
        "model": str(out),
        "data": str(Path(args.data)),
        "samples": len(records),
        "classes": counts,
        "train": len(train_idx),
        "val": len(val_idx),
        "epochs": args.epochs,
        "seed": args.seed,
        "preprocess": args.preprocess,
        "sad_weight": args.sad_weight,
        "elapsed_s": elapsed,
        "metrics": met,
        "notes": notes,
    }
    report_path = Path(args.out) / "train_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (Path(args.out) / "labels.json").write_text(
        json.dumps(labels, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"模型：{out}")
    print(f"验证准确率：{met.get('accuracy')}（样本 {met.get('total')}）")
    print(f"报告：{report_path}")
    if backend == "numpy":
        print("提示：没有 torch 时这是线性/单隐层基线，别拿它当深度模型效果；"
              "真训 CNN 需安装 torch。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
