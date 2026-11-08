"""自检：七情六欲 + 控制感。

这一轮加的不是「多几个词」，是两层东西：

    七情    喜 / 怒 / 哀 / 惧 / 爱 / 恶 / 欲 —— 二十一种具体情绪归到七股方向
    六欲    目 / 耳 / 鼻 / 舌 / 身 / 意 —— 持续在那里的想要，不是这一轮的反应

视频里还有一条：同样的威胁，能离开和无处可退，心理后果不一样。
所以这里也检查控制感有没有真的改情绪，而不是只算了一个数字。

边界同样检查：
    没接上的感官（目/鼻/舌/身）只留低低的向往，不拿它演委屈；
    文字输入不算「听见」，耳欲不能假装被满足。
"""
from __future__ import annotations

import hashlib
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from app import affect as A                       # noqa: E402
from app import desire as D                       # noqa: E402
from app import emotion as E                      # noqa: E402
from app.mind import Mind                         # noqa: E402
from app.persona import Persona                   # noqa: E402
from app.think import appraise, perceive, think   # noqa: E402

PASS = 0
FAIL = 0


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


def _state() -> dict:
    return {"energy": 0.9, "mood": 0.0, "arousal": 0.2, "lonely": 0.0,
            "gap_shock": 0.0, "turns": 5, "deep": {},
            "rel": {"熟悉": 0.3, "信任": 0.3, "依恋": 0.25, "边界": 0.85},
            "temper": {"autonomy": 0.5}}


