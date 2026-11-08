"""A4.2 协议自检：真实 TTS 语音 -> WS 上行 -> 服务端 STT/Chat/TTS -> 下行。

    cd robot
    C:\\robot\\venv\\Scripts\\python.exe -m tools.selftest_body_chat_server

使用 aiohttp TestServer，不真连板子；验证的是 `xiaozhi_server --chat` 的完整路径。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
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


async def run() -> int:
    print("—— A4.2 服务端协议自检 ——")
    real_memory = C.DATA / "memory.db"
    real_sleep = C.DATA / "sleep.json"
    before = (digest(real_memory), digest(real_sleep))
    stamp = time.strftime("%Y%m%d_%H%M%S")
    work = C.DATA / "_selftest_body_server" / stamp
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
        silence = body_audio.pcm16_to_opus(
            body_audio.sine_pcm16(0.0, 1.2, amplitude=0.0))
        print(f"  上行：语音 {len(speech_packets)} 包 + 静音 {len(silence)} 包")

        ws = await client.ws_connect(
            body_link.WS_PATH,
            headers={"Device-Id": "selftest-chat-body",
                     "Client-Id": "selftest-chat-client",
                     "Protocol-Version": "1"},
        )
        await ws.send_json(HELLO)
        hello = await ws.receive_json(timeout=10)
        check("server hello", hello.get("type") == "hello")

        await ws.send_json({"type": "listen", "state": "start", "mode": "auto"})
        for packet in speech_packets + silence:
            await ws.send_bytes(packet)
            await asyncio.sleep(0.061)

        stt_text = ""
        llm_text = ""
        tts_started = False
        tts_stopped = False
        binary = 0
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            try:
                msg = await ws.receive(timeout=5)
            except asyncio.TimeoutError:
                continue
            if msg.type == WSMsgType.TEXT:
                obj = json.loads(msg.data)
                typ = obj.get("type")
                if typ == "stt":
                    stt_text = str(obj.get("text") or "")
                    print(f"  <- STT {stt_text!r}")
                elif typ == "llm":
                    llm_text = str(obj.get("text") or "")
                    print(f"  <- LLM {llm_text!r}")
                elif typ == "tts":
                    state = obj.get("state")
                    print(f"  <- TTS {state} {obj.get('text','')!r}")
                    if state == "start":
                        tts_started = True
                    elif state == "stop":
                        tts_stopped = True
                    if tts_stopped:
                        break
            elif msg.type == WSMsgType.BINARY:
                binary += 1
            elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR):
                print(f"  WS 结束：{msg.type}")
                break

        check("收到 STT 文本", bool(stt_text), stt_text)
        check("STT 认出大意", ("你好" in stt_text or "谁" in stt_text), stt_text)
        check("收到 Chat 回答", bool(llm_text), llm_text)
        check("TTS start", tts_started)
        check("下发 Opus 包", binary > 0, f"binary={binary}")
        check("TTS stop", tts_stopped)
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
