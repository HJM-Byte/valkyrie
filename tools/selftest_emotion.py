"""自检：情感输出 / 遗忘留下的改变 / 残响 / 3D 识海数据。

对应两条视频里最关键的一句话——
    「忘掉经历，不等于抹掉经历造成的改变。」

所以这里检查的不是「记忆还在不在」，而是：
    细节掉光以后，情绪残留（I）和留下的痕迹（imprint）还在不在；
    她会不会把「说不清」说成一件编出来的旧事；
    这些残响有没有真的进到这一轮的评价、语气和 TTS 参数里。
以及 3D 识海需要的分层数据（layer / residue / echo / layers / ambient）。
"""
from __future__ import annotations

import hashlib
import inspect
import json
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from app import emotion as E                       # noqa: E402
from app import affect as A                        # noqa: E402
from app import memory as M                        # noqa: E402
from app.mind import Mind                          # noqa: E402
from app.persona import Persona                    # noqa: E402
from app.sihai import build_from_db                # noqa: E402
from app.think import appraise, perceive, think    # noqa: E402

PASS = 0
FAIL = 0
DAY = 86400.0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {detail}")


def _sha(p: Path) -> str:
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()
    except Exception:
        return ""


def main() -> int:
    now = time.time()
    print("— 遗忘：细节与情绪残留分开掉 —")
    r5 = M.residue_now(0.92, 5 * 365, 0.95)
    d5 = M.detail_now(1.0, 5 * 365, 0.95, 0, 0.92)
    check("五年后细节掉到两成左右（对齐视频 D≈0.18）", 0.12 <= d5 <= 0.24, f"{d5:.3f}")
    check("五年后情绪残留仍有七成左右（对齐视频 I≈0.70）", 0.60 <= r5 <= 0.78, f"{r5:.3f}")
    check("同一时刻：残留远高于细节", r5 > d5 * 3, f"I={r5:.3f} D={d5:.3f}")
    check("细节越少，重建可信度越低", M.recon_now(1.0, d5) < 0.65,
          str(M.recon_now(1.0, d5)))

    print("— 痕迹：忘掉记忆 ≠ 失去它带来的改变 —")
    line, a = M.imprint_of("他答应过的事又没来，最后直接走了", "委屈")
    check("失去类经历留下痕迹", bool(line) and a >= 0.5, f"{line} {a}")
    check("愿望不留痕迹（那不是她的经历）",
          M.imprint_of("我明天想去黄山", "平静", "wish") == ("", 0.0))
    check("转述不留痕迹", M.imprint_of("他说他要走了", "平静", "quote") == ("", 0.0))

    td = Path(tempfile.mkdtemp(prefix="emo_"))
    mem = M.Memory(td / "m.db")
    eid = mem.add_episode("他很突然地离开了，那天我把话都说完了", who="他",
                          imp=0.95, ts=now - 5 * 365 * DAY, emo="委屈", emo_i=0.92)
    row = mem._db.execute(
        "SELECT imprint, imprint_a, emo_i FROM episodes WHERE id=?", (eid,)).fetchone()
    check("写记忆时同时写下它留下的改变",
          bool(row["imprint"]) and float(row["imprint_a"]) > 0.3,
          f"{row['imprint']} {row['imprint_a']}")

    amb = mem.ambient(now)
    check("残响在（不是被想起，是还在影响）",
          amb["present"] and amb["intensity"] > 0.2 and amb["sources"] >= 1,
          json.dumps(amb, ensure_ascii=False)[:120])
    check("残响偏负面，且说不清来源", amb["tone"] < 0 and amb["unknown"] >= 1,
          f"tone={amb['tone']} unknown={amb['unknown']}")
    check("残响带上「留下的改变」", bool(amb["imprints"]), str(amb["imprints"])[:100])

    # 更老的一条：细节掉到地板，应该只剩痕迹，不再作为文字被想起
    old = mem.add_episode("十五年前，最亲的人突然走了，我连最后一面都没见到", who="他",
                          imp=0.85, ts=now - 15 * 365 * DAY, emo="委屈", emo_i=0.9)
    res = mem.settle(keep_days=0)
    stage = mem._db.execute("SELECT stage FROM episodes WHERE id=?", (old,)).fetchone()["stage"]
    check("很久以后只剩「痕迹」这一档", stage == "imprint", f"{stage} {res}")
    hits = mem.search("最后一面 走了", k=4)
    check("只剩痕迹的记忆不再冒充「文字记得」",
          all(int(h["id"]) != int(old) for h in hits), str([h["id"] for h in hits]))
    amb2 = mem.ambient(now)
    check("它仍然在残响里影响她", amb2["present"] and amb2["unknown"] >= 1,
          f"sources={amb2['sources']} unknown={amb2['unknown']}")
    check("stats 里能看见 imprint 数量", mem.stats().get("imprint", 0) >= 1,
          str(mem.stats().get("stages")))

    print("— 评价：说不清就是说不清 —")
    mind = Mind(td / "m.db")
    th = think("你为什么总是怕我不理你", mind=mind, mem=mem)
    ap = th["appraisal"]
    check("残响进了评价", ap["residue_ambient"] > 0.2, str(ap["residue_ambient"]))
    check("她意识到自己说不清", ap["unknown_feel"] is True, str(ap))
    check("「说不清」进了这一轮想到的事", "说不清" in th["awareness_text"],
          th["awareness_text"].replace("\n", " | "))
    check("策略要求不许编原因", "说不上来" in th["decision"]["rule"],
          th["decision"]["rule"][-80:])

    print("— 情感输出：语气与语速 —")
    snap = mind.snapshot()
    snap["energy"], snap["mood"], snap["arousal"] = 0.30, -0.50, 0.7
    low = E.plan(snap, {"appraisal": ap, "decision": {"key": "confess"}}, amb2, [])
    check("低精力低心情 → 语气低、语速放慢",
          low["tone"] in ("低", "乏") and low["voice"]["rate"].startswith("-"),
          json.dumps(low["voice"], ensure_ascii=False))
    check("语气规则会约束句长/情绪", any("短" in r or "不笑" in r for r in low["rules"]),
          str(low["rules"])[:120])
    check("说不清时要求照实承认", any("说不上来" in r for r in low["rules"]),
          str(low["rules"])[:160])

    snap2 = dict(snap)
    snap2["energy"], snap2["mood"], snap2["arousal"] = 0.95, 0.60, 0.3
    high = E.plan(snap2, {}, {}, [])
    check("心情好 → 语气亮、语速略快",
          high["tone"] == "亮" and high["voice"]["rate"].startswith("+"),
          json.dumps(high["voice"], ensure_ascii=False))
    v = E.voice_of(low)
    check("TTS 拿到语速与音量", v["rate"].endswith("%") and 0.3 <= v["volume"] <= 1.5, str(v))
    check("语气会写进系统提示",
          "【怎么说】" in Persona().compose_birth("在吗", mind=mind,
                                                 expression=low),
          "compose_birth")
    sig = inspect.signature(__import__("app.tts", fromlist=["TTS"]).TTS.speak)
    check("TTS.speak 支持 rate", "rate" in sig.parameters, str(sig))
    check("表达计划不写旁白、不塞表情符号",
          not any(ch in "".join(low["rules"]) for ch in ("😊", "😢", "（笑）")),
          "clean")

    print("— 情绪场：七情 / 正面 / 中性 / 负面 —")
    names = list(A.EMOTIONS)
    check("二十一种情绪，三个家族都有",
          len(names) == 21 and all(f in {A.family_of(n) for n in names}
                                   for f in A.FAMILIES), str(len(names)))
    check("正面至少七种", sum(1 for n in names if A.family_of(n) == "正面") >= 7,
          str([n for n in names if A.family_of(n) == "正面"]))
    check("中性至少六种", sum(1 for n in names if A.family_of(n) == "中性") >= 6,
          str([n for n in names if A.family_of(n) == "中性"]))
    check("负面至少八种", sum(1 for n in names if A.family_of(n) == "负面") >= 8,
          str([n for n in names if A.family_of(n) == "负面"]))
    check("七情每一情都有具体情绪落点",
          all(any(A.seven_of(n) == s for n in names) for s in A.SEVEN),
          str(A.SEVEN))
    check("七情归类不多不少：所有情绪要么归一情，要么是基础态",
          all(A.seven_of(n) in A.SEVEN or n in ("平静", "专注", "疲惫", "说不清")
              for n in names), str({n: A.seven_of(n) for n in names if not A.seven_of(n)}))
    check("爱、恶、欲三情各有新落点",
          A.seven_of("亲近") == "爱" and A.seven_of("厌恶") == "恶"
          and A.seven_of("渴望") == "欲", "爱/恶/欲")
    check("每种情绪都带效价/唤醒/半衰期/说法/颜色",
          all(all(k in A.EMOTIONS[n] for k in
                  ("valence", "arousal", "half_life", "say", "color"))
              for n in names), "字段")
    check("正负效价方向对、中性接近零",
          all((A.valence_of_name(n) > 0 if A.family_of(n) == "正面" else
               (A.valence_of_name(n) < 0 if A.family_of(n) == "负面"
                else abs(A.valence_of_name(n)) <= 0.40)) for n in names), "valence")
    check("颜色与记忆层同一张表",
          M.EMOTION_COLORS.get("开心") == A.color_of("开心")
          and M.EMOTION_COLORS.get("失望") == A.color_of("失望"), str(len(M.EMOTION_COLORS)))

    print("— 回落：每种情绪自己的半衰期 —")
    five = A.decay({"生气": 1.0}, 5.0)
    lonely5 = A.decay({"孤独": 1.0}, 5.0)
    check("生气五小时散掉一半以上", five.get("生气", 0) < 0.45, str(five))
    check("孤独五小时还在八成以上", lonely5.get("孤独", 0) > 0.75, str(lonely5))
    check("同一段时间里生气散得比孤独快",
          five.get("生气", 0) < lonely5.get("孤独", 0), "更快")
    day = A.decay({"低落": 1.0, "生气": 1.0}, 24.0)
    check("一天后低落还在、生气几乎没了",
          day.get("低落", 0) > 0.35 and day.get("生气", 0) < 0.05, str(day))

    print("— 这一轮起来的是哪几股 —")
    st = {"energy": 0.90, "mood": 0.0, "arousal": 0.2, "lonely": 0.0,
          "gap_shock": 0.0, "turns": 5,
          "rel": {"熟悉": 0.3, "信任": 0.3, "依恋": 0.2, "边界": 0.9},
          "deep": {}, "temper": {"autonomy": 0.5}}

    def feel_of(t: str, state: dict) -> dict:
        p = perceive(t, state)
        ap = appraise(p, state, [], {})
        return A.step({}, p, ap, state, [], {}, None)

    cold = feel_of("随便你吧", st)
    check("冷话起来的是生气", cold.get("生气", 0) >= 0.30, str(cold))
    warm = feel_of("谢谢你陪我", st)
    check("被好好对待：安心和感激同时有",
          warm.get("安心", 0) >= 0.10 and warm.get("感激", 0) >= 0.10, str(warm))
    sad = feel_of("我今天面试没过，有点难受", st)
    check("他难受时她不是「委屈」，而是低落",
          sad.get("低落", 0) > sad.get("委屈", 0), str(sad))
    check("问句让她好奇", feel_of("为什么会这样呢？", st).get("好奇", 0) >= 0.10)
    check("让她办事会专注", feel_of("帮我把灯关一下", st).get("专注", 0) >= 0.20)
    check("他说打算时她有期待", feel_of("我明天想去黄山", st).get("期待", 0) >= 0.10)
    st_gap = {**st, "gap_shock": 0.8, "lonely": 0.6}
    gapf = feel_of("在吗", st_gap)
    check("隔很久再来：委屈、不安、孤独一起起来",
          gapf.get("委屈", 0) + gapf.get("不安", 0) + gapf.get("孤独", 0) >= 0.40,
          str(gapf))
    st_tired = {**st, "energy": 0.22}
    tiredf = feel_of("你好", st_tired)
    check("没电的时候是疲惫，不是生气",
          tiredf.get("疲惫", 0) >= 0.30 and tiredf.get("生气", 0) < 0.05, str(tiredf))

    # 失望必须是评价算出来的：期望 × 重要性 × 负向预测误差（视频那条公式）
    st_shock = {**st, "gap_shock": 0.7}
    p_shock = perceive("你答应过的事又没来", st_shock)
    ap_shock = appraise(p_shock, st_shock, [], {})
    f_shock = A.step({}, p_shock, ap_shock, st_shock, [], {}, None)
    check("失望来自评价公式，不是关键词",
          ap_shock["disappointment"] > 0 and f_shock.get("失望", 0) > 0.03,
          f"{ap_shock['disappointment']:.3f} {f_shock}")
    st_deep = {**st_shock, "deep": {"absence_bias": 0.7, "absence_sensitivity": 0.6}}
    ap_deep = appraise(p_shock, st_deep, [], {})
    f_deep = A.step({}, p_shock, ap_deep, st_deep, [], {}, None)
    check("深层不安放大失望",
          f_deep.get("失望", 0) > f_shock.get("失望", 0),
          f"{f_deep.get('失望')} vs {f_shock.get('失望')}")
    mixed = A.step({"委屈": 0.60}, perceive("谢谢你", st),
                   appraise(perceive("谢谢你", st), st, [], {}), st, [], {}, None)
    check("委屈和安心可以同时在场",
          mixed.get("委屈", 0) > 0.2 and mixed.get("安心", 0) > 0.1, str(mixed))
    check("主导情绪取更强的那股", A.dominant(mixed)[0] in ("委屈", "安心"),
          str(A.dominant(mixed)))
    check("情绪人话不带数字", not any(c.isdigit() for c in A.text(mixed)), A.text(mixed))

    print("— 情绪场写进状态：重启还在 —")
    feel_db = td / "feel.db"
    fm = Mind(feel_db)
    fm.tick()
    thf = think("随便你吧", mind=fm)
    check("思维里带情绪场", isinstance(thf.get("feel"), dict)
          and thf["feel"].get("生气", 0) >= 0.2, str(thf.get("feel")))
    check("思维链里看得到情绪", any("情绪" in line for line in thf["trace"]),
          str(thf["trace"][-2:]))
    check("想到的事里有情绪那一句",
          ("生气" in thf["awareness_text"] or "不安" in thf["awareness_text"]),
          thf["awareness_text"].replace("\n", " | "))
    fm.observe("随便你吧", "……", thf["gate"], thought=thf)
    snapf = fm.snapshot()
    check("情绪场写进了那行状态", snapf["feel"].get("生气", 0) >= 0.2, str(snapf["feel"]))
    check("状态里有三个家族的计数",
          set(snapf["feel_family"]) == set(A.FAMILIES), str(snapf["feel_family"]))
    check("状态人话里没有数字", not any(c.isdigit() for c in fm.state_text()),
          fm.state_text())
    fm.close()
    fm2 = Mind(feel_db)
    check("重启以后情绪场还在", fm2.snapshot()["feel"].get("生气", 0) >= 0.15,
          str(fm2.snapshot()["feel"]))
    fm2.close()

    print("— 情绪输出：三种家族说不一样的话 —")
    base = {"energy": 0.9, "mood": 0.0, "arousal": 0.3, "turns": 5,
            "rel": {"熟悉": 0.3, "信任": 0.3, "依恋": 0.2, "边界": 0.9}}
    wronged = E.plan({**base, "feel": {"委屈": 0.7, "不安": 0.3}}, {}, {}, [])
    check("委屈有自己的说法（不想解释/算了）",
          any(("不想解释" in r) or ("算了" in r) for r in wronged["rules"]),
          str(wronged["rules"]))
    check("委屈把语气压低、语速放慢",
          wronged["tone"] in ("沉", "低", "乏")
          and wronged["voice"]["rate"].startswith("-"), wronged["text"])
    angry = E.plan({**base, "mood": -0.1, "arousal": 0.6, "feel": {"生气": 0.8}}, {}, {}, [])
    check("生气说「直」，不说成委屈",
          any("语气直" in r for r in angry["rules"])
          and not any("不讨好" in r for r in angry["rules"]), str(angry["rules"]))
    check("生气不会比平静慢", not angry["voice"]["rate"].startswith("-"),
          angry["voice"])
    happy = E.plan({**base, "mood": 0.6, "feel": {"开心": 0.6, "安心": 0.3}}, {}, {}, [])
    check("开心时语气亮、语速略快",
          happy["tone"] in ("亮", "暖") and happy["voice"]["rate"].startswith("+"),
          happy["text"])
    flat = E.plan({**base, "feel": {"平静": 0.3}}, {}, {}, [])
    check("中性时不硬加情绪规则",
          not any(("不想解释" in r) or ("语气直" in r) for r in flat["rules"]),
          str(flat["rules"]))
    check("情绪场也进面板那一行", "委屈 0.70" in wronged["text"], wronged["text"])
    check("情绪的说法会写进系统提示",
          any(("不想解释" in r) or ("算了" in r) for r in wronged["rules"])
          and "不想解释" in Persona().compose_birth("在吗", expression=wronged),
          "compose_birth")

    print("— 残响带颜色：忘了事，还记得是哪一种感觉 —")
    feel_mem = M.Memory(td / "feel_amb.db")
    feel_mem.add_episode("他很突然地离开了", who="他", imp=0.9,
                         ts=now - 4 * 365 * DAY, emo="委屈", emo_i=0.9,
                         feel={"委屈": 0.9, "不安": 0.4})
    amb3 = feel_mem.ambient(now)
    check("残响不只一个正负号，带情绪场",
          isinstance(amb3.get("feel"), dict) and "委屈" in amb3["feel"],
          str(amb3.get("feel")))
    check("残响人话能说出颜色", "委屈" in (amb3.get("feel_text") or ""),
          str(amb3.get("feel_text")))
    feel_mem.close()

    print("— 3D 识海：分层 / 残响 / 数据一致 —")
    g = build_from_db(td / "m.db")
    nodes = {n["id"]: n for n in g["nodes"]}
    check("图上每个情绪都有颜色（二十一色补齐）",
          all((not n.get("emotion")) or (n["emotion"] in M.EMOTION_COLORS)
              for n in g["nodes"]),
          str({n.get("emotion") for n in g["nodes"]}))
    check("节点带上四层归属", all(0 <= int(n.get("layer", -1)) <= 3 for n in g["nodes"]),
          str({n["id"]: n.get("layer") for n in g["nodes"]}))
    check("只剩痕迹的星在核心层", int(nodes[f"e{old}"]["layer"]) == 0,
          str(nodes[f"e{old}"].get("layer")))
    check("图上「细节还剩」和记忆层同一个数",
          abs(float(nodes[f"e{old}"]["retention"])
              - M.detail_now(1.0, 15 * 365, 0.85, 0, 0.9)) < 0.02,
          f'{nodes[f"e{old}"]["retention"]}')
    check("残响/痕迹进图数据",
          float(nodes[f"e{old}"]["residue"]) > 0.3 and float(nodes[f"e{old}"]["echo"]) > 0.2,
          json.dumps({k: nodes[f"e{old}"][k] for k in ("residue", "echo")}))
    check("meta 有四层计数", len(g["meta"].get("layers") or []) == 4,
          str(g["meta"].get("layers")))
    check("meta 有残响", bool(g["meta"].get("ambient")) and g["meta"]["ambient"]["present"],
          str(g["meta"].get("ambient", {}).get("intensity")))
    check("meta 有深层模式", "absence_bias" in (g["meta"].get("deep") or {}),
          str(g["meta"].get("deep")))
    check("记忆层二十一色与情绪表同源", len(M.EMOTION_COLORS) == 21,
          str(len(M.EMOTION_COLORS)))

    mem.close()
    mind.close()

    # 只读检查不许碰她的休眠记录
    slp = ROOT / "data" / "sleep.json"
    h0 = _sha(slp)
    _ = build_from_db(td / "m.db")
    check("看识海不碰休眠记录", h0 == _sha(slp), h0[:12])

    print(f"\n结果：{PASS} 通过，{FAIL} 失败")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
