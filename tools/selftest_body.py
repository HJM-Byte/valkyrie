"""A2 协议自检：OTA、hello、设备去重、Opus 编解码。

    cd robot
    C:\\robot\\venv\\Scripts\\python.exe -m tools.selftest_body

不启动真 Chat、不打开麦克风、不写 memory.db；测试结束真实数据不变。
"""
from __future__ import annotations

import asyncio
import json
import sys

import numpy as np
from aiohttp import WSMsgType
from aiohttp.test_utils import TestClient, TestServer

from app import body_audio, body_link
from app.xiaozhi_server import ServerConfig, make_app

HELLO = {
    "type": "hello",
    "version": 1,
    "features": {"mcp": True},
    "transport": "websocket",
    "audio_params": {
        "format": "opus",
        "sample_rate": 16000,
        "channels": 1,
        "frame_duration": 60,
    },
}


class Checks:
    def __init__(self) -> None:
        self.ok = 0
        self.fail = 0

    def check(self, name: str, cond: bool, detail: str = "") -> None:
        if cond:
            self.ok += 1
            print(f"  ok   {name}")
        else:
            self.fail += 1
            print(f"  FAIL {name} {detail}")


async def _ota(client: TestClient, device_id: str) -> dict:
    resp = await client.post(
        body_link.OTA_PATH,
        json={"application": {"name": "selftest", "version": "0.1"}},
        headers={"Device-Id": device_id, "Client-Id": "selftest-client"},
    )
    text = await resp.text()
    assert resp.status == 200, f"OTA HTTP {resp.status}: {text}"
    data = json.loads(text)
    assert isinstance(data, dict)
    return data


async def _connect(client: TestClient, device_id: str):
    ws = await client.ws_connect(
        body_link.WS_PATH,
        headers={
            "Device-Id": device_id,
            "Client-Id": "selftest-client",
            "Protocol-Version": "1",
        },
    )
    await ws.send_json(HELLO)
    msg = await ws.receive_json(timeout=5)
    return ws, msg


async def run() -> int:
    checks = Checks()
    cfg = ServerConfig(host="127.0.0.1", port=0, token="local-dev",
                       max_devices=3, demo_reply=True)
    app = make_app(cfg)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        # 1. OTA 结构
        ota = await _ota(client, "selftest-device-1")
        checks.check("OTA 有 websocket.url",
                     isinstance(ota.get("websocket"), dict) and bool(ota["websocket"].get("url")))
        checks.check("OTA 有 server_time", isinstance(ota.get("server_time"), dict))
        checks.check("OTA 无 mqtt", "mqtt" not in ota)
        checks.check("OTA 无 activation", "activation" not in ota)
        checks.check("OTA 无 firmware", "firmware" not in ota)
        checks.check("OTA token 正确", ota["websocket"].get("token") == "local-dev")

        # 2. hello
        ws, hello = await _connect(client, "selftest-device-1")
        checks.check("server hello type", hello.get("type") == "hello", repr(hello))
        checks.check("server hello transport=websocket", hello.get("transport") == "websocket")
        checks.check("server hello audio_params 回显",
                     hello.get("audio_params", {}).get("sample_rate") == 16000)
        await ws.close()

        # 3. 同一 Device-Id 重连，旧连接被 4001 踢掉
        ws1, hello1 = await _connect(client, "selftest-dup")
        ws2, hello2 = await _connect(client, "selftest-dup")
        close_msg = await ws1.receive(timeout=5)
        checks.check("重复设备旧连接被关闭", close_msg.type == WSMsgType.CLOSE, str(close_msg))
        if close_msg.type == WSMsgType.CLOSE:
            checks.check("旧连接 code=4001", close_msg.data == 4001, str(close_msg.data))
        await ws2.close()

        # 4. 3 个以上设备被拒
        conns = []
        for i in range(3):
            w, _ = await _connect(client, f"selftest-cap-{i}")
            conns.append(w)
        resp = await client.get(body_link.OTA_PATH, headers={"Device-Id": "selftest-cap-x"})
        # OTA 不占 WS 名额；第四台 WS 应被 1013 拒绝
        fourth = await client.ws_connect(
            body_link.WS_PATH,
            headers={"Device-Id": "selftest-cap-x", "Protocol-Version": "1"},
        )
        await fourth.send_json(HELLO)
        msg = await fourth.receive(timeout=5)
        checks.check("第 4 台设备被拒", msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED), str(msg))
        for w in conns:
            await w.close()

        # 5. Opus 编解码
        pcm = body_audio.sine_pcm16(440.0, 0.30)
        packets = body_audio.pcm16_to_opus(pcm)
        checks.check("Opus 有包", len(packets) >= 5, f"packets={len(packets)}")
        decoded = body_audio.opus_frames_to_pcm16(packets)
        checks.check("Opus 解码非空", decoded.size > 4000, f"samples={decoded.size}")
        checks.check("Opus 解码是 int16", decoded.dtype == np.int16, str(decoded.dtype))
        duration = body_audio.pcm16_duration(decoded)
        checks.check("Opus 时长接近 0.3s", 0.27 <= duration <= 0.38, f"duration={duration:.3f}")

        # 6. A4.1 唤醒后下行固定 TTS
        ws_demo, _ = await _connect(client, "selftest-demo")
        await ws_demo.send_json({"type": "listen", "state": "start", "mode": "auto"})
        first = await ws_demo.receive(timeout=10)
        checks.check("demo 先发 tts start",
                     first.type == WSMsgType.TEXT and
                     json.loads(first.data).get("state") == "start", str(first))
        got_audio = False
        for _ in range(8):
            msg = await ws_demo.receive(timeout=10)
            if msg.type == WSMsgType.BINARY:
                got_audio = len(msg.data) > 0
                break
        checks.check("demo 下发 Opus 包", got_audio)
        await ws_demo.close()

        # 7. 健康接口
        health = await client.get("/health")
        hdata = await health.json()
        checks.check("健康接口 ok", health.status == 200 and hdata.get("ok") is True)
        checks.check("健康接口记录 demo", hdata.get("demo_reply") is True)

    finally:
        await client.close()

    print(f"\n结果：{checks.ok} ok / {checks.fail} fail")
    return 0 if checks.fail == 0 else 1


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())
