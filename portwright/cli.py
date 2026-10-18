"""portwright CLI – create and manage REST endpoints, credentials and the server.

All logic lives in ``portwright.services``; these commands only collect input and print results.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import typer
import yaml

from .config import DEFAULT_CONFIG, ConfigError, ProjectConfig
from .credentials import TYPES
from .services import Project, ServiceError, Services, load_dotenv  # noqa: F401  (load_dotenv re-exported)

app = typer.Typer(help="Portwright – secure REST endpoints and data loads for databases, files, APIs and "
                       "Python functions. Short alias: pw (e.g. 'pw call transfer-list').",
                  no_args_is_help=True)


def _version(value: bool) -> None:
    if value:
        from . import __version__
        typer.echo(f"Portwright {__version__}")
        raise typer.Exit()


@app.callback()
def _main(version: bool = typer.Option(False, "--version", "-V", callback=_version, is_eager=True,
                                       help="Show the version and exit.")):
    """Portwright – secure REST endpoints and data loads. Short alias: pw."""
source_app = typer.Typer(help="Manage data sources.", no_args_is_help=True)
endpoint_app = typer.Typer(help="Manage REST endpoints.", no_args_is_help=True)
key_app = typer.Typer(help="Manage API keys.", no_args_is_help=True)
user_app = typer.Typer(help="Manage users (for JWT login).", no_args_is_help=True)
cred_app = typer.Typer(help="Manage connection credentials (DB logins, API tokens, OAuth clients…).",
                       no_args_is_help=True)
app.add_typer(cred_app, name="cred")
app.add_typer(source_app, name="source")
app.add_typer(endpoint_app, name="endpoint")
app.add_typer(key_app, name="key")
app.add_typer(user_app, name="user")

def _config_cb(value: str) -> str:
    from .compat import resolve_config
    return resolve_config(value) if value else value


CONFIG_OPT = typer.Option(DEFAULT_CONFIG, "--config", "-c", callback=_config_cb,
                          help="Project config file (portwright.yaml; an existing restforge.yaml is also found).")


# ------------------------------------------------------------------ helpers
def _fail(exc: Exception) -> None:
    typer.secho(f"✗ {exc}", fg="red", err=True)
    raise typer.Exit(1)


def _ok(msg: str) -> None:
    typer.secho(f"✓ {msg}", fg="green")


def _warn(msgs) -> None:
    for m in msgs:
        typer.secho(f"⚠ {m}", fg="yellow")


def _svc(config: str) -> Services:
    try:
        return Services.open(config)
    except ServiceError as exc:
        _fail(exc)


def _load(path: str) -> ProjectConfig:
    try:
        return ProjectConfig.load(path)
    except ConfigError as exc:
        _fail(exc)


def _save(cfg: ProjectConfig, path: str) -> None:
    """Used by the load commands: validate + atomic save with backup via Project."""
    proj = Project(path)
    proj.config = cfg
    try:
        proj.save(force=True)
    except ServiceError as exc:
        _fail(exc)


def _kv(pairs: list[str]) -> dict[str, str]:
    out = {}
    for p in pairs:
        if "=" not in p:
            raise typer.BadParameter(f"expected KEY=VALUE, got '{p}'")
        k, v = p.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def _run(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except ServiceError as exc:
        _fail(exc)


# ------------------------------------------------------------------- init
@app.command()
def init(name: str = typer.Option("portwright-project", help="Project name."),
         directory: Path = typer.Argument(Path("."), help="Target directory.")):
    """Create a new project (portwright.yaml, .env with a generated JWT secret, .gitignore)."""
    _run(Project.init, directory, name)
    _ok(f"Initialised project '{name}' in {directory.resolve()}")
    typer.echo("Credential vault ready (master key in .env). Add logins with: portwright cred add …")
    typer.echo("Next: portwright source add …  →  portwright endpoint add …  →  portwright key create …  →  portwright serve")


# ----------------------------------------------------------------- sources
@source_app.command("add")
def source_add(
    name: str,
    type: str = typer.Option(..., "--type", "-t", help="sql | file | rest | callable"),
    url: Optional[str] = typer.Option(None, help="SQLAlchemy URL – use ${ENV:VAR} for credentials."),
    path: Optional[str] = typer.Option(None, help="File name inside the data folder."),
    sheet: Optional[str] = typer.Option(None, help="Excel sheet name."),
    base_url: Optional[str] = typer.Option(None, help="Upstream REST base URL."),
    header: list[str] = typer.Option([], help="Upstream header KEY=VALUE (repeatable)."),
    module: Optional[str] = typer.Option(None, help="Default module for callable targets."),
    read_write: bool = typer.Option(False, "--read-write", help="Allow writes on a SQL source."),
    allow_host: bool = typer.Option(False, "--allow-host", help="Add the REST host to the allow-list."),
    credential: Optional[str] = typer.Option(None, "--credential", help="Managed credential for connecting/auth."),
    allow_credential: list[str] = typer.Option([], "--allow-credential",
                                               help="Callable only: credential the handler may read (repeatable)."),
    description: str = "",
    config: str = CONFIG_OPT,
):
    """Register a data source."""
    s = _svc(config)
    warnings = _run(s.sources.add, name, type, url=url, path=path, sheet=sheet, base_url=base_url,
                    headers=_kv(header), module=module, read_write=read_write, allow_host=allow_host,
                    credential=credential, allow_credentials=allow_credential, description=description)
    _warn(warnings)
    _ok(f"Source '{name}' ({type}) added")


@source_app.command("set-credential")
def source_set_credential(name: str, credential: Optional[str] = typer.Argument(None, help="Omit to unset."),
                          config: str = CONFIG_OPT):
    """Attach (or detach) a managed credential to an existing source."""
    _run(_svc(config).sources.set_credential, name, credential)
    _ok(f"Source '{name}' now uses credential {credential!r}")


@source_app.command("list")
def source_list(config: str = CONFIG_OPT):
    for s in _svc(config).sources.list():
        detail = s["detail"] + (f" (credential: {s['credential']})" if s["credential"] else "")
        ro = " [read-only]" if s["read_only"] else ""
        typer.echo(f"{s['name']:<20} {s['type']:<9} {detail}{ro}")


@source_app.command("test")
def source_test(name: str, config: str = CONFIG_OPT):
    """Check a source: DB connection, file readable (shows first rows), module import, host allowed."""
    r = _svc(config).sources.test(name)
    if not r.ok:
        _fail(Exception(r.message))
    _ok(r.message)
    for row in r.rows:
        typer.echo(f"  {row}")


@source_app.command("remove")
def source_remove(name: str, config: str = CONFIG_OPT):
    _run(_svc(config).sources.remove, name)
    _ok(f"Source '{name}' removed")


# --------------------------------------------------------------- endpoints
@endpoint_app.command("add")
def endpoint_add(
    name: str,
    source: str = typer.Option(..., "--source", "-s"),
    path: str = typer.Option(..., "--path", "-p", help="e.g. /employees/{id}"),
    method: str = typer.Option("GET", "--method", "-m"),
    param: list[str] = typer.Option([], "--param", help="name:type[:query|path|body][:required] (repeatable)"),
    scope: list[str] = typer.Option([], "--scope", help="Required scope (repeatable)."),
    public: bool = typer.Option(False, "--public", help="No authentication (use with care)."),
    rate_limit: Optional[int] = typer.Option(None, help="Requests per window for this endpoint."),
    description: str = "",
    query: Optional[str] = typer.Option(None, help="SQL with :named binds."),
    operation: Optional[str] = typer.Option(None, help="list|get|create|update|delete (table mode)"),
    table: Optional[str] = None,
    key: Optional[str] = None,
    columns: Optional[str] = typer.Option(None, help="Comma-separated column allow-list."),
    filter: list[str] = typer.Option([], "--filter", help="Filterable field (repeatable)."),
    upstream_path: Optional[str] = None,
    upstream_method: Optional[str] = None,
    target: Optional[str] = typer.Option(None, help="module:function"),
    max_limit: int = 500,
    config: str = CONFIG_OPT,
):
    """Create an endpoint."""
    s = _svc(config)
    ep, warnings = _run(s.endpoints.add, name, source, path, method=method, params=param, scopes=scope,
                        public=public, rate_limit=rate_limit, description=description, query=query,
                        operation=operation, table=table, key=key,
                        columns=[c.strip() for c in columns.split(",")] if columns else None,
                        filters=filter, upstream_path=upstream_path, upstream_method=upstream_method,
                        target=target, max_limit=max_limit)
    _warn([w for w in warnings if "PUBLIC" in w])
    _ok(f"Endpoint '{name}': {ep.method} {s.project.config.server.base_path}{ep.path}")


@endpoint_app.command("crud")
def endpoint_crud(
    resource: str = typer.Argument(..., help="Resource name, e.g. employees"),
    source: str = typer.Option(..., "--source", "-s"),
    table: str = typer.Option(...),
    key: str = typer.Option(..., help="Primary key column."),
    key_type: str = typer.Option("integer"),
    field: list[str] = typer.Option([], "--field", help="Writable column name:type (repeatable)."),
    filter: list[str] = typer.Option([], "--filter", help="Filterable column on list (repeatable)."),
    read_scope: str = typer.Option(None, help="Scope for reads (default <resource>:read)."),
    write_scope: str = typer.Option(None, help="Scope for writes (default <resource>:write)."),
    read_only: bool = typer.Option(False, "--read-only", help="Generate list+get only."),
    config: str = CONFIG_OPT,
):
    """Generate list/get/create/update/delete endpoints for a SQL table."""
    new, rs, ws = _run(_svc(config).endpoints.crud, resource, source, table, key, key_type=key_type,
                       fields=field, filters=filter, read_scope=read_scope, write_scope=write_scope,
                       read_only=read_only)
    _ok(f"Generated {len(new)} endpoints for '{resource}' (scopes: {rs}{'' if read_only else ', ' + ws})")


@endpoint_app.command("list")
def endpoint_list(config: str = CONFIG_OPT):
    for e in _svc(config).endpoints.list():
        typer.echo(f"{e['method']:<7} {e['path']:<36} {e['name']:<24} {e['source']:<14} {e['access']}")


@endpoint_app.command("show")
def endpoint_show(name: str, config: str = CONFIG_OPT):
    ep = _run(_svc(config).endpoints.get, name)
    typer.echo(yaml.safe_dump(ep.model_dump(mode="json", by_alias=True, exclude_none=True), sort_keys=False))


@endpoint_app.command("set-access")
def endpoint_set_access(name: str, public: bool = typer.Option(False, "--public"),
                        scope: list[str] = typer.Option([], "--scope"), config: str = CONFIG_OPT):
    """Make an endpoint public, or set the scopes it requires."""
    if not public and not scope:
        raise typer.BadParameter("give --public or at least one --scope")
    _run(_svc(config).endpoints.set_access, name, public=public, scopes=scope)
    _ok(f"Endpoint '{name}' is now {'PUBLIC' if public else 'restricted to ' + ', '.join(scope)} "
        "(restart the server to apply)")


@endpoint_app.command("remove")
def endpoint_remove(name: str, config: str = CONFIG_OPT):
    _run(_svc(config).endpoints.remove, name)
    _ok(f"Endpoint '{name}' removed")


# ------------------------------------------------------------------- keys
@key_app.command("create")
def key_create(name: str, scope: list[str] = typer.Option([], "--scope"),
               expires_days: Optional[int] = typer.Option(90, help="0 = never expires."),
               config: str = CONFIG_OPT):
    """Create an API key. The plaintext key is printed ONCE."""
    k = _run(_svc(config).access.create_key, name, scope, expires_days)
    _ok(f"API key '{name}' created (id {k.id}, scopes: {', '.join(scope) or 'none'})")
    typer.secho(f"\n  {k.plaintext}\n", bold=True)
    typer.echo(f"Store it now – it cannot be shown again. Send it in the '{k.header}' header.")


@key_app.command("list")
def key_list(config: str = CONFIG_OPT):
    for k in _svc(config).access.keys():
        typer.echo(f"{k['id']}  {k['name']:<20} {k['status']:<8} expires={k['expires'] or 'never'}  "
                   f"scopes={','.join(k['scopes'])}")


@key_app.command("revoke")
def key_revoke(key_id: str, config: str = CONFIG_OPT):
    _run(_svc(config).access.revoke_key, key_id)
    _ok(f"Key {key_id} revoked (restart/reload the server to apply)")


# ------------------------------------------------------------- credentials
def _collect_fields(ctype: str, field: list[str], secret_from_env: list[str],
                    existing: dict | None = None, prompt_all_secrets: bool = True) -> dict:
    """Interactive part of credential entry: secrets are prompted with hidden input."""
    fields = dict(existing or {})
    given = _kv(field)
    fields.update(given)
    from_env = _kv(secret_from_env)
    for k, var in from_env.items():
        if var not in os.environ:
            raise typer.BadParameter(f"environment variable '{var}' is not set")
        fields[k] = os.environ[var]
    t = TYPES[ctype]
    if set(given) & set(t.secret):
        typer.secho(f"⚠ secret field(s) {sorted(set(given) & set(t.secret))} passed on the command line may be "
                    "kept in shell history; omit them to be prompted instead.", fg="yellow")
    for k in sorted(t.secret):
        if k in given or k in from_env:
            continue
        if prompt_all_secrets or k not in fields:
            if ctype == "database" and k == "password" and str(fields.get("driver", "")).startswith("sqlite"):
                continue
            fields[k] = typer.prompt(f"{k}", hide_input=True, confirmation_prompt=True)
    for k in sorted(t.required - set(fields)):
        fields[k] = typer.prompt(k)
    return fields


@cred_app.command("init")
def cred_init(passphrase: bool = typer.Option(False, "--passphrase",
                                              help="Use a passphrase you keep yourself instead of a generated key."),
              config: str = CONFIG_OPT):
    """Create the vault master key (stored in .env, never in the vault or YAML)."""
    s = _svc(config)
    st = s.credentials.vault_status()
    if passphrase:
        if st["key_present"]:
            _fail(Exception(f"{st['key_env']} is already set – use 'portwright cred rekey' to change it"))
        typer.prompt("Master passphrase (min 16 chars)", hide_input=True, confirmation_prompt=True)
        typer.echo(f"Not stored. Set {st['key_env']} to this passphrase in the server's environment "
                   "(e.g. a service/secret manager variable).")
        return
    env_name = _run(s.credentials.init_vault)
    _ok(f"Master key generated and written to .env as {env_name}")
    typer.echo("Back it up somewhere safe (password manager). Without it the vault cannot be decrypted.")


@cred_app.command("types")
def cred_types():
    """Show credential types and their fields."""
    for t in TYPES.values():
        typer.secho(t.name, bold=True)
        typer.echo(f"  {t.description}")
        if t.required:
            typer.echo(f"  required: {', '.join(sorted(t.required))}")
        if t.optional:
            typer.echo(f"  optional: {', '.join(sorted(t.optional))}")
        if t.secret:
            typer.echo(f"  secret  : {', '.join(sorted(t.secret))}  (prompted, hidden)")


@cred_app.command("add")
def cred_add(
    name: str,
    type: str = typer.Option(..., "--type", "-t", help="database | basic | bearer | api_key | oauth2 | generic"),
    field: list[str] = typer.Option([], "--field", "-f", help="Non-secret field KEY=VALUE (repeatable)."),
    secret: list[str] = typer.Option([], "--secret", help="generic: name of a secret field (prompted)."),
    secret_from_env: list[str] = typer.Option([], "--secret-from-env",
                                              help="FIELD=ENV_VAR – read a secret from the environment (CI)."),
    expires_days: Optional[int] = typer.Option(None, help="Mark for rotation after N days."),
    provider: Optional[str] = typer.Option(None, help="Writable provider: vault | keyring | hashicorp."),
    description: str = "",
    config: str = CONFIG_OPT,
):
    """Store a credential. Secret fields are prompted with hidden input."""
    if type not in TYPES:
        raise typer.BadParameter(f"type must be one of {', '.join(TYPES)}")
    s = _svc(config)
    if any(c["name"] == name for c in _run(s.credentials.list)):
        _fail(Exception(f"credential '{name}' exists – use 'portwright cred rotate'"))
    fields = _collect_fields(type, field, secret_from_env)
    for k in secret:
        fields[k] = typer.prompt(k, hide_input=True, confirmation_prompt=True)
    where = _run(s.credentials.add, name, type, fields, secret_fields=secret, expires_days=expires_days,
                 provider=provider, description=description)
    _ok(f"Credential '{name}' ({type}) stored in {where}")
    typer.echo(f"Use it:  portwright source add <name> --type ... --credential {name}   "
               f"or  ${{CRED:{name}.<field>}}")


@cred_app.command("list")
def cred_list(config: str = CONFIG_OPT):
    """List credentials (metadata only – never secrets)."""
    creds = _run(_svc(config).credentials.list)
    if not creds:
        typer.echo("No credentials yet. Add one with 'portwright cred add'.")
    for c in creds:
        state = {"expired": "EXPIRED", "never": ""}.get(c["expiry_state"], f"expires in {c['days_left']}d")
        typer.echo(f"{c['name']:<20} {c['type']:<9} {c['provider']:<9} {state:<16} "
                   f"fields={','.join(c['fields'])}  used-by={','.join(c['used_by']) or '-'}")


@cred_app.command("show")
def cred_show(name: str, reveal: bool = typer.Option(False, "--reveal", help="Print secret values."),
              config: str = CONFIG_OPT):
    """Show one credential (secrets masked unless --reveal)."""
    s = _svc(config)
    info = _run(s.credentials.masked, name)
    if reveal:
        if not typer.confirm(f"Print secret values of '{name}' to the terminal?"):
            raise typer.Exit(1)
        info["fields"] = _run(s.credentials.get, name).fields
    typer.echo(yaml.safe_dump(info, sort_keys=False))


@cred_app.command("rotate")
def cred_rotate(name: str,
                field: list[str] = typer.Option([], "--field", "-f", help="Change a non-secret field KEY=VALUE."),
                secret_from_env: list[str] = typer.Option([], "--secret-from-env"),
                keep_secrets: bool = typer.Option(False, help="Only change the given --field values."),
                expires_days: Optional[int] = None,
                config: str = CONFIG_OPT):
    """Replace a credential's secret(s). Running servers pick it up within cache_ttl_seconds
    and rebuild their connection pools – no restart needed."""
    s = _svc(config)
    c = _run(s.credentials.get, name)
    changes = _collect_fields(c.type, field, secret_from_env, existing=c.fields,
                              prompt_all_secrets=not keep_secrets)
    if not keep_secrets:
        for k in c.secret_fields:
            changes[k] = typer.prompt(k, hide_input=True, confirmation_prompt=True)
    _run(s.credentials.rotate, name, changes, expires_days=expires_days)
    _ok(f"Credential '{name}' rotated")


@cred_app.command("remove")
def cred_remove(name: str, force: bool = typer.Option(False, "--force"), config: str = CONFIG_OPT):
    """Delete a credential (refuses while sources still use it, unless --force)."""
    _run(_svc(config).credentials.remove, name, force)
    _ok(f"Credential '{name}' removed")


@cred_app.command("test")
def cred_test(name: str, url: Optional[str] = typer.Option(None, help="HTTP types: URL to GET with the auth."),
              config: str = CONFIG_OPT):
    """Check that a credential works (DB login, OAuth token fetch, or an authenticated GET)."""
    ok, msg = _svc(config).credentials.test(name, url)
    if not ok:
        _fail(Exception(msg))
    _ok(msg)


@cred_app.command("rekey")
def cred_rekey(passphrase: bool = typer.Option(False, "--passphrase"), config: str = CONFIG_OPT):
    """Re-encrypt the vault under a new master key (key rotation)."""
    s = _svc(config)
    new = typer.prompt("New master passphrase", hide_input=True, confirmation_prompt=True) if passphrase else None
    n = _run(s.credentials.rekey, new)
    env_name = s.project.config.security.credentials.master_key_env
    _ok(f"Re-encrypted {n} credential(s); " + (f"update {env_name} on the server" if passphrase
                                                 else "new key written to .env"))
    typer.echo("Restart running servers so they use the new master key.")


# ------------------------------------------------------------------ loads
load_app = typer.Typer(help="Load data from a CSV/file source or endpoint into a database table, "
                            "with source-to-target column mapping.", no_args_is_help=True)
app.add_typer(load_app, name="load")


def _get_load(svc: Services, name: str):
    try:
        return svc.loads.get(name)
    except ServiceError:
        _fail(Exception(f"no load '{name}' – see 'portwright load list'"))


def _print_mapping(spec) -> None:
    rows = []
    for c in spec.columns:
        src = c.source if c.source is not None else f"= {c.value!r}"
        extra = []
        if c.format:
            extra.append(c.format)
        if c.transform:
            extra.append("transform=" + ",".join(t if isinstance(t, str) else next(iter(t)) for t in c.transform))
        if c.default is not None:
            extra.append(f"default={c.default!r}")
        if c.required:
            extra.append("required")
        if c.target.lower() in {k.lower() for k in spec.keys}:
            extra.append("KEY")
        if c.review:
            extra.append("REVIEW")
        rows.append((str(src), c.target, c.type, " ".join(extra)))
    if not rows:
        typer.echo("  (no columns mapped)")
        return
    w = [max(len(r[i]) for r in rows + [("SOURCE", "TARGET", "TYPE", "")]) for i in range(3)]
    typer.echo(f"  {'SOURCE'.ljust(w[0])}  ->  {'TARGET'.ljust(w[1])}  {'TYPE'.ljust(w[2])}  NOTES")
    for r in rows:
        line = f"  {r[0].ljust(w[0])}  ->  {r[1].ljust(w[1])}  {r[2].ljust(w[2])}  {r[3]}"
        typer.secho(line, fg="yellow" if "REVIEW" in r[3] else None)


def _print_report(rep, as_json: bool) -> None:
    import json as _json
    if as_json:
        typer.echo(_json.dumps(rep.to_dict(), indent=2, default=str))
        return
    color = {"success": "green", "dry_run": "cyan", "partial": "yellow", "failed": "red",
             "skipped": "blue"}[rep.status]
    typer.secho(f"{rep.status.upper()}  load={rep.load} run={rep.run_id} mode={rep.mode} ({rep.seconds}s)",
                fg=color, bold=True)
    typer.echo(f"  read={rep.read} mapped={rep.mapped} rejected={rep.rejected} "
               f"inserted={rep.inserted} updated={rep.updated} upserted={rep.upserted} deleted={rep.deleted}"
               + (f" dropped_duplicates={rep.dropped}" if rep.dropped else "")
               + (f" skipped_existing={rep.skipped}" if rep.skipped else ""))
    if rep.source_hash:
        state = {True: "CHANGED since the last load", False: "unchanged since the last load",
                 None: "first load"}[rep.source_changed]
        ver = f"v{rep.source_version}" if rep.source_version else "new data"
        typer.echo(f"  source: {ver} ({rep.source_hash[:12]}) – {state}"
                   + (f" (v{rep.last_loaded_version}, run {rep.last_loaded_run})" if rep.last_loaded_run else ""))
    if rep.keys:
        typer.echo(f"  key: {', '.join(rep.keys)}")
    if rep.table_rows is not None:
        typer.echo(f"  table had {rep.table_rows} rows before kill & fill")
    for w in rep.warnings:
        typer.secho(f"  ⚠ {w}", fg="yellow")
    if rep.rejects_file:
        typer.echo(f"  rejects: {rep.rejects_file}")
    if rep.error:
        if rep.status == "skipped":
            typer.secho(f"  {rep.error} (use --force to load anyway)", fg="blue")
        else:
            typer.secho(f"  ✗ {rep.error}", fg="red", err=True)
    for p in rep.preview:
        typer.echo(f"  -- record {p['record']}")
        typer.echo(f"     source: {p['source']}")
        typer.echo(f"     target: { {k: str(v) for k, v in p['target'].items()} }")


@load_app.command("create")
def load_create(
    name: str,
    to: str = typer.Option(..., "--to", help="Target SQL source (must be --read-write)."),
    table: str = typer.Option(..., "--table", help="Target table, e.g. hr.emp_transfers"),
    from_endpoint: Optional[str] = typer.Option(None, "--from-endpoint", help="Read rows from this GET endpoint."),
    from_source: Optional[str] = typer.Option(None, "--from-source", help="Read rows directly from this file source."),
    mode: str = typer.Option("insert", help="insert | replace | upsert"),
    key: list[str] = typer.Option([], "--key", help="Upsert key target column (repeatable)."),
    auto_map_: bool = typer.Option(False, "--auto-map", help="Suggest a mapping from the CSV header and table."),
    batch_size: int = 1000,
    max_rejects: int = 0,
    description: str = "",
    config: str = CONFIG_OPT,
):
    """Create a load job (optionally with an auto-suggested column mapping)."""
    svc = _svc(config)
    if svc.project.config.load_job(name):
        _fail(Exception(f"load '{name}' exists – edit it with 'portwright load map'"))
    draft = {"name": name, "description": description, "mode": "insert", "batch_size": batch_size,
             "max_rejects": max_rejects, "to": {"source": to, "table": table},
             "from": {"endpoint": from_endpoint} if from_endpoint else {"source": from_source}}
    _run(svc.loads.build, draft)
    sug = None
    if auto_map_:
        sug = _run(svc.loads.suggest, draft)
        draft["columns"] = sug.columns
    draft["mode"], draft["keys"] = mode, key
    try:
        spec = svc.loads.save(draft, force=True)
    except ServiceError as exc:
        _fail(Exception(f"{exc}\nTip: create with the default mode first, map the key columns, "
                        f"then set them with 'portwright load map {name} --mode upsert --key <col>'"))
    _ok(f"Load '{name}' created ({mode}: {from_endpoint or from_source} -> {to}.{table})")
    if sug:
        _print_mapping(spec)
        if sug.unmatched:
            typer.secho(f"  ⚠ source columns with no matching target (ignored): {sug.unmatched}", fg="yellow")
        if sug.missing_required:
            typer.secho(f"  ⚠ required target columns not mapped: {sug.missing_required}", fg="yellow")
        if any(c.review for c in spec.columns):
            typer.secho("  Yellow rows are guesses – confirm each with: "
                        f"portwright load map {name} --set \"<Source>=<target>\"", fg="yellow")
    typer.echo(f"Next: portwright load validate {name}  →  portwright load run {name} --dry-run --preview 3")


@load_app.command("map")
def load_map(
    name: str,
    set_: list[str] = typer.Option([], "--set", help='"Source Col=target[:type[:format]]"  e.g. "Eff Date=eff_dt:date:%m/%d/%Y"'),
    value: list[str] = typer.Option([], "--value", help='"target=constant" (also ${now}, ${today}, ${run_id})'),
    transform: list[str] = typer.Option([], "--transform", help='"target=trim,upper,null_if_empty"'),
    default: list[str] = typer.Option([], "--default", help='"target=value used when blank"'),
    required: list[str] = typer.Option([], "--required", help="Target column that must have a value."),
    remove: list[str] = typer.Option([], "--remove", help="Target column to unmap."),
    mode: Optional[str] = typer.Option(None, help="insert | replace (kill & fill) | upsert"),
    key: list[str] = typer.Option([], "--key", help="Load key target column, any mode (replaces existing keys)."),
    no_key: bool = typer.Option(False, "--no-key", help="Remove the load key."),
    duplicate_keys: Optional[str] = typer.Option(None, help="Same key twice in the source: reject | first | last"),
    existing_keys: Optional[str] = typer.Option(None, help="Insert mode, key already in table: reject | skip | fail"),
    on_unchanged: Optional[str] = typer.Option(None, help="Source unchanged since last load: skip | load | fail"),
    on_header_change: Optional[str] = typer.Option(None, help="Source columns changed: fail | warn"),
    stable_seconds: Optional[int] = typer.Option(None, help="Wait until the file is unchanged this long."),
    max_age_hours: Optional[float] = typer.Option(None, help="Fail if the file is older (0 = no limit)."),
    archive_keep: Optional[int] = typer.Option(None, help="Source versions kept as copies (0 = none)."),
    min_rows: Optional[int] = typer.Option(None, help="Kill & fill: refuse with fewer rows than this."),
    min_percent: Optional[float] = typer.Option(None, help="Kill & fill: refuse below this % of current rows."),
    config: str = CONFIG_OPT,
):
    """Add or change column mappings and settings of a load job."""
    svc = _svc(config)
    spec = _get_load(svc, name)
    cols = {c.target.lower(): c.model_dump(exclude_none=True) for c in spec.columns}

    def _col(target: str) -> dict:
        if target.lower() not in cols:
            _fail(Exception(f"target '{target}' is not mapped yet; map it first with --set or --value"))
        return cols[target.lower()]

    for item in set_:
        if "=" not in item:
            raise typer.BadParameter(f"--set expects \"Source=target[:type[:format]]\", got {item!r}")
        src, rhs = item.split("=", 1)
        parts = rhs.split(":", 2)
        entry = cols.get(parts[0].lower(), {})
        entry.update({"source": src.strip(), "target": parts[0].strip(), "review": False})
        entry.pop("value", None)
        if len(parts) > 1 and parts[1]:
            entry["type"] = parts[1]
        if len(parts) > 2:
            entry["format"] = parts[2]
        cols[parts[0].lower()] = entry
    for item in value:
        tgt, val = item.split("=", 1)
        entry = cols.get(tgt.lower(), {"target": tgt})
        entry.pop("source", None)
        entry.update({"value": val, "review": False})
        cols[tgt.lower()] = entry
    for item in transform:
        tgt, steps = item.split("=", 1)
        _col(tgt)["transform"] = [s.strip() for s in steps.split(",") if s.strip()]
    for item in default:
        tgt, val = item.split("=", 1)
        _col(tgt)["default"] = val
    for tgt in required:
        _col(tgt)["required"] = True
    for tgt in remove:
        if cols.pop(tgt.lower(), None) is None:
            _fail(Exception(f"target '{tgt}' is not mapped"))
    data = spec.model_dump(mode="json", by_alias=True, exclude_none=True)
    data["columns"] = list(cols.values())
    if mode:
        data["mode"] = mode
    if key:
        data["keys"] = key
    if no_key:
        data["keys"] = []
    for k, v in (("duplicate_keys", duplicate_keys), ("existing_keys", existing_keys)):
        if v is not None:
            data[k] = v
    tr, sf = data.setdefault("tracking", {}), data.setdefault("safety", {})
    for k, v in (("on_unchanged", on_unchanged), ("on_header_change", on_header_change),
                 ("stable_seconds", stable_seconds), ("archive_keep", archive_keep)):
        if v is not None:
            tr[k] = v
    if max_age_hours is not None:
        tr["max_age_hours"] = max_age_hours or None
    for k, v in (("min_rows", min_rows), ("min_percent", min_percent)):
        if v is not None:
            sf[k] = v
    new = _run(svc.loads.save, data, original=name, force=True)
    _ok(f"Load '{name}' updated")
    _print_mapping(new)


@load_app.command("show")
def load_show(name: str, yaml_: bool = typer.Option(False, "--yaml", help="Print the raw definition."),
              config: str = CONFIG_OPT):
    """Show a load job and its column mapping."""
    spec = _get_load(_svc(config), name)
    if yaml_:
        typer.echo(yaml.safe_dump(spec.model_dump(mode="json", by_alias=True, exclude_none=True), sort_keys=False))
        return
    src = spec.from_.endpoint and f"endpoint {spec.from_.endpoint}" or f"source {spec.from_.source}"
    typer.secho(f"{spec.name}: {src}  ->  {spec.to.source}.{spec.to.table}", bold=True)
    typer.echo(f"  mode={spec.mode} keys={spec.keys or '-'} batch_size={spec.batch_size} "
               f"max_rejects={spec.max_rejects} unmapped_source_columns={spec.unmapped_source_columns}")
    typer.echo(f"  duplicate_keys={spec.duplicate_keys} existing_keys={spec.existing_keys}"
               + (f" safety: min_rows={spec.safety.min_rows} min_percent={spec.safety.min_percent:g}"
                  if spec.mode == "replace" else ""))
    t = spec.tracking
    typer.echo(f"  tracking: on_unchanged={t.on_unchanged} on_header_change={t.on_header_change} "
               f"stable_seconds={t.stable_seconds} max_age_hours={t.max_age_hours or '-'} archive_keep={t.archive_keep}")
    _print_mapping(spec)


@load_app.command("list")
def load_list(config: str = CONFIG_OPT):
    """List load jobs."""
    items = _svc(config).loads.list()
    if not items:
        typer.echo("No loads yet. Create one with 'portwright load create'.")
    for l in items:
        typer.echo(f"{l['name']:<24} {l['from']:<22} -> {l['to_source']}.{l['table']:<28} {l['mode']:<8} "
                   f"{l['columns']} cols")


@load_app.command("remove")
def load_remove(name: str, config: str = CONFIG_OPT):
    """Delete a load job definition (the table and its data are not touched)."""
    svc = _svc(config)
    _get_load(svc, name)
    _run(svc.loads.remove, name, force=True)
    _ok(f"Load '{name}' removed")


@load_app.command("validate")
def load_validate(name: str, config: str = CONFIG_OPT):
    """Check the mapping against the real source columns and target table (writes nothing)."""
    svc = _svc(config)
    spec = _get_load(svc, name)
    chk = _run(svc.loads.validate, spec)
    for w in chk.warnings:
        typer.secho(f"⚠ {w}", fg="yellow")
    if chk.problems:
        for p in chk.problems:
            typer.secho(f"✗ {p}", fg="red", err=True)
        raise typer.Exit(1)
    _ok(f"Load '{name}' is valid ({len(spec.columns)} columns mapped, source has {chk.row_count} rows)")


@load_app.command("run")
def load_run(
    name: str,
    dry_run: bool = typer.Option(False, "--dry-run", help="Read, map and validate every row; write nothing."),
    preview: int = typer.Option(0, "--preview", help="Show N rows before/after mapping."),
    as_json: bool = typer.Option(False, "--json", help="Print the run report as JSON."),
    force: bool = typer.Option(False, "--force", help="Load even if the source is unchanged or its columns changed."),
    config: str = CONFIG_OPT,
):
    """Run a load job. Exit code: 0 = loaded, 2 = loaded with rejects, 1 = failed (rolled back),
    3 = skipped because the source has not changed since the last successful load."""
    svc = _svc(config)
    _get_load(svc, name)
    rep = _run(svc.loads.run, name, dry_run=dry_run, preview=preview, force=force)
    _print_report(rep, as_json)
    raise typer.Exit(rep.exit_code)


@load_app.command("history")
def load_history(name: Optional[str] = typer.Argument(None), last: int = typer.Option(10, "--last"),
                 config: str = CONFIG_OPT):
    """Show recent load runs."""
    runs = _svc(config).loads.history(name, last)
    if not runs:
        typer.echo("No runs yet.")
        return
    for r in runs:
        written = r["inserted"] + r["updated"] + r["upserted"]
        typer.echo(f"{r['started']}  {r['load']:<22} {r['status']:<8} read={r['read']:<6} "
                   f"written={written:<6} rejected={r['rejected']:<5} {r['seconds']}s  {r['run_id']}"
                   + (f"  ✗ {r['error'].splitlines()[0]}" if r.get("error") else ""))


@load_app.command("check")
def load_check(name: str, config: str = CONFIG_OPT):
    """Has the source changed since the last successful load? (reads the source, writes nothing)
    Exit code 0 = changed or never loaded, 3 = unchanged."""
    st = _run(_svc(config).loads.source_status, name)
    ver = f"v{st['version']}" if st["version"] else "new data (no version yet)"
    typer.echo(f"source now: {ver}  hash {st['hash'][:12]}  {st['rows']} rows"
               + (f"  file {st['file']} modified {st['modified']}" if st["file"] else ""))
    if st["changed"] is None:
        typer.secho("never loaded successfully", fg="cyan")
    elif st["changed"]:
        typer.secho(f"CHANGED since the last load (v{st['last_loaded_version']}, run {st['last_loaded_run']})",
                    fg="yellow")
    else:
        typer.secho(f"unchanged since the last load (v{st['last_loaded_version']}, run {st['last_loaded_run']})",
                    fg="green")
    for k, cols in st["header_change"].items():
        typer.secho(f"  columns {k}: {', '.join(cols)}", fg="yellow")
    raise typer.Exit(3 if st["changed"] is False else 0)


@load_app.command("versions")
def load_versions(name: str, last: int = typer.Option(20, "--last"), config: str = CONFIG_OPT):
    """List the versions of a load's source and the runs that read each one."""
    vs = _run(_svc(config).loads.versions, name)[:last]
    if not vs:
        typer.echo("No versions yet – they are recorded by each real run.")
        return
    for v in vs:
        runs = ", ".join(f"{r['run_id']}:{r['status']}" for r in v["runs"][-3:]) or "-"
        hc = "; ".join(f"{k} {', '.join(c)}" for k, c in v["header_change"].items())
        typer.echo(f"v{v['version']:<4} {v['first_seen']}  {v['rows']:>7} rows  {v['hash'][:12]}  "
                   f"{'archived' if v['archived'] else 'no copy ':<8}  {'LOADED' if v['loaded'] else 'not loaded'}"
                   f"  runs: {runs}" + (f"  columns: {hc}" if hc else ""))


