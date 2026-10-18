"""View-models: everything the Studio screens need that is not drawing.

Kept free of Flet so it can be unit-tested; the views only render these and
pass user input back. All changes go through ``portwright.services``.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..credentials import TYPES
from ..services import Services, ServiceError

DB_DRIVERS = ["oracle+oracledb", "postgresql+psycopg", "mysql+pymysql", "mssql+pyodbc", "sqlite"]
HTTP_CRED_TYPES = {"basic", "bearer", "api_key", "oauth2"}

_FIELD_HELP = {
    "driver": "Database type and driver",
    "host": "Server name or IP",
    "port": "e.g. 1521 (Oracle), 5432 (PostgreSQL), 1433 (SQL Server)",
    "database": "Database name (SQLite: file path, e.g. data/hr.db)",
    "service_name": "Oracle service name, e.g. ORCLPDB1",
    "sid": "Oracle SID (older databases, instead of service name)",
    "dsn": "Oracle TNS alias (instead of host/port)",
    "schema": "Default schema for tables, e.g. hr",
    "options": "Extra driver options: key=value;key2=value2",
    "username": "Login name",
    "password": "Password",
    "token": "Bearer token",
    "key": "API key value",
    "header": "Header name (default X-API-Key)",
    "query_param": "Send the key as this query parameter instead of a header",
    "token_url": "OAuth token URL (https)",
    "client_id": "OAuth client id",
    "client_secret": "OAuth client secret",
    "scope": "OAuth scopes (space separated)",
    "audience": "OAuth audience",
}
_FIELD_ORDER = ["driver", "host", "port", "service_name", "sid", "dsn", "database", "schema", "username",
                "password", "token", "key", "header", "query_param", "token_url", "client_id",
                "client_secret", "scope", "audience", "options"]


_LABELS = {"sid": "SID", "dsn": "TNS alias (DSN)", "service_name": "Service name", "token_url": "Token URL",
           "client_id": "Client ID", "query_param": "Query parameter", "options": "Driver options"}

# which database fields make sense for each driver family
_DB_FIELDS = {
    "sqlite": ["driver", "database"],
    "oracle": ["driver", "host", "port", "service_name", "sid", "dsn", "schema", "username", "password", "options"],
    "other": ["driver", "host", "port", "database", "schema", "username", "password", "options"],
}


def db_family(driver: str | None) -> str:
    d = (driver or "").lower()
    return "sqlite" if d.startswith("sqlite") else "oracle" if d.startswith("oracle") else "other"


@dataclass
class FieldDef:
    name: str
    label: str
    required: bool = False
    secret: bool = False
    hint: str = ""
    options: list[str] = field(default_factory=list)


# --------------------------------------------------------------- credentials
def credential_fields(ctype: str, driver: str | None = None) -> list[FieldDef]:
    """Fields for a credential type; for databases only those that fit the chosen driver."""
    t = TYPES[ctype]
    names = sorted(t.required | t.optional, key=lambda n: _FIELD_ORDER.index(n) if n in _FIELD_ORDER else 99)
    if ctype == "database" and driver:
        allowed = _DB_FIELDS[db_family(driver)]
        names = [n for n in names if n in allowed]
    return [FieldDef(n, _LABELS.get(n, n.replace("_", " ").capitalize()), n in t.required, n in t.secret,
                     _FIELD_HELP.get(n, ""), DB_DRIVERS if n == "driver" else []) for n in names]


def credential_errors(ctype: str, values: dict[str, str]) -> dict[str, str]:
    """Field -> error message, for required fields left blank."""
    errs = {}
    filled = {k for k, v in values.items() if str(v or "").strip()}
    for f in credential_fields(ctype):
        if f.required and f.name not in filled:
            errs[f.name] = "required"
    if ctype == "database" and not str(values.get("driver", "")).startswith("sqlite"):
        if not ({"host", "dsn"} & filled):
            errs["host"] = "host or dsn is required"
    return errs


def parse_kv_lines(text: str) -> dict[str, str]:
    out = {}
    for line in (text or "").splitlines():
        if line.strip():
            if "=" not in line:
                raise ServiceError(f"expected KEY=VALUE, got '{line.strip()}'")
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


# ------------------------------------------------------------------- sources
SOURCE_TYPES = {
    "sql": "SQL database (Oracle, PostgreSQL, SQL Server, MySQL, SQLite)",
    "file": "File in the data folder (CSV, JSON, Excel)",
    "rest": "Upstream REST API",
    "callable": "Python function in the handlers folder",
    "ldap": "Active Directory (target for group loads)",
}


def source_values(svc: Services, name: str) -> dict[str, Any]:
    """Current settings of a source as form values (for editing)."""
    s = svc.sources.get(name)
    host_allowed = False
    if s.base_url:
        from urllib.parse import urlsplit
        host_allowed = (urlsplit(s.base_url).hostname or "") in svc.project.config.security.allowed_upstream_hosts
    return {"name": name, "type": s.type, "credential": s.credential or "", "read_write": not s.read_only,
            "path": s.path or "", "sheet": s.sheet or "", "base_url": s.base_url or "",
            "headers": "\n".join(f"{k}={v}" for k, v in s.headers.items()), "allow_host": host_allowed,
            "module": s.module or "", "allow_credentials": ", ".join(s.credentials),
            "description": s.description, "url": s.url or "",
            "servers": "\n".join(s.servers), "base_dn": s.base_dn or "", "directory": s.directory,
            "ca_file": s.ca_file or "", "allow_plain_ldap": s.allow_plain_ldap}


def save_source(svc: Services, v: dict[str, Any], editing: bool = False) -> list[str]:
    name = (v.get("name") or "").strip()
    if not name:
        raise ServiceError("give the source a name")
    t = v.get("type")
    if t not in SOURCE_TYPES:
        raise ServiceError("choose a source type")
    kw: dict[str, Any] = {"description": v.get("description", ""), "replace": editing}
    if t == "sql":
        if not v.get("credential") and not v.get("url"):
            raise ServiceError("choose the database credential")
        kw.update(credential=v.get("credential") or None, url=v.get("url") or None,
                  read_write=bool(v.get("read_write")))
    elif t == "file":
        kw.update(path=(v.get("path") or "").strip(), sheet=v.get("sheet") or None)
    elif t == "rest":
        kw.update(base_url=(v.get("base_url") or "").strip(), allow_host=bool(v.get("allow_host")),
                  credential=v.get("credential") or None, headers=parse_kv_lines(v.get("headers", "")))
    elif t == "ldap":
        servers = [x.strip() for x in str(v.get("servers") or "").replace(",", "\n").splitlines() if x.strip()]
        method = v.get("directory") or "ldap"
        if method == "ldap" and not v.get("credential"):
            raise ServiceError("choose the service-account credential (type basic)")
        kw.update(servers=servers, base_dn=(v.get("base_dn") or "").strip(), directory=method,
                  credential=v.get("credential") or None, ca_file=(v.get("ca_file") or "").strip() or None,
                  allow_plain_ldap=bool(v.get("allow_plain_ldap")))
    else:
        allowed = [c.strip() for c in (v.get("allow_credentials") or "").split(",") if c.strip()]
        kw.update(module=(v.get("module") or "").strip() or None, allow_credentials=allowed)
    return svc.sources.add(name, t, **kw)


# ----------------------------------------------------------------- dashboard
def dashboard(svc: Services) -> dict[str, Any]:
    cfg = svc.project.config
    try:
        creds = svc.credentials.list()
        cred_error = None
    except ServiceError as exc:
        creds, cred_error = [], str(exc)
    keys = svc.access.keys()
    runs = []
    hist = svc.project.root / "logs" / "loads" / "history.jsonl"
    if hist.exists():
        for line in hist.read_text(encoding="utf-8").splitlines()[-5:]:
            try:
                runs.append(json.loads(line))
            except ValueError:
                pass
    return {
        "project": cfg.project, "path": str(svc.project.path),
        "sources": len(cfg.sources), "endpoints": len(cfg.endpoints),
        "public_endpoints": [e.name for e in cfg.endpoints if e.public],
        "credentials": len(creds), "cred_error": cred_error,
        "expiring": [c for c in creds if c["expiry_state"] in ("soon", "expired")],
        "keys_active": sum(1 for k in keys if k["status"] == "active"),
        "keys_expiring": [k for k in keys if k["status"] == "active" and k["expiry_state"] == "soon"],
        "loads": len(cfg.loads), "runs": list(reversed(runs)),
        "vault": svc.credentials.vault_status(),
    }


# ------------------------------------------------------------ recent projects
class Recent:
    def __init__(self, path: Path | None = None):
        self.path = path or Path.home() / ".portwright" / "studio.json"
        old = Path.home() / ".restforge" / "studio.json"          # recent projects from before the rename
        if path is None and not self.path.exists() and old.exists():
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(old.read_text(encoding="utf-8"), encoding="utf-8")
            except OSError:
                pass

    def list(self) -> list[str]:
        try:
            items = json.loads(self.path.read_text(encoding="utf-8")).get("recent", [])
        except Exception:
            return []
        return [p for p in items if Path(p).exists()]

    def add(self, config_path: str | Path) -> None:
        p = str(Path(config_path).resolve())
        items = [x for x in self.list() if x != p]
        items.insert(0, p)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({"recent": items[:8]}, indent=2), encoding="utf-8")
        except OSError:
            pass


EXPIRY_CHOICES = {"30 days": 30, "90 days": 90, "180 days": 180, "1 year": 365, "Never": 0}


# ================================================================ load mapping
MAP_TYPES = ["string", "integer", "number", "boolean", "date", "timestamp"]
LOAD_MODES = {"insert": "Append – insert new rows",
              "replace": "Kill & fill – delete all rows, then insert",
              "upsert": "Upsert – update matching keys, insert the rest"}
DUPLICATE_KEYS = {"reject": "Reject all copies", "first": "Keep the first", "last": "Keep the last"}
EXISTING_KEYS = {"reject": "Reject the row", "skip": "Skip the row", "fail": "Fail the load"}
ON_UNCHANGED = {"skip": "Skip the run (exit 3)", "load": "Load anyway", "fail": "Fail the run"}
ON_HEADER_CHANGE = {"fail": "Fail the run", "warn": "Warn and load"}
SIMPLE_TRANSFORMS = ["trim", "upper", "lower", "null_if_empty"]
NOT_MAPPED, CONSTANT = "(not mapped)", "(constant value)"
DATE_FORMATS = ["%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%d-%b-%Y", "%Y%m%d"]
TS_FORMATS = ["%Y-%m-%d %H:%M:%S", "%m/%d/%Y %H:%M", "%m/%d/%Y %I:%M %p", "%d/%m/%Y %H:%M:%S"]


@dataclass
class MapRow:
    """One line of the mapping grid: a target column and where its value comes from."""
    target: str
    db_type: str = ""
    nullable: bool = True
    has_default: bool = False
    primary_key: bool = False
    in_table: bool = True               # False: mapped, but the table has no such column
    suggested_type: str = "string"
    source: str | None = None
    value: Any = None
    is_constant: bool = False
    type: str = "string"
    format: str | None = None
    transform: list = field(default_factory=list)
    default: Any = None
    required: bool = False
    max_length: int | None = None
    key: bool = False
    review: bool = False

    @property
    def mapped(self) -> bool:
        return self.source is not None or (self.is_constant and self.value not in (None, ""))

    @property
    def needs_value(self) -> bool:
        return self.in_table and not self.nullable and not self.has_default

    def status(self, header: list[str] | None = None) -> tuple[str, str]:
        """(kind, message) for the status column: bad / warn / good / neutral."""
        if not self.in_table:
            return "bad", "column does not exist in the table"
        if not self.mapped:
            if self.needs_value and self.primary_key and self.suggested_type == "integer":
                return "neutral", "not loaded – assumed to be generated by the database"
            if self.needs_value:
                return "bad", "required (NOT NULL) – map a source column or a constant"
            return "neutral", "not loaded"
        if header is not None and self.source is not None and self.source not in header:
            return "bad", f"'{self.source}' is not in the source"
        if self.review:
            return "warn", "auto-mapped guess – check it, then tick Confirm"
        if self.type in ("date", "timestamp") and self.source is not None and not self.format:
            return "warn", "no format: only ISO dates (2026-01-31) will parse"
        if self.suggested_type != "string" and self.type == "string" and not self.is_constant:
            return "warn", f"column is {self.db_type}; type {self.suggested_type} is safer"
        return "good", "ok"

    def to_column(self) -> dict:
        d: dict[str, Any] = {"target": self.target, "type": self.type}
        if self.is_constant:
            d["value"] = self.value
        else:
            d["source"] = self.source
        for k in ("format", "default", "max_length"):
            v = getattr(self, k)
            if v not in (None, ""):
                d[k] = v
        if self.transform:
            d["transform"] = list(self.transform)
        if self.required:
            d["required"] = True
        if self.review:
            d["review"] = True
        return d


def mapping_rows(targets: list, columns: list[dict], keys: list[str] | None = None) -> list[MapRow]:
    """Grid rows: every table column (in table order), plus mapped targets the table lacks."""
    by_target = {c["target"].lower(): c for c in columns}
    keyset = {k.lower() for k in keys or []}
    rows: list[MapRow] = []
    for t in targets:
        r = MapRow(target=t.name, db_type=t.db_type, nullable=t.nullable, has_default=t.has_default,
                   primary_key=t.primary_key, suggested_type=t.type, type=t.type,
                   key=t.name.lower() in keyset)
        c = by_target.pop(t.name.lower(), None)
        if c:
            _fill(r, c)
        rows.append(r)
    for c in by_target.values():      # mapped but not (or no longer) in the table
        r = MapRow(target=c["target"], in_table=not targets, key=c["target"].lower() in keyset)
        _fill(r, c)
        r.suggested_type = r.type
        rows.append(r)
    return rows


def _fill(r: MapRow, c: dict) -> None:
    r.source = c.get("source")
    r.is_constant = c.get("source") is None and c.get("value") is not None
    r.value = c.get("value")
    r.type = c.get("type", "string")
    r.format = c.get("format")
    r.transform = list(c.get("transform") or [])
    r.default = c.get("default")
    r.required = bool(c.get("required"))
    r.max_length = c.get("max_length")
    r.review = bool(c.get("review"))


def rows_to_columns(rows: list[MapRow]) -> list[dict]:
    return [r.to_column() for r in rows if r.mapped]


def apply_suggestion(rows: list[MapRow], suggested: list[dict], overwrite: bool = False) -> int:
    """Fill grid rows from an auto-map suggestion; mapped rows are kept unless overwrite. Returns count."""
    by_target = {r.target.lower(): r for r in rows}
    n = 0
    for c in suggested:
        r = by_target.get(c["target"].lower())
        if r is None or (r.mapped and not overwrite):
            continue
        r.source, r.is_constant, r.value = c["source"], False, None
        r.type = c.get("type", r.suggested_type)
        r.review = bool(c.get("review"))
        n += 1
    return n


def unused_sources(header: list[str], rows: list[MapRow]) -> list[str]:
    used = {r.source for r in rows if r.mapped and not r.is_constant}
    return [h for h in header if h not in used]


def transform_summary(steps: list) -> str:
    out = []
    for s in steps:
        if isinstance(s, str):
            out.append(s)
        elif isinstance(s, dict) and "replace" in s:
            a, b = s["replace"]
            out.append(f"replace '{a}' with '{b}'")
        elif isinstance(s, dict) and "lookup" in s:
            out.append(f"lookup ({len([k for k in s['lookup'] if k != '_default'])} codes)")
    return ", ".join(out)


def lookup_to_text(mapping: dict) -> tuple[str, str]:
    """(lines 'code=value', default)"""
    lines = "\n".join(f"{k}={v}" for k, v in mapping.items() if k != "_default")
    return lines, ("" if mapping.get("_default") is None else str(mapping["_default"]))


def build_transforms(simple: list[str], replace_from: str, replace_to: str, lookup_text: str,
                     lookup_default: str, use_lookup_default: bool = False) -> list:
    """Compose the transform list in a fixed order: trim/upper/lower/null_if_empty, replace, lookup."""
    steps: list[Any] = [s for s in SIMPLE_TRANSFORMS if s in simple]   # trim, case, then blank -> NULL
    if replace_from:
        steps.append({"replace": [replace_from, replace_to or ""]})
    if lookup_text.strip():
        table = parse_kv_lines(lookup_text)
        if use_lookup_default:
            table["_default"] = lookup_default
        steps.append({"lookup": table})
    return steps


def split_transforms(steps: list) -> dict:
    out = {"simple": [], "replace_from": "", "replace_to": "", "lookup": "", "lookup_default": "",
           "use_default": False}
    for s in steps:
        if isinstance(s, str):
            out["simple"].append(s)
        elif isinstance(s, dict) and "replace" in s:
            out["replace_from"], out["replace_to"] = (str(x) for x in s["replace"])
        elif isinstance(s, dict) and "lookup" in s:
            out["lookup"], out["lookup_default"] = lookup_to_text(s["lookup"])
            out["use_default"] = "_default" in s["lookup"]
    return out


def load_draft(name: str, description: str, from_kind: str, from_name: str | None, to: str | None,
               table: str, mode: str, rows: list[MapRow], batch_size: Any, max_rejects: Any,
               unmapped: str, params: dict | None = None, settings: dict | None = None) -> dict:
    """The editor's state as a load definition (validated by LoadService.build)."""
    def _int(v, label):
        try:
            return int(str(v).strip())
        except ValueError:
            raise ServiceError(f"{label} must be a whole number") from None
    def _num(v, label, kind=int, allow_empty=False):
        t = str(v if v is not None else "").strip()
        if t == "" and allow_empty:
            return None
        try:
            return kind(t)
        except ValueError:
            raise ServiceError(f"{label} must be a number") from None
    st = dict(settings or {})
    extra: dict[str, Any] = {}
    for k in ("duplicate_keys", "existing_keys"):
        if k in st:
            extra[k] = st[k]
    if "tracking" in st:
        t = dict(st["tracking"])
        t["stable_seconds"] = _num(t.get("stable_seconds", 0), "Wait until unchanged") or 0
        t["archive_keep"] = _num(t.get("archive_keep", 30), "Versions to keep")
        t["max_age_hours"] = _num(t.get("max_age_hours"), "Max file age", float, allow_empty=True) or None
        extra["tracking"] = t
    if "safety" in st:
        sf = st["safety"]
        extra["safety"] = {"min_rows": _num(sf.get("min_rows", 0), "Minimum rows") or 0,
                           "min_percent": _num(sf.get("min_percent", 0), "Minimum %", float) or 0}
    return {
        **extra,
        "name": (name or "").strip(), "description": description or "",
        "from": {from_kind: from_name, **({"params": params} if params else {})},
        "to": {"source": to, "table": (table or "").strip()}, "mode": mode,
        "keys": [r.target for r in rows if r.key and r.mapped],
        "batch_size": _int(batch_size, "Batch size"), "max_rejects": _int(max_rejects, "Max rejects"),
        "unmapped_source_columns": unmapped, "columns": rows_to_columns(rows),
    }


