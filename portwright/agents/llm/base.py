"""LLM provider port: one chat call with tool support, same shape for every provider."""
from __future__ import annotations

import ssl
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from ...config import ConfigError, SecuritySettings
from ..spec import LlmModelSpec


class LlmError(Exception):
    """A model call failed (message is safe to show; it never contains keys)."""


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LlmResponse:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    stop_reason: str = ""

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


# Neutral message format used by the agent loop (providers translate it):
#   {"role": "user"|"assistant", "content": str, "tool_calls": [ToolCall]?}
#   {"role": "tool", "tool_call_id": str, "name": str, "content": str}
Message = dict[str, Any]


class LlmProvider:
    """Subclasses implement ``chat``. Registered by name in ``PROVIDERS``."""

    def __init__(self, name: str, spec: LlmModelSpec, client: httpx.AsyncClient):
        self.name, self.spec, self.client = name, spec, client

    async def chat(self, system: str, messages: list[Message], tools: list[dict]) -> LlmResponse:
        """tools: [{"name", "description", "parameters": JSON schema}]"""
        raise NotImplementedError

    async def aclose(self) -> None:
        await self.client.aclose()

    def _fail(self, exc: Exception) -> LlmError:
        if isinstance(exc, httpx.HTTPStatusError):
            body = exc.response.text[:300].replace("\n", " ")
            hint = {401: " - the API key was rejected (check the credential and its type)",
                    403: " - access denied for this key or model",
                    404: " - not found: check base_url (OpenAI needs https://api.openai.com/v1) and the model name",
                    429: " - rate limited or out of quota"}.get(exc.response.status_code, "")
            return LlmError(f"model '{self.name}' returned HTTP {exc.response.status_code}{hint}: {body}")
        if isinstance(exc, httpx.TimeoutException):
            return LlmError(f"model '{self.name}' timed out after {self.spec.timeout_seconds:g}s")
        if isinstance(exc, ssl.SSLError) or "CERTIFICATE" in str(exc).upper():
            return LlmError(f"model '{self.name}': TLS error - check tls.ca_bundle / client_cert")
        return LlmError(f"model '{self.name}' call failed: {type(exc).__name__}: {str(exc)[:200]}")


def check_llm_host(base_url: str, security: SecuritySettings) -> None:
    u = urlparse(base_url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ConfigError(f"model base_url '{base_url}' must be an http(s) URL")
    allowed = {h.lower() for h in security.allowed_llm_hosts}
    if u.hostname.lower() not in allowed and u.netloc.lower() not in allowed:
        raise ConfigError(f"LLM host '{u.netloc}' is not in security.allowed_llm_hosts")


def build_client(spec: LlmModelSpec, root: str | Path, headers: dict, params: dict | None = None,
                 auth: httpx.Auth | None = None,
                 transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    """httpx client with the model's CA bundle / client certificate (mTLS) applied."""
    root = Path(root)

    def _path(p: str) -> str:
        q = Path(p)
        q = q if q.is_absolute() else root / q
        if not q.is_file():
            raise ConfigError(f"TLS file '{p}' not found")
        return str(q)

    tls = spec.tls
    verify: Any = True
    if not tls.verify:
        verify = False
    elif tls.ca_bundle or tls.client_cert:
        ctx = ssl.create_default_context(cafile=_path(tls.ca_bundle) if tls.ca_bundle else None)
        if tls.client_cert:
            ctx.load_cert_chain(_path(tls.client_cert), _path(tls.client_key) if tls.client_key else None)
        verify = ctx
    return httpx.AsyncClient(base_url=spec.base_url.rstrip("/"), headers=headers, params=params or None, auth=auth,
                             timeout=spec.timeout_seconds, follow_redirects=False, verify=verify,
                             transport=transport)
