"""BreezeBlue 网络路径探针：分阶段测 DNS/TCP/TLS/首包，不做重试。

用法：
    $env:BREEZE_API_KEY = ...     # 只读环境变量，不落盘
    python tools/breeze_net_probe.py --ips 172.67.159.130,104.21.41.34 --path voices
    python tools/breeze_net_probe.py --ips 172.67.159.130 --path tts --text "今天过得怎么样？"

输出每个阶段的耗时和状态码，用来判断慢在握手、上游合成，还是根本没通。
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import socket
import ssl
import time

HOST = "api.breeze.blue"
DEFAULT_IPS = "104.26.0.1,103.31.4.1,104.20.0.1,172.67.159.130,104.21.41.34"
VOICE = "voc_wkzpcy2xxwch"
KEY = os.environ.get("BREEZE_API_KEY", "").strip()


def recv_headers(sock: socket.socket, limit: int = 65536) -> tuple[bytes, bytes]:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
        if len(data) > limit:
            break
    head, _, rest = data.partition(b"\r\n\r\n")
    return head, rest


def read_response(sock: socket.socket, head: bytes, initial: bytes = b"") -> bytes:
    """只用于本次探针：读完 Content-Length 或 chunked/close 响应。"""
    body = initial
    if b"\r\n\r\n" in head:
        head, _, extra = head.partition(b"\r\n\r\n")
        body = extra + body
    lines = head.decode("latin1", "replace").split("\r\n")
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    if "content-length" in headers:
        need = int(headers["content-length"])
        while len(body) < need:
            chunk = sock.recv(65536)
            if not chunk:
                break
            body += chunk
        return head + b"\r\n\r\n" + body[:need]
    if headers.get("transfer-encoding", "").lower() == "chunked":
        out = body
        while True:
            while b"\r\n" not in out:
                out += sock.recv(65536)
            line, _, rest = out.partition(b"\r\n")
            size = int(line.strip().split(b";")[0], 16)
            if size == 0:
                return head + b"\r\n\r\n" + body
            while len(rest) < size + 2:
                rest += sock.recv(65536)
            out = rest[size + 2:]
            body += rest[:size + 2]
    # Connection: close 且没有长度
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            break
        body += chunk
    return head + b"\r\n\r\n" + body


def read_stream(sock: socket.socket, head: bytes, initial: bytes,
                t0: float) -> dict:
    """读流式响应：只记录首块字节、总字节和结束时间。"""
    body = initial
    first_ms = None
    if body:
        first_ms = round((time.perf_counter() - t0) * 1000)
    # chunked 或 close 都可以；只要收到数据就开始计时，读到 EOF 为止。
    while True:
        chunk = sock.recv(16384)
        if not chunk:
            break
        if first_ms is None:
            first_ms = round((time.perf_counter() - t0) * 1000)
        body += chunk
        if len(body) > 8 * 1024 * 1024:
            break
    return {"first_audio_ms": first_ms, "stream_bytes": len(body),
            "total_ms": round((time.perf_counter() - t0) * 1000)}


def probe(ip: str, path: str, text: str, timeout: float,
          tls12: bool = False) -> dict:
    result = {"ip": ip, "ok": False, "stage": "init"}
    t0 = time.perf_counter()
    sock = None
    try:
        result["stage"] = "tcp"
        sock = socket.create_connection((ip, 443), timeout=timeout)
        result["tcp_ms"] = round((time.perf_counter() - t0) * 1000)
        ctx = ssl.create_default_context()
        if tls12:
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            ctx.maximum_version = ssl.TLSVersion.TLSv1_2
        result["stage"] = "tls"
        sock = ctx.wrap_socket(sock, server_hostname=HOST)
        result["tls_ms"] = round((time.perf_counter() - t0) * 1000)
        result["tls_version"] = sock.version()
        if path == "tls":
            result["ok"] = True
            result["stage"] = "done"
            return result
        if path == "voices":
            req = (
                f"GET /v1/voices?page_size=1 HTTP/1.1\r\n"
                f"Host: {HOST}\r\n"
                f"xi-api-key: {KEY}\r\n"
                "User-Agent: valkyrie-net-probe/1.0\r\n"
                "Accept: application/json\r\n"
                "Connection: close\r\n\r\n"
            ).encode()
        else:
            body = json.dumps({
                "text": text,
                "model_id": "breeze-tts-2",
                "language_code": "zh",
            }, ensure_ascii=False).encode("utf-8")
            endpoint = (f"/v1/text-to-speech/{VOICE}")
            if path == "stream":
                endpoint += "/stream"
            req = (
                f"POST {endpoint}?output_format=mp3 HTTP/1.1\r\n"
                f"Host: {HOST}\r\n"
                f"xi-api-key: {KEY}\r\n"
                "User-Agent: valkyrie-net-probe/1.0\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\n"
                "Accept: audio/mpeg\r\n"
                "Connection: close\r\n\r\n"
            ).encode() + body
        result["stage"] = "send"
        sock.sendall(req)
        result["stage"] = "first_byte"
        head, initial = recv_headers(sock)
        result["ttfb_ms"] = round((time.perf_counter() - t0) * 1000)
        status = head.split(b"\r\n", 1)[0].decode("latin1", "replace")
        result["status"] = status
        if path == "stream":
            result.update(read_stream(sock, head, initial, t0))
            result["ok"] = (status.startswith("HTTP/1.1 200")
                            and result["stream_bytes"] > 0)
            result["stage"] = "done"
            return result
        result["stage"] = "body"
        data = read_response(sock, head, initial)
        result["total_ms"] = round((time.perf_counter() - t0) * 1000)
        body = data.partition(b"\r\n\r\n")[2]
        result["bytes"] = len(body)
        result["ok"] = status.startswith("HTTP/1.1 200") and len(body) > 0
        result["stage"] = "done"
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
        result["elapsed_ms"] = round((time.perf_counter() - t0) * 1000)
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ips", default=os.environ.get("BREEZE_PROBE_IPS", DEFAULT_IPS))
    ap.add_argument("--path", choices=("tls", "voices", "tts", "stream"), default="voices")
    ap.add_argument("--text", default="今天过得怎么样？")
    ap.add_argument("--timeout", type=float, default=25.0)
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--tls12", action="store_true",
                    help="强制 TLS 1.2（对比 TLS 1.3 握手是否被中间设备卡住）")
    ap.add_argument("--workers", type=int, default=1,
                    help="并发探测数；扫描 IP 池时设成 8–16 更快")
    args = ap.parse_args()
    if not KEY and args.path != "tls":
        raise SystemExit("缺少 BREEZE_API_KEY")
    ips = [x.strip() for x in args.ips.split(",") if x.strip()]
    def run(ip: str) -> dict:
        return probe(ip, args.path, args.text, args.timeout, args.tls12)
    if args.workers > 1:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
            for result in pool.map(run, ips):
                print(json.dumps(result, ensure_ascii=False), flush=True)
    else:
        for ip in ips:
            print(json.dumps(run(ip), ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
