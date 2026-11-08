# -*- coding: utf-8 -*-
"""生成瓦尔基里身体 v2 的框图、电源树、控制接线、机械摆位和开孔图。

运行：
  python make_diagrams.py

输出均为“接线/布局级”图纸，不是已经布好线的 Gerber。
"""
from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent
FONT = r"C:\Windows\Fonts\msyh.ttc"
FONT_B = r"C:\Windows\Fonts\msyhbd.ttc"

INK = (26, 32, 42)
MUTED = (92, 106, 122)
PAPER = (247, 249, 252)
LINE = (167, 181, 198)
BLUE = (38, 106, 184)
TEAL = (18, 137, 130)
ORANGE = (205, 110, 34)
RED = (187, 55, 55)
GREEN = (52, 135, 78)
PURPLE = (112, 78, 166)
GRAY = (112, 124, 140)
WHITE = (255, 255, 255)


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(FONT_B if bold else FONT, size)


def text(d, xy, s, size=22, fill=INK, bold=False, anchor="la"):
    d.text(xy, s, font=font(size, bold), fill=fill, anchor=anchor)


def rbox(d, box, fill, outline=None, width=2, radius=14):
    d.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def arrow(d, start, end, fill=BLUE, width=4, head=13):
    d.line([start, end], fill=fill, width=width)
    ang = math.atan2(end[1] - start[1], end[0] - start[0])
    p1 = (end[0] - head * math.cos(ang - 0.45), end[1] - head * math.sin(ang - 0.45))
    p2 = (end[0] - head * math.cos(ang + 0.45), end[1] - head * math.sin(ang + 0.45))
    d.polygon([end, p1, p2], fill=fill)


def chip(d, box, title, sub="", fill=(238, 245, 253), outline=BLUE, title_color=INK,
         radius=10, title_size=18, sub_size=14):
    rbox(d, box, fill, outline, 2, radius)
    x1, y1, x2, y2 = box
    if sub:
        text(d, ((x1 + x2) / 2, y1 + (y2 - y1) * 0.36), title, title_size, title_color, True, "mm")
        text(d, ((x1 + x2) / 2, y1 + (y2 - y1) * 0.68), sub, sub_size, MUTED, False, "mm")
    else:
        text(d, ((x1 + x2) / 2, (y1 + y2) / 2), title, title_size, title_color, True, "mm")


def title(d, main, sub=""):
    text(d, (54, 38), main, 42, INK, True)
    if sub:
        text(d, (56, 94), sub, 21, MUTED)


def note(d, xy, s, size=16, color=MUTED):
    text(d, xy, s, size, color)


# ---------------------------------------------------------------- system block
def make_system_block():
    W, H = 1800, 1120
    im = Image.new("RGB", (W, H), PAPER)
    d = ImageDraw.Draw(im)
    title(d, "瓦尔基里 · 单片机身体 v2", "保留 v1 的看/听/说/脸/WiFi/电池，新增电源键、主动消息键、语音键；大脑仍在 PC。")

    # PC
    rbox(d, (60, 180, 520, 930), (234, 242, 252), BLUE, 3, 22)
    text(d, (92, 212), "PC · 大脑", 30, BLUE, True)
    text(d, (94, 254), "现有 Python 程序", 20, INK)
    pc_items = [
        ("Chat.turn / 记忆 / R·θ", 320),
        ("faster-whisper 语音识别", 372),
        ("YuNet + SFace 认脸", 424),
        ("TTS：Breeze / edge / VITS", 476),
        ("pocket_server 手机服务", 528),
        ("proactive.py 主动消息", 580),
        ("body_link.py 身体桥", 632),
    ]
    for s, y in pc_items:
        text(d, (94, y), "· " + s, 20, INK)
    rbox(d, (84, 700, 496, 790), (255, 251, 240), ORANGE, 2, 12)
    text(d, (102, 718), "新增运行时开关", 19, ORANGE, True)
    text(d, (102, 752), "voice_enabled / proactive_enabled", 17, INK)

    # WiFi
    rbox(d, (640, 430, 1010, 650), (232, 249, 246), TEAL, 3, 20)
    text(d, (668, 458), "WiFi 2.4 GHz", 28, TEAL, True)
    text(d, (668, 500), "WebSocket 8768", 19, INK)
    text(d, (668, 532), "UDP 发现 8769", 19, INK)
    text(d, (668, 572), "音频 16 kHz PCM16 / 30 ms", 17, MUTED)
    text(d, (668, 602), "JPEG 快照 · 状态/事件 JSON", 17, MUTED)
    arrow(d, (520, 540), (640, 540), TEAL, 5)
    arrow(d, (1010, 540), (1080, 540), TEAL, 5)

    # body
    rbox(d, (1080, 150, 1740, 980), (255, 247, 235), ORANGE, 3, 22)
    text(d, (1110, 182), "身体 · ESP32-S3", 31, ORANGE, True)
    text(d, (1112, 224), "DevKitC-1 N8R8 v1.1", 19, INK)
    blocks = [
        (1114, 268, 1400, 350, "OV2640 摄像头", "DVP 8-bit · 640×480 JPEG", ORANGE),
        (1420, 268, 1706, 350, "1.28\" GC9A01 圆屏", "240×240 · 脸 / 状态图标", BLUE),
        (1114, 378, 1400, 460, "INMP441 麦克风", "I2S · 16 kHz 单声道", TEAL),
        (1420, 378, 1706, 460, "MAX98357A 功放", "I2S · 8Ω 2W · SD 可关断", GREEN),
        (1114, 500, 1400, 600, "TCA9534 扩展", "2 键 / 3 LED / AMP_SD / 5V 检测", PURPLE),
        (1420, 500, 1706, 600, "INA226 电量计", "电池电压 / 电流 / 欠压告警", PURPLE),
        (1114, 630, 1400, 730, "电源键 + IP5306", "短按开关 · 长按硬断 · 心跳防休眠", RED),
        (1420, 630, 1706, 730, "电池 / 充电", "3.7V 2000mAh · USB-C 充电", RED),
        (1114, 760, 1706, 900, "控制面板", "POWER / PROACTIVE / VOICE\n+ 原有 MUTE / VOL / BOOT", GRAY),
    ]
    for x1, y1, x2, y2, t, s, c in blocks:
        chip(d, (x1, y1, x2, y2), t, s, (255, 255, 255), c, c, 9, 19, 15)

    # annotations
    text(d, (1114, 916), "电源键硬件直连 IP5306；MCU 只做检测、心跳和软关机。", 16, RED)
    text(d, (1114, 940), "语音 / 主动消息键走 TCA9534；PC 持久化开关并同步手机。", 16, PURPLE)

    rbox(d, (60, 972, 1740, 1060), (255, 255, 255), LINE, 2, 12)
    text(d, (84, 994), "边界：ESP32-S3 只负责身体。完整大脑、记忆、模型仍在 PC；关机键只关身体，不关 PC。", 19, INK)
    text(d, (84, 1028), "所有键都经 body_link 转成事件；PC 离线时，语音键仍可本地静音，电源键仍可硬件开关。", 17, MUTED)

    im.save(ROOT / "system_block_v2.png")


