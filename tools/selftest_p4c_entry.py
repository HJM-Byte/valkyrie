"""P4-C 生产入口自检：识海人格视图的读取、候选、确认、否定与回滚。

    C:\\robot\\venv\\Scripts\\python.exe -m tools.selftest_p4c_entry

只用 data/acceptance/tmp/ 下的临时库，起本地 HTTP 服务，不碰生产 memory.db。
验证的是“入口不会绕过证据门槛、不会自动确认、回滚不删历史”。
"""
from __future__ import annotations

import hashlib
import json
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
    req = Request(url, data=data, headers=headers,
                  method="POST" if data else "GET")
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
    with fixtures.temp_workspace("p4c_entry_") as ws:
        mem = ws.open_memory()
        S.DB_PATH = ws.memory_db
        e1 = mem.add_episode("他卡住时我先问哪一步", who="我",
                             source="对话", imp=0.65, ts=now - 4 * DAY)
        e2 = mem.add_episode("后来他自己把问题拆开了", who="我",
                             source="对话", imp=0.65, ts=now - 2 * DAY)
        m1 = mem.add_meaning_candidate("先确认再猜", "先问清再继续",
                                       episode_ids=[e1])
        m2 = mem.add_meaning_candidate("不替他决定", "只拆问题，不替他选",
                                       episode_ids=[e2])
        for x in (m1, m2):
            mem.set_meaning_status(x["id"], "confirmed")

        class FakePersona:
            def block(self, name):
                return "身份与硬边界测试文本" if name == "core" else ""

        class FakeMind:
            def snapshot(self):
                return {}

        class FakeChat:
            def __init__(self):
                self.mem = mem
                self.mind = FakeMind()
                self.persona = FakePersona()

            def panel_payload(self):
                return {}

            def thought_view(self):
                return {}

        port = free_port()
        base = f"http://127.0.0.1:{port}"
        import app.chat as chat_mod
        import app.diary as diary_mod

        with mock.patch.object(chat_mod, "Chat", FakeChat), \
                mock.patch.object(diary_mod, "start_scheduler", lambda: None):
            th = threading.Thread(target=S.serve, args=(False, port, False),
                                  daemon=True)
            th.start()
            ok = wait_port(port)
            check("临时识海服务能起来", ok, str(port))
            if not ok:
                return 1

            print("— 读取人格面板 —")
            code, body = call(base + "/api/insight")
            check("insight 200", code == 200 and body.get("ok"), str(code))
            p = body.get("persona") or {}
            check("人格面板带身份与硬边界只读",
                  (p.get("core") or {}).get("locked") is True, str(p.get("core")))
            check("人格面板有三层",
                  set((p.get("layers") or {}).keys())
                  == {"tendency", "interaction", "narrative"},
                  str(p.get("layers")))

            print("— 候选必须过证据门槛 —")
            code, body = call(base + "/api/persona/propose", {
                "layer": "core", "field": "honesty", "value": "模型不能自改硬边界",
            })
            check("core 候选被拒", code == 409 and body.get("code") == "core_locked",
                  str(body))
            code, body = call(base + "/api/persona/propose", {
                "layer": "tendency", "field": "curiosity",
                "value": "只有一条意义",
                "meaning_ids": [m1["id"]],
            })
            check("一条意义被拒", code == 409
                  and body.get("code") == "need_two_meanings", str(body))
            code, body = call(base + "/api/persona/propose", {
                "layer": "tendency", "field": "curiosity",
                "value": "他卡住时先问清哪一步，再往下说",
                "meaning_ids": [m1["id"], m2["id"]],
            })
            check("两条跨日意义可建候选", code == 200 and body.get("ok"), str(body))
            pid = str(body.get("id") or "")
            check("候选不自动 active",
                  mem.persona_block() == "" and len(mem.persona_versions("active")) == 0)
            code, body = call(base + "/api/persona/" + pid + "/confirm",
                              {"note": "测试确认"})
            check("候选可确认", code == 200 and body.get("ok"), str(body))
            check("确认后进入提示",
                  "先问清哪一步" in mem.persona_block(), mem.persona_block())

            print("— 否定、版本替换与回滚 —")
            code, body = call(base + "/api/persona/propose", {
                "layer": "tendency", "field": "curiosity",
                "value": "先停一下，让他自己说",
                "meaning_ids": [m1["id"], m2["id"]],
            })
            pid2 = str(body.get("id") or "")
            code, body = call(base + "/api/persona/" + pid2 + "/confirm", {})
            check("第二版可确认", code == 200 and body.get("ok"), str(body))
            check("同字段仍只有一条 active",
                  len(mem.persona_versions("active")) == 1,
                  str(mem.persona_versions("active")))
            code, body = call(base + "/api/persona/" + pid + "/rollback", {})
            check("旧版可回滚", code == 200 and body.get("ok"), str(body))
            check("回滚后旧值生效",
                  "先问清哪一步" in mem.persona_block(), mem.persona_block())
            superseded = [x for x in mem.persona_versions()
                          if x["id"] == pid2]
            check("被替换版本仍在历史", superseded
                  and superseded[0]["status"] == "superseded", str(superseded))

            print("— 否定与撤回 —")
            code, body = call(base + "/api/persona/propose", {
                "layer": "interaction", "field": "address_style",
                "value": "先接住再给东西",
                "meaning_ids": [m1["id"], m2["id"]],
            })
            pid3 = str(body.get("id") or "")
            code, body = call(base + "/api/persona/" + pid3 + "/status",
                              {"status": "rejected"})
            check("候选可否定", code == 200
                  and body.get("status") == "rejected", str(body))
            check("被否定候选不能确认",
                  mem.confirm_persona_version(pid3).get("reason") == "not_candidate")

            print("— 生产库没有被测试碰到 —")
            prod_hash1 = hashlib.sha256(prod.read_bytes()).hexdigest()
            check("生产库哈希未变", prod_hash0 == prod_hash1,
                  f"{prod_hash0[:12]} -> {prod_hash1[:12]}")
            check("生产库行数未变", prod_counts0 == fixtures.db_counts(prod),
                  f"{prod_counts0} -> {fixtures.db_counts(prod)}")
        mem.close()

    print(f"\n{PASS} 通过 / {FAIL} 失败")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
