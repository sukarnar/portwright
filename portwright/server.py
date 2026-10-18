"""Application factory: wires config -> adapters -> security -> routes."""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import sources as _sources  # noqa: F401 (registers built-in adapters)
from .builder import EndpointBuilder
from .config import DEFAULT_CONFIG, ConfigError, ProjectConfig, credential_refs
from .credentials import CredentialError, CredentialManager, install_redaction
from .credentials.manager import expiring_soon
from .security.audit import AuditLogger
from .security.auth import Authenticator
from .security.middleware import SecurityMiddleware
from .security.ratelimit import InMemoryRateLimiter, RateLimiter
from .sources.base import create_source

log = logging.getLogger("portwright")


class TokenRequest(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=256)


def _referenced_credentials(config: ProjectConfig) -> set[str]:
    names: set[str] = set(credential_refs(config.security.jwt.secret))
    for spec in config.sources.values():
        if spec.credential:
            names.add(spec.credential)
        names.update(spec.credentials)
        names |= credential_refs(spec.url) | credential_refs(spec.headers) | credential_refs(spec.base_url)
    return names


def create_app(config: ProjectConfig | str | Path = DEFAULT_CONFIG,
               project_root: str | Path | None = None,
               limiter: RateLimiter | None = None,
               credentials: CredentialManager | None = None) -> FastAPI:
    if not isinstance(config, ProjectConfig):
        cfg_path = Path(config)
        project_root = project_root or cfg_path.resolve().parent
        config = ProjectConfig.load(cfg_path)
    project_root = str(project_root or Path.cwd())
    sec = config.security

    # ------------------------------------------------ credential management
    credentials = credentials or CredentialManager.from_settings(sec.credentials, project_root)
    install_redaction(credentials)
    cred_problems = []
    for name in sorted(_referenced_credentials(config)):
        try:
            credentials.get(name, consumer="startup-check")
        except CredentialError as exc:
            cred_problems.append(str(exc))
    if cred_problems:
        raise ConfigError("Credential problems:\n  - " + "\n  - ".join(cred_problems))
    for c in expiring_soon(credentials.list()):
        log.warning("credential '%s' expires on %s – rotate it soon", c.name, c.expires)

    data_sources = {name: create_source(name, spec, sec, project_root, credentials)
                    for name, spec in config.sources.items()}
    authenticator = Authenticator(config, credentials)
    limiter = limiter or InMemoryRateLimiter()
    audit_path = str(Path(project_root) / sec.audit_log) if sec.audit_log else None
    audit = AuditLogger(audit_path)

    builder = EndpointBuilder(config, data_sources, authenticator, limiter, audit)
    problems = builder.validate()
    if problems:
        raise ConfigError("Invalid endpoint definitions:\n  - " + "\n  - ".join(problems))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        for s in data_sources.values():
            await s.startup()
        log.info("portwright '%s' started with %d endpoints", config.project, len(config.endpoints))
        try:
            yield
        finally:
            for s in data_sources.values():
                await s.shutdown()

    base = config.server.base_path.rstrip("/")
    docs = sec.expose_docs
    app = FastAPI(
        title=config.project, version=config.version, lifespan=lifespan,
        docs_url=None, redoc_url=None,          # served below from bundled assets (works offline)
        openapi_url=f"{base}/openapi.json" if docs else None,
    )
    app.state.config = config
    app.state.sources = data_sources
    app.state.credentials = credentials

    # ---------------------------------------------------------- middleware
    if sec.cors_origins:
        if "*" in sec.cors_origins:
            raise ConfigError("Wildcard CORS origin is not allowed; list explicit origins")
        app.add_middleware(CORSMiddleware, allow_origins=sec.cors_origins, allow_credentials=False,
                           allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
                           allow_headers=["Authorization", "Content-Type", sec.api_key_header])
    app.add_middleware(SecurityMiddleware, max_body_bytes=sec.max_body_bytes,
                       hsts=bool(config.server.ssl_certfile),
                       docs_paths=(f"{base}/docs", f"{base}/openapi.json", f"{base}/_static") if docs else ())

    if docs:
        # Swagger UI assets ship inside the package: no CDN, so /docs works on air-gapped servers.
        app.mount(f"{base}/_static", StaticFiles(directory=str(Path(__file__).parent / "static")),
                  name="portwright-static")

        @app.get(f"{base}/docs", include_in_schema=False)
        async def swagger_ui():
            return get_swagger_ui_html(
                openapi_url=f"{base}/openapi.json", title=f"{config.project} – API docs",
                swagger_js_url=f"{base}/_static/swagger/swagger-ui-bundle.js",
                swagger_css_url=f"{base}/_static/swagger/swagger-ui.css",
                swagger_favicon_url=f"{base}/_static/swagger/favicon-32x32.png")

    # ----------------------------------------------------- system routes
    @app.get("/health", include_in_schema=False)
    async def health():
        return {"status": "ok"}             # liveness: no internal details leaked

    @app.get(f"{base}/_meta/health", tags=["system"])
    async def detailed_health(request: Request):
        authenticator.require(request, ["admin"], public=False)
        return {name: await s.health() for name, s in data_sources.items()}

    @app.get(f"{base}/_meta/endpoints", tags=["system"])
    async def list_endpoints(request: Request):
        authenticator.require(request, ["admin"], public=False)
        return [{"name": e.name, "method": e.method, "path": base + e.path, "source": e.source,
                 "public": e.public, "scopes": e.scopes} for e in config.endpoints]

    @app.get(f"{base}/_meta/credentials", tags=["system"])
    async def list_credentials(request: Request):
        """Inventory only – names, types, providers, expiry, consumers. Never secrets."""
        authenticator.require(request, ["admin"], public=False)
        return [{"name": c.name, "type": c.type, "provider": c.provider, "expires": c.expires,
                 "updated": c.updated, "used_by": sorted(credentials.usage.get(c.name, set()) - {"startup-check"})}
                for c in credentials.list()]

    # ------------------------------------------------------- load jobs
    from .loads import LoadRunner
    load_runner = LoadRunner(config, data_sources, project_root, audit)

    @app.get(f"{base}/_loads", tags=["loads"])
    async def list_loads(request: Request):
        authenticator.require(request, ["load:run"], public=False)
        return [{"name": l.name, "from": l.from_.endpoint or l.from_.source,
                 "to": f"{l.to.source}.{l.to.target}", "mode": l.mode, "columns": len(l.columns)}
                for l in config.loads]

    @app.post(f"{base}/_loads/{{name}}/run", tags=["loads"])
    async def run_load(name: str, request: Request, dry_run: bool = False, force: bool = False):
        """Run a load job. 200 = success/partial/dry_run/skipped (see 'status'), 422 = failed and rolled back.
        'skipped' means the source is unchanged since the last successful load; force=true loads anyway."""
        authenticator.require(request, ["load:run"], public=False)
        spec = config.load_job(name)
        if spec is None:
            raise HTTPException(404, f"No load '{name}'")
        rep = await load_runner.run(spec, dry_run=dry_run, force=force)
        body = rep.to_dict()
        body.pop("preview", None)
        if body.get("rejects_file"):
            body["rejects_file"] = Path(body["rejects_file"]).name     # don't reveal server paths
        return JSONResponse(body, status_code=422 if rep.status == "failed" else 200)

    if "jwt" in sec.auth_methods:
        @app.post(f"{base}/auth/token", tags=["auth"])
        async def issue_token(body: TokenRequest, request: Request):
            ip = request.client.host if request.client else "unknown"
            rl = sec.rate_limit
            for key in (f"login|ip:{ip}", f"login|user:{body.username}"):
                ok, retry = limiter.hit(key, rl.login_requests, rl.window_seconds)
                if not ok:
                    raise HTTPException(429, "Too many login attempts", headers={"Retry-After": str(retry)})
            result = authenticator.login(body.username, body.password)
            audit.record(event="login", user=body.username, ip=ip)
            return result

    app.include_router(builder.build(), prefix=base)
    return app


def app_from_env() -> FastAPI:
    """Factory used by ``portwright serve`` (supports uvicorn --reload)."""
    from .compat import getenv, resolve_config
    return create_app(getenv("PORTWRIGHT_CONFIG") or resolve_config(DEFAULT_CONFIG))
