"""大模型：运行链路只走云端，本地 ollama 只留作调试。

当前云端是 Qwen3.8-Flash（阿里云百炼 OpenAI 兼容协议）。
以后整个程序要搬到安卓手机上，只保留 HTTP 调用，不依赖任何本地模型。
换别家云只改 config 里那几个变量，接口不动。

密钥只从环境变量 / robot/.env 读，不写进代码、不写进库、不落日志。
`ROBOT_LLM_BACKEND=ollama` 仍可切回本地，但只建议在 PC 上做对照实验。
"""
from __future__ import annotations

import json
import os
import time
from typing import Iterator, Optional

import requests

from . import config as C


class LLMError(RuntimeError):
    pass


class BudgetExceeded(LLMError):
    """当日 token 预算不足；调用没有发出去。"""


def _from_env_file() -> str:
    """robot/.env —— 一行一个 KEY=VALUE。想自己管密钥就用这个，别写进代码。"""
    f = C.PROJECT / ".env"
    if not f.exists():
        return ""
    try:
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip().removeprefix("export ").strip()
            # 只认当前 provider 的 key：换了 base_url 还去拿别家的 key，
            # 结果一定是 401，而且会把「有 key」误报成可用。
            if k == C.CLOUD_KEY_ENV:
                return v.strip().strip('"').strip("'")
    except Exception:
        pass
    return ""


def _from_dsh_credentials() -> str:
    """兜底：本机 DSH 的凭据库。只读，不复制、不落盘、不进日志。"""
    p = os.path.expanduser("~/.dsh/.credentials.yaml")
    if not os.path.exists(p):
        return ""
    try:
        import yaml
        data = yaml.safe_load(open(p, encoding="utf-8")) or {}
        refs = data.get("refs") or {}
        v = refs.get(C.CLOUD_KEY_ENV)
        if isinstance(v, str) and v.strip():
            return v.strip()
    except Exception:
        pass
    return ""


def key_source() -> str:
    """密钥从哪来的。只返回来源名字，永远不返回值本身。"""
    if (os.environ.get(C.CLOUD_KEY_ENV) or "").strip():
        return f"环境变量 {C.CLOUD_KEY_ENV}"
    if _from_env_file():
        return "robot/.env"
    if _from_dsh_credentials():
        return "~/.dsh/.credentials.yaml"
    return ""


def _cloud_key() -> str:
    v = (os.environ.get(C.CLOUD_KEY_ENV) or "").strip()
    if v:
        return v
    return _from_env_file() or _from_dsh_credentials()


