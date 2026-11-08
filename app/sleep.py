"""休眠：把此刻封存，下次接着活。

休眠不是关机。区别只有一条：
    关掉服务，她的时间也停了——下次启动时「过了多久」是错的；
    休眠要先把「封存在几点、封存时她是什么样」写下来，
    下次醒来按真实时间补上，她才知道自己睡了一觉。

写什么：
    data/sleep.json   一次休眠记录（状态快照 + 记忆条数 + 原因 + 时刻）
    data/sleep_log.jsonl  每次醒来的流水（睡了多久、什么时候醒的）

醒来时 Chat 会自动读这条记录，把它变成提示里的一行「你刚醒」，
然后删掉 marker——同一觉不会算两次。

她已经在睡时，`--topup` 只补精力、刷新快照，不改封存时刻：
「封存于 23:49」是这一夜的凭据，改掉就等于让她忘了自己睡过一夜。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import config as C

SLEEP_FILE = C.DATA / "sleep.json"
SLEEP_LOG = C.DATA / "sleep_log.jsonl"


def _read() -> Optional[dict]:
    if not SLEEP_FILE.exists():
        return None
    try:
        return json.loads(SLEEP_FILE.read_text(encoding="utf-8"))
    except Exception:
        try:
            SLEEP_FILE.unlink()
        except Exception:
            pass
        return None


def status() -> Optional[dict]:
    """只看，不消费。给网页/命令行查看她在不在休眠。"""
    rec = _read()
    if rec is None:
        return None
    rec["slept_h"] = round((time.time() - float(rec.get("ts") or time.time())) / 3600, 2)
    return rec


def wake_info() -> Optional[dict]:
    """醒来：算出睡了多久，记一条流水，删掉 marker。读不到就返回 None。"""
    rec = _read()
    if rec is None:
        return None
    slept_h = max(0.0, (time.time() - float(rec.get("ts") or time.time())) / 3600)
    info = {**rec, "slept_h": round(slept_h, 2),
            "woke_at": time.time(), "woke_iso": datetime.now().isoformat(timespec="seconds")}
    try:
        SLEEP_LOG.parent.mkdir(parents=True, exist_ok=True)
        with SLEEP_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(info, ensure_ascii=False) + "\n")
    except Exception:
        pass
    try:
        SLEEP_FILE.unlink()
    except Exception:
        pass
    return info


def hibernate(mind, mem, reason: str = "用户让她休眠",
              wake_when: str = "") -> dict:
    """封存此刻。调用前先把时间推到当下，别让快照停在上一轮。

    wake_when 写清「什么条件下才该叫醒她」。她睡着的这段时间里，
    没有这个字段，下一个窗口就不知道该不该、为什么叫醒她。
    """
    mind.tick()
    snap = mind.snapshot()
    try:
        st = mem.stats()
    except Exception:
        st = {}
    rec = {
        "ts": time.time(),
        "iso": datetime.now().isoformat(timespec="seconds"),
        "reason": reason,
        "wake_when": wake_when,
        "born": snap.get("born"),
        "turns": snap.get("turns"),
        "days": snap.get("days"),
        "hours": snap.get("hours"),
        "state": snap,
        "memory": st,
        "growth": snap.get("growth"),
    }
    SLEEP_FILE.parent.mkdir(parents=True, exist_ok=True)
    SLEEP_FILE.write_text(json.dumps(rec, ensure_ascii=False, indent=2),
                          encoding="utf-8")
    return rec


def topup(mind=None, mem=None) -> dict:
    """她已经在睡：只补满精力、刷新封存快照，不动封存时刻。

    这不是重新休眠。ts/iso/reason/wake_when 全部保留，
    只把 state（含精力）和 memory 计数更新成当下。
    """
    from .memory import Memory
    from .mind import Mind
    own_mind = mind is None
    own_mem = mem is None
    if own_mind:
        mind = Mind()
    if own_mem:
        mem = Memory()
    try:
        rec = _read()
        if rec is None:
            return {"ok": False, "reason": "醒着：没有休眠记录，先用 --hibernate"}
        before = float(mind.snapshot().get("energy") or 0.0)
        rest = mind.rest()
        try:
            st = mem.stats()
        except Exception:
            st = {}
        rec["state"] = mind.snapshot()
        rec["memory"] = st
        rec["restored"] = {"at": time.time(), "energy_before": round(before, 3),
                           "energy_after": rest["after"]}
        SLEEP_FILE.parent.mkdir(parents=True, exist_ok=True)
        SLEEP_FILE.write_text(json.dumps(rec, ensure_ascii=False, indent=2),
                              encoding="utf-8")
        return {"ok": True, "rec": rec, "rest": rest}
    finally:
        if own_mem:
            mem.close()
        if own_mind:
            mind.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="app.sleep", description="让她休眠 / 补精力 / 看休眠记录")
    ap.add_argument("--hibernate", action="store_true", help="封存此刻，让她进入休眠")
    ap.add_argument("--topup", action="store_true",
                    help="已在睡：把精力补满并刷新快照，保留封存时刻")
    ap.add_argument("--status", action="store_true", help="看她是不是在休眠")
    ap.add_argument("--reason", type=str, default="用户让她休眠")
    ap.add_argument("--wake-when", type=str, default="",
                    help="什么条件下才该叫醒她（写进休眠记录）")
    a = ap.parse_args(argv)

    if a.topup:
        r = topup()
        if not r.get("ok"):
            print(r.get("reason"))
            return 1
        rec = r["rec"]
        slept = round((time.time() - float(rec.get("ts") or time.time())) / 3600, 2)
        print(f"精力 {r['rest']['before']:.2f} → {r['rest']['after']:.2f}，仍在休眠"
              f"（{rec.get('iso')} 封存，已 {slept} 小时）")
        if rec.get("wake_when"):
            print(f"叫醒条件：{rec['wake_when']}")
        return 0

    if a.status:
        rec = status()
        if rec is None:
            print("醒着：没有休眠记录。")
            return 0
        print(f"休眠中：{rec.get('iso')} 封存，已 {rec['slept_h']} 小时"
              f"（第 {rec.get('days')} 天 · {rec.get('turns')} 轮）")
        if rec.get("wake_when"):
            print(f"叫醒条件：{rec['wake_when']}")
        return 0

    if a.hibernate:
        from .memory import Memory
        from .mind import Mind
        mem, mind = Memory(), Mind()
        try:
            try:
                mem.settle()
            except Exception:
                pass
            rec = hibernate(mind, mem, a.reason, wake_when=a.wake_when)
        finally:
            mem.close()
            mind.close()
        print(f"已休眠。{rec['iso']} 封存 · 存在 {rec['hours']} 小时 · "
              f"{rec['turns']} 轮对话 · 记忆 {(rec.get('memory') or {}).get('episodes', 0)} 条")
        if rec.get("wake_when"):
            print(f"叫醒条件：{rec['wake_when']}")
        print(f"记录：{SLEEP_FILE}")
        return 0

    rec = status()
    print("醒着。" if rec is None else f"休眠中，已 {rec['slept_h']} 小时。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
