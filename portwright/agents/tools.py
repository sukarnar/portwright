"""Tools: what agents, workflows and MCP servers can call.

One ``ToolRegistry`` is the single source of truth. Tools run inside a ``ToolRuntime``, which builds the real
application (``create_app``) with a short-lived API key limited to the caller's scopes, so endpoint tools go
through the normal pipeline: authentication, scopes, rate limit, validation and audit.
"""
from __future__ import annotations

import asyncio
import json
import re
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import quote

import httpx

from ..config import ApiKeyRecord, EndpointSpec, ProjectConfig
from ..security.audit import AuditLogger
from ..security.hashing import generate_api_key, hash_api_key
from .spec import McpServerSpec, ToolSpec

MAX_RESULT_CHARS = 20_000
_READ_SQL = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)
_FROM_TABLE = re.compile(r"\b(?:from|join)\s+([A-Za-z_][\w$.\"]*)", re.IGNORECASE)
_COMMA_JOIN = re.compile(r"\bfrom\s+[\w$.\"]+(?:\s+(?:as\s+)?\w+)?\s*,", re.IGNORECASE)
_OAS = {"string": "string", "integer": "integer", "number": "number", "boolean": "boolean"}


class ToolError(Exception):
    """A tool call failed; the message is returned to the model so it can correct itself."""


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    run: Callable[[dict], Awaitable[Any]]
    kind: str = "endpoint"

    def schema(self) -> dict:
        return {"name": self.name, "description": self.description, "parameters": self.parameters}


class ToolRegistry:
    def __init__(self, tools: list[Tool] | None = None):
        self.tools: dict[str, Tool] = {}
        for t in tools or []:
            self.add(t)

    def add(self, tool: Tool) -> None:
        self.tools[tool.name] = tool

    def schemas(self) -> list[dict]:
        return [t.schema() for t in self.tools.values()]

    def __contains__(self, name: str) -> bool:
        return name in self.tools

    async def call(self, name: str, args: dict) -> Any:
        tool = self.tools.get(name)
        if tool is None:
            raise ToolError(f"unknown tool '{name}'. Available: {', '.join(sorted(self.tools)) or 'none'}")
        return await tool.run(args or {})


def render(value: Any) -> str:
    """Tool result as text for the model, capped so one call cannot flood the context."""
    text = value if isinstance(value, str) else json.dumps(value, default=str, ensure_ascii=False)
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + f"... [truncated, {len(text) - MAX_RESULT_CHARS} more characters]"
    return text


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #
def endpoint_schema(ep: EndpointSpec, paginated: bool) -> dict:
    props: dict[str, Any] = {}
    required = []
    for p in ep.params:
        s: dict[str, Any] = {"type": _OAS[p.type]}
        if p.description:
            s["description"] = p.description
        if p.enum:
            s["enum"] = p.enum
        if p.type == "string" and p.max_length:
            s["maxLength"] = p.max_length
        if p.min is not None:
            s["minimum"] = p.min
        if p.max is not None:
            s["maximum"] = p.max
        props[p.name] = s
        if p.required:
            required.append(p.name)
    if paginated:
        props["limit"] = {"type": "integer", "minimum": 1, "maximum": ep.max_limit}
        props["offset"] = {"type": "integer", "minimum": 0}
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


