"""假 ESP32 身体：走一遍真实 OTA -> WebSocket hello，给 A2 协议自检用。

    cd robot
    C:\\robot\\venv\\Scripts\\python.exe -m tools.fake_body --seconds 5

它不会写 memory.db、不会启动 Chat；只验证协议与服务端。
真板子联调时用 `tools.fake_body` 对 `app.xiaozhi_server`，真身体走 `desktop.py --body`。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

import aiohttp
import websockets
from websockets.exceptions import ConnectionClosed

from app import body_audio

DEFAULT_OTA = "http://127.0.0.1:8768/xiaozhi/ota/"
DEFAULT_WS = "ws://127.0.0.1:8768/xiaozhi/v1/"
CLIENT_VERSION = "1"
HELLO_AUDIO = {
    "format": "opus",
    "sample_rate": 16000,
    "channels": 1,
    "frame_duration": 60,
}


def device_headers(device_id: str, client_id: str, token: str) -> dict[str, str]:
    headers = {
        "Device-Id": device_id,
        "Client-Id": client_id,
        "Protocol-Version": CLIENT_VERSION,
        "User-Agent": "fake-body/0.1 (A2 protocol test)",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


async def fetch_ota(ota_url: str, headers: dict[str, str]) -> dict[str, Any]:
    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout) as http:
        async with http.post(
            ota_url,
            json={"application": {"name": "fake-body", "version": "0.1"}},
            headers=headers,
        ) as resp:
            text = await resp.text()
            print(f"[ota] HTTP {resp.status} {text[:800]}")
            if resp.status != 200:
                raise RuntimeError(f"OTA HTTP {resp.status}")
            data = json.loads(text)
            if not isinstance(data, dict):
                raise RuntimeError("OTA 响应不是 JSON 对象")
            ws = data.get("websocket")
            if not isinstance(ws, dict) or not ws.get("url"):
                raise RuntimeError("OTA 响应缺少 websocket.url")
            for forbidden in ("mqtt", "activation", "firmware"):
                if forbidden in data:
                    raise RuntimeError(f"OTA 响应不应该包含 {forbidden}")
            print(f"[ota] websocket={ws['url']} token={ws.get('token', '')!r}")
            return data


async def run(args: argparse.Namespace) -> int:
    headers = device_headers(args.device_id, args.client_id, args.token)
    if args.ws_url:
        ws_url = args.ws_url
        print(f"[ws] 跳过 OTA，直连 {ws_url}")
        token = args.token
    else:
        try:
            ota = await fetch_ota(args.ota_url, headers)
        except Exception as exc:
            print(f"[fail] OTA: {exc}")
            return 2
        ws_url = ota["websocket"]["url"]
        token = str(ota["websocket"].get("token") or args.token)
        headers["Authorization"] = f"Bearer {token}"

    print(f"[ws] 连接 {ws_url} device={args.device_id}")
    got_hello = False
    got_audio = False
    audio_frames = 0
    audio_bytes = 0
    try:
        async with websockets.connect(
            ws_url,
            additional_headers=headers,
            open_timeout=8,
            ping_interval=20,
            ping_timeout=20,
            max_size=2 * 1024 * 1024,
        ) as ws:
            hello = {
                "type": "hello",
                "version": 1,
                "features": {"mcp": True},
                "transport": "websocket",
                "audio_params": dict(HELLO_AUDIO),
            }
            print(f"[ws] -> {json.dumps(hello, ensure_ascii=False)}")
            await ws.send(json.dumps(hello, ensure_ascii=False))

            deadline = time.monotonic() + max(0.5, args.seconds)
            while time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=min(1.0, remaining))
                except asyncio.TimeoutError:
                    continue
                except ConnectionClosed as exc:
                    print(f"[ws] 连接关闭 code={getattr(exc, 'code', '?')} "
                          f"reason={getattr(exc, 'reason', '')!r}")
                    break

                if isinstance(raw, bytes):
                    got_audio = True
                    audio_frames += 1
                    audio_bytes += len(raw)
                    print(f"[ws] <- binary {len(raw)}B")
                    continue

                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    print(f"[ws] <- non-json text {raw[:200]!r}")
                    continue
                print(f"[ws] <- {json.dumps(msg, ensure_ascii=False)[:800]}")
                if msg.get("type") == "hello":
                    got_hello = True
                    print(f"[ok] server hello transport={msg.get('transport')} "
                          f"session={msg.get('session_id')} audio={msg.get('audio_params')}")
                    if args.send_audio:
                        pcm = body_audio.sine_pcm16(440.0, 0.12)
                        packets = body_audio.pcm16_to_opus(pcm)
                        print(f"[ws] -> {len(packets)} 个 Opus 包做上行测试")
                        for packet in packets:
                            await ws.send(packet)
                            await asyncio.sleep(0.01)
                elif msg.get("type") == "error":
                    print(f"[fail] server error: {msg}")
                    return 3

            if not got_hello:
                print("[fail] 没有收到 server hello")
                return 4
            print(f"[ok] 会话结束 hello={got_hello} audio={got_audio} "
                  f"frames={audio_frames} bytes={audio_bytes}")
            return 0
    except Exception as exc:
        print(f"[fail] WebSocket: {type(exc).__name__}: {exc}")
        return 3


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="假 ESP32 身体（A2 协议自检）")
    ap.add_argument("--ota-url", default=DEFAULT_OTA)
    ap.add_argument("--ws-url", default="", help="给了就跳过 OTA，直接连 WS")
    ap.add_argument("--device-id", default="fake-body-68ee8f5c60ec")
    ap.add_argument("--client-id", default="fake-client-a2")
    ap.add_argument("--token", default="local-dev")
    ap.add_argument("--seconds", type=float, default=8.0)
    ap.add_argument("--send-audio", action="store_true", help="hello 后发两个 Opus 包")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    args = parse_args(argv)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