GROUP_MODES = {"replace": "Kill & fill – the group gets exactly the source's users (one AD change)",
               "sync": "Sync – add the missing users, remove the ones no longer in the source",
               "insert": "Add only – add the missing users, never remove anybody"}
MATCH_CHOICES = {"mail,userPrincipalName": "mail, then userPrincipalName",
                 "mail": "mail only",
                 "mail,userPrincipalName,proxyAddresses": "mail, UPN, then proxyAddresses (aliases)"}


def group_settings(email_column: str, match: str, max_remove: Any, protected: str, keep_nested: bool,
                   exception_group: str, reject_disabled: bool) -> dict:
    """AD group load settings from the editor's fields."""
    try:
        mr = float(str(max_remove).strip() or "20")
    except ValueError:
        raise ServiceError("Max % removed must be a number") from None
    return {"email_column": (email_column or "").strip() or "email",
            "match": [m for m in (match or "mail,userPrincipalName").split(",") if m],
            "max_remove_percent": mr,
            "protected": [p.strip() for p in str(protected or "").replace(";", "\n").splitlines() if p.strip()],
            "keep_nested_groups": bool(keep_nested),
            "exception_group": (exception_group or "").strip() or None,
            "reject_disabled": bool(reject_disabled)}


RUN_STATUS_KIND = {"success": "good", "partial": "warn", "failed": "bad", "dry_run": "info", "skipped": "info"}


