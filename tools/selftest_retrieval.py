"""自检：识海分区检索（第一步 + 第二步）。

不碰真库、不联网。跑法：
    python -m tools.selftest_retrieval

覆盖：
    region 列、episode_grams 倒排表、旧库迁移；
    先 SQL 取候选再算 R，search() 不再调用 _load_cache；
    四个存储区的规则路由、最多两个区、候选上限 200、无命中回退最近 20；
    think.recall 接路由，且没有 route_regions 的旧桩仍能工作；
    星图节点/面板元数据带 region，但不改变原有布局。
"""
from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from app import memory as M                       # noqa: E402
from app import sihai                             # noqa: E402
from app import think as T                        # noqa: E402

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


class _MemStub:
    """旧调度桩：只有 search()，没有 route_regions()。"""

    def search(self, *args, **kwargs):
        return [{"id": 1, "text": "旧的记忆", "emo": "平静", "emo_i": 0.1,
                 "act": 0.3, "q_recon": 1.0, "detail_now": 1.0,
                 "recon": 1.0, "stage": "raw", "claim": "fact", "day": "2026-01-01"}]


def _tmp(name: str) -> Path:
    """DSH 沙箱里 tempfile 目录 ACL 会拒绝 SQLite；统一放到项目 data/_tmp。"""
    base = ROOT / "data" / "_tmp" / f"{name}_{int(time.time() * 1000)}"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _open(path: Path) -> M.Memory:
    return M.Memory(path)


