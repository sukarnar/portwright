"""MCP server definitions: publish chosen endpoints, SQL sources and loads as MCP tools.

Needs the optional ``mcp`` package (``pip install portwright[agents]``). The tools are the same ones agents use
(``portwright.agents.tools``), so scopes, rate limits, audit and read-only rules apply identically.
"""
from __future__ import annotations

from pathlib import Path

from ..config import ConfigError, ProjectConfig
from ..agents.tools import ToolError, ToolRegistry, ToolRuntime, mcp_tools, render


def _sdk():
    try:
        import mcp.types as types
        from mcp.server.lowlevel import Server
    except ImportError:
        raise ConfigError("MCP support needs the 'mcp' package: pip install \"portwright[agents]\"") from None
    return types, Server


def describe_server(config: ProjectConfig, name: str) -> list[dict]:
    """Tool names/descriptions a server would publish, without starting anything (for 'mcp list')."""
    spec = config.mcp_servers.get(name)
    if spec is None:
        raise ConfigError(f"no MCP server '{name}'")
    out = [{"tool": e, "kind": "endpoint", "description": f"{config.endpoint(e).method} {config.endpoint(e).path}"}
           for e in spec.expose.endpoints]
    for s in spec.expose.sources:
        out.append({"tool": f"describe_{s.name}", "kind": "sql", "description": f"tables: {', '.join(s.tables) or '-'}"})
        if s.tables:
            out.append({"tool": f"query_{s.name}", "kind": "sql", "description": "read-only SELECT"})
    out += [{"tool": f"run_load_{l}", "kind": "load", "description": f"run load '{l}'"} for l in spec.expose.loads]
    return out


def build_server(name: str, registry: ToolRegistry):
    """A low-level MCP Server whose tools are the registry's tools."""
    types, Server = _sdk()
    server = Server(f"portwright-{name}")

    @server.list_tools()
    async def _list() -> list:
        return [types.Tool(name=t.name, description=t.description, inputSchema=t.parameters)
                for t in registry.tools.values()]

    @server.call_tool()
    async def _call(tool: str, arguments: dict) -> list:
        try:
            return [types.TextContent(type="text", text=render(await registry.call(tool, arguments or {})))]
        except ToolError as exc:
            raise ValueError(str(exc)) from None      # becomes an MCP error result for the client

    return server


async def serve_stdio(config: ProjectConfig, root: str | Path, credentials, name: str) -> None:
    _, _ = _sdk()
    from mcp.server.stdio import stdio_server
    spec = config.mcp_servers.get(name)
    if spec is None:
        raise ConfigError(f"no MCP server '{name}'")
    async with ToolRuntime(config, root, credentials, spec.scopes, f"mcp:{name}") as rt:
        server = build_server(name, ToolRegistry(mcp_tools(rt, spec)))
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())


async def serve_http(config: ProjectConfig, root: str | Path, credentials, name: str,
                     host: str = "127.0.0.1", port: int = 8765) -> None:
    """Streamable-HTTP transport. Every request must carry an API key/JWT that holds the server's scopes."""
    _sdk()
    import uvicorn
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from starlette.applications import Starlette
    from starlette.exceptions import HTTPException
    from starlette.requests import Request
    from starlette.responses import PlainTextResponse
    from starlette.routing import Mount

    from ..security.auth import Authenticator
    spec = config.mcp_servers.get(name)
    if spec is None:
        raise ConfigError(f"no MCP server '{name}'")
    auth = Authenticator(config, credentials)
    async with ToolRuntime(config, root, credentials, spec.scopes, f"mcp:{name}") as rt:
        server = build_server(name, ToolRegistry(mcp_tools(rt, spec)))
        manager = StreamableHTTPSessionManager(app=server, json_response=True)

        async def handle(scope, receive, send):
            try:
                auth.require(Request(scope), spec.scopes, public=False)
            except HTTPException as exc:
                await PlainTextResponse(str(exc.detail), status_code=exc.status_code)(scope, receive, send)
                return
            await manager.handle_request(scope, receive, send)

        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def lifespan(_app):
            async with manager.run():
                yield

        app = Starlette(routes=[Mount("/mcp", app=handle)], lifespan=lifespan)
        await uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning")).serve()
