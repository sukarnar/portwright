"""OpenAI-compatible chat/completions (OpenAI, Azure gateways, vLLM, Ollama, corporate proxies)."""
from __future__ import annotations

import json

from .base import LlmProvider, LlmResponse, Message, ToolCall


def to_openai_messages(system: str, messages: list[Message]) -> list[dict]:
    out: list[dict] = [{"role": "system", "content": system}] if system else []
    for m in messages:
        if m["role"] == "assistant":
            d: dict = {"role": "assistant", "content": m.get("content") or None}
            if m.get("tool_calls"):
                d["tool_calls"] = [{"id": c.id, "type": "function",
                                    "function": {"name": c.name, "arguments": json.dumps(c.arguments)}}
                                   for c in m["tool_calls"]]
            out.append(d)
        elif m["role"] == "tool":
            out.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"]})
        else:
            out.append({"role": "user", "content": m["content"]})
    return out


class OpenAICompatible(LlmProvider):
    async def chat(self, system: str, messages: list[Message], tools: list[dict]) -> LlmResponse:
        body: dict = {"model": self.spec.model, "messages": to_openai_messages(system, messages),
                      "max_tokens": self.spec.max_tokens}
        if self.spec.temperature is not None:
            body["temperature"] = self.spec.temperature
        if tools:
            body["tools"] = [{"type": "function", "function": {
                "name": t["name"], "description": t["description"], "parameters": t["parameters"]}} for t in tools]
        try:
            r = await self.client.post("/chat/completions", json=body)
            for _ in range(2):
                if r.status_code != 400:
                    break
                # newer OpenAI models (gpt-5, o-series) renamed max_tokens and fix the temperature
                changed = False
                if "max_completion_tokens" in r.text and "max_tokens" in body:
                    body["max_completion_tokens"] = body.pop("max_tokens")
                    changed = True
                elif "temperature" in r.text and "temperature" in body:
                    body.pop("temperature")
                    changed = True
                if not changed:
                    break
                r = await self.client.post("/chat/completions", json=body)
            r.raise_for_status()
            data = r.json()
        except Exception as exc:  # noqa: BLE001
            raise self._fail(exc) from None
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        calls = []
        for c in msg.get("tool_calls") or []:
            raw = (c.get("function") or {}).get("arguments") or "{}"
            try:
                args = json.loads(raw)
            except ValueError:
                args = {"_invalid_json": raw[:200]}
            calls.append(ToolCall(c.get("id") or f"call_{len(calls)}", c["function"]["name"],
                                  args if isinstance(args, dict) else {"_value": args}))
        usage = data.get("usage") or {}
        return LlmResponse(msg.get("content") or "", calls, usage.get("prompt_tokens", 0),
                           usage.get("completion_tokens", 0), choice.get("finish_reason") or "")
