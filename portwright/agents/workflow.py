"""Workflows: steps (input | tool | agent | llm | load | foreach | condition | switch | set | output) with
``${...}`` templating between them."""
from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..config import ConfigError, ProjectConfig
from .llm import LlmError, create_provider
from .loop import ProviderFactory, run_agent
from .models import load_models
from .runs import new_run_id, save_run
from .tools import ToolError, ToolRuntime, build_registry, render

_REF = re.compile(r"\$\{([A-Za-z_][\w\-]*(?:\.[\w\-]+|\[\d+\])*)\}")
_FALSE = {"", "0", "false", "no", "none", "null", "[]", "{}"}
_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL | re.IGNORECASE)


@dataclass
class StepResult:
    id: str
    type: str
    ok: bool
    output: Any = None
    error: str = ""
    seconds: float = 0.0
    run_id: str = ""          # agent steps: the agent run's trace
    tokens: int = 0           # llm / agent steps: model tokens used


@dataclass
class WorkflowResult:
    workflow: str
    status: str               # ok | failed
    output: Any
    steps: list[StepResult] = field(default_factory=list)
    run_id: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def to_dict(self) -> dict:
        return asdict(self)


def _lookup(path: str, scope: dict) -> Any:
    cur: Any = scope
    for part in re.findall(r"[^.\[\]]+", path):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            raise ConfigError(f"workflow reference '${{{path}}}' not found")
    return cur


def resolve(value: Any, scope: dict) -> Any:
    """Replace ${a.b[0]} references. A string that is exactly one reference keeps the referenced type."""
    if isinstance(value, str):
        m = _REF.fullmatch(value.strip())
        if m:
            return _lookup(m.group(1), scope)
        return _REF.sub(lambda m: (lambda v: v if isinstance(v, str) else json.dumps(v, default=str))(
            _lookup(m.group(1), scope)), value)
    if isinstance(value, dict):
        return {k: resolve(v, scope) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve(v, scope) for v in value]
    return value


def truthy(value: Any) -> bool:
    if isinstance(value, str):
        for op, neg in ((" != ", True), (" == ", False)):
            if op in value:
                a, b = (x.strip().strip("'\"") for x in value.split(op, 1))
                return (a != b) if neg else (a == b)
        return value.strip().lower() not in _FALSE
    return bool(value)


_INPUT_KEYS = {"default", "required", "description"}


def declared_inputs(spec: dict | None) -> dict[str, dict]:
    """Normalise an input node's ``inputs`` into {name: {default, required, description}}.

    ``name: value`` is an optional input with that default; ``name: null`` (or nothing) is required;
    ``name: {default, required, description}`` spells it out.
    """
    out: dict[str, dict] = {}
    for name, d in (spec or {}).items():
        if isinstance(d, dict) and d and set(d) <= _INPUT_KEYS:
            has_default = "default" in d
            out[name] = {"default": d.get("default"), "required": bool(d.get("required", not has_default)),
                         "description": d.get("description", "")}
        else:
            out[name] = {"default": d, "required": d is None, "description": ""}
    return out


def apply_inputs(spec: dict | None, given: Any) -> dict:
    """The workflow input with defaults filled in; a missing required input is an error."""
    decl = declared_inputs(spec)
    if given is None or given == {}:
        given = {}
    if not isinstance(given, dict):
        if decl:
            raise ToolError("this workflow expects its input as KEY=VALUE pairs")
        return given
    merged = dict(given)
    missing = []
    for name, d in decl.items():
        if name in merged and merged[name] not in (None, ""):
            continue
        if d["required"]:
            missing.append(name)
        elif name not in merged or merged[name] in (None, ""):
            merged[name] = d["default"]
    if missing:
        raise ToolError("missing required input: " + ", ".join(missing))
    return merged


def _structured(text: str) -> Any:
    try:
        v = json.loads(text)
        return v if isinstance(v, (dict, list)) else text
    except ValueError:
        return text


def _norm(value: Any) -> str:
    return str(value).strip().lower()


