"""BreezeBlue TTS/voice 工具：列音色、取音色、合成音频。

Key 从环境变量 BREEZE_API_KEY 读取，不落盘。
本机到 api.breeze.blue 的默认解析节点不稳定，脚本会在多个可达 CF 节点间重试。
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import time
from pathlib import Path

import requests

HOST = "api.breeze.blue"
_IPS = [x.strip() for x in os.environ.get(
    "BREEZE_FORCE_IPS",
    "104.26.0.1,103.31.4.1,104.20.0.1,172.67.159.130,104.21.41.34",
).split(",") if x.strip()]
_CURRENT_IP = _IPS[0]
_real_gai = socket.getaddrinfo


def _gai(host, port, family=0, type=0, proto=0, flags=0):
    if host == HOST:
        return _real_gai(_CURRENT_IP, port, family, type, proto, flags)
    return _real_gai(host, port, family, type, proto, flags)


socket.getaddrinfo = _gai

KEY = os.environ.get("BREEZE_API_KEY", "").strip()
if not KEY:
    raise SystemExit("缺少 BREEZE_API_KEY 环境变量")


def _new_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"xi-api-key": KEY, "User-Agent": "valkyrie-voice-lab/1.0"})
    return s


def _request(method: str, path: str, *, tries: int = 2, **kwargs):
    last = None
    for attempt in range(tries):
        for ip in _IPS:
            global _CURRENT_IP
            _CURRENT_IP = ip
            s = _new_session()
            try:
                r = s.request(method, f"https://{HOST}{path}", timeout=(12, 180), **kwargs)
                if r.status_code >= 500:
                    last = f"HTTP {r.status_code} via {ip}: {r.text[:160]}"
                    print(last, flush=True)
                    continue
                return r, ip
            except Exception as e:
                last = f"{type(e).__name__} via {ip}: {str(e)[:160]}"
                print(last, flush=True)
            finally:
                s.close()
    raise RuntimeError(f"所有节点失败：{last}")


def search_voices(query: str, page_size: int = 20) -> dict:
    r, ip = _request("GET", "/v1/voices", params={"search": query, "page_size": page_size})
    print("via", ip, "HTTP", r.status_code, flush=True)
    r.raise_for_status()
    return r.json()


def get_voice(voice_id: str) -> dict:
    r, ip = _request("GET", f"/v1/voices/{voice_id}")
    print("via", ip, "HTTP", r.status_code, flush=True)
    r.raise_for_status()
    return r.json()


def synth(voice_id: str, text: str, out: Path, *, model_id: str = "breeze-tts-2",
          language_code: str = "zh", instructions: str = "") -> Path:
    payload = {"text": text, "model_id": model_id, "language_code": language_code}
    if instructions:
        payload["instructions"] = instructions
    r, ip = _request("POST", f"/v1/text-to-speech/{voice_id}",
                     params={"output_format": "mp3"}, json=payload, tries=2)
    print("via", ip, "HTTP", r.status_code, r.headers.get("content-type"), flush=True)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:500]}")
    if not r.content:
        raise RuntimeError("空音频响应")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(r.content)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--search", default=None)
    ap.add_argument("--dump", default=None)
    ap.add_argument("--voice-id", default=None)
    ap.add_argument("--text", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--instructions", default="")
    args = ap.parse_args()

    if args.search:
        data = search_voices(args.search)
        if args.dump:
            Path(args.dump).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            print("dump", args.dump)
        print("count", len(data.get("voices", [])))
        for v in data.get("voices", [])[:10]:
            print(v.get("voice_id"), v.get("name"), v.get("language_code"), v.get("gender"), v.get("age"))
        return 0

    if args.voice_id and not args.text:
        print(json.dumps(get_voice(args.voice_id), ensure_ascii=False, indent=2))
        return 0

    if args.voice_id and args.text and args.out:
        out = Path(args.out)
        synth(args.voice_id, args.text, out, instructions=args.instructions)
        print("wrote", out, out.stat().st_size, "bytes")
        return 0

    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