# ---------------------------------------------------------------- power tree
def make_power_tree():
    W, H = 1800, 1160
    im = Image.new("RGB", (W, H), PAPER)
    d = ImageDraw.Draw(im)
    title(d, "v2 电源树与电源键", "IP5306 负责充电/升压/保护；MCU 用心跳保持输出，用 NPN 模拟长按关机。")

    chip(d, (60, 190, 300, 290), "3.7V LiPo", "2000mAh 带保护板", (255, 246, 238), RED, RED)
    chip(d, (350, 190, 600, 290), "INA226 + 0.01Ω", "电池电压 / 电流", (244, 239, 252), PURPLE, PURPLE)
    chip(d, (650, 190, 930, 290), "IP5306 模块", "充电 + 5V 2A 升压", (255, 246, 238), RED, RED)
    chip(d, (980, 190, 1230, 290), "PTC 2A", "5V 轨总入口", (250, 250, 250), GRAY, GRAY)
    chip(d, (1280, 190, 1450, 290), "TVS", "SMAJ5.0A", (250, 250, 250), GRAY, GRAY)
    chip(d, (1500, 190, 1740, 290), "5V 轨", "4.86–4.97V", (238, 248, 240), GREEN, GREEN)
    for a, b in [((300, 240), (350, 240)), ((600, 240), (650, 240)),
                 ((930, 240), (980, 240)), ((1230, 240), (1280, 240)), ((1450, 240), (1500, 240))]:
        arrow(d, a, b, RED if b[0] < 1450 else GREEN, 4, 10)
    note(d, (70, 300), "电池 → 分流器 → IP5306 BAT+；充电电流也走 INA226，能看充/放方向。", 16)
    note(d, (995, 300), "PTC 装在 5V 总入口：2A hold / 4A trip；不再串 SS34，也不做第二路或路。", 16, RED)

    # 5V branches
    chip(d, (1200, 420, 1450, 520), "DevKitC 5V", "负载；USB 只烧写", (238, 245, 253), BLUE, BLUE)
    chip(d, (1490, 420, 1740, 520), "MAX98357A VIN", "功放 5V", (238, 248, 240), GREEN, GREEN)
    chip(d, (930, 420, 1160, 520), "P5 5V 检测", "100k:150k + 100nF", (250, 250, 250), GRAY, GRAY)
    arrow(d, (1665, 290), (1665, 360), GREEN, 4, 10)
    d.line([(1665, 360), (1325, 360)], fill=GREEN, width=4)
    arrow(d, (1325, 360), (1325, 420), GREEN, 4, 10)
    d.line([(1665, 360), (1615, 360)], fill=GREEN, width=4)
    arrow(d, (1615, 360), (1615, 420), GREEN, 4, 10)
    d.line([(1325, 360), (1045, 360)], fill=GREEN, width=4)
    arrow(d, (1045, 360), (1045, 420), GREEN, 4, 10)

    # 3.3V
    chip(d, (1200, 620, 1450, 720), "3.3V 轨", "DevKitC LDO", (238, 245, 253), BLUE, BLUE)
    arrow(d, (1325, 520), (1325, 620), BLUE, 4, 10)
    chip(d, (900, 780, 1120, 880), "摄像头 / 屏幕", "3.3V", (238, 245, 253), BLUE, BLUE)
    chip(d, (1140, 780, 1360, 880), "麦克风 / 扩展", "3.3V", (238, 245, 253), BLUE, BLUE)
    chip(d, (1380, 780, 1600, 880), "INA226 / TCA9534", "3.3V", (244, 239, 252), PURPLE, PURPLE)
    for x in (1010, 1250, 1490):
        arrow(d, (1325, 720), (x, 780), BLUE, 3, 9)
    text(d, (900, 935), "P7 电源 LED 由 3V3 供电、软件控制；5V 有无看 P5。", 14, MUTED)

    # power key circuit: block-level inset, detailed schematic is separate
    rbox(d, (60, 560, 820, 1120), (255, 253, 248), ORANGE, 3, 18)
    text(d, (90, 588), "电源键 / 心跳 / 关机", 27, ORANGE, True)
    text(d, (90, 628), "面板 POWER 键并联在 IP5306 的 KEY 与 GND 之间。", 17, INK)
    text(d, (90, 658), "MCU 用 NPN 拉低 KEY：12 s 心跳 100 ms；软关机保持 2.5 s。", 17, INK)
    text(d, (90, 688), "KEY 经 100k 到 BSS138 栅极，栅源 100k 下拉；不装稳压管。", 17, INK)
    # blocks
    chip(d, (100, 750, 280, 820), "IP5306 KEY", "模块按键焊盘", (255, 246, 238), RED, RED, 7, 15, 12)
    chip(d, (330, 750, 500, 820), "S1 POWER", "面板按键", (255, 253, 248), ORANGE, ORANGE, 7, 15, 12)
    chip(d, (550, 750, 760, 820), "Q2 NPN", "集电极→KEY；发射极→GND", (250, 250, 250), INK, INK, 7, 15, 12)
    chip(d, (100, 880, 280, 950), "GPIO48", "1k 基极电阻", (238, 245, 253), BLUE, BLUE, 7, 15, 12)
    chip(d, (330, 880, 500, 950), "Q1 BSS138", "KEY→R5→栅极；R6 下拉", (255, 253, 248), ORANGE, ORANGE, 7, 13, 12)
    chip(d, (550, 880, 760, 950), "GPIO1", "漏极 + 10k 上拉 3V3", (238, 245, 253), BLUE, BLUE, 7, 15, 12)
    # wires between blocks
    arrow(d, (280, 785), (330, 785), RED, 3, 8)
    arrow(d, (500, 785), (550, 785), RED, 3, 8)
    d.line([(655, 820), (655, 850), (190, 850), (190, 880)], fill=BLUE, width=3)
    d.line([(280, 915), (330, 915)], fill=ORANGE, width=3)
    d.line([(500, 915), (550, 915)], fill=BLUE, width=3)
    text(d, (100, 980), "KEY 节点到 S1、Q2 集电极、R5；R5 后才是 Q1 栅极。三者共地。", 15, MUTED)
    text(d, (100, 1012), "软关机：0.3–1.5 s 松开 → power 事件 → NVS → GPIO48 高 2.5 s。", 15, INK)
    text(d, (100, 1044), "按住 >2 s：IP5306 硬件断电；不经过固件，等同拔电。", 15, RED)
    text(d, (100, 1076), "详细电路见 power_key_circuit_v2.png；电源树见本图上半部分。", 15, MUTED)

    im.save(ROOT / "power_tree_v2.png")


