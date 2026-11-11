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

from .agents.spec import (AgentSpec, LlmModelSpec, McpServerSpec, SkillSpec, ToolSpec, WorkflowSpec)

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
    type: Literal["sql", "file", "rest", "callable", "ldap", "folder"]
    description: str = ""
    # managed credential used to connect/authenticate (see `portwright cred`)
    credential: str | None = None
    # callable only: credentials the handler may read via ctx.credentials
    credentials: list[str] = Field(default_factory=list)
    # sql
    url: str | None = None
    read_only: bool = True
    pool_size: int = 5
    # file (and folder: the folder – a name inside the data folder, or an allowed absolute / UNC path)
    path: str | None = None
    sheet: str | None = None
    # folder – every matching file is a source row (document) or a set of rows (records)
    include: list[str] = Field(default_factory=lambda: ["*"])      # glob patterns, e.g. *.pdf
    exclude: list[str] = Field(default_factory=list)               # e.g. ~*, *.tmp
    recursive: bool = False
    max_file_mb: float = Field(100, gt=0, le=2048)                 # bigger files are rejected
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
    test_group: str | None = None                      # 'source test' lists this group's members

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
        required = {"file": "path", "rest": "base_url", "folder": "path"}
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
    # Global (cross-project) vault — consulted after the project vault so project creds win
    use_global_vault: bool = True
    global_vault_path: str = "~/.portwright/credentials.vault"
    global_master_key_env: str = "PORTWRIGHT_GLOBAL_KEY"  # falls back to master_key_env when unset


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
    allowed_llm_hosts: list[str] = Field(default_factory=list)   # hosts LLM models may call
    data_root: str = "data"               # file sources are sandboxed here
    allowed_folders: list[str] = Field(default_factory=list)   # absolute / UNC folders a folder source may use
    cors_origins: list[str] = Field(default_factory=list)
    max_body_bytes: int = 1_048_576
    expose_docs: bool = True
    audit_log: str | None = "logs/audit.jsonl"



# --------------------------------------------------------------------------- #
# Load jobs (CSV/endpoint -> mapped -> database table)
# --------------------------------------------------------------------------- #
ColumnType = Literal["string", "integer", "number", "boolean", "date", "timestamp", "binary", "text"]
# binary: BLOB / VARBINARY(MAX) / BYTEA / LONGBLOB · text: CLOB / NVARCHAR(MAX) / TEXT / LONGTEXT


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


class FolderLoad(BaseModel):
    """Settings for loads that read a folder source."""
    read: Literal["document", "records"] = "document"   # one row per file, or parse each CSV/Excel/JSON file
    on_changed: Literal["version", "replace"] = "version"   # same file name, new content: another row, or
                                                            # replace the rows loaded from that file
    file_key: str | None = None            # target column identifying the file (mapped from file_path or
                                           # file_name) – required for on_changed: replace
    check_target: bool = True              # skip a file whose sha256 is already in the target table
                                           # (when a column is mapped from sha256)
    after_load: Literal["move", "leave", "delete"] = "move"
    archive_folder: str = "processed"      # inside the source folder, or an absolute path
    dated_folders: bool = True             # processed\2026-10-23\file.pdf
    rejected_folder: str | None = "rejected"   # failed files go here with a .reason.txt (None = leave them)
    encoding: str = "auto"                 # content_text / CSV: auto (BOM, UTF-8, then Windows-1252) or a codec
    max_files: int = Field(0, ge=0)        # per run, oldest first (0 = all)


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
    files: FolderLoad = Field(default_factory=FolderLoad)
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
        if self.files.on_changed == "replace" and self.files.file_key and self.columns and \
                self.files.file_key.lower() not in targets:
            raise ValueError(f"load '{self.name}': files.file_key '{self.files.file_key}' is not a mapped column")
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


# --------------------------------------------------------------------------- #
# Reports  (Jinja2-templated PDF / HTML generation)
# --------------------------------------------------------------------------- #

REPORT_FORMATS = ("pdf", "html", "md", "csv", "xlsx", "json")
TEMPLATE_FORMATS = {"pdf", "html"}       # formats rendered from the Jinja2 HTML template
ReportFormat = Literal["pdf", "html", "md", "csv", "xlsx", "json"]


