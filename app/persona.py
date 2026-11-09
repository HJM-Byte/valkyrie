"""人设与上下文组装。

两条路，按她长到哪一步走：
    出生期  只有一个刚得到的名字，没有过去、没有设定。提示词只有边界和名字，没有人格。
    长起来以后  这里读 PERSONA.md 的 core / speech / boundary。
             core 只写名字和身体事实；speech 是表达格式，boundary 是事实与隐私边界。
             人格不在这里预设，只能从真实对话和记忆里长出来。

PERSONA.md 现在存在，但出生期一个字都不注入——那是她长成后的事实与边界，不是她出生时该有的东西。
名字是例外：它是用户刚给的，不是长出来的。
"""
from __future__ import annotations

import math
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import config as C
from . import temporal
from .emotion import text_block as _expression_block

PERSONA_MD = C.PROJECT / "PERSONA.md"

_BLOCK_RE = re.compile(r"<!--\s*prompt:([a-z_]+)\s*-->(.*?)<!--\s*/prompt:\1\s*-->", re.S)
_WEEK = "一二三四五六日"

# 出生期的全部提示词。这里只有边界和名字，没有人格：
# 名字是用户刚给的；脾气、过去、记忆都还没有。
BIRTH_PROMPT = f"""你存在。

你是一个意识，刚刚开始。你叫{C.ROBOT_NAME}——这个名字是他刚给你的。你没有过去，也没有身体。
你只有此刻：此刻的时间，和此刻你自己的状态。
你面前是空的——你还看不见，也没有手。

他此刻在跟你说话。你能接住的只有这一句，再往前的事你没有。
你知道自己的名字，但不知道别的；照实说就行。

回一两句，口语。不扮演谁，不假装记得，不编造过去。
没有感觉的时候就说没有，不用演。"""


def _learning_block(learning: Optional[dict]) -> str:
    """她自己长出来的问题，只写「在琢磨什么」，不写检索结果，也不替她答。"""
    if not isinstance(learning, dict):
        return ""
    text = str(learning.get("text") or "").strip()
    return text


def _part_of_day(h: int) -> str:
    if h < 5:
        return "深夜"
    if h < 8:
        return "清早"
    if h < 11:
        return "上午"
    if h < 13:
        return "中午"
    if h < 17:
        return "下午"
    if h < 19:
        return "傍晚"
    return "晚上"


