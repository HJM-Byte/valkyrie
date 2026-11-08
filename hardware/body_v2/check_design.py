# -*- coding: utf-8 -*-
"""v2 设计包自检：几何、引脚、I2C、BOM/接线、关键文字。

不替代 DRC/实物复核；它只拦住“图与表互相矛盾、器件跑出板框、
评审已经点过的硬伤又回来”这类问题。
"""
from __future__ import annotations

import csv
import math
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DOC = ROOT.parents[1] / "docs" / "单片机身体方案_v2_20261101.md"

FAIL: list[str] = []
PASS: list[str] = []


def check(ok: bool, name: str, detail: str = "") -> None:
    if ok:
        PASS.append(name)
    else:
        FAIL.append(f"{name}: {detail}")


# ---------------------------------------------------------------- geometry
# 顶面/前面板共用坐标；LED 孔必须和板载 LED 同坐标，JST/连接器必须在 r<=46。
LEDS = [(30, 28), (40, 28), (50, 28)]
BUTTONS = [(25, 15), (37, 15), (49, 15)]
CONNECTORS = [
    ("5V JST", 12, 25, 12, 7),
    ("喇叭 JST", 78, 32, 12, 7),
    ("UART", 8, 33, 16, 6),
]
KEEPOUT = (63.0, 10.0, 78.0, 38.0)  # x1,y1,x2,y2
BOARD_R = 60.0
TARGET_R = 56.0

for x, y in LEDS + BUTTONS:
    check((x - 50) ** 2 + (y - 50) ** 2 <= TARGET_R**2,
          f"LED/按键在 r<=46 内 ({x},{y})")

for name, x, y, w, h in CONNECTORS:
    corners = [(x, y), (x + w, y), (x, y + h), (x + w, y + h)]
    rmax = max(math.hypot(cx - 50, cy - 50) for cx, cy in corners)
    check(rmax <= TARGET_R, f"{name} 角点 r<=46", f"rmax={rmax:.2f}")

# 天线禁布区不能压到顶面按键/LED/JST/屏幕；只检查已知矩形/圆
X1, Y1, X2, Y2 = KEEPOUT


def circle_hits(cx, cy, r):
    nx = min(max(cx, X1), X2)
    ny = min(max(cy, Y1), Y2)
    return math.hypot(cx - nx, cy - ny) < r


for i, (x, y) in enumerate(LEDS):
    check(not circle_hits(x, y, 1.6), f"LED{i} 不进天线禁布", f"({x},{y})")
for i, (x, y) in enumerate(BUTTONS):
    check(not circle_hits(x, y, 3.1), f"按键{i} 不进天线禁布", f"({x},{y})")
for name, x, y, w, h in CONNECTORS:
    overlap = not (x + w <= X1 or x >= X2 or y + h <= Y1 or y >= Y2)
    check(not overlap, f"{name} 不进天线禁布")

# 屏幕/摄像头/功放/麦克风/电池/模块的包围盒也不能压禁布
BBOXES = [
    ("屏幕", 29.8, 38.25, 70.2, 75.75),
    ("摄像头", 38.5, 76, 61.5, 97),
    ("麦克风", 14, 58, 28, 68),
    ("功放", 73, 45, 91, 59),
    ("电池", 12, 60, 52, 86),
    ("IP5306", 55, 40, 86, 58),
]

# DevKitC 2x22 模块必须完整落在圆内；它和天线禁布区是故意重叠的，
# 所以只查四个角，不放进上面的 BBOXES。
module_corners = [(10, 10), (78, 10), (10, 38), (78, 38)]
module_rmax = max(math.hypot(x - 50, y - 50) for x, y in module_corners)
check(module_rmax <= BOARD_R, "DevKitC 模块四角在板内", f"rmax={module_rmax:.2f}")
for name, x1, y1, x2, y2 in BBOXES:
    overlap = not (x2 <= X1 or x1 >= X2 or y2 <= Y1 or y1 >= Y2)
    check(not overlap, f"{name} 不进天线禁布")

# 所有安装孔/连接器/模块的角点必须在圆内；M3 孔按半径 1.8 算
for hx, hy in [(11.9, 72), (88.1, 72), (50, 6)]:
    check(math.hypot(hx - 50, hy - 50) + 1.8 <= BOARD_R,
          f"M3 孔在板内 ({hx},{hy})")

# ---------------------------------------------------------------- pins
sys.path.insert(0, str(ROOT))
import make_tables  # noqa: E402

pins = []
for row in make_tables.PINS:
    token = row[0]
    for part in token.replace("GPIO", "").split("/"):
        part = part.strip()
        if part.isdigit():
            pins.append(int(part))
