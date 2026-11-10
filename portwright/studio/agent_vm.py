"""View-model helpers for the Agents screen: form values <-> definitions, no UI code (unit-tested)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from ..services import ServiceError, Services

KINDS = [("agent", "Agents"), ("model", "Models"), ("tool", "Tools"), ("skill", "Skills"),
         ("workflow", "Workflows"), ("mcp", "MCP servers"), ("history", "Run history")]
PROVIDERS = {"openai_compatible": "OpenAI-compatible (OpenAI, Azure, vLLM, Ollama, corporate gateway)",
             "anthropic": "Anthropic"}
TOOL_TYPES = {"endpoint": "An endpoint (called through the full API pipeline)",
              "source": "A SQL source (the model writes SELECT statements)",
              "mcp": "An MCP server defined here (all of its tools)"}
WORKFLOW_TEMPLATE = """steps:
  - id: first
    type: tool            # tool | agent | condition | set
    tool: my_tool
    input: {}
  - id: review
    type: agent
    agent: my_agent
    input: "Review: ${steps.first.output}"
"""


def lines(text: str | None) -> list[str]:
    return [x.strip() for x in (text or "").replace(",", "\n").splitlines() if x.strip()]


def _num(text: Any, label: str, kind=int, empty_ok: bool = False):
    t = str(text if text is not None else "").strip()
    if t == "" and empty_ok:
        return None
    try:
        return kind(t)
    except ValueError:
        raise ServiceError(f"{label} must be a number") from None


def _clean(d: dict) -> dict:
    return {k: v for k, v in d.items() if v not in (None, "", [], {})}


# ------------------------------------------------------------------ values
def item_values(svc: Services, kind: str, name: str) -> dict:
    """Form values (strings) of an existing definition."""
    spec = svc.agents.get(kind, name)
    d = spec.model_dump(mode="json", exclude_none=True)
    v: dict[str, Any] = {"name": name}
    if kind == "model":
        v.update(provider=d["provider"], base_url=d["base_url"], model=d["model"], credential=d.get("credential", ""),
                 ca_bundle=d["tls"].get("ca_bundle", ""), client_cert=d["tls"].get("client_cert", ""),
                 client_key=d["tls"].get("client_key", ""), tls_verify=d["tls"].get("verify", True),
                 headers="\n".join(f"{k}={x}" for k, x in d.get("headers", {}).items()),
                 timeout=str(d["timeout_seconds"]), max_tokens=str(d["max_tokens"]),
                 temperature="" if d.get("temperature") is None else str(d["temperature"]))
    elif kind == "tool":
        v.update(type=d["type"], endpoint=d.get("endpoint", ""), source=d.get("source", ""), server=d.get("server", ""),
                 write=not d.get("read_only", True), max_rows=str(d.get("max_rows", 200)),
                 description=d.get("description", ""))
    elif kind == "skill":
        v.update(file=d["file"], description=d.get("description", ""), text=svc.agents.skill_text(name))
    elif kind == "agent":
        v.update(model=d["model"], instructions=d.get("instructions", ""), tools=d.get("tools", []),
                 skills=d.get("skills", []), scopes=", ".join(d.get("scopes", [])), max_steps=str(d["max_steps"]),
                 budget=str(d.get("max_tokens_budget", "")), timeout=str(d["timeout_seconds"]),
                 description=d.get("description", ""))
    elif kind == "workflow":
        v.update(description=d.get("description", ""), scopes=", ".join(d.get("scopes", [])),
                 steps=yaml.safe_dump({"steps": d["steps"]}, sort_keys=False),
                 output=yaml.safe_dump(d["output"]).strip() if d.get("output") is not None else "")
    elif kind == "mcp":
        exp = d.get("expose", {})
        v.update(description=d.get("description", ""), transport=d["transport"], scopes=", ".join(d.get("scopes", [])),
                 endpoints=exp.get("endpoints", []), loads=exp.get("loads", []),
                 sources="\n".join(f"{s['name']}: {', '.join(s.get('tables', []))}" for s in exp.get("sources", [])))
    return v


# ---------------------------------------------------------------- payloads
def item_payload(kind: str, v: dict) -> dict:
    """Definition dict for AgentService.set (validated there against the real models)."""
    if kind == "model":
        headers = {}
        for ln in (v.get("headers") or "").splitlines():
            if ln.strip():
                if "=" not in ln:
                    raise ServiceError(f"header '{ln.strip()}': use Name=value")
                k, x = ln.split("=", 1)
                headers[k.strip()] = x.strip()
        tls = _clean({"ca_bundle": v.get("ca_bundle", "").strip(), "client_cert": v.get("client_cert", "").strip(),
                      "client_key": v.get("client_key", "").strip()})
        if not v.get("tls_verify", True):
            tls["verify"] = False
        return _clean({"provider": v["provider"], "base_url": (v.get("base_url") or "").strip(),
                       "model": (v.get("model") or "").strip(), "credential": v.get("credential") or None,
                       "headers": headers, "tls": tls,
                       "timeout_seconds": _num(v.get("timeout") or 120, "Timeout", float),
                       "max_tokens": _num(v.get("max_tokens") or 4096, "Max tokens"),
                       "temperature": _num(v.get("temperature"), "Temperature", float, True)})
    if kind == "tool":
        t = v["type"]
        return _clean({"type": t, "endpoint": v.get("endpoint") if t == "endpoint" else None,
                       "source": v.get("source") if t == "source" else None,
                       "server": v.get("server") if t == "mcp" else None,
                       "read_only": not v.get("write", False), "max_rows": _num(v.get("max_rows") or 200, "Max rows"),
                       "description": v.get("description", "")})
    if kind == "skill":
        return _clean({"file": (v.get("file") or "").strip(), "description": v.get("description", "")})
    if kind == "agent":
        return _clean({"model": v.get("model"), "instructions": v.get("instructions", ""),
                       "tools": list(v.get("tools", [])), "skills": list(v.get("skills", [])),
                       "scopes": lines(v.get("scopes")), "max_steps": _num(v.get("max_steps") or 8, "Max steps"),
                       "max_tokens_budget": _num(v.get("budget"), "Token budget", int, True),
                       "timeout_seconds": _num(v.get("timeout") or 300, "Timeout", float),
                       "description": v.get("description", "")})
    if kind == "workflow":
        try:
            body = yaml.safe_load(v.get("steps") or "") or {}
            out = yaml.safe_load(v["output"]) if (v.get("output") or "").strip() else None
        except yaml.YAMLError as exc:
            raise ServiceError(f"workflow YAML: {str(exc).splitlines()[0]}") from None
        if not isinstance(body, dict) or "steps" not in body:
            raise ServiceError("the workflow text must start with 'steps:' followed by a list of steps")
        return _clean({"description": v.get("description", ""), "scopes": lines(v.get("scopes")),
                       "steps": body["steps"], "output": out})
    if kind == "mcp":
        sources = []
        for ln in (v.get("sources") or "").splitlines():
            if ln.strip():
                name, _, tables = ln.partition(":")
                sources.append({"name": name.strip(), "tables": lines(tables), "read_only": True})
        return _clean({"description": v.get("description", ""), "transport": v.get("transport", "stdio"),
                       "scopes": lines(v.get("scopes")),
                       "expose": _clean({"endpoints": list(v.get("endpoints", [])), "loads": list(v.get("loads", [])),
                                         "sources": sources})})
    raise ServiceError(f"unknown kind '{kind}'")


def save_item(svc: Services, kind: str, values: dict, editing: bool) -> list[str]:
    """Validate and save one definition. Returns warnings (e.g. a skill file that does not exist yet)."""
    name = (values.get("name") or "").strip()
    if not name:
        raise ServiceError("Name is required")
    warnings: list[str] = []
    if kind == "skill" and values.get("text") is not None and (values.get("text") or "").strip():
        svc.agents.save_skill_file(values["file"], values["text"])
    svc.agents.set(kind, name, item_payload(kind, values), replace=editing)
    if kind == "model":
        host = urlsplit(values["base_url"]).hostname
        sec = svc.project.config.security
        if host and host not in sec.allowed_llm_hosts:
            sec.allowed_llm_hosts.append(host)
            svc.project.save()
            warnings.append(f"{host} added to the allowed LLM hosts")
    problems = [p for p in svc.agents.validate() if name in p]
    return warnings + problems


# ------------------------------------------------------------------ choices
def choices(svc: Services) -> dict[str, list[str]]:
    cfg = svc.project.config
    creds = []
    try:
        creds = [c["name"] for c in svc.credentials.list() if c["type"] in ("bearer", "api_key")]
    except ServiceError:
        pass
    return {"models": [m["name"] for m in svc.agents.list("model")], "tools": list(cfg.tools),
            "skills": list(cfg.skills), "agents": list(cfg.agents), "endpoints": [e.name for e in cfg.endpoints],
            "sql_sources": [n for n, s in cfg.sources.items() if s.type == "sql"],
            "mcp_servers": list(cfg.mcp_servers), "loads": [l.name for l in cfg.loads], "credentials": creds,
            "workflows": list(cfg.workflows)}


def summary_rows(svc: Services, kind: str) -> list[dict]:
    """One dict per definition with display strings: name, a, b, c (column meaning depends on kind)."""
    out = []
    for r in svc.agents.list(kind):
        n = r["name"]
        if kind == "agent":
            row = (r["model"], f"{len(r.get('tools', []))} tools, {len(r.get('skills', []))} skills",
                   f"up to {r['max_steps']} steps")
        elif kind == "model":
            row = (PROVIDERS.get(r["provider"], r["provider"]).split(" (")[0], r["base_url"], r["model"])
        elif kind == "tool":
            row = (r["type"], r.get("endpoint") or r.get("source") or r.get("server") or "",
                   "read-only" if r.get("read_only", True) else "can write")
        elif kind == "skill":
            row = (r["file"], r.get("description", ""), "")
        elif kind == "workflow":
            row = (f"{len(r['steps'])} steps", ", ".join(s["id"] for s in r["steps"]), ", ".join(r.get("scopes", [])))
        else:
            e = r.get("expose", {})
            row = (r["transport"], f"{len(e.get('endpoints', []))} endpoints, {len(e.get('sources', []))} sources, "
                                   f"{len(e.get('loads', []))} loads", ", ".join(r.get("scopes", [])))
        out.append({"name": n, "a": row[0], "b": row[1], "c": row[2], "file": bool(r.get("file")) and kind == "model"})
    return out


# ------------------------------------------------------------------ results
def agent_steps_rows(result) -> list[list[str]]:
    rows = []
    for s in result.steps:
        if not s.tool_calls:
            rows.append([str(s.n), "answer", s.text[:200], ""])
        for c in s.tool_calls:
            rows.append([str(s.n), c.name, json.dumps(c.arguments, default=str)[:160],
                         ("ok: " if c.ok else "ERROR: ") + c.result[:160]])
    return rows


def trace_rows(trace: dict) -> list[list[str]]:
    """Rows for a saved run (agent or workflow trace JSON)."""
    rows = []
    if trace.get("kind") == "workflow":
        for s in trace.get("steps", []):
            rows.append([s["id"], s["type"], "ok" if s["ok"] else "FAILED", (s.get("error") or str(s.get("output")))[:200]])
        return rows
    for s in trace.get("steps", []):
        if not s.get("tool_calls"):
            rows.append([str(s["n"]), "answer", "", str(s.get("text", ""))[:200]])
        for c in s.get("tool_calls", []):
            rows.append([str(s["n"]), c["name"], json.dumps(c.get("arguments"), default=str)[:120],
                         ("ok: " if c["ok"] else "ERROR: ") + str(c["result"])[:160]])
    return rows


def claude_desktop_config(config_path: str | Path, server: str) -> str:
    """The snippet for claude_desktop_config.json that launches an MCP server over stdio."""
    return json.dumps({"mcpServers": {server: {"command": "portwright", "args": [
        "mcp", "serve", server, "--config", str(config_path)]}}}, indent=2)
