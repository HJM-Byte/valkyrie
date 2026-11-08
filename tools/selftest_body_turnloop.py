"""A5 专项：板子回复后立刻自动发 listen start，必须能接上第二轮。

旧实现的冷却公式把“发送耗时 + 音频时长 + 0.8s”叠在一起，板子在
`tts stop` 后几十毫秒发的 `listen start mode=auto` 会被丢掉，屏幕回到
“聆听中”，但服务端永远不会再开录音。这个测试模拟真机行为：

1. 第一轮：listen start → 语音 + 静音 → STT/Chat/TTS；
2. 收到 tts stop 后立刻发第二个 listen start（不重新唤醒）；
3. 紧接着上语音 + 静音；
4. 断言收到第二轮的 STT/LLM/tts stop。

    cd robot
    C:\\robot\\venv\\Scripts\\python.exe -m tools.selftest_body_turnloop
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sys
import time
from pathlib import Path

from aiohttp import WSMsgType
from aiohttp.test_utils import TestClient, TestServer

from app import body_audio, body_link, config as C
from app.tts import TTS
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


def digest(path: Path) -> str:
    if not path.exists():
        return "MISSING"
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def wait_tts_stop(ws, deadline_sec: float, label: str) -> tuple[bool, dict]:
    """等到本轮 tts stop；返回 (ok, 看到的统计)。"""
    deadline = time.monotonic() + deadline_sec
    stats = {"stt": "", "llm": "", "tts_stop": False, "binary": 0}
    while time.monotonic() < deadline:
        try:
            msg = await ws.receive(timeout=3)
        except asyncio.TimeoutError:
            continue
        if msg.type == WSMsgType.TEXT:
            obj = json.loads(msg.data)
            typ = obj.get("type")
            if typ == "stt":
                stats["stt"] = str(obj.get("text") or "")
                print(f"  [{label}] <- STT {stats['stt']!r}")
            elif typ == "llm":
                stats["llm"] = str(obj.get("text") or "")
                print(f"  [{label}] <- LLM {stats['llm'][:80]!r}")
            elif typ == "tts":
                state = obj.get("state")
                print(f"  [{label}] <- TTS {state}")
                if state == "stop":
                    stats["tts_stop"] = True
                    return True, stats
        elif msg.type == WSMsgType.BINARY:
            stats["binary"] += 1
        elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR):
            print(f"  [{label}] WS 结束：{msg.type}")
            return False, stats
    return False, stats


async def send_turn(ws, speech_packets, silence_packets) -> None:
    for packet in speech_packets + silence_packets:
        await ws.send_bytes(packet)
        await asyncio.sleep(0.061)


async def run() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    print("—— A5 二轮对话自检：回复后自动 listen start ——")
    real_memory = C.DATA / "memory.db"
    real_sleep = C.DATA / "sleep.json"
    before = (digest(real_memory), digest(real_sleep))
    stamp = time.strftime("%Y%m%d_%H%M%S")
    work = C.DATA / "_selftest_body_turnloop" / stamp
    cfg = ServerConfig(host="127.0.0.1", port=0, chat=True,
                       brain_dir=str(work), capture_max_sec=8.0)
    app = make_app(cfg)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    ok = 0
    fail = 0

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal ok, fail
        if cond:
            ok += 1
            print(f"  ok   {name}")
        else:
            fail += 1
            print(f"  FAIL {name} {detail}")

    tts = TTS(cache_dir=work / "tts_input")
    try:
        src = tts.synth("你好，你是谁？")
        speech = body_audio.decode_audio_file(src)
        speech_packets = body_audio.pcm16_to_opus(speech)
        silence_packets = body_audio.pcm16_to_opus(
            body_audio.sine_pcm16(0.0, 1.2, amplitude=0.0))
        print(f"  上行：语音 {len(speech_packets)} 包 + 静音 {len(silence_packets)} 包")

        ws = await client.ws_connect(
            body_link.WS_PATH,
            headers={"Device-Id": "selftest-turnloop-body",
                     "Client-Id": "selftest-turnloop-client",
                     "Protocol-Version": "1"},
        )
        await ws.send_json(HELLO)
        hello = await ws.receive_json(timeout=10)
        check("server hello", hello.get("type") == "hello")

        # 第一轮：正常唤醒后的 listen start
        await ws.send_json({"type": "listen", "state": "start", "mode": "auto"})
        await send_turn(ws, speech_packets, silence_packets)
        first_ok, first = await wait_tts_stop(ws, 45, "第1轮")
        check("第一轮收到 STT", bool(first["stt"]), first["stt"])
        check("第一轮收到 LLM", bool(first["llm"]), first["llm"])
        check("第一轮 TTS stop", first_ok)

        # 真机在 tts stop 后几十毫秒自动发 listen start；不能等用户重新唤醒。
        await ws.send_json({"type": "listen", "state": "start", "mode": "auto"})
        # 立刻开口，检验推迟启动期间的预滚缓存不丢首字。
        await send_turn(ws, speech_packets, silence_packets)
        second_ok, second = await wait_tts_stop(ws, 45, "第2轮")
        check("第二轮收到 STT", bool(second["stt"]), second["stt"])
        check("第二轮收到 LLM", bool(second["llm"]), second["llm"])
        check("第二轮 TTS stop", second_ok)
        check("两轮都发了 Opus", first["binary"] > 0 and second["binary"] > 0,
              f"first={first['binary']} second={second['binary']}")

        health = await client.get("/health")
        data = await health.json()
        check("没有卡住的 pending listen", not data.get("pending_starts"),
              str(data.get("pending_starts")))
        last = data.get("last_turn") or {}
        check("last_turn 是第二轮", bool(last.get("ok")), str(last))
        await ws.close()
    finally:
        try:
            tts.close()
        except Exception:
            pass
        await client.close()

    after = (digest(real_memory), digest(real_sleep))
    if before == after:
        ok += 1
        print("  ok   真实 memory.db / sleep.json 哈希不变")
    else:
        fail += 1
        print(f"  FAIL 真实数据被改动 before={before} after={after}")

    print(f"\n结果：{ok} ok / {fail} fail  临时库 {work}")
    return 0 if fail == 0 else 1


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())
