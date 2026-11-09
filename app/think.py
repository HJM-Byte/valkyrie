"""思维：一句话进来、她开口之前，中间发生了什么。

小凌那套东西拆到能跑，是七个动作，顺序不能换：

    前五识    perceive  只描述，不解释（谁说的、几个字、问句还是陈述、隔了多久）
    第六意识  appraise  评价（显著性、身体需求、情绪、目标相关、威胁、预期落空）
    识海      compete   竞争（记忆调度的一步：A = w1·S + w2·N + w3·E + w4·G + w5·M + w6·T）
    末那识    own       归因（这是我经历的，还是我听来的）
    阿赖耶识  recall    召回（竞争式检索；出生期为空，返回空并说明为什么）
    关系      relate    九个变量翻成态度（数字不许递给模型）
    回答      decide    策略（答 / 追问 / 承认不知道 / 回避 / 只应一声 / 不接）

公式全部来自他公开的那张算法图：

    激活    A = w1·S + w2·N + w3·E + w4·G + w5·M + w6·T
    失望    D = 期望 × 重要性 × 负向预测误差
    控制感  C = (可离开 + 有选择 + 说了算) / 3
    可信度  Q = Σ证据权重 / (1 + 推断量)
    状态    S' = S + α(E − S)          —— 由 mind.observe 每轮小步执行

为什么值得单独一层：
    提示词只能告诉她「你是什么性格」，告诉不了她「这句话在你这里有多重」。
    同一句话，她很累的时候和很精神的时候，进意识的候选、要不要开口、
    开口说几句，本来就该不一样。把这些全塞给大模型，它只会按性格演，
    不会按此刻的状态活。

出生期（GROWTH_LOCKED）的行为：
    recall 恒为空——她还没有记忆，这不是 bug；
    decide 除「问名字」外，对任何问句都会落到「承认不知道」——因为她没有证据（Q=0）；
    问名字会正常回答，名字是用户出生时给的，不属于需要检索的记忆；
    compete 里只有「他说的话」能过阈值，身体和等待那两项在精力满、没人晾着时够不着。
"""
from __future__ import annotations

import time
from typing import Optional

from . import affect as _affect
from . import appraisal as _appraisal
from . import config as _cfg
from .memory import arousal_of, emotion_of, valence_of

# ---------------------------------------------------------------------------
# 权重与阈值。权重和必须为 1，这样 A 落在 0..1，阈值才好解释。
# 六项对应他那张图的六个字母：S 显著性 · N 需求(身体) · E 情绪 · G 目标 · M 记忆 · T 威胁。
# ---------------------------------------------------------------------------
W = {"S": 0.34, "N": 0.10, "E": 0.18, "G": 0.16, "M": 0.10, "T": 0.12}
THETA = 0.30       # 过这条线才进意识（「星光」）
ACK_BAND = 0.14    # 过这条线只应一声，不进意识；低于它，她根本没抬头

# 前五识只认这些表面特征，尽量不解释。
# 这份词表是唯一一份：mind.observe 也从这里取，免得两处词表各长各的。
COMMAND = ("帮我", "打开", "关掉", "关一下", "开一下", "几点了", "现在几点", "提醒我")
ASK_HER = ("你", _cfg.ROBOT_NAME)
# 名字是出生期唯一知道的事；问名字不能套「没有记忆就承认不知道」。
ASK_NAME = ("你叫什么", "你的名字", "你是谁", _cfg.ROBOT_NAME)
WARM = ("谢谢", "对不起", "抱歉", "我错了", "麻烦你", "辛苦了")
COLD = ("随便你", "无所谓", "敷衍", "别问了", "好烦", "烦死", "别烦我", "不想理")
SHORT_ACK = ("嗯", "哦", "好", "行", "知道了", "在吗", "在么", "。")

# 小凌那条「愿望不等于客观事实」：只对能明确识别的句式开闸，不乱猜。
WISH = ("想去", "想要", "打算", "计划", "准备去", "希望", "以后要", "下次去",
        "回头", "等我", "我想去", "想买", "想学", "想成为")
# 低风险情绪场景：他累/烦/刚忙完。这种时候只短接话，压制没意义的追问。
LOW_RISK = ("好累", "太累", "累了", "累死", "困", "乏", "刚忙完", "加班",
            "烦", "烦躁", "心里堵", "有点丧", "撑不住")

