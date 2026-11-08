"""物体识别与人脸底库的离线自检：不打开摄像头。

用公开网图验证手机/凳子，用已有采集验证“之前那张脸”。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config as C  # noqa: E402
from app.objects import ObjectDetector  # noqa: E402
from app.vision import FaceDB, FaceEmbedder, imread_unicode  # noqa: E402

WEB = ROOT / "data" / "objects_web"


def main() -> int:
    checks = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append(ok)
        print(f"[{'OK  ' if ok else 'FAIL'}] {name:18s} {detail}")

    det = ObjectDetector()
    phone = WEB / "smartphone" / "phone_d.jpg"
    if phone.exists():
        got = det.detect(imread_unicode(phone))
        phone_hit = next((d for d in got if d["label"] == "cell phone"), None)
        check("phone_detect", phone_hit is not None,
              f"cell phone {phone_hit['score'] if phone_hit else 0}")
    else:
        check("phone_detect", False, f"缺样本 {phone}")

    stool = WEB / "stool" / "stool_a.jpg"
    if stool.exists():
        got = det.detect(imread_unicode(stool))
        chair_hit = next((d for d in got if d["label"] == "chair"), None)
        check("stool_as_chair", chair_hit is not None,
              f"chair {chair_hit['score'] if chair_hit else 0}（COCO 无 stool 独立类）")
    else:
        check("stool_as_chair", False, f"缺样本 {stool}")

    db = FaceDB()
    check("face_db", bool(db.entries), f"{list(db.entries)} @ {db.path}")
    face = C.VISION_DATA / "faces" / "中性" / "20261029_light1_00001.jpg"
    if face.exists() and db.entries:
        emb = FaceEmbedder()
        name, score = db.match(emb.embed_aligned(imread_unicode(face)))
        check("previous_face", name is not None,
              f"match={name} score={score:.4f} threshold={C.FACE_MATCH_THRESHOLD}")
    else:
        check("previous_face", False, "缺底库或验证图")

    print(f"=== {sum(checks)}/{len(checks)} 通过 ===")
    return 0 if all(checks) else 4


if __name__ == "__main__":
    raise SystemExit(main())
