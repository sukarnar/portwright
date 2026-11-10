"""LLM providers and the factory that builds one from a model spec."""
from __future__ import annotations

from pathlib import Path

import httpx

from ...config import ConfigError, SecuritySettings, resolve_secrets
from ...credentials.types import build_http_auth
from ..spec import LlmModelSpec
from .anthropic import AnthropicProvider
from .base import LlmError, LlmProvider, LlmResponse, Message, ToolCall, build_client, check_llm_host
from .openai_compat import OpenAICompatible

PROVIDERS: dict[str, type[LlmProvider]] = {"openai_compatible": OpenAICompatible, "anthropic": AnthropicProvider}


def create_provider(name: str, spec: LlmModelSpec, security: SecuritySettings, root: str | Path,
                    credentials=None, transport: httpx.AsyncBaseTransport | None = None) -> LlmProvider:
    """Build a provider: host allow-list, API key from the credential vault, CA bundle / client cert."""
    check_llm_host(spec.base_url, security)
    headers = {"Content-Type": "application/json", **resolve_secrets(dict(spec.headers), credentials)}
    params: dict = {}
    auth = None
    if spec.credential:
        if credentials is None:
            raise ConfigError(f"model '{name}' needs credential '{spec.credential}' but no credential manager is set")
        cred = credentials.get(spec.credential, consumer=f"model:{name}")
        if cred.type == "api_key" and not (cred.fields.get("header") or cred.fields.get("query_param")):
            # a plain API key: OpenAI-style gateways want "Authorization: Bearer", Anthropic wants "x-api-key"
            if spec.provider == "anthropic":
                auth, extra_headers, params = None, {"x-api-key": cred.fields["key"]}, {}
            else:
                auth, extra_headers, params = None, {"Authorization": f"Bearer {cred.fields['key']}"}, {}
        else:
            auth, extra_headers, params = build_http_auth(cred, security.allowed_upstream_hosts)
        headers.update(extra_headers)
    if spec.provider == "anthropic":
        headers.setdefault("anthropic-version", "2023-06-01")
    return PROVIDERS[spec.provider](name, spec, build_client(spec, root, headers, params, auth, transport))


__all__ = ["PROVIDERS", "LlmError", "LlmProvider", "LlmResponse", "Message", "ToolCall", "create_provider"]
