"""验收用临时库 fixture。

硬约束：
- 所有数据库路径必须落在 `robot/data/acceptance/tmp/` 下；
- 生产库（memory.db / learn.db / proactive.db / budget.db / p3.db）一律拒绝；
- 临时目录必须显式 `TemporaryDirectory(dir=TMP_ROOT)`，不依赖系统临时目录；
- 测试结束由 TemporaryDirectory 自动清理，不写生产数据。

用法：
    with temp_workspace("S01_") as ws:
        mem = ws.open_memory()
        ...
        ws.close_all()
"""
from __future__ import annotations

import contextlib
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Iterator


ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
TMP_ROOT = DATA / "acceptance" / "tmp"

PRODUCTION_DBS = {
    "memory": DATA / "memory.db",
    "learn": DATA / "learning" / "learn.db",
    "proactive": DATA / "proactive.db",
    "budget": DATA / "budget.db",
    "p3": DATA / "p3.db",
}


class ProductionDBError(RuntimeError):
    """测试试图打开生产库。"""


def _resolve(path: Path | str) -> Path:
    return Path(path).expanduser().resolve()


def _key(path: Path | str) -> str:
    return os.path.normcase(str(_resolve(path)))


def assert_not_production(path: Path | str) -> Path:
    """解析后确认不是任何生产库；不要求落在临时目录。"""
    p = _resolve(path)
    for name, prod in PRODUCTION_DBS.items():
        if _key(p) == _key(prod):
            raise ProductionDBError(f"拒绝打开生产库 {name}: {p}")
    return p


def assert_temp_db(path: Path | str) -> Path:
    """确认路径是验收临时库，不是生产库。"""
    p = assert_not_production(path)
    try:
        p.relative_to(TMP_ROOT.resolve())
    except ValueError as exc:
        raise ProductionDBError(
            f"验收数据库必须位于 {TMP_ROOT} 下，实际是 {p}"
        ) from exc
    return p


def db_counts(path: Path | str) -> dict:
    """只读读取一个 SQLite 库的表行数；库不存在返回空。"""
    p = Path(path)
    if not p.exists():
        return {}
    uri = p.resolve().as_uri() + "?mode=ro"
    out: dict[str, int] = {}
    with sqlite3.connect(uri, uri=True, timeout=5) as db:
        tables = [
            r[0]
            for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        for name in tables:
            out[name] = int(db.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0])
    return out


class TempWorkspace:
    """一次验收用例的临时工作区。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.memory_db = root / "memory.db"
        self.learn_db = root / "learning" / "learn.db"
        self.p3_db = root / "p3.db"
        self.flow_json = root / "flow.json"
        # 生产库集合包含 p3.db；临时 workspace 也显式提供它，测试不自己造路径。
        self.books = root / "learning" / "books"
        self.channels_file = root / "learning" / "channels.json"
        self.footprint = root / "learning" / "footprint.jsonl"
        self.bili_queue = root / "learning" / "bili_queue.json"
        self._open: list[object] = []

    def _prepare(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.learn_db.parent.mkdir(parents=True, exist_ok=True)
        self.books.mkdir(parents=True, exist_ok=True)

    def open_memory(self):
        self._prepare()
        assert_temp_db(self.memory_db)
        from app.memory import Memory

        obj = Memory(self.memory_db)
        self._open.append(obj)
        return obj

    @contextlib.contextmanager
    def open_learning(self) -> Iterator[object]:
        """打开临时 Learning；同时把模块级路径改到临时目录。"""
        self._prepare()
        assert_temp_db(self.learn_db)
        from app import learning as L

        saved = {
            "BOOKS": L.BOOKS,
            "CHANNELS_FILE": L.CHANNELS_FILE,
            "FOOTPRINT": L.FOOTPRINT,
            "BILI_QUEUE": L.BILI_QUEUE,
            "DB_PATH": L.DB_PATH,
        }
        L.BOOKS = self.books
        L.CHANNELS_FILE = self.channels_file
        L.FOOTPRINT = self.footprint
        L.BILI_QUEUE = self.bili_queue
        L.DB_PATH = self.learn_db
        obj = None
        try:
            obj = L.Learning(db_path=self.learn_db)
            self._open.append(obj)
            yield obj
        finally:
            if obj is not None:
                with contextlib.suppress(Exception):
                    obj.close()
            for key, value in saved.items():
                setattr(L, key, value)

    def open_p3(self):
        self._prepare()
        assert_temp_db(self.p3_db)
        from app.p3 import P3Store

        obj = P3Store(self.p3_db)
        self._open.append(obj)
        return obj

    def close_all(self) -> None:
        for obj in reversed(self._open):
            with contextlib.suppress(Exception):
                obj.close()
        self._open.clear()


@contextlib.contextmanager
def temp_workspace(prefix: str = "case_") -> Iterator[TempWorkspace]:
    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=prefix, dir=str(TMP_ROOT)) as td:
        ws = TempWorkspace(Path(td))
        try:
            yield ws
        finally:
            ws.close_all()


def self_check() -> dict:
    """fixture 自身冒烟：临时库能建、能读、生产库没被碰。"""
    checks: list[dict] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"name": name, "pass": bool(ok), "detail": detail})

    with temp_workspace("selfcheck_") as ws:
        check("临时目录在 acceptance/tmp 下",
              TMP_ROOT.resolve() in ws.root.resolve().parents, str(ws.root))
        check("临时目录由显式 dir 创建",
              ws.root.resolve().parent == TMP_ROOT.resolve(), str(ws.root.parent))
        for name, prod in PRODUCTION_DBS.items():
            try:
                assert_temp_db(prod)
                ok, detail = False, "未拒绝"
            except ProductionDBError as exc:
                ok, detail = True, str(exc)
            check(f"{name} 生产库路径被拒", ok, detail)
        try:
            assert_temp_db(ROOT / "data" / "not_tmp.db")
            ok, detail = False, "未拒绝"
        except ProductionDBError as exc:
            ok, detail = True, str(exc)
        check("data 下非 tmp 路径被拒", ok, detail)
        mem = ws.open_memory()
        mstats = mem.stats()
        check("memory 临时库为空", mstats.get("episodes") == 0, str(mstats))
        with ws.open_learning() as lr:
            check("learning 临时库可建", lr is not None)
            check("learning 指向临时库",
                  Path(lr.path).resolve() == ws.learn_db.resolve(), str(lr.path))
            cur = lr._db.execute("SELECT COUNT(*) FROM candidates").fetchone()
            check("learning 候选表为空", int(cur[0]) == 0, str(tuple(cur)))
        for name, prod in PRODUCTION_DBS.items():
            before = prod.stat().st_mtime_ns if prod.exists() else None
            check(f"{name} 生产库未被 fixture 打开",
                  before == (prod.stat().st_mtime_ns if prod.exists() else None),
                  str(prod))
    return {
        "checks": checks,
        "pass": all(c["pass"] for c in checks),
        "total": len(checks),
        "failed": [c for c in checks if not c["pass"]],
    }
