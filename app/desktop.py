"""瓦尔基里 · 识海 —— 桌面程序入口。

网页版（`sihai.cmd` / 8767 的 sihai.html）是浏览器里的 canvas；这一版把同一套功能搬进
一个真正的桌面窗口：星图、她现在的状态、记忆详情、时间轴、对话、
语音输入与朗读都在程序里，不再启动本地 HTTP 服务。

    python -m app.desktop            # 真实数据
    python -m app.desktop --demo     # 示例数据
    python -m app.desktop --screenshot out.png

保留网页版作为回退入口；桌面版不修改数据库结构，不叫醒她，只有
在输入框里真正发一句话才会消费休眠记录。
"""
from __future__ import annotations

import argparse
import ctypes
import math
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import webbrowser
from tkinter import font as tkfont
from pathlib import Path
from typing import Any, Optional

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageGrab, ImageTk

from . import config as C
from .desktop_prep import (
    SHELL_TINTS,
    clamp,
    display_palette,
    hex_mix,
    layer_name,
    node_color,
    prepare_graph,
    rnd01,
)
from .sihai import graph_payload

# ---- 颜色与字体 -----------------------------------------------------------
# 和 8767 的 sihai.html 同一套：深海蓝底、星图冷光、暮光琥珀。
# 桌面窗口不是另一套皮肤；网页版是基准，这里只做原生控件能承载的近似。
BG = "#03050b"
BG_2 = "#020307"
PANEL_BG = "#08111c"
CARD_BG = "#08101c"
INK = "#dfe7f5"
INK_SOFT = "#c8d4e6"
DIM = "#7d8ba3"
SUB = "#98a7bd"
LINE = "#1b2638"
LINE_2 = "#2a3a52"
ACCENT = "#8ee6ff"
SEA = "#8ee6ff"
AMBER = "#f0c17a"
WARN = "#ffb35c"
CORAL = "#ffb2a4"
GREEN = "#7de08a"
USER_BG = "#08101c"
HER_BG = "#08101c"
FONT = "Microsoft YaHei UI"
FONT_ALT = "Noto Sans SC"
SERIF = "Noto Serif SC"
MONO = "Cascadia Mono"

# 星图是一张 200+ 节点、1000+ 连线的图。Tk 的 itemconfigure/coords 每次都要
# 过 Tcl，60fps 全量重画会把主线程吃满，滚动和输入就会排队。桌面版改成
# 按需绘制：静止时完全不画；交互/动画时限制重绘频率，拖动时只画星点。
# 连线也只画权重最高的一批，避免 1000 条贝塞尔每帧重投影。
FRAME_MIN = 0.020
EDGE_BUDGET = 180

# 电脑端的识海入口：统一指向正在跑的 8767，不再新开 8765。
SIHAI_WEB = "http://127.0.0.1:8767/sihai.html"

# 桌面端为主入口：五个和网页版同名的独立视图。
VIEWS = [
    ("thought", "她在想什么"),
    ("dispatch", "调度"),
    ("learning", "学习"),
    ("echo", "残响"),
    ("diary", "日记"),
]


def _dpi_aware() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _dark_titlebar(root: tk.Tk) -> None:
    """把 Windows 默认的白色标题栏压成和识海同一块深色。

    只改窗口外观，不碰系统主题；Win10 只认暗色模式，Win11 再补标题栏
    底色和字色。失败就保持系统默认，不影响程序。
    """
    try:
        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id()) or root.winfo_id()
        value = ctypes.c_int(1)
        # 20 = DWMWA_USE_IMMERSIVE_DARK_MODE, 19 = 旧预览版编号
        for attr in (20, 19):
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, attr, ctypes.byref(value), ctypes.sizeof(value))
        # Win11 才有的标题栏/字色/边框色；失败返回负值，无副作用。
        caption = ctypes.c_int(0x000B0503)   # COLORREF: #03050b
        text = ctypes.c_int(0x00F5E7DF)      # COLORREF: #dfe7f5
        border = ctypes.c_int(0x0038241A)    # COLORREF: #1a2438
        for attr, ref in ((35, ctypes.byref(caption)),
                          (36, ctypes.byref(text)),
                          (34, ctypes.byref(border))):
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, attr, ref, ctypes.sizeof(ctypes.c_int))
    except Exception:
        pass


def _now() -> float:
    return time.time()


def _time_text(ts: float) -> str:
    if not ts:
        return "—"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts)))


EMO_VALUE = {
    "好奇": 5, "心动": 6, "开心": 5, "委屈": 4, "安心": 4, "平静": 3,
    "亲近": 5, "厌恶": 4, "渴望": 5, "被理解": 5, "期待": 5, "感激": 4,
    "专注": 4, "疲惫": 3, "说不清": 2, "低落": 3, "失望": 4, "不安": 4,
    "生气": 5, "孤独": 3, "内疚": 3,
}


# ---- 光斑缓存 -------------------------------------------------------------
class GlowCache:
    """按颜色与像素直径缓存径向光斑，Tk canvas 只换图，不逐帧重画渐变。"""

    def __init__(self) -> None:
        self._photos: dict[tuple[str, int], ImageTk.PhotoImage] = {}

    def photo(self, color: str, size: int) -> ImageTk.PhotoImage:
        size = max(8, min(512, int(round(float(size) / 4.0) * 4)))
        key = (str(color), size)
        hit = self._photos.get(key)
        if hit is not None:
            return hit
        arr = np.zeros((size, size, 4), dtype=np.uint8)
        yy, xx = np.mgrid[0:size, 0:size]
        cx = (size - 1) / 2.0
        d = np.sqrt((xx - cx) ** 2 + (yy - cx) ** 2) / max(1.0, size / 2.0)
        alpha = np.interp(d, [0.0, 0.10, 0.26, 0.55, 1.0],
                          [250, 188, 82, 24, 0]).astype(np.uint8)
        try:
            raw = color.lstrip("#")
            r, g, b = int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16)
        except Exception:
            r, g, b = 195, 204, 219
        arr[:, :, 0] = r
        arr[:, :, 1] = g
        arr[:, :, 2] = b
        arr[:, :, 3] = alpha
        photo = ImageTk.PhotoImage(Image.fromarray(arr, "RGBA"))
        self._photos[key] = photo
        return photo


class ScrollPanel(tk.Frame):
    """窄面板的竖向滚动容器。窗口矮的时候，属性不能直接掉出屏幕。"""

    def __init__(self, master: tk.Misc, bg: str) -> None:
        super().__init__(master, bg=bg)
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0,
                                height=560, yscrollincrement=18)
        self.scroll = tk.Scrollbar(self, orient="vertical", width=7,
                                   command=self.canvas.yview,
                                   bg=bg, troughcolor=bg,
                                   activebackground="#2a3a55", bd=0,
                                   highlightthickness=0)
        self.scroll.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas.configure(yscrollcommand=self.scroll.set)
        self.inner = tk.Frame(self.canvas, bg=bg)
        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.bind("<Configure>", self._on_canvas)
        self.inner.bind("<Configure>", self._on_inner)
        self.canvas.bind("<Enter>", lambda _e: self.canvas.bind_all(
            "<MouseWheel>", self._on_wheel))
        self.canvas.bind("<Leave>", lambda _e: self.canvas.unbind_all("<MouseWheel>"))

    def _on_canvas(self, event: tk.Event) -> None:
        self.canvas.itemconfigure(self._win, width=max(1, event.width))

    def _on_inner(self, _event: tk.Event) -> None:
        bbox = self.canvas.bbox("all")
        if bbox and str(self.canvas.cget("scrollregion")) != " ".join(map(str, bbox)):
            self.canvas.configure(scrollregion=bbox)

    def _on_wheel(self, event: tk.Event) -> str:
        try:
            step = int(-event.delta / 120)
            if step:
                self.canvas.yview_scroll(step, "units")
        except Exception:
            pass
        return "break"


# ---- 聊天气泡 -------------------------------------------------------------
class ChatLog(tk.Frame):
    """可滚动的轻量聊天气泡：最多留 12 条，和网页版一致。"""

    def __init__(self, master: tk.Misc, font: tkfont.Font, small_font: tkfont.Font,
                 user_font: Optional[tkfont.Font] = None) -> None:
        super().__init__(master, bg=CARD_BG)
        self.font = font
        self.user_font = user_font or font
        self.small_font = small_font
        self.canvas = tk.Canvas(self, bg=CARD_BG, highlightthickness=0,
                                height=126, bd=0, yscrollincrement=16)
        self.canvas.pack(fill="both", expand=True)
        self.inner = tk.Frame(self.canvas, bg=CARD_BG)
        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.bind("<Configure>", self._on_canvas)
        self.inner.bind("<Configure>", self._on_inner)
        self.canvas.bind("<Enter>", lambda _e: self.canvas.bind_all("<MouseWheel>", self._on_wheel))
        self.canvas.bind("<Leave>", lambda _e: self.canvas.unbind_all("<MouseWheel>"))
        self.labels: list[tk.Widget] = []

    def _on_canvas(self, event: tk.Event) -> None:
        self.canvas.itemconfigure(self._win, width=max(1, event.width))

    def _on_inner(self, _event: tk.Event) -> None:
        bbox = self.canvas.bbox("all")
        if bbox and str(self.canvas.cget("scrollregion")) != " ".join(map(str, bbox)):
            self.canvas.configure(scrollregion=bbox)

    def _on_wheel(self, event: tk.Event) -> str:
        try:
            step = int(-event.delta / 120)
            if step:
                self.canvas.yview_scroll(step, "units")
        except Exception:
            pass
        return "break"

    def _scroll(self) -> None:
        self.canvas.update_idletasks()
        self.canvas.yview_moveto(1.0)

    def add(self, kind: str, text: str) -> None:
        text = str(text or "")
        if not text:
            return
        # 网页端没有气泡：她的话是正文，用户的话靠右退成灰色，工具话最小。
        if kind == "user":
            bg, fg = CARD_BG, DIM
            font = self.user_font
            anchor, justify = "e", "right"
            padx = (44, 0)
        elif kind == "her":
            bg, fg = CARD_BG, INK
            font = self.font
            anchor, justify = "w", "left"
            padx = (0, 32)
        elif kind == "think":
            bg, fg = CARD_BG, DIM
            font = self.small_font
            anchor, justify = "w", "left"
            padx = (4, 36)
        else:
            bg, fg = CARD_BG, CORAL
            font = self.small_font
            anchor, justify = "center", "center"
            padx = (0, 0)
        label = tk.Label(
            self.inner, text=text, bg=bg, fg=fg, font=font,
            justify=justify, anchor=anchor, wraplength=500,
            padx=0, pady=5,
        )
        label.pack(anchor=anchor, padx=padx, pady=(1, 3))
        self.labels.append(label)
        while len(self.labels) > 12:
            old = self.labels.pop(0)
            try:
                old.destroy()
            except Exception:
                pass
        self._scroll()

    def clear(self) -> None:
        for w in self.labels:
            try:
                w.destroy()
            except Exception:
                pass
        self.labels.clear()


def _read_live_state() -> dict:
    """桌面版的 /api/state：只读一次她此刻的面板数据，不消费休眠记录。

    网页版每次打开页面都会单独取一份实时状态；桌面版不经过 HTTP，
    所以在这里做同样的事。任何一块失败都不拖垮整张图。
    """
    out: dict = {}
    mind = mem = None
    learn_obj = None
    try:
        from .memory import Memory
        from .mind import Mind
        mind, mem = Mind(), Memory()
        try:
            snap = mind.snapshot()
            out["state"] = snap
        except Exception:
            snap = None
        try:
            out["memory"] = mem.stats()
            out["ambient"] = mem.ambient()
        except Exception:
            pass
        try:
            from .learning import Learning
            from .autonomy import Autonomy
            learn_obj = Learning()
            out["learn"] = learn_obj.stats()
            out["autonomy"] = Autonomy(learning=learn_obj, mem=mem, mind=mind).brief(
                state=snap or {})
        except Exception:
            pass
        try:
            from . import emotion as _emotion
            out["expression"] = _emotion.plan(snap or {}, None, out.get("ambient") or {})
        except Exception:
            pass
        try:
            from .sleep import status as sleep_status
            out["sleep"] = sleep_status()
        except Exception:
            pass
        if snap:
            out["feel"] = snap.get("feel") or {}
            out["feel_text"] = snap.get("feel_text") or ""
        return out
    finally:
        if learn_obj is not None:
            try:
                learn_obj.close()
            except Exception:
                pass
        if mem is not None:
            try:
                mem.close()
            except Exception:
                pass
        if mind is not None:
            try:
                mind.close()
            except Exception:
                pass


