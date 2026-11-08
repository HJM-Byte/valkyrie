"""A4.3 自检：用真实记忆的副本跑一轮 Chat，确认真实库一个字节都不动。

为什么不用真实库直接跑：
- `Chat.turn()` 会写 turn、episode、flow、mind，还会认领 sleep；
- 自检只该验证「真实记忆接进来以后，提示词/人格/召回对不对」，
  不该往真实识海里塞测试句。

做法：
1. 用 sqlite backup 拷一份真实 memory.db / learn.db，拷 flow.json；
2. 把 sleep / Dispatch 足迹 / Learning 路径全部改到临时目录；
3. 用假的嘴（FakeLLM）跑一轮「你还记得我是谁吗？」；
4. 核对提示词带人格、临时库多一条 episode、真实文件哈希不变。

    python tools/selftest_body_live.py
"""
from __future__ import annotations

import hashlib
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
sys.path.insert(0, str(ROOT))

OK = 0
FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global OK, FAIL
    if ok:
        OK += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {detail}")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def copy_sqlite(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    s = sqlite3.connect(str(src))
    try:
        d = sqlite3.connect(str(dst))
        try:
            s.backup(d)
        finally:
            d.close()
    finally:
        s.close()


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="valkyrie_body_live_"))
    print(f"临时目录：{tmp}")
    try:
        from app import learning as _learning
        from app import sleep as _sleep
        from app import dispatch as _dispatch
        from app.chat import Chat
        from app.dispatch import Dispatch
        from app.flow import Flow
        from app.learning import Learning
        from app.memory import Memory
        from app.mind import Mind

        # 1) 临时副本
        copy_sqlite(DATA / "memory.db", tmp / "memory.db")
        copy_sqlite(DATA / "learning" / "learn.db", tmp / "learning" / "learn.db")
        shutil.copy2(DATA / "flow.json", tmp / "flow.json")

        # 2) 所有可能写真实状态的模块级路径，先改到临时目录
        _sleep.SLEEP_FILE = tmp / "sleep.json"
        _sleep.SLEEP_LOG = tmp / "sleep_log.jsonl"
        _dispatch.FOOTPRINT = tmp / "sihai_footprint.jsonl"
        _learning.ROOT = tmp / "learning"
        _learning.DB_PATH = _learning.ROOT / "learn.db"
        _learning.BOOKS = _learning.ROOT / "books"
        _learning.CHANNELS_FILE = _learning.ROOT / "channels.json"
        _learning.FOOTPRINT = _learning.ROOT / "footprint.jsonl"
        _learning.BILI_QUEUE = _learning.ROOT / "bili_queue.json"
        _learning.BOOKS.mkdir(parents=True, exist_ok=True)
        shutil.copy2(DATA / "learning" / "channels.json", _learning.CHANNELS_FILE)

        # 3) 真实文件的哈希（跑完必须一致）
        real_files = [
            DATA / "memory.db",
            DATA / "flow.json",
            DATA / "sihai_footprint.jsonl",
            DATA / "learning" / "learn.db",
            DATA / "learning" / "footprint.jsonl",
            ROOT / "PERSONA.md",
        ]
        before = {str(p): sha256(p) for p in real_files if p.exists()}

        class Silent:
            def voice_note_text(self, limit: int = 3) -> str:
                return ""

            def brief(self):
                return None

            def maybe_study(self) -> None:
                return None

            def status(self):
                return {}

            def close(self) -> None:
                pass

        class FakeLLM:
            def __init__(self) -> None:
                self.systems: list[str] = []

            def chat(self, text, image_b64=None, system=None):
                self.systems.append(system or "")
                return {"text": "嗯，我在。", "seconds": 0.01}

        mem = Memory(tmp / "memory.db")
        mind = Mind(tmp / "memory.db")
        flow = Flow(tmp / "flow.json")
        learning = Learning(tmp / "learning" / "learn.db")
        fake = FakeLLM()
        chat = Chat(mem=mem, mind=mind, flow=flow, llm=fake)
        chat.dispatch = Dispatch(mem=mem, learning=learning)
        chat._school = Silent()        # type: ignore[assignment]
        chat._autonomy = Silent()      # type: ignore[assignment]
        chat._housekept = True

        before_eps = mem.stats()["episodes"]
        t0 = time.time()
        r = chat.turn("你还记得我是谁吗？", channel="voice")
        elapsed = time.time() - t0

        system = fake.systems[0] if fake.systems else ""
        check("一轮：成功", r.get("ok") is True, str(r.get("error")))
        check("一轮：有回复", bool(r.get("reply")))
        check("提示词：带人格", "瓦尔基里" in system, system[:200])
        check("提示词：长度像真的", len(system) > 800, f"{len(system)} 字")
        check("记忆：临时库多一条 episode",
              r["memory"]["episodes"] == before_eps + 1,
              f"{before_eps} -> {r['memory']['episodes']}")
        check("记忆：调度有候选", int((r.get("dispatch") or {}).get("candidates") or 0) > 0,
              str(r.get("dispatch")))
        print(f"  轮次耗时 {elapsed:.2f}s，提示词 {len(system)} 字，"
              f"episodes {r['memory']['episodes']}")

        # 身体入口必须是独立来源，不能沿用手机的「他用手机说的」
        body_text = "这句话是从身体麦克风进来的吗？"
        t1 = time.time()
        rb = chat.turn(body_text, channel="body")
        body_elapsed = time.time() - t1
        body_system = fake.systems[-1] if fake.systems else ""
        check("身体入口：成功", rb.get("ok") is True, str(rb.get("error")))
        check("身体入口：提示词标明身体麦克风",
              "身体上的麦克风" in body_system, body_system[-300:])
        check("身体入口：不再说屏幕/手机",
              "屏幕上" not in body_system and "手机麦克风" not in body_system)
        check("身体入口：临时库再多一条 episode",
              rb["memory"]["episodes"] == before_eps + 2,
              f"{before_eps} -> {rb['memory']['episodes']}")
        with sqlite3.connect(str(tmp / "memory.db")) as db:
            row = db.execute(
                "SELECT source FROM episodes WHERE text=? ORDER BY id DESC LIMIT 1",
                (body_text,)).fetchone()
        check("身体入口：来源记为身体",
              bool(row) and row[0] == "他对着机器人说的", str(row))
        print(f"  身体入口耗时 {body_elapsed:.2f}s，来源 {row[0] if row else 'MISSING'}")

        chat.close()

        # 4) 真实文件必须一个字节没变
        after = {str(p): sha256(p) for p in real_files if p.exists()}
        changed = [p for p in before if before[p] != after.get(p)]
        check("真实状态：哈希不变", not changed, str(changed))
        for p in before:
            print(f"  {Path(p).name:28s} {before[p][:16]}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{OK} ok / {FAIL} fail")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