class ReportFrom(BaseModel):
    """Where the report pulls its data from."""
    endpoint: str | None = None   # a GET endpoint name
    source: str | None = None     # a file or SQL source (direct read, no HTTP round-trip)
    params: dict[str, Any] = Field(default_factory=dict)   # endpoint params / SQL bind values
    query: str | None = None      # SQL sources: one read-only SELECT, values bound from params (:name)
    paginate: bool = True         # endpoints: follow limit/offset until every page is read
    max_rows: int = Field(50_000, ge=1, le=1_000_000)       # stop reading after this many rows

    @model_validator(mode="after")
    def _one(self) -> "ReportFrom":
        if bool(self.endpoint) == bool(self.source):
            raise ValueError("report 'from' needs exactly one of 'endpoint' or 'source'")
        if self.query is not None and not self.source:
            raise ValueError("report 'from.query' needs 'from.source' (a SQL source)")
        return self


class ReportSpec(BaseModel):
    """Declaration of one report, rendered to one or more formats."""
    name: str = Field(pattern=_NAME)
    description: str = ""
    # Jinja2 HTML template (relative to the project root, or absolute). Needed for pdf/html output only.
    template: str | None = None
    from_: ReportFrom = Field(alias="from")
    # Formats to produce on every run. Empty: taken from the output suffix (.html → html), else pdf.
    formats: list[ReportFormat] = Field(default_factory=list)
    # Output path *stem*; may contain Jinja2 expressions, e.g.
    #   reports/output/{{ now | strftime('%Y-%m') }}/{{ report_name }}
    # A known suffix (.pdf, .html, …) is ignored; each format adds its own.
    output: str | None = None
    # Extra variables injected into every template render
    vars: dict[str, Any] = Field(default_factory=dict)
    # Data formats (csv / xlsx / json / md): column order/subset and header renames
    columns: list[str] = Field(default_factory=list)
    rename: dict[str, str] = Field(default_factory=dict)

    model_config = {"populate_by_name": True}

    @field_validator("formats")
    @classmethod
    def _unique(cls, v: list[str]) -> list[str]:
        return list(dict.fromkeys(v))

    def effective_formats(self) -> list[str]:
        if self.formats:
            return list(self.formats)
        return ["html"] if (self.output or "").lower().endswith((".html", ".htm")) else ["pdf"]

    @model_validator(mode="after")
    def _template_needed(self) -> "ReportSpec":
        need = TEMPLATE_FORMATS & set(self.effective_formats())
        if need and not self.template:
            raise ValueError(f"report '{self.name}': format(s) {sorted(need)} need a 'template'")
        return self


# --------------------------------------------------------------------------- #
# Filesystem roots and destinations (workflow copy/move/file steps, fs tools)
# --------------------------------------------------------------------------- #
_ROOT_NAME = r"^[a-zA-Z][a-zA-Z0-9_\-]{1,63}$"     # 2+ characters, so 'C:\x' is never read as a root address
OverwritePolicy = Literal["never", "always", "rename", "if_newer"]


class FsRoot(BaseModel):
    """A named folder steps and tools may use. Relative paths are inside the project; absolute / UNC paths
    must also be listed in security.allowed_folders."""
    path: str
    mode: Literal["read", "write", "read_write"] = "read"
    patterns: list[str] = Field(default_factory=list)     # file-name globs allowed here (empty: any)
    delete: Literal["none", "trash", "hard"] = "trash"    # what 'delete' does on a writable root


class DestinationSpec(BaseModel):
    """A write-only place outputs are delivered to (``dest:<name>/sub/folder``). Reached as the account
    running portwright - there are no destination credentials."""
    path: str
    overwrite: OverwritePolicy = "never"                  # default policy for copy / move steps
    description: str = ""


class FilesystemSettings(BaseModel):
    roots: dict[str, FsRoot] = Field(default_factory=dict)
    max_file_mb: int = Field(50, ge=1, le=10_240)
    trash: str = ".portwright/trash"                      # deleted files go here (inside the project)


def is_absolute_path(p: str) -> bool:
    from pathlib import PureWindowsPath
    return PureWindowsPath(p).is_absolute() or p.startswith(("/", "\\\\")) or bool(PureWindowsPath(p).drive)


def path_allowed(p: str, allowed: list[str]) -> bool:
    """Is absolute path *p* one of / inside one of the *allowed* folders (case-insensitive, either slash)?"""
    def norm(x: str) -> str:
        return str(x).replace("\\", "/").rstrip("/").lower()
    n = norm(p)
    return any(n == a or n.startswith(a + "/") for a in (norm(x) for x in allowed))