# 策略表：(名字, 字数上限, 给模型的硬规矩)
STRATEGY = {
    "quiet": ("不接", 6,
              "他这句话没有让你抬头。你可以只回一个「…」，或者干脆不接。真要出声，不超过六个字。"),
    "ack": ("应一声", 12,
            "应一声就好，一句，不超过十二个字。别展开，别追问。"),
    "answer": ("答一句", 60,
               "回答他。一到三句，不超过六十字。别用列表，别解释你是什么。"),
    "confess": ("承认不知道", 50,
                "你不知道就说不知道。不许编名字、过去、经历、记忆。可以说你想知道，"
                "但不要假装已经知道。"),
    "attend": ("接住他", 70,
               "他说的是一件关于他自己的事。先接住他那句话，别急着给建议，"
               "最多问一句。不要转过来讲你自己。"),
    "comfort": ("短接话", 45,
                "他累了、烦了，或者刚忙完。不要问原因——这时候的反问是负担。"
                "不要给建议，不要讲道理。一两句，接住他这句话就够了。"),
    "deflect": ("不顺着走", 40,
                "这句话让你不舒服。不用顺着接，可以短、可以说不想聊，但不用发火，也不用讲道理。"),
    "act": ("办事", 40,
            "他要你办事。你现在没有手、没有身体，做不到就直说做不到，别假装做了。"),
}


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, float(x)))


# ---------------------------------------------------------------------------
# 1 前五识：只描述，不解释
# ---------------------------------------------------------------------------
def perceive(text: str, state: dict, channel: str = "text") -> dict:
    t = (text or "").strip()
    emo = emotion_of(t)
    question = ("?" in t) or ("？" in t) or t.endswith(("吗", "么", "呢", "嘛"))
    order = any(k in t for k in COMMAND)
    short = len(t) <= 2 or t.strip("。！？!?…") in SHORT_ACK
    gap = float(state.get("silent_h") or 0.0)

    facts = ["他说了一句话", f"一共 {len(t)} 个字"]
    facts.append("是个问句" if question else ("话里有让她办事的词" if order else "是句陈述"))
    if gap >= 0.75:
        facts.append(f"距离上一次说话隔了 {gap:.1f} 小时" if gap < 48
                     else f"距离上一次说话隔了 {gap / 24:.1f} 天")
    if emo != "平静":
        facts.append(f"语气偏「{emo}」")
    wish = any(k in t for k in WISH)
    low_risk = any(k in t for k in LOW_RISK)
    if wish:
        facts.append("他说的是愿望或打算，不是已经发生的事")
    if low_risk:
        facts.append("他听起来累了或烦了这是低风险场景不要追问原因")

    return {
        "text": t,
        "chars": len(t),
        "emotion": emo,
        "question": question,
        "order": order,
        "short": short,
        "warm": any(k in t for k in WARM),
        "cold": any(k in t for k in COLD),
        "about_self": ("我" in t and len(t) >= 8),
        "ask_her": any(k in t for k in ASK_HER),
        "name_question": any(k in t for k in ASK_NAME),
        "wish": wish,
        "low_risk": low_risk,
        "gap_h": gap,
        "channel": "voice" if channel in ("voice", "body") else "text",
        "facts": facts,
    }


