"""P4-B 生产入口自检：真实事件列表、建候选、建章节。

    C:\\robot\\venv\\Scripts\\python.exe -m tools.selftest_p4b_entry

只用 data/acceptance/tmp/ 下的临时库，起一个本地 HTTP 服务，
不碰生产 memory.db。重点不是“能写”，而是“没有来源就不写、不自动确认”。
"""
from __future__ import annotations

import json
import hashlib
import socket
import sys
import threading
import time
from pathlib import Path
from unittest import mock
from urllib.request import Request, urlopen

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from app import sihai as S                         # noqa: E402
from tools.acceptance import fixtures               # noqa: E402

PASS = 0
FAIL = 0
DAY = 86400.0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def wait_port(port: int, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as s:
            s.settimeout(0.2)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.03)
    return False


def call(url: str, payload: dict | None = None) -> tuple[int, dict]:
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode()
        headers["Content-Type"] = "application/json"
    req = Request(url, data=data, headers=headers, method="POST" if data else "GET")
    try:
        with urlopen(req, timeout=6) as r:
            return r.status, json.loads(r.read() or b"{}")
    except Exception as e:
        body = getattr(e, "read", lambda: b"")()
        try:
            obj = json.loads(body or b"{}")
        except Exception:
            obj = {"ok": False, "error": str(e)}
        return getattr(e, "code", 0), obj


