"""用已有采集的人脸建立用户底库，不打开摄像头。

默认用今天早些时候的 light1/light2 中性脸做底库，faces4c 做留出验证。
这不是“训练一个新的人脸模型”，是给 SFace 做注册/底库；所以不用 GPU。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config as C  # noqa: E402
from app.vision import FaceDB, FaceEmbedder, imread_unicode  # noqa: E402
from tools.collect_face_data import _quality, accept_quality  # noqa: E402


def load_rows(root: Path) -> list[dict]:
    index = root / "faces_index.jsonl"
    if not index.exists():
        raise SystemExit(f"没有索引: {index}")
    rows = []
    for line in index.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if rec.get("file") and rec.get("label"):
            rows.append(rec)
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default=os.environ.get("ROBOT_USER_NAME", "用户"))
    ap.add_argument("--enroll-sessions", default="20261029_light1,20261029_light2")
    ap.add_argument("--enroll-labels", default="中性")
    ap.add_argument("--verify-sessions", default="20261029_faces4c")
    ap.add_argument("--verify-labels", default="中性,开心,生气")
    ap.add_argument("--max-enroll", type=int, default=80)
    args = ap.parse_args()

    root = C.VISION_DATA
    rows = load_rows(root)
    labels = set(x.strip() for x in args.enroll_labels.split(",") if x.strip())
    sessions = set(x.strip() for x in args.enroll_sessions.split(",") if x.strip())
    pool = [r for r in rows if r.get("session") in sessions and r.get("label") in labels]
    pool.sort(key=lambda r: (str(r.get("session")), str(r.get("file"))))
    emb = FaceEmbedder()
    vecs = []
    used = []
    for rec in pool:
        img = imread_unicode(root / rec["file"])
        if img is None:
            continue
        q = _quality(img)
        ok, _ = accept_quality(q)
        if not ok:
            continue
        v = emb.embed_aligned(img)
        v = v / max(1e-9, float(np.linalg.norm(v)))
        vecs.append(v)
        used.append(str(rec["file"]))
        if len(vecs) >= args.max_enroll:
            break
    if not vecs:
        raise SystemExit("没有可用底库人脸")
    center = np.mean(np.stack(vecs), axis=0)
    center = center / max(1e-9, float(np.linalg.norm(center)))
    db = FaceDB()
    db.enroll(args.name, center)
    print(f"底库: {args.name}，{len(vecs)} 张，{len(used)} 条记录")
    print(f"数据库: {db.path}")

    # 留出验证：另一 session 的同一人
    vlabels = set(x.strip() for x in args.verify_labels.split(",") if x.strip())
    vsessions = set(x.strip() for x in args.verify_sessions.split(",") if x.strip())
    verify = [r for r in rows if r.get("session") in vsessions and r.get("label") in vlabels]
    scores = []
    low = []
    for rec in verify:
        img = imread_unicode(root / rec["file"])
        if img is None:
            continue
        q = _quality(img)
        if not accept_quality(q)[0]:
            continue
        v = emb.embed_aligned(img)
        s = FaceEmbedder.score(v, center)
        scores.append(s)
        if s < C.FACE_MATCH_THRESHOLD:
            low.append((s, rec["file"]))
    if scores:
        scores_arr = np.array(scores)
        print(f"留出验证: {len(scores)} 张，均值 {scores_arr.mean():.4f}，"
              f"最低 {scores_arr.min():.4f}，低于阈值 {len(low)} 张")
        print(f"阈值: {C.FACE_MATCH_THRESHOLD}")
        for s, f in sorted(low)[:10]:
            print(f"  低分 {s:.4f} {f}")
        if scores_arr.min() < C.FACE_MATCH_THRESHOLD:
            print("警告：有低分样本，考虑增加底库或换更干净的验证集。")
            return 4
    else:
        print("没有可用验证样本。")
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
