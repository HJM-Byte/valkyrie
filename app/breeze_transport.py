"""BreezeBlue HTTP 传输层：多边缘节点 + 流式合成 + 短超时。

为什么不用一次普通 POST：
- 2026-11-03 夜间旧域名 api.breeze.blue 的 Cloudflare 节点在 TLS 握手和
  首包阶段会随机卡 20–40 秒；同样格式的请求换到 api.breezeblue.ai 后
  2.3–3.7 秒出完整音频，x-api-key/voice_id 都不用改。
- 普通 POST 要等整段合成完才返回，长读超时会把本来能用的请求判死；
  流式端点每收到一块就续一次读超时，TTFB 也更早。

这个模块只做传输，不做音色/缓存决策；缓存和降级仍在 app.tts。
"""
from __future__ import annotations

import http.client
import json
import logging
import socket
import ssl
import time
from pathlib import Path
from typing import Iterable, Iterator, Optional

HOST = "api.breezeblue.ai"

log = logging.getLogger("valkyrie.tts.breeze")

# 2026-11-03 23:40 实测：.ai 的 172.67.138.101 约 2.3–2.8 秒，
# 104.21.86.248 约 2.8–6.4 秒；两个都 200 出 audio/mpeg。
# 最后一项 "dns" 不写死 IP，交给系统重新解析，防止节点漂移后全断。
DEFAULT_IPS = (
    "172.67.138.101",
    "104.21.86.248",
    "dns",
)

# 4xx 中这些错误重试其它节点没有意义，直接上报。
# 403 不列入：某些 Cloudflare 边缘会对该 zone 返回 403，换节点仍可能成功。
_FATAL_STATUSES = {400, 401, 402, 404, 422}