class ProjectConfig(BaseModel):
    project: str = "portwright-project"
    version: str = "1.0.0"
    server: ServerSettings = Field(default_factory=ServerSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    sources: dict[str, SourceSpec] = Field(default_factory=dict)
    endpoints: list[EndpointSpec] = Field(default_factory=list)
    loads: list[LoadSpec] = Field(default_factory=list)
    reports: list[ReportSpec] = Field(default_factory=list)
    llm_models_include: list[str] = Field(default_factory=list)   # files/globs with more llm_models
    llm_models: dict[str, LlmModelSpec] = Field(default_factory=dict)
    tools: dict[str, ToolSpec] = Field(default_factory=dict)
    skills: dict[str, SkillSpec] = Field(default_factory=dict)
    agents: dict[str, AgentSpec] = Field(default_factory=dict)
    workflows: dict[str, WorkflowSpec] = Field(default_factory=dict)
    mcp_servers: dict[str, McpServerSpec] = Field(default_factory=dict)
    filesystem: FilesystemSettings = Field(default_factory=FilesystemSettings)
    destinations: dict[str, DestinationSpec] = Field(default_factory=dict)

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
            src = self.sources.get(ld.from_.source) if ld.from_.source else None
            if src is not None and src.type == "folder":
                if ld.to.group:
                    raise ValueError(f"load '{ld.name}': a folder source loads into a table, not an AD group")
                if ld.mode != "insert":
                    raise ValueError(f"load '{ld.name}': folder loads add files (mode insert); use "
                                     f"files.on_changed: version or replace for changed files")
                if ld.files.on_changed == "replace" and not ld.files.file_key:
                    raise ValueError(f"load '{ld.name}': files.on_changed: replace needs files.file_key (the "
                                     f"target column mapped from file_path or file_name)")
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
        report_names: set[str] = set()
        ep_names = {e.name for e in self.endpoints}
        for rpt in self.reports:
            if rpt.name in report_names:
                raise ValueError(f"duplicate report name '{rpt.name}'")
            report_names.add(rpt.name)
            if rpt.from_.endpoint and rpt.from_.endpoint not in ep_names:
                raise ValueError(
                    f"report '{rpt.name}' reads unknown endpoint '{rpt.from_.endpoint}'"
                )
            if rpt.from_.source and rpt.from_.source not in self.sources:
                raise ValueError(
                    f"report '{rpt.name}' reads unknown source '{rpt.from_.source}'"
                )
            if rpt.from_.query is not None and self.sources[rpt.from_.source].type != "sql":
                raise ValueError(f"report '{rpt.name}': 'from.query' needs a SQL source, "
                                 f"'{rpt.from_.source}' is {self.sources[rpt.from_.source].type}")
        self._check_filesystem()
        self._check_agents()
        return self

    def _check_address(self, wf: str, step: str, addr: str | None) -> None:
        """A literal 'root:...' / 'dest:name/...' in a step must name a defined root or destination."""
        if not addr or "${" in addr.split(":", 1)[0]:          # the root itself is chosen at run time
            return
        m = re.match(r"^([A-Za-z][A-Za-z0-9_\-]+):(.*)$", addr)
        if not m:
            return
        name, rest = m.groups()
        implicit = {"reports", "templates", "data"}
        if name == "dest":
            dname = re.split(r"[\\/]", rest.lstrip("/\\"), maxsplit=1)[0]
            if "${" not in dname and dname not in self.destinations:
                raise ValueError(f"workflow '{wf}' step '{step}': unknown destination '{dname}'")
        elif name not in self.filesystem.roots and name not in implicit:
            raise ValueError(f"workflow '{wf}' step '{step}': unknown filesystem root '{name}'")

    def _check_filesystem(self) -> None:
        places = [(f"filesystem root '{n}'", n, r.path) for n, r in self.filesystem.roots.items()]
        places += [(f"destination '{n}'", n, d.path) for n, d in self.destinations.items()]
        for what, name, path in places:
            if not re.match(_ROOT_NAME, name):
                raise ValueError(f"{what}: names are 2-64 letters, digits, - and _, starting with a letter")
            if not path.strip():
                raise ValueError(f"{what} has no path")
            if is_absolute_path(path):
                if not path_allowed(path, self.security.allowed_folders):
                    raise ValueError(f"{what}: '{path}' is outside the project; add it to "
                                     "security.allowed_folders first")
            elif ".." in re.split(r"[\\/]", path):
                raise ValueError(f"{what}: '..' is not allowed in '{path}'")
        if "dest" in self.filesystem.roots:
            raise ValueError("filesystem root name 'dest' is reserved for destinations (dest:<name>/...)")

    def _check_agents(self) -> None:
        """References between tools, skills, agents, workflows and MCP servers.
        Model names are checked at build time (they may live in included files)."""
        for section in (self.llm_models, self.tools, self.skills, self.agents, self.workflows, self.mcp_servers):
            for n in section:
                if not re.match(_NAME, n):
                    raise ValueError(f"invalid name '{n}' (letters, digits, - and _; start with a letter)")
        eps = {e.name for e in self.endpoints}
        for n, t in self.tools.items():
            if t.type == "endpoint" and t.endpoint not in eps:
                raise ValueError(f"tool '{n}' uses unknown endpoint '{t.endpoint}'")
            if t.type == "source":
                src = self.sources.get(t.source)
                if src is None or src.type != "sql":
                    raise ValueError(f"tool '{n}': '{t.source}' must be a SQL source")
                if not t.read_only and src.read_only:
                    raise ValueError(f"tool '{n}': source '{t.source}' is read-only; allow writes there first")
            if t.type == "mcp" and t.server not in self.mcp_servers:
                raise ValueError(f"tool '{n}' uses unknown MCP server '{t.server}'")
        for n, a in self.agents.items():
            for t in a.tools:
                if t not in self.tools:
                    raise ValueError(f"agent '{n}' uses unknown tool '{t}'")
            for k in a.skills:
                if k not in self.skills:
                    raise ValueError(f"agent '{n}' uses unknown skill '{k}'")
        for n, w in self.workflows.items():
            ids = [st.id for st in w.steps]
            if len(set(ids)) != len(ids):
                raise ValueError(f"workflow '{n}': duplicate step ids")
            for st in w.steps:
                if st.type == "tool" and st.tool not in self.tools:
                    raise ValueError(f"workflow '{n}' step '{st.id}': unknown tool '{st.tool}'")
                if st.type == "agent" and st.agent not in self.agents:
                    raise ValueError(f"workflow '{n}' step '{st.id}': unknown agent '{st.agent}'")
                if st.type == "load" and st.load not in {x.name for x in self.loads}:
                    raise ValueError(f"workflow '{n}' step '{st.id}': unknown load '{st.load}'")
                kind = st.do if st.type == "foreach" else st.type
                if kind == "report" and st.report and self.report(st.report) is None:
                    raise ValueError(f"workflow '{n}' step '{st.id}': unknown report '{st.report}'")
                for addr in (st.to, st.path) if st.type in ("copy", "move", "file", "foreach") else ():
                    self._check_address(n, st.id, addr)
                if st.type == "foreach":
                    if st.do == "tool" and st.tool not in self.tools:
                        raise ValueError(f"workflow '{n}' step '{st.id}': unknown tool '{st.tool}'")
                    if st.do == "agent" and st.agent not in self.agents:
                        raise ValueError(f"workflow '{n}' step '{st.id}': unknown agent '{st.agent}'")
                    if st.do == "load" and st.load not in {x.name for x in self.loads}:
                        raise ValueError(f"workflow '{n}' step '{st.id}': unknown load '{st.load}'")
                for tgt in (st.then, st.else_, st.next, st.default, *(st.cases or {}).values()):
                    if tgt and tgt != "end" and tgt not in ids:
                        raise ValueError(f"workflow '{n}' step '{st.id}': unknown target step '{tgt}'")
        for n, m in self.mcp_servers.items():
            for e in m.expose.endpoints:
                if e not in eps:
                    raise ValueError(f"MCP server '{n}' exposes unknown endpoint '{e}'")
            for s in m.expose.sources:
                src = self.sources.get(s.name)
                if src is None or src.type != "sql":
                    raise ValueError(f"MCP server '{n}': source '{s.name}' must be a SQL source")
                if not s.read_only and src.read_only:
                    raise ValueError(f"MCP server '{n}': source '{s.name}' is read-only")
            for l in m.expose.loads:
                if l not in {x.name for x in self.loads}:
                    raise ValueError(f"MCP server '{n}' exposes unknown load '{l}'")

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

    def report(self, name: str) -> "ReportSpec | None":
        return next((r for r in self.reports if r.name == name), None)
