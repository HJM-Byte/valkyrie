"""时间锚点自检：日期换算、最近对话、记忆日期戳。

    cd robot
    python -m tools.selftest_time_context

不写真实 memory.db；只用一个临时库，跑完删掉。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from app import temporal
from app.memory import Memory
from app.persona import Persona

OK = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


def main() -> int:
    now = datetime(2026, 11, 3, 22, 3, tzinfo=temporal.TZ)
    yest = (now - timedelta(days=1)).timestamp()
    before = (now - timedelta(days=2)).timestamp()

    check("昨天", temporal.relative_day(yest, now) == "昨天")
    check("前天", temporal.relative_day(before, now) == "前天")
    anchors = temporal.anchor_lines(now)
    check("锚点含今天日期", "2026-11-03" in anchors)
    check("锚点含昨天日期", "2026-11-02" in anchors)
    check("锚点含前天日期", "2026-11-01" in anchors)

    with TemporaryDirectory() as d:
        mem = Memory(Path(d) / "memory.db")
        try:
            mem.add_turn("user", "昨天我们聊了什么？", ts=yest)
            mem.add_turn("assistant", "昨天说的是考试。",
                         ts=yest + 180)
            mem.add_turn("user", "今天几号？", ts=(now - timedelta(minutes=10)).timestamp())

            prompt = Persona().compose("今天几号？", mem=mem, now=now)
            check("提示里有今天日期", "2026-11-03" in prompt)
            check("提示里有昨天日期", "2026-11-02" in prompt)
            check("提示里有最近对话", "【最近对话】" in prompt)
            check("最近对话带昨天", "昨天 22:03" in prompt, prompt[:500])

            birth = Persona().compose_birth("你好", now=now)
            check("出生期也有日期锚点", "2026-11-03" in birth)

            mem.add_episode("昨天我们聊了考试", kind="talk", ts=yest,
                            who="他", imp=0.8)
            block = mem.episodes_block("考试", k=2)
            check("记忆日期戳带昨天", "昨天" in block, block)
            prompt2 = Persona().compose("考试", mem=mem, now=now)
            check("提示里有最近记下的事", "【最近记下的事】" in prompt2)
        finally:
            mem.close()

    print(f"\n结果：{OK} ok / {FAIL} fail")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