# ---------------------------------------------------------------- control wiring
def make_control_wiring():
    W, H = 1900, 1320
    im = Image.new("RGB", (W, H), PAPER)
    d = ImageDraw.Draw(im)
    title(d, "v2 控制面板接线", "TCA9534 挂在摄像头 SCCB 总线上；屏幕 CS 接地、RST 接 EN；GPIO48 改作 IP5306 心跳。")

    # ESP32
    rbox(d, (60, 180, 560, 1080), (238, 245, 253), BLUE, 3, 18)
    text(d, (88, 208), "ESP32-S3-DevKitC-1", 25, BLUE, True)
    text(d, (90, 244), "v1.1 / N8R8", 18, MUTED)
    pins = [
        ("GPIO5", "I2C SCL / CAM_SIOC", 292),
        ("GPIO4", "I2C SDA / CAM_SIOD", 330),
        ("GPIO1", "PWR_KEY_SENSE", 368),
        ("GPIO48", "IP5306_KEY (NPN)", 406),
        ("GPIO2", "KEY_MUTE", 444),
        ("GPIO14", "KEY_VOL", 482),
        ("GPIO0", "KEY_BOOT", 520),
        ("GPIO43", "UART_TX / 调试", 558),
        ("GPIO44", "UART_RX / 调试", 596),
        ("GPIO47", "LCD_MOSI", 634),
        ("GPIO21", "LCD_SCK", 672),
        ("GPIO3", "LCD_DC", 710),
        ("GPIO41", "I2S_BCLK", 748),
        ("GPIO42", "I2S_WS", 786),
        ("GPIO39", "I2S_MIC_DIN", 824),
        ("GPIO40", "I2S_SPK_DOUT", 862),
    ]
    for name, sub, y in pins:
        text(d, (90, y), name, 18, INK, True)
        text(d, (180, y), sub, 16, MUTED)

    # TCA9534
    rbox(d, (760, 180, 1170, 690), (246, 241, 253), PURPLE, 3, 18)
    text(d, (790, 208), "TCA9534 · 0x20", 25, PURPLE, True)
    text(d, (792, 244), "8-bit I2C I/O 扩展", 17, MUTED)
    exp = [
        ("P0", "PROACTIVE_KEY", 292),
        ("P1", "VOICE_KEY", 330),
        ("P2", "BATT_ALERT", 368),
        ("P3", "VOICE_LED", 406),
        ("P4", "PROACTIVE_LED", 444),
        ("P5", "5V_PRESENT", 482),
        ("P6", "AMP_SD_GATE", 520),
        ("P7", "POWER_LED", 558),
    ]
    for p, net, y in exp:
        text(d, (792, y), p, 18, PURPLE, True)
        text(d, (835, y), net, 17, INK, True)
    text(d, (792, 600), "P0/P1 10k 上拉；P2 10k 上拉；", 14, MUTED)
    text(d, (792, 628), "P3/P4/P7 低电平点灯；P6 高=功放关断。", 14, MUTED)
    text(d, (792, 656), "P5 = 5V 经 100k:150k 分压；100nF 到地。", 14, MUTED)

    # buttons right
    rbox(d, (1270, 180, 1810, 600), (255, 253, 248), ORANGE, 3, 18)
    text(d, (1298, 208), "面板按键 / LED", 25, ORANGE, True)
    keys = [
        ("POWER", "硬件：IP5306 KEY ↔ GND", "短按开关；短按松开软关；>2s 硬断", 270),
        ("PROACTIVE", "TCA9534 P0 ↔ GND", "切换主动消息；LED P4", 350),
        ("VOICE", "TCA9534 P1 ↔ GND", "切换语音；LED P3；P6 高=功放关", 430),
        ("MUTE / VOL", "GPIO2 / GPIO14 ↔ GND", "保留原有短按/循环音量", 510),
    ]
    for name, wiring, desc, y in keys:
        rbox(d, (1298, y, 1780, y + 64), WHITE, LINE, 2, 10)
        text(d, (1312, y + 9), name, 19, INK, True)
        text(d, (1312, y + 33), wiring + " · " + desc, 14, MUTED)

    # I2C devices
    chip(d, (760, 760, 1040, 860), "OV2640 SCCB", "0x30 · 共用总线", (238, 245, 253), BLUE, BLUE)
    chip(d, (1080, 760, 1360, 860), "INA226", "0x40 · 0.01Ω 分流", (244, 239, 252), PURPLE, PURPLE)
    chip(d, (1400, 760, 1740, 860), "I2C 上拉", "2.2k ×2 到 3.3V", (250, 250, 250), GRAY, GRAY)
    d.line([(560, 310), (760, 310)], fill=BLUE, width=4)
    d.line([(560, 348), (760, 348)], fill=BLUE, width=4)
    text(d, (610, 282), "SDA/SCL", 14, BLUE)
    for x in (900, 1220, 1570):
        d.line([(x, 690), (x, 760)], fill=PURPLE, width=3)

    # AMP / battery
    chip(d, (760, 920, 1070, 1030), "MAX98357A", "SD ← Q3 漏极；GAIN 不动", (238, 248, 240), GREEN, GREEN)
    chip(d, (1110, 920, 1450, 1030), "IP5306 KEY", "NPN 集电极；GPIO48 经 1k", (255, 246, 238), RED, RED)
    chip(d, (1490, 920, 1770, 1030), "BSS138 ×2", "Q1 KEY→GPIO1；Q3 SD 低边", (255, 253, 248), ORANGE, ORANGE)

    # notes
    rbox(d, (60, 1120, 1810, 1260), (255, 255, 255), LINE, 2, 12)
    text(d, (84, 1140), "I2C 共享：esp32-camera 新版支持 pin_sccb_sda=NO_PIN + sccb_i2c_port；OV2640(0x30)、TCA9534(0x20)、INA226(0x40) 地址不冲突。", 17, INK)
    text(d, (84, 1172), "屏幕 CS 接地，RST 接 DevKitC EN（经 100Ω）：MCU 复位时屏也复位，避免上电毛刺被当命令。", 17, INK)
    text(d, (84, 1204), "GPIO1 不再做 5V ADC；5V 有无由 P5 读 100k:150k。GPIO43/44 UART 调试保留。", 17, MUTED)

    im.save(ROOT / "control_wiring_v2.png")


