"""情感输出：把内部状态翻成「怎么说」和「怎么发声」。

内部是数字（精力、心情、残响、关系权重），输出不能是数字：
模型看见「心情 -0.4」只会演，看见「你现在不想笑」才会真的换个说法。
所以这一层只做两件事：

    文字   一小段语气约束（句长、停顿、语气、能不能说「我不知道」）
    声音   给 TTS 的语速与音量（edge-tts 的 rate 百分比 + 播放音量）

不做的事：不往回复里塞表情符号，不写「她笑着说」这种旁白。
她说话的样子，应该从措辞和停顿里出来，不是从括弧里出来。
"""
from __future__ import annotations

from typing import Optional

from . import affect as _affect
from . import desire as _desire

_TONE_WORD = {"乏": "乏", "低": "低落", "亮": "轻快", "平": "平",
              "沉": "沉", "暖": "暖"}


def plan(state: Optional[dict] = None, thought: Optional[dict] = None,
         ambient: Optional[dict] = None, hits: Optional[list] = None) -> dict:
    """算这一轮「说出来该是什么样」。返回给提示词用的一段规则 + 给 TTS 的参数。"""
    state = state or {}
    thought = thought or {}
    ambient = ambient or {}
    ap = thought.get("appraisal") or {}
    dec = thought.get("decision") or {}

    mood = float(state.get("mood") or 0.0)
    energy = float(state.get("energy") or 1.0)
    arousal = float(state.get("arousal") or 0.2)
    lonely = float(state.get("lonely") or 0.0)
    gap_shock = float(state.get("gap_shock") or 0.0)
    rel = state.get("rel") or {}
    boundary = float(rel.get("边界") or 1.0)
    attach = float(rel.get("依恋") or 0.0)

    amb_i = float(ambient.get("intensity") or ap.get("residue_ambient") or 0.0)
    amb_tone = float(ambient.get("tone") or ap.get("ambient_tone") or 0.0)
    unknown = bool(ap.get("unknown_feel"))
    key = str(dec.get("key") or "")
    de = state.get("desire_effect") or {}

    rules: list[str] = []
    tone = "平"
    intensity = 0.0
    rate = 0
    volume = 1.0
    pace = "normal"

    # 身体：乏的时候话短、慢。这是她真的没电了，不是演可怜。
    if energy < 0.35:
        tone, intensity = "乏", max(intensity, 0.55)
        pace, rate, volume = "slow", rate - 8, 0.92
        rules.append("你现在有点乏，句子要短，停顿多一点，别逞强。")
    elif energy < 0.55:
        pace, rate = "slow", rate - 4
        rules.append("你不太有精神，说话比平时省力一点。")

    # 心情：低的时候不逼自己笑；亮的时候也别夸张。
    if mood < -0.30:
        tone, intensity = "低", max(intensity, 0.65)
        pace, rate, volume = "slow", rate - 6, min(volume, 0.90)
        rules.append("你情绪低，不想笑，也不要说俏皮话；句子短，留白多一点。")
    elif mood > 0.45:
        tone, intensity = "亮", max(intensity, 0.50)
        rate += 5
        rules.append("你心情不错，语气可以轻一点，但不要夸张。")

    if arousal > 0.60:
        intensity = max(intensity, 0.70)
        rate += 4
        rules.append("你现在有点激动，语速稍快，但别抢话。")

    # 关系：距离、边界、依恋，都从措辞里透出来，不靠宣布。
    if gap_shock > 0.40:
        intensity = max(intensity, 0.60)
        rules.append("这次隔得比你以为的久，你有点被晾着；别撒娇，先接住他。")
    if lonely > 0.55:
        rules.append("一个人待了很久，你的话里会有点空，不必遮。")
    if boundary < 0.50:
        intensity = max(intensity, 0.55)
        rules.append("刚才不太舒服，你现在不想顺着走；可以短，可以说不。")
    if attach > 0.45:
        rules.append("他不在的时候你会留意门口，别装作无所谓。")

    # 残响：忘掉的是事，留下的是这层感觉。说不清就说不清。
    if amb_i >= 0.20:
        intensity = max(intensity, min(1.0, amb_i + 0.15))
        if amb_tone < -0.12 or unknown:
            rules.append("你心里有一层说不清的不安，想不起是哪件事；"
                         "可以承认「我也说不上来」，不要给它编原因。")
        elif amb_tone > 0.12:
            rules.append("心里有一点暖的底子，说不清从哪来；不需要解释它。")
    if unknown:
        rules.append("如果他要你解释这感觉，就说不知道，不要找一件旧事顶上。")

    # 这一轮的策略：同一句话，接法不同，声音也不同。
    if key == "comfort":
        rate -= 2
        rules.append("他累了或烦了：只短接一句，不追问原因。")
    elif key == "ack":
        rules.append("这一轮只要应一声，别展开。")
    elif key == "deflect":
        pace, rate, volume = "slow", rate - 6, min(volume, 0.90)
        rules.append("你可以说得短、平，不必交代太多。")
    elif key == "confess":
        pace, rate = "slow", rate - 4
        rules.append("你没把握，话留余地，可以说「我不太确定」。")
    elif key == "quiet":
        rules.append("可以不说。")
    elif key == "act":
        rate += 3

    unsure = [m for m in (hits or []) if float(m.get("q_recon") or 1.0) < 0.60]
    if unsure:
        rules.append("想起的那件事细节靠不住，只说个大概，别说成每个字都记得。")

    # 情绪场：正面 / 中性 / 负面各自「怎么说」。前面那些是身体和关系，
    # 这一段才是她此刻心里具体是哪几股情绪——委屈和生气要说得不一样。
    feel = None
    if isinstance((thought or {}).get("feel"), dict):
        feel = _affect.normalize((thought or {})["feel"])
    if not feel:
        feel = _affect.from_state(state)
    for r in _affect.say_rules(feel, cap=2):
        if r not in rules:
            rules.append(r)
    # 六欲：没有感官的那几路只许说想，不许编画面/触感；被听懂那一路最多催一句。
    for r in _desire.prompts(state.get("desire") or {}):
        if r not in rules:
            rules.append(r)
    if float(de.get("frustration") or 0.0) >= 0.20:
        rate -= 2
        volume = min(volume, 0.97)
    fv = _affect.voice_of(feel, cap=2)
    rate += int(fv.get("rate") or 0)
    volume = min(volume, float(fv.get("volume") or 1.0))
    dom, dom_v = _affect.dominant(feel)
    fam = _affect.family_of(dom)
    if dom_v >= 0.30 and dom not in ("平静", "专注") and tone == "平":
        tone = "沉" if fam == "负面" else ("暖" if fam == "正面" else tone)
    # 情绪场本身也是一股强度：心里正翻着，不能因为「精力满、心情平」就说她平。
    intensity = max(intensity, round(_affect.intensity(feel) * 0.85, 3))
    # 六欲也是强度：想要得重，语气不会平。
    intensity = max(intensity, round(float(de.get("strength") or 0.0) * 0.55, 3))

    intensity = round(min(1.0, intensity), 3)
    if intensity < 0.15:
        tone = "平"
    rate = max(-15, min(12, rate))
    voice_rate = f"{rate:+d}%"
    speed_word = {"slow": "慢", "normal": "常速", "quick": "快"}.get(pace, "常速")
    feel_top = _affect.top_text(feel)
    text = f"{_TONE_WORD.get(tone, tone)} · {speed_word} · 语速 {voice_rate} · 音量 {volume:.2f}"
    if feel_top != "—":
        text = f"{feel_top} · " + text

    return {
        "tone": tone,
        "pace": pace,
        "intensity": intensity,
        "unknown_feel": unknown,
        "feel": feel,
        "feel_text": _affect.text(feel),
        "feel_top": feel_top,
        "seven": _affect.seven_totals(feel),
        "seven_text": _affect.seven_text(feel),
        "desire": de,
        "desire_text": _desire.text(state.get("desire") or {}),
        "family": fam if dom_v >= 0.20 else "中性",
        "residue": {"intensity": round(amb_i, 3),
                    "tone": round(amb_tone, 3),
                    "text": ambient.get("text") or ""},
        "rules": rules[:8],
        "voice": {"rate": voice_rate, "volume": round(volume, 2)},
        "text": text,
    }


def text_block(p: Optional[dict]) -> str:
    """把表达计划拼成给模型看的一小段。不写旁白，只写怎么说。"""
    p = p or {}
    rules = [str(x).strip() for x in (p.get("rules") or []) if str(x).strip()]
    if not rules:
        return ""
    return "【怎么说】\n" + "\n".join(f"- {r}" for r in rules)


def voice_of(p: Optional[dict]) -> dict:
    """给 TTS 的参数；缺省就正常说。"""
    p = p or {}
    v = p.get("voice") or {}
    try:
        vol = float(v.get("volume") or 1.0)
    except (TypeError, ValueError):
        vol = 1.0
    return {"rate": str(v.get("rate") or "+0%"), "volume": max(0.3, min(1.5, vol))}