def switch_target(st, value: Any) -> str | None:
    """Step id a switch jumps to: the case equal to the value (case-insensitive), else its default."""
    cases = {_norm(k): v for k, v in (st.cases or {}).items()}
    return cases.get(_norm(value)) or st.default


def _items(value: Any) -> list:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            pass
    if isinstance(value, dict) and isinstance(value.get("rows"), list):      # a SQL tool result
        value = value["rows"]
    if not isinstance(value, list):
        raise ToolError("'items' must be a list (for example ${steps.query.output.rows})")
    return value


@dataclass
class _Env:
    """What the steps of one run share."""
    config: ProjectConfig
    root: Path
    credentials: Any
    registry: Any = None
    rt: Any = None
    provider_factory: ProviderFactory | None = None
    transport_factory: Any = None
    save: bool = True


async def _llm(st, prompt: Any, scope: dict, env: _Env) -> tuple[Any, int]:
    models = load_models(env.config, env.root)
    if st.model not in models:
        raise ConfigError(f"step '{st.id}' uses unknown model '{st.model}'")
    factory = env.provider_factory or (lambda n, s: create_provider(n, s, env.config.security, env.root, env.credentials))
    provider = factory(st.model, models[st.model])
    system = str(resolve(st.system, scope)) if st.system else ""
    if st.format == "json":
        system = (system + "\n\n" if system else "") + "Reply with one valid JSON value and nothing else."
    text = prompt if isinstance(prompt, str) else json.dumps(prompt, default=str)
    try:
        resp = await provider.chat(system, [{"role": "user", "content": text}], [])
    except LlmError as exc:
        raise ToolError(str(exc)) from None
    finally:
        await provider.aclose()
    out: Any = resp.text
    if st.format == "json":
        body = _FENCE.sub(r"\1", resp.text.strip())
        try:
            out = json.loads(body)
        except ValueError:
            raise ToolError("the model did not return valid JSON") from None
    return out, resp.total_tokens


async def _do(kind: str, st, raw_input: Any, scope: dict, env: _Env) -> tuple[Any, str, int]:
    """Run one tool / agent / llm / load action. Returns (output, agent run id, tokens)."""
    if kind == "tool":
        args = resolve(raw_input, scope) if raw_input is not None else {}
        if not isinstance(args, dict):
            raise ToolError("tool step 'input' must be a mapping of arguments")
        return _structured(render(await env.registry.call(st.tool, args))), "", 0
    if kind == "load":
        return _structured(render(await env.rt.load_tool(st.load, st.load).run({}))), "", 0
    if kind == "agent":
        task = resolve(raw_input, scope) if raw_input is not None else ""
        ar = await run_agent(env.config, env.root, env.credentials, st.agent,
                             task if isinstance(task, str) else json.dumps(task, default=str),
                             provider_factory=env.provider_factory, transport_factory=env.transport_factory,
                             save=env.save)
        if not ar.ok:
            raise ToolError(f"agent '{st.agent}' stopped by {ar.stopped_by}" + (f": {ar.error}" if ar.error else ""))
        return _structured(ar.answer), ar.run_id, ar.tokens
    prompt = resolve(raw_input, scope) if raw_input is not None else ""          # llm
    out, tokens = await _llm(st, prompt, scope, env)
    return out, "", tokens


def _needs_runtime(steps) -> tuple[list[str], bool]:
    tools, load = set(), False
    for s in steps:
        kind = s.do if s.type == "foreach" else s.type
        if kind == "tool" and s.tool:
            tools.add(s.tool)
        if kind == "load":
            load = True
    return sorted(tools), load


