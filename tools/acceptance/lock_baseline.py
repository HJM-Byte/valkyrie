"""锁定施工前基线：文档哈希 + 代码哈希 + 运行时事实 + 存档指纹。

生成 `docs/design_baseline/LOCK.json`；`--check` 只校验文档、代码和存档，
不把会持续变化的 live 数据库当作锁。

用法：
    python -m tools.acceptance.lock_baseline
    python -m tools.acceptance.lock_baseline --check
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

try:
    from . import baseline_facts
except ImportError:  # 直接按文件运行时
    import baseline_facts  # type: ignore


ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs" / "design_baseline"
DEFAULT_OUT = DOCS / "LOCK.json"

DOC_FILES = [
    DOCS / "BASELINE.md",
    DOCS / "CHANGELOG.md",
    DOCS / "README.md",
    DOCS / "PREWORK_FIXES_20261107.md",
    DOCS / "source" / "V1_research_20261106.docx",
    DOCS / "source" / "V1_research_20261106.md",
    DOCS / "source" / "V2_implementation_20261107.docx",
    DOCS / "source" / "V2_implementation_20261107.md",
    DOCS / "source" / "V3_longterm_20261107.docx",
    DOCS / "source" / "V3_longterm_20261107.md",
    ROOT / "docs" / "瓦尔基里施工方案_20261107.md",
]


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def file_entry(path: Path) -> dict:
    return {
        "path": str(path.relative_to(ROOT)).replace("\\", "/"),
        "bytes": path.stat().st_size,
        "sha256": sha256(path),
    }


def _code_files() -> list[Path]:
    files = sorted((ROOT / "app").glob("*.py"))
    files += sorted((ROOT / "tools").glob("selftest_*.py"))
    return files


def _snapshot_info() -> dict:
    backups = ROOT / "backups"
    dirs = sorted(
        p for p in backups.glob("snapshot_*") if p.is_dir()
    ) if backups.exists() else []
    if not dirs:
        return {"path": None}
    snap = dirs[-1]
    zip_path = snap.with_suffix(".zip")
    info = {"path": str(snap), "zip": str(zip_path) if zip_path.exists() else None}
    for name in ("SNAPSHOT.md", "MANIFEST.sha256"):
        p = snap / name
        if p.exists():
            info[name] = {"bytes": p.stat().st_size, "sha256": sha256(p)}
    if zip_path.exists():
        info["zip_sha256"] = sha256(zip_path)
        info["zip_bytes"] = zip_path.stat().st_size
    return info


def build_lock() -> dict:
    facts = baseline_facts.collect()
    docs = [file_entry(p) for p in DOC_FILES if p.exists()]
    code = [file_entry(p) for p in _code_files()]
    snapshot = _snapshot_info()
    lock_id = datetime.now().strftime("valkyrie-lock-%Y%m%d-%H%M%S")
    return {
        "lock_id": lock_id,
        "baseline_id": "valkyrie-baseline-20261107",
        "locked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "status": "locked",
        "scope": {
            "documents": "V1/V2/V3、BASELINE、CHANGELOG、README、PREWORK_FIXES、施工方案",
            "code": "robot/app/*.py 与 robot/tools/selftest_*.py 的施工前哈希",
            "runtime": "GROWTH_LOCKED、服务命令行、端口、配置键",
            "data": "只记录计数与 live 哈希；不可变副本见 snapshot",
            "not_locked": [
                "live memory.db / learn.db 会继续被 8766 写入",
                "机器人模型、人脸库、密钥、日志",
                "tools/acceptance/ 自身",
            ],
        },
        "documents": docs,
        "code": code,
        "runtime": facts["runtime"],
        "config": facts["config"],
        "databases": facts["databases"],
        "snapshot": snapshot,
        "counts": {
            "documents": len(docs),
            "code": len(code),
        },
    }


def check(out: Path) -> int:
    if not out.exists():
        print(f"LOCK 不存在: {out}", file=sys.stderr)
        return 2
    lock = json.loads(out.read_text(encoding="utf-8"))
    problems: list[str] = []

    def compare(group: str, old_entries: list[dict]) -> None:
        old = {e["path"]: e["sha256"] for e in old_entries}
        current_paths = ([p for p in DOC_FILES if p.exists()] if group == "documents"
                         else _code_files())
        current = {
            str(p.relative_to(ROOT)).replace("\\", "/"): sha256(p)
            for p in current_paths
        }
        for path, digest in old.items():
            if path not in current:
                problems.append(f"{group} 缺失: {path}")
            elif current[path] != digest:
                problems.append(f"{group} 已改: {path}")
        for path in current:
            if path not in old:
                problems.append(f"{group} 新增: {path}")

    compare("documents", lock.get("documents", []))
    compare("code", lock.get("code", []))

    snap = lock.get("snapshot") or {}
    zip_path = Path(snap["zip"]) if snap.get("zip") else None
    if zip_path and zip_path.exists() and snap.get("zip_sha256"):
        if sha256(zip_path) != snap["zip_sha256"]:
            problems.append(f"存档 zip 已改: {zip_path}")
    elif zip_path and not zip_path.exists():
        problems.append(f"存档 zip 缺失: {zip_path}")

    if problems:
        print("LOCK 校验失败：", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    print(f"LOCK OK  {lock.get('lock_id')}  "
          f"documents={len(lock.get('documents', []))} "
          f"code={len(lock.get('code', []))}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="锁定施工前基线")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--check", action="store_true", help="只校验，不重写")
    ap.add_argument("--force", action="store_true", help="覆盖已有 LOCK")
    args = ap.parse_args(argv)
    out = Path(args.out)
    if args.check:
        return check(out)
    if out.exists() and not args.force:
        print(f"已存在: {out}；加 --force 覆盖", file=sys.stderr)
        return 2
    lock = build_lock()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(lock, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "out": str(out),
        "lock_id": lock["lock_id"],
        "documents": lock["counts"]["documents"],
        "code": lock["counts"]["code"],
        "growth_locked": lock["config"]["growth_locked"],
        "memory_episodes": lock["databases"]["memory"]["counts"].get("episodes"),
        "learn_approved": lock["databases"]["learn"]["counts"].get("approved"),
        "learn_approved_empty_points": lock["databases"]["learn"]["counts"].get(
            "approved_empty_points"),
        "snapshot": lock["snapshot"].get("path"),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
