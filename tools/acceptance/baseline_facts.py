"""只读采集施工前事实，产出 baseline_YYYYMMDD.json。

原则：只读，不启动服务、不调用 Chat/LLM、不写生产库。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
OUT_DIR = DATA / "acceptance"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _connect_ro(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise FileNotFoundError(path)
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    return db


def _table_count(db: sqlite3.Connection, table: str) -> int | None:
    try:
        return int(db.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
    except sqlite3.Error:
        return None


def _table_counts(path: Path) -> dict[str, int | None]:
    if not path.exists():
        return {}
    db = _connect_ro(path)
    try:
        tables = [
            r[0] for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        return {t: _table_count(db, t) for t in tables}
    finally:
        db.close()


def _memory_counts(path: Path) -> dict:
    if not path.exists():
        return {}
    db = _connect_ro(path)
    try:
        out = {
            "episodes": _table_count(db, "episodes"),
            "turns": _table_count(db, "turns"),
            "facts": _table_count(db, "facts"),
            "stage": {},
            "status": {},
        }
        for col in ("stage", "status"):
            try:
                out[col] = {
                    str(r[0]): int(r[1])
                    for r in db.execute(
                        f"SELECT {col}, COUNT(*) FROM episodes GROUP BY {col}"
                    )
                }
            except sqlite3.Error:
                out[col] = {}
        return out
    finally:
        db.close()


def _points_empty(points: str | None) -> bool:
    if points is None:
        return True
    text = str(points).strip()
    if not text:
        return True
    try:
        val = json.loads(text)
    except Exception:
        return False
    if isinstance(val, list):
        return len(val) == 0
    if isinstance(val, dict):
        for key in ("points", "claims", "items"):
            if isinstance(val.get(key), list):
                return len(val[key]) == 0
        return len(val) == 0
    return False


def _learn_counts(path: Path) -> dict:
    if not path.exists():
        return {}
    db = _connect_ro(path)
    try:
        out: dict = {
            "candidates": _table_count(db, "candidates"),
            "curiosities": _table_count(db, "curiosities"),
            "study_sessions": _table_count(db, "study_sessions"),
            "approved": 0,
            "rejected": 0,
            "pending": 0,
            "approved_empty_points": 0,
            "approved_nonempty_points": 0,
            "curiosities_status": {},
        }
        try:
            for r in db.execute(
                "SELECT status, COUNT(*) FROM candidates GROUP BY status"
            ):
                key = str(r[0])
                out[key] = int(r[1])
        except sqlite3.Error:
            pass
        try:
            for r in db.execute(
                "SELECT points FROM candidates WHERE status='approved'"
            ):
                if _points_empty(r[0]):
                    out["approved_empty_points"] += 1
                else:
                    out["approved_nonempty_points"] += 1
        except sqlite3.Error:
            pass
        try:
            out["curiosities_status"] = {
                str(r[0]): int(r[1])
                for r in db.execute(
                    "SELECT status, COUNT(*) FROM curiosities GROUP BY status"
                )
            }
        except sqlite3.Error:
            pass
        return out
    finally:
        db.close()


def _runtime() -> dict:
    ps = (
        "$ErrorActionPreference='SilentlyContinue';"
        "$p = Get-CimInstance Win32_Process | "
        "Where-Object { $_.Name -in @('python.exe','pythonw.exe') } | "
        "Select-Object ProcessId,ParentProcessId,Name,CommandLine;"
        "$c = Get-NetTCPConnection -State Listen | "
        "Where-Object { $_.LocalPort -in @(8766,8768) } | "
        "Select-Object LocalAddress,LocalPort,OwningProcess;"
        "[pscustomobject]@{processes=@($p);ports=@($c)} | ConvertTo-Json -Compress"
    )
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=15,
        )
        raw = proc.stdout.strip()
        if not raw:
            return {"error": "no output", "stderr": proc.stderr[-500:]}
        data = json.loads(raw)
        # 不把采集器自己写进运行时事实。
        data["processes"] = [
            p for p in (data.get("processes") or [])
            if "tools.acceptance" not in str(p.get("CommandLine") or "")
        ]
        return data
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def _config_facts() -> dict:
    cfg = read_text(ROOT / "app" / "config.py")
    mind = read_text(ROOT / "app" / "mind.py")
    growth = re.search(r"^GROWTH_LOCKED\s*=\s*(True|False)\s*$", mind, re.M)
    vision = re.search(r"^VISION_LOCKED\s*=\s*(True|False)\s*$", cfg, re.M)
    budget_keys = {
        key: bool(re.search(rf"^\s*{key}\s*=", cfg, re.M))
        for key in ("ROBOT_RESEARCH_DAILY_TOKENS",
                    "ROBOT_BUDGET_TZ")
    }
    return {
        "growth_locked": None if not growth else growth.group(1) == "True",
        "vision_locked": None if not vision else vision.group(1) == "True",
        "token_budget_config_present": budget_keys,
        "config_sha256": sha256(ROOT / "app" / "config.py"),
        "mind_sha256": sha256(ROOT / "app" / "mind.py"),
    }


def collect() -> dict:
    runtime = _runtime()
    memory_path = DATA / "memory.db"
    learn_path = DATA / "learning" / "learn.db"
    proactive_path = DATA / "proactive.db"
    return {
        "collected_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "workspace": str(ROOT.parent),
        "robot_root": str(ROOT),
        "config": _config_facts(),
        "databases": {
            "memory": {
                "path": str(memory_path),
                "exists": memory_path.exists(),
                "bytes": memory_path.stat().st_size if memory_path.exists() else None,
                "sha256": sha256(memory_path) if memory_path.exists() else None,
                "counts": _memory_counts(memory_path),
            },
            "learn": {
                "path": str(learn_path),
                "exists": learn_path.exists(),
                "bytes": learn_path.stat().st_size if learn_path.exists() else None,
                "sha256": sha256(learn_path) if learn_path.exists() else None,
                "counts": _learn_counts(learn_path),
            },
            "proactive": {
                "path": str(proactive_path),
                "exists": proactive_path.exists(),
                "bytes": proactive_path.stat().st_size if proactive_path.exists() else None,
                "sha256": sha256(proactive_path) if proactive_path.exists() else None,
                "tables": _table_counts(proactive_path),
            },
        },
        "runtime": runtime,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="只读采集施工前事实")
    ap.add_argument("--out", default="")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args(argv)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = Path(args.out) if args.out else OUT_DIR / f"baseline_{datetime.now():%Y%m%d}.json"
    if out.exists() and not args.force:
        print(f"已存在: {out}；加 --force 覆盖", file=sys.stderr)
        return 2
    facts = collect()
    out.write_text(json.dumps(facts, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "out": str(out),
        "growth_locked": facts["config"]["growth_locked"],
        "memory_episodes": facts["databases"]["memory"]["counts"].get("episodes"),
        "learn_approved": facts["databases"]["learn"]["counts"].get("approved"),
        "learn_approved_empty_points": facts["databases"]["learn"]["counts"].get(
            "approved_empty_points"),
        "curiosities_open": facts["databases"]["learn"]["counts"].get(
            "curiosities_status", {}).get("open"),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