# --------------------------------------------------------------------------- #
# Runtime
# --------------------------------------------------------------------------- #
class ToolRuntime:
    """Builds the app once, runs its lifespan (connection pools) and calls it in-process.

    Use as ``async with ToolRuntime(config, root, credentials, scopes, actor) as rt``.
    """

    def __init__(self, config: ProjectConfig, root: str | Path, credentials, scopes: list[str], actor: str,
                 transport_factory: Callable[[Any], httpx.AsyncBaseTransport] | None = None):
        self.root, self.credentials, self.actor = Path(root), credentials, actor
        self.scopes = list(scopes)
        self.cfg = ProjectConfig.model_validate(config.model_dump(mode="json", by_alias=True))
        self.app = None
        self.client: httpx.AsyncClient | None = None
        self._key = ""
        self._stack = AsyncExitStack()
        self._transport_factory = transport_factory
        self.agent_model: str | None = None     # report tools: whose data access applies / who designs
        self.provider_factory = None            # report design: override the model provider (tests)
        sec = self.cfg.security
        self.audit = AuditLogger(str(self.root / sec.audit_log) if sec.audit_log else None)

    async def __aenter__(self) -> "ToolRuntime":
        from ..server import create_app
        key_id, self._key = generate_api_key()
        self.cfg.security.api_keys.append(ApiKeyRecord(
            id=key_id, name=self.actor, hash=hash_api_key(self._key), scopes=self.scopes,
            created=datetime.now(timezone.utc).isoformat()))
        if "api_key" not in self.cfg.security.auth_methods:
            self.cfg.security.auth_methods.append("api_key")
        self.app = create_app(self.cfg, project_root=self.root, credentials=self.credentials)
        await self._stack.enter_async_context(self.app.router.lifespan_context(self.app))
        transport = (self._transport_factory or httpx.ASGITransport)(self.app)
        self.client = httpx.AsyncClient(transport=transport, base_url="http://tools.local",
                                        headers={self.cfg.security.api_key_header: self._key}, timeout=120)
        await self._stack.enter_async_context(self.client)
        return self

    async def __aexit__(self, *exc) -> None:
        await self._stack.aclose()

    # ------------------------------------------------------------- tool makers
    def endpoint_tool(self, ep: EndpointSpec, name: str | None = None, description: str = "") -> Tool:
        from ..builder import is_paginated
        src_type = self.cfg.sources[ep.source].type
        schema = endpoint_schema(ep, is_paginated(ep, src_type))
        where = {p.name: p.location for p in ep.params}
        base = self.cfg.server.base_path.rstrip("/")

        async def run(args: dict) -> Any:
            unknown = sorted(set(args) - set(where) - {"limit", "offset"})
            if unknown:
                raise ToolError(f"unknown argument(s) {unknown}; accepted: {sorted(schema['properties'])}")
            path = ep.path
            for n, loc in where.items():
                if loc == "path":
                    if n not in args:
                        raise ToolError(f"missing path argument '{n}'")
                    path = path.replace("{" + n + "}", quote(str(args[n]), safe=""))
            query = {n: v for n, v in args.items() if v is not None and (where.get(n) == "query" or n in ("limit", "offset"))}
            body = {n: v for n, v in args.items() if v is not None and where.get(n) == "body"}
            resp = await self.client.request(ep.method, base + path, params=query or None, json=body or None)
            try:
                data = resp.json()
            except ValueError:
                data = {"content": resp.text}
            if resp.status_code >= 400:
                raise ToolError(f"HTTP {resp.status_code}: {render(data)}")
            return data

        return Tool(name or ep.name, description or f"{ep.method} {ep.path}", schema, run, "endpoint")

    def sql_tool(self, name: str, source: str, description: str = "", read_only: bool = True, max_rows: int = 200,
                 tables: list[str] | None = None) -> Tool:
        """A SQL tool. ``tables`` (MCP exposure) restricts the tables a query may read - a best-effort text
        check; the real boundary is the database account behind the source credential."""
        spec = self.cfg.sources[source]
        allowed = {t.lower() for t in tables} if tables is not None else None
        writable = not read_only and not spec.read_only

        async def run(args: dict) -> Any:
            q = str(args.get("query") or "").strip().rstrip(";").strip()
            if not q:
                raise ToolError("'query' is required")
            if ";" in q:
                raise ToolError("one statement only (no ';')")
            if not _READ_SQL.match(q) and not writable:
                raise ToolError("only SELECT statements are allowed on this tool")
            if allowed is not None:
                if _COMMA_JOIN.search(q):
                    raise ToolError("use explicit JOIN syntax (no comma joins)")
                for t in _FROM_TABLE.findall(q):
                    t = t.strip('"').lower()
                    if t not in allowed and t.split(".")[-1] not in allowed:
                        raise ToolError(f"table '{t}' is not exposed; allowed: {sorted(allowed)}")
            cap = min(int(args.get("max_rows") or max_rows), max_rows)
            self.audit.record(event="agent_sql", actor=self.actor, source=source, tool=name, statement=q[:500])
            return await asyncio.to_thread(self._run_sql, source, q, cap, writable)

        desc = description or f"Run a {'read-only SELECT' if not writable else 'SQL statement'} on source '{source}'"
        schema = {"type": "object", "required": ["query"], "additionalProperties": False, "properties": {
            "query": {"type": "string", "description": "A single SQL statement"},
            "max_rows": {"type": "integer", "minimum": 1, "maximum": max_rows}}}
        return Tool(name, desc, schema, run, "sql")

    def _run_sql(self, source: str, query: str, cap: int, writable: bool) -> Any:
        from sqlalchemy import text
        from sqlalchemy.exc import SQLAlchemyError
        engine = self.app.state.sources[source].engine
        try:
            with engine.connect() as conn:
                if _READ_SQL.match(query):
                    result = conn.execution_options(stream_results=True).execute(text(query))
                    rows = [dict(r._mapping) for r in result.fetchmany(cap + 1)]
                    result.close()
                    conn.rollback()
                    return {"rows": rows[:cap], "count": min(len(rows), cap), "truncated": len(rows) > cap}
                if not writable:
                    raise ToolError("write statements are not allowed")
                result = conn.execute(text(query))
                conn.commit()
                return {"affected": result.rowcount}
        except SQLAlchemyError as exc:
            raise ToolError(f"database error: {str(getattr(exc, 'orig', exc))[:300]}") from None

    def describe_tool(self, name: str, source: str, tables: list[str]) -> Tool:
        async def run(args: dict) -> Any:
            return await asyncio.to_thread(self._describe, source, tables)

        return Tool(name, f"List the tables and columns available on source '{source}'",
                    {"type": "object", "properties": {}, "additionalProperties": False}, run, "sql")

    def _describe(self, source: str, tables: list[str]) -> Any:
        from sqlalchemy import inspect
        insp = inspect(self.app.state.sources[source].engine)
        out = {}
        for t in tables:
            schema, _, tbl = t.rpartition(".")
            try:
                out[t] = [{"name": c["name"], "type": str(c["type"]), "nullable": c["nullable"]}
                          for c in insp.get_columns(tbl, schema=schema or None)]
            except Exception:  # noqa: BLE001
                out[t] = "not found"
        return out

    def load_tool(self, name: str, load: str) -> Tool:
        async def run(args: dict) -> Any:
            resp = await self.client.post(f"{self.cfg.server.base_path.rstrip('/')}/_loads/{quote(load, safe='')}/run",
                                          json={})
            data = resp.json() if resp.content else {}
            if resp.status_code >= 400:
                raise ToolError(f"HTTP {resp.status_code}: {render(data)}")
            return data

        return Tool(name, f"Run the data load '{load}' (needs scope load:run)",
                    {"type": "object", "properties": {}, "additionalProperties": False}, run, "load")


