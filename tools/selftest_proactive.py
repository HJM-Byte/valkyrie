"""主动消息的队列与「不设节律」自检：不联网、不调用模型。"""
from __future__ import annotations

import shutil
from datetime import datetime, time as dtime
from pathlib import Path

from app import config as C
from app.proactive import ProactiveStore, can_send

ROOT = Path(__file__).resolve().parent.parent / "_selftest_tmp_proactive"
PASS: list[str] = []


def ok(name: str) -> None:
    PASS.append(name)
    print(f"PASS  {name}")


def at(hour: int, minute: int = 0) -> float:
    return datetime.combine(datetime.now().date(), dtime(hour, minute)).timestamp()


def main() -> int:
    if ROOT.exists():
        shutil.rmtree(ROOT, ignore_errors=True)
    ROOT.mkdir(parents=True, exist_ok=True)
    old_enabled = C.PROACTIVE_ENABLED
    store = None
    try:
        store = ProactiveStore(ROOT / "proactive.db")
        C.PROACTIVE_ENABLED = True

        gate = can_send()
        assert gate["ok"], gate
        ok("不设节律：没有每日上限、最小间隔、夜间禁发")

        C.PROACTIVE_ENABLED = False
        gate = can_send()
        assert not gate["ok"], gate
        C.PROACTIVE_ENABLED = True
        ok("总开关关闭时不发")

        rec = store.record("你今天忙吗？想听你说两句。", kind="greet",
                           seed="greet:x", ts=at(12))
        assert rec["id"] > 0
        assert store.last()["id"] == rec["id"]
        assert len(store.since(0)) == 1
        assert len(store.latest(3)) == 1
        ok("队列写入、游标与最近消息")

        gate = can_send()
        assert gate["ok"], gate
        ok("刚发过也允许下一次；节律已取消")

        store.record("第二句", kind="ask", seed="ask:q",
                     ts=at(12) + 3 * 3600)
        latest = store.latest(3)
        assert [x["text"] for x in latest] == [
            "你今天忙吗？想听你说两句。", "第二句"], latest
        assert store.count_today() >= 1
        ok("两条消息按时间顺序返回")
    finally:
        C.PROACTIVE_ENABLED = old_enabled
        if store is not None:
            store.close()
        shutil.rmtree(ROOT, ignore_errors=True)

    print(f"\n{len(PASS)} 通过 / 0 失败")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