# ---------------------------------------------------------------- board layout
def draw_board(which: str):
    S = 8.4
    M = 100
    W = H = 1200
    M = 180
    im = Image.new("RGB", (W, H), PAPER)
    d = ImageDraw.Draw(im)

    def p(x_mm, y_mm):
        return (M + x_mm * S, M + (100 - y_mm) * S)

    def box(x1, y1, x2, y2):
        a, b = p(x1, y1), p(x2, y2)
        return [min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1])]

    def ell(x, y, rx, ry):
        return box(x - rx, y - ry, x + rx, y + ry)

    text(d, (36, 18), "瓦尔基里身体 v2 · " + ("顶面 / 面板" if which == "top" else "底面 / 电源与主控"), 27, INK, True)
    text(d, (36, 50), "Ø120 mm · 2 层 · 1.6 mm · 3×M3 · 单位 mm", 17, MUTED)

    d.ellipse(ell(50, 50, 60, 60), fill=(250, 252, 255), outline=INK, width=3)
    for i in range(-10, 111, 10):
        d.line([p(i, -10), p(i, 110)], fill=(235, 240, 246), width=1)
        d.line([p(-10, i), p(110, i)], fill=(235, 240, 246), width=1)

    # USB notch left
    d.rectangle(box(0, 14, 6, 26), fill=PAPER, outline=INK, width=3)
    text(d, p(1.2, 20), "USB\n缺口", 12, RED, anchor="lm")

    if which == "top":
        # screen
        d.rounded_rectangle(box(29.8, 38.25, 70.2, 75.75), radius=8, fill=(24, 32, 46), outline=BLUE, width=3)
        d.ellipse(ell(50, 57, 16.2, 16.2), outline=(90, 170, 240), width=2)
        text(d, p(50, 57), "1.28\" 圆屏\n240×240", 18, (220, 235, 250), anchor="mm")
        # camera
        d.rounded_rectangle(box(38.5, 76, 61.5, 97), radius=7, fill=(40, 56, 76), outline=ORANGE, width=3)
        d.ellipse(ell(50, 88, 6, 6), fill=(14, 22, 34), outline=(140, 190, 240), width=2)
        d.ellipse(ell(50, 88, 2, 2), fill=(70, 130, 210))
        text(d, p(50, 78.5), "OV2640 · 2×9", 13, (255, 220, 180), anchor="ma")
        # mic
        d.rounded_rectangle(box(14, 58, 28, 68), radius=5, fill=(232, 248, 246), outline=TEAL, width=3)
        d.ellipse(ell(21, 63, 1.1, 1.1), fill=(20, 30, 40), outline=TEAL, width=2)
        text(d, p(21, 63), "INMP441", 12, TEAL, anchor="mm")
        # amp
        d.rounded_rectangle(box(73, 45, 91, 59), radius=5, fill=(255, 241, 226), outline=ORANGE, width=3)
        text(d, p(82, 52), "MAX98357A", 12, ORANGE, anchor="mm")
        # new buttons
        for bx, label, color in [(25, "PWR", RED), (37, "PRO", PURPLE), (49, "VOX", GREEN)]:
            d.ellipse(ell(bx, 15, 3.2, 3.2), fill=(250, 250, 250), outline=color, width=3)
            d.ellipse(ell(bx, 15, 1.1, 1.1), fill=color)
            text(d, p(bx, 8.0), label, 10, color, True, "ma")
        # LEDs（与前面板孔同一 y=28，三只）
        for lx, color, label in [(30, GREEN, "V"), (40, PURPLE, "P"), (50, RED, "PW")]:
            d.ellipse(ell(lx, 28, 1.4, 1.4), fill=color, outline=INK, width=1)
            text(d, p(lx, 31), label, 9, MUTED, anchor="ma")
        # mute / vol
        for bx, by, label in [(8, 55, "音量"), (8, 43, "静音")]:
            d.ellipse(ell(bx, by, 3, 3), fill=(60, 70, 85), outline=INK, width=2)
            text(d, p(bx + 7, by), label, 14, INK)
        # connectors：全部在 r≤46 内，避开按键带和 M3
        for bx, by, bw, bh, label, color in [
            (12, 25, 12, 7, "5V 输入\nJST-PH", RED),
            (78, 32, 12, 7, "喇叭\nJST-PH", ORANGE),
            (8, 33, 16, 6, "调试 UART", GRAY),
        ]:
            d.rounded_rectangle(box(bx, by, bx + bw, by + bh), radius=4, fill=WHITE, outline=color, width=2)
            text(d, p(bx + bw / 2, by + bh / 2), label, 9, color, anchor="mm")
        # antenna keepout：模块右端内侧 15mm，不是板外
        keep = box(63, 10, 78, 38)
        d.rectangle(keep, fill=None, outline=RED, width=3)
        for k in range(0, 9):
            x = keep[0] + (keep[2] - keep[0]) * k / 8
            d.line([(x, keep[3]), (min(x + 10, keep[2]), keep[1])], fill=(245, 190, 190), width=1)
        text(d, p(70.5, 21), "天线禁布区\n15×28 mm", 10, RED, anchor="mm")
        # holes
        for hx, hy in [(11.9, 72.0), (88.1, 72.0), (50.0, 6.0)]:
            d.ellipse(ell(hx, hy, 1.8, 1.8), fill=WHITE, outline=INK, width=2)
            d.ellipse(ell(hx, hy, 1.1, 1.1), fill=(230, 235, 242), outline=GRAY, width=1)
            text(d, p(hx, hy - 6.5), "M3", 9, MUTED, anchor="ma")

    else:
        # DevKitC
        d.rounded_rectangle(box(10, 10, 78, 38), radius=5, fill=(238, 242, 248), outline=INK, width=3)
        text(d, p(44, 21), "ESP32-S3-DevKitC-1\n2×22 母座（背面）", 16, INK, anchor="mm")
        text(d, p(44, 33), "USB 朝左 · 天线朝右", 12, MUTED, anchor="mm")
        # antenna：模块右端内侧 15mm
        d.rectangle(box(63, 10, 78, 38), outline=RED, width=2)
        text(d, p(70.5, 24), "PCB 天线\n禁铺铜 15×28", 11, RED, anchor="mm")
        # battery
        d.rounded_rectangle(box(12, 60, 52, 86), radius=6, fill=(240, 248, 240), outline=GREEN, width=2)
        text(d, p(32, 73), "LiPo 3.7V 2000mAh\n带保护板", 12, GREEN, anchor="mm")
        # IP5306
        d.rounded_rectangle(box(55, 40, 86, 58), radius=4, fill=(255, 245, 235), outline=RED, width=2)
        text(d, p(70.5, 49), "IP5306 模块\n充电 USB-C / 5V 2A", 11, RED, anchor="mm")
        # control circuit
        d.rounded_rectangle(box(12, 44, 44, 56), radius=4, fill=(246, 241, 253), outline=PURPLE, width=2)
        text(d, p(28, 50), "TCA9534 / INA226\n电源键电路", 10, PURPLE, anchor="mm")
        # speaker (rear panel, not front)
        d.ellipse(ell(70, 78, 14, 14), fill=(255, 247, 235), outline=ORANGE, width=2)
        text(d, p(70, 78), "Ø28 喇叭\n腔体（后壳）", 11, ORANGE, anchor="mm")
        # charging USB-C right edge
        d.rectangle(box(94, 42, 100, 52), fill=WHITE, outline=RED, width=2)
        text(d, p(96, 38), "充电\nUSB-C", 9, RED, anchor="ma")
        # holes
        for hx, hy in [(11.9, 72.0), (88.1, 72.0), (50.0, 6.0)]:
            d.ellipse(ell(hx, hy, 1.8, 1.8), fill=WHITE, outline=INK, width=2)
            d.ellipse(ell(hx, hy, 1.1, 1.1), fill=(230, 235, 242), outline=GRAY, width=1)

    text(d, (24, 1160), "注：图为机械摆位参考，不是已布线的 Gerber。排针方向、模块高度、电池尺寸需按实物复核。", 14, MUTED)
    im.save(ROOT / f"board_{which}_v2.png")


