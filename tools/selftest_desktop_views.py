# -*- coding: utf-8 -*-
"""桌面端五视图与扇区布局的最小自检（不创建 Tk 窗口）。

跑法（仓库根目录）：
    python tools\\selftest_desktop_views.py
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.desktop_prep import _cluster_of, prepare_graph  # noqa: E402
from app.sihai import graph_payload  # noqa: E402

ok = fail = 0


def check(cond: bool, name: str, detail: str = "") -> None:
    global ok, fail
    if cond:
        ok += 1
        print(f"  PASS  {name}")
    else:
        fail += 1
        print(f"  FAIL  {name}" + (f"  [{detail}]" if detail else ""))


def main() -> int:
    print("— 桌面端八视图与扇区布局 —")
    g = prepare_graph(graph_payload(demo=True))
    nodes, edges = g["nodes"], g["edges"]
    check(len(nodes) > 10, "示例图有节点", f"n={len(nodes)}")
    check(bool(edges), "示例图有连线", f"edges={len(edges)}")
    check(all(math.isfinite(float(n["px3"])) and math.isfinite(float(n["py3"]))
              and math.isfinite(float(n["pz3"])) for n in nodes),
          "所有节点坐标有限")
    check(all(math.hypot(float(n["px3"]), float(n["py3"]),
                         float(n["pz3"])) <= 1.05 for n in nodes),
          "节点都在单位球内")
    check(all(n.get("pl") in (0, 1, 2, 3) for n in nodes), "四层都合法")
    check(all(isinstance(n.get("is_ambient"), bool) for n in nodes), "残响标记是布尔值")
    by_id = {str(n.get("id")): n for n in nodes}
    check(all(str(e.get("a")) in by_id and str(e.get("b")) in by_id for e in edges),
          "边的两端都存在")
    check(_cluster_of({"id": "x", "kind": "episode", "emotion": "开心",
                       "day": "2026-11-02"}) ==
          _cluster_of({"id": "x", "kind": "episode", "emotion": "开心",
                       "day": "2026-11-02"}), "同一节点扇区稳定")
    check(_cluster_of({"kind": "fact", "label": "名字"}) == "fact:名", "事实单独成扇区")
    # 桌面版八视图的入口键必须和网页版同名；这里只查常量，不建窗口。
    from app.desktop import VIEWS
    check([k for k, _ in VIEWS] == ["thought", "dispatch", "learning",
                                   "feedback", "meaning", "persona",
                                   "echo", "diary"],
          "八视图键齐全", str([k for k, _ in VIEWS]))
    check(len({k for k, _ in VIEWS}) == 8, "八视图不重复")
    print(f"\nDESKTOP {ok} ok, {fail} fail")
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
