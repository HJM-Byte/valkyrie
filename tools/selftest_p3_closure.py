"""P3 收口自检：只读边界、旧库兼容与反馈撤回边界。

    python -m tools.selftest_p3_closure

这不是新的情感功能，而是 P3 4.4–4.7 的出门复核：

- 4.4 三类情感对象只读，且不写库、不改 state；
- 4.5 回答前分寸只改表达，不碰召回、竞争、人格和关系变量；
- 4.6 反馈收据只在下一次真实 turn 结算，只读预览不消费；
- 4.7 撤回只改 feedback_events，旧库能升到 schema 4 且不丢旧数据。

全部数据库都在 data/acceptance/tmp/ 下，不打开生产库。
"""
from __future__ import annotations

import copy
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from app.appraisal import evaluate  # noqa: E402
from app.memory import Memory  # noqa: E402
from app.persona import Persona  # noqa: E402
from app.think import think  # noqa: E402
from tools.acceptance import fixtures  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def state() -> dict:
    return {
        "energy": 0.55, "mood": -0.25, "arousal": 0.45,
        "lonely": 0.20, "silent_h": 0.10, "gap_shock": 0.0,
        "feel": {"低落": 0.50, "平静": 0.30},
        "rel": {"熟悉": 0.40, "信任": 0.30, "边界": 0.80},
        "desire_effect": {}, "temper": {},
    }


def _old_schema_db(path: Path) -> None:
    """造一个 schema 2 的小库：没有 feedback_events，也没有新 episodes 列。"""
    db = sqlite3.connect(str(path))
    db.executescript(
        """
        PRAGMA user_version=2;
        CREATE TABLE turns(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL, day TEXT NOT NULL,
            role TEXT NOT NULL, text TEXT NOT NULL
        );
        CREATE TABLE facts(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT NOT NULL UNIQUE, value TEXT NOT NULL,
            conf REAL NOT NULL DEFAULT 0.7,
            ts REAL NOT NULL, updated REAL NOT NULL,
            source TEXT DEFAULT ''
        );
        CREATE TABLE episodes(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL, day TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'event',
            who TEXT DEFAULT '', text TEXT NOT NULL,
            imp REAL NOT NULL DEFAULT 0.5
        );
        INSERT INTO turns(ts,day,role,text)
            VALUES(1000,'1970-01-01','user','旧轮次');
        INSERT INTO facts(key,value,conf,ts,updated,source)
            VALUES('旧称呼','他',0.7,1000,1000,'旧库');
        INSERT INTO episodes(ts,day,kind,who,text,imp)
            VALUES(1000,'1970-01-01','talk','他','旧经历',0.5);
        """
    )
    db.commit()
    db.close()


