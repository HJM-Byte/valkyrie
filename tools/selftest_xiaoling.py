"""自检：二读小凌之后补的四条机制。

    1 愿望 ≠ 事实（claim 类型 + 提示里的硬闸门）
    2 低风险情绪场景只短接话，不追问
    3 记忆生命周期 settle（完整经历 → 情景记忆 → 意义残留）
    4 被回忆次数影响可及性（R 项）

不碰真库、不联网。跑法：python -m tools.selftest_xiaoling
"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

OK = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def main() -> int:
    from app import memory as M
    from app import think as T

    print("— 愿望不是事实 —")
    check("「想去黄山」判成愿望", M.claim_of("我想去黄山") == "wish",
          M.claim_of("我想去黄山"))
    check("「他说他明天来」判成转述", M.claim_of("他说他明天会来") == "quote",
          M.claim_of("他说他明天会来"))
    check("「可能是周三」判成推测", M.claim_of("可能是周三吧") == "guess",
          M.claim_of("可能是周三吧"))
    check("「今天吃了面」还是事实", M.claim_of("今天中午吃了面") == "fact",
          M.claim_of("今天中午吃了面"))

    th = T.think("我想去黄山", state=T._empty_state())
    check("愿望句进提示前被标出来",
          "愿望" in th["decision"]["rule"], th["decision"]["rule"][:60])

    th2 = T.think("我好累啊，今天刚加班回来", state=T._empty_state())
    check("累了 → 短接话而不是追问",
          th2["decision"]["key"] == "comfort", th2["decision"]["name"])
    check("短接话规则里禁止追问原因",
          "不要问原因" in th2["decision"]["rule"])
    th3 = T.think("我今天面试没过，有点难受", state=T._empty_state())
    check("普通诉苦仍然走接住他",
          th3["decision"]["key"] == "attend", th3["decision"]["name"])

    print("— 记忆归属与生命周期 —")
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "m.db"
        mem = M.Memory(path=db)
        try:
            old = time.time() - 1200 * 86400      # 三年多，imp 0.4 → 该落到 meaning
            mem.add_episode("我想去黄山看雪", kind="talk", who="他",
                            imp=0.4, ts=old)
            mem.add_episode("今天中午吃了面", kind="talk", who="他",
                            imp=0.1, ts=time.time() - 60 * 86400)
            blk = mem.episodes_block("黄山", k=3)
            check("愿望记忆带着「这是他的愿望」", "愿望" in blk, blk[:80])

            res = mem.settle(keep_days=3)
            check("settle 有推进", res["moved"] >= 1, str(res))
            stage = mem._db.execute(
                "SELECT stage FROM episodes WHERE text LIKE '%黄山%'").fetchone()["stage"]
            check("三年前的愿望落到 meaning", stage == "meaning", stage)
            seg = mem._db.execute(
                "SELECT stage FROM episodes WHERE text LIKE '%吃了面%'").fetchone()["stage"]
            check("六十天的不重要经历落到 segment", seg == "segment", seg)
            blk2 = mem.episodes_block("黄山", k=3)
            check("老记忆说话留余地",
                  ("只记得当时的感觉" in blk2) or ("只剩几个片段" in blk2)
                  or ("细节记不太清了" in blk2), blk2[:100])

            # R 项：被回忆过的更容易再被想起（只加一点点）
            mem._db.execute("UPDATE episodes SET used=9 WHERE text LIKE '%吃%'")
            mem._db.commit()
            mem._cache = None
            hit_new = mem.search("中午吃了什么", k=2)
            check("召回带出被回忆过的旧事", bool(hit_new), str(hit_new)[:80])
            check("可及性加成很小（act 不超过 1.75）",
                  all(h["act"] <= 1.75 for h in hit_new))
        finally:
            mem.close()

    print(f"\n{OK} 通过 / {FAIL} 失败")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