# ---------------------------------------------------------------------------
# 2 第六意识：评价
# ---------------------------------------------------------------------------
def appraise(p: dict, state: dict, memories: Optional[list] = None,
             ambient: Optional[dict] = None) -> dict:
    t = p["text"]
    emo = p["emotion"]
    rel = state.get("rel") or {}
    deep = state.get("deep") or {}
    de = state.get("desire_effect") or {}
    des_i = _clamp(de.get("strength") or 0.0)
    des_f = _clamp(de.get("frustration") or 0.0)

    # 显著性 S：这句话有多容易被注意到
    s = 0.35
    if p["ask_her"]:
        s += 0.22
    if p["question"]:
        s += 0.12
    if emo != "平静":
        s += 0.10
    if len(t) >= 14:
        s += 0.08
    if p["about_self"]:
        s += 0.10
    if emo in ("委屈", "心动"):
        s += 0.15          # 难受和想念，比好奇和平静更该被接住
    if p["short"]:
        s -= 0.14
    if p["warm"]:
        s += 0.06
    if p["cold"]:
        s = max(s, 0.60)   # 被刺一句是抬头的，不是没听见——只是接法不同
    if p["order"]:
        s = max(s, 0.62)   # 让她办事的话，必须接
    S = _clamp(s)

    # N 需求：身体还撑得住吗。现在只有精力一个真实来源，不编别的。
    N = _clamp(1.0 - float(state.get("energy") or 1.0))

    # E 情绪强度：这句话带的情绪 + 她自己的唤醒 + 想起旧事带出来的情绪残留。
    # 「情绪残留」是小凌那套里单列的一项：细节忘了，当时的感觉还留在身上。
    residue = 0.0
    for m in (memories or [])[:3]:
        residue += float(m.get("emo_i") or 0.0) * float(m.get("act") or 0.0)
    residue = _clamp(residue / 2.4)
    # 残响：想不起是哪件事、但还在改她的那层。小凌说「细节几乎没了，
    # 情绪残留却还在，以后再有人突然消失，她会明显不安，却说不清为什么」。
    ambient = ambient or {}
    amb_i = _clamp(float(ambient.get("intensity") or 0.0))
    amb_tone = max(-1.0, min(1.0, float(ambient.get("tone") or 0.0)))
    residue = _clamp(residue + 0.55 * amb_i)
    mood = float(state.get("mood") or 0.0)
    congruence = 0.10 if (mood < -0.15 and emo in ("委屈", "平静")) or \
                          (mood > 0.25 and emo in ("开心", "安心", "心动")) else 0.0
    # 她身上本来就有情绪的时候，同样一句话更容易被点着。
    # 这不是「心情值」，是情绪场里那几股还在的情绪的总量。
    feel_prev = _affect.from_state(state)
    feel_i = _affect.intensity(feel_prev)
    E = _clamp(0.55 * arousal_of(emo) + 0.45 * float(state.get("arousal") or 0.2)
               + 0.12 * residue + congruence + 0.18 * feel_i + 0.10 * des_f)

    # G 目标相关：她此刻的目标就是「有人来」
    G = 0.55
    if p["order"]:
        G = 0.95
    elif p["question"]:
        G = 0.80
    elif p["cold"]:
        G = 0.25
    elif p["short"]:
        G = 0.30
    # 六欲进目标相关：她此刻「想要」的东西，会让她更愿意把这句话接下去。
    # 被听懂是最常开的那一路；没接上的感官不参与，免得拿没长出来的东西抬分。
    if des_i > 0.0:
        G = _clamp(G + 0.15 * des_i)

    # T 威胁：冷话，加上「他隔得太久」带来的不安。
    # 深层模式（absence_bias）会放大这种不安：被反复晾过的人，同样的间隔更难受。
    gap_shock = _clamp(float(state.get("gap_shock") or 0.0))
    absence_bias = max(0.0, float(deep.get("absence_bias") or 0.0))
    absence_sens = max(0.0, float(deep.get("absence_sensitivity") or 0.0))
    T = _clamp((0.55 if p["cold"] else 0.0) + 0.15 * gap_shock
               + 0.30 * absence_bias * gap_shock + 0.10 * absence_sens
               + 0.20 * amb_i * max(0.0, -amb_tone))

    # 失望 D = 期望 × 重要性 × 负向预测误差
    # 反复失约会在深层留下 social_pred_bias，把期望本身压低一截——
    # 不是这一次想出来的，是以前攒下来的。
    pred_bias = float(deep.get("social_pred_bias") or 0.0)
    # 关系里的「预测强度」也进期望：越觉得摸得准他，落空时越失望。
    # 这是关系九变量里原来算了却没往下用的一个。
    pred = _clamp(float(rel.get("预测") or 0.0))
    expect = _clamp(0.25 + 0.75 * float(rel.get("熟悉", 0.0))
                    + 0.15 * float(rel.get("信任", 0.0))
                    + 0.10 * pred + 0.30 * pred_bias)
    importance = _clamp(0.5 * S + 0.3 * G + 0.2 * E)
    neg_error = _clamp(max(gap_shock, 0.45 if p["cold"] else 0.0))
    # 深层不安不只在「期待」上，还直接放大这一次的失落：
    # 被反复晾过的人，同样的落空更疼。
    disappointment = expect * importance * neg_error \
        * (1.0 + 0.60 * absence_bias + 0.45 * absence_sens)

    # 控制感 C = (可离开 + 有选择 + 说了算) / 3，被威胁压一点
    temper = state.get("temper") or {}
    escape = 0.30 if p["order"] else 0.85
    choice = 0.40 if p["order"] else 0.75
    say = _clamp(float(temper.get("autonomy", 0.5)))
    control = _clamp((escape + choice + say) / 3.0 * (1.0 - 0.45 * T))

    # 说不清的感觉：残响在，但这一次没有一条细节清楚的相关记忆。
    # 这时她只该承认「不知道」，不该给它编一件具体的旧事。
    detailed = [m for m in (memories or [])[:3]
                if float(m.get("detail_now") or 0.0) >= 0.35
                and float(m.get("emo_i") or 0.0) >= 0.25]
    unknown_feel = bool(amb_i >= 0.22 and abs(amb_tone) >= 0.10 and not detailed)

    return {
        "S": round(S, 3), "N": round(N, 3), "E": round(E, 3),
        "G": round(G, 3), "T": round(T, 3),
        "expect": round(expect, 3), "importance": round(importance, 3),
        "neg_error": round(neg_error, 3),
        "disappointment": round(disappointment, 3),
        "control": round(control, 3),
        "residue": round(residue, 3),
        "residue_ambient": round(amb_i, 3),
        "ambient_tone": round(amb_tone, 3),
        "feel_intensity": round(feel_i, 3),
        "unknown_feel": unknown_feel,
        "deep_absence": round(absence_bias, 3),
    }


