"""水镜 App 的自检：只碰临时库，不碰真实 memory.db。

跑法：
    python -m tools.selftest_pocket
"""
from __future__ import annotations

import asyncio
import json
import shutil
import sys
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.pocket_server import PocketAuth, PocketStore, create_app  # noqa: E402
from app.proactive import ProactiveStore  # noqa: E402

TOKEN = "246810"
H = {"X-Valkyrie-Token": TOKEN}
PASS: list[str] = []


def ok(name: str) -> None:
    PASS.append(name)
    print(f"PASS  {name}")


async def run() -> None:
    # Windows 上系统临时目录在 DSH 沙箱里可能带 ACL，SQLite 会打不开；
    # 自检固定用工作区内自己的临时目录，跑完清掉。
    td = Path(__file__).resolve().parent.parent / "_selftest_tmp_pocket"
    if td.exists():
        shutil.rmtree(td, ignore_errors=True)
    td.mkdir(parents=True, exist_ok=True)
    try:
        db = td / "pocket.db"
        store = PocketStore(db, writable=True, demo=True)
        pstore = ProactiveStore(td / "proactive.db")
        app = create_app(store, auth=PocketAuth(TOKEN), token=TOKEN,
                         demo=True, readonly=False, chat_enabled=False,
                         proactive_enabled=True, proactive_store=pstore)
        try:
            async with TestClient(TestServer(app)) as client:
                # 页面与静态资源
                r = await client.get("/")
                assert r.status == 200
                html = await r.text()
                assert "瓦尔基里" in html and "app.js" in html
                assert "view-dialogue" in html and "micButton" in html
                assert "growthSection" in html and "meaningSection" in html
                ok("首页、对话页与 P4 成长区可打开")

                r = await client.get("/assets/app.js")
                assert r.status == 200
                js = await r.text()
                assert "loadData" in js and "openMemoryDetail" in js
                assert "renderGrowth" in js and "renderMeaning" in js
                ok("前端资源可加载")

                r = await client.get("/manifest.webmanifest")
                assert r.status == 200
                manifest = await r.json()
                assert manifest["display"] == "standalone"
                r = await client.get("/sw.js")
                assert r.status == 200 and "valkyrie-pocket-shell" in await r.text()
                ok("PWA 清单与离线壳可读")

                # 认证
                r = await client.get("/api/memory")
                assert r.status == 401
                ok("没有配对码时拒绝读记忆")

                r = await client.get("/api/memory", headers={"X-Valkyrie-Token": "bad"})
                assert r.status == 401
                ok("错配对码也拒绝")

                # 状态与示例
                r = await client.get("/api/state", headers=H)
                data = await r.json()
                assert r.status == 200 and data["source"] == "demo"
                assert data["memory"]["active"] == 3 and data["memory"]["facts"] == 2
                voices = {v["id"]: v["name"] for v in data["voices"]}
                assert voices == {
                    "voc_wkzpcy2xxwch": "寒蝉余音",
                    "voc_dcccp6b9p466": "幽兰影",
                }
                assert data["voice_default"] == "voc_wkzpcy2xxwch"
                assert data["growth"]["persona_active"] == 0
                assert data["growth"]["meanings_confirmed"] == 0
                ok("示例状态与计数正确，两个音色可选")

                r = await client.get("/api/meaning", headers=H)
                data = await r.json()
                assert r.status == 200 and data["ok"] is True
                assert data["stats"]["confirmed"] == 0 and data["chapters"] == []
                r = await client.get("/api/persona", headers=H)
                data = await r.json()
                assert r.status == 200 and data["active_count"] == 0
                assert set(data["layers"]) == {"tendency", "interaction", "narrative"}
                assert data["core"]["locked"] is True and "text" not in data["core"]
                ok("意义与人格只读接口形状正确，核心正文不下发")

                # 列表与搜索
                r = await client.get("/api/memory?limit=2", headers=H)
                data = await r.json()
                assert len(data["items"]) == 2 and data["total"] == 3
                assert data["items"][0]["status"] == "active"
                ok("记忆列表分页正确")

                r = await client.get("/api/memory?q=江边", headers=H)
                data = await r.json()
                assert data["total"] == 1 and "江边" in data["items"][0]["text"]
                ok("中文检索命中")

                # 记一笔 -> 详情 -> 更正 -> 撤回
                r = await client.post("/api/memory", headers=H, json={
                    "text": "他今晚把台灯调暗了一点，说眼睛有点酸。",
                    "imp": 0.61, "claim": "fact", "who": "他",
                    "region": "experience", "source": "自检",
                })
                data = await r.json()
                assert r.status == 201 and data["id"] > 0
                eid = data["id"]
                ok("新增记忆")

                r = await client.get(f"/api/memory/{eid}", headers=H)
                data = await r.json()
                assert data["item"]["source"] == "自检" and data["item"]["related"]
                ok("详情与相邻记忆")

                r = await client.post(f"/api/memory/{eid}/correct", headers=H, json={
                    "text": "他今晚把台灯调暗了一点，说眼睛有点酸；后来早点睡了。",
                    "reason": "补结果",
                })
                data = await r.json()
                assert r.status == 200 and data["old_id"] == eid and data["id"] != eid
                new_id = data["id"]
                ok("更正是替代而不是改写")

                r = await client.post(f"/api/memory/{new_id}/retract", headers=H, json={
                    "reason": "自检撤回",
                })
                data = await r.json()
                assert data["retracted"] == 1
                r = await client.get("/api/memory?status=all&limit=100", headers=H)
                data = await r.json()
                statuses = {i["id"]: i["status"] for i in data["items"]}
                assert statuses[eid] == "superseded" and statuses[new_id] == "retracted"
                ok("撤回与取代状态正确")

                # 事实
                r = await client.post("/api/facts", headers=H, json={
                    "key": "怕", "value": "太吵的地方", "conf": 0.8,
                })
                assert r.status == 201
                r = await client.get("/api/facts", headers=H)
                data = await r.json()
                assert any(i["key"] == "怕" for i in data["items"])
                r = await client.delete("/api/facts/%E6%80%95?confirm=DELETE", headers=H)
                assert r.status == 200
                r = await client.get("/api/facts", headers=H)
                data = await r.json()
                assert not any(i["key"] == "怕" for i in data["items"])
                ok("事实新增与删除")

                # 导出
                r = await client.get("/api/export", headers=H)
                data = await r.json()
                assert len(data["episodes"]) >= 5 and data["source"] == "demo"
                ok("导出 JSON")

                # 默认不叫醒她
                r = await client.post("/api/chat", headers=H, json={"text": "在吗"})
                assert r.status == 403
                r = await client.post("/api/tts", headers=H, json={"text": "在吗"})
                assert r.status == 403
                ok("默认禁用对话与声音，不叫醒她")

                # 主动消息：认证、游标、bootstrap 补最近一条
                r = await client.get("/api/proactive?since_id=0", headers=H)
                data = await r.json()
                assert r.status == 200 and data["enabled"] is True and data["items"] == []
                r = await client.get("/api/proactive?since_id=0")
                assert r.status == 401
                rec = pstore.record("你今天忙吗？想听你说两句。", kind="greet",
                                    seed="greet:selftest")
                r = await client.get("/api/proactive?since_id=0&bootstrap=1", headers=H)
                data = await r.json()
                assert len(data["items"]) == 1
                assert data["items"][0]["text"] == "你今天忙吗？想听你说两句。"
                assert data["last_id"] == rec["id"]
                r = await client.get(f"/api/proactive?since_id={rec['id']}", headers=H)
                data = await r.json()
                assert data["items"] == []
                ok("主动消息 API：认证、bootstrap 与游标")

            # 只读实例
            ro = PocketStore(db, writable=False, demo=False)
            app2 = create_app(ro, auth=PocketAuth(TOKEN), token=TOKEN,
                              demo=False, readonly=True, chat_enabled=False)
            try:
                async with TestClient(TestServer(app2)) as client:
                    r = await client.get("/api/memory", headers=H)
                    assert r.status == 200
                    r = await client.get("/api/meaning", headers=H)
                    assert r.status == 200
                    r = await client.get("/api/persona", headers=H)
                    assert r.status == 200
                    r = await client.post("/api/memory", headers=H, json={"text": "写不进去"})
                    assert r.status == 403
                    ok("只读模式可读意义/人格且拒写")
            finally:
                ro.close()

            # 对话与声音：用假 Chat / 假媒体，验证路由和返回结构，不碰云端与真实库
            chat_store = PocketStore(td / "chat.db", writable=True, demo=True)
            chat_app = create_app(chat_store, auth=PocketAuth(TOKEN), token=TOKEN,
                                  demo=True, readonly=False, chat_enabled=True)

            class FakeChat:
                def turn(self, text, scene="", channel="text"):
                    return {"ok": True, "reply": f"收到：{text}", "seconds": 0.01,
                            "birth": True, "scene": scene, "channel": channel}

                def turn_stream(self, text, scene="", channel="text"):
                    yield {"type": "delta", "text": "收到"}
                    yield {"type": "delta", "text": f"：{text}"}
                    yield {"type": "done", "result": {
                        "ok": True, "reply": f"收到：{text}", "seconds": 0.01,
                        "birth": True, "scene": scene, "channel": channel}}

            class FakeMedia:
                def synthesize(self, text, voice_id=""):
                    return {"audio": "UklGRg==", "mime": "audio/wav",
                            "backend": "fake", "voice": voice_id,
                            "voice_name": "测试音色", "seconds": 0.0}

            chat_app["chat"]._chat = FakeChat()
            chat_app["chat"].media = FakeMedia()
            try:
                async with TestClient(TestServer(chat_app)) as client:
                    r = await client.post("/api/chat", headers=H, json={"text": "在吗"})
                    data = await r.json()
                    assert r.status == 200 and data["reply"] == "收到：在吗"
                    ok("对话接口可回复")

                    r = await client.post("/api/chat/stream", headers=H,
                                          json={"text": "在吗"})
                    assert r.status == 200
                    assert r.content_type == "text/event-stream"
                    raw = await r.text()
                    events = [json.loads(line[5:].strip())
                              for line in raw.splitlines()
                              if line.startswith("data:")]
                    kinds = [e.get("type") for e in events]
                    assert kinds == ["meta", "delta", "delta", "done"]
                    assert "".join(e.get("text", "") for e in events
                                   if e.get("type") == "delta") == "收到：在吗"
                    assert events[-1]["result"]["reply"] == "收到：在吗"
                    ok("流式对话按 SSE 事件返回")
                    r = await client.post("/api/tts", headers=H, json={"text": "你好"})
                    data = await r.json()
                    assert r.status == 200 and data["mime"] == "audio/wav"
                    assert data["audio"] == "UklGRg=="
                    ok("声音接口可返回音频")
                    r = await client.post("/api/tts", headers=H, json={
                        "text": "你好", "voice": "voc_dcccp6b9p466"})
                    data = await r.json()
                    assert r.status == 200 and data["voice"] == "voc_dcccp6b9p466"
                    r = await client.post("/api/tts", headers=H, json={
                        "text": "你好", "voice": "not-a-voice"})
                    assert r.status == 400 and (await r.json())["code"] == "BAD_VOICE"
                    ok("声音接口支持两个音色并拒绝未知音色")
                    r = await client.post("/api/chat", headers=H, json={})
                    assert r.status == 400
                    r = await client.post("/api/chat", headers=H,
                                          json={"audio": "!!!!"})
                    assert r.status == 400
                    ok("空输入与坏 base64 被拒绝")
            finally:
                chat_store.close()
        finally:
            pstore.close()
            store.close()
    finally:
        shutil.rmtree(td, ignore_errors=True)

    print(f"\n{len(PASS)} 项通过")
    print("注意：本轮只用临时库；真实 robot/data/memory.db 没有被写入。")


def main() -> int:
    try:
        asyncio.run(run())
    except AssertionError as e:
        print(f"\nFAIL  {e}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
