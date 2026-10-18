"""Upstream REST adapter (proxy / facade over another HTTP API).

Security properties (SSRF hardening):
* The upstream host must be on ``security.allowed_upstream_hosts`` (deny-by-default).
* Redirects are not followed; path params are URL-encoded; only *declared*
  params are forwarded; caller credentials are never forwarded upstream.
* Upstream secrets live in env vars (``${ENV:...}`` in headers).
* Response size and timeout are bounded.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from ..config import EndpointSpec
from ..credentials.types import CredentialError, build_http_auth
from .base import DataSource, ExecutionContext, SourceError, register

_MAX_RESPONSE_BYTES = 10 * 1024 * 1024


@register("rest")
class RestSource(DataSource):
    client: httpx.AsyncClient | None = None

    def _check_host(self) -> None:
        parts = urlsplit(self.spec.base_url)
        if parts.scheme not in ("https", "http"):
            raise ValueError(f"source '{self.name}': base_url must be http(s)")
        host = (parts.hostname or "").lower()
        allowed = [h.lower() for h in self.security.allowed_upstream_hosts]
        if host not in allowed:
            raise ValueError(
                f"source '{self.name}': host '{host}' is not in security.allowed_upstream_hosts")

    def validate_endpoint(self, ep: EndpointSpec) -> None:
        self._check_host()
        self.validate_credential()

    def validate_credential(self) -> None:
        cred = self.credential()
        if cred is not None:
            try:
                build_http_auth(cred, self.security.allowed_upstream_hosts)   # type + token host check
            except CredentialError as exc:
                raise ValueError(f"source '{self.name}': {exc}") from None

    async def _build_client(self) -> None:
        auth, cred_headers, self._cred_params = None, {}, {}
        cred = self.credential()
        if cred is not None:
            auth, cred_headers, self._cred_params = build_http_auth(cred, self.security.allowed_upstream_hosts)
            self._cred_version = cred.updated
        old, self.client = self.client, httpx.AsyncClient(
            base_url=self.spec.base_url.rstrip("/"),
            headers={**self.resolve(self.spec.headers), **cred_headers},
            auth=auth,
            timeout=self.spec.timeout_seconds,
            follow_redirects=False,
            trust_env=True,
        )
        if old is not None:
            await old.aclose()

    async def startup(self) -> None:
        self._check_host()
        self._cred_version = None
        self._cred_params: dict = {}
        await self._build_client()

    async def _refresh_if_rotated(self) -> None:
        if self.spec.credential and self.credential().updated != self._cred_version:
            await self._build_client()

    async def shutdown(self) -> None:
        if self.client:
            await self.client.aclose()

    async def execute(self, ctx: ExecutionContext) -> Any:
        await self._refresh_if_rotated()
        ep = ctx.endpoint
        path_vals = {p.name: quote(str(ctx.params[p.name]), safe="")
                     for p in ep.params if p.location == "path"}
        upstream_path = (ep.upstream_path or ep.path).format(**path_vals)
        if not upstream_path.startswith("/"):
            upstream_path = "/" + upstream_path
        query = {p.name: ctx.params[p.name] for p in ep.params
                 if p.location == "query" and ctx.params.get(p.name) is not None}
        body = {p.name: ctx.params[p.name] for p in ep.params
                if p.location == "body" and p.name in ctx.params}
        method = ep.upstream_method or ep.method
        try:
            resp = await self.client.request(
                method, upstream_path, params={**query, **self._cred_params} or None,
                json=body if body and method not in ("GET", "DELETE") else None,
                headers={"X-Request-ID": ctx.request_id},
            )
        except httpx.TimeoutException as exc:
            raise SourceError("Upstream service timed out", 504) from exc
        except CredentialError as exc:
            raise SourceError("Upstream authentication failed", 502) from exc
        except httpx.HTTPError as exc:
            raise SourceError("Upstream service unavailable", 502) from exc

        if len(resp.content) > _MAX_RESPONSE_BYTES:
            raise SourceError("Upstream response too large", 502)
        if resp.status_code >= 400:
            status = resp.status_code if resp.status_code in (400, 404, 409, 422) else 502
            raise SourceError(f"Upstream returned HTTP {resp.status_code}", status)
        if "application/json" in resp.headers.get("content-type", ""):
            return resp.json()
        return {"content": resp.text}