# ---------------------------------------------------------------- panel drawing
def make_panel():
    W, H = 1240, 1600
    im = Image.new("RGB", (W, H), PAPER)
    d = ImageDraw.Draw(im)
    title(d, "v2 前面板开孔图", "Ø124 mm 圆面板；开孔以板中心为基准；加工前用实物模块复核。")
    cx, cy = 620, 720
    R = 558  # 124mm at 9px/mm
    S = 9.0
    d.ellipse((cx - R, cy - R, cx + R, cy + R), fill=(252, 253, 255), outline=INK, width=3)

    def pt(x_mm, y_mm):
        # panel coordinates: board center at 50,50; y up
        return (cx + (x_mm - 50) * S, cy - (y_mm - 50) * S)

    def circ(x, y, r_mm, color, label="", lab_dy=-1.0):
        x0, y0 = pt(x, y)
        rr = r_mm * S
        d.ellipse((x0 - rr, y0 - rr, x0 + rr, y0 + rr), fill=WHITE, outline=color, width=3)
        if label:
            text(d, (x0, y0 - rr - 8), label, 13, color, True, "ms")
        return x0, y0

    # cutouts
    circ(50, 57, 19, BLUE, "屏幕 Ø38")
    circ(50, 88, 4, ORANGE, "摄像头 Ø8")
    circ(21, 63, 1, TEAL, "麦克风 Ø2")
    for x, label, color in [(25, "POWER", RED), (37, "PROACTIVE", PURPLE), (49, "VOICE", GREEN)]:
        circ(x, 15, 3.1, color, label + " Ø6.2")
    for x, label, color in [(30, "V", GREEN), (40, "P", PURPLE), (50, "PWR", RED)]:
        circ(x, 28, 1.6, color, label + " Ø3.2")
    for x, y, label in [(8, 43, "静音"), (8, 55, "音量")]:
        circ(x, y, 3, GRAY, label + " Ø6")
    # M3
    for x, y in [(11.9, 72), (88.1, 72), (50, 6)]:
        circ(x, y, 1.7, INK, "M3")

    # dimensions
    d.line([(cx - R, cy + R + 26), (cx + R, cy + R + 26)], fill=GRAY, width=1)
    d.line([(cx - R, cy + R + 18), (cx - R, cy + R + 34)], fill=GRAY, width=1)
    d.line([(cx + R, cy + R + 18), (cx + R, cy + R + 34)], fill=GRAY, width=1)
    text(d, (cx, cy + R + 44), "Ø124 mm", 17, MUTED, False, "ma")
    text(d, (58, 1360), "按钮中心距 12 mm；Ø6.2 孔配 6×6 轻触帽；3 只 LED 用 Ø3 导光柱，孔在 y=28。", 16, MUTED)
    text(d, (58, 1392), "喇叭 Ø28 开在后壳，不在前面板；前面板开孔与 PCB 摆位一致。", 16, ORANGE)
    text(d, (58, 1424), "装配前先做纸板 1:1 试装；屏幕/摄像头/麦克风先不要上胶。", 16, MUTED)
    text(d, (58, 1456), "外壳材料用 PLA/PETG；天线正下方不得有金属或电池。", 16, RED)
    im.save(ROOT / "panel_cutout_v2.png")

    # a simple SVG version for laser cutting
    svg = ['<svg xmlns="http://www.w3.org/2000/svg" width="124mm" height="124mm" viewBox="0 0 124 124">',
           '<g fill="none" stroke="black" stroke-width="0.3">',
           '<circle cx="62" cy="62" r="62"/>']
    for x, y, r in [(50, 57, 19), (50, 88, 4), (21, 63, 1),
                    (30, 15, 3.1), (45, 15, 3.1), (60, 15, 3.1),
                    (30, 28, 1.6), (40, 28, 1.6), (50, 28, 1.6), (8, 43, 3), (8, 55, 3),
                    (11.9, 72, 1.7), (88.1, 72, 1.7), (50, 6, 1.7)]:
        svg.append(f'<circle cx="{x + 12:.2f}" cy="{112 - y:.2f}" r="{r}"/>')
    svg += ['</g>', '</svg>']
    (ROOT / "面板开孔图.svg").write_text("\n".join(svg), encoding="utf-8")