def report_counts(rep: dict) -> list[tuple[str, int]]:
    if rep.get("group"):
        dry = rep.get("status") == "dry_run"
        return [("read", rep.get("read", 0)), ("users found", rep.get("mapped", 0)), ("rejected", rep.get("rejected", 0)),
                ("would add" if dry else "added", rep.get("added", 0)),
                ("would remove" if dry else "removed", rep.get("removed", 0)), ("kept", rep.get("kept", 0))]
    keys = ["read", "mapped", "rejected", "dropped", "skipped", "inserted", "updated", "upserted", "deleted"]
    labels = {"dropped": "duplicates dropped", "skipped": "already in table"}
    return [(labels.get(k, k), rep.get(k, 0)) for k in keys if k in ("read", "mapped", "rejected") or rep.get(k)]


def source_state(rep_or_status: dict) -> tuple[str, str]:
    """(tag kind, text) for 'has the source changed since the last load'."""
    ch = rep_or_status.get("changed", rep_or_status.get("source_changed"))
    last = rep_or_status.get("last_loaded_version")
    if ch is None:
        return "info", "first load" if "source_changed" in rep_or_status else "never loaded"
    if ch:
        return "warn", f"changed since v{last}" if last else "changed"
    return "neutral", f"unchanged (v{last})" if last else "unchanged"


