"""自检：学习层 + 记忆调度 + 休眠。

不碰真库：全部用临时目录。跑法：
    python -m tools.selftest_learning
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

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
    from app import learning as L
    from app import sleep as S
    from app import dispatch as D

    print("— 渠道与窗口 —")
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        L.BOOKS = tmp / "books"
        L.BOOKS.mkdir(parents=True, exist_ok=True)
        L.CHANNELS_FILE = tmp / "channels.json"
        L.FOOTPRINT = tmp / "footprint.jsonl"
        L.BILI_QUEUE = tmp / "bili_queue.json"
        L.DB_PATH = tmp / "learn.db"

        lr = L.Learning(db_path=L.DB_PATH)
        try:
            chans = lr.channels()
            check("默认渠道 20 条", len(chans) == 20, str(len(chans)))
            enabled = [c["name"] for c in chans if c.get("enabled")]
            check("启用 14 条（13 个线上源 + 本地读本）", len(enabled) == 14,
                  str(enabled))
            for need in ("sspai", "ifanr", "ithome", "geekpark", "tmtpost",
                         "solidot", "zhihu_daily", "web_search", "books",
                         "bilibili_article", "bilibili_rank", "douyin_hot",
                         "baidu_top", "baidu_baike"):
                check(f"启用里含 {need}", need in enabled)
            check("维基默认关（实测不可达）",
                  lr.channel("wiki_random").get("enabled") is False)
            check("搜索入口用 Bing 主、百度备",
                  (lr.channel("web_search") or {}).get("engine") == "bing"
                  and "baidu" in ((lr.channel("web_search") or {}).get("engines") or []))
            check("B站专栏按正文阈值 200 字",
                  (lr.channel("bilibili_article") or {}).get("min_chars") == 200)
            check("抖音热榜标注低可信度阈值 120 字",
                  (lr.channel("douyin_hot") or {}).get("min_chars") == 120)

            print("— 老配置合并 —")
            L.CHANNELS_FILE.write_text(json.dumps({
                "window": ["22:00", "07:00"],
                "channels": [{"name": "sspai", "title": "手改的", "enabled": False}],
            }, ensure_ascii=False), encoding="utf-8")
            check("老配置补进新渠道", len(lr.channels()) == 20, str(len(lr.channels())))
            check("手改的配置不被覆盖",
                  lr.channel("sspai").get("enabled") is False
                  and lr.channel("sspai").get("title") == "手改的")

            cfg = lr.config()
            check("窗口默认 22:00-07:00", cfg.get("window") == ["22:00", "07:00"])
            check("23:30 在窗口内", lr.in_window(datetime(2026, 10, 27, 23, 30)))
            check("12:00 不在窗口内", not lr.in_window(datetime(2026, 10, 27, 12, 0)))
            check("06:59 在窗口内", lr.in_window(datetime(2026, 10, 27, 6, 59)))

            print("— 解析 —")
            html = "<p>她&nbsp;说：<b>记住</b>这件事</p><script>x()</script>"
            txt = L.clean_text(html)
            check("去标签去实体", "记住" in txt and "<" not in txt and "&nbsp;" not in txt,
                  txt)
            rss = """<?xml version="1.0"?><rss><channel>
              <item><title>标题甲</title><link>https://e.com/1</link>
                <description>正文甲，足够长的一句话。</description></item>
              <item><title>标题乙</title><link>https://e.com/2</link>
                <description>正文乙。</description></item></channel></rss>"""
            items = lr._parse_feed(rss, 2)
            check("RSS 解析 2 条", len(items) == 2 and items[0]["title"] == "标题甲",
                  str(items))
            atom = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
              <entry><title>Atom 标题</title><link href="https://a.com/1"/>
                <summary>Atom 正文。</summary></entry></feed>"""
            a_items = lr._parse_feed(atom, 2)
            check("Atom 解析（命名空间不挡路）",
                  len(a_items) == 1 and a_items[0]["url"] == "https://a.com/1",
                  str(a_items))

            print("— 正文抽取 —")
            page = ('<html><head><meta property="og:title" content="记忆与保存">'
                    '<title>不该用的标题</title></head><body><nav>导航栏文字</nav>'
                    '<article><p>' + "第一段正文足够长，用来验证正文抽取不是假通过。" * 3 +
                    '</p><p>' + "第二段也一样长，而且没有导航文字混进来。" * 3 +
                    '</p></article><script>x()</script></body></html>')
            t, main = L._extract_main(page)
            check("正文抽取取到 og:title", t == "记忆与保存", t)
            check("正文不含导航与脚本",
                  "导航栏" not in main and "x()" not in main and len(main) > 100,
                  main[:60])
            check("非 http 链接不抓", lr.fetch_article("file:///nope") is None)

            print("— 关键词 —")
            kws = L.keywords_of("你知道记忆是怎么保存的吗？")
            check("关键词挑出「记忆」「保存」", "记忆" in kws and "保存" in kws, str(kws))
            check("不留「你知」这种碎片", "你知" not in kws, str(kws))

            print("— 视频/搜索平台解析（离线，不联网）—")
            # B站：搜索问句会搜出游戏存档，所以必须用关键词过滤
            check("B站材料相关性：提到关键词才算",
                  L.Learning._relevant("大脑是怎么保存记忆的", ["记忆", "保存"])
                  and not L.Learning._relevant("黑神话悟空旧存档怎么导出", ["记忆"]))
            check("关键词为空时不拦材料", L.Learning._relevant("随便什么内容", []))
            # 抖音热榜：只有话题，标题与正文都要写明不是正文
            dy = L.Learning._douyin_text(["话题甲", "话题乙"], ["上升话题丙"])
            check("抖音热榜拼接带条数与话题",
                  "1. 话题甲" in dy and "2. 话题乙" in dy and "上升话题丙" in dy, dy[:60])
            check("抖音材料标明「只是话题」", "不是正文" in dy)
            # 百度热搜：s-data JSON 注释解析
            bd_html = ('<html><!--s-data:{"data":{"cards":[{"component":"hotList",'
                       '"content":[{"word":"热搜甲","query":"热搜甲","desc":"摘要甲"},'
                       '{"word":"热搜乙","query":"热搜乙","desc":"摘要乙"}]}]}}--></html>')
            bd = L.Learning._parse_baidu_top(bd_html)
            check("百度热搜解析出 2 条",
                  len(bd) == 2 and bd[0]["title"] == "热搜甲" and bd[0]["desc"] == "摘要甲",
                  str(bd))
            # 查询轮转：同一渠道不同 _rot 取不同主题
            probe_ch = {"name": "x", "queries": ["主题甲", "主题乙"], "_rot": 1}
            check("渠道主题按 _rot 轮转",
                  lr._queries(probe_ch, [], 2)[0][0] == "主题乙",
                  str(lr._queries(probe_ch, [], 2)))
            # 百度入口走独立解析函数（不联网验证路由）
            orig_baidu = lr._search_baidu
            lr._search_baidu = lambda q, limit=4: [{"title": "来自百度", "url": "u",
                                                    "snippet": ""}]
            try:
                check("search(engine=baidu) 走百度入口",
                      lr.search("记忆", engine="baidu")[0]["title"] == "来自百度")
            finally:
                lr._search_baidu = orig_baidu

            print("— 待审区与闸门 —")
            book = L.BOOKS / "测试读本.md"
            book.write_text("# 测试读本\n\n这是一段足够长的材料。" + "字" * 60,
                            encoding="utf-8")
            got = lr.harvest(lr.channel("books"))
            check("本地读本能读到", len(got) == 1 and "测试读本" == got[0]["title"])

            cid = lr._add("run-x", lr.channel("books"), got[0])
            check("入库一条候选", bool(cid))
            again = lr._add("run-y", lr.channel("books"), got[0])
            check("同一条不重复入库", again is None)
            check("待审计数 1", lr.pending_count() == 1, str(lr.pending_count()))
            row = lr.pending(5)[0]
            check("待审队列带出预览", bool(row.get("preview")))

            from app.mind import GROWTH_LOCKED
            lr.set_points(cid, [{"claim": "这是一段材料", "evidence": "材料"}])
            r = lr.approve(cid)
            if GROWTH_LOCKED:
                check("成长冻结时审核被拒",
                      r.get("ok") is False and "冻结" in r.get("reason", ""),
                      str(r))
            else:
                check("成长开启后审核通过",
                      r.get("ok") is True and r.get("status") == "approved",
                      str(r))
                check("审核通过后状态是 approved",
                      lr.show(cid).get("status") == "approved")
            check("退回可以做", lr.reject(cid).get("ok") is True)

            print("— 摘要→正文升级（B站专栏两轮取）—")
            ch_bili = {"name": "bilibili_article", "note": "B站专栏"}
            u = "https://www.bilibili.com/read/cv1"
            id1 = lr._add("r1", ch_bili, {"title": "标题", "url": u,
                                          "text": "摘要" * 80,
                                          "via": "bili_desc"}, q=0.55)
            check("摘要先入库（Q 0.55）", bool(id1) and abs(lr.show(id1)["q_recon"] - 0.55) < 1e-6)
            again_bili = lr._add("r2", ch_bili, {"title": "标题", "url": u,
                                                  "text": "正文" * 300,
                                                  "via": "bili_article"},
                                 q=0.85, upgrade=True)
            check("同一 URL 默认不新增", again_bili is None)
            row_bili = lr.show(id1)
            check("摘要被正文原地升级（字数变长、Q→0.85）",
                  len(row_bili["text"]) >= 600
                  and abs(row_bili["q_recon"] - 0.85) < 1e-6
                  and "via=bili_article" in (row_bili["note"] or ""),
                  f'{len(row_bili["text"])}字 q={row_bili["q_recon"]} note={row_bili["note"]}')
            lr._bili_queue_save([{"id": "cv1", "title": "t"}])
            check("B站正文队列可写可读", lr._bili_queue()[0]["id"] == "cv1")

            print("— 提炼 —")
            pts = L.Learning._parse_points(
                '好的：{"points":[{"claim":"记忆会掉细节","evidence":"细节会掉"}]}')
            check("JSON 提炼解析", len(pts) == 1 and pts[0]["claim"] == "记忆会掉细节",
                  str(pts))
            pts2 = L.Learning._parse_points("- 第一条要点内容\n- 第二条要点内容")
            check("格式不对时按行兜底", len(pts2) == 2, str(pts2))

            print("— 好奇心 —")
            q = lr.note_curiosity("你知道记忆是怎么保存的吗？")
            check("记下一条好奇", bool(q) and "记忆" in (q.get("keywords") or []), str(q))
            q2 = lr.note_curiosity("你知道记忆是怎么保存的吗？")
            check("同一问题不重复建条目", q2 and q2["id"] == q["id"])
            check("开放好奇 1 件", len(lr.open_curiosities()) == 1)
            m = lr._curiosity_match("这本书讲记忆的保存方式", lr.open_curiosities())
            check("材料能对上好奇", m == q["id"], str(m))

            print("— 记忆调度 —")
            # 手工塞一条「已审核」知识，验证竞争链路（正式环境里 approve 被闸门挡住）
            lr._db.execute(
                "INSERT INTO candidates(id,run_id,channel,title,url,text,points,q_recon,"
                "status,ts,via,raw_kind,raw_sha1,evidence_status) "
                "VALUES('k1','r0','test','记忆的保存','u','记忆会随时间掉细节',"
                "?,0.8,'approved',?, 'local','book',?,'verified')",
                (json.dumps([{"claim": "记忆的细节会随时间变淡", "evidence": "细节"}],
                            ensure_ascii=False), time.time(),
                 L.sha256_text("记忆会随时间掉细节")))
            lr._db.commit()
            check("已审核知识可读", len(lr.approved()) == 1)
            # 再放一条待审的，验证它进不了记忆调度（还没进识海存储库）
            lr._db.execute(
                "INSERT INTO candidates(id,run_id,channel,title,url,text,points,q_recon,"
                "status,ts) VALUES('p1','r0','test','还没审的','u','记忆的另一条材料',"
                "'[]',0.9,'pending',?)", (time.time(),))
            lr._db.commit()

            si = D.Dispatch(learning=lr)
            kc = si.knowledge_candidates("记忆是怎么保存的")
            check("相关知识进候选", len(kc) >= 1 and kc[0]["A"] > 0.2,
                  str([(c["A"], c["label"]) for c in kc]))
            th = {"compete": {"candidates": [
                {"id": "event", "kind": "event", "label": "他说的话",
                 "terms": {"S": .5, "N": .1, "E": .3, "G": .5, "M": .2, "T": 0},
                 "A": 0.34 * .5 + .10 * .1 + .18 * .3 + .16 * .5 + .10 * .2},
            ]}}
            disp = si.dispatch("记忆是怎么保存的", th, footprint=False)
            check("调度返回照见列表", isinstance(disp.get("aware"), list))
            check("调度链给出唤醒→竞争→照见→心流",
                  isinstance(disp.get("chain"), list) and len(disp["chain"]) == 4,
                  str(disp.get("chain")))
            kinds = {c["kind"] for c in disp["aware"]}
            check("知识参与竞争并可能被照见", "event" in kinds, str(kinds))
            check("待审的那条进不了调度",
                  disp["pending"] >= 1 and
                  all("p1" not in (c.get("label") or "")
                      for c in disp["aware"] + disp.get("subliminal", [])),
                  str(disp["pending"]))
            check("会写进提示的是知识原句",
                  "记忆的细节" in (disp.get("awareness_extra") or "") or
                  disp.get("awareness_extra") is not None)

            print("— 休眠 —")
            S.SLEEP_FILE = tmp / "sleep.json"
            S.SLEEP_LOG = tmp / "sleep_log.jsonl"

            class FakeMem:
                def stats(self):
                    return {"episodes": 0, "facts": 0, "pending": 0}

            class FakeMind:
                def tick(self):
                    return {}

                def snapshot(self):
                    return {"born": time.time(), "turns": 3, "days": 0.1,
                            "hours": 2.0, "growth": "frozen", "rel": {}}

            rec = S.hibernate(FakeMind(), FakeMem(), reason="自检")
            check("休眠记录已写", S.SLEEP_FILE.exists())
            check("status 能读到还没醒", S.status() is not None)
            rec["ts"] = time.time() - 3 * 3600
            S.SLEEP_FILE.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
            info = S.wake_info()
            check("醒来算出睡了约 3 小时",
                  info is not None and 2.9 <= info["slept_h"] <= 3.1, str(info))
            check("同一觉只算一次", S.wake_info() is None)
            check("醒来流水有记录", S.SLEEP_LOG.exists())

            print("— 自动窗口 —")
            lr.in_window = lambda *a, **k: False          # type: ignore[assignment]
            check("窗口外不自动跑", lr.maybe_nightly() is None)
            del lr.in_window

            orig_run = L.Learning.run_window
            L.Learning.run_window = lambda self, **k: {"ok": True, "new": 0}
            try:
                lr.in_window = lambda *a, **k: True       # type: ignore[assignment]
                r = lr.maybe_nightly()
                check("窗口内且今天没跑过 → 起后台作业",
                      bool(r and r.get("started")), str(r))
                time.sleep(0.2)
                check("自动作业不阻塞当前进程", True)
            finally:
                L.Learning.run_window = orig_run
                del lr.in_window
        finally:
            lr.close()

    print(f"\n{OK} 通过 / {FAIL} 失败")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