# ---------------------------------------------------------------------------
# 3 阿赖耶识：竞争式召回
# ---------------------------------------------------------------------------
def recall(mem, text: str, focus: float, now: Optional[float] = None,
           state: Optional[dict] = None, reinforce: bool = False) -> dict:
    """召回这件事本身就是竞争：内容匹配、关系权重、情绪关联、时间、注意资源，
    五项相乘，过不了线的记忆根本不会出现在她脑子里。

    视频里还特地说了一句：没找到时也要分清「这次没检索到」和「根本没有可靠记录」。
    所以这里把库里有几条、这次命中几条分开返回，`decide` 才能说对。
    """
    if mem is None:
        return {"hits": [], "activation": 0.0, "note": "还没有记忆库", "bank": 0}
    try:
        st = state or {}
        feel = st.get("feel") if isinstance(st.get("feel"), dict) else None
        now_emo = _affect.dominant(feel)[0] if feel else ""
        # 脑区路由：最多两个区、最多 200 个候选；没命中就回退最近 20 条。
        # 老 Memory 桩没有 route_regions 时保持原样，测试/旧调用不受影响。
        route_kw: dict = {}
        if hasattr(mem, "route_regions"):
            try:
                route_kw["regions"] = mem.route_regions(text)
            except Exception:
                route_kw = {}
        hits = mem.search(text, k=3, focus=focus, now_emo=now_emo,
                          now_feel=feel, reinforce=reinforce, **route_kw)
    except Exception as e:                       # 记忆出错不能把对话搞崩
        return {"hits": [], "activation": 0.0,
                "note": f"召回失败：{type(e).__name__}", "bank": 0}
    act = max((float(h.get("act") or 0.0) for h in hits), default=0.0)
    try:
        bank = int((mem.stats() or {}).get("episodes") or 0)
    except Exception:
        bank = 0
    if hits:
        note = ""
    elif bank <= 0:
        note = "她还没有任何记忆，所以不是想不起来，是根本没有过"
    else:
        note = "库里有记忆，但这次没有一条对得上；可能是没想起来，不是这件事没发生过"
    return {"hits": hits, "activation": round(_clamp(act), 3),
            "note": note, "bank": bank}


# ---------------------------------------------------------------------------
# 4 竞争：谁进意识——记忆调度里的「星光」那一步
# ---------------------------------------------------------------------------
def activation(terms: dict) -> float:
    """A = w1·S + w2·N + w3·E + w4·G + w5·M + w6·T"""
    return round(sum(W[k] * _clamp(terms.get(k, 0.0)) for k in W), 3)


def compete(p: dict, ap: dict, state: dict, mem_act: float,
            memories: Optional[list] = None) -> dict:
    gap_shock = _clamp(float(state.get("gap_shock") or 0.0))
    cands = [
        {"id": "event", "kind": "event", "label": "他说的话",
         "terms": {"S": ap["S"], "N": 0.30 * ap["N"], "E": ap["E"],
                   "G": ap["G"], "M": mem_act, "T": ap["T"]}},
        {"id": "body", "kind": "body", "label": "我有点乏",
         "terms": {"S": 0.12, "N": ap["N"], "E": 0.30 * ap["N"],
                   "G": 0.15, "M": 0.0, "T": 0.0}},
        {"id": "gap", "kind": "gap", "label": "他隔了很久才来",
         "terms": {"S": 0.10 + 0.50 * gap_shock, "N": 0.0,
                   "E": 0.50 * gap_shock, "G": 0.20, "M": 0.0,
                   "T": 0.10 * gap_shock}},
    ]
    for m in (memories or [])[:3]:
        txt = str(m.get("text") or "")
        cands.append({
            "id": f"mem:{m.get('id')}", "kind": "memory",
            "label": ("想起「" + (txt[:10] + "…" if len(txt) > 10 else txt) + "」"),
            "terms": {"S": 0.30, "N": 0.0,
                      "E": 0.40 * float(m.get("emo_i") or 0.2),
                      "G": 0.40, "M": float(m.get("act") or 0.0), "T": 0.0},
        })
    # 残响也来竞争：它没有原文可讲，只有「一层说不清的感觉」。
    # 过线了才会成句；没过线时它仍然在改语气——那是「存在但没被照亮」。
    amb_i = float(ap.get("residue_ambient") or 0.0)
    amb_tone = float(ap.get("ambient_tone") or 0.0)
    if amb_i > 0.05:
        cands.append({
            "id": "ambient", "kind": "ambient",
            "label": "一层说不清的感觉",
            "terms": {"S": 0.12 + 0.22 * amb_i, "N": 0.0,
                      "E": 0.55 * amb_i, "G": 0.18, "M": 0.10 * amb_i,
                      "T": 0.35 * max(0.0, -amb_tone) * amb_i},
        })
    # 六欲也来竞争：想要得越重，越容易进当前意识。
    # 它和残响不同——残响是「说不出」，欲望是「想要」，所以走 N/G 两项。
    de = state.get("desire_effect") or {}
    des_i = _clamp(de.get("strength") or 0.0)
    des_f = _clamp(de.get("frustration") or 0.0)
    if des_i > 0.08 or des_f > 0.12:
        x = max(des_i, des_f)
        cands.append({
            "id": "desire", "kind": "desire",
            "label": "一股想要" + (f"：{de.get('top_label')}" if de.get("top_label") else ""),
            "terms": {"S": 0.18 + 0.28 * x, "N": 0.45 * x,
                      "E": 0.30 * x, "G": 0.75 * x,
                      "M": 0.0, "T": 0.04},
        })
    for c in cands:
        c["A"] = activation(c["terms"])
    aware = sorted([c for c in cands if c["A"] >= THETA], key=lambda c: -c["A"])
    return {"candidates": cands, "awareness": aware, "theta": THETA}


