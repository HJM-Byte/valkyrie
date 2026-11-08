"""桌面版识海自检：不启动完整窗口，只验证数据、布局、面板和交互能挂起来。

跑法：
    C:\\robot\\venv\\Scripts\\python.exe tools\\selftest_desktop.py

它不会发消息、不会调用模型、不会叫醒她。Tk 窗口会短暂创建并 withdraw，
验证完成后销毁。
"""
from __future__ import annotations

import math
import sys
import time
import tkinter as tk
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.desktop_prep import display_palette, prepare_graph  # noqa: E402
from app.sihai import build_birth, build_demo  # noqa: E402


class Checker:
    def __init__(self) -> None:
        self.ok = 0
        self.fail: list[str] = []

    def check(self, name: str, cond: bool, detail: str = "") -> None:
        if cond:
            self.ok += 1
            print(f"  PASS  {name}")
        else:
            self.fail.append(name + (f"  [{detail}]" if detail else ""))
            print(f"  FAIL  {name}  {detail}")


def test_prepare(c: Checker) -> None:
    g = prepare_graph(build_demo())
    c.check("示例图节点数 = 72", len(g["nodes"]) == 72, str(len(g["nodes"])))
    c.check("示例图连线数 > 100", len(g["edges"]) > 100, str(len(g["edges"])))
    c.check("示例图不是出生期", not g["birth"])
    c.check("四层都有节点", sorted({int(n["pl"]) for n in g["nodes"]}) == [0, 1, 2, 3],
            str(sorted({int(n["pl"]) for n in g["nodes"]})))
    finite = all(
        math.isfinite(n["px3"]) and math.isfinite(n["py3"])
        and math.isfinite(n["pz3"]) for n in g["nodes"])
    c.check("所有节点坐标有限", finite)
    near = all(math.hypot(n["px3"], n["py3"], n["pz3"]) <= 1.03 for n in g["nodes"])
    c.check("所有节点被收在单位球内", near)
    c.check("每个节点都有颜色", all(n.get("col") for n in g["nodes"]))
    pal = display_palette(g["meta"])
    c.check("桌面情绪调色至少 21 色", len(pal) >= 21, str(len(pal)))
    c.check("平静不是原来的灰白", pal.get("平静") != "#c3ccdb", str(pal.get("平静")))
    c.check("星色已换到桌面调色", all(
        str(n.get("col")) != "#c3ccdb" for n in g["nodes"]))
    c.check("每个节点都有留存值", all(0 <= float(n.get("ret") or 0) <= 1
                                       for n in g["nodes"]))
    c.check("每条边两端都能解析", all(e.get("A") and e.get("B") for e in g["edges"]))
    c.check("会画 3 个壳环 + 4 个层锚点", len(g["circles"]) == 7,
            str(len(g["circles"])))

    b = prepare_graph(build_birth())
    c.check("出生期只有一颗星", b["birth"] and len(b["nodes"]) == 1)
    c.check("出生期没有连线", len(b["edges"]) == 0)
    c.check("出生星在原点", abs(b["nodes"][0]["px3"]) < 1e-9)
    c.check("出生星标签是我", b["nodes"][0].get("label") == "我")


def test_window(c: Checker) -> None:
    from app.desktop import DesktopApp

    root = tk.Tk()
    root.withdraw()
    app = DesktopApp(root, demo=True, geometry="1180x780")
    state = {"deadline": time.time() + 15.0, "done": False}

    def finish() -> None:
        if state["done"]:
            return
        state["done"] = True
        try:
            app._on_close()
        except Exception:
            pass

    def poll() -> None:
        if state["done"]:
            return
        if app.graph is None:
            if time.time() > state["deadline"]:
                c.check("窗口在 15 秒内载入示例图", False)
                finish()
            else:
                root.after(120, poll)
            return

        c.check("窗口在 15 秒内载入示例图", True)
        c.check("窗口载入后不是出生期", not app.birth)
        c.check("面板存在存在/精力/七情/六欲控件",
                all(k in app.pv for k in ("days", "energy", "seven", "desire")))
        c.check("精力条已经按状态画出来", "energy" in app.pb)
        c.check("对话入口存在", app.entry is not None and app.send_btn is not None)
        c.check("语音按钮存在", app.mic_btn is not None)
        c.check("时间轴存在且值为现在", int(app.scale.get()) == 1000,
                str(app.scale.get()))
        c.check("图例已生成", len(app.legend.winfo_children()) > 0)
        c.check("有全部标签开关", hasattr(app, "label_all") and app.label_chk is not None)
        c.check("窗口几何参数生效", root.geometry().startswith("1180x780"),
                root.geometry())

        if app.nodes:
            node = app.nodes[0]
            app.recall(node)
            c.check("点击星能打开详情卡", app.picked is node)
            c.check("详情卡标题已填", app.card_title.cget("text") != "")
            c.check("连线提示已填", len(app.say.cget("text")) > 0)
            app.clear_pick()
            c.check("关闭详情卡能清掉选中", app.picked is None)

        # 示例图拖拽后，节点屏幕坐标必须真的跟着相机动。
        if app.nodes:
            before = tuple(app.nodes[0].get("_screen") or ())
            app._on_press(types.SimpleNamespace(x=500, y=400))
            app._on_drag(types.SimpleNamespace(x=650, y=440))
            app._on_release(types.SimpleNamespace(x=650, y=440))
            app._render(time.time())
            after = tuple(app.nodes[0].get("_screen") or ())
            c.check("拖拽后星图坐标变化", before != after)

        # 出生期也必须能动：以前这里画的是屏幕坐标的扁椭圆，相机改了画面不动。
        app._apply_graph(prepare_graph(build_birth()))
        c.check("切到出生期", app.birth and len(app.nodes) == 1)
        c.check("出生期有 3D 壳环", len(app._birth_items.get("rings", [])) == 4)
        app._render_birth(time.time())
        rec = app._birth_items["rings"][0]
        before = tuple(app.canvas.coords(rec["item"]))
        app._on_press(types.SimpleNamespace(x=500, y=400))
        app._on_drag(types.SimpleNamespace(x=650, y=430))
        app._on_release(types.SimpleNamespace(x=650, y=430))
        app._render_birth(time.time())
        after = tuple(app.canvas.coords(rec["item"]))
        c.check("出生期拖拽后 3D 环坐标变化", before != after)

        root.after(100, finish)

    root.after(120, poll)
    try:
        root.mainloop()
    except tk.TclError:
        pass
    c.check("窗口能干净关闭", state["done"])


def main() -> int:
    print("=" * 64)
    print("桌面版识海自检")
    print("=" * 64)
    c = Checker()
    test_prepare(c)
    try:
        test_window(c)
    except Exception as e:
        c.fail.append(f"窗口自检异常: {type(e).__name__}: {e}")
        print(f"  FAIL  窗口自检异常: {type(e).__name__}: {e}")
    print("-" * 64)
    if c.fail:
        print(f"{c.ok} 通过 / {len(c.fail)} 失败")
        for item in c.fail:
            print("  -", item)
        return 1
    print(f"{c.ok} 通过 / 0 失败")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