@load_app.command("diff")
def load_diff(name: str, old: int = typer.Argument(..., help="older version number"),
              new: int = typer.Argument(..., help="newer version number"), config: str = CONFIG_OPT):
    """Compare two archived source versions (by the load key, or by whole rows)."""
    d = _run(_svc(config).loads.diff, name, old, new)
    by = f"key {', '.join(d['key'])}" if d["key"] else "whole rows (no load key mapped from source columns)"
    typer.secho(f"v{old} -> v{new} compared by {by}", bold=True)
    typer.echo(f"  added={d['added']} removed={d['removed']} changed={d['changed'] if d['changed'] is not None else '-'}"
               f" unchanged={d['unchanged']}")
    for r in d["added_rows"][:10]:
        typer.secho(f"  + {r}", fg="green")
    for r in d["removed_rows"][:10]:
        typer.secho(f"  - {r}", fg="red")
    for c in d["changed_rows"][:10]:
        typer.secho(f"  ~ {c['key']}: " + ", ".join(f"{f}: {c['before'][f]!r} -> {c['after'][f]!r}"
                                                   for f in c["fields"]), fg="yellow")


@load_app.command("rejects")
def load_rejects(run_id: str, last: int = typer.Option(20, "--limit", help="Rows to show."),
                 config: str = CONFIG_OPT):
    """Show the rows a run rejected and why."""
    cols, rows, total = _run(_svc(config).loads.rejects, run_id, last)
    typer.echo(f"{total} rejected row(s) in run {run_id}" + (f" (showing {len(rows)})" if total > len(rows) else ""))
    for r in rows:
        data = {k: v for k, v in r.items() if k not in ("record", "reason")}
        typer.secho(f"  record {r['record']}: {r['reason']}", fg="red")
        typer.echo(f"     {data}")



