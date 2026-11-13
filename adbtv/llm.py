"""大模型客户端：支持 Anthropic /v1/messages 与 OpenAI /v1/chat/completions。

本机 7866 网关是 Anthropic 协议（本地模型，纯文本）；
云端可填 OpenAI 兼容接口（可带视觉）。
"""
from __future__ import annotations

import base64
import json
import urllib.request
import urllib.error
from typing import Any


class LLMError(RuntimeError):
    """Base error for model transport/protocol failures."""


class LLMTimeout(LLMError):
    """The request exceeded the caller's remaining budget."""


class LLMHTTPError(LLMError):
    """Upstream returned a non-2xx status."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status

    @property
    def retryable(self):
        return self.status in (408, 425, 429, 500, 502, 503, 504)


class LLMConnectionError(LLMError):
    """DNS/TCP/TLS failure before any response."""


class LLMClient:
    def __init__(self, cfg: dict[str, Any]):
        lc = cfg.get("llm", {})
        self.provider = (lc.get("provider") or "none").lower()
        self.base_url = (lc.get("base_url") or "").rstrip("/")
        self.api_key = lc.get("api_key") or ""
        self.model = lc.get("model") or ""
        self.vision_model = lc.get("vision_model") or ""
        self.max_tokens = int(lc.get("max_tokens", 1200))
        self.temperature = float(lc.get("temperature", 0.2))
        self.timeout = int(lc.get("timeout", 90))
        self.last_usage: dict[str, Any] = {}
        # Only the known legacy local gateway needs a user-message system shim.
        # Normal cloud endpoints keep their native system-role semantics.
        self.system_in_user = lc.get("system_in_user", self.base_url == "http://127.0.0.1:7866")

    @property
    def enabled(self) -> bool:
        return self.provider in ("anthropic", "openai") and bool(self.base_url) and bool(self.model)

    def _post(self, url: str, headers: dict[str, str], payload: dict[str, Any],
              timeout: float | None = None) -> dict[str, Any]:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        # A caller deadline always wins over the configured ceiling: a 90 s HTTP
        # timeout must never outlive a 14 s task budget.
        budget = self.timeout if timeout is None else min(float(self.timeout), float(timeout))
        if budget <= 0:
            raise LLMTimeout("没有剩余预算，未发起模型请求")
        try:
            with urllib.request.urlopen(req, timeout=max(0.5, budget)) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "ignore")
            raise LLMHTTPError(f"HTTP {e.code} @ {url}: {body[:300]}", status=e.code) from e
        except urllib.error.URLError as e:
            reason = getattr(e, "reason", e)
            if isinstance(reason, TimeoutError) or "timed out" in str(reason).lower():
                raise LLMTimeout(f"模型请求超时({budget:.1f}s)") from e
            raise LLMConnectionError(f"无法连接模型服务: {reason}") from e
        except TimeoutError as e:
            raise LLMTimeout(f"模型请求超时({budget:.1f}s)") from e
        except ValueError as e:
            raise LLMError(f"模型返回非 JSON: {e}") from e
        except Exception as e:  # noqa: BLE001 - transport-specific classes above
            raise LLMConnectionError(f"请求失败 @ {url}: {e}") from e

    # ---------- 文本对话 ----------
    def chat(self, messages: list[dict[str, Any]], system: str | None = None,
             timeout: float | None = None) -> str:
        if not self.enabled:
            raise LLMError("未启用大模型（llm.provider = none 或缺少配置）")
        if system and self.system_in_user:
            messages = [dict(message) for message in messages]
            if messages and messages[0].get("role") == "user" and isinstance(messages[0].get("content"), str):
                messages[0]["content"] = system + "\n\n用户请求：" + messages[0]["content"]
            else:
                messages.insert(0, {"role": "user", "content": system})
            system = None
        if self.provider == "anthropic":
            return self._chat_anthropic(messages, system, timeout)
        return self._chat_openai(messages, system, timeout)

    def _chat_anthropic(self, messages: list[dict[str, Any]], system: str | None,
                        timeout: float | None = None) -> str:
        url = self.base_url + "/v1/messages"
        headers = {"Content-Type": "application/json", "anthropic-version": "2023-06-01"}
        if self.api_key:
            headers["x-api-key"] = self.api_key
        payload: dict[str, Any] = {
            "model": self.model, "max_tokens": self.max_tokens,
            "temperature": self.temperature, "messages": messages,
        }
        if system:
            payload["system"] = system
        resp = self._post(url, headers, payload, timeout=timeout)
        self.last_usage = resp.get("usage", {})
        parts = []
        for block in resp.get("content", []):
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "".join(parts).strip()

    def _chat_openai(self, messages: list[dict[str, Any]], system: str | None,
                     timeout: float | None = None) -> str:
        url = self.base_url + "/v1/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        msgs = list(messages)
        if system:
            msgs = [{"role": "system", "content": system}] + msgs
        payload = {"model": self.model, "messages": msgs,
                   "max_tokens": self.max_tokens, "temperature": self.temperature}
        resp = self._post(url, headers, payload, timeout=timeout)
        self.last_usage = resp.get("usage", {})
        return (resp.get("choices", [{}])[0].get("message", {}).get("content") or "").strip()

    # ---------- 视觉（可选） ----------
    def analyze_image(self, path: str, prompt: str) -> str:
        if not self.vision_model:
            raise LLMError("未配置 llm.vision_model，无法做截图视觉分析")
        with open(path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
        media_type = "image/png"
        if path.lower().endswith((".jpg", ".jpeg")):
            media_type = "image/jpeg"
        if self.provider == "anthropic":
            url = self.base_url + "/v1/messages"
            headers = {"Content-Type": "application/json", "anthropic-version": "2023-06-01"}
            if self.api_key:
                headers["x-api-key"] = self.api_key
            payload = {"model": self.vision_model, "max_tokens": self.max_tokens,
                       "messages": [{"role": "user", "content": [
                           {"type": "image", "source": {"type": "base64",
                                                        "media_type": media_type, "data": b64}},
                           {"type": "text", "text": prompt}]}]}
            resp = self._post(url, headers, payload)
            return "".join(b.get("text", "") for b in resp.get("content", [])).strip()
        # openai
        url = self.base_url + "/v1/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload = {"model": self.vision_model, "max_tokens": self.max_tokens,
                   "messages": [{"role": "user", "content": [
                       {"type": "text", "text": prompt},
                       {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{b64}"}}]}]}
        resp = self._post(url, headers, payload)
        return (resp.get("choices", [{}])[0].get("message", {}).get("content") or "").strip()
