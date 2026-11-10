"""P4 记忆强化自检：召回才强化，年龄越大加得越少，影响会退。

    python -m tools.selftest_memory_strength

全部使用 data/acceptance/tmp/ 下的临时库，不打开生产 memory.db。
"""
from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from app import memory_strength as S                  # noqa: E402
from app.memory import Memory, detail_now             # noqa: E402
from app.think import think                           # noqa: E402
from tools.acceptance import fixtures                 # noqa: E402

PASS = 0
FAIL = 0
DAY = 86400.0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def _state(now: float) -> dict:
    return {
        "energy": 0.55, "mood": -0.10, "arousal": 0.42,
        "lonely": 0.18, "silent_h": 0.10, "gap_shock": 0.0,
        "feel": {"平静": 0.60}, "rel": {"熟悉": 0.40, "信任": 0.30},
        "desire_effect": {}, "temper": {},
    }


def _old_schema(path: Path) -> None:
    """造一个 schema 4 的旧库：有 episodes，没有 memory_strength 旁表。"""
    db = sqlite3.connect(str(path))
    db.executescript(
        """
        PRAGMA user_version=4;
        CREATE TABLE turns(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL, day TEXT NOT NULL,
            role TEXT NOT NULL, text TEXT NOT NULL
        );
        CREATE TABLE facts(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT NOT NULL UNIQUE, value TEXT NOT NULL,
            conf REAL NOT NULL DEFAULT 0.7, ts REAL NOT NULL,
            updated REAL NOT NULL, source TEXT DEFAULT ''
        );
        CREATE TABLE episodes(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL, day TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'event',
            who TEXT DEFAULT '', text TEXT NOT NULL,
            imp REAL NOT NULL DEFAULT 0.5,
            used INTEGER NOT NULL DEFAULT 0,
            last_used REAL DEFAULT 0
        );
        INSERT INTO episodes(ts,day,kind,who,text,imp,used,last_used)
            VALUES(1000,'1970-01-01','talk','他','旧事里的海边',0.8,8,0);
        """
    )
    db.commit()
    db.close()