# ------------------------------------------------------------------ users
@user_app.command("add")
def user_add(username: str, scope: list[str] = typer.Option([], "--scope"),
             password: str = typer.Option(..., prompt=True, hide_input=True, confirmation_prompt=True),
             config: str = CONFIG_OPT):
    """Add a user who can obtain JWTs via POST /auth/token."""
    try:
        _svc(config).access.add_user(username, password, scope)
    except ServiceError as exc:
        raise typer.BadParameter(str(exc))
    _ok(f"User '{username}' added")


@user_app.command("remove")
def user_remove(username: str, config: str = CONFIG_OPT):
    _run(_svc(config).access.remove_user, username)
    _ok(f"User '{username}' removed")


# --------------------------------------------------------- validate/call/serve
@app.command()
def validate(config: str = CONFIG_OPT):
    """Validate the project (schema, adapters, security rules) without starting the server."""
    v = _svc(config).runtime.validate()
    if not v.ok:
        _fail(Exception(v.message))
    _ok(v.message)
    _warn(v.warnings)


@app.command()
def call(
    endpoint: str = typer.Argument(..., help="Endpoint name (see 'portwright endpoint list')."),
    param: list[str] = typer.Option([], "--param", "-p", help="NAME=VALUE (repeatable): path, query or body param."),
    body: Optional[str] = typer.Option(None, "--body", help="Raw JSON object for the request body (or @file.json)."),
    limit: Optional[int] = typer.Option(None, help="Page size for list endpoints."),
    offset: Optional[int] = typer.Option(None, help="Rows to skip for list endpoints."),
    table: bool = typer.Option(False, "--table", help="Print 'items' as a table instead of JSON."),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Write the JSON result to a file."),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="No output; exit code only."),
    config: str = CONFIG_OPT,
):
    """Run an endpoint in-process – no server needed. Exit code 0 on success, 1 on error.

    Runs with full (admin) rights because whoever can read this project's .env and
    vault already controls it; params are still validated and the call is audited.
    """
    import json as _json
    s = _svc(config)
    if s.project.config.endpoint(endpoint) is None:
        _fail(Exception(f"no endpoint '{endpoint}' – see 'portwright endpoint list'"))
    json_body = None
    if body:
        raw = Path(body[1:]).read_text(encoding="utf-8") if body.startswith("@") else body
        try:
            json_body = _json.loads(raw)
        except Exception as exc:
            _fail(Exception(f"--body is not valid JSON: {exc}"))
    values = _kv(param)
    ep = s.project.config.endpoint(endpoint)
    missing = [p.name for p in ep.params if p.location == "path" and p.name not in values]
    if missing:
        _fail(Exception(f"missing path param '{missing[0]}' (use -p {missing[0]}=VALUE)"))
    res = _run(s.runtime.call, endpoint, values, body=json_body, limit=limit, offset=offset, actor="cli")
    data = res.data
    if output:
        output.write_text(_json.dumps(data, indent=2, default=str), encoding="utf-8")
    if not quiet:
        if not res.ok:
            typer.secho(f"✗ HTTP {res.status}", fg="red", err=True)
            typer.echo(_json.dumps(data, indent=2, default=str), err=True)
        elif table and isinstance(data, dict) and isinstance(data.get("items"), list):
            rows = data["items"]
            if rows:
                cols = list(rows[0].keys())
                widths = {c: max(len(str(c)), *(len(str(r.get(c, ""))) for r in rows)) for c in cols}
                typer.echo("  ".join(str(c).ljust(widths[c]) for c in cols))
                typer.echo("  ".join("-" * widths[c] for c in cols))
                for r in rows:
                    typer.echo("  ".join(str(r.get(c, "")).ljust(widths[c]) for c in cols))
            typer.echo(f"({len(rows)} rows)")
        elif not output:
            typer.echo(_json.dumps(data, indent=2, default=str))
        else:
            _ok(f"HTTP {res.status} – result written to {output}")
    raise typer.Exit(0 if res.ok else 1)