async def run_workflow(config: ProjectConfig, root: str | Path, credentials, name: str, input: Any = None, *,
                       provider_factory: ProviderFactory | None = None, transport_factory=None,
                       save: bool = True) -> WorkflowResult:
    wf = config.workflows.get(name)
    if wf is None:
        raise ConfigError(f"no workflow '{name}'")
    root = Path(root)
    run_id = new_run_id()
    scope: dict[str, Any] = {"input": input if input is not None else {}, "steps": {}}
    results: list[StepResult] = []
    status, error, last = "ok", "", None
    final, has_final = None, False
    order = [s.id for s in wf.steps]
    env = _Env(config, root, credentials, provider_factory=provider_factory, transport_factory=transport_factory,
               save=save)

    tool_names, needs_load = _needs_runtime(wf.steps)
    rt = None
    try:
        if tool_names or needs_load:
            rt = await ToolRuntime(config, root, credentials, wf.scopes, f"workflow:{name}", transport_factory).__aenter__()
            env.rt, env.registry = rt, build_registry(rt, tool_names, config.tools)
        i, executed = 0, 0
        while i < len(wf.steps):
            executed += 1
            if executed > wf.max_steps:
                status, error = "failed", f"stopped after {wf.max_steps} executed steps (loop?)"
                break
            st = wf.steps[i]
            t0, jump = time.monotonic(), None
            res = StepResult(st.id, st.type, True)
            try:
                if st.type in ("tool", "agent", "llm", "load"):
                    res.output, res.run_id, res.tokens = await _do(st.type, st, st.input, scope, env)
                elif st.type == "foreach":
                    res.output, res.tokens = await _foreach(st, scope, env)
                elif st.type == "input":
                    scope["input"] = apply_inputs(st.inputs, scope["input"])
                    res.output = ({k: scope["input"].get(k) for k in (st.inputs or {})}
                                  if isinstance(scope["input"], dict) else scope["input"])
                elif st.type == "output":
                    res.output = resolve(st.value, scope) if st.value is not None else last
                    final, has_final, jump = res.output, True, "end"
                elif st.type == "set":
                    res.output = resolve(st.value, scope)
                elif st.type == "switch":
                    res.output = resolve(st.when, scope)
                    jump = switch_target(st, res.output)
                else:                                             # condition
                    res.output = truthy(resolve(st.when, scope))
                    jump = st.then if res.output else st.else_
            except (ToolError, ConfigError) as exc:
                res.ok, res.error = False, str(exc)
                res.output = {"error": str(exc)}
            if st.type == "output":
                jump = "end"
            elif st.type not in ("condition", "switch"):
                jump = st.next
            res.seconds = round(time.monotonic() - t0, 2)
            results.append(res)
            scope["steps"][st.id] = {"output": res.output, "ok": res.ok}
            if res.ok or st.type not in ("condition", "switch"):
                last = res.output
            if not res.ok and st.on_error == "fail":
                status, error = "failed", f"step '{st.id}' failed: {res.error}"
                break
            if jump and jump != "end" and jump not in order:
                status, error = "failed", f"step '{st.id}': unknown target step '{jump}'"
                break
            i = len(wf.steps) if jump == "end" else (order.index(jump) if jump else i + 1)
        if status == "ok" and has_final:
            output = final
        else:
            output = resolve(wf.output, scope) if (wf.output is not None and status == "ok") else last
    except (ConfigError, ToolError) as exc:
        status, error, output = "failed", str(exc), None
    finally:
        if rt is not None:
            await rt.__aexit__(None, None, None)

    result = WorkflowResult(name, status, output, results, run_id, error)
    if save:
        save_run(root, "workflow", name, run_id, {"kind": "workflow", "name": name, "input": input,
                                                  "stopped_by": status, **result.to_dict()})
    return result


async def _foreach(st, scope: dict, env: _Env) -> tuple[list, int]:
    """Run the step's action once per item; ``${item}`` and ``${index}`` are available to its input."""
    items = _items(resolve(st.items, scope))
    if len(items) > st.max_items:
        raise ToolError(f"{len(items)} items is more than max_items ({st.max_items}); raise it or filter the list")
    outs: list = []
    tokens = 0
    for idx, item in enumerate(items):
        try:
            out, _, tok = await _do(st.do, st, st.input, {**scope, "item": item, "index": idx}, env)
            tokens += tok
            outs.append(out)
        except (ToolError, ConfigError) as exc:
            if st.on_error != "continue":
                raise ToolError(f"item {idx}: {exc}") from None
            outs.append({"error": str(exc)})
    return outs, tokens