def cell(v: Any, width: int = 40) -> str:
    s = "∅" if v is None else str(v)
    return s if len(s) <= width else s[: width - 1] + "…"


# ================================================================== endpoints
PARAM_TYPES = ["string", "integer", "number", "boolean"]
PARAM_IN = {"query": "query (?name=)", "path": "path ({name})", "body": "body (JSON)"}
HTTP_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE"]
METHOD_KIND = {"GET": "info", "POST": "good", "PATCH": "warn", "PUT": "warn", "DELETE": "bad"}
SQL_OPS = {"list": "list rows (filters, paging)", "get": "get one row by key", "create": "create a row",
           "update": "update a row by key", "delete": "delete a row by key"}
OP_METHOD = {"list": "GET", "get": "GET", "create": "POST", "update": "PATCH", "delete": "DELETE"}


@dataclass
class ParamRow:
    name: str = ""
    type: str = "string"
    location: str = "query"
    required: bool = False
    default: str = ""
    min: str = ""
    max: str = ""
    max_length: str = "1024"
    enum: str = ""                 # comma separated
    pattern: str = ""
    description: str = ""

    @classmethod
    def from_spec(cls, p: dict) -> "ParamRow":
        def s(v):
            return "" if v is None else (str(int(v)) if isinstance(v, float) and v.is_integer() else str(v))
        return cls(name=p["name"], type=p.get("type", "string"), location=p.get("in", p.get("location", "query")),
                   required=bool(p.get("required")), default=s(p.get("default")), min=s(p.get("min")),
                   max=s(p.get("max")), max_length=s(p.get("max_length", 1024)),
                   enum=", ".join(str(x) for x in (p.get("enum") or [])), pattern=p.get("pattern") or "",
                   description=p.get("description", ""))

    def to_spec(self) -> dict:
        name = self.name.strip()
        if not name:
            raise ServiceError("every parameter needs a name")

        def num(v, label):
            v = (v or "").strip()
            if not v:
                return None
            try:
                return float(v)
            except ValueError:
                raise ServiceError(f"parameter '{name}': {label} must be a number") from None

        def conv(v):
            if self.type == "integer":
                return int(float(v))
            if self.type == "number":
                return float(v)
            if self.type == "boolean":
                return str(v).strip().lower() in ("true", "1", "yes", "y")
            return v
        d: dict[str, Any] = {"name": name, "type": self.type, "in": self.location}
        if self.required or self.location == "path":
            d["required"] = True
        try:
            if self.default.strip():
                d["default"] = conv(self.default.strip())
            if self.enum.strip():
                d["enum"] = [conv(x.strip()) for x in self.enum.split(",") if x.strip()]
        except ValueError:
            raise ServiceError(f"parameter '{name}': default/allowed values must be {self.type}") from None
        for k, label in (("min", "min"), ("max", "max")):
            v = num(getattr(self, k), label)
            if v is not None:
                d[k] = v
        ml = (self.max_length or "").strip()
        if ml:
            if not ml.isdigit():
                raise ServiceError(f"parameter '{name}': max length must be a whole number")
            d["max_length"] = int(ml)
        if self.pattern.strip():
            d["pattern"] = self.pattern.strip()
        if self.description.strip():
            d["description"] = self.description.strip()
        return d