@app.command()
def docs(topic: Optional[str] = typer.Argument(None, help="Topic, e.g. loads, endpoints, cli-reference."),
         search: Optional[str] = typer.Option(None, "--search", "-s", help="Find topics containing these words."),
         part: str = typer.Option("all", "--part", help="all | studio | cli"),
         plain: bool = typer.Option(False, "--plain", help="Raw Markdown (for redirecting to a file)."),
         export: Optional[Path] = typer.Option(None, "--export", help="Write the whole guide to one Markdown file.")):
    """The portwright guide (the same text as Studio's Help screen), with CLI examples."""
    from . import docs as guide
    if export:
        out = ["# portwright guide\n"]
        for t in guide.topics():
            t = guide.get(t.slug)
            out.append(f"\n## {t.title}\n\n{t.intro}\n")
            if t.studio:
                out.append(t.studio.replace("## In Studio", "### In Studio") + "\n")
            if t.cli:
                out.append(t.cli.replace("## With the CLI", "### With the CLI") + "\n")
        export.write_text("\n".join(out), encoding="utf-8")
        _ok(f"Guide written to {export}")
        return
    if search:
        hits = guide.search(search)
        if not hits:
            typer.echo(f"Nothing found for '{search}'.")
        for slug, title, snippet in hits:
            typer.secho(f"{slug:<18} {title}", bold=True)
            typer.echo(f"    {snippet}")
        return
    if not topic:
        typer.echo("Topics (portwright docs <topic>):")
        for t in guide.topics():
            typer.echo(f"  {t.slug:<18} {t.title}")
        return
    try:
        t = guide.get(topic)
    except KeyError:
        names = ", ".join(x.slug for x in guide.topics())
        raise typer.BadParameter(f"unknown topic '{topic}'. Topics: {names}")
    pieces = [f"# {t.title}", t.intro] if part == "all" else [f"# {t.title}"]
    if part in ("all", "studio") and t.studio:
        pieces.append(t.studio)
    if part in ("all", "cli") and t.cli:
        pieces.append(t.cli)
    text = "\n\n".join(p for p in pieces if p)
    if plain:
        typer.echo(text)
        return
    try:
        from rich.console import Console
        from rich.markdown import Markdown
        Console().print(Markdown(text, code_theme="ansi_dark"))
    except Exception:          # pragma: no cover - rich missing
        typer.echo(text)


