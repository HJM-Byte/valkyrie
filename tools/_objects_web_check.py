"""临时验收：对 objects_web 下的网图跑物体检测，拼成对照图。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from app.objects import ObjectDetector, object_summary  # noqa: E402
from app.vision import imread_unicode  # noqa: E402

SRC = ROOT / "data" / "objects_web"
OUT = ROOT / "web" / "objects_web_check.jpg"
EXPECT = {
    "smartphone": ("cell phone", "手机"),
    "office_chair": ("chair", "椅子"),
    "wooden_stool": ("chair", "椅子/凳子"),
}

try:
    font = ImageFont.truetype(r"C:\Windows\Fonts\msyh.ttc", 16)
except Exception:
    font = None

rows = []
det = ObjectDetector()
for p in sorted(SRC.rglob("*.jpg")):
    img = imread_unicode(p)
    if img is None:
        continue
    ds = det.detect(img)
    expected = EXPECT.get(p.parent.name, ("", ""))
    hit = any(expected[0] == d["label"] or expected[1] in (d.get("name_zh"),) for d in ds)
    rows.append((p, img, ds, hit))
    print(f"{p.parent.name}/{p.name}: {object_summary(ds) or '无'} | expected={expected[1]} hit={hit}")

if not rows:
    raise SystemExit("没有网图可验收")

cell_w, cell_h = 300, 280
cols = 4
rows_n = (len(rows) + cols - 1) // cols
sheet = Image.new("RGB", (cell_w * cols, cell_h * rows_n), (20, 20, 24))
draw = ImageDraw.Draw(sheet)
for i, (p, img, ds, hit) in enumerate(rows):
    thumb = Image.fromarray(img[:, :, ::-1]).convert("RGB")
    thumb.thumbnail((cell_w - 12, cell_h - 70))
    x = (i % cols) * cell_w + 6
    y = (i // cols) * cell_h + 36
    sheet.paste(thumb, (x, y))
    text = f"{p.parent.name}/{p.name} | {object_summary(ds) or '无'} | {'OK' if hit else 'MISS'}"
    draw.text((x, (i // cols) * cell_h + 6), text[:52],
              fill=(120, 255, 120) if hit else (255, 160, 120), font=font)
sheet.save(OUT, quality=90)
print("OUT", OUT)
det.close()
