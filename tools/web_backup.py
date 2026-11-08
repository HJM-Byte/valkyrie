"""Keep a small, verified backup of the web/data layer.

This is deliberately narrower than tools/backup_state.py: it backs up the
live memory databases plus the web shell and launchers, not the learning
books or school data. Run it once a day from the watchdog; it is read-only
against the live databases and uses SQLite's online backup API.

    python tools/web_backup.py --due
    python tools/web_backup.py --force
    python tools/web_backup.py --force --dest data/acceptance/tmp/web_backup_test
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # robot/
DATA = ROOT / "data"
DEFAULT_DEST = ROOT / "backups" / "web_auto"
STAMP = DATA / "web_backup_last.json"
LOG = DATA / "web_backup.log"

DBS = [
    DATA / "memory.db",
    DATA / "p3.db",
    DATA / "proactive.db",
    DATA / "learning" / "learn.db",
]

WEB_FILES = [
    ROOT / "app" / "pocket_web",
    ROOT / "app" / "pocket_server.py",
    ROOT / "PERSONA.md",
    ROOT / "start.cmd",
    ROOT / "web.cmd",
    ROOT / "tools" / "server_watchdog.ps1",
    ROOT / "tools" / "server_watchdog_loop.ps1",
    ROOT / "tools" / "install_web_persistence.ps1",
    ROOT / "tools" / "web_backup.py",
    DATA / "pocket_token.txt",
]


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _db_facts(path: Path) -> dict:
    out = {"exists": path.exists()}
    if not path.exists():
        return out
    out["bytes"] = path.stat().st_size
    out["sha256"] = sha256(path)
    try:
        conn = sqlite3.connect(str(path))
        try:
            out["integrity"] = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
            out["user_version"] = int(conn.execute("PRAGMA user_version").fetchone()[0])
            try:
                out["episodes"] = int(conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0])
            except sqlite3.Error:
                pass
        finally:
            conn.close()
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def copy_sqlite(src: Path, dst: Path) -> dict:
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
    facts = _db_facts(dst)
    facts["dst"] = str(dst)
    return facts


def copy_file(src: Path, dst: Path) -> dict:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return {"src": str(src), "dst": str(dst), "bytes": dst.stat().st_size,
            "sha256": sha256(dst)}


def write_stamp(dest: Path, manifest: dict) -> None:
    STAMP.parent.mkdir(parents=True, exist_ok=True)
    STAMP.write_text(json.dumps({
        "date": datetime.now().strftime("%Y-%m-%d"),
        "at": datetime.now().isoformat(timespec="seconds"),
        "dest": str(dest),
        "files": len(manifest.get("files", [])),
        "databases": len(manifest.get("databases", [])),
    }, ensure_ascii=False, indent=2), encoding="utf-8")


def due() -> bool:
    if not STAMP.exists():
        return True
    try:
        data = json.loads(STAMP.read_text(encoding="utf-8"))
        return str(data.get("date") or "") != datetime.now().strftime("%Y-%m-%d")
    except Exception:
        return True


def prune(dest_root: Path, keep: int) -> list[str]:
    if keep <= 0 or not dest_root.exists():
        return []
    dirs = sorted((p for p in dest_root.iterdir() if p.is_dir() and p.name[:4].isdigit()),
                  key=lambda p: p.name, reverse=True)
    removed = []
    for old in dirs[keep:]:
        shutil.rmtree(old, ignore_errors=True)
        removed.append(str(old))
    return removed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="备份瓦尔基里网页与记忆层")
    ap.add_argument("--due", action="store_true", help="今天已备份就跳过")
    ap.add_argument("--force", action="store_true", help="忽略今天的备份戳")
    ap.add_argument("--dest", default=str(DEFAULT_DEST), help="备份根目录")
    ap.add_argument("--keep", type=int, default=14, help="保留最近 N 天（0 不清理）")
    ap.add_argument("--json", action="store_true", help="只输出 JSON 摘要")
    args = ap.parse_args(argv)

    if args.due and not args.force and not due():
        if args.json:
            print(json.dumps({"ok": True, "skipped": "already backed up today"}, ensure_ascii=False))
        else:
            print("今天已经备份过。")
        return 0

    dest_root = Path(args.dest)
    dest = dest_root / datetime.now().strftime("%Y%m%d_%H%M%S")
    if dest.exists():
        print(f"目标已存在：{dest}")
        return 2
    dest.mkdir(parents=True, exist_ok=False)

    manifest: dict = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "workspace": str(ROOT),
        "dest": str(dest),
        "databases": [],
        "files": [],
    }

    try:
        for src in DBS:
            if not src.exists():
                manifest["databases"].append({"src": str(src), "exists": False})
                continue
            rel = src.relative_to(ROOT)
            info = copy_sqlite(src, dest / rel)
            info["src"] = str(src)
            manifest["databases"].append(info)
            print(f"db   {rel}  {info.get('bytes', 0):,} B")

        for src in WEB_FILES:
            if not src.exists():
                manifest["files"].append({"src": str(src), "exists": False})
                continue
            rel = src.relative_to(ROOT)
            if src.is_dir():
                for f in sorted(src.rglob("*")):
                    if f.is_file() and f.suffix not in (".pyc", ".pyo"):
                        manifest["files"].append(copy_file(f, dest / rel / f.relative_to(src)))
            else:
                manifest["files"].append(copy_file(src, dest / rel))
            print(f"file {rel}")

        manifest["pruned"] = prune(dest_root, args.keep)
        (dest / "MANIFEST.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        write_stamp(dest, manifest)
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] backup ok {dest} "
                    f"dbs={len(manifest['databases'])} files={len(manifest['files'])}\n")
        if args.json:
            print(json.dumps({"ok": True, "dest": str(dest), "databases": manifest["databases"],
                              "files": len(manifest["files"]), "pruned": manifest["pruned"]},
                             ensure_ascii=False))
        else:
            print(f"\n完成：{len(manifest['files'])} 个文件，{len(manifest['databases'])} 个库")
            print(f"清单：{dest / 'MANIFEST.json'}")
        return 0
    except Exception as e:
        msg = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] backup failed: {type(e).__name__}: {e}\n"
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(msg)
        print(msg, end="")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