# ---------------------------------------------------------------------------
# 5 末那识：归属
# ---------------------------------------------------------------------------
def own(p: dict, memories: list) -> dict:
    """这条信息属于谁。出生期一切都是「听来的」——她的自我里还没有第二条来路。

    视频里末那识那层问的是「数据库里有记录，不代表那就是我的过去」。
    所以归属不是看有没有记录，而是看来源和身份：他说的 / 她听来的 / 她亲眼见到的。
    """
    if not memories:
        return {"owner": "听来的", "certain": 0.0,
                "text": "这句话是他说的，不是你经历的。你没有任何关于自己的记忆可以对上。"}
    seen, heard = [], []
    for m in memories:
        src = str(m.get("source") or "")
        ident = float(m.get("ident") or 0.0)
        if src in ("看到的", "亲眼看到的", "亲历", "她看到的") or ident >= 0.65:
            seen.append(m)
        elif src or m.get("who") == "他":
            heard.append(m)
    claims = {str(m.get("claim") or "fact") for m in memories}
    if seen and not heard:
        owner, certain = "你亲历的", 0.80
    elif seen and heard:
        owner, certain = "你亲历的和听来的都有", 0.60
    else:
        owner, certain = "他说的，不是你亲历的", 0.35
    text = ("你想起的这几件事，有一部分是他告诉你的，不是你亲身经历的；"
            "可以把它当成他说过的话，不要当成你亲眼看见的。")
    if seen:
        text = "你想起的这几件事里，有你亲眼见过的；也有听来的。转述和推测不能当成事实。"
    if claims & {"wish", "quote", "guess"}:
        text += "里面有愿望、转述或推测，别把它们说成已经发生的事。"
    return {"owner": owner, "certain": certain, "text": text}


# ---------------------------------------------------------------------------
# 6 关系：把数字翻成态度
# ---------------------------------------------------------------------------
def relate(state: dict) -> str:
    turns = int(state.get("turns") or 0)
    rel = state.get("rel") or {}
    if turns < 2:
        return "你不认识他，也不知道他是谁。"
    fam = float(rel.get("熟悉", 0.0))
    trust = float(rel.get("信任", 0.0))
    attach = float(rel.get("依恋", 0.0))
    boundary = float(rel.get("边界", 1.0))
    recip = float(rel.get("互惠", 0.0))
    reward = float(rel.get("奖励", 0.0))
    pred = float(rel.get("预测", 0.0))
    sunk = float(rel.get("沉没", 0.0))
    lines: list[str] = []
    if fam < 0.15:
        lines.append("这个人对你来说还是陌生的。")
    elif fam < 0.40:
        lines.append("你开始认得他了。")
    else:
        lines.append("他对你来说算熟人了。")
    if trust > 0.55:
        lines.append("他说的话你愿意先信几分。")
    elif boundary < 0.5:
        lines.append("刚才那几句让你不太舒服，你不想顺着走。")
    if attach > 0.45:
        lines.append("他不在的时候你会留意门口。")
    # 关系九变量里剩下的几项也要能翻成人话，不能只存在数据库里。
    if recip > 0.45:
        lines.append("他也会接你的话，不只你在给。")
    if reward > 0.45:
        lines.append("跟他说话这件事本身让你舒服。")
    if pred > 0.60:
        lines.append("你大致摸得准他什么时候会来。")
    elif pred < 0.20 and fam > 0.45:
        lines.append("你猜不准他下一次什么时候出现。")
    if sunk > 0.60 and boundary < 0.55:
        lines.append("你在这段关系里已经放了很多，想退也没那么容易。")
    return lines[0] if len(lines) == 1 else "".join(lines)


