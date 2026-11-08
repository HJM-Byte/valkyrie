# -*- coding: utf-8 -*-
"""日记与 23:00 定时器的自检。不碰 robot/diary/ 真实目录。"""
from __future__ import annotations

import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.diary import Diary, DiaryScheduler  # noqa: E402

OK = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="diary-selftest-") as td:
        root = Path(td)
        # 2026-11-02 19:00 / 23:00 两个固定时刻，避免机器时钟影响。
        evening = datetime(2026, 11, 2, 19, 0).timestamp()
        late = datetime(2026, 11, 2, 23, 0).timestamp()
        d = Diary(root=root, clock=lambda: evening, use_llm=False)
        s = d.summary()
        check("空目录 has_today=False", s["has_today"] is False)
        check("空目录 recent=[]", s["recent"] == [])
        check("写入时间固定 23:00", s["write_at"] == "23:00")

        # 22:59 不触发，23:00 触发。
        sch = DiaryScheduler(d, hour=23, minute=0)
        check("22:59 不 due", sch.due(datetime(2026, 11, 2, 22, 59).timestamp()) is False)
        check("23:00 到点", sch.due(late) is True)

        # 手动写一页，再读回来；正文不把固定头算进去。
        r = d.write("今天把识海重新排了一遍。", day="2026-11-02",
                    source="test", now=evening)
        check("write ok", r["ok"] and r["chars"] >= 8)
        got = d.read("2026-11-02")
        check("read ok", got["ok"] and "识海重新排" in got["text"])
        check("read 不带固定头", "瓦尔基里的日记" not in got["text"])
        check("list 有一页", len(d.list()) == 1)
        check("summary 认出今天", d.summary()["has_today"] is True)

        # 已存在时 ensure 不覆盖
        old = d.read("2026-11-02")["text"]
        r2 = d.ensure_today(now=late, use_llm=False)
        check("ensure 不覆盖", r2.get("skipped") is True and d.read("2026-11-02")["text"] == old)

        # 强制覆盖 + 不同日期
        r3 = d.write("换一页。", day="2026-11-03", source="test", now=late + 86400)
        check("第二天独立文件", d.path_for("2026-11-03").is_file() and r3["day"] == "2026-11-03")
        check("list 两页", len(d.list()) == 2)
        check("meta 记录 source", d.read("2026-11-03")["meta"].get("source") == "test")

        # 23:00 之前不生成；到点后才生成（用独立临时目录，不污染上面的手写页）
        with tempfile.TemporaryDirectory(prefix="diary-selftest2-") as td2:
            d2 = Diary(root=Path(td2), clock=lambda: late, use_llm=False)
            sch2 = DiaryScheduler(d2, hour=23, minute=0)
            check("未到点 tick 不写", sch2.tick(now=evening)["due"] is False)
            check("到点 tick 写", sch2.tick(now=late).get("due") is True)
            check("到点后有文件", d2.path_for("2026-11-02").is_file())

        # 文件锁：模拟另一个进程持有锁时不写
        with tempfile.TemporaryDirectory(prefix="diary-selftest3-") as td3:
            d3 = Diary(root=Path(td3), clock=lambda: late, use_llm=False)
            lock = d3.root / ".2026-11-02.lock"
            lock.write_text("x", encoding="utf-8")
            r4 = d3.ensure_today(now=late, use_llm=False)
            check("有锁时返回 locked", r4.get("locked") is True)
            lock.unlink(missing_ok=True)

    print(f"\nDIARY {OK} ok, {FAIL} fail")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
