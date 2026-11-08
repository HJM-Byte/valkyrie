"""临时排查：把各 session 每类的前两张人脸拼成一张对照图。"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "data" / "vision" / "faces_index.jsonl"
OUT = ROOT / "web" / "vision_contact.jpg"
SESSIONS = ["20261029_faces4b", "20261029_sad4c", "20261029_sad4d"]
LABELS = ["中性", "开心", "难过", "生气", "惊讶", "害怕", "厌恶", "疲惫"]

rows = [json.loads(x) for x in INDEX.read_text(encoding="utf-8").splitlines() if x.strip()]
by = defaultdict(list)
for r in rows:
    by[(r.get("session"), r.get("label"))].append(ROOT / "data" / "vision" / r["file"])

cell_w, cell_h = 124, 136
sheet = Image.new("RGB", (cell_w * (len(SESSIONS) + 1), cell_h * len(LABELS)), (24, 24, 28))
draw = ImageDraw.Draw(sheet)
for j, s in enumerate(SESSIONS):
    draw.text((cell_w * (j + 1) + 2, 4), s.replace("20261029_", ""), fill=(220, 220, 220))
for i, label in enumerate(LABELS):
    draw.text((2, cell_h * i + 4), label, fill=(220, 220, 220))
    for j, s in enumerate(SESSIONS):
        files = by.get((s, label), [])[:2]
        for k, p in enumerate(files):
            try:
                img = Image.open(p).convert("RGB").resize((112, 112))
            except Exception:
                continue
            x = cell_w * (j + 1) + k * 56
            y = cell_h * i + 22
            sheet.paste(img.resize((56, 56)), (x, y))
sheet.save(OUT, quality=92)
print(OUT)