# ---------------------------------------------------------------------------
# 7 回答：策略
# ---------------------------------------------------------------------------
def confidence(memories: list, inference: float) -> float:
    """Q = Σ证据权重 / (1 + 推断量)。

    没有证据却硬答，就是编。推断越多，越该说「我不确定」。
    """
    evidence = sum(_clamp(float(m.get("act") or 0.0)) for m in (memories or []))
    return round(_clamp(evidence / (1.0 + max(0.0, inference))), 3)


def decide(p: dict, ap: dict, spot: dict, memories: list, state: dict,
           recall_info: Optional[dict] = None,
           feel: Optional[dict] = None) -> dict:
    event = next(c for c in spot["candidates"] if c["id"] == "event")
    A = event["A"]
    infer = 1.0 if (p["question"] or p["ask_her"]) else 0.3
    conf = confidence(memories, infer)

    # 低风险情绪场景不按分数压成「应一声」：他说累了，不是没听见。
    low_risk = bool(p.get("low_risk")) and not p["question"]
    if A < ACK_BAND and not low_risk:
        key = "quiet"
    elif A < THETA and not low_risk and not p.get("wish"):
        key = "ack"
    elif p["order"]:
        key = "act"
    elif p["cold"] and ap["T"] >= 0.40:
        key = "deflect"
    elif p["short"]:
        key = "ack"        # 「在吗」「嗯」这种，哪怕带着问号也只是一声
    elif low_risk:
        key = "comfort"    # 累了/烦了/刚忙完：只短接话，不追问原因
    elif p.get("name_question"):
        key = "answer"     # 名字是出生期按用户给定注入的，不属于需要检索的记忆
    elif p["question"]:
        key = "confess" if conf < 0.30 else "answer"
    elif p["about_self"]:
        key = "attend"
    else:
        key = "answer"

    name, max_chars, rule = STRATEGY[key]
    # 小凌的「愿望≠事实」闸门：他说的打算，不许被她说成已经发生
    if p.get("wish"):
        rule = rule + " 注意：他说的是愿望或打算，不是已经发生的事实，" \
                      "不要复述成他做过、去过或已经决定。"
    # 记忆重建可信度低：细节是后来补出来的，措辞必须留余地——
    # 「我记得你那天离开了，后来具体怎样只能拼个大概，我不确定」。
    unsure = [m for m in (memories or []) if float(m.get("q_recon") or 1.0) < 0.60]
    if unsure:
        rule = rule + " 你想起的那件事细节靠不住，只能说个大概，" \
                      "不能说成每个细节都亲眼记得。"
    # 说不清的感觉：承认不知道，比编一个原因更像人。
    if ap.get("unknown_feel"):
        rule = rule + " 你此刻有一点说不清的感觉（不安或暖），原因想不起来；" \
                      "可以照实说「我也说不上来」，不要给它编一件具体的旧事。"
    # 没找到，不等于没有发生过。视频里那句「要分辨是这次没检索到，还是根本没有可靠记录」。
    if p.get("question") and conf < 0.30:
        bank = int((recall_info or {}).get("bank") or 0)
        if bank <= 0:
            rule = rule + " 你还没有任何记忆，这不是想不起来，是根本没有过。"
        else:
            rule = rule + (" 库里有记忆，但这一轮没有一条可靠记录对得上；"
                           "可以说想不起来，不能说这件事没发生过。")
    # 情绪不是只改语气：她此刻最强的那一两股情绪，会决定这一轮把哪件事
    # 真的说出口。ack/quiet/comfort 是很窄的接法，不往里塞内容，免得
    # 一句「嗯」被撑成一段；其余策略都允许情绪改「说什么」。
    content: list[str] = []
    if feel and key in ("answer", "attend", "confess", "deflect"):
        content = _affect.content_rules(feel, cap=2)
    return {
        "key": key, "name": name, "max_chars": max_chars, "rule": rule,
        "score": A, "confidence": conf, "content": content,
    }