def path_params(path: str) -> list[str]:
    import re
    return re.findall(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", path or "")


def sync_path_params(path: str, rows: list[ParamRow]) -> list[ParamRow]:
    """Add a required path parameter for every {name} in the path; turn stale path params into query."""
    names = path_params(path)
    by = {r.name: r for r in rows}
    for n in names:
        if n in by:
            by[n].location, by[n].required = "path", True
        else:
            rows.append(ParamRow(name=n, location="path", required=True))
    for r in rows:
        if r.location == "path" and r.name not in names:
            r.location = "query"
    return rows


def endpoint_draft(base: dict, rows: list[ParamRow], *, public: bool, scopes: list[str], rate_limit: str,
                   max_limit: str) -> dict:
    """The editor's state as an endpoint definition (validated by EndpointService.build)."""
    d = {k: v for k, v in base.items() if v not in (None, "", [])}
    d["name"] = (base.get("name") or "").strip()
    d["params"] = [r.to_spec() for r in rows]
    names = [p["name"] for p in d["params"]]
    dup = {n for n in names if names.count(n) > 1}
    if dup:
        raise ServiceError(f"parameter names used twice: {sorted(dup)}")
    d["public"] = bool(public)
    d["scopes"] = [] if public else [s.strip() for s in scopes if s.strip()]
    rl = (rate_limit or "").strip()
    if rl:
        if not rl.isdigit():
            raise ServiceError("rate limit must be a whole number (requests per window)")
        d["rate_limit"] = int(rl)
    ml = (max_limit or "500").strip()
    if not ml.isdigit() or int(ml) < 1:
        raise ServiceError("max page size must be a whole number")
    d["max_limit"] = int(ml)
    return d


def endpoint_cli(d: dict) -> str:
    """The 'portwright endpoint add' command that creates the same endpoint."""
    def q(v: str) -> str:
        v = str(v)
        return f'"{v}"' if any(c in v for c in ' {}$;|&') else v
    parts = ["portwright endpoint add", d["name"], "-s", d["source"], "-p", q(d["path"])]
    if d.get("method", "GET") != "GET":
        parts += ["-m", d["method"]]
    for p in d.get("params", []):
        if p["name"] in (d.get("filters") or []) and p.get("in", "query") == "query" and not p.get("required"):
            continue
        spec = f"{p['name']}:{p.get('type', 'string')}:{p.get('in', 'query')}" + (":required" if p.get("required") else "")
        parts += ["--param", spec]
    for f in d.get("filters") or []:
        parts += ["--filter", q(f)]
    if d.get("query"):
        parts += ["--query", '"' + " ".join(d["query"].split()).replace('"', '\\"') + '"']
    for k, flag in (("operation", "--operation"), ("table", "--table"), ("key", "--key"),
                    ("upstream_path", "--upstream-path"), ("upstream_method", "--upstream-method"),
                    ("target", "--target")):
        if d.get(k) and not (k == "operation" and d.get("query")):
            parts += [flag, q(d[k])]
    if d.get("columns"):
        parts += ["--columns", q(",".join(d["columns"]))]
    if d.get("public"):
        parts.append("--public")
    for s in d.get("scopes", []):
        parts += ["--scope", s]
    if d.get("rate_limit"):
        parts += ["--rate-limit", str(d["rate_limit"])]
    if d.get("max_limit", 500) != 500:
        parts += ["--max-limit", str(d["max_limit"])]
    if d.get("description"):
        parts += ["--description", q(d["description"])]
    return " ".join(parts)


def keys_covering(svc: Services, scopes: list[str], include_admin: bool = False) -> list[str]:
    """Names of active API keys that can call an endpoint needing these scopes (admin '*' keys only
    when include_admin – applications should get their own narrow key)."""
    out = []
    for k in svc.access.keys():
        if k["status"] != "active":
            continue
        if (include_admin and "*" in k["scopes"]) or not scopes or all(s in k["scopes"] for s in scopes):
            out.append(k["name"])
    return out


def result_table(data: Any, limit: int = 50) -> tuple[list[str], list[dict]] | None:
    """(columns, rows) when a result is tabular: {"items": [...]}, a list of dicts, or one dict."""
    rows = data.get("items") if isinstance(data, dict) and isinstance(data.get("items"), list) else \
        (data if isinstance(data, list) else None)
    if rows is None:
        return None
    rows = [r for r in rows if isinstance(r, dict)][:limit]
    cols: list[str] = []
    for r in rows:
        cols += [k for k in r if k not in cols]
    return cols, rows


# ========================================================= getting started
def _state_path(svc: Services) -> Path:
    return svc.project.root / ".portwright" / "studio-state.json"


def project_state(svc: Services) -> dict:
    try:
        return json.loads(_state_path(svc).read_text(encoding="utf-8"))
    except Exception:
        return {}


def set_project_state(svc: Services, **changes) -> None:
    st = project_state(svc)
    st.update(changes)
    try:
        p = _state_path(svc)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(st, indent=2), encoding="utf-8")
    except OSError:
        pass