# --------------------------------------------------------------------------- #
# Registries
# --------------------------------------------------------------------------- #
def mcp_tools(rt: ToolRuntime, server: McpServerSpec, prefix: str = "") -> list[Tool]:
    """The tools an MCP server definition exposes (shared by the MCP server and in-process agent use)."""
    cfg, out = rt.cfg, []
    for en in server.expose.endpoints:
        ep = cfg.endpoint(en)
        out.append(rt.endpoint_tool(ep, prefix + en))
    for s in server.expose.sources:
        out.append(rt.describe_tool(f"{prefix}describe_{s.name}", s.name, s.tables))
        if s.tables:
            out.append(rt.sql_tool(f"{prefix}query_{s.name}", s.name, read_only=s.read_only, tables=s.tables,
                                   description=f"Run a read-only SELECT on {s.name}. Tables: {', '.join(s.tables)}"))
    for ln in server.expose.loads:
        out.append(rt.load_tool(f"{prefix}run_load_{ln}", ln))
    from .file_tools import fs_tools, report_run_tool
    for rn in server.expose.reports:
        t = report_run_tool(rt, rn)
        t.name = prefix + t.name
        out.append(t)
    op_roots: dict[str, list[str]] = {}
    for f in server.expose.filesystem:
        for op in f.ops:
            op_roots.setdefault(op, [])
            if f.root not in op_roots[op]:
                op_roots[op].append(f.root)
    if op_roots:
        out += fs_tools(rt, f"{prefix}fs_", op_roots, blob_mb=server.max_blob_mb)
    return out


def build_registry(rt: ToolRuntime, tool_names: list[str], specs: dict[str, ToolSpec]) -> ToolRegistry:
    reg = ToolRegistry()
    cfg = rt.cfg
    for n in tool_names:
        t = specs[n]
        if t.type == "endpoint":
            reg.add(rt.endpoint_tool(cfg.endpoint(t.endpoint), n, t.description))
        elif t.type == "source":
            reg.add(rt.sql_tool(n, t.source, t.description, t.read_only, t.max_rows))
        elif t.type == "mcp":
            for tool in mcp_tools(rt, cfg.mcp_servers[t.server], prefix=f"{n}__"):
                reg.add(tool)
        elif t.type == "filesystem":
            from .file_tools import fs_tools
            for tool in fs_tools(rt, f"{n}_", {op: list(t.roots) for op in t.ops}):
                reg.add(tool)
        elif t.type == "report":
            from .file_tools import report_tools
            for tool in report_tools(rt, f"{n}_", t.reports, t.ops, t.model):
                reg.add(tool)
    return reg
