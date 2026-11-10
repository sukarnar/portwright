"""Anthropic Messages API over plain httpx (so corporate CA / mTLS settings apply)."""
from __future__ import annotations

from .base import LlmProvider, LlmResponse, Message, ToolCall


def to_anthropic_messages(messages: list[Message]) -> list[dict]:
    out: list[dict] = []
    for m in messages:
        if m["role"] == "assistant":
            blocks: list[dict] = []
            if m.get("content"):
                blocks.append({"type": "text", "text": m["content"]})
            for c in m.get("tool_calls") or []:
                blocks.append({"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments})
            out.append({"role": "assistant", "content": blocks})
        elif m["role"] == "tool":
            block = {"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": m["content"]}
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                out[-1]["content"].append(block)       # all results of one turn go in one user message
            else:
                out.append({"role": "user", "content": [block]})
        else:
            out.append({"role": "user", "content": m["content"]})
    return out


class AnthropicProvider(LlmProvider):
    async def chat(self, system: str, messages: list[Message], tools: list[dict]) -> LlmResponse:
        body: dict = {"model": self.spec.model, "max_tokens": self.spec.max_tokens,
                      "messages": to_anthropic_messages(messages)}
        if system:
            body["system"] = system
        if self.spec.temperature is not None:
            body["temperature"] = self.spec.temperature
        if tools:
            body["tools"] = [{"name": t["name"], "description": t["description"],
                              "input_schema": t["parameters"]} for t in tools]
        try:
            r = await self.client.post("/v1/messages", json=body)
            r.raise_for_status()
            data = r.json()
        except Exception as exc:  # noqa: BLE001
            raise self._fail(exc) from None
        text, calls = [], []
        for b in data.get("content") or []:
            if b.get("type") == "text":
                text.append(b.get("text", ""))
            elif b.get("type") == "tool_use":
                calls.append(ToolCall(b["id"], b["name"], b.get("input") or {}))
        usage = data.get("usage") or {}
        return LlmResponse("".join(text), calls, usage.get("input_tokens", 0), usage.get("output_tokens", 0),
                           data.get("stop_reason") or "")