def test_schema_and_write() -> None:
    print("— 存储：region + 倒排表 + 老库迁移 —")
    td = _tmp("retrieval_schema")
    db = td / "m.db"
    mem = _open(db)
    try:
        cols = {r["name"] for r in mem._db.execute("PRAGMA table_info(episodes)")}
        check("episodes 有 region 列", "region" in cols, str(sorted(cols)))
        tables = {r["name"] for r in mem._db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        check("episode_grams 表存在", "episode_grams" in tables, str(tables))
        e1 = mem.add_episode("他答应周末一起去看海", who="他", imp=0.9,
                             region="relation")
        check("写入时指定区", _region_of(mem, e1) == "relation",
              _region_of(mem, e1))
        ngrams = mem._db.execute(
            "SELECT COUNT(*) c FROM episode_grams WHERE ep_id=?", (e1,)
        ).fetchone()["c"]
        check("写入时同步写倒排表", ngrams >= 4, str(ngrams))
        check("写入时同步维护词频",
              mem._query_df(["摄像", "答应"]).get("答应", 0) >= 1,
              str(mem._query_df(["摄像", "答应"])))
        e2 = mem.add_episode("今天中午吃了面", imp=0.2)
        check("默认落到经历区", _region_of(mem, e2) == "experience",
              _region_of(mem, e2))
        e3 = mem.add_episode("知识区的记忆", imp=0.3, region="知识")
        check("中文区名归一", _region_of(mem, e3) == "knowledge",
              _region_of(mem, e3))
        e4 = mem.add_episode("不认识的分区", imp=0.3, region="不存在")
        check("未知区名回退经历区", _region_of(mem, e4) == "experience",
              _region_of(mem, e4))
        stats = mem.stats()
        check("stats 带 regions/grams/schema_version",
              isinstance(stats.get("regions"), dict)
              and stats.get("grams", 0) > 0 and stats.get("schema_version") == 8,
              str(stats))
    finally:
        mem.close()

    # 模拟旧库：删掉 region 和倒排表，user_version 退回 0，再打开应自动迁移
    db2 = td / "old.db"
    old = _open(db2)
    try:
        old.add_episode("旧库里的海边记忆", who="他", imp=0.8)
    finally:
        old.close()
    raw = sqlite3.connect(db2)
    raw.execute("DROP INDEX IF EXISTS ix_ep_region_ts")
    raw.execute("DROP INDEX IF EXISTS ix_ep_region_who_ts")
    raw.execute("DROP TABLE episode_grams")
    raw.execute("ALTER TABLE episodes DROP COLUMN region")
    raw.execute("PRAGMA user_version=0")
    raw.commit()
    raw.close()
    migrated = _open(db2)
    try:
        check("旧库补出 region（默认经历区）",
              all(r["region"] == "experience" for r in migrated._db.execute(
                  "SELECT region FROM episodes")),
              str([dict(r) for r in migrated._db.execute("SELECT region FROM episodes")]))
        check("旧库补出倒排表",
              migrated.stats()["grams"] > 0, str(migrated.stats()["grams"]))
        check("旧库迁移后能检索",
              any(h["text"] == "旧库里的海边记忆" for h in migrated.search("海边")),
              str([h["text"] for h in migrated.search("海边")]))
        check("迁移只跑一次（user_version=8）",
              migrated.stats()["schema_version"] == 8, str(migrated.stats()))
    finally:
        migrated.close()


def test_search_candidates() -> None:
    print("— 检索：SQL 取候选，R 公式不变 —")
    td = _tmp("retrieval_search")
    mem = _open(td / "m.db")
    try:
        now = time.time()
        hit = mem.add_episode("他答应周末一起去看海", who="他", imp=0.9,
                              rel=0.9, emo="开心", emo_i=0.8,
                              ts=now - 3600)
        mem._db.execute("UPDATE episodes SET used=2 WHERE id=?", (hit,))
        mem._db.commit()
        old_hit = mem.add_episode("很久以前我们去看过海", who="他", imp=0.7,
                                  ts=now - 200 * DAY)
        high = mem.add_episode("今天调通了摄像头", imp=0.85, ts=now - 600)
        irrelevant = mem.add_episode("今天中午吃了面", imp=0.2, ts=now - 1200)
        retracted = mem.add_episode("这是一条要撤回的约定", imp=0.9, ts=now - 300)
        imprint = mem.add_episode("只剩痕迹的旧事", imp=0.95, emo_i=0.9,
                                  ts=now - 3000 * DAY)
        mem.retract(retracted, reason="自检")
        mem._db.execute("UPDATE episodes SET stage='imprint' WHERE id=?", (imprint,))
        mem._db.commit()
        mem._cache = None

        # search 不能再走 _load_cache；把入口打断，验证它仍然能工作
        original = mem._load_cache
        mem._load_cache = lambda: (_ for _ in ()).throw(
            AssertionError("search 不应该再调用 _load_cache"))
        hits = mem.search("他答应周末一起去看海", k=5)
        mem._load_cache = original
        texts = [h["text"] for h in hits]
        check("不靠全量缓存也能召回", "他答应周末一起去看海" in texts, str(texts))
        check("撤回的记忆不参与召回",
              "这是一条要撤回的约定" not in texts, str(texts))
        check("只剩痕迹的记忆不冒充文字记忆",
              "只剩痕迹的旧事" not in texts, str(texts))
        check("高意义无关键词的旧事仍可自己浮上来",
              "今天调通了摄像头" in texts, str(texts))
        check("召回结果带 region",
              all(h.get("region") == "experience" for h in hits),
              str([h.get("region") for h in hits]))
        check("召回仍带 R / Q_recon / 细节",
              {"R", "q_recon", "detail_now", "act"} <= set(hits[0]),
              str(sorted(hits[0].keys())))

        # max_scan：旧的检索只看最近 4000 条；这里用 1 条验证截断仍生效
        only_recent = mem.search("很久以前我们去看过海", k=5, max_scan=1)
        check("max_scan 仍然截断候选",
              all(h["id"] != old_hit for h in only_recent),
              str([(h["id"], h["text"]) for h in only_recent]))

        # 候选上限：直接看候选行，不靠最终 top-k 猜
        rows = mem._rows_for_search(M._grams("他答应周末"), 4000,
                                    ["experience"], 1)
        check("候选上限生效（1 条）", len(rows) <= 1, str(len(rows)))
        fallback = mem._rows_for_search(M._grams("不存在的暗号"), 4000, [], 20)
        check("路由空时回退最近 20 条", 0 < len(fallback) <= 20, str(len(fallback)))
        check("回退也是 active/非 imprint",
              all((r.get("status") or "active") == "active"
                  and (r.get("stage") or "raw") != "imprint" for r in fallback),
              str([(r["id"], r["status"], r["stage"]) for r in fallback]))
        check("无词可查时返回空，不硬翻",
              mem.search("……") == [], str(mem.search("……")))
        mem.forget("摄像头")
        check("forget 同步清倒排与词频",
              mem._query_df(["摄像", "像头"]) == {},
              str(mem._query_df(["摄像", "像头"])))
    finally:
        mem.close()


def test_regions_and_routes() -> None:
    print("— 脑区：四个存储区 + 规则路由 —")
    td = _tmp("retrieval_routes")
    mem = _open(td / "m.db")
    try:
        now = time.time()
        mem.add_episode("他答应周末一起去看海", region="relation", who="他",
                        imp=0.9, ts=now - 60)
        mem.add_episode("今天中午吃了面", region="experience", imp=0.2,
                        ts=now - 120)
        mem.add_episode("为什么记忆会忘记细节", region="knowledge", imp=0.5,
                        ts=now - 180)
        mem.add_episode("你叫什么名字", region="self", imp=0.5, ts=now - 240)
        active = set(mem._active_regions())
        check("四个区都有记忆", {"self", "relation", "experience", "knowledge"} <= active,
              str(active))
        check("关系线索进关系区", mem.route_regions("他答应周末") == ["relation"],
              str(mem.route_regions("他答应周末")))
        check("自我线索进自我区", mem.route_regions("你叫什么名字") == ["self"],
              str(mem.route_regions("你叫什么名字")))
        check("知识线索进知识区", mem.route_regions("为什么记忆会忘记") == ["knowledge"],
              str(mem.route_regions("为什么记忆会忘记")))
        check("日常线索进经历区", mem.route_regions("今天中午吃了什么") == ["experience"],
              str(mem.route_regions("今天中午吃了什么")))
        check("没有线索不进区（交给最近回退）", mem.route_regions("随便聊聊") == [],
              str(mem.route_regions("随便聊聊")))
        both = mem.route_regions("他为什么答应这件事")
        check("同时命中最多两个区", 1 <= len(both) <= 2, str(both))
        check("路由顺序稳定（关系优先）", both[0] == "relation", str(both))
        hits = mem.search("他答应周末", regions=["relation"])
        check("按区检索只拿该区", bool(hits) and all(h["region"] == "relation" for h in hits),
              str([(h["region"], h["text"]) for h in hits]))
        hit2 = mem.search("你叫什么名字", regions=mem.route_regions("你叫什么名字"))
        check("路由 + 检索接得上", bool(hit2) and hit2[0]["region"] == "self",
              str([(h["region"], h["text"]) for h in hit2]))
        fallback = mem._rows_for_search(M._grams("随便聊聊"), 4000, [], 20)
        check("无命中回退最近 20 条（不是全表）",
              0 < len(fallback) <= 20,
              str([(r["id"], r["text"]) for r in fallback]))
    finally:
        mem.close()

    # 只有经历区时，不因为一句「为什么」就漏掉她仅有的记忆
    only = _open(td / "only.db")
    try:
        only.add_episode("今天中午吃了面", region="experience", imp=0.2)
        check("单区时直接进该区", only.route_regions("为什么记忆会忘") == ["experience"],
              str(only.route_regions("为什么记忆会忘")))
    finally:
        only.close()


def test_think_and_legacy_stub() -> None:
    print("— 调度接入：think.recall 走路由，旧桩不崩 —")
    td = _tmp("retrieval_think")
    mem = _open(td / "m.db")
    try:
        now = time.time()
        mem.add_episode("他答应周末一起去看海", region="relation", who="他",
                        imp=0.9, rel=0.9, emo="开心", emo_i=0.8,
                        ts=now - 60)
        mem.add_episode("今天中午吃了面", region="experience", imp=0.2,
                        ts=now - 120)
        th = T.think("他答应周末一起去看海", state=T._empty_state(), mem=mem)
        hits = th["memory"]["hits"]
        check("think 召回的是关系区", bool(hits) and all(
            h.get("region") == "relation" for h in hits),
            str([(h.get("region"), h.get("text")) for h in hits]))
        th2 = T.think("你还记得吗", state=T._empty_state(), mem=_MemStub())
        check("没有 route_regions 的旧桩仍能工作",
              len(th2["memory"]["hits"]) == 1, str(th2["memory"]["hits"]))
    finally:
        mem.close()


def test_star_map() -> None:
    print("— 星图：保留原有节点，只多带 region 字段 —")
    td = _tmp("retrieval_star")
    mem = _open(td / "m.db")
    try:
        mem.add_episode("他答应周末一起去看海", region="relation", who="他",
                        imp=0.9, emo="开心", emo_i=0.9)
        mem.add_episode("今天调通了摄像头", region="experience", imp=0.8)
        mem.add_fact("名字", "测试用户", conf=0.95)
        data = sihai.build_from_db(td / "m.db")
        nodes = data["nodes"]
        check("星图仍是原来那批节点", len(data["nodes"]) == 3, str(len(data["nodes"])))
        eps = [n for n in nodes if n.get("kind") == "episode"]
        check("星图节点带 region", all("region" in n for n in eps),
              str([(n["id"], n.get("region")) for n in eps]))
        check("星图 meta 带分区计数",
              data["meta"].get("regions", {}).get("relation") == 1,
              str(data["meta"].get("regions")))
        check("星图原有层/残响/连线字段都在",
              {"layers", "ambient", "deep"} <= set(data["meta"])
              and all("layer" in n and "deg" in n for n in eps),
              str(sorted(data["meta"])))
    finally:
        mem.close()


def _region_of(mem: M.Memory, eid: int) -> str:
    row = mem._db.execute("SELECT region FROM episodes WHERE id=?", (int(eid),)).fetchone()
    return (row["region"] if row else "")


def main() -> int:
    test_schema_and_write()
    test_search_candidates()
    test_regions_and_routes()
    test_think_and_legacy_stub()
    test_star_map()
    print(f"\n结果：{PASS} 通过，{FAIL} 失败")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