forbidden = {19, 20, 35, 36, 37, 38, 45, 46}
used = [p for p in pins if p not in forbidden]
check(len(used) == 28, "实际使用 28 个 GPIO", f"count={len(used)}")
check(len(set(used)) == 28, "28 个实际 GPIO 无重复", f"unique={len(set(used))}")
check(not (set(used) & forbidden), "未占用 USB/PSRAM/strapping/RGB 脚",
      f"bad={sorted(set(used) & forbidden)}")

# ---------------------------------------------------------------- csv
bom_path = ROOT / "BOM_采购.csv"
wire_path = ROOT / "接线表.csv"
pin_path = ROOT / "引脚表.csv"
for p in (bom_path, wire_path, pin_path):
    check(p.exists() and p.stat().st_size > 0, f"{p.name} 存在且非空")

bom = list(csv.reader(bom_path.open(encoding="utf-8-sig")))
wire = list(csv.reader(wire_path.open(encoding="utf-8-sig")))
bom_text = "\n".join(",".join(r) for r in bom)
wire_text = "\n".join(",".join(r) for r in wire)

check(len(bom) - 1 == 43, "BOM 43 行", f"rows={len(bom)-1}")
check(len(wire) - 1 == 107, "接线表 107 行", f"rows={len(wire)-1}")
check("PTC 2A" in bom_text, "BOM 使用 PTC 2A")
check("SS34" not in bom_text, "BOM 不再列 SS34")
check("BZT52" not in bom_text, "BOM 不再列 5.1V 稳压管")
check("备 PCF8574" not in bom_text, "BOM 不再把 PCF8574 当替代")
check("Q3" in bom_text and "AMP_SD" in wire_text, "BOM/接线包含 Q3 低边关断")
check("100k/150k" in wire_text, "P5 使用 100k:150k 分压")
check("LCD_RST" in wire_text and "EN（RST）" in wire_text, "LCD RST 接 EN")
check("AMP_SD_GATE" in wire_text, "P6 走 Q3 栅极")
check("100k → BSS138" in wire_text, "KEY 串 100k 到栅极")

# ---------------------------------------------------------------- panel svg
svg_path = ROOT / "面板开孔图.svg"
check(svg_path.exists() and svg_path.stat().st_size > 0, "前面板 SVG 存在")
if svg_path.exists():
    tree = ET.parse(svg_path)
    circles = tree.getroot().findall(".//{http://www.w3.org/2000/svg}circle")
    led_circles = []
    for c in circles:
        cx, cy, r = float(c.get("cx")), float(c.get("cy")), float(c.get("r"))
        # 面板坐标 y 向上；SVG 已做 104-(y+2) 变换，LED y=28 -> cy=74
        if abs(cy - 84.0) < 0.01 and abs(r - 1.6) < 0.01:
            led_circles.append((cx, cy))
    expected = [(x + 12, 84.0) for x, _ in LEDS]
    check(len(led_circles) == 3 and all(
        any(abs(cx - ex) < 0.01 and abs(cy - ey) < 0.01 for cx, cy in led_circles)
        for ex, ey in expected),
        "SVG 三只 LED 孔与板载同坐标", f"found={led_circles}")

# ---------------------------------------------------------------- text
doc_text = DOC.read_text(encoding="utf-8")
checks = [
    ("Ø120 板", "Ø120" in doc_text and "Ø124" in doc_text),
    ("天线 15×28", "15×28" in doc_text),
    ("KEY 100k 串联", "R5 100k" in doc_text),
    ("无 5.1V 稳压管", "删掉 5.1V 稳压管" in doc_text),
    ("SD 不是 GAIN", "多电平输入" in doc_text and "GAIN" in doc_text),
    ("P7 软件状态", "软件状态灯" in doc_text),
    ("CS/RST EN", "LCD `RST`" in doc_text and "EN" in doc_text),
    ("后壳 DevKitC USB", "DevKitC 双 USB-C 缺口" in doc_text),
]
for name, ok in checks:
    check(ok, f"文档包含 {name}")

# 图文件存在
for name in [
    "system_block_v2.png", "power_tree_v2.png", "power_key_circuit_v2.png",
    "control_wiring_v2.png", "board_top_v2.png", "board_bottom_v2.png",
    "panel_cutout_v2.png", "back_panel_cutout_v2.png", "button_fsm_v2.png",
    "power_key_timing_v2.png",
]:
    p = ROOT / name
    check(p.exists() and p.stat().st_size > 0, f"图存在 {name}")

print(f"PASS {len(PASS)}  FAIL {len(FAIL)}")
for f in FAIL:
    print("FAIL:", f)
if FAIL:
    raise SystemExit(1)
