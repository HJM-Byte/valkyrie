"""自检：出生期的几条不变量。

    python -m tools.selftest_mind

不测「她说得像不像人」——那个只能靠时间看。这里只测会算错、算错了很难发现的东西：
    1 出生     新库跑起来，状态是一行，不是零散变量
    2 单星     成长冻结时，星图只有一颗
    3 闸门     consolidate() 必须拒绝
    4 时间     没人说话，精力会回来、孤独会长
    5 衰减     细节掉得快，意义几乎不掉
    6 改写     他说的话会改她的状态
    7 人话     注入提示词的状态里不许有数字
    8 召回     关系权重高的记忆排前面
"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

from app.memory import Memory, detail_now, meaning_now
from app.mind import GROWTH_LOCKED, Mind

_ok = _bad = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global _ok, _bad
    if cond:
        _ok += 1
        print(f"  ok   {name}")
    else:
        _bad += 1
        print(f"  FAIL {name}   {extra}")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="mind_")) / "t.db"
    print(f"临时库 {tmp}")

    # 1 出生
    m = Mind(tmp)
    s0 = m.snapshot()
    check("出生：有一行状态", s0["turns"] == 0 and s0["born"] > 0)
    check("出生：出生时间就是现在", abs(s0["born"] - time.time()) < 5)

    # 3 闸门
    r = m.consolidate()
    if GROWTH_LOCKED:
        check("闸门：出生期拒绝沉淀", r["ok"] is False)
    else:
        check("闸门：成长已开，允许沉淀", r["ok"] is True and r["grown"] == 0)

    # 4 时间推进
    t1 = s0["born"] + 20 * 3600
    m.tick(now=t1)
    s1 = m.snapshot(now=t1)
    check("时间：20 小时没人，孤独起来了", s1["lonely"] > 0.5, f"lonely={s1['lonely']}")
    check("时间：没人说话，精力回来了", s1["energy"] > 0.9, f"energy={s1['energy']}")

    # 5 衰减
    d0, d5 = detail_now(1.0, 0, 0.5, 0), detail_now(1.0, 365 * 5, 0.5, 0)
    s_0, s_5 = meaning_now(0.95, 0), meaning_now(0.95, 365 * 5)
    check("衰减：五年后细节掉到三成以下", d5 < 0.30, f"D5={d5:.3f}")
    check("衰减：五年后意义还在八成以上", s_5 > 0.80, f"S5={s_5:.3f}")
    check("衰减：重要的事掉得慢", detail_now(1, 180, 0.9, 0) > detail_now(1, 180, 0.3, 0))

    # 6 改写
    before = m.snapshot()
    for _ in range(6):
        m.observe("谢谢你，麻烦你了", "嗯。", {"emotion": "安心"})
    after = m.snapshot()
    check("改写：熟悉度会涨", after["rel"]["熟悉"] > before["rel"]["熟悉"])
    check("改写：被道谢，信任会涨", after["rel"]["信任"] > before["rel"]["信任"])

    # 7 人话
    txt = m.state_text()
    check("人话：状态里没有数字", not any(c.isdigit() for c in txt), repr(txt))

    # 注意力门的三档
    check("注意力：让她办事的话必须接", m.salience("帮我把灯关一下")["mode"] == "respond")
    check("注意力：单字应声不该长篇回", m.salience("嗯。")["mode"] != "respond")
    check("注意力：问到她的名字要接", m.salience("你叫什么？")["mode"] == "respond")

    # 8 召回
    mem = Memory(tmp)
    mem.add_episode("他说带我去看海", imp=0.7, rel=0.9, emo="心动")
    mem.add_episode("海边的风很大，吹得我站不稳", imp=0.5, rel=0.15, emo="平静")
    mem.add_episode("今天调通了摄像头", imp=0.8, rel=0.4)
    hits = mem.search("去看海", k=3)
    check("召回：召回了东西", len(hits) > 0)
    if hits:
        check("召回：关系权重高的排前面", hits[0]["text"] == "他说带我去看海",
              hits[0]["text"])
    # 重要但没命中关键词的，允许浮上来；不重要且没命中的，不许
    hits2 = mem.search("完全不相干的问句", k=5)
    texts = [h["text"] for h in hits2]
    check("召回：无关的普通事不浮上来",
          all("海边" not in t and "看海" not in t for t in texts), str(texts))

    # 2 单星（要用到 sihai，放最后避免权重被前面的测试影响）
    from app import sihai
    g = sihai.build_birth(tmp)
    check("单星：星图只有一个节点", len(g["nodes"]) == 1)
    check("单星：那颗星是她自己", g["nodes"][0]["id"] == "star_self")
    check("单星：没有边", g["edges"] == [])
    check("单星：标着成长冻结", g["meta"]["growth"] == "frozen")
    check("单星：待沉淀数对得上", g["meta"]["pending"] == mem.stats()["pending"])

    # 提示词里不许出现数字状态
    from app.persona import Persona
    p = Persona()
    prompt = p.compose("你还记得吗", mem=mem, mind=m, salience=m.salience("你还记得吗"))
    check("提示词：能拼出来", len(prompt) > 200)
    check("提示词：带上了状态段", "【你现在】" in prompt or "【现在】" in prompt)

    # 9 一轮完整对话（用假的嘴，只验接线）
    from app.chat import Chat

    class FakeLLM:
        def __init__(self):
            self.systems = []

        def chat(self, text, image_b64=None, system=None):
            self.systems.append(system or "")
            return {"text": "嗯，我在。", "seconds": 0.01}

    # Chat.turn 会读掉休眠标记（_claim_wake），测试绝不能碰真实的 data/sleep.json。
    # 之前这里没隔离，跑一次自检就等于把她从睡眠里叫醒了一次。
    # 心流落盘同理：turn 会推进 flow，必须给临时文件，否则会污染 data/flow.json。
    from app import sleep as _sleep
    from app import dispatch as _disp
    from app.flow import Flow
    _sleep.SLEEP_FILE = tmp.parent / "sleep_test.json"
    _sleep.SLEEP_LOG = tmp.parent / "sleep_log_test.jsonl"
    # 调度足迹同理：不隔离的话，跑一次自检就往真实足迹里写一条测试句
    _disp.FOOTPRINT = tmp.parent / "footprint_test.jsonl"

    fake = FakeLLM()
    from app.learning import Learning
    c = Chat(mem=Memory(tmp), mind=m, persona=p, llm=fake,
             flow=Flow(tmp.parent / "flow_test.json"),
             learning=Learning(tmp.parent / "learn_test.db"))
    before_turns = m.snapshot()["turns"]
    before_eps = Memory(tmp).stats()["episodes"]
    # 这个用例只验 Chat 的接线，不启动真实学习窗口；学习层另有自检。
    from app import config as _cfg
    _paused = _cfg.LEARNING_PAUSED
    _cfg.LEARNING_PAUSED = True
    try:
        r = c.turn("我今天面试没过，有点难受")
    finally:
        _cfg.LEARNING_PAUSED = _paused
    check("一轮：成功了", r.get("ok") is True, str(r.get("error")))
    check("一轮：有回复", r.get("reply") == "嗯，我在。")
    check("一轮：轮次 +1", r["state"]["turns"] == before_turns + 1)
    if GROWTH_LOCKED:
        check("一轮：出生期不写经历", r["memory"]["episodes"] == before_eps,
              f"{before_eps} -> {r['memory']['episodes']}")
        check("一轮：成长仍然冻结", r["state"]["growth"] == "frozen")
        check("一轮：出生期没有记忆段", "【你记得】" not in fake.systems[0])
        check("一轮：出生期没有人设", "爱弥丝" not in fake.systems[0]
              and "主人叫你" not in fake.systems[0], fake.systems[0][:120])
        check("一轮：出生期知道自己的名字", "瓦尔基里" in fake.systems[0],
              fake.systems[0][:120])
    else:
        check("一轮：成长开启后写经历",
              r["memory"]["episodes"] == before_eps + 1,
              f"{before_eps} -> {r['memory']['episodes']}")
        check("一轮：成长已开启", r["state"]["growth"] == "open")
        check("一轮：成长后有人设",
              "主人给你起的名字" in fake.systems[0]
              or "你是瓦尔基里" in fake.systems[0],
              fake.systems[0][:160])
        check("一轮：成长后知道自己的名字", "瓦尔基里" in fake.systems[0],
              fake.systems[0][:120])
    check("一轮：提示词进了模型", len(fake.systems) == 1 and len(fake.systems[0]) > 100)
    c.close()

    # 10 服务端接口：模型不在时必须干净地报错，不能 500 崩
    import json as _json
    import os as _os
    import urllib.request as _u

    base = _os.environ.get("ROBOT_TEST_BASE", "http://127.0.0.1:8766")

    def _post(path: str, obj: dict) -> dict:
        req = _u.Request(base + path, data=_json.dumps(obj).encode(),
                         headers={"Content-Type": "application/json"})
        with _u.urlopen(req, timeout=60) as fp:
            return _json.loads(fp.read())

    try:
        got = _post("/api/chat", {"text": "在吗"})
        check("接口：回了 JSON", isinstance(got, dict) and "ok" in got)
        if not got.get("ok"):
            check("接口：失败时给出具体原因", bool(got.get("error")))
        else:
            check("接口：云端能回话", bool(got.get("reply")))
    except Exception as e:
        print(f"  skip 接口（服务没起或连不上 {base}: {type(e).__name__}）")

    # ---- 11 思维：公式与策略 ------------------------------------------------
    import pathlib as _pl
    import tempfile as _tf

    from app.think import (THETA, W, activation, appraise, confidence,
                           perceive, think)

    tdb = _pl.Path(_tf.mkdtemp(prefix="think_")) / "t.db"
    tm, tmem = Mind(tdb), Memory(tdb)
    base = {"energy": 1.0, "arousal": 0.2, "gap_shock": 0.0, "silent_h": 0.0,
            "turns": 0, "rel": {"熟悉": 0.0, "信任": 0.0, "依恋": 0.0, "边界": 1.0},
            "temper": {"autonomy": 0.5}}

    check("思维：六项权重和为 1", abs(sum(W.values()) - 1.0) < 1e-9, str(sum(W.values())))
    full = activation({k: 1.0 for k in W})
    check("思维：全满时激活值为 1", abs(full - 1.0) < 1e-6, str(full))
    check("思维：空输入时激活值为 0", activation({}) == 0.0)

    p_hurt = perceive("我今天面试没过，有点难受", base)
    check("前五识：认出了问句以外的东西",
          p_hurt["about_self"] is True and p_hurt["emotion"] == "委屈")
    a_neutral = appraise(p_hurt, base)
    check("评价：没有落空就不会失望", a_neutral["disappointment"] == 0.0,
          str(a_neutral["disappointment"]))
    a_shock = appraise(p_hurt, {**base, "gap_shock": 0.8})
    check("评价：被晾久了会失望（乘积不为零）", a_shock["disappointment"] > 0.0,
          str(a_shock["disappointment"]))

    p_cold = perceive("随便你吧", base)
    a_cold = appraise(p_cold, base)
    check("评价：冷话把控制感压低", a_cold["control"] < a_neutral["control"])
    check("评价：冷话是威胁", a_cold["T"] >= 0.5, str(a_cold["T"]))

    check("可信度：没有证据就是 0", confidence([], 1.0) == 0.0)
    check("可信度：证据越多越敢说",
          confidence([{"act": 0.9}], 1.0) > confidence([{"act": 0.3}], 1.0))
    check("可信度：推断越多越不敢说",
          confidence([{"act": 0.8}], 3.0) < confidence([{"act": 0.8}], 0.5))

    def _th(t):
        return think(t, mind=tm, mem=tmem)

    check("策略：单字应声不展开", _th("嗯。")["decision"]["key"] in ("ack", "quiet"))
    check("策略：问她的名字，知道名字就要答",
          _th("你叫什么？")["decision"]["key"] == "answer")
    check("策略：让她办事要走 act",
          _th("帮我把灯关一下")["decision"]["key"] == "act")
    check("策略：冷话不顺着走",
          _th("随便你吧")["decision"]["key"] == "deflect")
    check("策略：他说自己的事要先接住",
          _th("我今天面试没过，有点难受")["decision"]["key"] == "attend")

    th = _th("你叫什么？")
    check("识海：进意识的东西都在阈值之上",
          all(c["A"] >= THETA for c in th["compete"]["awareness"]))
    check("识海：事件本身在候选里", th["compete"]["candidates"][0]["id"] == "event")
    check("末那识：没有记忆时一律算听来的", th["ownership"]["owner"] == "听来的")
    # 九行是旧基线；情绪调制 enforced 时会多一行「调制」审计，
    # 不能因此把主链路判成失败。
    check("思维：输出里带完整链路（九行，含六欲）",
          len(th["trace"]) in (9, 10), str(len(th["trace"])))
    check("思维：链路上有六欲那一行", any(s.startswith("六欲") for s in th["trace"]),
          str(th["trace"][-2:]))
    if len(th["trace"]) == 10:
        check("思维：调制开启时多一行审计",
              any("调制" in s for s in th["trace"]), str(th["trace"][-2:]))

    from app.persona import Persona as _P
    pp = _P()
    prompt2 = pp.compose_birth("你叫什么？", mind=tm, salience=th["gate"], thought=th)
    check("提示词：出生期带上了思维段", "【你此刻想到】" in prompt2)
    check("提示词：带上了这一轮的规矩", "【这一轮】" in prompt2)
    check("提示词：思维段里没有数字",
          not any(c.isdigit() for c in th["awareness_text"]), th["awareness_text"])

    tm.close()
    tmem.close()

    mem.close()
    m.close()
    print(f"\n{_ok} 通过，{_bad} 失败")
    return 0 if _bad == 0 else 1

if __name__ == "__main__":
    sys.exit(main())
