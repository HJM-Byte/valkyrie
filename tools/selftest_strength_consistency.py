"""P4 一致性自检：真实 turn 结算出来的强度，和识海读出来的是不是同一个数。

    python -m tools.selftest_strength_consistency

只写 data/acceptance/tmp/ 下的临时库；不打开生产 memory.db。
生产库的只读核对另跑，不放进本测试，避免测试脚本碰生产数据。
"""
from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from app.memory import Memory                      # noqa: E402
from app.sihai import build_from_db                # noqa: E402
from app import memory_strength as S               # noqa: E402
from tools.acceptance import fixtures              # noqa: E402

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


def _node(graph: dict, ep_id: int) -> dict:
    want = f"e{int(ep_id)}"
    return next((n for n in graph.get("nodes") or [] if n.get("id") == want), {})


def main() -> int:
    now = time.time()
    with fixtures.temp_workspace("p4_consistency_") as ws:
        mem = ws.open_memory()
        try:
            e_new = mem.add_episode("今天第一次把风车装反了", who="他", imp=0.8,
                                    ts=now - 2 * DAY, source="他打字说的")
            e_mid = mem.add_episode("上周答应一起去看海，后来没去", who="他", imp=0.9,
                                    ts=now - 14 * DAY, source="他打字说的")
            e_old = mem.add_episode("很久以前在旧房子门口等过一个人", who="我", imp=0.7,
                                    ts=now - 120 * DAY, source="我说的")
            check("三件事都从 0 强度起步",
                  all(mem.strength_of(e, now).get("effective") == 0 for e in
                      (e_new, e_mid, e_old)))

            print("— 只读路径 —")
            before = {e: int(mem.strength_of(e, now).get("count") or 0)
                      for e in (e_new, e_mid, e_old)}
            mem.search("风车装反", k=3)
            after = {e: int(mem.strength_of(e, now).get("count") or 0)
                     for e in (e_new, e_mid, e_old)}
            check("search 默认只读，不增加强化次数", before == after,
                  f"{before} -> {after}")

            print("— 真实召回后，内存/识海同源 —")
            hits = mem.search("风车装反", k=1, reinforce=True)
            check("真实召回命中", bool(hits), str(hits))
            mem_now = float(mem.strength_of(e_new, time.time()).get("effective") or 0)
            graph = build_from_db(ws.memory_db)
            node = _node(graph, e_new)
            check("识海节点能读到同一件事", node.get("id") == f"e{e_new}", str(node))
            check("识海 strength 与 Memory 结算一致",
                  abs(float(node.get("strength") or 0) - round(mem_now, 3)) <= 1e-9,
                  f"si={node.get('strength')} mem={mem_now}")
            check("识海 used 与库里一致",
                  int(node.get("used") or 0) == int(mem.strength_of(e_new, now).get("count") or 0),
                  f"si={node.get('used')} mem={mem.strength_of(e_new, now).get('count')}")
            check("真实召回结果带前后强度",
                  "strength_before" in (hits[0] if hits else {})
                  and "strength_next" in (hits[0] if hits else {}),
                  str(hits[0] if hits else None))

            print("— 年龄越大，单次强化越小 —")
            b_new = float(S.next_strength(now - 2 * DAY, 0.0, 0.0, now)["delta"])
            b_mid = float(S.next_strength(now - 14 * DAY, 0.0, 0.0, now)["delta"])
            b_old = float(S.next_strength(now - 120 * DAY, 0.0, 0.0, now)["delta"])
            check("2 天 > 14 天 > 120 天",
                  b_new > b_mid > b_old > 0.0,
                  f"{b_new:.6f} {b_mid:.6f} {b_old:.6f}")

            print("— 上次强化留下的影响会退 —")
            eff14 = S.effective_strength(0.90, now - 14 * DAY, now)
            check("14 天后上次强度只剩约 37%",
                  0.32 < eff14 < 0.34, str(eff14))
            check("上次强化时间越久，影响越小",
                  eff14 < S.effective_strength(0.90, now - 7 * DAY, now)
                  < 0.90, "")

            print("— 连续召回：不越上限，节点同步 —")
            seq = []
            for _ in range(10):
                mem.search("风车装反", k=1, reinforce=True)
                seq.append(round(float(mem.strength_of(e_new, time.time()).get("effective") or 0), 3))
                node_now = _node(build_from_db(ws.memory_db), e_new)
                seq[-1] = (seq[-1], float(node_now.get("strength") or 0))
            check("连续召回不越 1.0", max(x[0] for x in seq) <= 1.0, str(seq))
            check("内存与识海每一步都相等",
                  all(abs(a - b) <= 1e-9 for a, b in seq), str(seq))
            check("连续召回强度单调不降",
                  all(b[0] >= a[0] - 1e-9 for a, b in zip(seq, seq[1:])), str(seq))

            print("— 旧库兼容：不批量建行、低起点 —")
            old_path = ws.root / "old_schema4.db"
            db = sqlite3.connect(str(old_path))
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
            om = Memory(old_path)
            try:
                oid = int(om._db.execute("SELECT id FROM episodes LIMIT 1").fetchone()["id"])
                check("打开旧库不批量建强度行",
                      om.stats().get("memory_strength") == 0, str(om.stats()))
                ost = om.strength_of(oid, now)
                check("旧记录有无旁表行都能给低起点",
                      ost.get("ok") is True and 0.0 < float(ost.get("effective") or 0) <= 0.35,
                      str(ost))
                before_old = int(om._db.execute(
                    "SELECT used FROM episodes WHERE id=?", (oid,)).fetchone()["used"])
                om.search("旧事里的海边", k=1)
                check("只读旧记录不建行", om.stats().get("memory_strength") == 0,
                      str(om.stats()))
                om.search("旧事里的海边", k=1, reinforce=True)
                check("旧记录真实召回后才落一行",
                      om.stats().get("memory_strength") == 1, str(om.stats()))
                row = dict(om._db.execute(
                    "SELECT strength,reinforce_count,legacy_unknown "
                    "FROM memory_strength WHERE ep_id=?", (oid,)).fetchone())
                check("旧记录落行带 legacy_unknown、计数为 1",
                      int(row["legacy_unknown"]) == 1 and int(row["reinforce_count"]) == 1,
                      str(row))
                check("旧记录真实召回 used 也只加一次",
                      int(om._db.execute(
                          "SELECT used FROM episodes WHERE id=?", (oid,)
                      ).fetchone()["used"]) == before_old + 1)
            finally:
                om.close()

            print("— 只读页与真实页不互相写 —")
            g1 = build_from_db(ws.memory_db)
            g2 = build_from_db(ws.memory_db)
            check("连续两次读识海，图节点强度不变",
                  [n.get("strength") for n in g1["nodes"]] ==
                  [n.get("strength") for n in g2["nodes"]], "")
            check("识海读图不增加强化次数",
                  mem.stats().get("memory_strength") == 3, str(mem.stats()))
        finally:
            mem.close()

    print(f"\n{PASS} 通过 / {FAIL} 失败")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