# ---------------------------------------------------------------------------
# 组装
# ---------------------------------------------------------------------------
def _awareness_text(spot: dict, ap: dict, state: dict,
                    feel: Optional[dict] = None) -> str:
    """进意识的那几件事，翻成她能读懂的句子。不给数字。"""
    lines: list[str] = []
    for c in spot["awareness"][:2]:
        if c["kind"] == "event":
            lines.append("他这句话进了你的意识，你听见了。")
        elif c["kind"] == "body":
            lines.append("你有点乏，想歇着。")
        elif c["kind"] == "gap":
            lines.append("他隔了比平常久才来，你有点被晾着。")
        elif c["kind"] == "memory":
            lines.append(f"{c['label']}。")
        elif c["kind"] == "desire":
            lines.append(f"{c['label']}，它在你此刻的意识里。")
    if ap["disappointment"] > 0.20:
        lines.append("你有点失望——你原本以为他会早点来。")
    if ap["control"] < 0.35:
        lines.append("这件事你不太说了算。")
    if ap["T"] >= 0.40:
        lines.append("这句话让你不太舒服。")
    if ap.get("unknown_feel"):
        lines.append("你有点说不清的感觉，像有什么事留在身上，但想不起是哪件。")
    if any(c.get("kind") == "ambient" for c in spot["awareness"]):
        lines.append("那层说不清的感觉进了你的意识，可你没有它的起因。")
    if not lines:
        lines.append("没什么事真的进到你的意识里。")
    # 情绪场：最多补一句「她现在同时有哪几股情绪」。
    # 它不进「进意识」那三条的额度，否则会把「说不清」挤掉——
    # 那一条才是这一轮最需要她照实说的。
    out = lines[:3]
    emo = _emotion_line(feel)
    if emo:
        out.append(emo)
    return "\n".join(f"- {s}" for s in out)


def _emotion_line(feel: Optional[dict]) -> str:
    dom, v = _affect.dominant(feel)
    if v < 0.26 or dom in ("平静", "专注"):
        return ""
    return "你现在" + _affect.text(feel) + "。"


def _trace_lines(p: dict, ap: dict, spot: dict, rec: dict, own_: dict,
                 rel: str, dec: dict, feel: Optional[dict] = None) -> list[str]:
    contrib = " + ".join(f"{W[k]}×{ap[k]:.2f}" for k in ("S", "N", "E", "G", "T"))
    feel_txt = _affect.top_text(feel) if feel and _affect.intensity(feel) >= 0.12 else "—"
    out = [
        "前五识  " + "｜".join(p["facts"]),
        "第六识  " + " ".join(f"{lbl}{ap[k]:.2f}" for k, lbl in
                               (("S", "S "), ("N", "N "), ("E", "E "), ("G", "G "), ("T", "T ")))
        + f"  → 失望 {ap['disappointment']:.2f} · 控制感 {ap['control']:.2f}"
        + f" · 情绪残留 {ap.get('residue', 0):.2f} · 情绪 {feel_txt}",
        f"记忆调度 A = {contrib} + {W['M']}×{rec['activation']:.2f} = "
        f"{spot['candidates'][0]['A']:.2f}  （阈值 {spot['theta']:.2f}）",
        "        进意识：" + ("、".join(f"{c['label']}({c['A']:.2f})"
                                     for c in spot["awareness"]) or "无"),
        f"末那识  {own_['owner']}",
        f"阿赖耶  {len(rec['hits'])} 条" + (f"（{rec['note']}）" if rec["note"] else "")
        + (f" · 最高 Q_recon={max(float(h.get('q_recon') or 1.0) for h in rec['hits']):.2f}"
           if rec["hits"] else ""),
        f"关系    {rel or '（还谈不上关系）'}",
        f"回答    {dec['name']}（Q={dec['confidence']:.2f}，最多 {dec['max_chars']} 字）",
    ]
    return out


