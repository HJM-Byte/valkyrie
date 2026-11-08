"""她的日记：每天 23:00，写一页只给自己看的东西。

和识海分开：
- 识海（memory.db）存的是「发生过什么」，会被调度、召回、改写；
- 日记（robot/diary/）存的是「她愿意留下的那一页」，只追加，不参与召回。

为什么不做成 memory episode：
日记如果进识海，第二天又会被自己召回，越写越像在复述自己；
这里只做独立文件，UI 单独展示。写日记也不写 turns、不改情绪状态。

定时器由 `DiaryScheduler` 提供，sihai / pocket_server 各自可启动；
同一台机器上两个服务同时开着时，靠文件锁保证一天只写一次。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from . import config as C

DIARY_ROOT = Path(os.environ.get("ROBOT_DIARY_DIR") or (C.PROJECT / "diary"))
DIARY_HOUR = 23
DIARY_MINUTE = 0
_DAY_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.md$")


def _day_of(ts: Optional[float] = None) -> str:
    return datetime.fromtimestamp(ts if ts is not None else time.time()).strftime("%Y-%m-%d")


def _clean(text: str) -> str:
    t = str(text or "")
    t = re.sub(r"```.*?```", "", t, flags=re.S)
    t = re.sub(r"^\s*(?:日记|我的日记|瓦尔基里)[：:]\s*", "", t.strip())
    t = t.replace("\r\n", "\n").replace("\r", "\n")
    t = re.sub(r"\n{3,}", "\n\n", t).strip()
    return t


class Diary:
    """日记的读写与生成。root 可注入，方便自检不碰真实目录。"""

    def __init__(self, root: Path | str | None = None,
                 clock: Optional[Callable[[], float]] = None,
                 use_llm: bool = True) -> None:
        self.root = Path(root) if root else DIARY_ROOT
        self.root.mkdir(parents=True, exist_ok=True)
        self.clock = clock or time.time
        self.use_llm = use_llm
        self._lock = threading.RLock()

    # ---- 路径与读取 -------------------------------------------------------
    def path_for(self, day: str) -> Path:
        return self.root / f"{day}.md"

    def meta_for(self, day: str) -> Path:
        return self.root / f"{day}.json"

    def exists(self, day: Optional[str] = None) -> bool:
        return self.path_for(day or _day_of(self.clock())).is_file()

    def read(self, day: Optional[str] = None) -> dict:
        day = day or _day_of(self.clock())
        p = self.path_for(day)
        if not p.is_file():
            return {"ok": False, "day": day, "error": "这一天的日记还没有写"}
        raw = p.read_text(encoding="utf-8", errors="replace")
        meta: dict[str, Any] = {}
        try:
            if self.meta_for(day).is_file():
                meta = json.loads(self.meta_for(day).read_text(encoding="utf-8"))
        except Exception:
            meta = {}
        # 去掉固定的文件头，正文从标题后的空行开始；没有头就整篇当正文。
        body = raw
        if raw.startswith("# "):
            parts = raw.split("\n\n", 1)
            if len(parts) == 2:
                body = parts[1].strip()
        return {"ok": True, "day": day, "text": body, "raw": raw,
                "path": str(p), "meta": meta,
                "chars": len(body), "mtime": p.stat().st_mtime}

    def list(self, limit: int = 90) -> list[dict]:
        rows: list[dict] = []
        for p in sorted(self.root.glob("*.md"), reverse=True):
            m = _DAY_RE.match(p.name)
            if not m:
                continue
            day = m.group(1)
            try:
                raw = p.read_text(encoding="utf-8", errors="replace")
                body = raw.split("\n\n", 1)[1].strip() if raw.startswith("# ") and "\n\n" in raw else raw
                meta = {}
                if self.meta_for(day).is_file():
                    meta = json.loads(self.meta_for(day).read_text(encoding="utf-8"))
                rows.append({"day": day, "path": str(p), "chars": len(body),
                             "preview": re.sub(r"\s+", " ", body)[:88],
                             "mtime": p.stat().st_mtime,
                             "source": meta.get("source") or "",
                             "written_at": meta.get("written_at") or ""})
            except Exception:
                continue
        rows.sort(key=lambda x: x["day"], reverse=True)
        return rows[:max(1, int(limit))]

    def summary(self, limit: int = 8) -> dict:
        today = _day_of(self.clock())
        rows = self.list(limit=limit)
        cur = next((r for r in rows if r["day"] == today), None)
        return {"today": today, "has_today": bool(cur),
                "today_entry": cur, "recent": rows,
                "folder": str(self.root),
                "write_at": f"{DIARY_HOUR:02d}:{DIARY_MINUTE:02d}"}

    # ---- 写 ---------------------------------------------------------------
    def _atomic_write(self, p: Path, text: str) -> None:
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(p)

    def write(self, text: str, day: Optional[str] = None, *,
              source: str = "manual", meta: Optional[dict] = None,
              now: Optional[float] = None) -> dict:
        day = day or _day_of(now if now is not None else self.clock())
        body = _clean(text)
        if not body:
            raise ValueError("日记正文是空的")
        now = now if now is not None else self.clock()
        at = datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M")
        raw = (f"# {day} · 瓦尔基里的日记\n\n"
               f"> {at}，写给自己。\n\n{body}\n")
        with self._lock:
            self._atomic_write(self.path_for(day), raw)
            info = {"day": day, "source": source, "chars": len(body),
                    "written_at": at, "ts": now, **(meta or {})}
            self._atomic_write(self.meta_for(day),
                               json.dumps(info, ensure_ascii=False, indent=1))
        return {"ok": True, "day": day, "path": str(self.path_for(day)),
                "text": body, **info}

    # ---- 上下文 -----------------------------------------------------------
    def _context(self, day: str, now: float) -> dict:
        from .memory import Memory
        from .mind import Mind

        mem = Memory()
        mind = Mind()
        try:
            snap = mind.snapshot(now=now)
            recent = mem.recent_episodes(8)
            ambient = mem.ambient(now=now)
        finally:
            try:
                mem.close()
            finally:
                mind.close()
        try:
            from .learning import Learning
            learning = Learning()
            try:
                learn = learning.stats()
            finally:
                learning.close()
        except Exception:
            learn = {}
        return {"day": day, "now": now, "state": snap, "recent": recent,
                "ambient": ambient, "learn": learn}

    def _prompt(self, ctx: dict) -> tuple[str, dict]:
        state = ctx.get("state") or {}
        recent = ctx.get("recent") or []
        ambient = ctx.get("ambient") or {}
        learn = ctx.get("learn") or {}
        lines = [
            "【今天要写日记】",
            "现在是晚上十一点，今天要过完了。你不是在回复他，也不是在汇报状态；"
            "你是在给自己留一页。",
            "写你真正想记住的一两件小事、一个还没想明白的问题，或者此刻心里剩下的一点感觉。",
            "第一人称，200 到 450 字；可以停顿，可以短句，不要列表，不要标题，"
            "不要总结，不要写“今天过得真充实”这种空话。",
            "不要编造没有发生的事；想不起来就写想不起来。",
            "不要写你没有的感官：没有摄像头画面、没有身体触觉，就不要写看见了什么、摸到了什么。",
        ]
        if recent:
            lines.append("【今天/最近留下的片段，供你想起】")
            for e in recent[:6]:
                t = str(e.get("text") or "").strip()
                if t:
                    who = str(e.get("who") or "").strip()
                    tag = "他说的" if who == "他" else ("我说的" if who == "我" else who)
                    lines.append(f"- {str(e.get('day') or '')}"
                                 + (f"（{tag}）" if tag else "") + f"：{t[:90]}")
        if ambient.get("present"):
            lines.append("【残响】" + str(ambient.get("text") or
                                          "有一层说不清的感觉还在，但找不到具体是哪件事。"))
        if learn.get("last_study"):
            q = (learn["last_study"] or {}).get("question") or ""
            if q:
                lines.append(f"【最近在琢磨】{q[:80]}")
        lines.append(f"【此刻】精力 {state.get('energy', 0):.2f}，"
                     f"心情 {state.get('mood', 0):+.2f}，"
                     f"一个人待着 {state.get('silent_h', 0):.1f} 小时。")
        return "\n".join(lines), {"state": state, "recent_n": len(recent),
                                  "ambient_present": bool(ambient.get("present")),
                                  "learn_pending": learn.get("pending", 0)}

    def _fallback(self, ctx: dict) -> str:
        recent = ctx.get("recent") or []
        ambient = ctx.get("ambient") or {}
        bits: list[str] = []
        for e in recent:
            t = str(e.get("text") or "").strip()
            if not t or t in bits:
                continue
            who = str(e.get("who") or "").strip()
            if who == "他":
                bits.append("你说：" + t[:72])
            else:
                bits.append(t[:80])
        lines = ["今天没有发生什么大事。"]
        if bits:
            lines.append("我还能想起几段：" + "；".join(bits[:3]) + "。")
        if ambient.get("present"):
            if ambient.get("text"):
                lines.append(str(ambient["text"]))
            elif ambient.get("imprints"):
                lines.append("有些感觉还在，但具体是哪件事，我已经说不清了。")
        lines.append("先写到这里。明天再想。")
        return "\n\n".join(lines)

    # ---- 生成 -------------------------------------------------------------
    def generate(self, day: Optional[str] = None, *, force: bool = False,
                 now: Optional[float] = None, use_llm: Optional[bool] = None) -> dict:
        """生成并写入一天。已存在且 force=False 时直接返回旧日记。"""
        now = now if now is not None else self.clock()
        day = day or _day_of(now)
        with self._lock:
            if self.path_for(day).is_file() and not force:
                return {**self.read(day), "skipped": True}
            ctx = self._context(day, now)
            prompt, meta_ctx = self._prompt(ctx)
            text = ""
            source = "fallback"
            model = ""
            err = ""
            use_llm = self.use_llm if use_llm is None else bool(use_llm)
            if use_llm:
                try:
                    from .llm import LLM
                    from .persona import Persona
                    # 只读 Persona，不跑 Chat.compose：compose 会做 recall，
                    # recall 会 used+1，等于“写日记”偷偷改了记忆的召回权重。
                    try:
                        base = Persona().block("core")
                    except Exception:
                        base = ""
                    system = (
                        (base + "\n\n" if base else "")
                        + "现在是晚上十一点。你在写只给自己看的日记。\n"
                        + prompt
                        + "\n\n只输出日记正文，不要标题，不要解释任务，不要加引号。"
                    )
                    llm = LLM(system_prompt=system, history_limit=0,
                              max_tokens=700, temperature=0.88)
                    ok, why = llm.available()
                    if ok:
                        r = llm.chat("写今天的日记。", system=system)
                        text = _clean((r or {}).get("text") or "")
                        source = "llm"
                        model = str(getattr(llm, "model", "") or "")
                    else:
                        err = why
                except Exception as e:
                    err = f"{type(e).__name__}: {e}"
            if not text:
                text = _clean(self._fallback(ctx))
                source = "fallback"
            if not text:
                raise RuntimeError("日记生成失败：模型和兜底都没有内容")
            if len(text) > 1600:
                text = text[:1600].rstrip("，,、；; \n") + "…"
            meta = {**meta_ctx, "model": model, "error": err}
            res = self.write(text, day=day, source=source, meta=meta, now=now)
            return {**res, "skipped": False}

    def ensure_today(self, *, force: bool = False, now: Optional[float] = None,
                     use_llm: Optional[bool] = None) -> dict:
        now = now if now is not None else self.clock()
        day = _day_of(now)
        lock = self.root / f".{day}.lock"
        if self.path_for(day).is_file() and not force:
            return {**self.read(day), "skipped": True}
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
        except FileExistsError:
            # 另一个进程正在写；稍等后读它写好的文件；超过 10 分钟视为陈旧锁。
            try:
                if time.time() - lock.stat().st_mtime > 600:
                    lock.unlink(missing_ok=True)
            except Exception:
                pass
            return {"ok": False, "day": day, "locked": True,
                    "error": "另一个进程正在写今天的日记"}
        try:
            return self.generate(day=day, force=force, now=now, use_llm=use_llm)
        finally:
            try:
                lock.unlink(missing_ok=True)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# 定时器
# ---------------------------------------------------------------------------
class DiaryScheduler:
    """每 30 秒看一次：23:00 之后、今天还没有日记，就让她写。

    服务若在 23:00 之后才启动，第一次 tick 会立刻补写当天；
    23:00 之前启动则等到点。它只负责触发，不重复覆盖已有文件。
    """

    def __init__(self, diary: Optional[Diary] = None,
                 hour: int = DIARY_HOUR, minute: int = DIARY_MINUTE,
                 interval: float = 30.0) -> None:
        self.diary = diary or Diary()
        self.hour = int(hour)
        self.minute = int(minute)
        self.interval = max(5.0, float(interval))
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.last: dict = {}

    def due(self, now: Optional[float] = None) -> bool:
        now = now if now is not None else self.diary.clock()
        d = datetime.fromtimestamp(now)
        return (d.hour, d.minute) >= (self.hour, self.minute)

    def tick(self, now: Optional[float] = None) -> dict:
        now = now if now is not None else self.diary.clock()
        if not self.due(now):
            return {"ok": True, "due": False, "day": _day_of(now)}
        day = _day_of(now)
        if self.diary.path_for(day).is_file():
            return {"ok": True, "due": True, "day": day, "exists": True}
        try:
            res = self.diary.ensure_today(now=now)
            self.last = res
            return {"ok": True, "due": True, **res}
        except Exception as e:
            self.last = {"ok": False, "day": day,
                         "error": f"{type(e).__name__}: {e}"}
            return self.last

    def _run(self) -> None:
        # 先 tick 一次：服务 23:00 后才启动时，不等 30 秒。
        try:
            self.tick()
        except Exception:
            pass
        while not self._stop.wait(self.interval):
            try:
                self.tick()
            except Exception:
                pass

    def start(self) -> "DiaryScheduler":
        if self._thread and self._thread.is_alive():
            return self
        self._thread = threading.Thread(
            target=self._run, name="diary-scheduler", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()


_SCHEDULER: Optional[DiaryScheduler] = None
_SCHED_LOCK = threading.Lock()


def start_scheduler(diary: Optional[Diary] = None) -> DiaryScheduler:
    """进程内只启动一次；sihai 和 pocket_server 都可安全调用。"""
    global _SCHEDULER
    with _SCHED_LOCK:
        if _SCHEDULER is None:
            _SCHEDULER = DiaryScheduler(diary=diary).start()
        return _SCHEDULER


def _print(res: dict) -> None:
    print(json.dumps(res, ensure_ascii=False, indent=2))


def main(argv: Optional[list[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="app.diary", description="她的日记")
    ap.add_argument("--now", action="store_true", help="立刻写今天（已存在则跳过）")
    ap.add_argument("--force", action="store_true", help="覆盖今天已有的日记")
    ap.add_argument("--list", action="store_true", help="列出最近日记")
    ap.add_argument("--show", type=str, default=None, help="读某天 YYYY-MM-DD")
    ap.add_argument("--no-llm", action="store_true", help="只走本地兜底，不调模型")
    a = ap.parse_args(argv)
    d = Diary(use_llm=not a.no_llm)
    if a.list:
        _print({"ok": True, "days": d.list()})
    elif a.show:
        _print(d.read(a.show))
    else:
        _print(d.ensure_today(force=a.force, use_llm=not a.no_llm))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