@app.command()
def studio(config: str = CONFIG_OPT,
           web: bool = typer.Option(False, "--web", help="Open in the browser (localhost only) instead of a window."),
           port: int = typer.Option(8550, help="Port for --web mode."),
           client_path: Optional[Path] = typer.Option(None, "--client-path",
                                                      help="Folder containing flet.exe (offline desktop client).")):
    """Open Portwright Studio – the desktop app for sources, credentials, endpoints and keys."""
    try:
        from .studio.app import launch
    except ImportError as exc:
        _fail(Exception(f"Studio needs the GUI extra: pip install portwright[gui]  ({exc})"))
    launch(config_path=config, web=web, port=port, client_path=client_path)




@app.command()
def serve(config: str = CONFIG_OPT,
          host: Optional[str] = typer.Option(None, help="Default from config (127.0.0.1)."),
          port: Optional[int] = None,
          reload: bool = typer.Option(False, help="Auto-reload on config/code changes (dev only)."),
          workers: int = 1):
    """Run the API server."""
    import uvicorn
    cfg_path = Path(config).resolve()
    load_dotenv(cfg_path.parent / ".env")
    cfg = _load(str(cfg_path))
    os.environ["PORTWRIGHT_CONFIG"] = str(cfg_path)
    os.chdir(cfg_path.parent)
    h = host or cfg.server.host
    if h in ("0.0.0.0", "::") and not cfg.server.ssl_certfile:
        typer.secho("⚠ Listening on all interfaces without TLS – put a TLS proxy in front.", fg="yellow")
    uvicorn.run("portwright.server:app_from_env", factory=True, host=h, port=port or cfg.server.port,
                reload=reload, reload_includes=["*.yaml", "*.py"] if reload else None,
                workers=None if reload else workers, server_header=False, proxy_headers=True,
                ssl_certfile=cfg.server.ssl_certfile, ssl_keyfile=cfg.server.ssl_keyfile)


if __name__ == "__main__":
    app()