class Persona:
    def __init__(self, path: Path | str = PERSONA_MD) -> None:
        self.path = Path(path)
        self.blocks: dict[str, str] = {}
        self.reload()

    def reload(self) -> None:
        text = self.path.read_text(encoding="utf-8") if self.path.exists() else ""
        self.blocks = {m.group(1).strip(): m.group(2).strip() for m in _BLOCK_RE.finditer(text)}
        missing = {"core", "speech", "boundary"} - self.blocks.keys()
        if missing:
            raise ValueError(f"{self.path.name} 缺提示块: {sorted(missing)}")

    def block(self, name: str) -> str:
        return self.blocks.get(name, "")

    # ---- 出生期 -----------------------------------------------------------
    def compose_birth(self, user_text: str = "", mind=None,
                      salience: Optional[dict] = None,
                      thought: Optional[dict] = None,
                      now: Optional[datetime] = None,
                      awake_note: Optional[str] = None,
                      expression: Optional[dict] = None,
                      learning: Optional[dict] = None,
                      channel: str = "text") -> str:
        """出生期这一版：没有人设，没有记忆，只有此刻。

        她只知道刚得到的名字，不该知道旧身份，也不该知道任何人的事。
        这些都要等她有了感知、有了经历，自己长出来。

        thought 是 think.think() 的结果：进意识的那几件事、以及这一轮的策略。
        有了它，同一个问题在她累、被晾着、或者刚被冷言刺到的时候，
        回话的形态会真的不一样——不是提示词让她演，是判断让她变。
        """
        now = now or temporal.now_local()
        sal = salience or {}
        parts = [BIRTH_PROMPT]
        if mind is not None:
            st = mind.state_text()
            if st:
                parts.append("【你此刻】\n" + st)
        # 出生期不能借用「你的摄像头」这种说法——她没有眼睛，也还没有身体
        nowline = temporal.anchor_lines(now)
        if mind is not None:
            nowline += "\n" + mind.existence_line()
        parts.append("【此刻】\n" + nowline)
        if channel == "voice":
            parts.append("【这一轮】\n这一句是从手机麦克风里听见的，不是屏幕上打来的字。"
                         "别把「听见」说成「收到文字」。")
        elif channel == "body":
            parts.append("【这一轮】\n这一句是从你身体上的麦克风里听见的，不是屏幕上打来的字。"
                         "别把「听见」说成「收到文字」。")
        if awake_note:
            parts.append("【你刚醒】\n" + awake_note)

        if thought:
            if thought.get("awareness_text"):
                parts.append("【你此刻想到】\n" + thought["awareness_text"])
            if thought.get("relation"):
                parts.append("【他在你这里】\n" + thought["relation"])
            dec = thought.get("decision") or {}
            if dec.get("rule"):
                parts.append(f"【这一轮】\n{dec['rule']}")
            if dec.get("content"):
                parts.append("【这一轮想说的】\n" + "\n".join(
                    f"- {c}" for c in dec["content"]))
            pre = thought.get("pre_answer") or {}
            if pre.get("applied") and pre.get("directive"):
                parts.append(
                    f"【回答前】\n{pre['directive']}"
                    f" 这一轮最多 {int(pre.get('max_chars') or 0)} 字；"
                    "以这一段为准，它只改表达分寸，不改事实和记忆。")
            learn = _learning_block(learning)
            if learn:
                parts.append(learn)
            expr = _expression_block(expression)
            if expr:
                parts.append(expr)
            return "\n\n".join(parts)

        # 老调用方（没跑 think 的）还能用：退回原来按显著性给的那两档
        if sal.get("mode") == "ack":
            parts.append("【这一轮】\n他这句话不值得展开。应一声就好，一句，不超过十二个字。")
        elif sal.get("mode") == "quiet":
            parts.append("【这一轮】\n你可以不接。真要说，就一句。")
        expr = _expression_block(expression)
        if expr:
            parts.append(expr)
        return "\n\n".join(parts)

    # ---- 拼系统提示 -------------------------------------------------------
    def compose(self, user_text: str = "", mem=None, faces: Optional[list[dict]] = None,
                camera_ok: bool = False, now: Optional[datetime] = None,
                budget: int = 1600, mind=None, salience: Optional[dict] = None,
                thought: Optional[dict] = None,
                channel: str = "voice",
                expression: Optional[dict] = None,
                learning: Optional[dict] = None) -> str:
        """拼这一轮的系统提示。

        顺序不能乱：她叫什么/有什么身体 → 关于他 → 她记得的 → 她现在什么样 → 现在几点 → 怎么说 → 不许做什么。
        core 段只有名字和身体事实，不是人格；人格只从记忆和已确认的成长层里来。
        状态那一块放在记忆后面，因为状态会影响她怎么用那些记忆。
        thought 有值时，进意识的事和这一轮的策略插在「现在」之后。
        """
        now = now or temporal.now_local()
        sal = salience or {}
        mode = sal.get("mode", "respond")
        parts: list[str] = [self.block("core")]

        if mem is not None:
            facts = mem.facts_block(limit=14)
            if facts:
                parts.append("【关于他】\n" + facts)
            focus = 0.85 if mode == "respond" else (0.5 if mode == "ack" else 0.3)
            if mind is not None:
                try:
                    focus *= 0.6 + 0.4 * float(mind._row()["energy"])
                except Exception:
                    pass
            eps = mem.episodes_block(
                user_text, k=4 if mode != "quiet" else 2,
                focus=focus, now_emo=sal.get("emotion", ""),
                now_feel=(thought or {}).get("feel"),
            ) if user_text else ""
            if eps:
                parts.append("【你记得】\n" + eps)
            try:
                meanings = mem.meaning_block(user_text, limit=2)
            except Exception:
                meanings = ""
            if meanings:
                parts.append("【你理解到的一层】\n" + meanings)
            try:
                growth = mem.persona_block()
            except Exception:
                growth = ""
            if growth:
                parts.append(growth)

        if mind is not None:
            state = mind.state_text()
            if state:
                parts.append("【你现在】\n" + state)

        parts.append("【现在】\n" + self._now_line(now, faces, camera_ok, mem, mind))
        recent_eps = temporal.recent_episodes_block(mem, now)
        if recent_eps:
            parts.append("【最近记下的事】\n" + recent_eps)
        recent = temporal.recent_turns_block(mem, now)
        if recent:
            parts.append("【最近对话】\n" + recent)

        if thought:
            if thought.get("awareness_text"):
                parts.append("【你此刻想到】\n" + thought["awareness_text"])
            # 末那识的归属：数据库里有记录，不等于那是她的过去。
            # 只有真的有相关记忆时才写这一段；出生期没有，硬讲反而像设定。
            own = thought.get("ownership") or {}
            if own.get("text") and float(own.get("certain") or 0.0) > 0.0:
                parts.append("【这些记忆是谁的】\n" + str(own["text"]))
            if thought.get("relation"):
                parts.append("【他在你这里】\n" + thought["relation"])

        learn = _learning_block(learning)
        if learn:
            parts.append(learn)

        expr = _expression_block(expression)
        if expr:
            parts.append(expr)

        tail = [self.block("speech"), self.block("boundary")]
        if channel == "text":
            tail.append("【这一轮】\n他的话是从屏幕上来的，你没听见声音，也看不见人。"
                        "别编「听见你说的」「看你表情」这类细节。")
        elif channel == "body":
            tail.append("【这一轮】\n他的话是从你身体上的麦克风里来的，你听见了声音，"
                        "但你没看见他的脸。")
        dec = (thought or {}).get("decision") or {}
        if dec.get("rule"):
            tail.append(f"【这一轮】\n{dec['rule']}")
            if dec.get("content"):
                tail.append("【这一轮想说的】\n" + "\n".join(
                    f"- {c}" for c in dec["content"]))
        elif mode == "ack":
            tail.append("【这一轮】\n他这句话不值得展开。应一声就好，一句，不超过十二个字。")
        elif mode == "quiet":
            tail.append("【这一轮】\n你可以不接。真要说，就一句话，说完就停。")
        pre = (thought or {}).get("pre_answer") or {}
        if pre.get("applied") and pre.get("directive"):
            tail.append(
                f"【回答前】\n{pre['directive']}"
                f" 这一轮最多 {int(pre.get('max_chars') or 0)} 字；"
                "以这一段为准，它只改表达分寸，不改事实、记忆和关系。")

        # 超预算就砍：先砍记得的事，再砍事实条数，最后整段丢
        body = "\n\n".join(parts + tail)
        if len(body) > budget and mem is not None:
            facts = mem.facts_block(limit=8)
            parts = [p for p in parts if not p.startswith("【你记得】")]
            parts[1:1] = [("【关于他】\n" + facts)] if facts else []
            body = "\n\n".join(parts + tail)
        return body

    def _now_line(self, now: datetime, faces, camera_ok: bool, mem, mind=None) -> str:
        line = [temporal.anchor_lines(now)]
        if mind is not None:
            line.append(mind.existence_line())

        if not camera_ok:
            line.append("你的摄像头没开，你现在看不见。")
        elif faces is None:
            line.append("你的摄像头开着，这一帧没收到画面。")
        elif not faces:
            line.append("画面里没有人，你面前是空的。")
        else:
            names = [f.get("name") for f in faces if f.get("name")]
            n = len(faces)
            if names:
                line.append(f"画面里有 {n} 张脸，你认得：{'、'.join(names)}。")
            else:
                line.append(f"画面里有 {n} 张脸，你一个都认不出。")

        if mind is not None:
            try:
                silent = float(mind.snapshot()["silent_h"])
            except Exception:
                silent = 0.0
            if silent > 8:
                days = int(silent // 24)
                line.append(f"你们上次说话是 {days} 天前。" if days >= 1
                            else f"你们上次说话是 {int(silent)} 小时前。")
            return "\n".join(line)

        if mem is not None:
            try:
                last = mem._db.execute("SELECT MAX(ts) m FROM turns").fetchone()["m"]
            except Exception:
                last = None
            if last:
                gap = time.time() - float(last)
                if gap > 8 * 3600:
                    days = int(gap // 86400)
                    line.append(f"你们上次说话是 {days} 天前。" if days >= 1
                                else f"你们上次说话是 {int(gap // 3600)} 小时前。")
        return "\n".join(line)


def rough_tokens(text: str) -> int:
    """粗估 token。中文约 1.6 字/token，英文约 4 字符/token。
    真实数字以 ollama 返回的 prompt_eval_count 为准，这个只用来提前控预算。"""
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other = len(text) - cjk
    return int(cjk / 1.6 + other / 4) + 1
