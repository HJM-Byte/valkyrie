#!/usr/bin/env python3
"""把 7 条原文可用的 approved 补成 verified，5 条 feed 留作 reference。

用法（在 robot 目录）：
  C:\\robot\\venv\\Scripts\\python.exe -m tools.repair_knowledge --dry-run
  C:\\robot\\venv\\Scripts\\python.exe -m tools.repair_knowledge --apply

约束：
- 先校验所有 evidence 都是原文连续子串，任何一条不过就整体不写；
- apply 前用 SQLite backup API 冷备 learn.db，失败自动回滚；
- 不删除任何行；5 条 feed 只改 status=reference，保留原文和审核痕迹。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "learning" / "learn.db"
DEFAULT_MANIFEST = ROOT / "data" / "acceptance" / "knowledge_repair_20261107.json"
BACKUPS = ROOT / "backups"
REPORT = ROOT / "data" / "acceptance" / "knowledge_repair_report_20261107.json"


def _ro(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _counts(path: Path) -> dict:
    con = _ro(path)
    try:
        out = {}
        for status in ("pending", "approved", "reference", "rejected"):
            out[status] = int(con.execute(
                "SELECT COUNT(*) FROM candidates WHERE status=?", (status,)
            ).fetchone()[0])
        out["total"] = int(con.execute(
            "SELECT COUNT(*) FROM candidates").fetchone()[0])
        return out
    finally:
        con.close()


def _backup_db(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    src_con = sqlite3.connect(str(src), timeout=15)
    dst_con = sqlite3.connect(str(dst))
    try:
        src_con.backup(dst_con)
    finally:
        dst_con.close()
        src_con.close()


def validate(db: Path, manifest: dict) -> tuple[list[dict], list[dict]]:
    """只读校验；返回 (repairs, references) 并附带逐条证据检查。"""
    from app.learning import check_points, parse_points_json

    repairs = []
    references = []
    con = _ro(db)
    try:
        for item in manifest.get("repairs") or []:
            cid = str(item.get("id") or "")
            row = con.execute(
                "SELECT * FROM candidates WHERE id=?", (cid,)).fetchone()
            if row is None:
                raise RuntimeError(f"repair {cid}: 不存在")
            row = dict(row)
            if str(row.get("status")) != "approved":
                raise RuntimeError(f"repair {cid}: status={row.get('status')}，不是 approved")
            points = item.get("points") or []
            checks = check_points(str(row.get("text") or ""), points)
            bad = [c for c in checks if not c["matched"]]
            if bad:
                raise RuntimeError(
                    f"repair {cid}: evidence 不是原文子串: {bad[:2]}")
            repairs.append({
                "id": cid,
                "title": str(row.get("title") or ""),
                "via": item.get("via") or str(row.get("via") or ""),
                "raw_kind": item.get("raw_kind") or str(row.get("raw_kind") or ""),
                "points": points,
                "checks": checks,
            })
        for item in manifest.get("references") or []:
            cid = str(item.get("id") or "")
            row = con.execute(
                "SELECT * FROM candidates WHERE id=?", (cid,)
            ).fetchone()
            if row is None:
                raise RuntimeError(f"reference {cid}: 不存在")
            row = dict(row)
            if str(row.get("status")) not in ("approved", "reference"):
                raise RuntimeError(
                    f"reference {cid}: status={row.get('status')}，不是 approved/reference")
            references.append({
                "id": cid,
                "title": str(row.get("title") or ""),
                "via": item.get("via") or "feed",
                "raw_kind": item.get("raw_kind") or "feed",
                "reason": item.get("reason") or "外部参考，不参与 dispatch",
            })
    finally:
        con.close()
    if len(repairs) != 7 or len(references) != 5:
        raise RuntimeError(f"清单数量不对：repairs={len(repairs)} references={len(references)}")
    return repairs, references


def apply(db: Path, manifest: dict, repairs: list[dict],
          references: list[dict], backup_root: Path = BACKUPS,
          report_path: Path = REPORT) -> dict:
    from app.learning import Learning
    from app.mind import GROWTH_LOCKED

    if GROWTH_LOCKED:
        raise RuntimeError("GROWTH_LOCKED=True，审核会被拒；先不要迁移")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = backup_root / f"knowledge_evidence_{stamp}"
    backup_dir.mkdir(parents=True, exist_ok=False)
    backup_db = backup_dir / "learn.db"
    _backup_db(db, backup_db)
    shutil.copy2(DEFAULT_MANIFEST, backup_dir / "knowledge_repair_20261107.json")
    before = _counts(db)
    (backup_dir / "before.json").write_text(
        json.dumps({"before": before, "db": str(db)}, ensure_ascii=False, indent=2),
        encoding="utf-8")

    L = Learning(db_path=db)
    results = []
    try:
        for r in repairs:
            s = L.set_points(r["id"], r["points"], via=r["via"],
                             raw_kind=r["raw_kind"],
                             note="2.2 人工补点，approve 时逐条校证据")
            if not s.get("ok"):
                raise RuntimeError(f"set_points {r['id']}: {s}")
            a = L.approve(r["id"])
            if not a.get("ok"):
                raise RuntimeError(f"approve {r['id']}: {a}")
            results.append({"id": r["id"], "kind": "repair",
                            "evidence": a.get("evidence"),
                            "points": a.get("points")})
        for ref in references:
            m = L.mark_reference(ref["id"], ref["reason"],
                                 via=ref["via"], raw_kind=ref["raw_kind"])
            if not m.get("ok"):
                raise RuntimeError(f"mark_reference {ref['id']}: {m}")
            results.append({"id": ref["id"], "kind": "reference",
                            "status": m.get("status"), "raw_kind": m.get("raw_kind")})
        after = _counts(db)
        stats = L.stats()
        report = {
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "db": str(db),
            "backup": str(backup_db),
            "before": before,
            "after": after,
            "approved": stats.get("approved"),
            "reference": stats.get("reference"),
            "eligible": stats.get("eligible"),
            "repairs": [x for x in results if x["kind"] == "repair"],
            "references": [x for x in results if x["kind"] == "reference"],
        }
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                               encoding="utf-8")
        (backup_dir / "after_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
        return report
    except Exception:
        L.close()
        _backup_db(backup_db, db)
        raise
    finally:
        L.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tools.repair_knowledge",
                                 description="7 条补证 + 5 条 feed 留作参考")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    ap.add_argument("--apply", action="store_true", help="真正写库；默认只校验")
    ap.add_argument("--dry-run", action="store_true", help="只校验（默认行为）")
    ap.add_argument("--backup-root", type=Path, default=BACKUPS)
    ap.add_argument("--report", type=Path, default=REPORT)
    a = ap.parse_args(argv)

    manifest = json.loads(a.manifest.read_text(encoding="utf-8"))
    repairs, references = validate(a.db, manifest)
    print(f"校验通过：repairs={len(repairs)} references={len(references)}")
    for r in repairs:
        print(f"  repair  {r['id']}  {r['raw_kind']:<6}  {len(r['checks'])} 条证据")
    for r in references:
        print(f"  reference {r['id']}  {r['title'][:34]}")
    if not a.apply:
        print("未写库（--apply 才写）")
        return 0
    report = apply(a.db, manifest, repairs, references,
                   backup_root=a.backup_root, report_path=a.report)
    print(json.dumps({k: report.get(k) for k in
                      ("before", "after", "approved", "reference", "eligible")},
                     ensure_ascii=False))
    print(f"报告: {a.report}")
    print(f"备份: {report['backup']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