class BreezeError(RuntimeError):
    def __init__(self, message: str, *, status: Optional[int] = None,
                 code: str = "", ip: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.ip = ip


class BreezeTransport:
    def __init__(self, api_key: str, voice_id: str,
                 host: str = HOST,
                 ips: Iterable[str] = DEFAULT_IPS,
                 connect_timeout: float = 2.0,
                 header_timeout: float = 3.0,
                 read_timeout: float = 5.0,
                 total_timeout: float = 6.0,
                 max_seconds: float = 5.0) -> None:
        self.api_key = (api_key or "").strip()
        self.voice_id = (voice_id or "").strip()
        self.host = (host or HOST).strip() or HOST
        # "dns" 是一个占位节点：不写死边缘 IP，让 create_connection 重新解析。
        self.ips = [ip.strip() for ip in ips if ip and ip.strip()]
        if not self.ips:
            self.ips = list(DEFAULT_IPS)
        self.connect_timeout = max(0.8, float(connect_timeout))
        self.header_timeout = max(1.0, float(header_timeout))
        self.read_timeout = max(1.0, float(read_timeout))
        self.total_timeout = max(self.read_timeout, float(total_timeout))
        # 跨节点的总预算：Cloudflare 坏路时不在四个 IP 上逐个等超时。
        self.max_seconds = max(0.0, float(max_seconds or 0.0))
        self._ssl = ssl.create_default_context()
        self.last_ip = ""
        self.last_first_chunk_ms = 0
        self.last_total_ms = 0
        self._idle: Optional[tuple[str, http.client.HTTPConnection]] = None
        self._idle_at = 0.0
        # Cloudflare 的 Anycast 地址会随调度变化：硬编码 IP 一旦过期，
        # 旧节点会逐个吃满 connect/header 超时，真正能用的 DNS 节点反而
        # 排在队尾。这里缓存一次 IPv4 解析结果，定期刷新并排到队首。
        self._resolved_at = 0.0

    # ---- 连接 ------------------------------------------------------------
    def _connect(self, ip: str,
                 timeout: Optional[float] = None) -> http.client.HTTPConnection:
        sec = self.connect_timeout if timeout is None else max(
            0.5, min(self.connect_timeout, float(timeout)))
        target = self.host if ip in ("", "dns") else ip
        sock = socket.create_connection((target, 443), timeout=sec)
        try:
            sock.settimeout(sec)
            tls = self._ssl.wrap_socket(sock, server_hostname=self.host)
        except Exception:
            try:
                sock.close()
            except Exception:
                pass
            raise
        tls.settimeout(sec)
        conn = http.client.HTTPConnection(target, 443, timeout=sec)
        conn.sock = tls
        return conn

    @staticmethod
    def _left(deadline: Optional[float]) -> Optional[float]:
        if deadline is None:
            return None
        return deadline - time.monotonic()

    @staticmethod
    def _close(conn: Optional[http.client.HTTPConnection]) -> None:
        if conn is None:
            return
        try:
            conn.close()
        except Exception:
            pass

    def _refresh_targets(self, *, force: bool = False) -> None:
        """把系统当前解析到的 IPv4 排到队首，旧硬编码地址退为后备。

        Cloudflare 的边缘 IP 经常在几分钟内换一组。只按配置里的固定顺序
        尝试时，过期节点会先用完整个 max_seconds，等不到真正可用的节点。
        """
        now = time.monotonic()
        if not force and now - self._resolved_at < 60.0:
            return
        self._resolved_at = now
        try:
            infos = socket.getaddrinfo(
                self.host, 443, socket.AF_INET, socket.SOCK_STREAM
            )
        except Exception:
            return
        fresh: list[str] = []
        for info in infos:
            ip = str(info[4][0])
            if ip and ip not in fresh:
                fresh.append(ip)
        if not fresh:
            return
        # 配置里经过实测的节点放前面；DNS 结果只作后备。
        # 直接把 DNS 放队首会被 50% 丢包的 Anycast 地址吃掉整个预算，
        # 即使它 TLS 能握手，HTTP 请求也可能卡到超时。
        rest = [ip for ip in self.ips
                if ip not in fresh and ip not in ("", "dns")]
        self.ips = rest + fresh
        # 上次成功的节点如果在列表里，仍然优先用它。
        if self.last_ip and self.last_ip in self.ips:
            self.ips.remove(self.last_ip)
            self.ips.insert(0, self.last_ip)

    def _take_idle(self) -> Optional[tuple[str, http.client.HTTPConnection]]:
        idle, self._idle = self._idle, None
        return idle

    def _reorder(self, ip: str, ok: bool) -> None:
        """成功的节点提前，失败的节点放最后；下次少撞已知坏路。"""
        try:
            self.ips.remove(ip)
        except ValueError:
            pass
        if ok:
            self.ips.insert(0, ip)
        else:
            self.ips.append(ip)

    # ---- 流式请求 --------------------------------------------------------
    def _stream_once(self, conn: http.client.HTTPConnection,
                     payload: dict, out: Path,
                     deadline: Optional[float] = None) -> None:
        def sec(value: float) -> float:
            left = self._left(deadline)
            return value if left is None else max(0.5, min(value, left))

        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        path = f"/v1/text-to-speech/{self.voice_id}/stream?output_format=mp3"
        headers = {
            "Host": self.host,
            "xi-api-key": self.api_key,
            "User-Agent": "valkyrie-voice/2.0",
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
            "Content-Length": str(len(body)),
        }
        t0 = time.perf_counter()
        conn.request("POST", path, body=body, headers=headers)
        # 握手/等首包和读音频分开计时：某个节点如果连上了却不回包，
        # 8 秒就放弃换路，不让 20 秒读超时吃掉整个手机请求。
        if conn.sock is not None:
            conn.sock.settimeout(sec(self.header_timeout))
        resp = conn.getresponse()
        if conn.sock is not None:
            conn.sock.settimeout(sec(self.read_timeout))
        self.last_first_chunk_ms = 0
        if resp.status != 200:
            raw = resp.read(4096)
            try:
                detail = json.loads(raw.decode("utf-8", "replace"))
                code = str(detail.get("code") or "")
                message = str(detail.get("detail") or detail.get("error") or "")
            except Exception:
                code = ""
                message = raw.decode("utf-8", "replace")[:300]
            raise BreezeError(
                f"HTTP {resp.status} {code}: {message}".strip(),
                status=resp.status, code=code)
        if not (resp.getheader("Content-Type") or "").lower().startswith("audio/"):
            # 不是音频通常意味着网关返回了 HTML/JSON 错误页。
            chunk = resp.read(1024)
            raise BreezeError(
                f"非音频响应: {chunk.decode('utf-8', 'replace')[:200]}",
                status=resp.status)
        tmp = out.with_suffix(out.suffix + ".tmp")
        total = 0
        try:
            with tmp.open("wb") as f:
                while True:
                    chunk = resp.read(16384)
                    if not chunk:
                        break
                    if not self.last_first_chunk_ms:
                        self.last_first_chunk_ms = int(
                            (time.perf_counter() - t0) * 1000)
                    f.write(chunk)
                    total += len(chunk)
                    if time.perf_counter() - t0 > self.total_timeout:
                        raise TimeoutError("流式合成超过总超时")
                    if deadline is not None and time.monotonic() > deadline:
                        raise TimeoutError("Breeze 总预算耗尽")
            if total <= 0:
                raise BreezeError("流式响应没有音频数据")
            tmp.replace(out)
        except Exception:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
            raise
        finally:
            try:
                resp.close()
            except Exception:
                pass
        self.last_total_ms = int((time.perf_counter() - t0) * 1000)

    def _request_pcm(self, conn: http.client.HTTPConnection,
                     payload: dict,
                     deadline: Optional[float] = None
                     ) -> http.client.HTTPResponse:
        """发一次 PCM 流式请求并校验响应头，返回未读完的 response。"""
        def sec(value: float) -> float:
            left = self._left(deadline)
            return value if left is None else max(0.5, min(value, left))

        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        path = f"/v1/text-to-speech/{self.voice_id}/stream?output_format=pcm"
        headers = {
            "Host": self.host,
            "xi-api-key": self.api_key,
            "User-Agent": "valkyrie-voice/2.0",
            "Content-Type": "application/json",
            "Accept": "audio/pcm, application/octet-stream",
            "Content-Length": str(len(body)),
        }
        conn.request("POST", path, body=body, headers=headers)
        if conn.sock is not None:
            conn.sock.settimeout(sec(self.header_timeout))
        resp = conn.getresponse()
        if conn.sock is not None:
            conn.sock.settimeout(self.read_timeout)
        if resp.status != 200:
            raw = resp.read(4096)
            try:
                detail = json.loads(raw.decode("utf-8", "replace"))
                code = str(detail.get("code") or "")
                message = str(detail.get("detail") or detail.get("error") or "")
            except Exception:
                code = ""
                message = raw.decode("utf-8", "replace")[:300]
            raise BreezeError(
                f"HTTP {resp.status} {code}: {message}".strip(),
                status=resp.status, code=code)
        ctype = (resp.getheader("Content-Type") or "").lower()
        if ctype and not (ctype.startswith("audio/")
                          or ctype.startswith("application/octet-stream")):
            chunk = resp.read(1024)
            raise BreezeError(
                f"非音频响应（{ctype}）: "
                f"{chunk.decode('utf-8', 'replace')[:200]}",
                status=resp.status)
        return resp

    def iter_pcm(self, text: str, *,
                 model_id: str = "breeze-tts-2",
                 instructions: str = "",
                 chunk_size: int = 4096) -> Iterator[bytes]:
        """逐块产出 Breeze 的 24k/单声道/s16le PCM。

        只在**尚未产出任何字节**时换节点重试；一旦开始 yield，后面的
        断流只能上抛，绝不能悄悄从头重合成，否则板子会听到重复半句。
        """
        if not self.api_key:
            raise BreezeError("未设置 BREEZE_API_KEY")
        if not self.voice_id:
            raise BreezeError("未设置 BREEZE_VOICE_ID")
        payload = {
            "text": text,
            "model_id": model_id,
        }
        if instructions:
            payload["instructions"] = instructions

        self._refresh_targets()
        setup_deadline = (time.monotonic() + self.max_seconds
                          if self.max_seconds > 0 else None)
        errors: list[str] = []
        t0 = time.perf_counter()
        self.last_first_chunk_ms = 0
        self.last_total_ms = 0

        # 先试上一轮留下的 keep-alive 连接。超过 6 秒的连接 Breeze 多半
        # 已经关掉，继续复用只会白等一轮读超时。
        idle = self._take_idle()
        attempts: list[tuple[str, Optional[http.client.HTTPConnection]]] = []
        if idle is not None:
            if time.monotonic() - self._idle_at <= 6.0:
                attempts.append(("reuse", idle[1]))
            else:
                self._close(idle[1])
        for ip in list(self.ips):
            attempts.append((ip, None))

        for index, (label, conn) in enumerate(attempts):
            got_any = False
            received = 0
            ip = label if label != "reuse" else (
                idle[0] if idle is not None else "")
            completed = False
            try:
                if conn is None:
                    left = self._left(setup_deadline)
                    if left is not None and left <= 0.5:
                        errors.append("总预算耗尽")
                        break
                    conn = self._connect(ip, timeout=left)
                resp = self._request_pcm(
                    conn, payload, deadline=setup_deadline)
                # 首包之后 Breeze 的块间间隔实测 <=193ms；偶发“音频已收全
                # 但服务端迟迟不发 EOF”（见过 19.3s）会把整轮对话卡住。
                # 读静默超过 2 秒就按流结束处理，宁可少收尾巴，不能堵住
                # 下一轮；首包前仍用正常读超时，不能把慢合成误判为断流。
                if conn.sock is not None:
                    conn.sock.settimeout(min(2.0, self.read_timeout))
                while True:
                    try:
                        chunk = resp.read(chunk_size)
                    except TimeoutError:
                        if got_any:
                            log.warning(
                                "Breeze PCM 流静默超时，按结束处理（已收 %d 字节）",
                                received)
                            break
                        raise
                    if not chunk:
                        break
                    received += len(chunk)
                    if not got_any:
                        got_any = True
                        self.last_first_chunk_ms = int(
                            (time.perf_counter() - t0) * 1000)
                    yield chunk
                try:
                    resp.close()
                except Exception:
                    pass
                completed = True
                self.last_total_ms = int(
                    (time.perf_counter() - t0) * 1000)
                self.last_ip = ip
                self._reorder(ip, True)
                self._idle = (ip, conn)
                self._idle_at = time.monotonic()
                return
            except GeneratorExit:
                self._close(conn)
                raise
            except Exception as e:
                self._close(conn)
                if label != "reuse":
                    self._reorder(ip, False)
                errors.append(
                    f"{ip or label}: {type(e).__name__}: {str(e)[:160]}")
                if got_any:
                    # 已经出过音，不能换节点重来；让上层收尾并报失败。
                    raise BreezeError(
                        "PCM 流在首块之后中断: " + errors[-1]) from e
                continue
            finally:
                if not completed and got_any:
                    self._close(conn)

        raise BreezeError("PCM 流式合成失败: " + " | ".join(errors[-5:]))

    def prewarm(self) -> bool:
        """先建好 TLS 连接，别让第一句话多付一次握手。

        只建连，不发送合成请求，不产生云端费用。旧连接可能已被对端关掉；
        这里保留 6 秒内的连接，避免后台保活每几秒重建一次 TLS。真正合成
        失败时仍走原来的多节点重试。
        """
        idle = self._idle
        if idle is not None and time.monotonic() - self._idle_at <= 6.0:
            return True
        self._refresh_targets()
        # 只预连队首节点。把“TLS 能握手”当成“HTTP 能出音”会选到
        # 丢包 50% 的 Anycast 地址，下一句话反而多等一轮读超时；
        # 队首失败时保留旧空闲连接，不能把上一条能用的连接先关掉。
        for ip in list(self.ips[:1]):
            conn = None
            try:
                conn = self._connect(ip)
            except Exception:
                self._close(conn)
                self._reorder(ip, False)
                continue
            old = self._take_idle()
            if old is not None:
                self._close(old[1])
            self.last_ip = ip
            self._reorder(ip, True)
            self._idle = (ip, conn)
            self._idle_at = time.monotonic()
            return True
        return False

    def synthesize(self, text: str, out: Path, *,
                   model_id: str = "breeze-tts-2",
                   language_code: str = "zh",
                   instructions: str = "") -> Path:
        if not self.api_key:
            raise BreezeError("未设置 BREEZE_API_KEY")
        if not self.voice_id:
            raise BreezeError("未设置 BREEZE_VOICE_ID")
        payload = {
            "text": text,
            "model_id": model_id,
            "language_code": language_code,
        }
        if instructions:
            payload["instructions"] = instructions
        out = Path(out)
        out.parent.mkdir(parents=True, exist_ok=True)
        errors: list[str] = []
        deadline = (time.monotonic() + self.max_seconds
                    if self.max_seconds > 0 else None)
        self._refresh_targets()

        # 优先复用上一轮成功的 keep-alive 连接；失败再按 IP 列表重建。
        idle = self._take_idle()
        idle_failed = ""
        if idle is not None and time.monotonic() - self._idle_at > 6.0:
            # Breeze/Cloudflare 会很快关掉空闲连接；复用一条已死的连接
            # 会让第一句白白等一轮 4 秒超时再换节点。超过 6 秒直接新建。
            self._close(idle[1])
            idle = None
        if idle is not None:
            ip, conn = idle
            try:
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError("Breeze 总预算耗尽")
                self._stream_once(conn, payload, out, deadline=deadline)
                self.last_ip = ip
                self._reorder(ip, True)
                self._idle = (ip, conn)
                self._idle_at = time.monotonic()
                return out
            except Exception as e:
                errors.append(f"{ip}(reuse): {type(e).__name__}: {str(e)[:120]}")
                self._close(conn)
                self._reorder(ip, False)
                idle_failed = ip

        # Cloudflare 的失败经常是“这一次连接”的问题，不是节点永久坏了。
        # 第一轮按列表换节点，第二轮再给失败节点一次短机会；总预算仍由
        # max_seconds 卡死，不会回到 20 秒以上的空等。
        exhausted = False
        fatal = False
        for round_no in range(2):
            attempted: set[str] = set()
            for ip in list(self.ips):
                if ip in attempted:
                    continue
                attempted.add(ip)
                if round_no == 0 and ip == idle_failed:
                    continue
                left = self._left(deadline)
                if left is not None and left <= 0.5:
                    errors.append("总预算耗尽")
                    exhausted = True
                    break
                conn = None
                try:
                    conn = self._connect(ip, timeout=left)
                    self._stream_once(conn, payload, out, deadline=deadline)
                    self.last_ip = ip
                    self._reorder(ip, True)
                    # 流式响应读完，若服务端保持连接则留给下一句，省一次握手。
                    self._idle = (ip, conn)
                    self._idle_at = time.monotonic()
                    return out
                except BreezeError as e:
                    self._close(conn)
                    self._reorder(ip, False)
                    errors.append(f"{ip}: {e}")
                    if e.status in _FATAL_STATUSES:
                        fatal = True
                        break
                except Exception as e:
                    self._close(conn)
                    self._reorder(ip, False)
                    errors.append(f"{ip}: {type(e).__name__}: {str(e)[:120]}")
            if exhausted or fatal:
                break
        raise BreezeError("所有 Breeze 节点失败: " + " | ".join(errors[-4:]))

    def close(self) -> None:
        idle = self._take_idle()
        if idle is not None:
            self._close(idle[1])