# ---------------------------------------------------------------- back panel
def make_back_panel():
    W, H = 1240, 1600
    im = Image.new("RGB", (W, H), PAPER)
    d = ImageDraw.Draw(im)
    title(d, "v2 后壳开孔图", "Ø124 mm 圆后壳；喇叭、充电 USB-C、DevKitC 双 USB、M3 柱；天线区只能塑料。")
    cx, cy = 620, 720
    R = 558
    S = 9.0
    d.ellipse((cx - R, cy - R, cx + R, cy + R), fill=(252, 253, 255), outline=INK, width=3)

    def pt(x_mm, y_mm):
        return (cx + (x_mm - 50) * S, cy - (y_mm - 50) * S)

    def circ(x, y, r_mm, color, label=""):
        x0, y0 = pt(x, y)
        rr = r_mm * S
        d.ellipse((x0 - rr, y0 - rr, x0 + rr, y0 + rr), fill=WHITE, outline=color, width=3)
        if label:
            text(d, (x0, y0 - rr - 8), label, 14, color, True, "ms")

    # M3 bosses
    for x, y in [(11.9, 72), (88.1, 72), (50, 6)]:
        circ(x, y, 1.7, INK, "M3")
    # speaker
    circ(70, 78, 14, ORANGE, "喇叭 Ø28")
    # charging USB-C cutout (right edge)
    x1, y1 = pt(93, 52)
    x2, y2 = pt(100, 42)
    d.rectangle((min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)),
                fill=WHITE, outline=RED, width=3)
    text(d, (max(x1, x2) + 10, (y1 + y2) / 2), "充电\nUSB-C", 14, RED, True, "lm")
    # DevKitC twin USB-C cutout (left edge, 14 x 9)
    x1, y1 = pt(0, 23)
    x2, y2 = pt(14, 14)
    d.rectangle((min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)),
                fill=WHITE, outline=BLUE, width=3)
    text(d, (max(x1, x2) + 8, (y1 + y2) / 2), "DevKitC\nUSB×2", 12, BLUE, True, "lm")
    # antenna zone (rear, under module right end 15 mm)
    x1, y1 = pt(63, 38)
    x2, y2 = pt(78, 10)
    d.rectangle((min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)),
                fill=None, outline=RED, width=2)
    text(d, ((x1 + x2) / 2, (y1 + y2) / 2), "天线区\n只许塑料", 13, RED, True, "mm")

    text(d, (58, 1360), "喇叭 Ø28 装在后壳，出声孔朝后；不要贴金属网太近，避免短路。", 16, MUTED)
    text(d, (58, 1392), "充电 USB-C 在右侧；DevKitC 两个 USB-C 在左侧，后壳开 14×9 缺口，烧写不用拆壳。", 16, MUTED)
    text(d, (58, 1424), "电池用双面胶 + 尼龙扎带固定在底壳，远离喇叭磁铁和天线。", 16, MUTED)
    text(d, (58, 1456), "后壳材料 PLA/PETG；天线正下方不得有金属箔、螺丝、电池。", 16, RED)
    im.save(ROOT / "back_panel_cutout_v2.png")


