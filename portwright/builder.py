"""EndpointBuilder: turns declarative ``EndpointSpec`` objects into FastAPI routes.

Each request flows through a fixed pipeline (Chain of Responsibility):

    authenticate -> authorize (scopes) -> rate-limit -> validate input
        -> adapter.execute -> audit

The pipeline is identical for every source type; only the adapter differs.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from decimal import Decimal as _Decimal

# database NUMBER/NUMERIC values: whole numbers stay integers, others become JSON numbers
_ENCODERS = {_Decimal: lambda d: int(d) if d == d.to_integral_value() and abs(d) < 2 ** 53 else float(d),
             bytes: lambda b: b.hex()}
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError, create_model

from .config import EndpointSpec, ProjectConfig
from .credentials.types import CredentialError
from .security.audit import AuditLogger
from .security.auth import Authenticator
from .security.ratelimit import RateLimiter
from .sources.base import DataSource, ExecutionContext, SourceError

log = logging.getLogger("portwright")

_PY_TYPES = {"string": str, "integer": int, "number": float, "boolean": bool}
_OAS_TYPES = {"string": "string", "integer": "integer", "number": "number", "boolean": "boolean"}
RESERVED = {"limit", "offset"}


def build_param_model(ep: EndpointSpec) -> type[BaseModel]:
    """Generate a strict pydantic model from the endpoint's declared params."""
    fields: dict[str, Any] = {}
    for p in ep.params:
        py_type: Any = _PY_TYPES[p.type]
        if p.enum:
            py_type = Literal[tuple(p.enum)]  # type: ignore[valid-type]
        constraints: dict[str, Any] = {"description": p.description}
        if p.type in ("integer", "number"):
            if p.min is not None:
                constraints["ge"] = p.min
            if p.max is not None:
                constraints["le"] = p.max
        if p.type == "string" and not p.enum:
            if p.max_length:
                constraints["max_length"] = p.max_length
            if p.pattern:
                constraints["pattern"] = p.pattern
        if p.required:
            fields[p.name] = (py_type, Field(..., **constraints))
        else:
            fields[p.name] = (py_type | None, Field(p.default, **constraints))
    model_name = re.sub(r"\W", "_", ep.name.title()) + "Params"
    # extra="forbid": unknown inputs are rejected, not silently passed along
    return create_model(model_name, __config__={"extra": "forbid"}, **fields)


def is_paginated(ep: EndpointSpec, source_type: str) -> bool:
    if ep.method != "GET":
        return False
    if source_type == "sql":
        return (ep.operation or "query") in ("query", "list")
    if source_type == "file":
        return not ep.key
    return False


def _openapi(ep: EndpointSpec, paginated: bool) -> dict:
    params, body_props, body_required = [], {}, []
    for p in ep.params:
        schema: dict[str, Any] = {"type": _OAS_TYPES[p.type]}
        if p.enum:
            schema["enum"] = p.enum
        if p.max_length and p.type == "string":
            schema["maxLength"] = p.max_length
        if p.min is not None:
            schema["minimum"] = p.min
        if p.max is not None:
            schema["maximum"] = p.max
        if p.location == "body":
            body_props[p.name] = schema
            if p.required:
                body_required.append(p.name)
        else:
            params.append({"name": p.name, "in": p.location, "required": p.required,
                           "description": p.description, "schema": schema})
    if paginated:
        params += [
            {"name": "limit", "in": "query", "schema": {"type": "integer", "minimum": 1, "maximum": ep.max_limit}},
            {"name": "offset", "in": "query", "schema": {"type": "integer", "minimum": 0}},
        ]
    extra: dict[str, Any] = {"parameters": params}
    if body_props:
        extra["requestBody"] = {"required": bool(body_required), "content": {"application/json": {
            "schema": {"type": "object", "properties": body_props, "required": body_required,
                       "additionalProperties": False}}}}
    return extra


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


