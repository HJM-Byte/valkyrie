# -*- coding: utf-8 -*-
"""生成瓦尔基里身体底板 v1 的框图与机械布局图（Pillow，无网络依赖）。

运行：
  python make_diagrams.py
输出：system_block.png / board_top.png / board_bottom.png
"""
from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent
FONT = r"C:\Windows\Fonts\msyh.ttc"
FONT_B = r"C:\Windows\Fonts\msyhbd.ttc"

INK = (28, 34, 44)
MUTED = (94, 108, 124)
PAPER = (247, 249, 252)
LINE = (170, 184, 200)
BLUE = (42, 112, 190)
TEAL = (22, 142, 135)
ORANGE = (210, 116, 42)
RED = (188, 62, 62)
GRAY = (120, 132, 148)


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(FONT_B if bold else FONT, size)


def text(draw, xy, s, size=22, fill=INK, bold=False, anchor="la"):
    draw.text(xy, s, font=font(size, bold), fill=fill, anchor=anchor)


def rbox(draw, box, fill, outline=None, width=2, radius=14):
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def arrow(draw, start, end, fill=BLUE, width=4, head=12):
    draw.line([start, end], fill=fill, width=width)
    ang = math.atan2(end[1] - start[1], end[0] - start[0])
    p1 = (end[0] - head * math.cos(ang - 0.45), end[1] - head * math.sin(ang - 0.45))
    p2 = (end[0] - head * math.cos(ang + 0.45), end[1] - head * math.sin(ang + 0.45))
    draw.polygon([end, p1, p2], fill=fill)


def make_system_block() -> None:
    W, H = 1600, 980
    im = Image.new("RGB", (W, H), PAPER)
    d = ImageDraw.Draw(im)

    text(d, (70, 52), "瓦尔基里 · 单片机身体 v1", 48, INK, True)
    text(d, (72, 116), "ESP32-S3 只做身体：摄像头 / 麦克风 / 喇叭 / 屏幕 / WiFi；大脑仍在 PC。", 24, MUTED)

    rbox(d, (80, 185, 470, 760), (235, 243, 252), BLUE, 3, 20)
    text(d, (110, 215), "PC · 大脑", 30, BLUE, True)
    text(d, (110, 262), "现有 Python 程序", 22, INK)
    for i, s in enumerate([
        "Chat.turn / 记忆 / R·θ",
        "faster-whisper 语音识别",
        "Yunet + SFace 认脸",
        "TTS（edge / 本地 VITS）",
        "网页版：星图 / 面板 / 对话",
    ]):
        text(d, (112, 315 + i * 54), "· " + s, 22, INK)

    rbox(d, (560, 405, 1030, 575), (232, 248, 246), TEAL, 3, 18)
    text(d, (588, 432), "WiFi 2.4 GHz · WebSocket", 30, TEAL, True)
    text(d, (588, 482), "音频 16 kHz PCM16 / 30 ms", 22, INK)
    text(d, (588, 520), "相机 JPEG 快照 · 面部状态 JSON", 22, INK)
    arrow(d, (470, 470), (560, 470), TEAL, 5)
    arrow(d, (1030, 470), (1110, 470), TEAL, 5)

    rbox(d, (1110, 150, 1510, 840), (255, 247, 235), ORANGE, 3, 20)
    text(d, (1140, 182), "身体 · ESP32-S3", 30, ORANGE, True)
    text(d, (1140, 226), "DevKitC-1 N8R8", 22, INK)

    blocks = [
        ("OV2640 摄像头", "DVP 8-bit · 640×480 JPEG"),
        ("INMP441 麦克风", "I2S · 16 kHz 单声道"),
        ("MAX98357A 功放", "I2S · 8Ω 2W 喇叭"),
        ("1.28\" 圆屏", "GC9A01 · 240×240 · 脸"),
        ("IP5306 电源", "5V 2A · 2000mAh"),
    ]
    for i, (title, sub) in enumerate(blocks):
        top = 285 + i * 102
        rbox(d, (1140, top, 1480, top + 78), (255, 255, 255), (228, 205, 176), 2, 12)
        text(d, (1160, top + 13), title, 23, INK, True)
        text(d, (1160, top + 43), sub, 18, MUTED)

    for i in range(4):
        top = 363 + i * 102
        d.line([(1310, top), (1310, top + 24)], fill=(210, 150, 90), width=2)
    text(d, (1310, 810), "统一 5V / 3.3V 供电", 17, MUTED, anchor="ma")

    rbox(d, (80, 835, 1030, 930), (255, 255, 255), LINE, 2, 14)
    text(d, (106, 856), "边界：ESP32-S3 跑不动 4B 大模型。完全脱离 PC 需要换 Jetson Orin Nano / RPi 5；这块身体板可以原样保留。", 20, INK)

    im.save(ROOT / "system_block.png")
    print("wrote", ROOT / "system_block.png")


