"""The agent loop: model call -> tool calls -> results -> repeat, bounded by steps, tokens and time."""
from __future__ import annotations

import asyncio
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..config import ConfigError, ProjectConfig
from .llm import LlmError, LlmProvider, create_provider
from .models import load_models
from .runs import new_run_id, save_run
from .skills import Skill, load_skills, skill_index
from .tools import Tool, ToolError, ToolRegistry, ToolRuntime, build_registry, render

ProviderFactory = Callable[[str, Any], LlmProvider]


@dataclass
class ToolRecord:
    name: str
    arguments: dict
    ok: bool
    result: str


@dataclass
class StepRecord:
    n: int
    text: str
    tool_calls: list[ToolRecord] = field(default_factory=list)
    tokens: int = 0


@dataclass
class AgentResult:
    agent: str
    answer: str
    stopped_by: str                    # final | max_steps | budget | timeout | error
    steps: list[StepRecord]
    tokens: int
    run_id: str
    error: str = ""
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return self.stopped_by == "final"

    def to_dict(self) -> dict:
        return asdict(self)


def _skill_tool(skills: dict[str, Skill]) -> Tool:
    async def run(args: dict) -> Any:
        name = str(args.get("name") or "")
        if name not in skills:
            raise ToolError(f"unknown skill '{name}'. Available: {', '.join(skills)}")
        return skills[name].body

    return Tool("use_skill", "Read the full instructions of a skill by name.",
                {"type": "object", "required": ["name"], "additionalProperties": False,
                 "properties": {"name": {"type": "string", "enum": list(skills)}}}, run, "skill")


async def run_agent(config: ProjectConfig, root: str | Path, credentials, name: str, task: str, *,
                    provider_factory: ProviderFactory | None = None,
                    transport_factory=None, save: bool = True,
                    on_step: Callable[[StepRecord], None] | None = None) -> AgentResult:
    """Run one agent to completion. Never raises for model/tool problems: they end up in ``AgentResult``."""
    spec = config.agents.get(name)
    if spec is None:
        raise ConfigError(f"no agent '{name}'")
    root = Path(root)
    models = load_models(config, root)
    if spec.model not in models:
        raise ConfigError(f"agent '{name}' uses unknown model '{spec.model}'")
    skills = load_skills(spec.skills, config.skills, root)
    factory = provider_factory or (lambda n, s: create_provider(n, s, config.security, root, credentials))
    provider = factory(spec.model, models[spec.model])

    run_id, t0 = new_run_id(), time.monotonic()
    steps: list[StepRecord] = []
    tokens, stopped, answer, error = 0, "max_steps", "", ""
    system = (spec.instructions or "You are a helpful agent.") + skill_index(skills)
    messages: list[dict] = [{"role": "user", "content": task}]

    try:
        async with ToolRuntime(config, root, credentials, spec.scopes, f"agent:{name}",
                               transport_factory) as rt:
            registry = build_registry(rt, spec.tools, config.tools)
            if skills:
                registry.add(_skill_tool(skills))
            try:
                async with asyncio.timeout(spec.timeout_seconds):
                    for n in range(1, spec.max_steps + 1):
                        resp = await provider.chat(system, messages, registry.schemas())
                        tokens += resp.total_tokens
                        step = StepRecord(n, resp.text, tokens=resp.total_tokens)
                        steps.append(step)
                        if not resp.tool_calls:
                            stopped, answer = "final", resp.text
                            break
                        messages.append({"role": "assistant", "content": resp.text, "tool_calls": resp.tool_calls})
                        for c in resp.tool_calls:
                            rec = await _call_tool(registry, rt, name, c.name, c.arguments)
                            step.tool_calls.append(rec)
                            messages.append({"role": "tool", "tool_call_id": c.id, "name": c.name,
                                             "content": ("" if rec.ok else "ERROR: ") + rec.result})
                        if on_step:
                            on_step(step)
                        if spec.max_tokens_budget and tokens >= spec.max_tokens_budget:
                            stopped, answer = "budget", resp.text
                            break
                        answer = resp.text
                    else:
                        stopped = "max_steps"
            except TimeoutError:
                stopped = "timeout"
            except LlmError as exc:
                stopped, error = "error", str(exc)
            rt.audit.record(event="agent_run", agent=name, run_id=run_id, steps=len(steps), tokens=tokens,
                            stopped_by=stopped)
    except (ConfigError, LlmError) as exc:
        stopped, error = "error", str(exc)
    finally:
        await provider.aclose()

    result = AgentResult(name, answer, stopped, steps, tokens, run_id, error, round(time.monotonic() - t0, 2))
    if save:
        save_run(root, "agent", name, run_id, {"kind": "agent", "name": name, "task": task, **result.to_dict()})
    return result


async def _call_tool(registry: ToolRegistry, rt: ToolRuntime, agent: str, tool: str, args: dict) -> ToolRecord:
    try:
        if "_invalid_json" in args:
            raise ToolError("arguments were not valid JSON")
        out = render(await registry.call(tool, args))
        ok = True
    except ToolError as exc:
        out, ok = str(exc), False
    except Exception as exc:  # noqa: BLE001 - never let one tool crash the run; details stay server-side
        out, ok = f"tool failed ({type(exc).__name__})", False
    rt.audit.record(event="agent_tool", agent=agent, tool=tool, ok=ok)
    return ToolRecord(tool, args, ok, out)
