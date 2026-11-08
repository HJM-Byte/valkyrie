#!/usr/bin/env python3
"""只读审计 approved 知识候选：原文类型、证据能否回到原文、当前是否可召回。

用法（在 robot 目录）：
  C:\\robot\\venv\\Scripts\\python.exe -m tools.audit_knowledge
  C:\\robot\\venv\\Scripts\\python.exe -m tools.audit_knowledge --self-check

审计只读，不 UPDATE / INSERT / DELETE 任何表。
当前库 approve() 会把 note 覆盖成“已审核”，原始的 via 会丢失；
默认从审核前备份 recover_via，若备份不在则把 via 标成 unknown，不猜。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "learning" / "learn.db"
DEFAULT_VIA_BACKUP = ROOT / "backups" / "learn_review_20261106_160935" / "learn.db"
DEFAULT_JSON = ROOT / "data" / "acceptance" / "knowledge_audit_20261107.json"
DEFAULT_MD = ROOT / "data" / "acceptance" / "knowledge_audit_20261107.md"

ALLOWED_RAW = {"book", "article", "page", "api_extract"}
RAW_BY_VIA = {
    "local": "book",
    "article": "article",
    "search": "article",
    "self_study": "article",
    "baike": "page",
    "api": "api_extract",
    "feed": "feed",
    "snippet": "summary",
    "bili_desc": "summary",
    "bili_rank": "summary",
    "dy_hot": "summary",
}
VIA_RE = re.compile(r"via\s*=\s*([a-z_]+)", re.I)


def normalize_evidence(text: str) -> str:
    """NFC + 连续空白折叠为单空格；不删标点、不做同义改写。"""
    s = unicodedata.normalize("NFC", str(text or ""))
    return re.sub(r"\s+", " ", s).strip()


def sha256_text(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()


def _open_ro(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    return con


def _load_via_backup(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    con = _open_ro(path)
    try:
        out: dict[str, str] = {}
        for row in con.execute("SELECT id, note FROM candidates"):
            m = VIA_RE.search(str(row["note"] or ""))
            if m:
                out[str(row["id"])] = m.group(1).lower()
        return out
    finally:
        con.close()


def _via_from_note(note: str) -> str:
    m = VIA_RE.search(str(note or ""))
    return m.group(1).lower() if m else ""


def _infer_via(row: sqlite3.Row) -> str:
    channel = str(row["channel"] or "").lower()
    url = str(row["url"] or "")
    if channel == "books" or url.startswith("file:///"):
        return "local"
    if channel == "self_study":
        return "baike" if "baike.baidu.com" in url else "self_study"
    if channel == "web_search":
        return "search"
    # RSS/未知渠道不能从 channel 单独断定是 feed 还是 article；留空，不猜。
    return ""


def _raw_kind(via: str) -> str:
    return RAW_BY_VIA.get(via, "unknown")


def _locator(url: str) -> tuple[str, bool]:
    url = str(url or "").strip()
    if not url:
        return "", False
    if url.startswith("file:///"):
        rel = url[len("file:///"):]
        rel = rel.split("?", 1)[0].split("#", 1)[0]
        p = ROOT / "data" / "learning" / "books" / Path(rel).name
        return str(p), p.is_file()
    if url.startswith(("http://", "https://")):
        return url, True
    return url, False


def _parse_points(raw: Any) -> list[dict]:
    if isinstance(raw, list):
        return [p for p in raw if isinstance(p, dict)]
    if not raw:
        return []
    try:
        data = json.loads(str(raw))
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    return [p for p in data if isinstance(p, dict)]


def _check_points(text: str, points: list[dict]) -> list[dict]:
    raw_norm = normalize_evidence(text)
    checks: list[dict] = []
    for i, p in enumerate(points, 1):
        claim = str(p.get("claim") or "").strip()
        evidence = str(p.get("evidence") or "").strip()
        ev_norm = normalize_evidence(evidence)
        if not claim:
            matched, reason = False, "empty_claim"
        elif not ev_norm:
            matched, reason = False, "empty_evidence"
        elif not raw_norm:
            matched, reason = False, "empty_raw"
        elif ev_norm in raw_norm:
            matched, reason = True, "substring"
        else:
            matched, reason = False, "not_substring"
        checks.append({
            "index": i,
            "claim": claim,
            "evidence": evidence,
            "evidence_norm": ev_norm,
            "matched": matched,
            "reason": reason,
        })
    return checks


def _classify(raw_kind: str, locator_ok: bool, points_n: int,
              checks: list[dict]) -> tuple[str, str]:
    all_matched = bool(checks) and all(c["matched"] for c in checks)
    if raw_kind in ALLOWED_RAW and locator_ok and points_n > 0 and all_matched:
        return "pass", "证据为原文连续子串，可参与 dispatch"
    if raw_kind not in ALLOWED_RAW:
        return "unrepairable", (
            f"raw_kind={raw_kind} 不是原文/正文，不能把当前 text 当证据；"
            "需要重新抓正文或重新选题"
        )
    if not locator_ok:
        return "unrepairable", "没有可定位来源，无法回到原文"
    if points_n <= 0:
        return "repairable", "原文可用但 points 为空，需要重新提炼并逐条校证据"
    return "repairable", "原文可用但 evidence 不是原文连续子串，需要重写 points"


def audit(db: Path, via_backup: Path) -> dict:
    backup_vias = _load_via_backup(via_backup)
    con = _open_ro(db)
    try:
        rows = con.execute(
            "SELECT id,status,channel,title,url,text,points,q_recon,note,"
            "via,raw_kind,raw_sha1,evidence_status "
            "FROM candidates WHERE status IN ('approved','reference') "
            "ORDER BY status,id"
        ).fetchall()
    finally:
        con.close()

    items: list[dict] = []
    for row in rows:
        d = dict(row)
        text = str(d.get("text") or "")
        points = _parse_points(d.get("points"))
        via = str(d.get("via") or "") or backup_vias.get(str(d["id"])) or _via_from_note(
            str(d.get("note") or ""))
        via_source = ("column" if d.get("via") else
                      "backup_note" if str(d["id"]) in backup_vias else
                      "current_note" if via else "")
        if not via:
            via = _infer_via(row)
            via_source = "inferred" if via else "unknown"
        raw_kind = str(d.get("raw_kind") or "") or _raw_kind(via)
        locator, locator_ok = _locator(str(d.get("url") or ""))
        checks = _check_points(text, points)
        sha_now = sha256_text(text)
        sha_ok = bool(d.get("raw_sha1")) and str(d.get("raw_sha1")) == sha_now
        if str(d.get("status")) == "reference":
            classification, recommendation = (
                "reference", "外部参考：非原文，不参与 dispatch；保留原文与审核痕迹")
        elif not sha_ok and str(d.get("status")) == "approved":
            classification, recommendation = (
                "unrepairable", "raw_sha1 与当前 text 不一致，读取时会被跳过")
        else:
            classification, recommendation = _classify(
                raw_kind, locator_ok, len(points), checks)
        eligible = (str(d.get("status")) == "approved"
                    and str(d.get("evidence_status")) == "verified"
                    and sha_ok and classification == "pass")
        items.append({
            "id": str(d["id"]),
            "status": str(d.get("status")),
            "title": str(d.get("title") or ""),
            "channel": str(d.get("channel") or ""),
            "via": via or "unknown",
            "via_source": via_source or "unknown",
            "raw_kind": raw_kind,
            "raw_sha1": sha_now,
            "raw_sha1_matches": sha_ok,
            "text_len": len(text),
            "locator": locator,
            "locator_ok": locator_ok,
            "points_n": len(points),
            "points": points,
            "evidence_checks": checks,
            "all_evidence_matched": bool(checks) and all(c["matched"] for c in checks),
            "eligible": eligible,
            "classification": classification,
            "recommendation": recommendation,
        })

    summary = {
        "approved": sum(1 for x in items if x["status"] == "approved"),
        "reference": sum(1 for x in items if x["status"] == "reference"),
        "eligible": sum(1 for x in items if x["eligible"]),
        "pass": sum(1 for x in items if x["classification"] == "pass"),
        "repairable": sum(1 for x in items if x["classification"] == "repairable"),
        "unrepairable": sum(1 for x in items if x["classification"] == "unrepairable"),
        "eligible_ids": [x["id"] for x in items if x["eligible"]],
        "reference_ids": [x["id"] for x in items if x["status"] == "reference"],
        "by_raw_kind": {},
        "by_channel": {},
    }
    for x in items:
        summary["by_raw_kind"][x["raw_kind"]] = summary["by_raw_kind"].get(x["raw_kind"], 0) + 1
        summary["by_channel"][x["channel"]] = summary["by_channel"].get(x["channel"], 0) + 1
    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source": {
            "db": str(db),
            "via_backup": str(via_backup),
            "via_backup_exists": via_backup.is_file(),
            "note": "只读审计；approve() 会覆盖 note，via 优先从审核前备份恢复，缺失则标 unknown。",
        },
        "summary": summary,
        "items": items,
    }


def _md(result: dict) -> str:
    s = result["summary"]
    lines = [
        "# 知识审计（approved + reference）",
        "",
        f"- 生成：{result['generated_at']}",
        f"- 数据库：`{result['source']['db']}`",
        f"- via 备份：`{result['source']['via_backup']}`"
        f"（{'存在' if result['source']['via_backup_exists'] else '缺失'}）",
        "",
        "## 结论",
        "",
        f"- approved：**{s['approved']}**；reference：**{s['reference']}**；"
        f"eligible：**{s['eligible']}**",
        f"- pass：**{s['pass']}**；repairable：**{s['repairable']}**；"
        f"unrepairable：**{s['unrepairable']}**",
        f"- eligible_ids：{', '.join(s['eligible_ids']) or '（空）'}",
        f"- reference_ids：{', '.join(s['reference_ids']) or '（空）'}",
        "- reference 只是外部参考，不参与 dispatch；eligible 只算 approved + verified + raw_sha1 匹配。",
        "",
        "## 逐条",
        "",
        "| id | 状态 | 标题 | via | raw_kind | points | evidence | 结论 |",
        "|---|---|---|---|---|---:|---|---|",
    ]
    for x in result["items"]:
        ev = f"{sum(1 for c in x['evidence_checks'] if c['matched'])}/{len(x['evidence_checks'])}"
        title = str(x["title"]).replace("|", "\\|")[:42]
        lines.append(
            f"| `{x['id'][:8]}` | {x['status']} | {title} | {x['via']} | "
            f"{x['raw_kind']} | {x['points_n']} | {ev} | {x['classification']} |"
        )
    lines += ["", "## 说明", ""]
    for x in result["items"]:
        if x["classification"] in ("unrepairable", "reference"):
            lines.append(f"- `{x['id'][:8]}` {x['classification']}：{x['recommendation']}")
    lines += [
        "",
        "分类口径：`pass` = 当前 eligible；`reference` = 外部参考，不参与 dispatch；"
        "`repairable` = 原文可用但 points/证据不合格；`unrepairable` = "
        "当前 text 不是原文/正文，或来源/哈希不可信。",
        "",
        "本文件只读生成，没有改 `candidates.status`、`points` 或任何表。",
        "",
    ]
    return "\n".join(lines)


def self_check() -> int:
    assert normalize_evidence("  A\n\nB  ") == "A B"
    assert normalize_evidence("记忆　会掉") == "记忆 会掉"
    assert sha256_text("x") == hashlib.sha256(b"x").hexdigest()
    assert _raw_kind("local") == "book"
    assert _raw_kind("baike") == "page"
    assert _raw_kind("feed") == "feed"
    assert _raw_kind("snippet") == "summary"
    assert _raw_kind("nope") == "unknown"
    checks = _check_points("时间过去，细节会掉。", [{"claim": "细节会掉", "evidence": "细节会掉"}])
    assert checks[0]["matched"] is True
    checks = _check_points("时间过去，细节会掉。", [{"claim": "细节会掉", "evidence": "细节会保留"}])
    assert checks[0]["matched"] is False
    assert _classify("book", True, 0, [])[0] == "repairable"
    assert _classify("feed", True, 1, [{"matched": True}])[0] == "unrepairable"
    print("audit_knowledge self-check: OK")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tools.audit_knowledge",
                                 description="只读审计 approved 知识候选")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--via-backup", type=Path, default=DEFAULT_VIA_BACKUP)
    ap.add_argument("--out-json", type=Path, default=DEFAULT_JSON)
    ap.add_argument("--out-md", type=Path, default=DEFAULT_MD)
    ap.add_argument("--self-check", action="store_true")
    a = ap.parse_args(argv)
    if a.self_check:
        return self_check()
    result = audit(a.db, a.via_backup)
    a.out_json.parent.mkdir(parents=True, exist_ok=True)
    a.out_md.parent.mkdir(parents=True, exist_ok=True)
    a.out_json.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                          encoding="utf-8")
    a.out_md.write_text(_md(result), encoding="utf-8")
    s = result["summary"]
    print(f"approved={s['approved']} reference={s['reference']} "
          f"eligible={s['eligible']} pass={s['pass']} "
          f"repairable={s['repairable']} unrepairable={s['unrepairable']}")
    print(f"json={a.out_json}")
    print(f"md={a.out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
