"""Declarative project specification (the single source of truth).

Everything the server exposes is described in ``portwright.yaml``. The CLI edits
this file; the server reads it. Secrets are never written here in plaintext:
use ``${ENV:VAR_NAME}`` references, which are resolved at runtime only.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

DEFAULT_CONFIG = "portwright.yaml"      # an existing restforge.yaml is used when this is missing
_ENV_REF = re.compile(r"\$\{ENV:([A-Za-z_][A-Za-z0-9_]*)\}")
_CRED_REF = re.compile(r"\$\{CRED:([a-zA-Z][a-zA-Z0-9_\-]{0,63})\.([a-zA-Z_][a-zA-Z0-9_]{0,63})\}")
_NAME = r"^[a-zA-Z][a-zA-Z0-9_\-]{0,63}$"


class ConfigError(Exception):
    """Raised when the specification is invalid or a secret cannot be resolved."""


def resolve_secrets(value: Any, credentials: Any = None) -> Any:
    """Resolve secret references recursively.

    * ``${ENV:VAR}``          – environment variable
    * ``${CRED:name.field}``  – field of a managed credential (needs a CredentialManager)
    """
    if isinstance(value, str):
        def _env(m: re.Match) -> str:
            name = m.group(1)
            from .compat import getenv
            value = getenv(name)              # PORTWRIGHT_X falls back to the old RESTFORGE_X
            if value is None:
                raise ConfigError(f"Environment variable '{name}' is not set")
            return value

        def _cred(m: re.Match) -> str:
            if credentials is None:
                raise ConfigError("${CRED:...} reference used but no credential manager is configured")
            return str(credentials.field(m.group(1), m.group(2)))

        return _CRED_REF.sub(_cred, _ENV_REF.sub(_env, value))
    if isinstance(value, dict):
        return {k: resolve_secrets(v, credentials) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_secrets(v, credentials) for v in value]
    return value


def credential_refs(value: Any) -> set[str]:
    """Names of credentials referenced via ``${CRED:name.field}`` inside a value."""
    if isinstance(value, str):
        return {m.group(1) for m in _CRED_REF.finditer(value)}
    if isinstance(value, dict):
        return set().union(*(credential_refs(v) for v in value.values())) if value else set()
    if isinstance(value, list):
        return set().union(*(credential_refs(v) for v in value)) if value else set()
    return set()


# --------------------------------------------------------------------------- #
# Parameters
# --------------------------------------------------------------------------- #
ParamType = Literal["string", "integer", "number", "boolean"]
ParamLocation = Literal["query", "path", "body"]


class ParamSpec(BaseModel):
    name: str = Field(pattern=r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")
    type: ParamType = "string"
    location: ParamLocation = Field("query", alias="in")
    required: bool = False
    default: Any = None
    description: str = ""
    # Validation constraints (all optional)
    min: float | None = None
    max: float | None = None
    max_length: int | None = 1024          # bounded by default
    pattern: str | None = None
    enum: list[Any] | None = None

    model_config = {"populate_by_name": True}

    @classmethod
    def parse_short(cls, text: str) -> "ParamSpec":
        """Parse CLI shorthand: ``name:type[:in][:required]``  e.g. ``id:integer:path``."""
        parts = text.split(":")
        data: dict[str, Any] = {"name": parts[0]}
        for p in parts[1:]:
            if p in ("string", "integer", "number", "boolean"):
                data["type"] = p
            elif p in ("query", "path", "body"):
                data["in"] = p
            elif p in ("required", "req"):
                data["required"] = True
            else:
                raise ConfigError(f"Unknown param modifier '{p}' in '{text}'")
        if data.get("in") == "path":
            data["required"] = True
        return cls(**data)


# --------------------------------------------------------------------------- #
# Sources
# --------------------------------------------------------------------------- #
class SourceSpec(BaseModel):
    type: Literal["sql", "file", "rest", "callable", "ldap"]
    description: str = ""
    # managed credential used to connect/authenticate (see `portwright cred`)
    credential: str | None = None
    # callable only: credentials the handler may read via ctx.credentials
    credentials: list[str] = Field(default_factory=list)
    # sql
    url: str | None = None
    read_only: bool = True
    pool_size: int = 5
    # file
    path: str | None = None
    sheet: str | None = None
    # rest
    base_url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: float = 10.0
    # callable
    module: str | None = None
    # ldap (Active Directory) – a load target for group membership
    servers: list[str] = Field(default_factory=list)   # ldaps://dc01.corp.local[:636], tried in order
    base_dn: str | None = None                         # where users and groups are searched
    directory: Literal["ldap", "powershell"] = "ldap"   # ldap3 over LDAPS, or the PowerShell AD module
    ca_file: str | None = None                         # extra CA bundle (PEM); default: system store
    allow_plain_ldap: bool = False                     # ldap:// without TLS is refused unless set

    @model_validator(mode="after")
    def _check(self) -> "SourceSpec":
        if self.type == "sql" and not (self.url or self.credential):
            raise ValueError("source type 'sql' requires 'url' or 'credential'")
        if self.type == "ldap":
            if not self.base_dn:
                raise ValueError("source type 'ldap' requires 'base_dn', e.g. DC=corp,DC=local")
            if self.directory == "ldap":
                if not self.servers:
                    raise ValueError("source type 'ldap' requires 'servers', e.g. ldaps://dc01.corp.local")
                if not self.credential:
                    raise ValueError("an ldap source needs a 'credential' (service account, type basic)")
                plain = [s for s in self.servers if s.lower().startswith("ldap://")]
                if plain and not self.allow_plain_ldap:
                    raise ValueError(f"{plain[0]} is not encrypted; use ldaps:// (or set allow_plain_ldap: true)")
        required = {"file": "path", "rest": "base_url"}
        field = required.get(self.type)
        if field and not getattr(self, field):
            raise ValueError(f"source type '{self.type}' requires '{field}'")
        if self.credentials and self.type != "callable":
            raise ValueError("'credentials' (list) is only for callable sources; use 'credential'")
        return self


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
HttpMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
SqlOperation = Literal["query", "list", "get", "create", "update", "delete"]


class EndpointSpec(BaseModel):
    name: str = Field(pattern=_NAME)
    path: str
    method: HttpMethod = "GET"
    source: str
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    params: list[ParamSpec] = Field(default_factory=list)

    # --- security ---
    public: bool = False                  # explicit opt-out of auth
    scopes: list[str] = Field(default_factory=list)
    rate_limit: int | None = None         # requests / window, overrides default

    # --- sql ---
    operation: SqlOperation | None = None
    query: str | None = None
    table: str | None = None
    key: str | None = None
    columns: list[str] | None = None      # column allow-list

    # --- file ---
    filters: list[str] = Field(default_factory=list)  # filterable fields

    # --- rest ---
    upstream_path: str | None = None
    upstream_method: HttpMethod | None = None

    # --- callable ---
    target: str | None = None             # "package.module:function"

    # --- pagination for list-like results ---
    max_limit: int = 500

    @field_validator("path")
    @classmethod
    def _path(cls, v: str) -> str:
        if not v.startswith("/"):
            v = "/" + v
        if ".." in v or "//" in v:
            raise ValueError("path must not contain '..' or '//'")
        return v.rstrip("/") or "/"

    @model_validator(mode="after")
    def _path_params_declared(self) -> "EndpointSpec":
        in_path = set(re.findall(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", self.path))
        declared = {p.name for p in self.params if p.location == "path"}
        missing = in_path - declared
        if missing:
            # auto-declare as required strings to keep CLI ergonomic
            for name in sorted(missing):
                self.params.append(ParamSpec(name=name, location="path", required=True))
        extra = declared - in_path
        if extra:
            raise ValueError(f"path params {sorted(extra)} not present in path '{self.path}'")
        return self


# --------------------------------------------------------------------------- #
# Security
# --------------------------------------------------------------------------- #
class ApiKeyRecord(BaseModel):
    id: str
    name: str
    hash: str                              # sha256 of the key – plaintext never stored
    scopes: list[str] = Field(default_factory=list)
    created: str
    expires: str | None = None
    disabled: bool = False


class UserRecord(BaseModel):
    username: str = Field(pattern=_NAME)
    password_hash: str                     # scrypt
    scopes: list[str] = Field(default_factory=list)
    disabled: bool = False


class JwtSettings(BaseModel):
    secret: str = "${ENV:PORTWRIGHT_JWT_SECRET}"
    algorithm: Literal["HS256", "HS384", "HS512"] = "HS256"
    ttl_minutes: int = 30
    issuer: str = "portwright"
    audience: str = "portwright-api"


class RateLimitSettings(BaseModel):
    requests: int = 120
    window_seconds: int = 60
    login_requests: int = 5               # brute-force protection on /auth/token


class HashiCorpVaultSettings(BaseModel):
    url: str = "${ENV:VAULT_ADDR}"
    token: str = "${ENV:VAULT_TOKEN}"
    mount: str = "secret"                 # KV v2 mount point
    path_prefix: str = "restforge"        # secrets at <mount>/data/<prefix>/<name> (kept for existing secrets)
    namespace: str | None = None
    verify_tls: bool = True


class CredentialSettings(BaseModel):
    # Providers are consulted in order; the first that has the credential wins.
    providers: list[Literal["vault", "env", "keyring", "hashicorp"]] = Field(
        default_factory=lambda: ["vault", "env"])
    vault_path: str = ".portwright/credentials.vault"
    master_key_env: str = "PORTWRIGHT_MASTER_KEY"
    cache_ttl_seconds: int = 300
    keyring_service: str = "restforge"     # kept: existing keyring entries live under this service
    hashicorp: HashiCorpVaultSettings = Field(default_factory=HashiCorpVaultSettings)


class SecuritySettings(BaseModel):
    credentials: CredentialSettings = Field(default_factory=CredentialSettings)
    auth_methods: list[Literal["api_key", "jwt"]] = Field(default_factory=lambda: ["api_key", "jwt"])
    api_key_header: str = "X-API-Key"
    jwt: JwtSettings = Field(default_factory=JwtSettings)
    rate_limit: RateLimitSettings = Field(default_factory=RateLimitSettings)
    api_keys: list[ApiKeyRecord] = Field(default_factory=list)
    users: list[UserRecord] = Field(default_factory=list)
    allowed_callable_modules: list[str] = Field(default_factory=list)
    allowed_upstream_hosts: list[str] = Field(default_factory=list)
    data_root: str = "data"               # file sources are sandboxed here
    cors_origins: list[str] = Field(default_factory=list)
    max_body_bytes: int = 1_048_576
    expose_docs: bool = True
    audit_log: str | None = "logs/audit.jsonl"



# --------------------------------------------------------------------------- #
# Load jobs (CSV/endpoint -> mapped -> database table)
# --------------------------------------------------------------------------- #
ColumnType = Literal["string", "integer", "number", "boolean", "date", "timestamp"]


class ColumnMap(BaseModel):
    """One target column: where its value comes from and how it is converted."""
    target: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_$#]{0,127}$")
    source: str | None = None            # source/CSV column name (may contain spaces)
    value: Any = None                    # constant, or ${now} / ${today} / ${run_id}
    type: ColumnType = "string"
    format: str | None = None            # strptime format for date/timestamp, e.g. %m/%d/%Y
    transform: list[Any] = Field(default_factory=list)   # trim, upper, lower, null_if_empty,
                                                         # {replace: [old, new]}, {lookup: {...}}
    default: Any = None                  # used when the source cell is blank
    required: bool = False
    max_length: int | None = None
    review: bool = False                 # set by auto-map for uncertain matches

    @model_validator(mode="after")
    def _one_input(self) -> "ColumnMap":
        if self.source is None and self.value is None:
            raise ValueError(f"column '{self.target}': give 'source' or 'value'")
        if self.source is not None and self.value is not None:
            raise ValueError(f"column '{self.target}': use 'source' or 'value', not both")
        return self


class LoadFrom(BaseModel):
    endpoint: str | None = None          # any GET endpoint (all pages are read)
    source: str | None = None            # a file source, read directly
    params: dict[str, Any] = Field(default_factory=dict)   # endpoint params (filters)

    @model_validator(mode="after")
    def _one(self) -> "LoadFrom":
        if bool(self.endpoint) == bool(self.source):
            raise ValueError("load 'from' needs exactly one of 'endpoint' or 'source'")
        return self


class LoadTo(BaseModel):
    source: str                          # a SQL source with read_only: false, or an ldap source
    table: str | None = Field(None, pattern=r"^[A-Za-z_][A-Za-z0-9_$#.]{0,127}$")
    group: str | None = None             # AD group: distinguished name, or its name / sAMAccountName

    @model_validator(mode="after")
    def _one(self) -> "LoadTo":
        if bool(self.table) == bool(self.group):
            raise ValueError("load 'to' needs exactly one of 'table' or 'group'")
        return self

    @property
    def target(self) -> str:
        return self.table or self.group or ""


class GroupLoad(BaseModel):
    """Settings for loads into an Active Directory group."""
    email_column: str = "email"                       # source column holding the e-mail address
    match: list[Literal["mail", "userPrincipalName", "proxyAddresses"]] = Field(
        default_factory=lambda: ["mail", "userPrincipalName"])        # tried in this order
    max_remove_percent: float = Field(20, ge=0, le=100)   # refuse to remove more of the current members
    protected: list[str] = Field(default_factory=list)  # never removed: DNs, sAMAccountNames or e-mails
    keep_nested_groups: bool = True                     # groups inside the group are never removed
    exception_group: str | None = None                  # members of this group are never removed
    reject_disabled: bool = True                        # disabled accounts are rejected, not added


class SourceTracking(BaseModel):
    """Source versioning and change detection for a load."""
    on_unchanged: Literal["skip", "load", "fail"] = "skip"     # same data as the last successful load
    on_header_change: Literal["fail", "warn"] = "fail"         # columns added/removed vs that version
    stable_seconds: int = Field(0, ge=0, le=3600)              # wait until the file is this old (still copying?)
    max_age_hours: float | None = Field(None, gt=0)            # fail if the file is older than this
    archive_keep: int = Field(30, ge=0, le=10000)              # versions kept as compressed copies (0 = none)


class LoadSafety(BaseModel):
    """Guards for kill & fill (mode replace): refuse to empty the table for a short or empty file."""
    min_rows: int = Field(0, ge=0)                 # the source must have at least this many good rows
    min_percent: float = Field(0, ge=0, le=1000)   # ... and at least this % of the rows now in the table


class LoadSpec(BaseModel):
    name: str = Field(pattern=_NAME)
    description: str = ""
    from_: LoadFrom = Field(alias="from")
    to: LoadTo
    mode: Literal["insert", "replace", "upsert", "sync"] = "insert"   # group loads: replace | sync | insert (add only)
    keys: list[str] = Field(default_factory=list)          # load key (target columns), any mode
    duplicate_keys: Literal["reject", "first", "last"] = "reject"   # same key twice in the source
    existing_keys: Literal["reject", "skip", "fail"] = "reject"     # insert mode: key already in the table
    tracking: SourceTracking = Field(default_factory=SourceTracking)
    safety: LoadSafety = Field(default_factory=LoadSafety)
    ad: GroupLoad = Field(default_factory=GroupLoad)
    batch_size: int = Field(1000, ge=1, le=50000)
    max_rejects: int = Field(0, ge=0)
    unmapped_source_columns: Literal["ignore", "error"] = "ignore"
    columns: list[ColumnMap] = Field(default_factory=list)

    model_config = {"populate_by_name": True}

    @model_validator(mode="after")
    def _check(self) -> "LoadSpec":
        targets = [c.target.lower() for c in self.columns]
        dupes = {t for t in targets if targets.count(t) > 1}
        if dupes:
            raise ValueError(f"load '{self.name}': target columns mapped twice: {sorted(dupes)}")
        if self.to.group:
            if self.mode == "upsert":
                raise ValueError(f"load '{self.name}': AD group loads use mode replace (kill & fill), "
                                 f"sync or insert (add only)")
            return self
        if self.mode == "sync":
            raise ValueError(f"load '{self.name}': mode 'sync' is for AD group loads; use upsert for tables")
        if self.mode == "upsert" and not self.keys:
            raise ValueError(f"load '{self.name}': mode 'upsert' needs 'keys'")
        missing = {k.lower() for k in self.keys} - set(targets)
        if missing and self.columns:
            raise ValueError(f"load '{self.name}': key columns {sorted(missing)} are not mapped")
        lower = [k.lower() for k in self.keys]
        if len(set(lower)) != len(lower):
            raise ValueError(f"load '{self.name}': a key column is listed twice")
        return self

class ServerSettings(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8080
    base_path: str = "/api"
    ssl_certfile: str | None = None
    ssl_keyfile: str | None = None


class ProjectConfig(BaseModel):
    project: str = "portwright-project"
    version: str = "1.0.0"
    server: ServerSettings = Field(default_factory=ServerSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    sources: dict[str, SourceSpec] = Field(default_factory=dict)
    endpoints: list[EndpointSpec] = Field(default_factory=list)
    loads: list[LoadSpec] = Field(default_factory=list)

    @model_validator(mode="after")
    def _integrity(self) -> "ProjectConfig":
        load_names: set[str] = set()
        for ld in self.loads:
            if ld.name in load_names:
                raise ValueError(f"duplicate load name '{ld.name}'")
            load_names.add(ld.name)
            if ld.from_.endpoint and ld.from_.endpoint not in {e.name for e in self.endpoints}:
                raise ValueError(f"load '{ld.name}' reads unknown endpoint '{ld.from_.endpoint}'")
            if ld.from_.source and ld.from_.source not in self.sources:
                raise ValueError(f"load '{ld.name}' reads unknown source '{ld.from_.source}'")
            tgt = self.sources.get(ld.to.source)
            if ld.to.group:
                if tgt is None or tgt.type != "ldap":
                    raise ValueError(f"load '{ld.name}': an AD group target needs an ldap source, "
                                     f"'{ld.to.source}' is not one")
            elif tgt is None or tgt.type != "sql":
                raise ValueError(f"load '{ld.name}': target '{ld.to.source}' must be a SQL source")
        seen_names: set[str] = set()
        seen_routes: set[tuple[str, str]] = set()
        for ep in self.endpoints:
            if ep.source not in self.sources:
                raise ValueError(f"endpoint '{ep.name}' references unknown source '{ep.source}'")
            if ep.name in seen_names:
                raise ValueError(f"duplicate endpoint name '{ep.name}'")
            route = (ep.method, re.sub(r"\{[^}]+\}", "{}", ep.path))
            if route in seen_routes:
                raise ValueError(f"duplicate route {ep.method} {ep.path}")
            seen_names.add(ep.name)
            seen_routes.add(route)
        return self

    # ------------------------------------------------------------------ io --
    @classmethod
    def load(cls, path: str | Path = DEFAULT_CONFIG) -> "ProjectConfig":
        from .compat import resolve_config
        path = resolve_config(path)
        p = Path(path)
        if not p.exists():
            raise ConfigError(f"Config file '{p}' not found. Run 'portwright init' first.")
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        try:
            return cls.model_validate(data)
        except Exception as exc:  # pydantic ValidationError
            raise ConfigError(str(exc)) from exc

    def save(self, path: str | Path = DEFAULT_CONFIG) -> None:
        data = self.model_dump(mode="json", by_alias=True, exclude_none=True)
        Path(path).write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    def load_job(self, name: str) -> "LoadSpec | None":
        return next((l for l in self.loads if l.name == name), None)

    def endpoint(self, name: str) -> EndpointSpec | None:
        return next((e for e in self.endpoints if e.name == name), None)
