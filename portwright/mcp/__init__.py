"""MCP server definitions: publish chosen endpoints, SQL sources, loads, reports and folders as MCP tools.

Exposed folders (``expose.filesystem`` with the ``read`` op) are also listed as MCP resources
(``portwright://fs/<root>:<path>``), and ``fs_read`` returns binary files (PDF, Excel, ...) as embedded resources
up to ``max_blob_mb``.

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
    out += [{"tool": f"run_report_{r}", "kind": "report",
             "description": f"run report '{r}' ({', '.join(config.report(r).effective_formats())})"}
            for r in spec.expose.reports]
    ops: dict[str, list[str]] = {}
    for f in spec.expose.filesystem:
        for op in f.ops:
            ops.setdefault(op, []).append(f.root)
    out += [{"tool": f"fs_{op}", "kind": "filesystem", "description": f"roots: {', '.join(roots)}"}
            for op, roots in ops.items()]
    return out


def to_content(types, result) -> list:
    """A tool result as MCP content: text, or an embedded blob resource for binary files."""
    if isinstance(result, dict) and isinstance(result.get("_blob"), dict):
        b = result["_blob"]
        meta = {k: v for k, v in result.items() if k != "_blob"}
        return [types.TextContent(type="text", text=render(meta)),
                types.EmbeddedResource(type="resource", resource=types.BlobResourceContents(
                    uri=b["uri"], mimeType=b["mime"], blob=b["data"]))]
    return [types.TextContent(type="text", text=render(result))]


def _resource_uri(address: str) -> str:
    from urllib.parse import quote
    return "portwright://fs/" + quote(address, safe="/:")


def build_server(name: str, registry: ToolRegistry, sandbox=None, read_roots: list[str] | None = None,
                 max_blob_bytes: int = 10 * 1024 * 1024):
    """A low-level MCP Server whose tools are the registry's tools. With a *sandbox* and *read_roots*, the files
    in those roots are also published as resources."""
    types, Server = _sdk()
    server = Server(f"portwright-{name}")

    @server.list_tools()
    async def _list() -> list:
        return [types.Tool(name=t.name, description=t.description, inputSchema=t.parameters)
                for t in registry.tools.values()]

    @server.call_tool()
    async def _call(tool: str, arguments: dict) -> list:
        try:
            return to_content(types, await registry.call(tool, arguments or {}))
        except ToolError as exc:
            raise ValueError(str(exc)) from None      # becomes an MCP error result for the client

    if sandbox is not None and read_roots:
        import asyncio
        import mimetypes
        from urllib.parse import unquote

        def listing() -> list:
            out = []
            for root in read_roots:
                try:
                    for e in sandbox.search(f"{root}:", None, None, 200):
                        out.append(types.Resource(uri=_resource_uri(e["address"]), name=e["address"],
                                                  mimeType=mimetypes.guess_type(e["name"])[0] or None,
                                                  size=e["bytes"]))
                except Exception:                     # noqa: BLE001 - one bad root must not hide the others
                    continue
            return out

        @server.list_resources()
        async def _resources() -> list:
            return await asyncio.to_thread(listing)

        @server.read_resource()
        async def _read(uri) -> list:
            from mcp.server.lowlevel.helper_types import ReadResourceContents
            from ..fs import FsError
            u = str(uri)
            if not u.startswith("portwright://fs/"):
                raise ValueError("unknown resource")
            address = unquote(u[len("portwright://fs/"):])
            try:
                pl, data = await asyncio.to_thread(sandbox.read_bytes, address, max_blob_bytes)
            except FsError as exc:
                raise ValueError(str(exc)) from None
            mime = mimetypes.guess_type(pl.path.name)[0] or "application/octet-stream"
            try:
                return [ReadResourceContents(content=data.decode("utf-8"), mime_type=mime)]
            except UnicodeDecodeError:
                return [ReadResourceContents(content=data, mime_type=mime)]

    return server


def _server_for(spec, name: str, rt):
    """The server plus its resource sandbox (roots exposed with the read op)."""
    from ..fs import Sandbox
    read_roots = [f.root for f in spec.expose.filesystem if "read" in f.ops and not f.root.startswith("dest:")]
    sandbox = Sandbox(rt.cfg, rt.root, scopes=rt.scopes, actor=rt.actor, audit=rt.audit,
                      allowed=read_roots) if read_roots else None
    return build_server(name, ToolRegistry(mcp_tools(rt, spec)), sandbox, read_roots,
                        int(spec.max_blob_mb * 1024 * 1024))


async def serve_stdio(config: ProjectConfig, root: str | Path, credentials, name: str) -> None:
    _, _ = _sdk()
    from mcp.server.stdio import stdio_server
    spec = config.mcp_servers.get(name)
    if spec is None:
        raise ConfigError(f"no MCP server '{name}'")
    async with ToolRuntime(config, root, credentials, spec.scopes, f"mcp:{name}") as rt:
        server = _server_for(spec, name, rt)
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
        server = _server_for(spec, name, rt)
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
