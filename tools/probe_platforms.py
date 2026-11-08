"""实探：B站 / 抖音 / 百度 这些平台接口现在还能不能用。

不另写一套抓取逻辑——直接调 `app.learning` 里真正在跑的方法，
通了就是夜间窗口也通；坏了就先修这里，再跑 `--verify`。

百度搜索是备用入口：它会间歇触发「百度安全验证」，所以单独标成可选，
不算故障——主入口是 Bing，百度只在 Bing 给空结果时顶上。

跑法（在 robot 目录）：
    python -m tools.probe_platforms
"""
from __future__ import annotations

import json
import time

from app.learning import Learning


def main() -> int:
    lr = Learning()
    ok = bad = warn = 0
    # (名称, 是否必过, 取数函数)
    tests = [
        ("B站 · 专栏正文", True, lambda: lr._bili_article_harvest(
            lr.channel("bilibili_article"), [], 1)),
        ("B站 · 知识区榜单", True, lambda: lr._bili_rank_harvest(
            lr.channel("bilibili_rank"), 1)),
        ("抖音 · 热榜", True, lambda: lr._douyin_harvest(
            lr.channel("douyin_hot"), 1)),
        ("百度 · 热搜", True, lambda: lr._baidu_top_harvest(
            lr.channel("baidu_top"), 1)),
        ("百度 · 百科", True, lambda: lr._baike_harvest(
            lr.channel("baidu_baike"), [], 1)),
        ("Bing 搜索", True, lambda: lr.search("记忆是怎么保存的", limit=3)),
        ("百度搜索（备用）", False, lambda: lr.search("记忆是怎么保存的", limit=3,
                                                      engine="baidu")),
    ]
    try:
        lr._fetches = 8
        for name, required, fn in tests:
            t0 = time.time()
            try:
                items = fn() or []
                chars = [len(i.get("text") or i.get("snippet") or "") for i in items]
                good = bool(items) and max(chars or [0]) >= 40
                if good:
                    ok += 1
                elif required:
                    bad += 1
                else:
                    warn += 1
                detail = {"n": len(items), "max_chars": max(chars or [0]),
                          "via": (items[0].get("via") if items else ""),
                          "sample": ((items[0].get("title") if items else "") or "")[:40]}
                mark = "OK  " if good else ("WARN" if not required else "FAIL")
                print(f"  {mark} {name:<16} {time.time() - t0:5.2f}s  "
                      f"{json.dumps(detail, ensure_ascii=False)[:110]}")
            except Exception as e:
                if required:
                    bad += 1
                    mark = "FAIL"
                else:
                    warn += 1
                    mark = "WARN"
                print(f"  {mark} {name:<16} {time.time() - t0:5.2f}s  "
                      f"{type(e).__name__}: {e}")
        print(f"\n合计：必过 {ok}/{ok + bad} 可用"
              + (f"，可选入口 {warn} 个未通（不算故障）" if warn else ""))
        return 0 if bad == 0 else 1
    finally:
        lr.close()


if __name__ == "__main__":
    raise SystemExit(main())