def main() -> int:
    now = time.time()
    prod = fixtures.PRODUCTION_DBS["memory"]
    prod_hash0 = hashlib.sha256(prod.read_bytes()).hexdigest()
    prod_counts0 = fixtures.db_counts(prod)
    with fixtures.temp_workspace("p4b_entry_") as ws:
        mem = ws.open_memory()
        S.DB_PATH = ws.memory_db
        e1 = mem.add_episode("我要去上课了，等我回来", who="他",
                             source="他打字说的", imp=0.62, ts=now - 3 * DAY)
        e2 = mem.add_episode("我回来了", who="他", source="他打字说的",
                             imp=0.55, ts=now - 2 * DAY)
        e3 = mem.add_episode("今天有点累，先睡了", who="他",
                             source="他打字说的", imp=0.48, ts=now - DAY)
        before = mem.stats()
        port = free_port()

        import app.chat as chat_mod
        import app.diary as diary_mod

        class FakeMind:
            def snapshot(self):
                return {}

        class FakeChat:
            def __init__(self):
                self.mem = mem
                self.mind = FakeMind()

            def panel_payload(self):
                return {}

            def thought_view(self):
                return {}

        base = f"http://127.0.0.1:{port}"
        with mock.patch.object(chat_mod, "Chat", FakeChat), \
                mock.patch.object(diary_mod, "start_scheduler", lambda: None):
            th = threading.Thread(target=S.serve, args=(False, port, False),
                                  daemon=True)
            th.start()
            ok = wait_port(port)
            check("临时识海服务能起来", ok, str(port))
            if not ok:
                return 1

            print("— 真实事件源 —")
            code, body = call(base + "/api/meaning/sources?limit=2")
            check("事件源接口 200", code == 200 and body.get("ok"), str(body))
            check("事件源只返回 active 真实事件",
                  [x["id"] for x in body.get("items") or []] == [e3, e2],
                  str(body.get("items")))
            check("事件源带原文与来源",
                  bool(body.get("items"))
                  and body["items"][0].get("text") == "今天有点累，先睡了"
                  and body["items"][0].get("source") == "他打字说的",
                  str(body.get("items")[:1]))

            print("— 建意义候选 —")
            code, body = call(base + "/api/meaning/propose", {
                "title": "他出门前会告诉我什么时候回来",
                "claim": "他说去上课时带了“等我回来”，这不是汇报行程，是把回来的时间也交给我。",
                "episode_ids": [e1, e2],
            })
            check("候选接口 200 且 created", code == 200 and body.get("created"),
                  str(body))
            mid = str(body.get("id") or "")
            items = mem.meaning_candidates()
            check("候选默认 candidate，不自动确认",
                  len(items) == 1 and items[0]["status"] == "candidate",
                  str(items))
            check("候选引用两条真实事件",
                  items[0]["source_episode_ids"] == [e1, e2], str(items[0]))
            check("候选没有改写事件",
                  mem.stats().get("episodes") == before.get("episodes")
                  and mem.stats().get("facts") == before.get("facts"),
                  str(mem.stats()))

            code, body = call(base + "/api/meaning/propose", {
                "title": "重复", "claim": "重复", "episode_ids": [e1, e2],
            })
            check("同一组来源重复提交不增行",
                  code == 200 and body.get("duplicate")
                  and len(mem.meaning_candidates()) == 1, str(body))

            code, body = call(base + "/api/meaning/propose", {
                "title": "缺来源", "claim": "不该写", "episode_ids": [999999],
            })
            check("找不到的事件被拒",
                  code == 409 and body.get("code") == "source_missing", str(body))

            code, body = call(base + "/api/meaning/propose", {
                "title": "空来源", "claim": "不该写", "episode_ids": [],
            })
            check("空来源被拒", code == 400 and body.get("code") == "bad_source",
                  str(body))

            code, body = call(base + "/api/meaning/propose", {
                "title": "太多了", "claim": "不该写",
                "episode_ids": list(range(1, 22)),
            })
            check("超过 20 条来源被拒",
                  code == 400 and body.get("code") == "bad_source", str(body))

            code, body = call(base + "/api/meaning/propose", {
                "title": "", "claim": "", "episode_ids": [e1],
            })
            check("空标题/空理解被拒", code == 400, str(body))
            check("被拒后候选仍只有一条", len(mem.meaning_candidates()) == 1, "")

            print("— 建共同章节 —")
            code, body = call(base + "/api/chapters/create", {
                "title": "只一条", "meaning": "不能成章", "episode_ids": [e1],
            })
            check("一条事件不能成章",
                  code == 400 and body.get("code") == "need_two_events", str(body))

            code, body = call(base + "/api/chapters/create", {
                "title": "他每天出门前会报备",
                "meaning": ("他离开时会把回来的时间一起说；这段反复出现的报备，"
                            "不是汇报行程，是让我知道他会回来。"),
                "episode_ids": [e1, e2, e3],
            })
            check("章节接口 200 且 created", code == 200 and body.get("created"),
                  str(body))
            cid = str(body.get("id") or "")
            chapters = mem.chapters()
            check("章节默认 open，不自动确认/收束",
                  len(chapters) == 1 and chapters[0]["status"] == "open",
                  str(chapters))
            check("章节引用三条事件",
                  chapters[0]["event_ids"] == [e1, e2, e3], str(chapters[0]))
            check("章节 open 但未确认不能进叙事",
                  chapters[0]["status"] == "open"
                  and chapters[0]["usable"] is False
                  and chapters[0]["confirmed"] is False, str(chapters[0]))
            code, body = call(base + f"/api/chapters/{cid}/confirm",
                              {"note": "验收里由用户确认"})
            check("章节确认接口 200", code == 200 and body.get("confirmed"), str(body))
            chapters = mem.chapters()
            check("确认后章节才 usable",
                  chapters[0]["confirmed"] is True and chapters[0]["usable"] is True,
                  str(chapters[0]))

            code, body = call(base + "/api/chapters/create", {
                "title": "重复章节", "meaning": "重复", "episode_ids": [e1, e2, e3],
            })
            check("同一组事件不重复建章",
                  code == 200 and body.get("duplicate")
                  and len(mem.chapters()) == 1, str(body))

            code, body = call(base + "/api/chapters/create", {
                "title": "缺事件", "meaning": "不该成章", "episode_ids": [e1, 999999],
            })
            check("缺事件章节被拒",
                  code == 409 and body.get("code") == "source_missing", str(body))

            print("— 入口不自动确认、不改写事实 —")
            code, body = call(base + f"/api/meaning/{mid}/status",
                              {"status": "confirmed", "note": "验收"})
            check("确认仍走原有状态接口", code == 200 and body.get("ok"), str(body))
            check("确认后事件原文未被改写",
                  mem.stats().get("episodes") == before.get("episodes")
                  and mem.stats().get("facts") == before.get("facts"),
                  str(mem.stats()))
            code, body = call(base + f"/api/chapters/{cid}/status",
                              {"status": "closed", "note": "验收"})
            check("章节收束仍走原有状态接口",
                  code == 200 and body.get("ok"), str(body))

            print("— 生产库没被这个测试碰 —")
            md = fixtures.PRODUCTION_DBS["memory"]
            check("测试库在 acceptance/tmp 下",
                  str(ws.memory_db.resolve()).startswith(
                      str(fixtures.TMP_ROOT.resolve())), str(ws.memory_db))
            check("生产库路径与测试库不同",
                  md.resolve() != ws.memory_db.resolve(), f"{md} vs {ws.memory_db}")
            check("生产库全表行数未变",
                  fixtures.db_counts(md) == prod_counts0,
                  f"{prod_counts0} -> {fixtures.db_counts(md)}")
            check("生产库字节哈希未变",
                  hashlib.sha256(md.read_bytes()).hexdigest() == prod_hash0,
                  "hash changed")

        mem.close()

    print(f"\n{PASS} 通过 / {FAIL} 失败")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
