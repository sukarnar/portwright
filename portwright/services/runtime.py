"""Runtime operations: validate the whole project, call an endpoint in-process."""
from __future__ import annotations

import getpass
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, urlencode

from ..config import ApiKeyRecord, ProjectConfig
from ..security.hashing import generate_api_key, hash_api_key
from .project import Project, ServiceError, _clean


@dataclass
class Validation:
    ok: bool
    message: str
    warnings: list[str] = field(default_factory=list)


@dataclass
class CallResult:
    status: int
    data: Any

    @property
    def ok(self) -> bool:
        return self.status < 400


class RuntimeService:
    def __init__(self, project: Project):
        self.p = project

    def validate(self) -> Validation:
        from ..server import create_app
        cfg = self.p.config
        try:
            create_app(ProjectConfig.model_validate(cfg.model_dump(mode="json", by_alias=True)),
                       project_root=self.p.root)
        except Exception as exc:
            return Validation(False, _clean(exc))
        warnings = []
        public = [e.name for e in cfg.endpoints if e.public]
        if public:
            warnings.append(f"public endpoints: {', '.join(public)}")
        return Validation(True, f"{len(cfg.endpoints)} endpoints across {len(cfg.sources)} sources are valid",
                          warnings)

    def _draft_config(self, endpoint: dict | None, original: str | None = None) -> ProjectConfig:
        """A copy of the project with an unsaved endpoint added (or replacing ``original``)."""
        data = self.p.config.model_dump(mode="json", by_alias=True)
        if endpoint is not None:
            keep = [e for e in data["endpoints"] if e["name"] not in {original, endpoint.get("name")}]
            data["endpoints"] = keep + [endpoint]
        try:
            return ProjectConfig.model_validate(data)
        except Exception as exc:
            raise ServiceError(_clean(exc)) from None

    def validate_endpoint(self, endpoint: dict, original: str | None = None) -> Validation:
        """Build the app with an unsaved endpoint (adapter checks: table, columns, binds, read-only…)."""
        from ..server import create_app
        try:
            cfg = self._draft_config(endpoint, original)
            create_app(cfg, project_root=self.p.root)
        except Exception as exc:
            return Validation(False, _clean(exc))
        return Validation(True, f"'{endpoint.get('name')}' is valid")

    def try_endpoint(self, endpoint: dict, values: dict[str, str] | None = None, *, body: dict | None = None,
                     limit: int | None = None, offset: int | None = None, original: str | None = None,
                     actor: str = "studio") -> CallResult:
        """'Try it' for an unsaved endpoint: same pipeline as call(), nothing is saved."""
        cfg = self._draft_config(endpoint, original)
        return self._call(cfg, endpoint["name"], values, body=body, limit=limit, offset=offset, actor=actor)

    def call(self, endpoint: str, values: dict[str, str] | None = None, *, body: dict | None = None,
             limit: int | None = None, offset: int | None = None, actor: str = "cli") -> CallResult:
        """Run an endpoint through the full pipeline with a single-use, never-saved admin key."""
        cfg = ProjectConfig.model_validate(self.p.config.model_dump(mode="json", by_alias=True))
        return self._call(cfg, endpoint, values, body=body, limit=limit, offset=offset, actor=actor)

    def _call(self, cfg: ProjectConfig, endpoint: str, values, *, body, limit, offset, actor) -> CallResult:
        from fastapi.testclient import TestClient
        from ..server import create_app

        ep = cfg.endpoint(endpoint)
        if ep is None:
            raise ServiceError(f"no endpoint '{endpoint}'")
        values = {k: v for k, v in (values or {}).items() if v not in (None, "")}
        where = {p.name: p.location for p in ep.params}
        unknown = sorted(set(values) - set(where))
        if unknown:
            raise ServiceError(f"unknown param(s) {unknown}; '{endpoint}' accepts {sorted(where) or 'none'}")
        path = ep.path
        for n, loc in where.items():
            if loc == "path":
                if n not in values:
                    raise ServiceError(f"missing path param '{n}'")
                path = path.replace("{" + n + "}", quote(str(values[n]), safe=""))
        query: dict[str, Any] = {n: v for n, v in values.items() if where[n] == "query"}
        if limit is not None:
            query["limit"] = limit
        if offset is not None:
            query["offset"] = offset
        json_body = {n: v for n, v in values.items() if where[n] == "body"}
        json_body.update(body or {})

        key_id, key = generate_api_key()
        cfg.security.api_keys.append(ApiKeyRecord(
            id=key_id, name=f"{actor}:{getpass.getuser()}", hash=hash_api_key(key), scopes=["*"],
            created=datetime.now(timezone.utc).isoformat()))
        if "api_key" not in cfg.security.auth_methods:
            cfg.security.auth_methods.append("api_key")
        try:
            app = create_app(cfg, project_root=self.p.root)
        except Exception as exc:
            raise ServiceError(_clean(exc)) from None
        url = cfg.server.base_path.rstrip("/") + path + (("?" + urlencode(query)) if query else "")
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.request(ep.method, url, headers={cfg.security.api_key_header: key},
                                  json=json_body if json_body else None)
        try:
            data = resp.json()
        except Exception:
            data = {"content": resp.text}
        return CallResult(resp.status_code, data)

    def snippets(self, endpoint: str | dict, values: dict[str, str] | None = None,
                 host: str = "http://localhost") -> dict:
        """Ready-to-paste curl.exe and PowerShell calls for an endpoint (saved, or a draft dict)."""
        cfg = self.p.config
        if isinstance(endpoint, dict):
            from ..config import EndpointSpec
            try:
                ep = EndpointSpec.model_validate(endpoint)
            except Exception as exc:
                raise ServiceError(_clean(exc)) from None
            endpoint = ep.name
        else:
            ep = cfg.endpoint(endpoint)
        if ep is None:
            raise ServiceError(f"no endpoint '{endpoint}'")
        values = {k: v for k, v in (values or {}).items() if v not in (None, "")}
        path = ep.path
        for p in ep.params:
            if p.location == "path":
                path = path.replace("{" + p.name + "}", str(values.get(p.name, f"<{p.name}>")))
        query = {p.name: values[p.name] for p in ep.params if p.location == "query" and p.name in values}
        body = {p.name: values[p.name] for p in ep.params if p.location == "body" and p.name in values}
        url = f"{host}:{cfg.server.port}{cfg.server.base_path}{path}" + (f"?{urlencode(query)}" if query else "")
        hdr = cfg.security.api_key_header
        auth_ps = "" if ep.public else f' -Headers @{{ "{hdr}" = "<your-key>" }}'
        auth_curl = "" if ep.public else f' -H "{hdr}: <your-key>"'
        ps = f'Invoke-RestMethod "{url}" -Method {ep.method.title()}{auth_ps}'
        curl = f'curl.exe -X {ep.method}{auth_curl} "{url}"'
        if body:
            js = json.dumps(body)
            ps += f" -ContentType 'application/json' -Body '{js}'"
            curl += f' -H "Content-Type: application/json" -d "{js.replace(chr(34), chr(92) + chr(34))}"'
        cli = f"portwright call {endpoint}" + "".join(f" -p {k}={v}" for k, v in values.items())
        return {"powershell": ps, "curl": curl, "portwright": cli}