# ---------------------------------------------------------------- power key circuit
def make_power_key_circuit():
    W, H = 1500, 860
    im = Image.new("RGB", (W, H), PAPER)
    d = ImageDraw.Draw(im)
    title(d, "电源键接口（方块级）", "详细网络见接线表；KEY 节点是电池域，不要直接接 ESP32 GPIO。")

    # rails
    d.line([(80, 160), (1420, 160)], fill=RED, width=5)
    text(d, (86, 120), "IP5306 KEY", 20, RED, True)
    d.line([(80, 650), (1420, 650)], fill=INK, width=4)
    text(d, (86, 612), "GND", 18, INK, True)
    d.line([(1180, 520), (1420, 520)], fill=BLUE, width=4)
    text(d, (1188, 484), "3V3", 18, BLUE, True)

    # S1
    rbox(d, (170, 240, 340, 340), WHITE, ORANGE, 3, 10)
    text(d, (255, 270), "S1 POWER", 18, ORANGE, True, "ma")
    text(d, (255, 300), "面板按键", 14, MUTED, False, "ma")
    text(d, (255, 322), "KEY ↔ GND", 13, MUTED, False, "ma")
    d.line([(255, 160), (255, 240)], fill=RED, width=4)
    d.line([(255, 340), (255, 650)], fill=INK, width=4)

    # Q2
    rbox(d, (500, 240, 700, 360), WHITE, INK, 3, 10)
    text(d, (600, 270), "Q2 NPN", 19, INK, True, "ma")
    text(d, (600, 300), "BC547 / 2N3904", 14, MUTED, False, "ma")
    text(d, (600, 326), "C→KEY  E→GND", 13, MUTED, False, "ma")
    d.line([(600, 160), (600, 240)], fill=RED, width=4)
    d.line([(600, 360), (600, 650)], fill=INK, width=4)

    # 1k and GPIO48
    rbox(d, (350, 282, 500, 318), WHITE, INK, 2, 6)
    text(d, (425, 300), "1k", 15, INK, True, "mm")
    # base pin comes out of Q2 left; 1k goes down to GPIO48, not through S1
    d.line([(350, 300), (500, 300)], fill=BLUE, width=4)
    d.line([(350, 300), (350, 420), (300, 420)], fill=BLUE, width=4)
    rbox(d, (160, 420, 300, 490), (238, 245, 253), BLUE, 2, 8)
    text(d, (230, 444), "GPIO48", 18, BLUE, True, "ma")
    text(d, (230, 470), "心跳 / 软关机", 12, MUTED, False, "ma")

    # Q1 + 栅极串联/下拉
    rbox(d, (930, 240, 1130, 360), WHITE, ORANGE, 3, 10)
    text(d, (1030, 270), "Q1 BSS138", 18, ORANGE, True, "ma")
    text(d, (1030, 300), "G←R5  S→GND", 13, MUTED, False, "ma")
    text(d, (1030, 326), "D→GPIO1", 13, MUTED, False, "ma")
    # KEY -> R5 100k -> gate
    rbox(d, (790, 282, 890, 318), WHITE, INK, 2, 6)
    text(d, (840, 300), "R5 100k", 13, INK, True, "mm")
    d.line([(830, 160), (830, 282)], fill=RED, width=4)
    d.line([(890, 300), (930, 300)], fill=ORANGE, width=4)
    # gate-source R6 100k
    d.line([(930, 300), (930, 650)], fill=ORANGE, width=3)
    d.rectangle((914, 450, 946, 510), fill=WHITE, outline=INK, width=2)
    text(d, (930, 480), "R6\n100k", 11, INK, True, "mm")
    # source to GND
    d.line([(1030, 360), (1030, 650)], fill=INK, width=4)

    # drain to GPIO1 and 10k to 3V3
    d.line([(1130, 300), (1180, 300)], fill=BLUE, width=4)
    d.ellipse((1172, 292, 1188, 308), fill=WHITE, outline=BLUE, width=2)
    d.line([(1180, 300), (1320, 300)], fill=BLUE, width=4)
    rbox(d, (1250, 280, 1320, 320), WHITE, INK, 2, 6)
    text(d, (1285, 300), "GPIO1", 14, BLUE, True, "mm")
    d.line([(1180, 300), (1180, 520)], fill=BLUE, width=4)
    d.rectangle((1164, 390, 1196, 450), fill=WHITE, outline=INK, width=2)
    text(d, (1180, 420), "10k", 13, INK, True, "mm")

    # explanatory notes
    rbox(d, (80, 700, 720, 830), WHITE, LINE, 2, 10)
    text(d, (104, 720), "用法", 18, INK, True)
    text(d, (104, 750), "心跳：每 12 s 拉高 GPIO48 100 ms，Q2 短按 KEY。", 15, INK)
    text(d, (104, 780), "软关机：GPIO48 拉高 2.5 s，Q2 长按 KEY。", 15, INK)
    text(d, (104, 810), "面板 S1 与 Q2 集电极并联；用户按键始终优先。", 15, MUTED)

    rbox(d, (760, 700, 1420, 830), (255, 248, 248), RED, 2, 10)
    text(d, (784, 720), "电平注意", 18, RED, True)
    text(d, (784, 750), "KEY 先经 R5 100k 到 Q1 栅极，R6 100k 栅源下拉；", 15, INK)
    text(d, (784, 780), "Vgs 最大 4.2V，在 ±20V 内，删掉 5.1V 稳压管。", 15, INK)
    text(d, (784, 810), "KEY 负载约 200k；P1 量挂电路前后 KEY 高电平差 <0.3V。", 15, RED)
    im.save(ROOT / "power_key_circuit_v2.png")