@dataclass
class SetupStep:
    key: str
    title: str
    done: bool
    screen: str
    hint: str


def setup_steps(svc: Services) -> list[SetupStep]:
    cfg = svc.project.config
    vault = svc.credentials.vault_status()
    try:
        creds = svc.credentials.list()
    except ServiceError:
        creds = []
    keys = [k for k in svc.access.keys() if k["status"] == "active"]
    st = project_state(svc)
    protected = [e for e in cfg.endpoints if not e.public]
    hist = svc.project.root / "logs" / "loads" / "history.jsonl"
    return [
        SetupStep("vault", "Create the vault master key", bool(vault["key_present"]), "credentials",
                  "Encrypts every credential. Created with the project; back up the .env file."),
        SetupStep("credential", "Add a credential (database login)", bool(creds), "credentials",
                  "The login Portwright uses for your database or API."),
        SetupStep("source", "Add a source", bool(cfg.sources), "sources",
                  "A database, a file in the data folder, an upstream API or Python functions."),
        SetupStep("endpoint", "Create an endpoint", bool(cfg.endpoints), "endpoints",
                  "A URL that returns or changes data from a source."),
        SetupStep("key", "Create an API key", bool(keys) or (bool(cfg.endpoints) and not protected), "access",
                  "Applications need a key with the endpoint's scope (not needed for public endpoints)."),
        SetupStep("try", "Try an endpoint", bool(st.get("tried")), "endpoints",
                  "Endpoints > Try it – runs the endpoint without a server."),
        SetupStep("serve", "Serve the API or set up a load", bool(cfg.loads) or bool(st.get("served")) or
                  hist.exists(), "help",
                  "Start 'portwright serve' (Help > Running the server) or copy data into a table on Loads."),
    ]