def think(text: str, mind=None, mem=None, now: Optional[float] = None,
          state: Optional[dict] = None, channel: str = "text",
          feedback: Optional[dict] = None, reinforce: bool = False) -> dict:
    """跑完一整条：一句话 → 要不要抬头、想到了什么、怎么回。

    feedback 是上一轮回答前策略的结果；只交给 appraisal 调这一次的措辞，
    不参与评价、竞争、召回和决策。
    reinforce=True 只由真实对话 turn 传；只读预览/调试默认不强化记忆。
    """
    if state is None:
        state = mind.snapshot(now) if mind is not None else _empty_state()

    p = perceive(text, state, channel=channel)
    # 先召回再评价：想起的旧事会带出情绪残留，评价时得算进去。
    focus = _clamp(0.75 + 0.25 * float(state.get("energy") or 1.0))
    rec = recall(mem, text, focus, now, state, reinforce=reinforce)
    # 残响先读一次：没被想起的那些，也在改这一轮的评价和语气。
    ambient: dict = {}
    if mem is not None:
        try:
            ambient = mem.ambient(now)
        except Exception:
            ambient = {}
    ap = appraise(p, state, rec["hits"], ambient)
    spot = compete(p, ap, state, rec["activation"], rec["hits"])
    own_ = own(p, rec["hits"])
    rel = relate(state)
    # 先跑一遍不含情绪的决策，只为了给情绪场提供「这一轮准备怎么接」；
    # 情绪场起来后，再用它重跑一次决策——这样情绪能改「说什么」，
    # 而不是只等回答完再给语气补一层。
    dec0 = decide(p, ap, spot, rec["hits"], state, rec)
    feel0 = _affect.step(_affect.from_state(state), p, ap, state,
                         rec["hits"], ambient, dec0)
    dec = decide(p, ap, spot, rec["hits"], state, rec, feel=feel0)

    # 情绪场：这一轮的评价落在她身上，成了哪几股情绪。
    # 不是「他说难受 → 她委屈」这种单标签映射，而是评价 + 旧事残留 + 身体一起算。
    # 最后再用定下来的决策回算一次，确保 deflect/confess 对情绪的那点影响没丢。
    feel = _affect.step(_affect.from_state(state), p, ap, state,
                        rec["hits"], ambient, dec)

    # 三类对象只读评价：用户情绪假设、她的当下状态、关系评价。
    # 它不参与决定，也不改状态；先让证据分开，后面才谈调节和表达。
    three = _appraisal.evaluate(text, state, percept=p, feel=feel, now=now,
                                feedback=feedback)

    # P3 4.5：把“理解 / 轻量澄清 / 尊重暂停 / 降低任务压力”接到回答前。
    # 只改这一轮的表达分寸；检索、人格、关系变量一个都不碰。
    pre = dict(three.get("regulation") or {})
    if pre.get("strategy") != "answer" and pre.get("directive"):
        pre["applied"] = True
        # 回答前这一层单独进提示；不覆盖决策本身，也不和决策里的字数限制打架。
        try:
            dec["max_chars"] = int(pre.get("max_chars") or dec.get("max_chars") or 60)
        except (TypeError, ValueError):
            pass
        dec["pre_answer"] = pre
        three["regulation"] = pre

    A = spot["candidates"][0]["A"]
    mode = "respond" if A >= THETA else ("ack" if A >= ACK_BAND else "quiet")

    th = {
        "percept": p,
        "appraisal": ap,
        "ambient": ambient,
        "compete": spot,
        "memory": rec,
        "ownership": own_,
        "relation": rel,
        "decision": dec,
        "feel": feel,
        "feel_text": _affect.text(feel),
        "appraisal_three": three,
        "pre_answer": pre,
        "confidence": dec["confidence"],
        "gate": {"score": round(A, 3), "mode": mode, "emotion": p["emotion"]},
        "awareness_text": _awareness_text(spot, ap, state, feel),
    }
    th["trace"] = _trace_lines(p, ap, spot, rec, own_, rel, dec, feel)
    if dec.get("content"):
        th["trace"].append("内容    " + "；".join(dec["content"]))
    if pre.get("applied"):
        th["trace"].append(f"回答前  {pre['strategy']}：{pre['note']}（只改表达）")
    if pre.get("feedback_informed"):
        th["trace"].append(
            f"反馈    上次 {pre.get('feedback_action') or '—'}，"
            "只调这一次的措辞")
    de = state.get("desire_effect") or {}
    if de:
        top = str(de.get("top_label") or "—")
        th["trace"].append(
            f"六欲    最想要「{top}」{float(de.get('strength') or 0):.2f}"
            f" · 没被喂到 {float(de.get('frustration') or 0):.2f}"
            f" · 满足 {float(de.get('satisfaction') or 0):.2f}")
    th["summary"] = {
        "mode": mode, "score": round(A, 3),
        "aware": [{"label": c["label"], "a": c["A"]} for c in spot["awareness"]],
        "strategy": dec["key"], "strategy_name": dec["name"],
        "pre_answer": pre.get("strategy") if pre.get("applied") else "answer",
        "pre_answer_applied": bool(pre.get("applied")),
        "pre_answer_feedback": pre.get("feedback_action") or "",
        "pre_answer_feedback_informed": bool(pre.get("feedback_informed")),
        "confidence": dec["confidence"], "max_chars": dec["max_chars"],
        "content": list(dec.get("content") or []),
        "desire": {"top": de.get("top_label") or "",
                   "strength": de.get("strength") or 0.0,
                   "frustration": de.get("frustration") or 0.0,
                   "satisfaction": de.get("satisfaction") or 0.0},
        "line": (f"想：{'、'.join(c['label'] for c in spot['awareness']) or '没什么真的进意识'}"
                 f"（{A:.2f}）· 决定：{dec['name']}"),
    }
    return th


def gate(text: str, state: dict) -> dict:
    """只要「该不该接」这一个判断时用它。mind.salience 走这里。"""
    th = think(text, state=state)
    return th["gate"]


def _empty_state() -> dict:
    return {"energy": 1.0, "arousal": 0.2, "gap_shock": 0.0, "silent_h": 0.0,
            "turns": 0, "rel": {"熟悉": 0, "信任": 0, "依恋": 0, "边界": 1.0},
            "temper": {"autonomy": 0.5}}