class EndpointBuilder:
    def __init__(self, config: ProjectConfig, sources: dict[str, DataSource],
                 authenticator: Authenticator, limiter: RateLimiter, audit: AuditLogger):
        self.config = config
        self.sources = sources
        self.auth = authenticator
        self.limiter = limiter
        self.audit = audit

    def validate(self) -> list[str]:
        """Return a list of problems (empty list == valid)."""
        problems = []
        for ep in self.config.endpoints:
            names = {p.name for p in ep.params}
            src = self.sources[ep.source]
            if is_paginated(ep, src.type_name) and names & RESERVED:
                problems.append(f"[{ep.name}] params {sorted(names & RESERVED)} are reserved for pagination")
            if not ep.public and not ep.scopes:
                log.warning("endpoint '%s' requires auth but declares no scopes", ep.name)
            try:
                src.validate_endpoint(ep)
            except ValueError as exc:
                problems.append(str(exc))
        return problems

    def build(self) -> APIRouter:
        router = APIRouter()
        for ep in self.config.endpoints:
            src = self.sources[ep.source]
            paginated = is_paginated(ep, src.type_name)
            router.add_api_route(
                ep.path, self._make_handler(ep, src, paginated), methods=[ep.method],
                name=ep.name, summary=ep.description or ep.name,
                tags=ep.tags or [ep.source], openapi_extra=_openapi(ep, paginated),
            )
        return router

    # ---------------------------------------------------------------- handler
    def _make_handler(self, ep: EndpointSpec, source: DataSource, paginated: bool):
        Model = build_param_model(ep)
        sec = self.config.security
        limit_n = ep.rate_limit or sec.rate_limit.requests
        window = sec.rate_limit.window_seconds
        locations = {p.name: p.location for p in ep.params}

        async def handler(request: Request):
            started = time.perf_counter()
            request_id = request.scope.get("state", {}).get("request_id", "")
            principal = None
            status_code = 500
            try:
                # 1-2. authenticate + authorize
                principal = self.auth.require(request, ep.scopes, ep.public)
                # 3. rate-limit (per principal; anonymous callers per IP)
                rl_key = principal.subject if principal.kind != "anonymous" else f"ip:{_client_ip(request)}"
                allowed, retry = self.limiter.hit(f"{ep.name}|{rl_key}", limit_n, window)
                if not allowed:
                    raise HTTPException(429, "Rate limit exceeded", headers={"Retry-After": str(retry)})
                # 4. collect + validate inputs
                raw, limit, offset = await self._collect(request, ep, locations, paginated)
                try:
                    params = Model.model_validate(raw).model_dump()
                except ValidationError as exc:
                    # Do not echo input values back (could reflect secrets/PII)
                    errors = [{"field": ".".join(map(str, e["loc"])), "error": e["msg"]} for e in exc.errors()]
                    raise HTTPException(422, {"message": "Invalid parameters", "errors": errors}) from None
                # 5. execute
                ctx = ExecutionContext(ep, params, principal, limit, offset, request_id)
                data = await source.execute(ctx)
                status_code = 201 if ep.method == "POST" and ep.operation == "create" else 200
                return JSONResponse(jsonable_encoder(data, custom_encoder=_ENCODERS), status_code=status_code)
            except HTTPException as exc:
                status_code = exc.status_code
                raise
            except SourceError as exc:
                status_code = exc.status_code
                if exc.__cause__:
                    log.error("request %s failed in source '%s'", request_id, ep.source, exc_info=exc.__cause__)
                return JSONResponse({"error": str(exc), "request_id": request_id}, status_code=status_code)
            except CredentialError as exc:
                status_code = 500
                log.error("credential error in endpoint '%s' (request %s): %s", ep.name, request_id, exc)
                return JSONResponse({"error": "Server credential configuration error", "request_id": request_id},
                                    status_code=500)
            except Exception:
                log.exception("unhandled error in endpoint '%s' (request %s)", ep.name, request_id)
                return JSONResponse({"error": "Internal server error", "request_id": request_id}, status_code=500)
            finally:
                # 6. audit
                self.audit.record(request_id=request_id, endpoint=ep.name, method=ep.method,
                                  path=request.url.path, status=status_code,
                                  principal=getattr(principal, "subject", None),
                                  ip=_client_ip(request),
                                  ms=round((time.perf_counter() - started) * 1000, 1))

        handler.__name__ = f"handle_{ep.name.replace('-', '_')}"
        return handler

    async def _collect(self, request: Request, ep: EndpointSpec, locations: dict[str, str],
                       paginated: bool) -> tuple[dict[str, Any], int, int]:
        raw: dict[str, Any] = dict(request.path_params)
        query = dict(request.query_params)
        limit, offset = 100, 0
        if paginated:
            try:
                limit = int(query.pop("limit", min(100, ep.max_limit)))
                offset = int(query.pop("offset", 0))
            except ValueError:
                raise HTTPException(422, "limit/offset must be integers") from None
            if not (1 <= limit <= ep.max_limit) or offset < 0:
                raise HTTPException(422, f"limit must be 1..{ep.max_limit} and offset >= 0")
        for k, v in query.items():
            if locations.get(k) != "query":
                raise HTTPException(422, f"Unknown query parameter '{k}'")
            raw[k] = v
        if any(loc == "body" for loc in locations.values()):
            ctype = request.headers.get("content-type", "")
            if not ctype.startswith("application/json"):
                raise HTTPException(415, "Content-Type must be application/json")
            try:
                body = await request.json()
            except Exception:
                raise HTTPException(400, "Malformed JSON body") from None
            if not isinstance(body, dict):
                raise HTTPException(422, "JSON body must be an object")
            for k, v in body.items():
                if locations.get(k) != "body":
                    raise HTTPException(422, f"Unknown body field '{k}'")
                raw[k] = v
        return raw, limit, offset
