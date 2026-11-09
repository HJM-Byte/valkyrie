"""备份瓦尔基里的真实状态（A4.3 切真实记忆前用）。

数据库走 sqlite3 的在线 backup API，能拿到一致快照，不会因为
pocket_server 正在写而拷到半个事务。JSON / JSONL 直接复制。
每份文件都记 SHA256，写进 MANIFEST.json，切完可以逐项核对。

    python tools/backup_state.py
    python tools/backup_state.py --dest data/../backups/state_manual

只读源文件，不修改、不移动、不删任何真实状态。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # robot/
DATA = ROOT / "data"
DEFAULT_DEST = ROOT / "backups" / ("state_" + time.strftime("%Y%m%d_%H%M%S"))

DBS = [
    DATA / "memory.db",
    DATA / "learning" / "learn.db",
    DATA / "proactive.db",
]

FILES = [
    ROOT / "PERSONA.md",
    DATA / "flow.json",
    DATA / "sleep.json",
    DATA / "sihai_footprint.jsonl",
    DATA / "sleep_log.jsonl",
    DATA / "faces.json",
    DATA / "learning" / "channels.json",
    DATA / "learning" / "footprint.jsonl",
    DATA / "learning" / "bili_queue.json",
]


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def copy_sqlite(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    src_conn = sqlite3.connect(str(src))
    try:
        dst_conn = sqlite3.connect(str(dst))
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="备份瓦尔基里真实状态")
    ap.add_argument("--dest", default=str(DEFAULT_DEST), help="备份目录")
    args = ap.parse_args(argv)
    dest = Path(args.dest)
    if dest.exists() and any(dest.iterdir()):
        print(f"目标已存在且非空，换一个：{dest}")
        return 2
    dest.mkdir(parents=True, exist_ok=True)

    items: list[dict] = []
    for src in DBS:
        if not src.exists():
            items.append({"src": str(src), "kind": "sqlite", "exists": False})
            continue
        rel = src.relative_to(ROOT)
        dst = dest / rel
        copy_sqlite(src, dst)
        items.append({"src": str(src), "dst": str(dst), "kind": "sqlite",
                      "exists": True, "bytes": dst.stat().st_size, "sha256": sha256(dst)})
        print(f"db   {rel}  {dst.stat().st_size:,} B")

    for src in FILES:
        if not src.exists():
            items.append({"src": str(src), "kind": "file", "exists": False})
            continue
        rel = src.relative_to(ROOT)
        dst = dest / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        items.append({"src": str(src), "dst": str(dst), "kind": "file",
                      "exists": True, "bytes": dst.stat().st_size, "sha256": sha256(dst)})
        print(f"file {rel}  {dst.stat().st_size:,} B")

    # 学习书这类目录整体带走（排除 WAL/SHM）。
    for sub in (DATA / "learning" / "books",):
        if not sub.exists():
            continue
        for src in sorted(sub.rglob("*")):
            if not src.is_file() or src.suffix in (".db-wal", ".db-shm"):
                continue
            rel = src.relative_to(ROOT)
            dst = dest / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            items.append({"src": str(src), "dst": str(dst), "kind": "file",
                          "exists": True, "bytes": dst.stat().st_size, "sha256": sha256(dst)})
            print(f"file {rel}  {dst.stat().st_size:,} B")

    manifest = {
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "workspace": str(ROOT),
        "dest": str(dest),
        "items": items,
    }
    (dest / "MANIFEST.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    ok = sum(1 for i in items if i.get("exists"))
    print(f"\n完成：{ok} 份，清单 {dest / 'MANIFEST.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
