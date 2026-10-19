"""Sources and endpoints: add, change, remove, test, introspect."""
from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import anyio

from ..config import EndpointSpec, ParamSpec, SourceSpec
from .project import Project, ServiceError, _clean


@dataclass
class TestResult:
    ok: bool
    message: str
    columns: list[str] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)


MEMBER_COLUMNS = ["name", "account", "mail", "kind", "status"]


def member_rows(members) -> list[dict[str, Any]]:
    """Group members as table rows, users first, sorted by name."""
    rows = [{"name": m.name or m.dn.split(",")[0].split("=", 1)[-1], "account": m.sam or "", "mail": m.mail or m.upn or "",
             "kind": m.kind, "status": "disabled" if m.disabled else ("" if m.kind == "user" else "kept")}
            for m in members]
    return sorted(rows, key=lambda r: (r["kind"] != "user", r["name"].lower()))


# ======================================================================== sources
class SourceService:
    TYPES = ("sql", "file", "rest", "callable", "ldap")

    def __init__(self, project: Project):
        self.p = project

    @property
    def cfg(self):
        return self.p.config

    def list(self) -> list[dict]:
        out = []
        for name, s in self.cfg.sources.items():
            detail = s.url or s.path or s.base_url or s.module or ""
            if s.type == "ldap":
                detail = (", ".join(s.servers) if s.servers else "PowerShell AD module") + f" · {s.base_dn}" \
                    + (f" · test group {s.test_group}" if s.test_group else "")
            if s.type == "sql" and not detail:
                detail = "database (login from credential)"
            out.append({"name": name, "type": s.type, "detail": detail, "credential": s.credential,
                        "read_only": s.read_only if s.type == "sql" else None,
                        "description": s.description,
                        "endpoints": [e.name for e in self.cfg.endpoints if e.source == name]})
        return out

    def get(self, name: str) -> SourceSpec:
        if name not in self.cfg.sources:
            raise ServiceError(f"no source '{name}'")
        return self.cfg.sources[name]

    def add(self, name: str, type: str, *, url: str | None = None, path: str | None = None,
            sheet: str | None = None, base_url: str | None = None, headers: dict | None = None,
            module: str | None = None, read_write: bool = False, allow_host: bool = False,
            credential: str | None = None, allow_credentials: list[str] | None = None,
            description: str = "", replace: bool = False, servers: list[str] | None = None,
            base_dn: str | None = None, directory: str = "ldap", ca_file: str | None = None,
            allow_plain_ldap: bool = False, test_group: str | None = None) -> list[str]:
        """Add (or with replace=True, overwrite) a source. Returns warnings."""
        warnings = []
        if name in self.cfg.sources and not replace:
            raise ServiceError(f"source '{name}' already exists")
        if url and "://" in url and "@" in url and "${" not in url:
            warnings.append("The URL appears to contain inline credentials. Use a credential instead.")
        if type == "file" and path:
            from pathlib import PurePosixPath, PureWindowsPath
            wp = PureWindowsPath(path)
            if (wp.drive or wp.root or PurePosixPath(path).is_absolute() or ".." in wp.parts
                    or any(ch in path for ch in '"<>|?*')):
                raise ServiceError("file path must be just the file name inside the data folder "
                                   f"(e.g. 'transfer.csv'), got {path!r}")
        try:
            spec = SourceSpec(type=type, url=url or None, path=path or None, sheet=sheet or None,
                              base_url=base_url or None, headers=headers or {}, module=module or None,
                              read_only=not read_write, credential=credential or None,
                              credentials=allow_credentials or [], description=description,
                              servers=[x.strip() for x in (servers or []) if x and x.strip()],
                              base_dn=(base_dn or "").strip() or None, directory=directory or "ldap",
                              ca_file=ca_file or None, allow_plain_ldap=allow_plain_ldap,
                              test_group=(test_group or "").strip() or None)
        except Exception as exc:
            raise ServiceError(_clean(exc)) from None
        if credential:
            try:
                known = {c.name for c in self.p.credentials().list()}
                if credential not in known:
                    warnings.append(f"credential '{credential}' does not exist yet")
            except Exception:
                pass
        self.cfg.sources[name] = spec
        sec = self.cfg.security
        if type == "callable" and module:
            root_mod = module.split(".")[0]
            if root_mod not in sec.allowed_callable_modules:
                sec.allowed_callable_modules.append(root_mod)
        if type == "rest" and allow_host and base_url:
            host = urlsplit(base_url).hostname
            if host and host not in sec.allowed_upstream_hosts:
                sec.allowed_upstream_hosts.append(host)
        self.p.save()
        return warnings

    def set_credential(self, name: str, credential: str | None) -> None:
        self.get(name).credential = credential or None
        self.p.save()

    def remove(self, name: str) -> None:
        self.get(name)
        users = [e.name for e in self.cfg.endpoints if e.source == name]
        users += [l.name for l in self.cfg.loads if l.to.source == name or l.from_.source == name]
        if users:
            raise ServiceError(f"'{name}' is still used by {users}; remove those first")
        del self.cfg.sources[name]
        self.p.save()

    def import_file(self, src: str | Path, dest_name: str | None = None) -> str:
        """Copy a file into the project's data folder; returns the name to use as 'path'."""
        src = Path(src)
        if not src.is_file():
            raise ServiceError(f"file not found: {src}")
        data_root = (self.p.root / self.cfg.security.data_root)
        data_root.mkdir(parents=True, exist_ok=True)
        name = dest_name or src.name
        target = data_root / name
        if src.resolve() != target.resolve():
            tmp = target.with_suffix(target.suffix + ".tmp")
            shutil.copy2(src, tmp)
            tmp.replace(target)
        return name

    # ---------------------------------------------------------------- test
    def test(self, name: str, preview_rows: int = 5) -> TestResult:
        return anyio.run(self._test, name, preview_rows)

    async def _test(self, name: str, preview_rows: int) -> TestResult:
        from .. import sources  # noqa: F401  # registers the adapters
        from ..sources.base import create_source
        spec = self.get(name)
        try:
            src = create_source(name, spec, self.cfg.security, str(self.p.root), self.p.credentials())
        except Exception as exc:
            return TestResult(False, _clean(exc))
        try:
            if spec.type == "file":
                rows = await anyio.to_thread.run_sync(src._load)
                cols = list(rows[0].keys()) if rows else []
                return TestResult(True, f"Read {len(rows)} rows, {len(cols)} columns", cols, rows[:preview_rows])
            if spec.type == "callable":
                if spec.module:
                    import importlib, sys
                    root = str(self.p.root)
                    if root not in sys.path:
                        sys.path.insert(0, root)
                    importlib.import_module(spec.module)
                return TestResult(True, f"Module '{spec.module}' imports OK" if spec.module else "No module set")
            if spec.type == "ldap":
                def _t():
                    d = src.directory()
                    try:
                        msg = d.test()
                        if not spec.test_group:
                            return TestResult(True, msg + " (set a test group to list its members)")
                        g = d.resolve_group(spec.test_group)
                        members = d.members(g.dn)
                        rows = member_rows(members)
                        limit = max(preview_rows, 50)
                        return TestResult(True, f"{msg}; group {g.name or g.sam} has {len(members)} member(s)"
                                          + (f", showing {limit}" if len(rows) > limit else ""),
                                          MEMBER_COLUMNS, rows[:limit])
                    finally:
                        d.close()
                return await anyio.to_thread.run_sync(_t)
            await src.startup()
            try:
                if spec.type == "sql":
                    ok = await src.health()
                    return TestResult(ok, "Connected to the database" if ok else "Could not connect to the database")
                src._check_host()
                return TestResult(True, f"Configuration is valid; host {urlsplit(spec.base_url).hostname} is allowed")
            finally:
                await src.shutdown()
        except Exception as exc:
            msg = _clean(exc)
            try:
                msg = self.p.credentials().redact(msg)
            except Exception:
                pass
            return TestResult(False, msg)

    # ---------------------------------------------------------- directory
    def _directory(self, name: str):
        from .. import sources  # noqa: F401
        from ..sources.base import create_source
        spec = self.get(name)
        if spec.type != "ldap":
            raise ServiceError(f"'{name}' is not an ldap (Active Directory) source")
        return create_source(name, spec, self.cfg.security, str(self.p.root), self.p.credentials()).directory()

    def ad_groups(self, name: str, text: str = "", limit: int = 50) -> list[dict]:
        """Find AD groups by name (for pickers)."""
        from ..directory import DirectoryError
        d = self._directory(name)
        try:
            return [{"dn": g.dn, "name": g.name, "sam": g.sam} for g in d.search_groups(text, limit)]
        except DirectoryError as exc:
            raise ServiceError(str(exc)) from None
        finally:
            d.close()

    def set_test_group(self, name: str, group: str | None) -> None:
        """The AD group whose members 'source test' lists (None to unset)."""
        spec = self.get(name)
        if spec.type != "ldap":
            raise ServiceError(f"'{name}' is not an ldap (Active Directory) source")
        spec.test_group = (group or "").strip() or None
        self.p.save()

    def ad_group(self, name: str, group: str, with_members: bool = False) -> dict:
        """One group with its member counts (and, with_members, the member list)."""
        from ..directory import DirectoryError
        d = self._directory(name)
        try:
            g = d.resolve_group(group)
            members = d.members(g.dn)
            kinds: dict[str, int] = {}
            for m in members:
                kinds[m.kind] = kinds.get(m.kind, 0) + 1
            out = {"dn": g.dn, "name": g.name, "sam": g.sam, "members": len(members), "kinds": kinds}
            if with_members:
                out["member_list"] = member_rows(members)
            return out
        except DirectoryError as exc:
            raise ServiceError(str(exc)) from None
        finally:
            d.close()

    def ad_user(self, name: str, email: str, match: list[str] | None = None) -> list[dict]:
        from ..directory import DirectoryError
        d = self._directory(name)
        try:
            found = d.find_users([email.strip().lower()], match or ["mail", "userPrincipalName"])
            return [{"dn": e.dn, "name": e.name, "sam": e.sam, "mail": e.mail, "upn": e.upn,
                     "disabled": e.disabled} for e in found.get(email.strip().lower(), [])]
        except DirectoryError as exc:
            raise ServiceError(str(exc)) from None
        finally:
            d.close()

    # ---------------------------------------------------------- introspect
    def tables(self, name: str) -> list[str]:
        from sqlalchemy import inspect
        return self._with_engine(name, lambda eng: sorted(inspect(eng).get_table_names(
            schema=self._schema(name))))

    def columns(self, name: str, table: str) -> list[dict]:
        from sqlalchemy import inspect
        schema, _, tbl = table.rpartition(".")

        def _cols(eng):
            insp = inspect(eng)
            pk = set(insp.get_pk_constraint(tbl, schema=schema or self._schema(name)).get("constrained_columns") or [])
            return [{"name": c["name"], "type": str(c["type"]), "nullable": c.get("nullable", True),
                     "default": c.get("default"), "primary_key": c["name"] in pk}
                    for c in insp.get_columns(tbl, schema=schema or self._schema(name))]
        return self._with_engine(name, _cols)

    def _schema(self, name: str) -> str | None:
        spec = self.get(name)
        if spec.credential:
            try:
                return self.p.credentials().get(spec.credential).fields.get("schema") or None
            except Exception:
                return None
        return None

    def _with_engine(self, name: str, fn):
        from sqlalchemy import create_engine
        from sqlalchemy.engine import make_url
        from ..config import resolve_secrets
        from ..credentials import build_sql_url
        spec = self.get(name)
        if spec.type != "sql":
            raise ServiceError(f"'{name}' is not a SQL source")
        try:
            url = (build_sql_url(self.p.credentials().get(spec.credential)) if spec.credential
                   else make_url(resolve_secrets(spec.url, self.p.credentials())))
            eng = create_engine(url)
            try:
                return fn(eng)
            finally:
                eng.dispose()
        except ServiceError:
            raise
        except Exception as exc:
            raise ServiceError(self.p.credentials().redact(_clean(exc))) from None