# ---- 主窗口 ---------------------------------------------------------------
class DesktopApp:
    def __init__(self, root: tk.Tk, demo: bool = False,
                 screenshot: Optional[Path] = None,
                 screenshot_exit: bool = False,
                 geometry: str = "1380x880",
                 open_view: str = "") -> None:
        self.root = root
        self.demo = bool(demo)
        self.screenshot = screenshot
        self.screenshot_exit = screenshot_exit
        self._geometry = geometry or "1380x880"
        self._open_view_on_load = str(open_view or "")
        self.graph: Optional[dict] = None
        self.birth = False
        self.meta: dict = {}
        self.nodes: list[dict] = []
        self.edges: list[dict] = []
        self.by_id: dict[str, dict] = {}
        self.act: dict[str, tuple[float, int]] = {}
        self.picked: Optional[dict] = None
        self.hover: Optional[dict] = None
        self.pulse = 0.0
        self.t_now = 1.0
        self.t_min = 0.0
        self.t_max = 0.0
        self.W = 0
        self.H = 0
        self.dragging = False
        self.drag_data: Optional[dict] = None
        self.last_click = 0.0
        self.last_click_pos = (0, 0)
        self.cam = {"yaw": 0.62, "pitch": 0.42, "zoom": 1.0,
                    "tzoom": 1.0, "vyaw": 0.0, "vpitch": 0.0,
                    "tween": None}
        self._last_t = 0.0
        self._tick_n = 0
        self._tick_job: Optional[str] = None
        self._resize_job: Optional[str] = None
        self._closing = False
        self._render_dirty = True
        self._anim_until = 0.0
        self._last_render = 0.0
        self._order_key: Optional[tuple[str, ...]] = None
        self._edge_drawn: set[int] = set()
        self._label_visible: set[str] = set()
        self._fast_mode = False
        self._chat: Any = None
        self._chat_lock = threading.Lock()
        self._ui_queue: "queue.Queue[Any]" = queue.Queue()
        self._busy = False
        self._voice_busy = False
        self._stt: Any = None
        self._tts: Any = None
        self._tts_lock = threading.Lock()
        self._glow = GlowCache()
        self._photos: dict[str, ImageTk.PhotoImage] = {}
        self._edge_items: list[tuple[int, dict]] = []
        self._node_items: dict[str, dict] = {}
        self._label_items: dict[str, dict] = {}
        self._birth_items: dict[str, Any] = {}
        self._twinkle_items: list[tuple[int, dict]] = []
        self._dust_items: list[tuple[int, dict]] = []
        self._layer_items: list[dict] = []
        self._selection_items: list[int] = []
        self._core_item: Optional[int] = None
        self._say_clear_job: Optional[str] = None
        self._loaded_at = 0.0
        self._clock_job: Optional[str] = None
        self._last_panel_update = 0.0
        self.view_kind = ""
        self.view_payload: dict = {}
        self.view_loading = False
        self.echo_only = False
        self.view_btns: dict[str, tk.Button] = {}
        self._diary_lock = threading.Lock()
        self.chat_open = False
        self.more_open = False
        self.bar_widths: dict[str, int] = {}

        # DPI 感知必须在 Tk() 之前设，否则 Windows 会把 1380×880 的窗口
        # 缩到 935×624，而 Tk 仍按 1380×880 排版，右侧和底部会被裁掉。
        self._configure_root()
        self._setup_fonts()
        self._setup_icon()
        _dark_titlebar(self.root)
        self._build_ui()
        self._bind_events()
        self._tick()
        # 桌面端现在是主入口：日记定时器跟着桌面进程跑。
        # 8765 网页备份同时开着时，文件锁保证一天只写一次。
        if not self.demo:
            try:
                from .diary import start_scheduler
                start_scheduler()
            except Exception:
                pass
        self.root.after(120, self._load_async)
        if screenshot:
            self.root.after(2600, self._capture)

    # ---- 窗口与基础 -------------------------------------------------------
    def _configure_root(self) -> None:
        self.root.title("瓦尔基里 · 识海")
        self.root.configure(bg=BG)
        try:
            self.root.geometry(self._geometry)
        except Exception:
            self.root.geometry("1380x880")
        self.root.minsize(1080, 720)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _setup_fonts(self) -> None:
        # 网页的字号是 CSS px：brand 17、正文 12、mono 10。Tk 用 pt，
        # 这里按 96dpi 取近似值，别让桌面版比网页胖一圈。
        self.f_brand = tkfont.Font(family=FONT, size=16, weight="bold")
        self.f_sub = tkfont.Font(family=FONT, size=9)
        self.f_body = tkfont.Font(family=FONT, size=10)
        self.f_head = tkfont.Font(family=SERIF, size=14, weight="bold")
        self.f_title = tkfont.Font(family=SERIF, size=14, weight="bold")
        self.f_small = tkfont.Font(family=FONT, size=9)
        self.f_tiny = tkfont.Font(family=MONO, size=8)
        self.f_mono = tkfont.Font(family=MONO, size=8)
        self.f_chat = tkfont.Font(family=SERIF, size=10)
        self.f_chat_user = tkfont.Font(family=FONT, size=9)
        self.f_chat_small = tkfont.Font(family=FONT, size=8)
        self.f_panel = tkfont.Font(family=MONO, size=8)
        self.f_val = tkfont.Font(family=FONT, size=9)
        self.f_hot = tkfont.Font(family=SERIF, size=10)

    def _setup_icon(self) -> None:
        """给窗口换掉 Tk 默认的羽毛图标：一颗带环的蓝星。"""
        try:
            img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
            d = ImageDraw.Draw(img, "RGBA")
            d.ellipse((4, 4, 60, 60), fill=(80, 160, 255, 20))
            d.ellipse((10, 10, 54, 54), outline=(125, 185, 255, 65), width=1)
            d.polygon([(32, 10), (36, 26), (52, 30), (37, 36),
                       (32, 54), (27, 36), (12, 30), (28, 26)],
                      fill=(226, 240, 255, 255))
            d.ellipse((27, 27, 37, 37), fill=(120, 200, 255, 255))
            self._icon = ImageTk.PhotoImage(img)
            self.root.iconphoto(True, self._icon)
        except Exception:
            pass

    # ---- 界面 -------------------------------------------------------------
    def _build_ui(self) -> None:
        self.canvas = tk.Canvas(self.root, bg=BG, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)

        self.ui_top = tk.Frame(self.root, bg=BG)
        self.ui_top.place(x=0, y=0, relwidth=1.0, height=58)
        # 8767 的 topbar 没有大标题和图标：左边是拉开字距的「识 海」，
        # 右边是来历 / 人和物 / 连接，最右一枚数据徽章。
        self.brand = tk.Label(self.ui_top, text="识  海", bg=BG, fg="#eaf1ff",
                              font=self.f_brand)
        self.brand.pack(side="left", padx=(22, 14))
        self.sub = tk.Label(self.ui_top, text="外层是新的经历，越往里越沉淀",
                            bg=BG, fg=DIM, font=self.f_sub)
        self.sub.pack(side="left")
        self.stats = tk.Label(self.ui_top, text="", bg=BG, fg="#9fb0c9",
                              font=self.f_mono)
        self.stats.pack(side="right", padx=(0, 12))
        self.badge = tk.Label(self.ui_top, text="示例数据", bg="#2a1810", fg=AMBER,
                              font=self.f_mono, padx=8, pady=2,
                              bd=0, relief="flat",
                              highlightbackground="#6a3d2a",
                              highlightthickness=1)
        self.badge.pack(side="right", padx=(0, 10))
        self.web_btn = tk.Button(
            self.ui_top, text="网页版", command=self._open_sihai_web,
            bg=BG, fg=SUB, activebackground=PANEL_BG, activeforeground=INK,
            bd=0, relief="flat", font=self.f_mono, padx=8, pady=3,
            cursor="hand2")
        self.web_btn.pack(side="right", padx=(0, 6))
        self.menu_btn = tk.Menubutton(self.ui_top, text="数据", bg=BG,
                                      fg=DIM, activebackground=PANEL_BG,
                                      activeforeground=INK, bd=0, relief="flat",
                                      font=self.f_mono, padx=8, pady=3)
        menu = tk.Menu(self.menu_btn, tearoff=False, bg=PANEL_BG, fg=INK,
                       activebackground=LINE_2, activeforeground=INK,
                       font=self.f_mono)
        menu.add_command(label="真实数据", command=lambda: self._reload(False))
        menu.add_command(label="示例数据", command=lambda: self._reload(True))
        menu.add_separator()
        menu.add_command(label="打开识海网页（8767）", command=self._open_sihai_web)
        menu.add_command(label="刷新（F5）", command=lambda: self._reload(self.demo))
        self.menu_btn.configure(menu=menu)
        self.menu_btn.pack(side="right", padx=(0, 8))
        # 网页 topbar 是渐隐到透明，Tk 用一条 1px 底线代替。
        self.top_line = tk.Frame(self.root, bg=LINE)
        self.top_line.place(x=0, y=58, relwidth=1.0, height=1)
        # 兼容旧引用：不显示，只让状态更新不报错。
        self.status_dot = tk.Label(self.ui_top, text="", bg=BG, fg=SEA,
                                   font=tkfont.Font(family=FONT, size=1))

        self.ui_views = self._build_views()
        self.hint = tk.Label(self.root, text="", bg=BG, fg=DIM,
                             font=self.f_small, justify="left")
        self.zoom = tk.Label(self.root, text="", bg=BG, fg=DIM,
                             font=self.f_mono)

        self.ui_panel = self._build_panel()
        self.ui_card = self._build_card()
        self.ui_chat = self._build_chat()
        self.ui_insight = self._build_insight()
        self.ui_footer = self._build_footer()
        self.empty = tk.Label(self.root, text="", bg=BG, fg=DIM,
                              font=self.f_body, justify="center")
        self.loading = tk.Label(self.root, text="正在打开她的识海…", bg=BG,
                                fg=DIM, font=self.f_body)

        self._place_overlays()
        self.zoom.place(x=22, y=0)
        self.loading.place(relx=0.5, rely=0.47, anchor="center")
        self.root.bind("<Configure>", self._on_root_configure)

    def _build_views(self) -> tk.Frame:
        """8767 的悬浮胶囊导航：视图在最上，聊天是一个开关，不再是常驻窗口。"""
        bar = tk.Frame(self.root, bg="#070b14", bd=0,
                       highlightbackground="#222f42", highlightthickness=1,
                       padx=5, pady=4)
        for key, label in VIEWS:
            b = tk.Button(
                bar, text=label, command=lambda k=key: self._open_view(k),
                bg="#070b14", fg="#8fa0b7", activebackground="#16263a",
                activeforeground="#f3f7ff", bd=0, relief="flat",
                font=self.f_small, padx=10, pady=5, cursor="hand2",
                highlightthickness=1, highlightbackground="#070b14")
            b.pack(side="left", padx=2)
            self.view_btns[key] = b
        sep = tk.Frame(bar, bg="#222f42", width=1, height=18)
        sep.pack(side="left", padx=5)
        self.chat_btn = tk.Button(
            bar, text="说话", command=self._toggle_chat,
            bg="#070b14", fg="#8fa0b7", activebackground="#16263a",
            activeforeground="#f3f7ff", bd=0, relief="flat",
            font=self.f_small, padx=10, pady=5, cursor="hand2",
            highlightthickness=1, highlightbackground="#070b14")
        self.chat_btn.pack(side="left", padx=2)
        return bar

    def _build_insight(self) -> tk.Frame:
        f = tk.Frame(self.root, bg=CARD_BG, padx=18, pady=16,
                     highlightbackground="#2a3a52", highlightthickness=1)
        kick = tk.Frame(f, bg=CARD_BG)
        kick.pack(fill="x")
        self.view_kicker = tk.Label(kick, text="INSIGHT", bg=CARD_BG,
                                    fg="#7b91ad", font=self.f_tiny)
        self.view_kicker.pack(side="left")
        close = tk.Label(kick, text="×", bg=CARD_BG, fg="#8495ab",
                         font=tkfont.Font(family=FONT, size=14), padx=4)
        close.pack(side="right", anchor="n")
        close.bind("<Button-1>", lambda _e: self._close_view())
        self.view_title = tk.Label(f, text="她在想什么", bg=CARD_BG,
                                   fg="#f0f6ff", font=self.f_head,
                                   justify="left", anchor="w")
        self.view_title.pack(fill="x", pady=(6, 0))
        self.view_status = tk.Label(f, text="", bg=CARD_BG, fg="#8091a8",
                                    font=self.f_small, anchor="w",
                                    justify="left", wraplength=350)
        self.view_status.pack(fill="x", pady=(6, 8))
        box = tk.Frame(f, bg=CARD_BG)
        box.pack(fill="both", expand=True)
        self.view_text = tk.Text(box, bg=CARD_BG, fg="#d8e4f3",
                                 insertbackground=CARD_BG, relief="flat",
                                 bd=0, wrap="word", padx=4, pady=4,
                                 font=self.f_body, cursor="arrow",
                                 selectbackground="#21344f")
        scroll = tk.Scrollbar(box, orient="vertical", width=7,
                              command=self.view_text.yview,
                              bg=CARD_BG, troughcolor=CARD_BG,
                              activebackground="#2a3a55", bd=0,
                              highlightthickness=0)
        self.view_text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.view_text.pack(side="left", fill="both", expand=True)
        self.view_text.configure(state="disabled")
        self.view_text.tag_configure("h", foreground=INK,
                                     font=self.f_head,
                                     spacing1=10, spacing3=5)
        self.view_text.tag_configure("dim", foreground=DIM)
        self.view_text.tag_configure("warm", foreground=AMBER)
        self.view_text.tag_configure("link", foreground=SEA, underline=True)
        foot = tk.Frame(f, bg=CARD_BG)
        foot.pack(fill="x", pady=(8, 0))
        self.view_action = tk.Button(
            foot, text="刷新", command=self._view_action,
            bg="#14263d", fg="#bfd7f3", activebackground="#1d3656",
            activeforeground="#eef5ff", bd=0, relief="flat",
            font=self.f_small, padx=13, pady=5)
        self.view_action.pack(side="right")
        self.view_hint = tk.Label(foot, text="", bg=CARD_BG, fg=DIM,
                                  font=self.f_tiny, anchor="w", justify="left",
                                  wraplength=320)
        self.view_hint.pack(side="left", fill="x", expand=True)
        return f

    def _build_panel(self) -> tk.Frame:
        """左栏按 8767 的 .panel：她此刻 / 精力心情独处 / 关系 2×2 / 深处。"""
        outer = tk.Frame(self.root, bg=PANEL_BG, highlightbackground=LINE,
                         highlightthickness=1)
        self.panel_scroll = ScrollPanel(outer, PANEL_BG)
        self.panel_scroll.pack(fill="both", expand=True)
        p = tk.Frame(self.panel_scroll.inner, bg=PANEL_BG, padx=16, pady=14)
        p.pack(fill="both", expand=True)
        self.pv: dict[str, tk.Label] = {}
        self.pb: dict[str, tuple[tk.Canvas, int]] = {}
        self.bar_widths = {}

        head = tk.Frame(p, bg=PANEL_BG)
        head.pack(fill="x", pady=(0, 9))
        left = tk.Frame(head, bg=PANEL_BG)
        left.pack(side="left", fill="x", expand=True)
        tk.Label(left, text="她  此  刻", bg=PANEL_BG, fg="#7f8fa8",
                 font=self.f_panel).pack(anchor="w")
        sub = tk.Frame(left, bg=PANEL_BG)
        sub.pack(anchor="w", pady=(5, 0))
        tk.Label(sub, text="●", bg=PANEL_BG, fg=SEA,
                 font=tkfont.Font(family=FONT, size=5)).pack(side="left")
        tk.Label(sub, text=" 存在", bg=PANEL_BG, fg="#7689a2",
                 font=self.f_tiny).pack(side="left")
        self.pv["days"] = tk.Label(sub, text="—", bg=PANEL_BG, fg="#c6d6e9",
                                   font=self.f_tiny)
        self.pv["days"].pack(side="left", padx=(3, 0))
        tk.Label(head, text="⌁", bg=PANEL_BG, fg="#4e6684",
                 font=tkfont.Font(family=FONT, size=17)).pack(side="right",
                                                               anchor="n")

        def row(key: str, label: str, wrap: bool = False,
                parent: Optional[tk.Misc] = None) -> None:
            f = tk.Frame(parent or p, bg=PANEL_BG)
            f.pack(fill="x", pady=(3, 0))
            tk.Label(f, text=label, bg=PANEL_BG, fg="#8798ae",
                     font=self.f_tiny).pack(side="left")
            v = tk.Label(f, text="—", bg=PANEL_BG, fg="#dce7f5",
                         font=self.f_tiny if wrap else self.f_val,
                         anchor="e", justify="right",
                         wraplength=196 if wrap else 0,
                         width=24 if wrap else 0)
            if wrap:
                v.pack(side="right")
            else:
                v.pack(side="right", fill="x", expand=True)
            self.pv[key] = v

        def bar(key: str, color: str, width: int = 236,
                parent: Optional[tk.Misc] = None) -> None:
            c = tk.Canvas(parent or p, height=3, bg="#1a2638",
                          highlightthickness=0, bd=0, width=width)
            c.pack(fill="x", pady=(2, 4))
            fill = c.create_rectangle(0, 0, 0, 3, fill=color, outline="")
            self.pb[key] = (c, fill)
            self.bar_widths[key] = width

        def rule(text: str) -> None:
            f = tk.Frame(p, bg=PANEL_BG)
            f.pack(fill="x", pady=(9, 6))
            tk.Label(f, text=text, bg=PANEL_BG, fg="#5f738c",
                     font=self.f_tiny).pack(side="left")
            tk.Frame(f, height=1, bg=LINE).pack(side="left", fill="x",
                                                 expand=True, padx=(9, 0))

        row("energy", "精力")
        bar("energy", "#6fd6ff")
        row("mood", "心情")
        bar("mood", "#ffb35c")
        row("lonely", "一个人待着")
        bar("lonely", "#8b7cf6")

        rule("关系")
        rel = tk.Frame(p, bg=PANEL_BG)
        rel.pack(fill="x")
        rel.columnconfigure(0, weight=1)
        rel.columnconfigure(1, weight=1)
        for i, (key, label, color) in enumerate([
                ("fam", "熟悉", "#7de08a"), ("trust", "信任", "#5fe0ff"),
                ("attach", "依恋", "#ff7ab8"), ("boundary", "边界", "#ffc94d")]):
            cell = tk.Frame(rel, bg=PANEL_BG)
            cell.grid(row=i // 2, column=i % 2, sticky="ew",
                      padx=(0, 10), pady=(0, 7))
            h = tk.Frame(cell, bg=PANEL_BG)
            h.pack(fill="x")
            tk.Label(h, text=label, bg=PANEL_BG, fg="#8798ae",
                     font=self.f_tiny).pack(side="left")
            v = tk.Label(h, text="—", bg=PANEL_BG, fg="#dce7f5",
                         font=self.f_tiny, anchor="e")
            v.pack(side="right")
            self.pv[key] = v
            bar(key, color, width=104, parent=cell)

        self.pv["feel"] = tk.Label(
            p, text="—", bg=PANEL_BG, fg="#a8b7cc", font=self.f_tiny,
            justify="left", anchor="w", wraplength=230)
        self.pv["feel"].pack(fill="x", pady=(6, 0))

        rule("深处")
        row("sihai", "识海", wrap=True)
        row("dispatch", "调度", wrap=True)
        row("learn", "学习", wrap=True)

        self.more_btn = tk.Button(
            p, text="＋ 七情 · 六欲 · 表达", command=self._toggle_more,
            bg=PANEL_BG, fg="#61758e", activebackground=PANEL_BG,
            activeforeground="#a5bad2", bd=0, relief="flat",
            font=self.f_tiny, anchor="w", padx=0, pady=2, cursor="hand2",
            highlightthickness=0)
        self.more_btn.pack(fill="x", pady=(7, 0))

        self.more_frame = tk.Frame(p, bg=PANEL_BG)
        row("seven", "七情", wrap=True, parent=self.more_frame)
        row("desire", "六欲", wrap=True, parent=self.more_frame)
        row("echo", "残响", wrap=True, parent=self.more_frame)
        row("expr", "表达", wrap=True, parent=self.more_frame)

        self.lock_line = tk.Label(
            p, text="成长：冻结\n学习照常跑，但只进「待审核」区，不写进记忆。",
            bg=PANEL_BG, fg="#c9a06a", font=self.f_tiny,
            justify="left", anchor="w", wraplength=230)
        self.lock_line.pack(fill="x", pady=(7, 0))
        return outer

    def _toggle_more(self) -> None:
        self.more_open = not self.more_open
        if self.more_open:
            self.more_frame.pack(fill="x", pady=(4, 0), before=self.lock_line)
            self.more_btn.configure(text="－ 七情 · 六欲 · 表达")
        else:
            self.more_frame.pack_forget()
            self.more_btn.configure(text="＋ 七情 · 六欲 · 表达")

    def _build_card(self) -> tk.Frame:
        c = tk.Frame(self.root, bg=CARD_BG, padx=15, pady=13,
                     highlightbackground=LINE_2, highlightthickness=1)
        head = tk.Frame(c, bg=CARD_BG)
        head.pack(fill="x")
        self.card_title = tk.Label(head, text="—", bg=CARD_BG, fg=INK,
                                   font=self.f_head, justify="left", anchor="w",
                                   wraplength=292)
        self.card_title.pack(side="left", fill="x", expand=True)
        close = tk.Label(head, text="×", bg=CARD_BG, fg=DIM,
                         font=tkfont.Font(family=FONT, size=14), padx=4)
        close.pack(side="right", anchor="n")
        close.bind("<Button-1>", lambda _e: self.clear_pick())
        self.card_tags = tk.Frame(c, bg=CARD_BG)
        self.card_tags.pack(fill="x", pady=(6, 6))
        self.card_rows: dict[str, tk.Label] = {}
        for key, label in [
            ("time", "时间"), ("emo", "情绪"), ("ret", "细节留存"),
            ("mean", "意义"), ("res", "情绪残留"), ("echo", "痕迹"),
            ("imp", "重要程度"), ("used", "被想起过"), ("deg", "连着的线"),
        ]:
            f = tk.Frame(c, bg=CARD_BG)
            f.pack(fill="x", pady=1)
            tk.Label(f, text=label, bg=CARD_BG, fg=DIM,
                     font=self.f_mono).pack(side="left")
            v = tk.Label(f, text="—", bg=CARD_BG, fg=INK_SOFT,
                         font=self.f_small, anchor="e")
            v.pack(side="right")
            self.card_rows[key] = v
        self.card_body = tk.Text(c, height=5, bg=CARD_BG, fg=INK_SOFT,
                                 font=self.f_body, relief="flat", bd=0,
                                 wrap="word", padx=0, pady=0,
                                 insertbackground=CARD_BG)
        self.card_body.pack(fill="x", pady=(8, 2))
        self.card_body.configure(state="disabled")
        self.card_rel = tk.Label(c, text="", bg=CARD_BG, fg=DIM,
                                 font=self.f_mono, justify="left", anchor="w",
                                 wraplength=292)
        self.card_rel.pack(fill="x", pady=(4, 0))
        return c

    def _build_chat(self) -> tk.Frame:
        f = tk.Frame(self.root, bg=CARD_BG, padx=14, pady=10,
                     highlightbackground="#2a3a52", highlightthickness=1)
        head = tk.Frame(f, bg=CARD_BG)
        head.pack(fill="x", pady=(0, 5))
        tk.Label(head, text="跟她说一句话", bg=CARD_BG, fg="#8798ae",
                 font=self.f_tiny).pack(side="left")
        self.chat_status = tk.Label(head, text="她还在睡", bg=CARD_BG,
                                    fg="#c9a06a", font=self.f_tiny)
        self.chat_status.pack(side="right")
        self.chatlog = ChatLog(f, self.f_chat, self.f_chat_small,
                               user_font=self.f_chat_user)
        self.chatlog.pack(fill="both", expand=True)
        row = tk.Frame(f, bg=CARD_BG)
        row.pack(fill="x", pady=(6, 0))
        self.entry = tk.Entry(row, bg="#0a1422", fg=INK, insertbackground=SEA,
                              relief="flat", bd=0, font=self.f_body,
                              highlightthickness=1, highlightbackground="#24344c",
                              highlightcolor="#5f8fd8")
        self.entry.pack(side="left", fill="x", expand=True, ipady=7)
        self.send_btn = tk.Button(row, text="说", command=self._send,
                                  bg="#2d5c9e", fg="#eaf3ff",
                                  activebackground="#3a74c0",
                                  activeforeground="#ffffff", bd=0,
                                  relief="flat", font=self.f_body,
                                  padx=15, pady=5)
        self.send_btn.pack(side="left", padx=(7, 0))
        self.mic_btn = tk.Button(row, text="语音", command=self._voice_start,
                                 bg="#0a1422", fg=SUB, activebackground="#16263a",
                                 activeforeground=INK, bd=0, relief="flat",
                                 font=self.f_small, padx=9, pady=6)
        self.mic_btn.pack(side="left", padx=(5, 0))
        self.read_var = tk.BooleanVar(value=False)
        self.read_chk = tk.Checkbutton(
            row, text="朗读", variable=self.read_var, bg=CARD_BG, fg=DIM,
            activebackground=CARD_BG, activeforeground=INK,
            selectcolor="#0a1422", font=self.f_tiny, bd=0,
            highlightthickness=0)
        self.read_chk.pack(side="left", padx=(5, 0))
        self.entry.bind("<Return>", lambda _e: self._send())
        return f

    def _build_footer(self) -> tk.Frame:
        f = tk.Frame(self.root, bg=BG)
        self.say = tk.Label(f, text="", bg=BG, fg="#cfd9ea",
                            font=self.f_body, justify="left", anchor="w",
                            wraplength=760)
        self.say.pack(fill="x", pady=(0, 6))
        tl = tk.Frame(f, bg=BG)
        tl.pack(fill="x")
        tk.Label(tl, text="时间流逝", bg=BG, fg=DIM,
                 font=self.f_tiny).pack(side="left")
        self.scale = tk.Scale(tl, from_=0, to=1000, orient="horizontal",
                              showvalue=0, command=self._on_time,
                              bg=BG, fg=INK, troughcolor="#142235",
                              activebackground=SEA, highlightthickness=0,
                              bd=0, sliderlength=11, width=6)
        self.scale.set(1000)
        self.scale.pack(side="left", fill="x", expand=True, padx=9)
        tk.Label(tl, text="现在", bg=BG, fg=DIM,
                 font=self.f_tiny).pack(side="left")
        self.label_all = tk.BooleanVar(value=False)
        self.label_chk = tk.Checkbutton(
            tl, text="标签", variable=self.label_all, bg=BG, fg=DIM,
            activebackground=BG, activeforeground=INK, selectcolor=BG_2,
            font=self.f_mono, bd=0, highlightthickness=0)
        self.label_chk.pack(side="right", padx=(8, 0))
        self.legend = tk.Frame(f, bg=BG)
        self.legend.pack(fill="x", pady=(7, 0))
        return f

    def _place_overlays(self) -> None:
        w = max(1, self.root.winfo_width())
        h = max(1, self.root.winfo_height())
        # 8767 的 .dock 是屏幕顶部居中的悬浮胶囊，不再靠左排方块。
        self.ui_views.place_forget()
        self.ui_views.place(relx=0.5, y=64, anchor="n")
        panel_h = max(220, h - 122 - (26 if self.birth else 110))
        if self.birth:
            self.ui_panel.place_forget()
            self.ui_panel.place(x=w - 22 - 272, y=122, width=272,
                                height=panel_h)
            self.panel_scroll.canvas.configure(height=panel_h)
            self.hint.place(x=22, y=h - 36, anchor="sw")
            self.ui_footer.place_forget()
            self.ui_chat.place(relx=0.5, y=h - 18, anchor="s",
                               width=620, height=230)
            self.ui_chat.lift()
        else:
            self.ui_panel.place_forget()
            self.ui_panel.place(x=22, y=122, width=272, height=panel_h)
            self.panel_scroll.canvas.configure(height=panel_h)
            self.hint.place(x=22, y=102)
            if self.chat_open:
                self.ui_footer.place_forget()
                self.ui_chat.place(relx=0.5, y=h - 18, anchor="s",
                                   width=620, height=230)
                self.ui_chat.lift()
            else:
                self.ui_chat.place_forget()
                self.ui_footer.place(x=22, y=h - 16, anchor="sw",
                                     width=max(320, w - 44))
                self.ui_footer.lift()
            self.ui_panel.lift()
        if self.view_kind:
            self.ui_chat.place_forget()
            self.ui_card.place_forget()
            self.ui_insight.place(relx=1.0, x=-20, y=118, anchor="ne",
                                  width=392, height=min(650, h - 174))
            self.ui_insight.lift()
        else:
            self.ui_insight.place_forget()
            if self.picked:
                self.ui_card.place(relx=1.0, x=-20, y=72, anchor="ne", width=340)
        self.zoom.place(x=w - 22, y=h - 26, anchor="e")

    # ---- 五块独立视图 -----------------------------------------------------
    def _ensure_chat(self):
        with self._chat_lock:
            if self._chat is None:
                from .chat import Chat
                self._chat = Chat()
            return self._chat

    def _style_dock(self) -> None:
        for key, btn in self.view_btns.items():
            on = key == self.view_kind
            btn.configure(
                bg="#12243a" if on else "#070b14",
                fg="#f3f7ff" if on else "#8fa0b7",
                highlightbackground="#2a4a6a" if on else "#070b14")
        if hasattr(self, "chat_btn"):
            on = bool(self.chat_open)
            self.chat_btn.configure(
                bg="#12243a" if on else "#070b14",
                fg="#f3f7ff" if on else "#8fa0b7",
                highlightbackground="#2a4a6a" if on else "#070b14")

    def _open_view(self, kind: str) -> None:
        if kind == self.view_kind:
            self._close_view()
            return
        self.view_kind = kind
        self.view_loading = False
        self.chat_open = False
        self._style_dock()
        self.view_title.configure(text=dict(VIEWS).get(kind, kind))
        self._set_view_text("", "读取中…")
        self._place_overlays()
        self._refresh_view()

    def _close_view(self) -> None:
        self.view_kind = ""
        self.view_loading = False
        self.echo_only = False
        self._style_dock()
        self._place_overlays()
        self._rebuild_items()

    def _toggle_chat(self) -> None:
        """8767 的聊天只在出生期常驻；桌面版把它收成导航里的一个开关。"""
        self.chat_open = not self.chat_open
        if self.chat_open and self.view_kind:
            self.view_kind = ""
            self.view_loading = False
            self.echo_only = False
            self._rebuild_items()
        self._style_dock()
        self._place_overlays()
        if self.chat_open:
            self.root.after(60, self.entry.focus_set)

    def _set_view_text(self, text: str, status: str = "") -> None:
        self.view_text.configure(state="normal")
        self.view_text.delete("1.0", "end")
        if text:
            self.view_text.insert("end", text)
        self.view_text.configure(state="disabled")
        self.view_text.yview_moveto(0.0)
        self.view_status.configure(text=status)

    def _refresh_view(self) -> None:
        if not self.view_kind or self.view_loading:
            return
        kind = self.view_kind
        self.view_loading = True
        self.view_status.configure(text="正在读她那一边…")

        def work() -> None:
            payload: dict = {}
            err = ""
            try:
                if kind == "diary":
                    from .diary import Diary
                    d = Diary()
                    s = d.summary(limit=10)
                    day = (s.get("today") if s.get("has_today")
                           else ((s.get("recent") or [{}])[0].get("day") if s.get("recent") else ""))
                    s["current"] = d.read(day) if day else {}
                    payload["diary"] = s
                else:
                    chat = self._ensure_chat()
                    with self._chat_lock:
                        payload = chat.panel_payload()
                    from .diary import Diary
                    payload["diary"] = Diary().summary(limit=6)
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
            self._post(lambda p=payload, x=err: self._view_loaded(kind, p, x))

        threading.Thread(target=work, name="desktop-view", daemon=True).start()

    def _view_loaded(self, kind: str, payload: dict, err: str) -> None:
        if self._closing or kind != self.view_kind:
            return
        self.view_loading = False
        if err:
            self._set_view_text("这一块暂时读不到：\n" + err, "读取失败")
            return
        self.view_payload = payload
        self._render_view(kind, payload)

    def _render_view(self, kind: str, payload: dict) -> None:
        self.view_text.configure(state="normal")
        self.view_text.delete("1.0", "end")

        def put(text: str, tag: str = "") -> None:
            self.view_text.insert("end", text, tag or ())

        if kind == "thought":
            t = payload.get("thought") or {}
            put("此刻\n", "h")
            put(str(t.get("line") or "她还没有留下可读的念头。") + "\n\n")
            if t.get("idle"):
                put("这是静息态：没有为了看一眼而强行跑一轮召回。\n\n", "dim")
            tr = t.get("trace") or []
            if tr:
                put("她这一轮怎么想的\n", "h")
                for line in tr:
                    put(str(line) + "\n")
                put("\n")
            if t.get("awareness"):
                put("意识里亮着的\n", "h")
                put(str(t.get("awareness")) + "\n")
            self.view_action.configure(text="刷新", state="normal")
            self.view_hint.configure(text="不是模型的隐藏思维，是 think() 已经算出来的那一轮。")
            self.view_status.configure(text="上一轮 · 她刚刚" if not t.get("idle") else "静息态")
        elif kind == "dispatch":
            d = payload.get("dispatch") or {}
            last = d.get("last") or {}
            put("这一次她先想起谁\n", "h")
            if last:
                put(" → ".join(str(x) for x in (last.get("chain") or [])) + "\n\n")
                put(f"候选 {last.get('candidates', 0)} 个 · 唤醒 {last.get('awakened', 0)} 条 · "
                    f"扩散 {last.get('spread', 0)} 条 · θ={last.get('theta', 0)}\n\n", "dim")
                aware = last.get("aware") or []
                if aware:
                    put("被照见\n", "h")
                    for x in aware:
                        put(f"· {x.get('label')}  A={x.get('A')}  [{x.get('kind')}]\n")
                    put("\n")
                sub = last.get("subliminal") or []
                if sub:
                    put("只是存在、还没进意识\n", "h")
                    for x in sub:
                        put(f"· {x.get('label')}  A={x.get('A')}\n")
                    put("\n")
                if last.get("awareness_extra"):
                    put("会写进提示的\n", "h")
                    put(str(last["awareness_extra"]) + "\n")
            else:
                put("还没有调度记录。\n\n", "dim")
                put("跟她说一句话之后，唤醒 → 竞争 → 照见 → 心流的链条会在这里亮起来。\n")
            self.view_status.configure(text=f"θ={d.get('theta', '—')} · 待审 {d.get('pending', 0)} 条")
            self.view_action.configure(text="刷新", state="normal")
            self.view_hint.configure(text="点一颗星，图上会顺着它亮起相连的线。")
        elif kind == "learning":
            l = payload.get("learning") or {}
            put("她最近在学什么\n", "h")
            put(f"待审 {l.get('pending', 0)} 条 · 已审 {l.get('approved', 0)} 条 · "
                f"好奇 {l.get('curiosities', 0)} 件 · 自习 {l.get('studies', 0)} 次\n\n", "dim")
            last = l.get("last_study") or {}
            if last:
                put("上一次自习\n", "h")
                put(f"{last.get('day') or ''} {last.get('status') or ''} · "
                    f"加了 {last.get('added', 0)} 条待审\n")
                if last.get("question"):
                    put("在琢磨：" + str(last["question"]) + "\n", "warm")
                put("\n")
            pending = payload.get("pending") or []
            put("待审材料\n", "h")
            if pending:
                for x in pending:
                    cur = x.get("curiosity_q") or x.get("for_curiosity") or ""
                    put(f"· {str(x.get('title') or '未命名')[:60]}\n")
                    if cur:
                        put("  为：" + str(cur)[:60] + "\n", "dim")
            else:
                put("暂时没有新的待审材料。\n", "dim")
            self.view_status.configure(text="学习只进待审区，不直接写进记忆")
            self.view_action.configure(text="刷新", state="normal")
            self.view_hint.configure(text="桌面端与网页端读同一份学习库。")
        elif kind == "echo":
            a = payload.get("echo") or {}
            put("忘掉的是事，留下的是这层感觉\n", "h")
            if a.get("text"):
                put(str(a["text"]) + "\n\n", "warm")
            put(f"强度 {float(a.get('intensity') or 0):.2f} · 来源 {a.get('sources', 0)} 处 · "
                f"说不清 {a.get('unknown', 0)} 处\n\n", "dim")
            imps = a.get("imprints") or []
            if imps:
                put("留下的痕迹\n", "h")
                for im in imps:
                    if isinstance(im, dict):
                        put(f"· {im.get('text')}  {float(im.get('strength') or 0):.2f}\n")
                    else:
                        put(f"· {im}\n")
                put("\n")
            feel = a.get("feel_text")
            if feel:
                put("情绪场\n", "h")
                put(str(feel) + "\n")
            ambient_n = sum(1 for n in self.nodes if n.get("is_ambient"))
            echo_n = sum(1 for n in self.nodes if n.get("is_echo"))
            put(f"\n图上：灰环 {echo_n} 颗 · 琥珀环 {ambient_n} 颗\n", "dim")
            self.view_status.configure(text="残响不是被想起的内容，只进语气和那点说不清的感觉")
            self.view_action.configure(text="看全部" if self.echo_only else "只看残响",
                                       state="normal")
            self.view_hint.configure(text="只看残响时，其余星会压暗；点星仍可读详情。")
        elif kind == "diary":
            d = payload.get("diary") or {}
            put("她的日记\n", "h")
            put(f"每天 {d.get('write_at', '23:00')} 写 · 文件夹 {d.get('folder', 'robot/diary')}\n\n", "dim")
            cur = d.get("current") or {}
            if cur.get("ok"):
                put(f"{cur.get('day')} · {cur.get('chars', 0)} 字\n", "link")
                put(str(cur.get("text") or "") + "\n\n")
            else:
                put("今天还没有写。\n\n", "warm")
                put("点下面的按钮可以让她现在写；23:00 会自动写。\n\n")
            recent = d.get("recent") or []
            if recent:
                put("最近几页\n", "h")
                for r in recent[:8]:
                    put(f"· {r.get('day')}  {r.get('chars', 0)} 字  {r.get('source', '')}\n")
            self.view_status.configure(text="日记独立于识海，不参与召回权重")
            self.view_action.configure(text="写今天的日记", state="normal")
            self.view_hint.configure(text="写日记只读记忆，不走 Chat.compose，不会把 used+1。")
        self.view_text.configure(state="disabled")
        self.view_text.yview_moveto(0.0)

    def _view_action(self) -> None:
        if self.view_kind == "diary":
            self._write_diary()
        elif self.view_kind == "echo":
            self.echo_only = not self.echo_only
            self._rebuild_items()
            self._refresh_view()
        else:
            self._refresh_view()

    def _write_diary(self) -> None:
        if self.view_loading:
            return
        self.view_loading = True
        self.view_action.configure(state="disabled", text="她正在写…")
        self.view_status.configure(text="写日记可能要走一次模型，稍等")

        def work() -> None:
            err = ""
            try:
                from .diary import Diary
                with self._diary_lock:
                    Diary().ensure_today(force=True)
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
            self._post(lambda x=err: self._diary_written(x))

        threading.Thread(target=work, name="desktop-diary", daemon=True).start()

    def _diary_written(self, err: str) -> None:
        self.view_loading = False
        if err:
            self.view_status.configure(text="写日记失败：" + err)
            self.view_action.configure(state="normal", text="重试")
            return
        self.view_action.configure(state="normal", text="写今天的日记")
        self._refresh_view()

    # ---- 事件绑定 ---------------------------------------------------------
    def _bind_events(self) -> None:
        c = self.canvas
        c.bind("<ButtonPress-1>", self._on_press)
        c.bind("<B1-Motion>", self._on_drag)
        c.bind("<ButtonRelease-1>", self._on_release)
        c.bind("<Motion>", self._on_motion)
        c.bind("<Leave>", lambda _e: setattr(self, "hover", None))
        c.bind("<MouseWheel>", self._on_wheel)
        c.bind("<Configure>", self._on_canvas_configure)
        self.root.bind_all("<F5>", lambda _e: self._reload(self.demo))
        self.root.bind_all("<Escape>", self._hotkey_clear)
        self.root.bind_all("<space>", self._hotkey_clear)
        self.root.bind_all("<Home>", self._hotkey_reset)
        self.root.bind_all("<Key-0>", self._hotkey_reset)

    @staticmethod
    def _typing_target(event: tk.Event) -> bool:
        try:
            return event.widget.winfo_class() in ("Entry", "Text", "TEntry")
        except Exception:
            return False

    def _hotkey_clear(self, event: tk.Event) -> Optional[str]:
        if self._typing_target(event):
            return None
        self.clear_pick()
        return "break"

    def _hotkey_reset(self, event: tk.Event) -> Optional[str]:
        if self._typing_target(event):
            return None
        self.reset_view()
        return "break"

    def _post(self, fn: Any) -> None:
        """从工作线程把 UI 操作送回主线程；Tk 不允许子线程直接碰控件。"""
        if not self._closing:
            self._ui_queue.put(fn)

    def _drain_ui(self) -> None:
        while True:
            try:
                fn = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except Exception:
                import traceback
                traceback.print_exc()

    # ---- 载入 -------------------------------------------------------------
    def _load_async(self) -> None:
        self.loading.configure(text="正在打开她的识海…", fg=DIM)
        self.loading.place(relx=0.5, rely=0.47, anchor="center")
        self.empty.place_forget()

        def work() -> None:
            try:
                raw = graph_payload(self.demo)
                live = _read_live_state()
                data = prepare_graph(raw)
                data["live"] = live
            except Exception as e:
                self._post(lambda err=e: self._load_failed(err))
                return
            self._post(lambda d=data: self._apply_graph(d))

        threading.Thread(target=work, daemon=True).start()

    def _load_failed(self, err: Exception) -> None:
        if self._closing:
            return
        self.loading.configure(text=f"识海打不开：{type(err).__name__}: {err}",
                               fg="#d98a7a")
        self.loading.place(relx=0.5, rely=0.47, anchor="center")

    def _apply_graph(self, g: dict) -> None:
        if self._closing:
            return
        self.graph = g
        self.meta = g.get("meta") or {}
        live = g.get("live") or {}
        if live.get("state"):
            self.meta["state"] = live["state"]
        if live.get("learn"):
            self.meta["learn"] = live["learn"]
        if live.get("ambient"):
            self.meta["ambient"] = live["ambient"]
        if live.get("expression"):
            self.meta["expression"] = live["expression"]
        if live.get("memory"):
            self.meta["memory"] = live["memory"]
        if self.meta.get("n_episodes") is None:
            self.meta["n_episodes"] = int(
                (live.get("memory") or {}).get("episodes") or
                (self.meta.get("n") or 0))
        if self.meta.get("n_people") is None:
            self.meta["n_people"] = len({
                str(n.get("who")) for n in (g.get("nodes") or [])
                if n.get("who") and n.get("kind") not in ("self", "fact")
            })
        if self.meta.get("n_edges") is None:
            self.meta["n_edges"] = len(g.get("edges") or [])
        self.birth = bool(g.get("birth"))
        self.nodes = g.get("nodes") or []
        self.edges = g.get("edges") or []
        self.by_id = {str(n.get("id")): n for n in self.nodes}
        self.t_min = float(g.get("t_min") or 0)
        self.t_max = float(g.get("t_max") or 0)
        self.t_now = 1.0
        self.picked = None
        self.act.clear()
        self._loaded_at = _now()
        self.loading.place_forget()
        self.ui_card.place_forget()
        demo = g.get("source") == "demo"
        self.badge.configure(
            text="示例数据" if demo else "真实数据",
            bg="#2a1810" if demo else "#0d1f1a",
            fg="#ffb2a4" if demo else "#96e5bf",
            highlightbackground="#6a3d2a" if demo else "#245c45",
        )
        if hasattr(self, "status_dot"):
            self.status_dot.configure(fg=AMBER if demo else SEA)
        self._place_overlays()
        try:
            self.panel_scroll.canvas.yview_moveto(0.0)
        except Exception:
            pass
        self._rebuild_items()
        self._update_header()
        self._update_panel(self.meta.get("state"), self.meta.get("learn"),
                           None, self.meta.get("memory") or
                           {"episodes": self.meta.get("n_episodes", len(self.nodes))})
        if self.meta.get("source") == "demo":
            n = self.meta.get("n_episodes", len(self.nodes))
            self.pv["sihai"].configure(text=f"记忆 {n} 段 · 知识 0 条")
            self.pv["echo"].configure(text=self._ambient_text(
                self.meta.get("ambient") or {}))
            self.pv["expr"].configure(text="示例数据 · 不接实时表达")
        self.root.after(80, lambda: self._update_panel(
            self.meta.get("state"), self.meta.get("learn"), None,
            self.meta.get("memory") or {"episodes": self.meta.get("n_episodes", len(self.nodes))}))
        if self.birth:
            self.hint.configure(text="拖拽转视角 · 滚轮远近 · 双击回到她身边")
            self.chat_status.configure(text="她还在睡 · 说一句才会醒")
        else:
            self.hint.configure(
                text="拖拽转视角 · 滚轮远近 · 点一颗星，顺着线亮起来 · 双击回到正位")
            self.chat_status.configure(text="文字与语音 · 说一句才叫得醒她")
            self._build_legend()
            if self.nodes:
                self.empty.place_forget()
            else:
                self.empty.configure(text="库里还没有记忆\n切到「示例数据」看效果，或先跟她说几句话。")
                self.empty.place(relx=0.5, rely=0.47, anchor="center")
        if self._clock_job:
            try:
                self.root.after_cancel(self._clock_job)
            except Exception:
                pass
        self._clock_job = self.root.after(15000, self._clock_tick)
        if self._open_view_on_load:
            self.root.after(240, lambda: self._open_view(self._open_view_on_load))
        if self.screenshot:
            self.root.after(600, self._capture)

    def _reload(self, demo: bool) -> None:
        if self._busy:
            return
        self.demo = bool(demo)
        self.graph = None
        self.picked = None
        self.chatlog.clear()
        self.canvas.delete("all")
        self._load_async()

    @staticmethod
    def _sihai_alive() -> bool:
        try:
            from urllib.request import urlopen
            with urlopen(SIHAI_WEB, timeout=1.0) as resp:
                return 200 <= int(resp.status) < 500
        except Exception:
            return False

    def _open_sihai_web(self) -> None:
        """电脑端的识海入口：统一打开 8767 的 sihai.html。

        8767 没跑时先拉起同一个服务，再开页面；这样重启电脑后
        点「识海网页」也不会只打开一个死链接。
        """
        if not self._sihai_alive():
            try:
                root = Path(__file__).resolve().parents[1]
                subprocess.Popen(
                    [sys.executable, "-m", "app.sihai", "--serve",
                     "--port", "8767"],
                    cwd=str(root),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                for _ in range(30):
                    time.sleep(0.1)
                    if self._sihai_alive():
                        break
            except Exception:
                pass
        try:
            webbrowser.open(SIHAI_WEB)
        except Exception:
            try:
                import os
                os.startfile(SIHAI_WEB)  # noqa: S606 - Windows 默认浏览器
            except Exception:
                pass

    # ---- 坐标与绘制缓存 ---------------------------------------------------
    def _on_canvas_configure(self, event: tk.Event) -> None:
        if event.width == self.W and event.height == self.H:
            return
        self.W = int(event.width)
        self.H = int(event.height)
        if self._resize_job:
            try:
                self.root.after_cancel(self._resize_job)
            except Exception:
                pass
        self._resize_job = self.root.after(120, self._rebuild_items)

    def _on_root_configure(self, event: tk.Event) -> None:
        if event.widget is self.root:
            self._place_overlays()

    def _rebuild_items(self) -> None:
        self._resize_job = None
        if self._closing or not self.graph:
            return
        if not self.W or not self.H:
            self.W = max(1, self.canvas.winfo_width())
            self.H = max(1, self.canvas.winfo_height())
        if self.W < 80 or self.H < 80:
            return
        self.canvas.delete("all")
        self._node_items.clear()
        self._label_items.clear()
        self._edge_items.clear()
        self._twinkle_items.clear()
        self._dust_items.clear()
        self._layer_items.clear()
        self._selection_items.clear()
        self._birth_items.clear()
        self._core_item = None
        self._order_key = None
        self._edge_drawn.clear()
        self._label_visible.clear()
        self._draw_static_background()
        if self.birth:
            self._build_birth_items()
        else:
            self._build_memory_items()
        self._place_overlays()
        self._mark_dirty(0.15)

    def _draw_static_background(self) -> None:
        img = self._make_background(self.W, self.H)
        photo = ImageTk.PhotoImage(img)
        self._photos["bg"] = photo
        self.canvas.create_image(0, 0, image=photo, anchor="nw")
        core = self._glow.photo("#5f7cff", min(1100, int(min(self.W, self.H) * 1.04)))
        self._photos["core"] = core
        self._core_item = self.canvas.create_image(
            self.W // 2, int(self.H * 0.48), image=core, anchor="center")

    def _make_background(self, w: int, h: int) -> Image.Image:
        """和网页的星云底同风格：深蓝底、斜银河、星点、暗角、细噪点。"""
        w = max(2, int(w)); h = max(2, int(h))
        yy, xx = np.mgrid[0:h, 0:w]
        nx = (xx - w * 0.5) / max(w, h)
        ny = (yy - h * 0.46) / max(w, h)
        rr = np.sqrt(nx * nx + ny * ny)
        # 底色：8767 的 #03050b 深海，中心略亮、边缘近黑。
        t = np.clip(rr / 0.85, 0, 1)[..., None]
        c0 = np.array([7, 11, 20], dtype=np.float32)
        c1 = np.array([2, 4, 8], dtype=np.float32)
        rgb = c0 * (1 - t) + c1 * t
        # 星云：上面一点琥珀，下面一片冷蓝 / 青，加法混合
        for dx, dy, radius, color, strength in [
            (0.00, -0.08, 0.42, (120, 160, 255), 0.052),
            (0.05, 0.10, 0.32, (56, 128, 220), 0.044),
            (-0.02, 0.02, 0.18, (110, 220, 255), 0.030),
            (0.18, -0.06, 0.22, (80, 140, 220), 0.026),
        ]:
            cx = w * (0.5 + dx); cy = h * (0.46 + dy)
            d2 = ((xx - cx) ** 2 + (yy - cy) ** 2) / max(1.0, (max(w, h) * radius) ** 2)
            field = np.exp(-d2 * 2.2)[..., None] * strength
            rgb = rgb + np.array(color, dtype=np.float32) * field
        # 斜银河盘
        ang = -0.40
        ca, sa = math.cos(ang), math.sin(ang)
        px = (xx - w * 0.5) * ca - (yy - h * 0.48) * sa
        py = (xx - w * 0.5) * sa + (yy - h * 0.48) * ca
        band = (np.exp(-(py / (max(w, h) * 0.19)) ** 2)
                * np.exp(-(px / (max(w, h) * 0.72)) ** 2))[..., None]
        rgb = rgb + np.array([70, 108, 180], dtype=np.float32) * band * 0.22
        vig = np.clip(1.12 - 0.62 * (rr / 0.9) ** 1.8, 0, 1)[..., None]
        rgb = rgb * vig
        arr = np.clip(rgb, 0, 255).astype(np.uint8)
        img = Image.fromarray(arr, "RGB")
        # 星点与噪声，用 PIL 画更锐利
        d = ImageDraw.Draw(img, "RGBA")
        rng = np.random.default_rng(20261031)
        n = int(clamp(w * h / 6200, 200, 520))
        for _ in range(n):
            x = float(rng.uniform(0, w)); y = float(rng.uniform(0, h))
            r = float(rng.uniform(0.4, 1.4))
            a = int(clamp(rng.uniform(13, 85), 0, 255))
            d.ellipse((x, y, x + r, y + r), fill=(190, 215, 255, a))
        # 细噪点，给暗部一点颗粒
        noise = rng.integers(0, 8, size=(h, w), dtype=np.uint8)
        arr = np.array(img).astype(np.int16) + noise[..., None]
        img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), "RGB")
        return img.filter(ImageFilter.GaussianBlur(0.25))

    # ---- 出生期：一颗星，但星海是立体的 -----------------------------------
    def _build_birth_items(self) -> None:
        self._birth_items["dust"] = []
        self._birth_items["rings"] = []
        cx, cy = self.W // 2, int(self.H * 0.48)
        # 四层壳：半径、倾斜、起始角、颜色。以前这里画的是屏幕坐标里的扁椭圆，
        # 所以拖拽只改了相机，画面不动；现在环是 3D 世界坐标，每帧重新投影。
        for r, tilt, phase, color in [
            (0.34, 0.20, 0.00, "#8fc7b9"),
            (0.54, 0.52, 1.10, "#76b3a6"),
            (0.74, 0.90, 2.00, "#5c968b"),
            (0.94, 0.34, 3.20, "#477d75"),
        ]:
            item = self.canvas.create_line(0, 0, 1, 1, fill=color,
                                           width=1, smooth=True)
            self._birth_items["rings"].append({
                "item": item,
                "world": self._ring_world(r, tilt, phase),
                "color": color,
                "r": r,
            })
        # 球壳里的尘：每一粒都有 (x, y, z)，不再只是屏幕上的一个点。
        for i in range(150):
            u = rnd01(f"b{i}", "u")
            rr = 0.22 + 0.78 * (u ** 0.55)
            th = math.acos(clamp(rnd01(f"b{i}", "h") * 2.0 - 1.0, -1.0, 1.0))
            ph = rnd01(f"b{i}", "p") * math.pi * 2
            x = rr * math.sin(th) * math.cos(ph)
            y = rr * math.cos(th) * 0.86
            z = rr * math.sin(th) * math.sin(ph)
            dot = self.canvas.create_oval(0, 0, 1, 1, fill="#3f665f", outline="")
            self._birth_items["dust"].append({
                "item": dot, "x": x, "y": y, "z": z,
                "sp": (0.018 + rnd01(f"b{i}", "s") * 0.035) *
                      (1 if i % 2 else -1),
                "sz": 0.45 + rnd01(f"b{i}", "z") * 1.25,
                "al": 0.10 + rnd01(f"b{i}", "l") * 0.34,
                "col": "#a9d8cf" if i % 5 else "#e0f2ec",
            })
        self._birth_items["halo"] = self.canvas.create_image(
            cx, cy, image=self._glow.photo("#8fd6c6", 300), anchor="center")
        self._birth_items["hot"] = self.canvas.create_image(
            cx, cy, image=self._glow.photo(AMBER, 150), anchor="center")
        self._birth_items["rim"] = self.canvas.create_oval(
            cx - 13, cy - 13, cx + 13, cy + 13,
            outline=SEA, width=1)
        self._birth_items["core"] = self.canvas.create_oval(
            cx - 6, cy - 6, cx + 6, cy + 6, fill="#eaf5f1", outline="")
        self._birth_items["label"] = self.canvas.create_text(
            cx, cy - 27, text="我", fill="#e9f1ee", font=self.f_hot)
        self._birth_items["label_shadow"] = self.canvas.create_text(
            cx + 1, cy - 26, text="我", fill=BG_2, font=self.f_hot)
        self.canvas.tag_raise(self._birth_items["label"])
        self._birth_items["tip"] = self.canvas.create_text(
            cx, cy + 36, text="拖动转视角 · 滚轮远近", fill=DIM,
            font=self.f_mono)
        self._birth_items["pulse"] = self.canvas.create_oval(
            cx - 20, cy - 20, cx + 20, cy + 20, outline=SEA, width=1)
        self.canvas.itemconfigure(self._birth_items["pulse"], state="hidden")
        self._selection_items = [self._birth_items["pulse"]]

    def _ring_world(self, r: float, tilt: float = 0.0, phase: float = 0.0,
                    n: int = 72) -> list[tuple[float, float, float]]:
        """一圈 3D 环：先在 XZ 平面成圆，再绕 X 轴倾斜。"""
        ct, st = math.cos(tilt), math.sin(tilt)
        out: list[tuple[float, float, float]] = []
        for i in range(n + 1):
            a = phase + i / n * math.pi * 2
            x = math.cos(a) * r
            z = math.sin(a) * r
            out.append((x, -z * st, z * ct))
        return out

    # ---- 有记忆：整片星海 -------------------------------------------------
    def _build_memory_items(self) -> None:
        self._birth_items.clear()
        # 壳层环
        self._birth_items["rings"] = []
        for c in self.graph.get("circles") or []:
            item = self.canvas.create_line(0, 0, 1, 1, fill="#1b3333",
                                           width=1, smooth=True)
            self._birth_items["rings"].append({"item": item, "c": c})
        # 尘埃与跳动星
        self._build_twinkles()
        self._build_dust()
        # 边
        for e in self.edges:
            item = self.canvas.create_line(0, 0, 1, 1, fill="#2b4a48", width=1,
                                           state="hidden", tags=("edge",))
            self._edge_items.append((item, e))
        # 1047 条边全画会压住星图，也会把每帧拖到 20ms 以上。按权重排序，
        # 每帧只画前 EDGE_BUDGET 条；选中一颗星时它的关联边优先补进来。
        self._edge_items.sort(
            key=lambda pair: -float(pair[1].get("w") or 0.0))
        self._edge_drawn.clear()
        # 节点
        for n in self.nodes:
            gid = str(n.get("id"))
            glow = self.canvas.create_image(-100, -100, image=self._glow.photo(
                node_color(n), 32), anchor="center", tags=("nodeglow", gid))
            ring = self.canvas.create_oval(0, 0, 0, 0, outline="#9aa7b8",
                                           width=1, state="hidden",
                                           tags=("nodering", gid))
            core = self.canvas.create_oval(0, 0, 0, 0, outline="",
                                           fill=node_color(n),
                                           tags=("nodecore", gid))
            inner = self.canvas.create_oval(0, 0, 0, 0, outline="",
                                            fill="#ffffff", state="hidden",
                                            tags=("nodeinner", gid))
            lbg = self.canvas.create_rectangle(0, 0, 0, 0, fill=BG_2,
                                               outline="", state="hidden",
                                               tags=("nodelabel", gid))
            ltxt = self.canvas.create_text(0, 0, text="", fill="#c3ccdb",
                                           font=self.f_small, anchor="center",
                                           state="hidden", tags=("nodelabel", gid))
            self._node_items[gid] = {
                "glow": glow, "ring": ring, "core": core, "inner": inner,
                "lbg": lbg, "ltxt": ltxt, "n": n,
            }
        # 选中环
        for i in range(2):
            self._selection_items.append(
                self.canvas.create_oval(0, 0, 0, 0, outline=ACCENT, width=1,
                                        state="hidden", tags=("selection",)))
        self._build_layer_label_items()

    def _build_twinkles(self) -> None:
        rng = np.random.default_rng(77)
        count = int(clamp(self.W * self.H / 16000, 40, 110))
        for i in range(count):
            item = self.canvas.create_oval(0, 0, 1, 1, fill="#9fc9c0", outline="")
            self._twinkle_items.append((item, {
                "x": float(rng.uniform(0, self.W)),
                "y": float(rng.uniform(0, self.H)),
                "r": float(rng.uniform(0.6, 1.9)),
                "a": float(rng.uniform(0.10, 0.48)),
                "ph": float(rng.uniform(0, 6.283)),
                "sp": float(rng.uniform(0.35, 1.45)),
                "layer": 0.30 if i % 3 == 0 else (0.60 if i % 3 == 1 else 1.0),
            }))

    def _build_dust(self) -> None:
        for i in range(150):
            item = self.canvas.create_oval(0, 0, 1, 1, fill="#3f665f", outline="")
            self._dust_items.append((item, {
                "r": 0.34 + rnd01("dust" + str(i), "r") ** 0.62 * 1.15,
                "a0": rnd01("dust" + str(i), "a") * math.pi * 2,
                "y": (rnd01("dust" + str(i), "y") * 2 - 1) * 0.15,
                "sp": (0.008 + rnd01("dust" + str(i), "s") * 0.020) * (
                    1 if i % 2 else -1),
                "sz": 0.55 + rnd01("dust" + str(i), "z") * 1.5,
                "al": 0.09 + rnd01("dust" + str(i), "l") * 0.26,
            }))

    def _build_layer_label_items(self) -> None:
        for i in range(4):
            bg = self.canvas.create_rectangle(0, 0, 0, 0, fill="#03060c",
                                              outline="", state="hidden",
                                              tags=("layerlabel",))
            line = self.canvas.create_line(0, 0, 0, 0, fill="#1e3452",
                                           state="hidden", tags=("layerlabel",))
            txt = self.canvas.create_text(0, 0, text="", fill="#889cbc",
                                          font=self.f_tiny, anchor="w",
                                          state="hidden", tags=("layerlabel",))
            self._layer_items.append({"bg": bg, "line": line, "txt": txt, "i": i})

    # ---- 每帧 -------------------------------------------------------------
    def _mark_dirty(self, seconds: float = 0.0) -> None:
        """标记画面需要重画；seconds 是这次交互的动画尾长。"""
        self._render_dirty = True
        if seconds:
            self._anim_until = max(self._anim_until, _now() + seconds)

    def _wants_frame(self, now: float) -> bool:
        # 静止时一帧都不画。Tk 主线程空出来，滚动、输入和窗口拖动才不会排队。
        if not self.graph:
            return False
        if now - self._last_render < FRAME_MIN:
            return False
        return self._render_dirty or now < self._anim_until

    def _tick(self) -> None:
        if self._closing:
            return
        try:
            now = _now()
            dt = 0.016 if not self._last_t else min(0.05, now - self._last_t)
            self._last_t = now
            self._drain_ui()
            self._step_cam(dt, now)
            if self._wants_frame(now):
                self._render(now)
                self._render_dirty = False
                self._last_render = now
            self._tick_job = self.root.after(10, self._tick)
        except Exception:
            self._closing = True
            raise

    def _step_cam(self, dt: float, now: float) -> None:
        c = self.cam
        if c["tween"]:
            tw = c["tween"]
            k = clamp((now - tw["t0"]) / tw["ms"], 0, 1)
            e = 1 - (1 - k) ** 3
            c["yaw"] = tw["yaw0"] + (tw["yaw1"] - tw["yaw0"]) * e
            c["pitch"] = tw["pitch0"] + (tw["pitch1"] - tw["pitch0"]) * e
            c["zoom"] = tw["zoom0"] + (tw["zoom1"] - tw["zoom0"]) * e
            c["tzoom"] = tw["zoom1"]
            if k >= 1:
                c["tween"] = None
            self._anim_until = max(self._anim_until, now + 0.05)
            self._paint_zoom()
            return
        if not self.dragging and (c["vyaw"] or c["vpitch"]):
            c["yaw"] += c["vyaw"] * dt
            c["pitch"] += c["vpitch"] * dt
            damp = math.pow(0.0025, dt)
            c["vyaw"] *= damp
            c["vpitch"] *= damp
            if abs(c["vyaw"]) < 0.003:
                c["vyaw"] = 0.0
            if abs(c["vpitch"]) < 0.003:
                c["vpitch"] = 0.0
            self._anim_until = max(self._anim_until, now + 0.10)
        c["pitch"] = clamp(c["pitch"], -1.32, 1.32)
        if abs(c["zoom"] - c["tzoom"]) > 0.0005:
            c["zoom"] += (c["tzoom"] - c["zoom"]) * min(1.0, dt * 9)
            self._anim_until = max(self._anim_until, now + 0.08)
        else:
            c["zoom"] = c["tzoom"]
        self._paint_zoom()

    def _paint_zoom(self) -> None:
        z = self.cam["zoom"]
        txt = "" if abs(z - 1) < 0.02 else ("×" + (f"{z:.2f}" if z < 1 else f"{z:.1f}"))
        if self.zoom.cget("text") != txt:
            self.zoom.configure(text=txt)

    def _prep_cam(self) -> tuple[float, float, float, float, float, float, float]:
        cy = math.cos(self.cam["yaw"]); sy = math.sin(self.cam["yaw"])
        cp = math.cos(self.cam["pitch"]); sp = math.sin(self.cam["pitch"])
        zq = math.sqrt(max(0.04, self.cam["zoom"]))
        dist = 2.1 / zq
        focal = 0.9 * min(self.W, self.H) * zq
        rmax = min(self.W, self.H) * 0.064
        return cy, sy, cp, sp, dist, focal, rmax

    def _project(self, x: float, y: float, z: float,
                 cam: tuple[float, ...]) -> tuple[float, float, float, float]:
        cy, sy, cp, sp, dist, focal, _rmax = cam
        rx = x * cy - z * sy
        rz0 = x * sy + z * cy
        ry = y * cp - rz0 * sp
        rz = y * sp + rz0 * cp
        zc = dist - rz
        if zc < 0.12:
            return 0.0, 0.0, zc, 0.0
        s = focal / zc
        return self.W * 0.5 + rx * s, self.H * 0.5 + ry * s, zc, s

    def _set_fast_mode(self, fast: bool) -> None:
        """拖动时只画星点，不画标签/连线/尘埃。

        拖动是最需要跟手的时刻；把这些装饰层暂时收起来，松手后一次补全，
        比每帧重画 1000 条边更像“流畅”。
        """
        if fast == self._fast_mode:
            return
        self._fast_mode = fast
        if fast:
            for gid in list(self._label_visible):
                rec = self._node_items.get(gid)
                if rec:
                    self.canvas.itemconfigure(rec["lbg"], state="hidden")
                    self.canvas.itemconfigure(rec["ltxt"], state="hidden")
            self._label_visible.clear()
            for item in list(self._edge_drawn):
                self.canvas.itemconfigure(item, state="hidden")
            self._edge_drawn.clear()
        else:
            self._mark_dirty(0.10)

    def _render(self, now: float) -> None:
        if self.birth:
            self._render_birth(now)
            return
        fast = bool(self.dragging and self.drag_data and
                    self.drag_data.get("moved", 0.0) >= 4.0)
        if fast != self._fast_mode:
            self._set_fast_mode(fast)
        cam = self._prep_cam()
        W, H = self.W, self.H
        if not fast:
            # 星点
            t = now
            ox = -self.cam["yaw"] * 66 + math.sin(t * 0.05) * 10
            oy = -self.cam["pitch"] * 52 + math.cos(t * 0.043) * 8
            for item, s in self._twinkle_items:
                x = (s["x"] + ox * s["layer"]) % W
                y = (s["y"] + oy * s["layer"]) % H
                r = s["r"]
                self.canvas.coords(item, x, y, x + r, y + r)
                a = s["a"] * (0.55 + 0.45 * math.sin(t * s["sp"] + s["ph"]))
                self.canvas.itemconfigure(item, fill=hex_mix(BG, "#cfe0ff", clamp(a * 2.2, 0.05, 0.85)))
        # 壳环和尘
        for rec in self._birth_items.get("rings", []):
            c = rec["c"]
            xy: list[float] = []
            for (x, y, z) in c["world"]:
                px, py, _pz, ps = self._project(x, y, z, cam)
                if not ps:
                    xy = []
                    break
                xy.extend((px, py))
            if len(xy) >= 4:
                self.canvas.coords(rec["item"], *xy)
                self.canvas.itemconfigure(
                    rec["item"], fill="#1b2d4a" if c["role"] == "ring" else "#243b5c")
        if not fast:
            self._render_dust(now, cam)
        self._render_nodes(now, cam)
        self._render_selection(now, cam)
        if not fast:
            self._render_labels(now, cam)
            self._render_layer_labels(cam)
            self._render_edges(now, cam)
            # 节点与边的前后关系：边在星下面，星在标签下面。
            # 节点在 _render_nodes 里按深度逐个 raise；标签、选中环和层名最后压上。
            self.canvas.tag_raise("nodelabel")
            self.canvas.tag_raise("selection")
            self.canvas.tag_raise("layerlabel")
        # 时间轴没动，但出生期计时需要刷新
        self._tick_n += 1

    def _render_dust(self, now: float, cam: tuple[float, ...]) -> None:
        for item, d in self._dust_items:
            ang = d["a0"] + now * d["sp"]
            px, py, pz, ps = self._project(
                math.cos(ang) * d["r"], d["y"], math.sin(ang) * d["r"], cam)
            if not ps:
                self.canvas.itemconfigure(item, state="hidden")
                continue
            s = d["sz"] * ps * 0.010
            if s < 0.35:
                self.canvas.itemconfigure(item, state="hidden")
                continue
            self.canvas.itemconfigure(item, state="normal")
            self.canvas.coords(item, px, py, px + s, py + s)
            self.canvas.itemconfigure(
                item, fill=hex_mix(BG, "#a9c6ff", clamp(d["al"] * 1.8, 0.06, 0.7)))

    def _render_nodes(self, now: float, cam: tuple[float, ...]) -> None:
        _cy, _sy, _cp, _sp, dist, _focal, rmax = cam
        order = []
        for n in self.nodes:
            gid = str(n.get("id"))
            rec = self._node_items.get(gid)
            if not rec:
                continue
            px, py, pz, ps = self._project(n["px3"], n["py3"], n["pz3"], cam)
            if (not ps or not self._shown(n)
                    or px < -80 or px > self.W + 80
                    or py < -80 or py > self.H + 80):
                if not rec.get("_culled"):
                    for key in ("glow", "ring", "core", "inner"):
                        self.canvas.itemconfigure(rec[key], state="hidden")
                    rec["_culled"] = True
                n["_screen"] = (0.0, 0.0, 0.0, 0.0, 0.0)
                continue
            was_culled = bool(rec.pop("_culled", False))
            r = min(n.get("r_world", 0.003) * ps, rmax)
            n["_screen"] = (px, py, pz, ps, r)
            act = self._act_alpha(n, now)
            df = clamp(dist / pz, 0.42, 1.55) if pz else 1.0
            gr = max(8.0, r * (3.2 + 1.8 * (1.0 - min(1.0, df))) * (1.0 + 0.9 * act))
            color = node_color(n)
            if act > 0.01:
                color = hex_mix(color, "#ffffff", clamp(act * 0.55, 0, 0.7))
            photo = self._glow.photo(color, int(gr * 2))
            if was_culled or rec.get("_glow_photo") is not photo:
                self.canvas.itemconfigure(rec["glow"], state="normal", image=photo)
                rec["_glow_photo"] = photo
            self.canvas.coords(rec["glow"], px, py)
            core = max(1.0, r)
            echo = bool(n.get("is_echo"))
            ambient = bool(n.get("is_ambient"))
            if self.echo_only and not (echo or ambient):
                for key in ("glow", "ring", "core", "inner", "lbg", "ltxt"):
                    self.canvas.itemconfigure(rec[key], state="hidden")
                rec["_culled"] = True
                n["_screen"] = (0.0, 0.0, 0.0, 0.0, 0.0)
                continue
            hollow = bool(n.get("scope") == "inferred")
            hot = n is self.picked or n is self.hover
            if echo or hollow:
                core_fill, core_outline, core_width = "#101a2b", color, 1.4
            else:
                core_fill, core_outline, core_width = color, "", 0
            if hot:
                core_outline, core_width = "#eaf4ff", 1.2
            core_style = (core_fill, core_outline, core_width)
            if was_culled or rec.get("_core_style") != core_style:
                self.canvas.itemconfigure(
                    rec["core"], state="normal", fill=core_fill,
                    outline=core_outline, width=core_width)
                rec["_core_style"] = core_style
            self.canvas.coords(rec["core"], px - core, py - core,
                               px + core, py + core)
            # 普通星只留一圈本星色；纯白芯会把「颜色 = 情绪」吃掉。
            # 悬停/选中的那颗才给白色高光，提醒你正在看它。
            inner_normal = bool(core > 3 and hot)
            if core > 3 and hot:
                inner_style = ("normal", "#f4f8ff")
            else:
                inner_style = ("hidden", "")
            if was_culled or rec.get("_inner_style") != inner_style:
                self.canvas.itemconfigure(rec["inner"], state=inner_style[0],
                                          fill=inner_style[1])
                rec["_inner_style"] = inner_style
            if inner_normal:
                self.canvas.coords(rec["inner"], px - core * 0.22, py - core * 0.22,
                                   px + core * 0.22, py + core * 0.22)
            if echo or ambient or float(n.get("ret") or 0) < 0.25:
                rr = max(4.0, core * (2.15 if ambient and not echo else 1.9))
                ring_style = ("normal",
                              "#ffb35c" if ambient and not echo else "#9aa7b8", rr)
                if was_culled or rec.get("_ring_style") != ring_style:
                    self.canvas.itemconfigure(
                        rec["ring"], state="normal",
                        outline=ring_style[1])
                    rec["_ring_style"] = ring_style
                self.canvas.coords(rec["ring"], px - rr, py - rr, px + rr, py + rr)
            elif rec.get("_ring_style") != ("hidden", "", 0):
                self.canvas.itemconfigure(rec["ring"], state="hidden")
                rec["_ring_style"] = ("hidden", "", 0)
            order.append((pz, gid))
        order.sort(reverse=True)
        order_key = tuple(gid for _pz, gid in order)
        if order_key != self._order_key:
            for _pz, gid in order:
                rec = self._node_items[gid]
                self.canvas.tag_raise(rec["glow"])
                self.canvas.tag_raise(rec["core"], rec["glow"])
                self.canvas.tag_raise(rec["ring"], rec["core"])
                self.canvas.tag_raise(rec["inner"], rec["core"])
            self._order_key = order_key
        else:
            # 静止/微动时深度顺序没变，不要每帧把 900 多个 item 再 raise 一遍。
            pass
        self._visible_order = [self.by_id.get(g) for _pz, g in order if self.by_id.get(g)]

    def _ui_occluders(self) -> list[tuple[float, float, float, float]]:
        """悬在星图上的那些面板占掉的屏幕矩形，标签不该钻到它们下面。"""
        out: list[tuple[float, float, float, float]] = []
        for w in (getattr(self, "ui_top", None), getattr(self, "ui_panel", None),
                  getattr(self, "ui_chat", None), getattr(self, "ui_card", None),
                  getattr(self, "ui_footer", None), getattr(self, "ui_views", None),
                  getattr(self, "ui_insight", None)):
            if w is None:
                continue
            try:
                if not w.winfo_ismapped():
                    continue
                x = float(w.winfo_x()); y = float(w.winfo_y())
                ww = float(w.winfo_width()); hh = float(w.winfo_height())
                if ww > 2 and hh > 2:
                    out.append((x - 8, y - 4, x + ww + 8, y + hh + 8))
            except Exception:
                continue
        return out

    def _render_labels(self, now: float, cam: tuple[float, ...]) -> None:
        # 默认只标重点：整片海每颗星都挂一行，字会互相压成一张网。
        # 勾「全部标签」回到旧行为；悬停/点选/高重要度的星始终有标签。
        show_all = bool(self.label_all.get()) if hasattr(self, "label_all") else False
        limit = 48 if show_all else 16
        occluders = self._ui_occluders()
        cands: list[tuple[float, dict, dict, float, float, float, bool]] = []
        for n in self.nodes:
            gid = str(n.get("id"))
            rec = self._node_items.get(gid)
            if not rec:
                continue
            sx, sy, _sz, _ps, r = n.get("_screen", (0, 0, 0, 0, 0))
            act = self._act_alpha(n, now)
            hot = n is self.hover or n is self.picked or act > 0.05
            if not r or not self._shown(n):
                continue
            if self.echo_only and not (n.get("is_echo") or n.get("is_ambient")):
                continue
            if not (hot or r >= 2.6 or float(n.get("imp") or 0) >= 0.8
                    or int(n.get("deg") or 0) >= 8):
                continue
            score = (10.0 if hot else 0.0) + float(n.get("imp") or 0)
            score += min(6, int(n.get("used") or 0)) / 6.0 * 0.45
            score += min(12, int(n.get("deg") or 0)) / 12.0 * 0.25
            score += 0.30 if r >= 4.0 else 0.0
            cands.append((score, n, rec, sx, sy, r, hot))
        cands.sort(key=lambda x: -x[0])
        shown = 0
        placed: list[tuple[float, float, float, float]] = []
        visible: set[str] = set()
        for _score, n, rec, sx, sy, r, hot in cands:
            if shown >= limit:
                continue
            text = str(n.get("label") or "")[:14]
            if not text:
                continue
            f = self.f_hot if hot else self.f_small
            tw = f.measure(text)
            ly = sy - max(9.0, r * 1.25 + 10.0)
            bx, by = sx - tw / 2 - 3, ly - 8
            bw, bh = tw + 6, 15
            if any(not (bx - 5 > px + pw + 3 or bx + bw + 3 < px - 5
                        or by - 3 > py + ph + 2 or by + bh + 2 < py - 3)
                   for px, py, pw, ph in placed):
                continue
            if any(not (bx > ox2 + 2 or bx + bw < ox1 - 2
                        or by > oy2 + 2 or by + bh < oy1 - 2)
                   for ox1, oy1, ox2, oy2 in occluders):
                continue
            placed.append((bx, by, bw, bh))
            self.canvas.itemconfigure(rec["lbg"], state="normal")
            self.canvas.coords(rec["lbg"], bx, by, bx + bw, by + bh)
            self.canvas.itemconfigure(rec["ltxt"], state="normal", text=text,
                                      fill="#edf4ff" if hot else "#b7c7dc",
                                      font=f)
            self.canvas.coords(rec["ltxt"], sx, ly)
            visible.add(str(n.get("id")))
            shown += 1
        # 只隐藏上一帧真实显示过、这一帧被挤掉的标签；不要每帧遍历全部节点。
        for gid in self._label_visible - visible:
            rec = self._node_items.get(gid)
            if rec:
                self.canvas.itemconfigure(rec["lbg"], state="hidden")
                self.canvas.itemconfigure(rec["ltxt"], state="hidden")
        self._label_visible = visible

    def _render_selection(self, now: float, cam: tuple[float, ...]) -> None:
        if not self.picked:
            for item in self._selection_items:
                self.canvas.itemconfigure(item, state="hidden")
            return
        n = self.picked
        sx, sy, _sz, _ps, r = n.get("_screen", (0, 0, 0, 0, 0))
        if not r:
            return
        pulse = 1.0 + 0.14 * math.sin(now * 2.4)
        geom = [(max(10.0, r * 2.6) * pulse, "#8ee6ff"),
                (max(16.0, r * 4.2) * pulse, "#365f95")]
        for item, (rr, color) in zip(self._selection_items, geom):
            self.canvas.itemconfigure(item, state="normal", outline=color, width=1)
            self.canvas.coords(item, sx - rr, sy - rr, sx + rr, sy + rr)

    def _render_layer_labels(self, cam: tuple[float, ...]) -> None:
        counts = [0, 0, 0, 0]
        for n in self.nodes:
            if self._shown(n):
                counts[int(n.get("pl") or 0)] += 1
        anchors = [c for c in (self.graph.get("circles") or []) if c.get("role") == "anchor"]
        for i, rec in enumerate(self._layer_items):
            if i >= len(anchors):
                for k in ("bg", "line", "txt"):
                    self.canvas.itemconfigure(rec[k], state="hidden")
                continue
            pts = []
            for (x, y, z) in anchors[i]["world"]:
                px, py, _pz, ps = self._project(x, y, z, cam)
                if ps:
                    pts.append((px, py))
            if not pts:
                for k in ("bg", "line", "txt"):
                    self.canvas.itemconfigure(rec[k], state="hidden")
                continue
            max_x = max(p[0] for p in pts)
            min_y = min(p[1] for p in pts)
            max_y = max(p[1] for p in pts)
            y = (min_y + max_y) / 2
            txt = layer_name(self.meta, i) + (f" {counts[i]}" if counts[i] else "")
            tw = self.f_tiny.measure(txt)
            x = min(self.W - tw - 26, max_x + 16)
            if y < 62 or y > self.H - 92 or x < 230:
                for k in ("bg", "line", "txt"):
                    self.canvas.itemconfigure(rec[k], state="hidden")
                continue
            for k in ("bg", "line", "txt"):
                self.canvas.itemconfigure(rec[k], state="normal")
            self.canvas.coords(rec["bg"], x - 5, y - 8, x + tw + 5, y + 8)
            self.canvas.coords(rec["line"], x - 16, y, x - 5, y)
            self.canvas.itemconfigure(rec["txt"], text=txt,
                                      fill=hex_mix(BG, "#88a0c8", 0.45 - i * 0.04))
            self.canvas.coords(rec["txt"], x, y)

    def _render_edges(self, now: float, cam: tuple[float, ...]) -> None:
        _cy, _sy, _cp, _sp, dist, _focal, _rmax = cam
        picked_id = str(self.picked.get("id")) if self.picked else ""
        active: set[int] = set()
        for item, e in self._edge_items:
            # 选中一颗星时，连到它的边不论权重都补画，否则只画前 EDGE_BUDGET 条。
            incident = bool(picked_id and (
                str(e.get("a")) == picked_id or str(e.get("b")) == picked_id))
            if len(active) >= EDGE_BUDGET and not incident:
                continue
            A, B = e["A"], e["B"]
            if not self._shown(A) or not self._shown(B):
                continue
            a = A.get("_screen", (0, 0, 0, 0, 0))
            b = B.get("_screen", (0, 0, 0, 0, 0))
            if not a[3] or not b[3]:
                continue
            if self.echo_only and not any(
                    x.get("is_echo") or x.get("is_ambient") for x in (A, B)):
                continue
            pts: list[float] = []
            ax, ay, az = A["px3"], A["py3"], A["pz3"]
            bx, by, bz = B["px3"], B["py3"], B["pz3"]
            cx = (ax + bx) * 0.32
            cy3 = (ay + by) * 0.32
            cz = (az + bz) * 0.32
            for j in range(9):
                u = j / 8.0
                v = 1 - u
                x = v * v * ax + 2 * v * u * cx + u * u * bx
                y = v * v * ay + 2 * v * u * cy3 + u * u * by
                z = v * v * az + 2 * v * u * cz + u * u * bz
                px, py, _pz, ps = self._project(x, y, z, cam)
                if not ps:
                    pts = []
                    break
                pts.extend((px, py))
            if len(pts) < 4:
                continue
            act = max(self._act_alpha(A, now), self._act_alpha(B, now))
            zavg = (a[2] + b[2]) * 0.5
            df = clamp(dist / zavg, 0.45, 1.5) if zavg else 1.0
            link = self.picked is A or self.picked is B
            if self.picked and not link:
                alpha = 0.10
            elif link:
                alpha = clamp(0.55 + 0.35 * act, 0.2, 0.9)
            else:
                alpha = clamp(0.10 + 0.08 * df + 0.30 * act, 0.04, 0.55)
            color = "#b9ecff" if link else "#6389b7"
            if self.picked and not link:
                color = "#2c4260"
            self.canvas.coords(item, *pts)
            self.canvas.itemconfigure(item, state="normal",
                                      fill=hex_mix(BG, color, alpha),
                                      width=2 if link else 1)
            active.add(item)
        # 只隐藏上一帧画过、这一帧没画的边；不要每帧遍历 1000 条边。
        for item in self._edge_drawn - active:
            self.canvas.itemconfigure(item, state="hidden")
        self._edge_drawn = active

    # ---- 出生期动画 -------------------------------------------------------
    def _render_birth(self, now: float) -> None:
        cam = self._prep_cam()
        cx, cy = self.W // 2, int(self.H * 0.48)
        pulse = max(0.0, 1.0 - (now - self.pulse) / 1.5) if self.pulse else 0.0
        br = 1.0 + 0.055 * math.sin(now * 0.75) + (
            0.10 * math.sin(pulse * math.pi) if pulse else 0)
        # 环和尘都在世界坐标里，拖拽/滚轮改变的是相机，所以画面会真的转。
        for rec in self._birth_items.get("rings", []):
            xy: list[float] = []
            for (x, y, z) in rec["world"]:
                px, py, pz, ps = self._project(x, y, z, cam)
                if not ps:
                    xy = []
                    break
                depth = clamp(cam[4] / max(0.2, pz), 0.62, 1.35)
                xy.extend((px, py))
            if len(xy) >= 4:
                self.canvas.coords(rec["item"], *xy)
                self.canvas.itemconfigure(
                    rec["item"], fill=hex_mix(BG, rec["color"], 0.72 + 0.20 * depth))
        for d in self._birth_items.get("dust", []):
            # 每粒尘也有自己的缓慢轨道，再交给同一个相机投影。
            ang = now * d["sp"]
            ca, sa = math.cos(ang), math.sin(ang)
            x = d["x"] * ca - d["z"] * sa
            z = d["x"] * sa + d["z"] * ca
            px, py, pz, ps = self._project(x, d["y"], z, cam)
            if not ps:
                self.canvas.itemconfigure(d["item"], state="hidden")
                continue
            depth = clamp(cam[4] / max(0.2, pz), 0.55, 1.45)
            s = max(0.35, d["sz"] * ps * 0.009)
            if s < 0.30:
                self.canvas.itemconfigure(d["item"], state="hidden")
                continue
            self.canvas.itemconfigure(d["item"], state="normal")
            self.canvas.coords(d["item"], px, py, px + s, py + s)
            self.canvas.itemconfigure(
                d["item"], fill=hex_mix(BG, d["col"], clamp(d["al"] * depth, 0.05, 0.68)))
        rr = 6.0 * br
        self.canvas.coords(self._birth_items["halo"], cx, cy)
        self.canvas.coords(self._birth_items["hot"], cx, cy)
        self.canvas.coords(self._birth_items["rim"],
                           cx - 13 * br, cy - 13 * br, cx + 13 * br, cy + 13 * br)
        self.canvas.coords(self._birth_items["core"], cx - rr, cy - rr,
                           cx + rr, cy + rr)
        self.canvas.coords(self._birth_items["label_shadow"], cx + 1, cy - 26)
        self.canvas.coords(self._birth_items["label"], cx, cy - 27)
        self.canvas.coords(self._birth_items["tip"], cx, cy + 36)
        if pulse:
            rr2 = 20 + 30 * (1 - pulse)
            self.canvas.itemconfigure(self._birth_items["pulse"], state="normal")
            self.canvas.coords(self._birth_items["pulse"], cx - rr2, cy - rr2,
                               cx + rr2, cy + rr2)
        else:
            self.canvas.itemconfigure(self._birth_items["pulse"], state="hidden")

    # ---- 交互 -------------------------------------------------------------
    def _pick(self, mx: float, my: float) -> Optional[dict]:
        if self.birth:
            n = self.nodes[0] if self.nodes else None
            if not n:
                return None
            cx, cy = self.W // 2, int(self.H * 0.48)
            rr = max(20, min(self.W, self.H) * 0.065)
            return n if math.hypot(mx - cx, my - cy) < rr else None
        best = None
        bd = 1e18
        for n in getattr(self, "_visible_order", []):
            if not self._shown(n):
                continue
            sx, sy, _sz, _ps, r = n.get("_screen", (0, 0, 0, 0, 0))
            if not r:
                continue
            d = math.hypot(mx - sx, my - sy)
            rr = max(9.0, r * 1.6)
            if d < rr and d < bd:
                bd = d
                best = n
        return best

    def _on_press(self, event: tk.Event) -> None:
        self.cam["tween"] = None
        self.dragging = True
        self.drag_data = {
            "x": float(event.x), "y": float(event.y),
            "yaw0": self.cam["yaw"], "pitch0": self.cam["pitch"],
            "moved": 0.0, "px": float(event.x), "py": float(event.y),
            "t": _now(),
        }
        self._mark_dirty(0.25)
        try:
            self.canvas.configure(cursor="fleur")
        except Exception:
            pass

    def _on_drag(self, event: tk.Event) -> None:
        d = self.drag_data
        if not d:
            return
        dx = event.x - d["x"]
        dy = event.y - d["y"]
        d["moved"] = max(d["moved"], math.hypot(dx, dy))
        self.cam["yaw"] = d["yaw0"] - dx * 0.0055
        self.cam["pitch"] = clamp(d["pitch0"] + dy * 0.0045, -1.32, 1.32)
        now = _now()
        dt = max(0.01, now - d["t"])
        vy = -(event.x - d["px"]) / dt * 0.0055
        vp = (event.y - d["py"]) / dt * 0.0045
        self.cam["vyaw"] = clamp(self.cam["vyaw"] * 0.5 + vy * 0.5, -5, 5)
        self.cam["vpitch"] = clamp(self.cam["vpitch"] * 0.5 + vp * 0.5, -4, 4)
        d["px"], d["py"], d["t"] = float(event.x), float(event.y), now
        self._mark_dirty(0.30)

    def _on_release(self, event: tk.Event) -> None:
        d = self.drag_data
        self.dragging = False
        self.drag_data = None
        try:
            self.canvas.configure(cursor="")
        except Exception:
            pass
        if not d:
            return
        if math.hypot(self.cam["vyaw"], self.cam["vpitch"]) < 0.12:
            self.cam["vyaw"] = 0.0
            self.cam["vpitch"] = 0.0
        self._mark_dirty(0.55)
        if d["moved"] >= 5:
            return
        now = _now()
        if self.last_click and now - self.last_click < 0.34 and math.hypot(
                event.x - self.last_click_pos[0], event.y - self.last_click_pos[1]) < 12:
            self.last_click = 0.0
            self.reset_view()
            return
        self.last_click = now
        self.last_click_pos = (event.x, event.y)
        n = self._pick(float(event.x), float(event.y))
        if n:
            self.pulse = now
            if not self.birth:
                self.recall(n)
        else:
            self.clear_pick()

    def _on_motion(self, event: tk.Event) -> None:
        if self.dragging:
            return
        h = self._pick(float(event.x), float(event.y))
        if h is not self.hover:
            self.hover = h
            self._mark_dirty(0.12)
            try:
                self.canvas.configure(cursor="hand2" if h else "")
            except Exception:
                pass

    def _on_wheel(self, event: tk.Event) -> None:
        self.cam["tween"] = None
        dy = float(event.delta)
        self.cam["tzoom"] = clamp(self.cam["tzoom"] * math.exp(-dy * 0.0012), 0.4, 4.0)
        self._mark_dirty(0.55)

    def reset_view(self) -> None:
        y1 = 0.62
        while y1 - self.cam["yaw"] > math.pi:
            y1 -= math.pi * 2
        while y1 - self.cam["yaw"] < -math.pi:
            y1 += math.pi * 2
        self.cam["tween"] = {
            "t0": _now(), "ms": 520,
            "yaw0": self.cam["yaw"], "pitch0": self.cam["pitch"],
            "zoom0": self.cam["zoom"],
            "yaw1": y1, "pitch1": 0.42, "zoom1": 1.0,
        }
        self.cam["vyaw"] = self.cam["vpitch"] = 0.0
        self.cam["tzoom"] = 1.0
        self._mark_dirty(0.65)

    # ---- 时间轴 -----------------------------------------------------------
    def _on_time(self, value: str) -> None:
        self.t_now = clamp(float(value) / 1000.0, 0.0, 1.0)
        if self.picked and not self._shown(self.picked):
            self.clear_pick()
        self._mark_dirty(0.15)

    def _shown(self, n: dict) -> bool:
        if self.birth or not self.t_max:
            return True
        try:
            ts = float(n.get("ts") or 0)
        except Exception:
            ts = 0.0
        return ts <= self.t_min + (self.t_max - self.t_min) * self.t_now + 1e-6

    # ---- 详情 / 想起 ------------------------------------------------------
    def clear_pick(self) -> None:
        self.picked = None
        self.act.clear()
        self.ui_card.place_forget()
        self._set_say("")
        if hasattr(self, "_visible_order"):
            self._visible_order = list(self.by_id.values())
        self._mark_dirty(0.10)

    def recall(self, n: dict) -> None:
        self.picked = n
        self.act.clear()
        t0 = _now()
        self.act[str(n.get("id"))] = (t0, 0)
        adj: dict[str, list[tuple[str, float]]] = {}
        for e in self.edges:
            a, b = str(e.get("a")), str(e.get("b"))
            try:
                w = float(e.get("w") or 0.5)
            except Exception:
                w = 0.5
            adj.setdefault(a, []).append((b, w))
            adj.setdefault(b, []).append((a, w))
        q = [(str(n.get("id")), 0)]
        seen = {str(n.get("id"))}
        while q:
            cur, hop = q.pop(0)
            if hop >= 3:
                continue
            for nid, _w in sorted(adj.get(cur, []), key=lambda x: -x[1])[:4]:
                if nid in seen:
                    continue
                seen.add(nid)
                self.act[nid] = (t0 + (hop + 1) * 0.23 + rnd01(nid, "d") * 0.09, hop + 1)
                q.append((nid, hop + 1))
        rest = []
        for nid, (t, hop) in sorted(self.act.items(), key=lambda kv: kv[1][0]):
            if nid == str(n.get("id")) or hop <= 0 or nid not in self.by_id:
                continue
            rest.append(str(self.by_id[nid].get("label") or ""))
            if len(rest) >= 7:
                break
        if rest:
            self._set_say(f"想起「{n.get('label')}」，顺着线亮起了：" + "、".join(rest))
        else:
            self._set_say(f"想起「{n.get('label')}」，这条还孤零零的，没有连线")
        self.show_card(n)
        self.ui_card.place(relx=1.0, x=-20, y=72, anchor="ne", width=340)
        self._mark_dirty(2.4)

    def _set_say(self, text: str, clear_after: float = 0.0) -> None:
        if self._say_clear_job:
            try:
                self.root.after_cancel(self._say_clear_job)
            except Exception:
                pass
            self._say_clear_job = None
        self.say.configure(text=text)
        if clear_after:
            self._say_clear_job = self.root.after(int(clear_after * 1000),
                                                  lambda: self.say.configure(text=""))

    def show_card(self, n: dict) -> None:
        self.card_title.configure(text=str(n.get("label") or "—"))
        for w in self.card_tags.winfo_children():
            w.destroy()
        scope = "认识" if n.get("kind") == "fact" else (
            "她自己" if n.get("kind") == "self" else "经历")
        src = "亲历" if n.get("scope") == "real" else "听来的"
        stage = ""
        if n.get("stage") and n.get("stage") != "raw":
            stage = {"segment": "已分段", "meaning": "已提炼意义",
                     "deep": "已沉淀"}.get(str(n.get("stage")), str(n.get("stage")))
        elif n.get("kind") not in ("fact", "self"):
            stage = "待沉淀"
        tags = [(scope, INK_SOFT), (src, INK_SOFT),
                (str(n.get("emotion") or "—"), node_color(n))]
        if stage:
            tags.append((stage, INK_SOFT))
        if n.get("is_echo"):
            tags.append(("残响", DIM))
        if n.get("recon") is not None and float(n.get("recon") or 0) < 0.6:
            tags.append(("有推测成分", AMBER))
        for text, color in tags:
            tk.Label(self.card_tags, text=text, bg=LINE, fg=color,
                     font=self.f_mono, padx=6, pady=1).pack(side="left", padx=(0, 4))
        ts = float(n.get("ts") or 0)
        emo = str(n.get("emotion") or "—")
        emo_n = EMO_VALUE.get(emo, 3)
        self.card_rows["time"].configure(text=_time_text(ts))
        self.card_rows["emo"].configure(text=f"{emo}（强度 {emo_n}/10）")
        ret = n.get("ret", n.get("retention"))
        self.card_rows["ret"].configure(
            text=(f"{round(float(ret) * 100)}%" if ret is not None else "—"))
        mean = n.get("meaning")
        self.card_rows["mean"].configure(
            text=(f"{round(float(mean) * 100)}%" if mean is not None else "—"))
        res = n.get("residue")
        if res is not None:
            res_txt = f"{round(float(res) * 100)}%"
        elif n.get("emo_i") is not None:
            res_txt = f"还留着 {round(clamp(float(n.get('emo_i') or 0), 0, 1) * 10)}/10"
        else:
            res_txt = "—"
        self.card_rows["res"].configure(text=res_txt)
        echo = n.get("echo")
        if echo is not None:
            echo_txt = f"{round(float(echo) * 100)}%"
        elif n.get("is_echo"):
            echo_txt = "还在（细节已淡）"
        else:
            echo_txt = "—"
        self.card_rows["echo"].configure(text=echo_txt)
        self.card_rows["imp"].configure(text=f"{round(clamp(float(n.get('imp') or 0), 0, 1) * 100)}%")
        self.card_rows["used"].configure(text=f"{int(n.get('used') or 0)} 次")
        self.card_rows["deg"].configure(text=f"{int(n.get('deg') or 0)} 条")
        body = str(n.get("text") or "")
        if n.get("scene"):
            body += f"\n场景：{n.get('scene')}"
        self.card_body.configure(state="normal")
        self.card_body.delete("1.0", "end")
        self.card_body.insert("1.0", body)
        self.card_body.configure(state="disabled")
        nid = str(n.get("id"))
        near = []
        for e in sorted(self.edges, key=lambda x: -float(x.get("w") or 0)):
            if str(e.get("a")) == nid or str(e.get("b")) == nid:
                other = self.by_id.get(str(e.get("b") if str(e.get("a")) == nid else e.get("a")))
                if other:
                    near.append(f"{other.get('label')}（{e.get('why') or ''}）")
                if len(near) >= 4:
                    break
        self.card_rel.configure(text=("挨着：" + "、".join(near)) if near else "还没有连上线")

    def _act_alpha(self, n: dict, now: float) -> float:
        rec = self.act.get(str(n.get("id")))
        if not rec:
            return 0.0
        t0, _hop = rec
        e = now - t0
        if e < 0:
            return 0.0
        flash = math.exp(-max(0.0, e - 0.10) * 2.2)
        travel = e / 0.10 if e < 0.10 else 1.0
        return min(1.0, flash * travel)

    # ---- 面板 -------------------------------------------------------------
    def _update_header(self) -> None:
        self.stats.configure(
            text=f"来历 {self.meta.get('n_episodes', len(self.nodes))} 段　"
                 f"人和物 {self.meta.get('n_people', 0)}　"
                 f"连接 {self.meta.get('n_edges', len(self.edges))}")
        self.sub.configure(text="外层是新的经历，越往里越沉淀")

    def _update_panel(self, s: Optional[dict], learn: Optional[dict],
                      si: Optional[dict], mem: Optional[dict]) -> None:
        if not s:
            return
        self._last_panel_update = _now()
        try:
            self.pv["days"].configure(text=self._live_age())
            energy = float(s.get("energy") or 0)
            self.pv["energy"].configure(text=f"{energy:.2f}")
            self._set_bar("energy", energy)
            mood = float(s.get("mood") or 0)
            self.pv["mood"].configure(
                text="低" if mood < -0.3 else ("好" if mood > 0.45 else "平"))
            self._set_bar("mood", (mood + 1) / 2)
            turns = int(s.get("turns") or 0)
            lonely = float(s.get("lonely") or 0)
            if not turns:
                lonely_txt = "还没人"
            elif lonely < 0.05:
                lonely_txt = "刚有人"
            else:
                lonely_txt = f"{float(s.get('silent_h') or 0):.1f}h"
            self.pv["lonely"].configure(text=lonely_txt)
            self._set_bar("lonely", lonely)
            rel = s.get("rel") or {}
            for key, label in [("fam", "熟悉"), ("trust", "信任"),
                               ("attach", "依恋"), ("boundary", "边界")]:
                val = float(rel.get(label) or 0)
                self.pv[key].configure(text=f"{val:.2f}")
                self._set_bar(key, val)
            feel = str(s.get("text") or "").replace("\n- ", " ").strip()
            feel_line = ""
            if s.get("feel_text") and s.get("feel_text") != "情绪不重，平平的":
                top = str(s.get("feel_top") or "")
                if top and top != "—":
                    feel_line = f"{s.get('feel_text')}（{' · '.join(top.split(' · ')[:2])}）"
                else:
                    feel_line = str(s.get("feel_text"))
            feel_text = (feel + "　" + feel_line).strip("　") or "她此刻没什么特别的起伏。"
            # 桌面左栏只有 272px，长句会挤掉「学习」那一行；这里保留摘要，
            # 完整情绪仍在点开详情/她在想什么里，不在这里硬塞满。
            self.pv["feel"].configure(text=self._clip(feel_text, 72))
            self.pv["seven"].configure(text=str(s.get("seven_text") or "—"))
            self.pv["desire"].configure(text=str(s.get("desire_text") or "—"))
            if learn:
                pending = int(learn.get("pending") or 0)
                approved = int(learn.get("approved") or 0)
                cur = int(learn.get("curiosities") or 0)
                last = learn.get("last_study") or {}
                q = f" · 自己在琢磨「{str(last.get('question'))[:10]}」" if last.get("question") else ""
                self.pv["learn"].configure(text=self._clip(
                    f"待审 {pending} 条 · 好奇 {cur} 件 · 自习 {learn.get('studies', 0)} 次{q}",
                    44))
                mem_n = int((mem or {}).get("episodes") or self.meta.get("n_episodes") or 0)
                self.pv["sihai"].configure(text=f"记忆 {mem_n} 条 · 知识 {approved} 条")
            if si:
                self.pv["dispatch"].configure(
                    text=f"照见 {len(si.get('aware') or [])} · 候选 {si.get('candidates', 0)}")
            self.pv["echo"].configure(text=self._ambient_text(
                self.meta.get("ambient") or {}))
            self.pv["expr"].configure(text=self._expr_text(
                self.meta.get("expression") or {}))
            if s.get("growth") and s.get("growth") != "frozen":
                self.lock_line.configure(text="成长：已开启\n她可以开始把经历沉淀成习惯了。",
                                         fg=GREEN)
        except Exception:
            pass

    def _set_bar(self, key: str, value: float) -> None:
        canvas, fill = self.pb[key]
        # 面板先 update 再布局，winfo_width 可能还是 1；按创建时的内容宽算，
        # 否则第一帧的条全是零，之后再也不会重画。
        width = self.bar_widths.get(key, max(20, canvas.winfo_width()))
        canvas.coords(fill, 0, 0, max(0.0, min(1.0, value)) * width, 3)

    def _live_age(self) -> str:
        since = float(self.meta.get("since") or 0)
        if not since:
            return "—"
        h = max(0.0, (_now() - since) / 3600)
        if h < 1:
            return f"{int(h * 60)} 分钟"
        if h < 24:
            return f"{h:.1f} 小时"
        return f"{h / 24:.2f} 天"

    def _ambient_text(self, a: Optional[dict]) -> str:
        if not a:
            return "—"
        if a.get("text"):
            return self._clip(a["text"], 28)
        tone = float(a.get("tone") or 0)
        inten = float(a.get("intensity") or 0)
        imps = [x for x in (a.get("imprints") or []) if x]
        if inten < 0.05 and not imps:
            return "—"
        base = ("说不出为什么的不安" if tone < -0.12 else
                ("说不出为什么的暖意" if tone > 0.12 else "说不上来的平静"))
        if imps:
            base += "（" + "、".join(
                str(x.get("text") if isinstance(x, dict) else x) for x in imps[:2]) + "）"
        return self._clip(base, 28)

    def _expr_text(self, e: Optional[dict]) -> str:
        if not e:
            return "—"
        if e.get("text"):
            return self._clip(e["text"], 30)
        tone = e.get("tone")
        if tone is not None:
            if isinstance(tone, str):
                return self._clip(tone, 30)
            try:
                t = float(tone)
                return "低，话短" if t < -0.2 else ("轻，话多一点" if t > 0.2 else "平")
            except Exception:
                pass
        if e.get("label"):
            return self._clip(e["label"], 30)
        if e.get("mood") is not None:
            return self._clip(e["mood"], 30)
        return "—"

    @staticmethod
    def _clip(s: Any, n: int) -> str:
        s = str(s)
        return s[:n] + "…" if len(s) > n else s

    def _build_legend(self) -> None:
        for w in self.legend.winfo_children():
            w.destroy()
        # 颜色语义不变：颜色 = 她当时的情绪。只是把桌面版调色换成更亮的
        # 一套，否则「平静」的灰白会把整片星海压成一张白点图。
        emo = display_palette(self.meta)
        items = [(str(k), str(v)) for k, v in emo.items()]
        cols = 11
        for i, (name, color) in enumerate(items):
            lbl = tk.Label(self.legend, text=f"● {name}", fg=color, bg=BG,
                           font=self.f_mono)
            lbl.grid(row=i // cols, column=i % cols, sticky="w",
                     padx=(0, 11), pady=1)
        extras = [("○ 空心：听来的", "#b9c9df"),
                  ("● 实心：亲历", "#ffffff"),
                  ("◎ 灰环：细节忘了，改变还在", "#98a3b3"),
                  ("◎ 琥珀环：细节还在，情绪残留重", "#ffb35c"),
                  ("拖动转视角 · 滚轮远近", "#50657f")]
        row = (len(items) + cols - 1) // cols
        for j, (text, color) in enumerate(extras):
            lbl = tk.Label(self.legend, text=text, fg=color, bg=BG,
                           font=self.f_mono)
            lbl.grid(row=row, column=j * 2, columnspan=2, sticky="w",
                     padx=(0, 13), pady=(2, 0))

    def _clock_tick(self) -> None:
        if self._closing:
            return
        if self.birth:
            self._update_header()
            if self.meta.get("state"):
                self._update_panel(self.meta.get("state"), self.meta.get("learn"),
                                   None, {"episodes": self.meta.get("n_episodes", 0)})
        # 抽屉开着时，每 15 秒跟着她的状态更新；日记不自动刷，免得打断阅读。
        if self.view_kind and self.view_kind != "diary" and not self.view_loading:
            self._refresh_view()
        self._clock_job = self.root.after(15000, self._clock_tick)

    # ---- 对话 -------------------------------------------------------------
    def _send(self) -> None:
        if self._busy:
            return
        text = self.entry.get().strip()
        if not text:
            return
        self.entry.delete(0, "end")
        self.chatlog.add("user", text)
        self._busy = True
        self.send_btn.configure(state="disabled")
        self.chat_status.configure(text="她在想…")
        threading.Thread(target=self._chat_worker, args=(text,), daemon=True).start()

    def _chat_worker(self, text: str) -> None:
        try:
            with self._chat_lock:
                if self._chat is None:
                    from .chat import Chat
                    self._chat = Chat()
                r = self._chat.turn(text)
        except Exception as e:
            r = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        self._post(lambda: self._chat_done(r))

    def _chat_done(self, r: dict) -> None:
        if self._closing:
            return
        self._busy = False
        self.send_btn.configure(state="normal")
        self.chat_status.configure(text="她的回答")
        if r.get("ok"):
            if r.get("silent"):
                # 自然沉默：不生成空气泡、不朗读，也不当成错误。
                self.chat_status.configure(text="她这次没接")
                self.pulse = _now()
                self._update_panel(r.get("state"), r.get("learn"),
                                   r.get("dispatch"), r.get("memory"))
                if self.view_kind:
                    self._refresh_view()
                self.entry.focus_set()
                return
            reply_text = str(r.get("reply") or "").strip()
            if reply_text:
                self.chatlog.add("her", reply_text)
            thought = r.get("thought") or {}
            line = thought.get("line") if isinstance(thought, dict) else None
            if line:
                self.chatlog.add("think", str(line))
            self.pulse = _now()
            self._update_panel(r.get("state"), r.get("learn"),
                               r.get("dispatch"), r.get("memory"))
            if r.get("ambient"):
                self.meta["ambient"] = r.get("ambient")
            if r.get("expression"):
                self.meta["expression"] = r.get("expression")
            self.chat_status.configure(text="她还在")
            if self.view_kind:
                self._refresh_view()
            if self.read_var.get() and reply_text:
                threading.Thread(target=self._speak_worker,
                                 args=(reply_text,), daemon=True).start()
        else:
            self.chatlog.add("error", "（没接上：" + str(r.get("error") or "unknown") + "）")
            self.chat_status.configure(text="没接上")
        self.entry.focus_set()

    def _speak_worker(self, text: str) -> None:
        try:
            with self._tts_lock:
                if self._tts is None:
                    from .tts import TTS
                    self._tts = TTS()
                self._tts.speak(text)
        except Exception as e:
            msg = f"朗读失败：{type(e).__name__}"
            self._post(lambda: self._safe_status(msg))

    def _safe_status(self, text: str) -> None:
        if self._closing:
            return
        try:
            self.chat_status.configure(text=text)
        except Exception:
            pass

    # ---- 语音输入 ---------------------------------------------------------
    def _voice_start(self) -> None:
        if self._voice_busy or self._busy:
            return
        self._voice_busy = True
        self.mic_btn.configure(state="disabled", text="在听…")
        self.chat_status.configure(text="开口说话，停一下就会收")
        threading.Thread(target=self._voice_worker, daemon=True).start()

    def _voice_worker(self) -> None:
        try:
            from .audio import Endpointer, Mic, record_utterance
            mic = Mic()
            ok = mic.open(timeout=3.0)
            if not ok:
                raise RuntimeError(mic.last_error or "麦克风打不开")
            ep = Endpointer()
            pcm = record_utterance(mic, ep, max_sec=12.0, wait_speech_sec=6.0)
            mic.close()
            if pcm is None or len(pcm) == 0:
                self._post(lambda: self._voice_done("", "没听到声音"))
                return
            with self._chat_lock:
                if self._stt is None:
                    from .stt import STT
                    self._stt = STT()
                r = self._stt.transcribe(pcm)
            text = str(r.get("text") or "").strip()
            if not text:
                self._post(lambda: self._voice_done("", "没听清"))
            else:
                self._post(lambda t=text: self._voice_done(t, ""))
        except Exception as e:
            self._post(lambda err=e: self._voice_done(
                "", f"{type(err).__name__}: {err}"))

    def _voice_done(self, text: str, err: str) -> None:
        if self._closing:
            return
        self._voice_busy = False
        self.mic_btn.configure(state="normal", text="语音")
        if err:
            self.chat_status.configure(text=err)
            self.chatlog.add("error", f"（语音：{err}）")
            return
        self.entry.delete(0, "end")
        self.entry.insert(0, text)
        self._send()

    # ---- 收尾 -------------------------------------------------------------
    def _capture(self) -> None:
        if not self.screenshot or self._closing:
            return
        # 不抢 topmost：抢了以后 DWM 合成会截出一张黑图。
        # 需要干净截图时用 --geometry 把窗口挪开桌面宠物等悬浮层即可。
        self.root.update_idletasks()
        self.root.update()
        self.root.after(650, self._grab_screen)

    def _grab_screen(self) -> None:
        if not self.screenshot or self._closing:
            return
        try:
            self.root.update_idletasks()
            x = self.root.winfo_rootx()
            y = self.root.winfo_rooty()
            w = self.root.winfo_width()
            h = self.root.winfo_height()
            img = ImageGrab.grab(bbox=(x, y, x + w, y + h))
            path = Path(self.screenshot)
            path.parent.mkdir(parents=True, exist_ok=True)
            img.save(path)
            print(f"[desktop] screenshot -> {path}")
        except Exception as e:
            print(f"[desktop] screenshot failed: {type(e).__name__}: {e}")
        if self.screenshot_exit:
            self.root.after(250, self.root.destroy)

    def _on_close(self) -> None:
        self._closing = True
        for job in (self._tick_job, self._clock_job, self._resize_job):
            if job:
                try:
                    self.root.after_cancel(job)
                except Exception:
                    pass
        self._tick_job = self._clock_job = self._resize_job = None
        try:
            if self._tts is not None:
                self._tts.stop()
        except Exception:
            pass
        try:
            if self._chat is not None:
                self._chat.mem.close()
                self._chat.mind.close()
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="app.desktop",
                                 description="瓦尔基里 · 识海桌面版")
    ap.add_argument("--demo", action="store_true", help="使用内置示例数据")
    ap.add_argument("--screenshot", type=Path, help="载入后把窗口截图保存到指定路径")
    ap.add_argument("--screenshot-exit", action="store_true",
                    help="截图后退出（配合 --screenshot）")
    ap.add_argument("--geometry", default="1380x880", help="窗口尺寸，默认 1380x880")
    ap.add_argument("--view", default="", help="载入后直接打开：thought/dispatch/learning/echo/diary")
    ap.add_argument("--response-gate", choices=("off", "shadow", "enforced"),
                    default=None,
                    help="自然回应闸门：off 保持原行为，shadow 只记录，enforced 执行")
    ap.add_argument("--social-silence", choices=("off", "on"), default=None,
                    help="社交性沉默（自然结束/无新价值）")
    args = ap.parse_args(argv)
    if args.response_gate:
        C.RESPONSE_GATE_MODE = args.response_gate
    if args.social_silence:
        C.SOCIAL_SILENCE = args.social_silence == "on"

    _dpi_aware()
    root = tk.Tk()
    app = DesktopApp(root, demo=args.demo, screenshot=args.screenshot,
                     screenshot_exit=args.screenshot_exit,
                     geometry=args.geometry, open_view=args.view)
    if args.screenshot and args.screenshot_exit:
        # 不要直接 destroy：主循环里的 after(_tick) 还会触发一次，
        # 会报 invalid command name。走 _on_close 先停掉所有 after。
        root.after(7000, app._on_close)
    try:
        root.mainloop()
    except KeyboardInterrupt:
        app._on_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
