"""P4-C 自检：人格四层、证据门槛、版本链与回滚。

    C:\\robot\\venv\\Scripts\\python.exe -m tools.selftest_p4c

只使用 data/acceptance/tmp/ 下的临时库。这里验证的是：
- 身份与硬边界不可由模型写；
- 人格候选必须有真实已确认意义/章节，证据不够就拒；
- candidate 不进提示，active 才进提示；
- 同层同字段 active 唯一，旧版可回滚，回滚不抹历史；
- 来源意义/章节失效会级联，active 立刻停止生效。
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, time as dt_time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from app.memory import Memory                       # noqa: E402
from app.persona import Persona                     # noqa: E402
from tools.acceptance import fixtures               # noqa: E402

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


def main() -> int:
    now = time.time()
    with fixtures.temp_workspace("p4c_") as ws:
        mem = ws.open_memory()
        try:
            print("— schema 与核心层 —")
            st = mem.stats()
            check("schema 升到 8", st.get("schema_version") == 8, str(st))
            tables = {r["name"] for r in mem._db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            check("persona_versions 表存在", "persona_versions" in tables,
                  str(sorted(tables)))
            idx = {r["name"] for r in mem._db.execute(
                "SELECT name FROM sqlite_master WHERE type='index'")}
            check("active 唯一索引存在", "ux_persona_active" in idx, str(sorted(idx)))
            check("身份与硬边界不能被模型写成候选",
                  mem.add_persona_candidate(
                      "core", "honesty", "模型不能自改核心").get("reason")
                  == "core_locked")
            check("未知层被拒",
                  mem.add_persona_candidate(
                      "mood", "x", "不是四层里的").get("reason") == "bad_layer")

            print("— 证据门槛 —")
            # 固定在“昨天中午”做同一天证据，避免在 23:59 附近运行
            # 时 now-86400+60 跨过午夜，把同一天样本误判成跨日。
            anchor_day = datetime.fromtimestamp(now - DAY).date()
            same_day_noon = datetime.combine(anchor_day, dt_time(12, 0)).timestamp()
            e_same1 = mem.add_episode("同一天第一次：他卡住时我先问", who="我",
                                       source="对话", imp=0.6, ts=same_day_noon)
            e_same2 = mem.add_episode("同一天第二次：我还是先问", who="我",
                                       source="对话", imp=0.6,
                                       ts=same_day_noon + 60)
            e_a = mem.add_episode("几天前他说项目卡住，我先问哪一步", who="我",
                                   source="对话", imp=0.7, ts=now - 5 * DAY)
            e_b = mem.add_episode("后来他愿意把问题拆开，我没有替他做决定",
                                   who="我", source="对话", imp=0.7,
                                   ts=now - 2 * DAY)
            e_ch1 = mem.add_episode("他出门前会说什么时候回来", who="我",
                                     source="对话", imp=0.7, ts=now - 4 * DAY)
            e_ch2 = mem.add_episode("他回来时也会说一声", who="我",
                                     source="对话", imp=0.7, ts=now - 3 * DAY)
            m_same1 = mem.add_meaning_candidate(
                "同一天的理解一", "先问清再继续", episode_ids=[e_same1])
            m_same2 = mem.add_meaning_candidate(
                "同一天的理解二", "不替他把话说完", episode_ids=[e_same2])
            for x in (m_same1, m_same2):
                mem.set_meaning_status(x["id"], "confirmed")
            one = mem.add_persona_candidate(
                "tendency", "curiosity", "先问清再继续",
                meaning_ids=[m_same1["id"]])
            check("稳定倾向只有一条意义时被拒",
                  one.get("reason") == "need_two_meanings", str(one))
            same = mem.add_persona_candidate(
                "tendency", "curiosity", "先问清再继续",
                meaning_ids=[m_same1["id"], m_same2["id"]])
            check("同一天两条意义不能算跨日",
                  same.get("reason") == "need_cross_day", str(same))

            m_a = mem.add_meaning_candidate(
                "先确认再猜", "他卡住时先问哪一步，再继续",
                episode_ids=[e_a])
            m_b = mem.add_meaning_candidate(
                "不替他决定", "只拆问题，不替他选",
                episode_ids=[e_b])
            for x in (m_a, m_b):
                mem.set_meaning_status(x["id"], "confirmed")
            cross = mem.add_persona_candidate(
                "tendency", "curiosity", "他卡住时先问清哪一步，再往下说",
                meaning_ids=[m_a["id"], m_b["id"]])
            check("跨日两条已确认意义可生成候选", cross.get("ok") is True,
                  str(cross))
            check("候选默认不是 active", cross.get("created") is True
                  and mem.persona_versions("active") == [], str(cross))

            print("— candidate 不进提示，active 才进 —")
            pv_id = str(cross.get("id") or "")
            check("候选不进 persona_block", mem.persona_block() == "",
                  mem.persona_block())
            check("候选 usable=false",
                  next(x for x in mem.persona_versions()
                       if x["id"] == pv_id)["usable"] is False)
            bad_field = mem.add_persona_candidate(
                "tendency", "bad-field", "字段名不合规",
                meaning_ids=[m_a["id"], m_b["id"]])
            check("非法字段被拒", bad_field.get("reason") == "bad_value",
                  str(bad_field))
            conf = mem.confirm_persona_version(pv_id)
            check("确认候选成功", conf.get("ok") is True, str(conf))
            check("确认后写入回归结果",
                  bool(conf.get("regression", {}).get("passed")), str(conf))
            block = mem.persona_block()
            check("active 进入 persona_block", "先问清哪一步" in block, block)
            check("active 只有一条", len(mem.persona_versions("active")) == 1)

            print("— 版本替换与回滚 —")
            pv2 = mem.add_persona_candidate(
                "tendency", "curiosity", "先停一下，让他自己说",
                meaning_ids=[m_a["id"], m_b["id"]])
            check("同字段可再提候选", pv2.get("ok") is True, str(pv2))
            conf2 = mem.confirm_persona_version(pv2["id"])
            check("第二版确认成功", conf2.get("ok") is True, str(conf2))
            active = mem.persona_versions("active")
            check("同层同字段仍只有一条 active", len(active) == 1, str(active))
            check("新版本记录了上一版",
                  active[0]["previous_value"] == "他卡住时先问清哪一步，再往下说",
                  str(active[0]))
            old = next(x for x in mem.persona_versions() if x["id"] == pv_id)
            check("旧版本变 superseded", old["status"] == "superseded", str(old))
            check("旧版本有 superseded_by", old["superseded_by"] == pv2["id"],
                  str(old))
            roll = mem.rollback_persona_version(pv_id)
            check("回滚成功", roll.get("ok") is True, str(roll))
            check("回滚是新建版本而不是改旧行",
                  roll.get("id") != pv_id and roll.get("id") != pv2["id"], str(roll))
            active = mem.persona_versions("active")
            check("回滚后 active 是旧值", len(active) == 1
                  and active[0]["value"] == "他卡住时先问清哪一步，再往下说",
                  str(active))
            check("回滚版本指向被恢复的旧版",
                  active[0]["rollback_of"] == pv_id, str(active[0]))
            check("历史里仍有被替换的版本",
                  any(x["id"] == pv2["id"] and x["status"] == "superseded"
                      for x in mem.persona_versions()))
            try:
                mem._db.execute(
                    "INSERT INTO persona_versions(id,layer,field,value,status,"
                    "created_at,updated_at) VALUES('dup','tendency','curiosity',"
                    "'dup','active',1,1)")
                dup_ok = False
            except Exception:
                dup_ok = True
            check("数据库拒绝第二条同字段 active", dup_ok)
            mem._db.rollback()

            print("— 否定、撤回与来源级联 —")
            third = mem.add_persona_candidate(
                "interaction", "address_style", "先接住再给东西",
                meaning_ids=[m_a["id"], m_b["id"]])
            check("互动偏好可由两条意义生成", third.get("ok") is True, str(third))
            rej = mem.set_persona_status(third["id"], "rejected")
            check("候选可否定", rej.get("ok") is True and rej.get("status") == "rejected",
                  str(rej))
            check("被否定的候选不能确认",
                  mem.confirm_persona_version(third["id"]).get("reason")
                  == "not_candidate")
            active_id = mem.persona_versions("active")[0]["id"]
            ret = mem.set_persona_status(active_id, "retracted",
                                         "用户撤回")
            check("active 可撤回", ret.get("ok") is True, str(ret))
            check("撤回后不进入提示", mem.persona_block() == "", mem.persona_block())
            react = mem.add_persona_candidate(
                "tendency", "curiosity", "先问清再往下说",
                meaning_ids=[m_a["id"], m_b["id"]])
            mem.confirm_persona_version(react["id"])
            check("重新确认后可再次生效", "先问清再往下说" in mem.persona_block())
            mem.set_meaning_status(m_a["id"], "disputed", "用户觉得理解偏了")
            live = [x for x in mem.persona_versions()
                    if x["id"] == react["id"]][0]
            check("来源意义争议后 active 变 disputed",
                  live["status"] == "disputed", str(live))
            check("级联后停止进入提示", mem.persona_block() == "",
                  mem.persona_block())

            print("— 章节与自我叙事 —")
            ch = mem.add_chapter("他出门和回来都会说一声", "报备不是汇报，是让彼此知道",
                                 [e_ch1, e_ch2])
            check("章节建立", ch.get("ok") is True, str(ch))
            mem.confirm_chapter(ch["id"])
            m_ch = mem.add_meaning_candidate(
                "报备是连接", "他说一声，是为了让我知道他在哪",
                episode_ids=[e_ch1])
            mem.set_meaning_status(m_ch["id"], "confirmed")
            narrative = mem.add_persona_candidate(
                "narrative", "self_view", "我不是在等消息，是在等一个会回来的人",
                meaning_ids=[m_ch["id"]], chapter_ids=[ch["id"]])
            check("章节 + 意义可生成自我叙事", narrative.get("ok") is True,
                  str(narrative))
            mem.confirm_persona_version(narrative["id"])
            check("自我叙事进入专属提示",
                  "【你怎么理解自己】" in mem.persona_block()
                  and "会回来的人" in mem.persona_block(),
                  mem.persona_block())
            mem.set_chapter_status(ch["id"], "retracted", "用户撤回章节")
            nar = [x for x in mem.persona_versions()
                   if x["id"] == narrative["id"]][0]
            check("章节撤回后叙事变 disputed", nar["status"] == "disputed", str(nar))

            print("— 提示拼装 —")
            # 用已确认章节的两条事件再做一条跨日意义，确认 compose 真的把
            # active 人格块接进系统提示；不是只验证 persona_block() 自己。
            m_ch2 = mem.add_meaning_candidate(
                "先问再拆", "他卡住时先问，再一起拆",
                episode_ids=[e_ch2])
            mem.set_meaning_status(m_ch2["id"], "confirmed")
            trend2 = mem.add_persona_candidate(
                "tendency", "curiosity", "先问清再一起拆，不替他做决定",
                meaning_ids=[m_ch["id"], m_ch2["id"]])
            mem.confirm_persona_version(trend2["id"])
            p = Persona()
            core = p.block("core")
            for bad in ("怕被忘记", "说话快", "会笑", "有点倔", "心软", "嘴上要赢",
                        "电子幽灵", "不喜欢被安排"):
                check(f"core 不含预设人格：{bad}", bad not in core, core)
            check("core 只有名字和身体事实",
                  "瓦尔基里" in core and "小机器人" in core
                  and "怕" not in core and "性格" not in core, core)
            body = p.compose("你还记得那次卡住吗", mem=mem, budget=5000)
            check("compose 能拼出身份", "瓦尔基里" in body)
            check("compose 接入 active 人格块",
                  "先问清再一起拆" in body and "【你正在长成的样子】" in body,
                  body[:800])
            check("compose 没有把未确认内容写进提示",
                  "先接住再给东西" not in body)
            check("payload 的 core 只读",
                  mem.persona_payload("核心测试")["core"]["locked"] is True)
            check("payload 不含 core 写入口",
                  "core" not in mem.persona_payload("核心测试")["layers"])
            check("stats 统计人格版本",
                  mem.stats().get("persona_versions", 0) >= 3,
                  str(mem.stats()))

            print("— 只读性 —")
            before = {k: mem.stats().get(k) for k in
                      ("episodes", "turns", "facts", "meaning_candidates",
                       "chapters", "persona_versions")}
            mem.persona_block()
            mem.persona_versions()
            mem.persona_payload("只读")
            after = {k: mem.stats().get(k) for k in before}
            check("读取人格层不写库", before == after, f"{before} -> {after}")
        finally:
            mem.close()

    print(f"\n{PASS} 通过 / {FAIL} 失败")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
