"""心流：一个念头牵出下一个念头，但不是无限联想。

小凌第三份礼物的工程版。他把「心流」拆成三条可检查的规则：

    1 有限工作上下文
        当前线索 + 被唤起的记忆 + 上一轮留在手里的结果，装进一份很小的工作区；
        下一步从这里继续，而不是每句话都从零开始。
    2 每轮只取少量
        不许把所有相关记忆一次翻出来。每一轮只带一层，思绪能延续，但不蔓延。
    3 继续要有来处，停止要有理由
        有新联想、问题没答完、还有值得追的线索 → 继续；
        连续几轮没有新进展 → 停；
        外面出现更重要的事 → 允许被打断。

和记忆的区别（这条最要紧）：
    记忆是长期的，落在 memory.db；心流是工作记忆，只活几轮。
    所以这里只存一句线索和一句念头，不存原文、不沉淀、不参与长期召回。
    落盘只是为了让进程重启后接得上，超过 6 小时自动散掉。

用法（chat.py 里）：
    fl = self.flow.context(text, th["memory"]["hits"], event_a=th["gate"]["score"])
    th["flow"] = fl
    if fl["continue"]: 把 fl["block"] 放进提示，按 fl["rule"] 接着想
    每轮结束：self.flow.advance(text, progress=..., pending=..., interrupt=fl["interrupt"])
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Optional

from . import config as C
from .memory import _grams

FLOW_FILE = C.DATA / "flow.json"

_TTL_H = 6.0          # 超过这么久，上一段思绪就散了
_MAX_STEPS = 6        # 只留最近几步
_STALL_LIMIT = 2      # 连续两轮没有进展就停
_CONTINUE_MIN = 0.25  # 思绪强度低于这条线，不值得再接着想


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, float(x or 0.0)))


def _short(text: str, n: int = 24) -> str:
    t = " ".join(str(text or "").split())
    return t[:n] + ("…" if len(t) > n else "")


class Flow:
    def __init__(self, path: Path | str = FLOW_FILE) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    # ---- 存取 -------------------------------------------------------------
    def _empty(self) -> dict:
        return {"chain": None, "last_closed": None}

    def load(self) -> dict:
        with self._lock:
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    return {**self._empty(), **raw}
            except Exception:
                pass
            return self._empty()

    def _save(self, state: dict) -> None:
        with self._lock:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1),
                           encoding="utf-8")
            tmp.replace(self.path)

    def _new_chain(self, now: float) -> dict:
        return {"opened_ts": now, "last_ts": now, "strength": 0.0, "stall": 0,
                "pending": "", "steps": [], "reason": ""}

    # ---- 读：这条线索接不接得上 -------------------------------------------
    def context(self, text: str, memories: Optional[list] = None,
                event_a: float = 0.0, now: Optional[float] = None) -> dict:
        """判断这轮能不能接着上一轮的念头。不改盘，只给判断和提示块。"""
        now = now or time.time()
        st = self.load()
        ch = st.get("chain")
        if not ch:
            return {"continue": False, "interrupt": False, "reason": "new",
                    "steps": 0, "strength": 0.0, "pending": "", "block": "", "rule": ""}

        age_h = max(0.0, now - float(ch.get("last_ts") or now)) / 3600.0
        if age_h > _TTL_H:
            return {"continue": False, "interrupt": False, "reason": "expired",
                    "steps": len(ch.get("steps") or []), "strength": 0.0,
                    "pending": "", "block": "", "rule": ""}

        strength = _clamp(ch.get("strength"))
        steps = ch.get("steps") or []
        if int(ch.get("stall") or 0) >= _STALL_LIMIT:
            return {"continue": False, "interrupt": False, "reason": "stalled",
                    "steps": len(steps), "strength": strength,
                    "pending": str(ch.get("pending") or ""), "block": "", "rule": ""}

        # 这条线索跟现在这句话有多接得上：最近两步的线索词重叠。
        # 光靠词面会漏掉「它/那/还/接着」这种回指，所以回指词也算接上。
        tg = _grams(text)
        prev = " ".join(str(s.get("cue") or "") + " " + str(s.get("thread") or "")
                        for s in steps[-2:])
        pg = _grams(prev)
        rel = len(tg & pg) / max(1, len(tg)) if (tg and pg) else 0.0
        ref = any(k in text for k in ("它", "那", "这", "还", "又", "接着",
                                      "继续", "刚才", "所以", "然后", "后来"))

        # 同一条记忆被再次唤起，也算接上了（「讲起一件事又勾起另外的事」）
        mem_rel = 0.0
        if memories and steps:
            last_ids = set(steps[-1].get("recalled") or [])
            mem_rel = 0.15 if last_ids & {m.get("id") for m in memories} else 0.0

        # 外界更重要的事：允许打断。机器人的世界里没有比面前这句话更重的事，
        # 所以这一条只有调用方显式给 event_a 时才可能触发。
        interrupt = bool(event_a and event_a > strength + 0.25 and strength < 0.85)
        cont = (strength >= _CONTINUE_MIN and (rel >= 0.10 or mem_rel > 0 or ref)) \
            and not interrupt

        if interrupt:
            reason = "interrupted"
        elif cont:
            reason = "continue"
        else:
            reason = "new_topic"

        pending = str(ch.get("pending") or "").strip()
        return {
            "continue": cont,
            "interrupt": interrupt,
            "reason": reason,
            "steps": len(steps),
            "strength": round(strength, 3),
            "related": round(rel, 3),
            "refer": bool(ref),
            "pending": pending,
            "block": (f"上一轮的念头还挂着：{pending or _short(prev)}。"
                      f"接着往下想一小步，只接一层，别把相关的全摊开。"
                      f"这条线索要是已经想完，就自然收住，不用硬接着说。"
                      if cont else ""),
            "rule": ("你上一轮的话没说完。顺着它接一句，但只接一小步；"
                     "想完了就收住。" if cont else ""),
        }

    # ---- 写：一轮结束后推进 -------------------------------------------------
    def advance(self, text: str, progress: float = 0.0, pending: str = "",
                interrupt: bool = False, recalled: Optional[list] = None,
                now: Optional[float] = None) -> dict:
        """一轮结束以后推进心流。

        progress 表示这一轮有没有往前挪：答上了、接住了、办成了 → 有进展；
        答不上来、只应一声 → 没进展。pending 是留在手里的那句念头。
        """
        now = now or time.time()
        st = self.load()
        ch = st.get("chain")
        progress = _clamp(progress)
        if interrupt:
            if ch:
                st["last_closed"] = {"ts": now, "reason": "转去接更急的事",
                                     "steps": len(ch.get("steps") or [])}
            # 打断了不等于这轮白说：眼前这句如果值得，就另起一条新链
            if progress > 0 or pending:
                ch = self._new_chain(now)
                ch["strength"] = round(0.40 + 0.20 * progress, 3)
                ch["steps"] = [{"ts": now, "cue": _short(text), "thread": _short(pending, 40),
                                "progress": round(progress, 2),
                                "recalled": [m.get("id") for m in (recalled or [])][:3]}]
                ch["pending"] = _short(pending, 60)
                ch["last_ts"] = now
                st["chain"] = ch
                self._save(st)
                return {"ok": True, "closed": "interrupted",
                        "strength": ch["strength"], "steps": 1, "stall": 0}
            st["chain"] = None
            self._save(st)
            return {"ok": True, "closed": "interrupted", "strength": 0.0}

        if not ch:
            # 没有必要就不开新链条：一句「嗯」不值得留下思绪
            if progress <= 0 and not pending:
                return {"ok": True, "closed": "", "strength": 0.0, "steps": 0}
            ch = self._new_chain(now)
            st["chain"] = ch

        steps = ch.get("steps") or []
        # 过期或停滞的旧链条直接收掉，不硬续
        age_h = max(0.0, now - float(ch.get("last_ts") or now)) / 3600.0
        if age_h > _TTL_H or int(ch.get("stall") or 0) >= _STALL_LIMIT:
            st["last_closed"] = {"ts": now, "reason": "散了", "steps": len(steps)}
            st["chain"] = None
            self._save(st)
            return {"ok": True, "closed": "expired", "strength": 0.0}

        if progress > 0:
            ch["strength"] = round(_clamp(0.72 * float(ch.get("strength") or 0)
                                          + 0.40 * progress + 0.10), 3)
            ch["stall"] = 0
        else:
            ch["strength"] = round(_clamp(float(ch.get("strength") or 0) * 0.80), 3)
            ch["stall"] = int(ch.get("stall") or 0) + 1

        if progress > 0 or pending:
            steps.append({"ts": now, "cue": _short(text), "thread": _short(pending, 40),
                          "progress": round(progress, 2),
                          "recalled": [m.get("id") for m in (recalled or [])][:3]})
            ch["steps"] = steps[-_MAX_STEPS:]
        ch["pending"] = _short(pending, 60)
        ch["last_ts"] = now

        closed = ""
        if int(ch.get("stall") or 0) >= _STALL_LIMIT:
            st["last_closed"] = {"ts": now, "reason": "想不出新东西了",
                                 "steps": len(ch.get("steps") or [])}
            st["chain"] = None
            closed = "stalled"
        st["chain"] = None if closed else ch
        self._save(st)
        return {"ok": True, "closed": closed,
                "strength": round(float(ch.get("strength") or 0.0), 3),
                "steps": len(ch.get("steps") or []), "stall": ch.get("stall", 0)}

    # ---- 展示 -------------------------------------------------------------
    def status(self, now: Optional[float] = None) -> dict:
        now = now or time.time()
        st = self.load()
        ch = st.get("chain")
        if not ch:
            return {"open": False, "last_closed": st.get("last_closed")}
        return {
            "open": True,
            "strength": round(float(ch.get("strength") or 0), 3),
            "stall": int(ch.get("stall") or 0),
            "steps": len(ch.get("steps") or []),
            "pending": ch.get("pending") or "",
            "age_h": round(max(0.0, now - float(ch.get("last_ts") or now)) / 3600.0, 2),
        }

    def trace(self) -> str:
        s = self.status()
        if not s.get("open"):
            last = s.get("last_closed") or {}
            if last:
                return f"心流    没有在想的（上次收束：{last.get('reason') or '?'}）"
            return "心流    还没有牵起来的念头"
        return (f"心流    接着上一轮（强度 {s['strength']:.2f}，第 {s['steps']} 步，"
                f"挂着「{s['pending']}」）")

    def close(self, reason: str = "手动收束") -> None:
        st = self.load()
        ch = st.get("chain")
        if not ch:
            return
        st["last_closed"] = {"ts": time.time(), "reason": reason,
                             "steps": len(ch.get("steps") or [])}
        st["chain"] = None
        self._save(st)
