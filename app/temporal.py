"""时间锚点：给她一条能落到具体日期的线。

之前的「现在」只有一行日期，模型知道今天几号，却不一定知道
「昨天是几号」「上周三是哪天」，也看不到最近几轮对话发生在什么时候。
这里把「此刻 + 昨天/前天/本周 + 最近对话」压成一段短提示，交给 Persona。

只做格式化，不写库，不依赖 Memory 的具体实现——传进来的 mem 只要有
recent_turns(n) 就能用。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

try:
    from zoneinfo import ZoneInfo

    TZ = ZoneInfo("Asia/Shanghai")
    TZ_NAME = "Asia/Shanghai"
except Exception:  # Windows 没装 tzdata 时退回固定 UTC+8（中国无夏令时）
    TZ = timezone(timedelta(hours=8))
    TZ_NAME = "Asia/Shanghai (UTC+8)"

_WEEK = "一二三四五六日"


def now_local() -> datetime:
    """当前本地时间；有时区就用带时区的，没有就用系统本地时区。"""
    if TZ is not None:
        return datetime.now(TZ)
    return datetime.now().astimezone()


def local_dt(ts: float) -> datetime:
    """时间戳 -> 本地 datetime。"""
    if TZ is not None:
        return datetime.fromtimestamp(float(ts), TZ)
    return datetime.fromtimestamp(float(ts)).astimezone()


def _w(dt: datetime) -> str:
    return _WEEK[dt.weekday()]


def _date_text(dt: datetime) -> str:
    return f"{dt:%Y-%m-%d} 星期{_w(dt)}"


def relative_day(ts: float, now: Optional[datetime] = None) -> str:
    """返回「今天 / 昨天 / 前天 / N天前 / 具体日期」。"""
    now = now or now_local()
    d = local_dt(ts).date()
    today = now.date()
    delta = (d - today).days
    if delta == 0:
        return "今天"
    if delta == -1:
        return "昨天"
    if delta == -2:
        return "前天"
    if -14 <= delta < 0:
        return f"{abs(delta)}天前"
    return f"{d:%Y-%m-%d}"


def day_stamp(ts: float, now: Optional[datetime] = None) -> str:
    """给记忆条目用的日期戳：2026-11-02（昨天，周一）。"""
    dt = local_dt(ts)
    return f"{dt:%Y-%m-%d}（{relative_day(ts, now)}，周{_w(dt)}）"


def clock_stamp(ts: float, now: Optional[datetime] = None) -> str:
    """给最近对话用的短戳：今天 22:03 / 昨天 18:20 / 11-01 09:10（前天）。"""
    now = now or now_local()
    dt = local_dt(ts)
    rel = relative_day(ts, now)
    if rel == "今天":
        return f"今天 {dt:%H:%M}"
    if rel in ("昨天", "前天"):
        return f"{rel} {dt:%H:%M}"
    return f"{dt:%m-%d} {dt:%H:%M}（{rel}）"


def anchor_lines(now: Optional[datetime] = None) -> str:
    """此刻与前后日期的锚点，防止她把「昨天」算错。"""
    now = now or now_local()
    today = now.date()
    yest = today - timedelta(days=1)
    before = today - timedelta(days=2)
    week_start = today - timedelta(days=today.weekday())
    last_week_start = week_start - timedelta(days=7)
    last_week_end = last_week_start + timedelta(days=6)

    def dtext(d) -> str:
        return f"{d:%Y-%m-%d} 星期{_WEEK[d.weekday()]}"

    return "\n".join([
        f"现在是 {dtext(today)} {now:%H:%M}（{TZ_NAME}）。",
        f"今天 {dtext(today)}；昨天 {dtext(yest)}；前天 {dtext(before)}。",
        f"本周一 {dtext(week_start)}；上周一 {dtext(last_week_start)}，"
        f"上周日 {dtext(last_week_end)}。",
        "他说“那天/上次/前几天”时，先把相对说法落到具体日期，再回答；"
        "不确定就直说记不清，不要猜。",
    ])


def recent_turns_block(mem: Any, now: Optional[datetime] = None,
                       limit: int = 6) -> str:
    """最近几轮对话，带时间戳；没有就返回空串。

    只读，不写库。text 截短，避免把整段历史塞进系统提示。
    """
    if mem is None:
        return ""
    try:
        rows = mem.recent_turns(limit)
    except Exception:
        return ""
    if not rows:
        return ""
    lines: list[str] = []
    for r in rows:
        role = str(r.get("role") or "")
        who = "他" if role == "user" else "你"
        text = " ".join(str(r.get("text") or "").split())
        if len(text) > 90:
            text = text[:90].rstrip() + "…"
        lines.append(f"- {clock_stamp(float(r.get('ts') or 0), now)} {who}：{text}")
    return "\n".join(lines)


def recent_episodes_block(mem: Any, now: Optional[datetime] = None,
                          limit: int = 4) -> str:
    """最近记下的几件事，带日期；只读，不写库。"""
    if mem is None:
        return ""
    try:
        rows = mem.recent_episodes(limit)
    except Exception:
        return ""
    if not rows:
        return ""
    lines: list[str] = []
    for r in rows:
        text = " ".join(str(r.get("text") or "").split())
        if len(text) > 80:
            text = text[:80].rstrip() + "…"
        ts = r.get("ts")
        stamp = day_stamp(float(ts), now) if ts else str(r.get("day") or "")
        lines.append(f"- {stamp}：{text}")
    return "\n".join(lines)