def main() -> int:
    st = state()
    before = copy.deepcopy(st)
    now = 1000.0

    # ---- 4.4 三类对象只读 ------------------------------------------------
    res = evaluate("我今天有点累", st, now=now)
    user = res.get("user_emotion") or {}
    self_state = res.get("self_state") or {}
    rel = res.get("relationship_appraisal") or {}
    check("用户情绪是假设，不是事实", user.get("is_fact") is False, user)
    check("她的状态不冒充用户情绪",
          self_state.get("is_user_emotion") is False, self_state)
    check("关系只是一次候选",
          rel.get("candidate") is True
          and rel.get("permanent_change") is False
          and rel.get("changes_relationship_variables") is False, rel)
    check("评价声明只读且不写库",
          res.get("read_only") is True and res.get("writes") == [], res)
    check("评价不改 state", st == before, st)

    # ---- 4.5 只改表达 ----------------------------------------------------
    reg = res.get("regulation") or {}
    check("回答前分寸权限是 expression_only",
          reg.get("permission") == "expression_only"
          and reg.get("changes_retrieval") is False
          and reg.get("changes_personality") is False
          and reg.get("changes_relationship_variables") is False, reg)

    th = think("我有点累", state=st, now=now)
    pre = th.get("pre_answer") or {}
    check("think 把理解接到回答前",
          pre.get("applied") is True and pre.get("strategy") == "understand", pre)
    check("回答前仍只写表达范围",
          pre.get("change_scope") == ["expression"]
          and pre.get("changes_retrieval") is False, pre)

    prompt = Persona().compose("我想静静",
                               thought=think("我想静静", state=st, now=now),
                               channel="text")
    check("分寸指令进提示词且不改事实层",
          "【回答前】" in prompt and "不改事实" in prompt, prompt[-300:])

    # 同一句话，带反馈与不带反馈，召回和竞争必须一致。
    # 两个独立临时库，避免 recall 自己累计的 used 把第二次读数抬高。
    with fixtures.temp_workspace("p3_closure_think_") as ws:
        mem_a = ws.open_memory()
        mem_b = Memory(ws.root / "memory_b.db")
        ws._open.append(mem_b)
        for mem in (mem_a, mem_b):
            for i, text in enumerate(("他上周说过喜欢安静", "今天他有点累",
                                      "他喜欢在晚上聊天")):
                mem.add_episode(text, who="他", source="P3 收口自检",
                                ts=900.0 + i)
        base = think("我有点累", mem=mem_a, state=st, now=now)
        with_fb = think(
            "我有点累", mem=mem_b, state=st, now=now,
            feedback={"last_strategy": "understand", "last_action": "rejected",
                      "stats": {"count": 1, "rejected": 1}})
        check("反馈不改召回命中",
              base["memory"] == with_fb["memory"], base["memory"])
        check("反馈不改竞争结果",
              base["compete"] == with_fb["compete"], base["compete"])
        check("反馈不改关系评价",
              base["appraisal_three"]["relationship_appraisal"]
              == with_fb["appraisal_three"]["relationship_appraisal"],
              base["appraisal_three"]["relationship_appraisal"])
        check("多次 think 后 state 仍不变", st == before, st)

    # ---- 4.6 只读预览不消费收据 ------------------------------------------
    with fixtures.temp_workspace("p3_closure_preview_") as ws:
        mem = ws.open_memory()
        offer = mem.offer_pre_answer("respect_ambiguous", 7, "只确认一次",
                                     note="P3 收口自检")
        first = mem.pre_answer_feedback_history()
        mem.pre_answer_feedback_stats("respect_ambiguous")
        second = mem.pre_answer_feedback_history()
        check("只读预览留下待反馈收据",
              offer.get("ok") is True and len(first.get("items") or []) == 1,
              first)
        check("只读预览不结算",
              (second.get("items") or [{}])[0].get("action") == "offered",
              second)

    # ---- 4.7 撤回边界 ----------------------------------------------------
    with fixtures.temp_workspace("p3_closure_retract_") as ws:
        mem = ws.open_memory()
        mem.add_fact("称呼", "他", source="P3 收口自检")
        before_stats = mem.stats()
        offer = mem.offer_pre_answer("respect_ambiguous", 8, "只确认一次",
                                     note="P3 收口自检")
        resolved = mem.resolve_pre_answer_feedback("我没事，别问了")
        item = (mem.pre_answer_feedback_history().get("items") or [{}])[0]
        out = mem.retract_pre_answer_feedback(item.get("id") or "", "收口测试")
        after_stats = mem.stats()
        hist = mem.pre_answer_feedback_history()
        check("下一句话结算为拒绝",
              resolved.get("outcome") == "rejected", resolved)
        check("撤回成功且只退一次",
              out.get("ok") is True
              and (hist.get("stats") or {}).get("count") == 0, out)
        check("撤回保留原判",
              "原判" in str((hist.get("items") or [{}])[0].get("reason") or ""),
              hist)
        check("撤回不动记忆、事实和轮次",
              before_stats.get("episodes") == after_stats.get("episodes")
              and before_stats.get("facts") == after_stats.get("facts")
              and before_stats.get("turns") == after_stats.get("turns"),
              {"before": before_stats, "after": after_stats})

    # ---- 旧库兼容：schema 2 -> 4 不丢旧数据 ------------------------------
    with fixtures.temp_workspace("p3_closure_old_") as ws:
        old = ws.root / "old_schema.db"
        _old_schema_db(old)
        mem = Memory(old)
        stats = mem.stats()
        tables = {
            r["name"] for r in mem._db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")
        }
        version = int(mem._db.execute("PRAGMA user_version").fetchone()[0])
        mem.close()
        check("旧库升级保留旧行",
              stats.get("episodes") == 1 and stats.get("turns") == 1
              and stats.get("facts") == 1, stats)
        check("旧库补出 feedback_events",
              "feedback_events" in tables, tables)
        check("旧库补出 memory_strength",
              "memory_strength" in tables, tables)
        check("旧库版本升到 8", version == 8 and stats.get("schema_version") == 8,
              {"version": version, "stats": stats})

    print(f"\n{PASS} 通过 / {FAIL} 失败")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