def main() -> int:
    now = time.time()
    with fixtures.temp_workspace("p4_strength_") as ws:
        mem = ws.open_memory()
        try:
            print("— schema 与新记忆 —")
            cols = {r["name"] for r in mem._db.execute("PRAGMA table_info(episodes)")}
            tables = {r["name"] for r in mem._db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            check("schema 升到 8", mem.stats().get("schema_version") == 8,
                  str(mem.stats()))
            check("memory_strength 表存在", "memory_strength" in tables, str(tables))
            check("旧 episodes 仍保留 used/last_used",
                  {"used", "last_used"} <= cols, str(sorted(cols)))

            eid = mem.add_episode("他答应周末一起去看海", who="他", imp=0.9,
                                  ts=now - 3600)
            st0 = mem.strength_of(eid, now)
            check("新记忆从 0 强度起步",
                  st0.get("ok") is True and float(st0.get("effective") or 0) == 0.0,
                  str(st0))
            check("stats 报出强化行数",
                  mem.stats().get("memory_strength") == 1, str(mem.stats()))

            row = dict(mem._db.execute(
                "SELECT used,recon FROM episodes WHERE id=?", (eid,)).fetchone())
            before_used, before_recon = int(row["used"]), float(row["recon"])
            print("— 只读预览不强化 —")
            for _ in range(5):
                mem.search("他答应周末一起去看海", k=3)
            ro = dict(mem._db.execute(
                "SELECT used,recon FROM episodes WHERE id=?", (eid,)).fetchone())
            check("只读 search 不增加 used", int(ro["used"]) == before_used,
                  f"{before_used} -> {ro['used']}")
            check("只读 search 不增加强度",
                  float(mem.strength_of(eid, now).get("effective") or 0) == 0.0,
                  str(mem.strength_of(eid, now)))
            check("只读 search 不改 recon", float(ro["recon"]) == before_recon,
                  f"{before_recon} -> {ro['recon']}")

            st = _state(now)
            before_state = dict(st)
            think("他答应周末一起去看海", mem=mem, state=st, now=now)
            check("think() 默认不强化 used",
                  int(mem._db.execute(
                      "SELECT used FROM episodes WHERE id=?", (eid,)
                  ).fetchone()["used"]) == before_used)
            check("think() 默认不改 state", st == before_state, str(st))
            import inspect
            from app.chat import Chat
            check("真实 turn 才传 reinforce=True",
                  "reinforce=True" in inspect.getsource(Chat._begin_turn))
            check("Chat.compose 默认不强化",
                  inspect.signature(Chat.compose).parameters["reinforce"].default is False)
            try:
                from app.pipeline import Pipeline
            except ModuleNotFoundError as exc:
                print(f"  SKIP  语音 turn 接线检查（缺可选依赖 {exc.name}）")
            else:
                check("语音 turn 也传 reinforce=True",
                      "reinforce=True" in inspect.getsource(Pipeline.one_turn))

            print("— 两条衰减 —")
            d0 = S.reinforcement_delta(now, 0.0, now)
            d7 = S.reinforcement_delta(now - 7 * DAY, 0.0, now)
            d30 = S.reinforcement_delta(now - 30 * DAY, 0.0, now)
            d180 = S.reinforcement_delta(now - 180 * DAY, 0.0, now)
            check("年龄越大，单次强化越小",
                  d0 > d7 > d30 > d180 > 0.0,
                  f"{d0:.6f} {d7:.6f} {d30:.6f} {d180:.6f}")
            eff14 = S.effective_strength(0.80, now - 14 * DAY, now)
            check("上次强化的影响会随时间退",
                  eff14 < 0.80 and 0.28 < eff14 < 0.31, str(eff14))
            check("没有上次强化时间时保留起点",
                  S.effective_strength(0.25, 0.0, now) == 0.25)
            cap = S.next_strength(now, 0.99, now, now)
            check("单次强化不越过上限", float(cap["after"]) <= S.BMAX, str(cap))

            print("— 真实召回才结算 —")
            h = mem.search("他答应周末一起去看海", k=1, reinforce=True)
            after_used = int(mem._db.execute(
                "SELECT used FROM episodes WHERE id=?", (eid,)).fetchone()["used"])
            after_st = mem.strength_of(eid, now)
            check("真实 recall 增加 used",
                  after_used == before_used + 1, f"{before_used} -> {after_used}")
            check("真实 recall 产生强度",
                  float(after_st.get("effective") or 0) > 0.0, str(after_st))
            check("召回结果带 strength_before/next",
                  bool(h) and "strength_before" in h[0] and "strength_next" in h[0],
                  str(h[0].get("strength_before") if h else None))
            check("强化不抬高 recon",
                  float(mem._db.execute(
                      "SELECT recon FROM episodes WHERE id=?", (eid,)
                  ).fetchone()["recon"]) <= before_recon + 1e-9)

            print("— 连续召回逼近上限 —")
            seq = []
            for _ in range(10):
                mem.search("他答应周末一起去看海", k=1, reinforce=True)
                seq.append(float(mem.strength_of(eid, now).get("effective") or 0))
            check("连续召回不越上限", max(seq) <= 1.0 and seq[-1] <= 1.0, str(seq))
            check("连续召回强度单调不降",
                  all(b >= a - 1e-9 for a, b in zip(seq, seq[1:])), str(seq))
            check("used 只计真实召回次数",
                  int(mem._db.execute(
                      "SELECT used FROM episodes WHERE id=?", (eid,)
                  ).fetchone()["used"]) == before_used + 11,
                  f"{before_used} + 11")

            print("— 旧记录：低起点、不迁移 —")
            old = ws.root / "old_schema4.db"
            _old_schema(old)
            om = Memory(old)
            try:
                old_id = int(om._db.execute(
                    "SELECT id FROM episodes LIMIT 1").fetchone()["id"])
                ost = om.strength_of(old_id, now)
                check("旧记录没有旁表行也能给低起点",
                      ost.get("ok") is True and 0.0 < float(ost["effective"]) <= 0.35,
                      str(ost))
                check("旧记录标记为 legacy", int(ost.get("legacy") or 0) == 1, str(ost))
                check("打开旧库不为旧行批量建强度行",
                      om.stats().get("memory_strength") == 0, str(om.stats()))
                om.search("旧事里的海边", k=1)
                check("只读旧记录也不建强度行",
                      om.stats().get("memory_strength") == 0, str(om.stats()))
                om.search("旧事里的海边", k=1, reinforce=True)
                orow = dict(om._db.execute(
                    "SELECT strength,reinforce_count,legacy_unknown "
                    "FROM memory_strength WHERE ep_id=?", (old_id,)).fetchone())
                check("旧记录真实召回后才落一行",
                      om.stats().get("memory_strength") == 1, str(om.stats()))
                check("旧记录强化行带 legacy_unknown",
                      int(orow["legacy_unknown"]) == 1 and int(orow["reinforce_count"]) == 1,
                      str(orow))
            finally:
                om.close()

            print("— 强度对细节保持的影响与兼容 —")
            d_hi = detail_now(1.0, 365, 0.8, 0, 0.5, 0.0, strength=0.9)
            d_lo = detail_now(1.0, 365, 0.8, 0, 0.5, 0.0, strength=0.0)
            check("强记忆的细节留得更多", d_hi > d_lo, f"{d_hi} vs {d_lo}")
            check("旧 detail_now(used=...) 仍可用",
                  detail_now(1.0, 365, 0.8, 4, 0.5, 0.0) >= 0.0)

            print("— forget 清理旁表 —")
            mem.forget("答应周末")
            check("forget 同时删掉强度行",
                  mem.stats().get("memory_strength") == 0, str(mem.stats()))
        finally:
            mem.close()

    print(f"\n{PASS} 通过 / {FAIL} 失败")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