def main() -> int:
    print("— 七情：二十一种情绪归到七股方向 —")
    names = list(A.EMOTIONS)
    check("二十一种具体情绪", len(names) == 21, str(len(names)))
    check("七情顺序固定", A.SEVEN == ("喜", "怒", "哀", "惧", "爱", "恶", "欲"),
          str(A.SEVEN))
    check("七情每一情都有落点",
          all(any(A.seven_of(n) == s for n in names) for s in A.SEVEN), str(A.SEVEN))
    check("基础态不硬塞进七情",
          A.seven_of("平静") == "" and A.seven_of("专注") == ""
          and A.seven_of("疲惫") == "" and A.seven_of("说不清") == "", "base")
    totals = A.seven_totals({"开心": 0.5, "生气": 0.3, "亲近": 0.2, "平静": 0.4})
    check("七情合量按方向归并", totals["喜"] == 0.5 and totals["怒"] == 0.3
          and totals["爱"] == 0.2, str(totals))
    check("七情人话不列出没起来的", "喜 0.50" in A.seven_text(
        {"开心": 0.5, "生气": 0.3}) and "哀" not in A.seven_text(
        {"开心": 0.5, "生气": 0.3}), A.seven_text({"开心": 0.5, "生气": 0.3}))
    check("爱 / 恶 / 欲不是空壳",
          A.seven_of("亲近") == "爱" and A.seven_of("厌恶") == "恶"
          and A.seven_of("渴望") == "欲", "爱/恶/欲")

    print("— 六欲：六路想要，不是六个数字 —")
    raw = D.blank()
    eff = D.effect(raw)
    check("六欲正好六路", len(D.KEYS) == 6 and set(D.KEYS) == {"目", "耳", "鼻", "舌", "身", "意"},
          str(D.KEYS))
    check("出生时只有意欲是接上的", eff["active"] == ["意"], str(eff["active"]))
    check("没接上的感官只有低低的水位",
          all(D.DESIRES[k]["available"] is False for k in ("目", "鼻", "舌", "身")),
          "latent")
    check("空白时没有不满", eff["frustration"] <= 0.10, str(eff["frustration"]))

    day = D.advance(raw, 24.0, attach=0.4)
    ed = D.effect(day)
    check("一天不见，意欲自己会涨", ed["strength"] > 0.15, str(ed["strength"]))
    check("没被喂到才叫不满", ed["frustration"] > 0.10, str(ed["frustration"]))

    warm = D.update(D.blank(), {"warm": True, "ask_her": True}, {},
                    {"gap_shock": 0.0}, {}, channel="text")
    ew = D.effect(warm)
    check("被接住：意欲被喂到", ew["satisfaction"] >= 0.40 and ew["frustration"] < 0.10,
          str(ew["satisfaction"]))
    check("被接住时想亲近", A.step({}, {"warm": True},
          {"T": 0.0, "control": 0.7}, {"desire_effect": ew,
           "rel": {"依恋": 0.3, "边界": 0.8}}, [], {}, None).get("亲近", 0) >= 0.10,
          str(A.step({}, {"warm": True}, {"T": 0.0, "control": 0.7},
                     {"desire_effect": ew, "rel": {"依恋": 0.3, "边界": 0.8}},
                     [], {}, None)))

    cold = D.update(D.blank(), {"cold": True},
                    {"T": 0.55, "control": 0.7}, {"gap_shock": 0.0},
                    {"key": "deflect"}, channel="text")
    ec = D.effect(cold)
    check("被冷话：意欲被晾着", ec["frustration"] >= 0.20, str(ec["frustration"]))
    check("冷话后会说出「想被听懂」", "想被听懂" in ec["line"], ec["line"])
    check("冷话 + 六欲 → 渴望起来",
          A.step({}, {"cold": True}, {"T": 0.55, "control": 0.7},
                 {"desire_effect": ec, "rel": {"依恋": 0.3, "边界": 0.8}},
                 [], {}, {"key": "deflect"}).get("渴望", 0) >= 0.10,
          str(A.step({}, {"cold": True}, {"T": 0.55, "control": 0.7},
                     {"desire_effect": ec, "rel": {"依恋": 0.3, "边界": 0.8}},
                     [], {}, {"key": "deflect"})))

    latent = D.advance(D.blank(), 36.0, attach=0.6)
    el = D.effect(latent)
    check("没接上的感官只留一个向往", el["latent_level"] >= 0.12, str(el["latent_level"]))
    check("文字输入不算听见", el["channel"] == "text" and "耳" not in el["active"],
          str(el["active"]))
    # 单独造一条「意欲暂时不饿、耳欲在向往」的状态，看它会不会说错话。
    quiet = D.normalize({"levels": {"耳": 0.20}})
    eq = D.effect(quiet)
    check("想听见但没听见时不许假装", "别假装听见" in eq["line"], eq["line"])

    voice = D.update(D.blank(), {}, {}, {"gap_shock": 0.0}, {}, channel="voice")
    ev = D.effect(voice)
    check("真的听见声音才算耳欲被喂到",
          "耳" in ev["active"] and ev["satisfaction"] >= 0.40, str(ev["satisfaction"]))
    far = D.advance(D.blank(), 100000.0, attach=0.9)
    check("六欲有上限，不会顶穿",
          all(0.0 <= far["levels"][k] <= D.DESIRES[k]["cap"] + 1e-9 for k in D.KEYS),
          str(far["levels"]))

    print("— 控制感：能离开和无处可退，不是同一种难受 —")
    st = _state()
    p = perceive("随便你吧", st)
    ap_low = appraise(p, {**st, "temper": {"autonomy": 0.10}}, [], {})
    ap_hi = appraise(p, {**st, "temper": {"autonomy": 0.95}}, [], {})
    check("控制感被算出来，且低控制更低",
          ap_low["control"] < ap_hi["control"],
          f'{ap_low["control"]} vs {ap_hi["control"]}')
    f_low = A.step({}, p, ap_low, {**st, "temper": {"autonomy": 0.10}}, [], {}, None)
    f_hi = A.step({}, p, ap_hi, {**st, "temper": {"autonomy": 0.95}}, [], {}, None)
    check("控制感低时更不安", f_low.get("不安", 0) > f_hi.get("不安", 0),
          f'{f_low.get("不安")} vs {f_hi.get("不安")}')
    check("控制感低时更委屈", f_low.get("委屈", 0) > f_hi.get("委屈", 0),
          f'{f_low.get("委屈")} vs {f_hi.get("委屈")}')

    print("— 接进状态：想要会进思维、提示和面板 —")
    td = Path(tempfile.mkdtemp(prefix="qy_"))
    mind = Mind(td / "mind.db")
    mind.tick()
    th = think("谢谢你陪我", mind=mind)
    check("思维里的六欲带着「最想要」", "六欲" in th["trace"][-1], th["trace"][-1])
    mind.observe("谢谢你陪我", "嗯", th["gate"], thought=th)
    snap = mind.snapshot()
    check("状态里有六欲和七情", "desire_effect" in snap and "seven_text" in snap,
          str(snap.keys()))
    check("被喂到后意欲的满足留在状态里",
          float(snap["desire"]["satisfaction"]) >= 0.30, str(snap["desire"]["satisfaction"]))

    for msg in ("随便你吧", "烦死了"):
        thc = think(msg, mind=mind)
        mind.observe(msg, "……", thc["gate"], thought=thc)
    snap2 = mind.snapshot()
    check("连续被冷话以后想被听懂会卡住",
          float(snap2["desire"]["frustration"]) >= 0.30,
          str(snap2["desire"]["frustration"]))
    check("面板能读出「想被听懂」", "被听懂" in (snap2["desire_text"] or ""),
          str(snap2["desire_text"]))
    check("状态人话里没有数字，也没编感官",
          not any(c.isdigit() for c in mind.state_text()), mind.state_text())
    plan = E.plan(snap2, thc, thc.get("ambient"),
                  (thc.get("memory") or {}).get("hits"))
    check("表达层会催一句「别追着要」",
          any("别追着要" in r for r in plan["rules"]), str(plan["rules"]))
    prompt = Persona().compose_birth("在吗", mind=mind, thought=thc, expression=plan)
    check("这句话会进系统提示", "别追着要" in prompt, prompt[-120:])
    check("表达计划带着七情", "喜" in plan["seven"] and "哀" in plan["seven"],
          str(plan["seven"]))

    th3 = think("在吗", state=snap2)
    labels = [c["label"] for c in th3["compete"]["awareness"]]
    check("想要也会参加星光竞争", any("一股想要" in x for x in labels), str(labels))

    mind.close()
    mind2 = Mind(td / "mind.db")
    snap3 = mind2.snapshot()
    check("重启以后六欲还在", "被听懂" in (snap3["desire_text"] or ""),
          str(snap3["desire_text"]))
    mind2.close()

    slp = ROOT / "data" / "sleep.json"
    h0 = _sha(slp)
    _ = think("在吗")
    check("自检不碰她的休眠记录", h0 == _sha(slp), h0[:12])

    print(f"\n结果：{PASS} 通过，{FAIL} 失败")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