# ---------------------------------------------------------------- FSM
def make_fsm():
    W, H = 1800, 980
    im = Image.new("RGB", (W, H), PAPER)
    d = ImageDraw.Draw(im)
    title(d, "v2 按键状态表与电源流程", "按键只产生事件；电源键另有硬件直连，MCU 卡死也能长按断电。")

    # power flow, one row
    flow = [
        ("OFF", 80, 180, 170, 74, RED),
        ("BOOT", 330, 180, 170, 74, ORANGE),
        ("RUN", 580, 180, 170, 74, BLUE),
        ("SHUTDOWN", 830, 180, 220, 74, RED),
        ("OFF", 1130, 180, 170, 74, RED),
    ]
    for name, x, y, w, h, color in flow:
        rbox(d, (x, y, x + w, y + h), WHITE, color, 3, 14)
        text(d, (x + w / 2, y + h / 2), name, 23, color, True, "mm")
    labels = ["POWER 短按", "自检完成", "POWER 松开", "IP5306 长按"]
    for i, lab in enumerate(labels):
        x1 = flow[i][1] + flow[i][3]
        x2 = flow[i + 1][1]
        arrow(d, (x1 + 6, 217), (x2 - 8, 217), RED if i in (0, 2, 3) else ORANGE, 3, 9)
        text(d, ((x1 + x2) / 2, 270), lab, 13, MUTED, False, "ma")

    # table
    x0, y0 = 80, 320
    cols = [170, 300, 900]
    rows = [
        ("OFF", "POWER 短按", "IP5306 开输出 → MCU 启动 → 屏幕自检 → RUN"),
        ("RUN", "POWER 0.3–1.5 s 松开", "发 power 事件 → 保存 NVS → 2.5 s 后 IP5306 断电"),
        ("RUN", "POWER >2 s", "IP5306 直接硬断；等同拔电，只在卡死时用"),
        ("RUN", "PROACTIVE", "切换主动消息；LED P4；PC 持久化并同步手机"),
        ("RUN", "VOICE", "切换语音；LED P3；P6 高=Q3 拉低 SD 关断"),
        ("RUN", "MUTE / VOL", "本地静音 / 音量 4 档循环；不改 PC 音色"),
        ("RUN", "BOOT", "进入下载模式；不参与正常运行"),
        ("RUN", "任意键按下", "屏幕亮 5 s；不改变对话状态"),
        ("RUN", "PC 断线", "语音/主动消息事件进 NVS 待发；电源键仍可用"),
    ]
    headers = ["状态", "输入", "结果"]
    # header
    yy = y0
    for j, h in enumerate(headers):
        xx = x0 + sum(cols[:j])
        d.rectangle((xx, yy, xx + cols[j], yy + 46), fill=(236, 242, 250), outline=LINE, width=1)
        text(d, (xx + 14, yy + 11), h, 18, INK, True)
    yy += 46
    for i, row in enumerate(rows):
        for j, cell in enumerate(row):
            xx = x0 + sum(cols[:j])
            fill = WHITE if i % 2 == 0 else (250, 252, 255)
            d.rectangle((xx, yy, xx + cols[j], yy + 52), fill=fill, outline=LINE, width=1)
            text(d, (xx + 14, yy + 15), cell, 16, INK if j < 2 else MUTED)
        yy += 52
    note(d, (80, 920), "去抖 25 ms；长按判定独立于 PC。电源键硬件通路见 power_key_circuit_v2.png。", 16)
    im.save(ROOT / "button_fsm_v2.png")


# ---------------------------------------------------------------- power key timing
def make_power_timing():
    W, H = 1700, 680
    im = Image.new("RGB", (W, H), PAPER)
    d = ImageDraw.Draw(im)
    title(d, "电源键时序", "IP5306 短按开输出，长按 >2 s 关输出；MCU 用 NPN 模拟长按，用 BSS138 读键。")
    x0, x1 = 120, 1580
    y = 300
    d.line([(x0, y), (x1, y)], fill=INK, width=4)
    for t in [0, 0.3, 1.0, 1.5, 2.0, 2.5, 3.0]:
        x = x0 + (x1 - x0) * t / 3.0
        d.line([(x, y - 10), (x, y + 10)], fill=INK, width=2)
        text(d, (x, y + 26), f"{t:.1f}s", 14, MUTED, False, "ma")
    # zones
    zones = [
        (0.0, 0.3, (226, 240, 255), "抖动/忽略"),
        (0.3, 1.5, (232, 248, 238), "软关机窗口：松开后发事件"),
        (1.5, 2.0, (255, 247, 226), "临界：尽快发完事件"),
        (2.0, 3.0, (255, 232, 232), "IP5306 硬件断电"),
    ]
    for a, b, color, label in zones:
        xa = x0 + (x1 - x0) * a / 3.0
        xb = x0 + (x1 - x0) * b / 3.0
        d.rectangle((xa, y - 70, xb, y - 20), fill=color)
        text(d, ((xa + xb) / 2, y - 88), label, 14, INK, False, "ma")
    # press waveform
    d.line([(x0, 430), (x0 + 100, 430), (x0 + 100, 480), (x0 + 460, 480),
            (x0 + 460, 430), (x1, 430)], fill=BLUE, width=4)
    text(d, (x0 + 120, 500), "按键按下（低有效）0.3–1.5 s", 16, BLUE)
    text(d, (120, 560), "固件动作：识别短按松开 → 发 power 事件 → 保存 NVS → 最多等 300 ms → 拉高 GPIO48 2.5 s。", 16, INK)
    text(d, (120, 596), "如果按住超过 2 s，IP5306 已经硬断，固件来不及软关；这是有意保留的逃生通道。", 16, RED)
    im.save(ROOT / "power_key_timing_v2.png")


if __name__ == "__main__":
    make_system_block()
    make_power_tree()
    make_power_key_circuit()
    make_control_wiring()
    draw_board("top")
    draw_board("bottom")
    make_panel()
    make_back_panel()
    make_fsm()
    make_power_timing()
    print("wrote diagrams to", ROOT)