class LLM:
    def __init__(self, backend: Optional[str] = None, model: Optional[str] = None,
                 timeout: Optional[float] = None,
                 system_prompt: Optional[str] = None,
                 history_limit: Optional[int] = None,
                 max_tokens: Optional[int] = None,
                 temperature: Optional[float] = None,
                 enable_thinking: Optional[bool] = None,
                 domain: Optional[str] = None,
                 budget=None) -> None:
        # max_tokens / temperature 给「学习」这类非对话调用用：
        # 提炼知识要长一点的回答（对话那条只要 300 token）。
        self.max_tokens = int(max_tokens if max_tokens is not None else C.CLOUD_MAX_TOKENS)
        self.temperature = float(
            temperature if temperature is not None else C.CLOUD_TEMPERATURE)
        # DashScope 的思考 token 不计入 max_tokens，却会把首字拖到 10–30s。
        # 身体语音单独关掉；默认值仍保持开启，避免影响手机/桌面。
        self.enable_thinking = (
            bool(C.CLOUD_ENABLE_THINKING)
            if enable_thinking is None else bool(enable_thinking))
        self.backend = (backend or C.LLM_BACKEND).strip().lower()
        if self.backend in ("deepseek", "openai", "cloud"):
            self.backend = "cloud"
        self.history_limit = C.LLM_MAX_HISTORY if history_limit is None else int(history_limit)
        # 只有 research / school 纳入每日硬预算；chat 不占这两个额度。
        self.domain = (domain or "").strip().lower() or None
        self.budget = budget
        self.system_prompt = system_prompt or (
            "你是一台桌面机器人的大脑。回答要短，口语化，一两句话说完，"
            "不要用列表，不要用 markdown。你不知道的事就说不知道。"
        )
        self.history: list[dict] = []
        if self.backend == "cloud":
            self.base = C.CLOUD_BASE.rstrip("/")
            self.model = model or C.CLOUD_MODEL
            self.timeout = timeout or C.CLOUD_TIMEOUT
        else:
            self.base = C.OLLAMA_URL.rstrip("/")
            self.model = model or C.LLM_MODEL
            self.timeout = timeout or C.LLM_TIMEOUT

    # ---- 基础 -------------------------------------------------------------
    def available(self) -> tuple[bool, str]:
        if self.backend == "cloud":
            src = key_source()
            if not src:
                return False, f"没有 API Key（{C.CLOUD_KEY_ENV}）"
            return True, f"云端 {self.model} · 密钥来自 {src}"
        try:
            r = requests.get(f"{self.base}/api/tags", timeout=3)
            if r.status_code != 200:
                return False, f"HTTP {r.status_code}"
            names = [m.get("name", "") for m in r.json().get("models", [])]
            if self.model not in names and not any(self.model.split(":")[0] in n for n in names):
                return False, f"模型未安装，现有: {names}"
            return True, "ok"
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"

    def reset(self) -> None:
        self.history.clear()

    def _remember(self, user_text: str, reply: str) -> None:
        self.history.append({"role": "user", "content": user_text})
        self.history.append({"role": "assistant", "content": reply})

    def _messages(self, user_text: str, image_b64: Optional[str] = None,
                  system: Optional[str] = None) -> list[dict]:
        msgs = [{"role": "system", "content": system or self.system_prompt}]
        if self.history_limit > 0:
            msgs.extend(self.history[-self.history_limit:])
        item = {"role": "user", "content": user_text}
        if image_b64 and self.backend == "ollama":
            item["images"] = [image_b64]
        msgs.append(item)
        return msgs

    # ---- 非流式 -----------------------------------------------------------
    def _reserve_budget(self, user_text: str, system: Optional[str]):
        """research/school 调用前原子预留；chat 不占预算。"""
        if self.domain not in ("research", "school"):
            return None
        from .budget import default_budget
        from .persona import rough_tokens
        b = self.budget or default_budget()
        est = max(1, rough_tokens(user_text)
                  + rough_tokens(system or self.system_prompt)
                  + int(self.max_tokens))
        r = b.reserve(self.domain, est)
        if not r.get("ok"):
            raise BudgetExceeded(
                f"{self.domain} 预算不可用: {r.get('reason')} "
                f"(day={r.get('day')} used={r.get('used')} limit={r.get('limit')})")
        if not r.get("reservation_id"):
            return None
        return b, r["reservation_id"], est

    def _settle_budget(self, guard, result: dict) -> None:
        if not guard:
            return
        b, rid, est = guard
        prompt = int(result.get("prompt_tokens") or 0)
        completion = int(result.get("eval_count") or 0)
        known = bool(prompt or completion)
        actual = (prompt + completion) if known else est
        try:
            settled = b.settle(rid, actual, estimated=not known,
                               note="llm_chat")
        except Exception as exc:
            result["budget_error"] = f"{type(exc).__name__}: {exc}"
            return
        if not settled.get("ok"):
            result["budget_error"] = settled.get("reason") or "settle_failed"

    def chat(self, user_text: str, image_b64: Optional[str] = None,
             system: Optional[str] = None) -> dict:
        guard = self._reserve_budget(user_text, system)
        try:
            if self.backend == "cloud":
                result = self._chat_cloud(user_text, system)
            else:
                result = self._chat_ollama(user_text, image_b64, system)
        except Exception:
            if guard:
                b, rid, est = guard
                b.settle(rid, est, estimated=True, note="failed_after_reserve")
            raise
        self._settle_budget(guard, result)
        return result

    def _chat_cloud(self, user_text: str, system: Optional[str]) -> dict:
        key = _cloud_key()
        if not key:
            raise LLMError(f"没有 API Key：请设置环境变量 {C.CLOUD_KEY_ENV}")
        payload = {
            "model": self.model,
            "messages": self._messages(user_text, None, system),
            "stream": False,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "enable_thinking": self.enable_thinking,
        }
        t0 = time.time()
        try:
            r = requests.post(
                f"{self.base}/chat/completions",
                headers={"Authorization": f"Bearer {key}",
                         "Content-Type": "application/json"},
                json=payload, timeout=self.timeout,
            )
        except Exception as e:
            raise LLMError(f"调用失败: {type(e).__name__}: {e}") from e
        if r.status_code != 200:
            # 把服务端原话带出来，别替它猜原因
            snippet = (r.text or "")[:300].replace(key, "***")
            raise LLMError(f"HTTP {r.status_code}: {snippet}")
        try:
            data = r.json()
            reply = (data["choices"][0]["message"]["content"] or "").strip()
        except Exception as e:
            raise LLMError(f"返回格式不对: {type(e).__name__}: {e}") from e
        self._remember(user_text, reply)
        usage = data.get("usage") or {}
        return {
            "text": reply,
            "seconds": time.time() - t0,
            "eval_count": usage.get("completion_tokens"),
            "prompt_tokens": usage.get("prompt_tokens"),
            "eval_duration": None,
        }

    def _chat_ollama(self, user_text: str, image_b64: Optional[str],
                     system: Optional[str]) -> dict:
        payload = {
            "model": self.model,
            "messages": self._messages(user_text, image_b64, system),
            "stream": False,
            "think": C.LLM_THINK,
            "options": {"temperature": 0.7, "num_predict": 220},
        }
        t0 = time.time()
        try:
            r = requests.post(f"{self.base}/api/chat", json=payload, timeout=self.timeout)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            raise LLMError(f"调用失败: {type(e).__name__}: {e}") from e
        reply = (data.get("message") or {}).get("content", "").strip()
        self._remember(user_text, reply)
        return {
            "text": reply,
            "seconds": time.time() - t0,
            "eval_count": data.get("eval_count"),
            "prompt_tokens": data.get("prompt_eval_count"),
            "eval_duration": data.get("eval_duration"),
        }

    # ---- 流式 -------------------------------------------------------------
    def chat_stream(self, user_text: str, image_b64: Optional[str] = None,
                    system: Optional[str] = None) -> Iterator[str]:
        if self.backend == "cloud":
            yield from self._stream_cloud(user_text, system)
            return
        yield from self._stream_ollama(user_text, image_b64, system)

    def _stream_cloud(self, user_text: str, system: Optional[str]) -> Iterator[str]:
        key = _cloud_key()
        if not key:
            raise LLMError(f"没有 API Key：请设置环境变量 {C.CLOUD_KEY_ENV}")
        payload = {
            "model": self.model, "messages": self._messages(user_text, None, system),
            "stream": True, "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "enable_thinking": self.enable_thinking,
        }
        acc: list[str] = []
        with requests.post(f"{self.base}/chat/completions",
                           headers={"Authorization": f"Bearer {key}"},
                           json=payload, timeout=self.timeout, stream=True) as r:
            if r.status_code != 200:
                raise LLMError(f"HTTP {r.status_code}: {(r.text or '')[:200]}")
            for line in r.iter_lines(decode_unicode=True):
                if not line or not line.startswith("data:"):
                    continue
                body = line[5:].strip()
                if body == "[DONE]":
                    break
                try:
                    obj = json.loads(body)
                except json.JSONDecodeError:
                    continue
                piece = (obj.get("choices") or [{}])[0].get("delta", {}).get("content") or ""
                if piece:
                    acc.append(piece)
                    yield piece
        self._remember(user_text, "".join(acc))

    def _stream_ollama(self, user_text: str, image_b64: Optional[str],
                       system: Optional[str]) -> Iterator[str]:
        payload = {
            "model": self.model,
            "messages": self._messages(user_text, image_b64, system),
            "stream": True, "think": C.LLM_THINK,
            "options": {"temperature": 0.7, "num_predict": 220},
        }
        with requests.post(f"{self.base}/api/chat", json=payload,
                           timeout=self.timeout, stream=True) as r:
            r.raise_for_status()
            acc = []
            for line in r.iter_lines(decode_unicode=True):
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                piece = (obj.get("message") or {}).get("content", "")
                if piece:
                    acc.append(piece)
                    yield piece
                if obj.get("done"):
                    break
            self._remember(user_text, "".join(acc))