# ====================================================================== endpoints
def _no_ldap(cfg, source: str) -> None:
    spec = cfg.sources.get(source)
    if spec is not None and spec.type == "ldap":
        raise ServiceError(f"'{source}' is an Active Directory source. It is the target of AD group loads "
                           f"(Loads > New load > Write to: AD group), not a source for endpoints; endpoints "
                           f"read from sql, file, rest or callable sources")


class EndpointService:
    def __init__(self, project: Project):
        self.p = project

    @property
    def cfg(self):
        return self.p.config

    def list(self) -> list[dict]:
        base = self.cfg.server.base_path
        return [{"name": e.name, "method": e.method, "path": base + e.path, "source": e.source,
                 "access": "PUBLIC" if e.public else (",".join(e.scopes) or "auth"),
                 "public": e.public, "scopes": e.scopes, "description": e.description}
                for e in self.cfg.endpoints]

    def get(self, name: str) -> EndpointSpec:
        ep = self.cfg.endpoint(name)
        if ep is None:
            raise ServiceError(f"no endpoint '{name}'")
        return ep

    def add(self, name: str, source: str, path: str, *, method: str = "GET", params: list[str] | None = None,
            scopes: list[str] | None = None, public: bool = False, rate_limit: int | None = None,
            description: str = "", query: str | None = None, operation: str | None = None,
            table: str | None = None, key: str | None = None, columns: list[str] | None = None,
            filters: list[str] | None = None, upstream_path: str | None = None,
            upstream_method: str | None = None, target: str | None = None, max_limit: int = 500,
            replace: bool = False) -> tuple[EndpointSpec, list[str]]:
        if self.cfg.endpoint(name) and not replace:
            raise ServiceError(f"endpoint '{name}' already exists")
        _no_ldap(self.cfg, source)
        if public and scopes:
            raise ServiceError("an endpoint is either public or has scopes, not both")
        filters = filters or []
        try:
            plist = [ParamSpec.parse_short(p) for p in params or []]
            for f in filters:          # filters are implicit optional query params
                if not any(p.name == f for p in plist):
                    plist.append(ParamSpec(name=f))
            ep = EndpointSpec(
                name=name, source=source, path=path, method=method.upper(), params=plist,
                scopes=scopes or [], public=public, rate_limit=rate_limit, description=description,
                query=query or None, operation=operation or ("query" if query else None),
                table=table or None, key=key or None, columns=columns or None, filters=filters,
                upstream_path=upstream_path or None,
                upstream_method=upstream_method.upper() if upstream_method else None,
                target=target or None, max_limit=max_limit)
        except Exception as exc:
            raise ServiceError(_clean(exc)) from None
        self.cfg.endpoints = [e for e in self.cfg.endpoints if e.name != name] + [ep]
        self.p.save()
        warnings = [f"'{name}' is PUBLIC – anyone can call it."] if public else []
        if not public and not scopes:
            warnings.append(f"'{name}' has no scope: any valid key can call it")
        return ep, warnings

    def crud(self, resource: str, source: str, table: str, key: str, *, key_type: str = "integer",
             fields: list[str] | None = None, filters: list[str] | None = None,
             read_scope: str | None = None, write_scope: str | None = None,
             read_only: bool = False) -> tuple[list[EndpointSpec], str, str]:
        rs = read_scope or f"{resource}:read"
        ws = write_scope or f"{resource}:write"
        filters = filters or []
        try:
            fspecs = [ParamSpec.parse_short(f) for f in fields or []]
            cols = [key] + [f.name for f in fspecs] + [f for f in filters if f not in {x.name for x in fspecs}]
            columns = list(dict.fromkeys(cols)) if fspecs or filters else None
            kp = ParamSpec(name=key, type=key_type, location="path", required=True)

            def body(req):
                return [f.model_copy(update={"location": "body", "required": req and f.required}) for f in fspecs]
            specs = [
                dict(name=f"{resource}-list", path=f"/{resource}", method="GET", operation="list",
                     params=[ParamSpec(name=f) for f in filters], scopes=[rs]),
                dict(name=f"{resource}-get", path=f"/{resource}/{{{key}}}", method="GET", operation="get",
                     params=[kp], scopes=[rs]),
            ]
            if not read_only:
                specs += [
                    dict(name=f"{resource}-create", path=f"/{resource}", method="POST", operation="create",
                         params=body(True), scopes=[ws]),
                    dict(name=f"{resource}-update", path=f"/{resource}/{{{key}}}", method="PATCH",
                         operation="update", params=[kp] + body(False), scopes=[ws]),
                    dict(name=f"{resource}-delete", path=f"/{resource}/{{{key}}}", method="DELETE",
                         operation="delete", params=[kp], scopes=[ws]),
                ]
            new = [EndpointSpec(source=source, table=table, key=key, columns=columns, tags=[resource], **s)
                   for s in specs]
        except Exception as exc:
            raise ServiceError(_clean(exc)) from None
        clash = [e.name for e in new if self.cfg.endpoint(e.name)]
        if clash:
            raise ServiceError(f"endpoints already exist: {clash}")
        self.cfg.endpoints.extend(new)
        self.p.save()
        return new, rs, ws

    # ---------------------------------------------------- generic edit (Studio)
    def as_dict(self, name: str) -> dict:
        return self.get(name).model_dump(mode="json", by_alias=True, exclude_none=True)

    def build(self, data: dict | EndpointSpec) -> EndpointSpec:
        """Validate a (possibly unsaved) endpoint definition against the project."""
        if isinstance(data, EndpointSpec):
            ep = data
        else:
            try:
                ep = EndpointSpec.model_validate(data)
            except Exception as exc:
                raise ServiceError(_clean(exc)) from None
        if ep.source not in self.cfg.sources:
            raise ServiceError(f"no source '{ep.source}'")
        _no_ldap(self.cfg, ep.source)
        if ep.public and ep.scopes:
            raise ServiceError("an endpoint is either public or has scopes, not both")
        return ep

    def save(self, data: dict | EndpointSpec, original: str | None = None) -> tuple[EndpointSpec, list[str]]:
        """Create (original=None) or replace an endpoint; renaming allowed. Returns (endpoint, warnings)."""
        ep = self.build(data)
        names = [e.name for e in self.cfg.endpoints]
        if original is None and ep.name in names:
            raise ServiceError(f"endpoint '{ep.name}' already exists")
        if original is not None and original not in names:
            raise ServiceError(f"no endpoint '{original}'")
        if original is not None and ep.name != original:
            if ep.name in names:
                raise ServiceError(f"endpoint '{ep.name}' already exists")
            users = [l.name for l in self.cfg.loads if l.from_.endpoint == original]
            if users:
                raise ServiceError(f"cannot rename: load(s) {users} read '{original}'")
        clash = [e.name for e in self.cfg.endpoints if e.name != (original or ep.name)
                 and e.method == ep.method and e.path == ep.path]
        if clash:
            raise ServiceError(f"{ep.method} {ep.path} is already used by endpoint '{clash[0]}'")
        if original is None:
            self.cfg.endpoints.append(ep)
        else:
            self.cfg.endpoints = [ep if e.name == original else e for e in self.cfg.endpoints]
        self.p.save()
        self.p.audit("endpoint_saved", endpoint=ep.name, method=ep.method, path=ep.path)
        return self.get(ep.name), self.warnings(ep)

    def warnings(self, ep: EndpointSpec) -> list[str]:
        out = []
        if ep.public:
            out.append(f"'{ep.name}' is PUBLIC – anyone who can reach the server can call it")
        elif not ep.scopes:
            out.append(f"'{ep.name}' has no scope: any valid API key can call it")
        src = self.cfg.sources.get(ep.source)
        if src and src.type == "sql" and ep.method != "GET" and src.read_only:
            out.append(f"source '{ep.source}' is read-only; this {ep.method} endpoint will be refused")
        return out

    def duplicate(self, name: str, new_name: str) -> EndpointSpec:
        d = self.as_dict(name)
        d["name"] = new_name
        d["path"] = d["path"].rstrip("/") + "-copy"
        return self.save(d)[0]

    @staticmethod
    def binds(sql: str) -> list[str]:
        """:name placeholders in a SQL text, in order (::casts ignored) – the same rule the SQL source uses."""
        import re
        return list(dict.fromkeys(re.findall(r"(?<![:\w]):([a-zA-Z_][a-zA-Z0-9_]*)", sql or "")))

    def crud_from_table(self, resource: str, source: str, table: str, key: str, columns: list[dict],
                        exposed: list[str], filters: list[str], operations: list[str],
                        read_scope: str | None = None, write_scope: str | None = None,
                        public_read: bool = False) -> list[EndpointSpec]:
        """Studio CRUD wizard: endpoints for the chosen operations, typed from the reflected columns."""
        types = {c["name"]: _param_type(c["type"]) for c in columns}
        required = {c["name"] for c in columns if not c.get("nullable", True) and c.get("default") is None
                    and not c.get("primary_key")}
        rs = read_scope or f"{resource}:read"
        ws = write_scope or f"{resource}:write"
        cols = list(dict.fromkeys([key] + list(exposed)))
        writable = [c for c in cols if c != key]
        kp = {"name": key, "type": types.get(key, "string"), "in": "path", "required": True}

        def body(req: bool):
            return [{"name": c, "type": types.get(c, "string"), "in": "body", "required": req and c in required}
                    for c in writable]
        read = {"public": True} if public_read else {"scopes": [rs]}
        all_specs = {
            "list": dict(name=f"{resource}-list", path=f"/{resource}", method="GET", operation="list",
                         params=[{"name": f, "type": types.get(f, "string")} for f in filters], **read),
            "get": dict(name=f"{resource}-get", path=f"/{resource}/{{{key}}}", method="GET", operation="get",
                        params=[kp], **read),
            "create": dict(name=f"{resource}-create", path=f"/{resource}", method="POST", operation="create",
                           params=body(True), scopes=[ws]),
            "update": dict(name=f"{resource}-update", path=f"/{resource}/{{{key}}}", method="PATCH",
                           operation="update", params=[kp] + body(False), scopes=[ws]),
            "delete": dict(name=f"{resource}-delete", path=f"/{resource}/{{{key}}}", method="DELETE",
                           operation="delete", params=[kp], scopes=[ws]),
        }
        if not operations:
            raise ServiceError("choose at least one operation")
        try:
            new = [EndpointSpec.model_validate({**all_specs[o], "source": source, "table": table, "key": key,
                                                "columns": cols, "tags": [resource]}) for o in operations]
        except Exception as exc:
            raise ServiceError(_clean(exc)) from None
        clash = [e.name for e in new if self.cfg.endpoint(e.name)]
        if clash:
            raise ServiceError(f"endpoints already exist: {clash}")
        self.cfg.endpoints.extend(new)
        self.p.save()
        self.p.audit("endpoint_saved", endpoint=",".join(e.name for e in new), table=table)
        return new

    def set_access(self, name: str, *, public: bool = False, scopes: list[str] | None = None) -> None:
        ep = self.get(name)
        if public and scopes:
            raise ServiceError("an endpoint is either public or has scopes, not both")
        ep.public, ep.scopes = public, ([] if public else list(scopes or []))
        self.p.save()

    def remove(self, name: str) -> None:
        self.get(name)
        users = [l.name for l in self.cfg.loads if l.from_.endpoint == name]
        if users:
            raise ServiceError(f"endpoint '{name}' is read by load(s) {users}; remove those first")
        self.cfg.endpoints = [e for e in self.cfg.endpoints if e.name != name]
        self.p.save()


def _param_type(db_type: str) -> str:
    t = (db_type or "").upper()
    if any(x in t for x in ("INT", "SERIAL")) or (t.startswith("NUMBER(") and t.endswith(",0)")):
        return "integer"
    if any(x in t for x in ("NUMERIC", "DECIMAL", "FLOAT", "DOUBLE", "REAL", "NUMBER")):
        return "number"
    if "BOOL" in t:
        return "boolean"
    return "string"