def draw_board(which: str) -> None:
    S = 8
    M = 90
    W = H = 1000
    im = Image.new("RGB", (W, H), PAPER)
    d = ImageDraw.Draw(im)
    cx, cy = M + 50 * S, M + 50 * S

    def p(x_mm, y_mm):
        return (M + x_mm * S, M + (100 - y_mm) * S)

    def box(x1, y1, x2, y2):
        a, b = p(x1, y1), p(x2, y2)
        return [min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1])]

    def ell(x, y, rx, ry):
        return box(x - rx, y - ry, x + rx, y + ry)

    title = "顶面 · 模块与接口" if which == "top" else "底面 · 安装与走线"
    text(d, (40, 24), "瓦尔基里身体 v1 · " + title, 30, INK, True)
    text(d, (40, 62), "100 mm 圆形 · 2 层 · 1.6 mm · 3×M3", 19, MUTED)

    d.ellipse(ell(50, 50, 50, 50), fill=(250, 252, 255), outline=INK, width=3)
    for i in range(0, 101, 10):
        d.line([p(i, 0), p(i, 100)], fill=(235, 240, 246), width=1)
        d.line([p(0, i), p(100, i)], fill=(235, 240, 246), width=1)

    # USB notch on left for the DevKitC's two USB ports
    d.rectangle(box(0, 15, 6, 25), fill=PAPER, outline=INK, width=3)
    text(d, p(1.2, 20), "USB\n缺口", 12, RED, anchor="lm")

    if which == "top":
        # display module (40.4 x 37.5) centered at 50,57
        d.rounded_rectangle(box(29.8, 38.25, 70.2, 75.75), radius=8,
                            fill=(26, 34, 48), outline=BLUE, width=3)
        d.ellipse(ell(50, 57, 16.2, 16.2), outline=(90, 170, 240), width=2)
        text(d, p(50, 57), "1.28\" 圆屏\n240×240", 18, (220, 235, 250), anchor="mm")
        text(d, p(50, 33.2), "显示排针 1×8 · 朝下", 14, BLUE, anchor="ma")

        # camera module (2x9 header, lens)
        d.rounded_rectangle(box(38.5, 76, 61.5, 97), radius=7,
                            fill=(40, 56, 76), outline=ORANGE, width=3)
        d.ellipse(ell(50, 88, 6, 6), fill=(14, 22, 34), outline=(140, 190, 240), width=2)
        d.ellipse(ell(50, 88, 2, 2), fill=(70, 130, 210))
        text(d, p(50, 78.5), "OV2640 · 2×9", 14, (255, 220, 180), anchor="ma")
        text(d, p(50, 96), "2×9 排针", 12, ORANGE, anchor="ma")

        # mic
        d.rounded_rectangle(box(14, 58, 28, 68), radius=5,
                            fill=(232, 248, 246), outline=TEAL, width=3)
        d.ellipse(ell(21, 63, 1.1, 1.1), fill=(20, 30, 40), outline=TEAL, width=2)
        text(d, p(21, 63), "INMP441", 13, TEAL, anchor="mm")

        # amp
        d.rounded_rectangle(box(73, 45, 91, 59), radius=5,
                            fill=(255, 241, 226), outline=ORANGE, width=3)
        text(d, p(82, 52), "MAX98357A", 13, ORANGE, anchor="mm")

        for bx, by, bw, bh, label, color in [
            (20, 5, 16, 8, "5V 输入\nJST-PH", RED),
            (58, 5, 10, 8, "喇叭\nJST-PH", ORANGE),
            (42, 18, 14, 8, "调试\nUART", GRAY),
        ]:
            d.rounded_rectangle(box(bx, by, bx + bw, by + bh), radius=4,
                                fill=(255, 255, 255), outline=color, width=2)
            text(d, p(bx + bw / 2, by + bh / 2), label, 11, color, anchor="mm")

        for bx, by, label in [(8, 55, "音量"), (8, 43, "静音")]:
            d.ellipse(ell(bx, by, 3, 3), fill=(60, 70, 85), outline=INK, width=2)
            text(d, p(bx + 7, by), label, 14, INK)

        # antenna keepout, right of DevKitC
        keep = box(68.5, 14, 88, 35)
        d.rectangle(keep, fill=None, outline=RED, width=3)
        for k in range(0, 9):
            x = keep[0] + (keep[2] - keep[0]) * k / 8
            d.line([(x, keep[3]), (min(x + 14, keep[2]), keep[1])], fill=(245, 190, 190), width=1)
        text(d, p(78, 24), "天线禁布区\n15×20 mm", 12, RED, anchor="mm")

        for hx, hy in [(11.9, 72.0), (88.1, 72.0), (50.0, 6.0)]:
            d.ellipse(ell(hx, hy, 1.8, 1.8), fill=(255, 255, 255), outline=INK, width=2)
            d.ellipse(ell(hx, hy, 1.1, 1.1), fill=(230, 235, 242), outline=GRAY, width=1)
            text(d, p(hx, hy - 6.5), "M3", 10, MUTED, anchor="ma")

        d.line([p(0, 100), p(100, 100)], fill=GRAY, width=1)
        text(d, (M + 50 * S, M - 12), "Ø100 mm", 16, MUTED, anchor="ma")

    else:
        d.rounded_rectangle(box(5, 7, 68.5, 35), radius=5,
                            fill=(238, 242, 248), outline=INK, width=3)
        text(d, p(36.7, 21), "ESP32-S3-DevKitC-1\n2×22 母座（背面）", 17, INK, anchor="mm")
        d.rectangle(box(68.5, 7, 88, 35), outline=RED, width=2)
        text(d, p(78.2, 21), "PCB 天线\n朝右，禁铺铜", 13, RED, anchor="mm")

        d.rounded_rectangle(box(15, 58, 45, 74), radius=4,
                            fill=(255, 245, 235), outline=ORANGE, width=2)
        text(d, p(30, 66), "IP5306 5V 模块\n背面双面胶固定", 12, ORANGE, anchor="mm")

        d.rounded_rectangle(box(50, 62, 82, 86), radius=6,
                            fill=(240, 248, 240), outline=(60, 140, 80), width=2)
        text(d, p(66, 74), "LiPo 3.7V 2000mAh\n带保护板", 12, (40, 120, 70), anchor="mm")
        text(d, p(50, 92), "背面朝里：所有排针/模块都不要超过 12 mm", 15, MUTED, anchor="ma")

    text(d, (20, 930), "注：此图为机械摆位参考，不是已布线的 Gerber；走线按接线表在 KiCad/立创 EDA 完成。", 14, MUTED)
    im.save(ROOT / f"board_{which}.png")
    print("wrote", ROOT / f"board_{which}.png")


if __name__ == "__main__":
    make_system_block()
    draw_board("top")
    draw_board("bottom")
