# Portwright

**Author:** Sukarna Chinchod

Declarative, secure REST endpoints for **SQL databases, files (CSV/JSON/Excel), upstream REST APIs and Python functions** – created from a CLI, served by FastAPI, documented automatically with OpenAPI.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the design patterns and the security model.


> **Renamed from restforge.** The command is now `portwright`, with the short alias **`pw`**
> (`pw serve`, `pw call transfer-list`, `pw load run xfer`). `restforge` still works, and existing
> projects need no changes: `restforge.yaml`, `RESTFORGE_*` variables in `.env`, the `.restforge`
> vault and `rf_` API keys are all still recognised. New projects use `portwright.yaml`,
> `PORTWRIGHT_*` variables and `pw_` keys.

## Install
```bash
pip install -e .            # Python 3.11+
pip install -e .[postgres]  # or [mysql], [oracle] for DB drivers
```

## Quick start
(The commands work the same in PowerShell; keep `'${ENV:...}'` in single quotes.)

```bash
portwright init hr-api --name hr-api && cd hr-api
# .env already contains a generated JWT secret and vault master key
portwright cred add hr_db -t database -f driver=sqlite -f database=data/hr.db

# 1. data sources
portwright source add hr      --type sql      --credential hr_db --read-write
portwright source add sales   --type file     --path sales.csv              # under ./data
portwright source add pricing --type callable --module handlers.pricing     # ./handlers/pricing.py
portwright source add todos   --type rest     --base-url https://jsonplaceholder.typicode.com --allow-host

# 2. endpoints
portwright endpoint crud employees -s hr --table employees --key id \
    --field name:string:required --field dept --field salary:number --filter dept
portwright endpoint add dept-summary -s hr -p /reports/departments --param dept --scope reports:read \
    --query "select dept, count(*) n from employees where (:dept is null or dept = :dept) group by dept"
portwright endpoint add sales-list -s sales -p /sales --filter region --columns order_id,region,amount --scope sales:read
portwright endpoint add quote -s pricing -p /quote/{symbol} --target quote --param symbol:string:path --param qty:integer --scope pricing:read
portwright endpoint add todo  -s todos -p /todos/{id} --param id:integer:path --upstream-path /todos/{id} --scope todos:read

# 3. credentials
portwright key create reporting --scope employees:read --scope reports:read --expires-days 90
portwright user add alice --scope employees:read --scope employees:write   # prompts for password

# 4. check & run
portwright validate
portwright endpoint list
portwright serve --port 8080          # docs at http://127.0.0.1:8080/api/docs
```

### Calling the API
```bash
curl -H "X-API-Key: pw_xxxxxxxx_..." "http://127.0.0.1:8080/api/employees?dept=ENG&limit=20"

TOKEN=$(curl -s -X POST localhost:8080/api/auth/token -H 'content-type: application/json' \
        -d '{"username":"alice","password":"..."}' | jq -r .access_token)
curl -X POST -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
     -d '{"name":"Dev","dept":"ENG","salary":120000}' localhost:8080/api/employees
```

## Credential management
Connection secrets (DB logins, API tokens, OAuth clients…) are stored as **typed, encrypted credentials** and
referenced by name – they never appear in `portwright.yaml`.

```bash
portwright cred types                                   # database | basic | bearer | api_key | oauth2 | generic

# Oracle login – password is prompted (hidden) and special characters are escaped for you
portwright cred add hr_oracle -t database -f driver=oracle+oracledb -f host=db.corp.local -f port=1521 \
    -f service_name=ORCLPDB1 -f username=hr_api -f schema=hr --expires-days 90
portwright cred test hr_oracle                          # real login check
portwright source add hr -t sql --credential hr_oracle

# Upstream APIs
portwright cred add crm_token -t bearer                 # prompts for token
portwright cred add weather -t api_key -f query_param=appid
portwright cred add erp -t oauth2 -f token_url=https://login.corp.com/oauth2/token -f client_id=portwright -f scope=api.read
portwright source add crm -t rest --base-url https://api.crm.com --allow-host --credential crm_token

# Anything else: reference fields anywhere in the config
portwright cred add partner -t generic --secret signing_key -f tenant=acme
portwright source add partner -t rest --base-url https://api.partner.com --allow-host \
    --header 'X-Tenant=${CRED:partner.tenant}' --header 'X-Signature=${CRED:partner.signing_key}'

# Python handlers get ONLY the credentials you allow
portwright source add billing -t callable --module handlers.billing --allow-credential stripe
#   def charge(amount: int, ctx=None): key = ctx.credentials.field("stripe", "key")

portwright cred list          # names, types, expiry, which sources use them – never secrets
portwright cred show NAME     # masked; --reveal asks for confirmation
portwright cred rotate NAME   # new secret; running servers pick it up and rebuild pools, no restart
portwright cred rekey         # re-encrypt the whole vault under a new master key
portwright cred remove NAME   # refuses while a source still uses it
```

**Where credentials live** (`security.credentials.providers`, checked in order – first match wins):

| Provider | Use for | Setup |
|---|---|---|
| `vault` (default) | Local/VPS installs. `.portwright/credentials.vault`, AES-256-GCM per credential | master key in `.env` (`PORTWRIGHT_MASTER_KEY`, created by `init`) |
| `env` (default) | Docker/Kubernetes/CI. `RF_CRED_<NAME>_TYPE`, `RF_CRED_<NAME>_<FIELD>` | set env vars |
| `keyring` | Windows Credential Manager / macOS Keychain | `pip install -e .[keyring]` |
| `hashicorp` | Enterprise secret store (KV v2) | `VAULT_ADDR`, `VAULT_TOKEN` |

Non-interactive (CI): `portwright cred add db -t database ... --secret-from-env password=DB_PASSWORD`.

## CLI reference
| Command | Purpose |
|---|---|
| `init [DIR] --name` | New project: `portwright.yaml`, `.env` (generated JWT secret), `.gitignore`, `data/`, `handlers/`, `logs/` |
| `source add/list/remove` | Register SQL / file / REST / callable sources |
| `endpoint add` | One endpoint. `--param name:type[:query\|path\|body][:required]`, `--scope`, `--public`, `--rate-limit` |
| `endpoint crud` | list/get/create/update/delete for a table with read/write scopes (`--read-only` for list+get) |
| `endpoint list/show/remove` | Inspect and manage endpoints |
| `key create/list/revoke` | API keys (plaintext shown once, hash stored, default 90-day expiry) |
| `user add/remove` | Users for JWT login (`POST /api/auth/token`) |
| `cred init/types/add/list/show/rotate/test/rekey/remove` | Connection credentials (see above) |
| `source set-credential SOURCE [CRED]` | Attach/detach a credential on an existing source |
| `validate` | Full build of the app without serving – use in CI |
| `studio [--web] [--client-path DIR]` | Open Portwright Studio (GUI) |
| `docs [TOPIC]` | The user guide in the terminal (same text as Studio's Help screen); `docs cli-reference` lists every command |
| `source test NAME` · `endpoint set-access NAME --public/--scope` | Test a source; change who may call an endpoint |
| `load create/map/show/list/validate/run/history/rejects/check/versions/diff/remove` | Load jobs: CSV/endpoint → mapped columns → database table |
| `call ENDPOINT [-p NAME=VALUE]… [--body JSON] [--table] [-o FILE]` | Run an endpoint directly, no server (scripts, schedulers); exit code 0/1 |
| `serve [--reload] [--workers]` | Run with uvicorn |

All commands accept `-c/--config` to point at another project file.

## Portwright Studio (desktop app)
A Flet-based GUI over the same service layer as the CLI – anything done in Studio is exactly what the
equivalent CLI command does (same validation, same files, same audit log).

```powershell
pip install -e .[gui]                 # adds Flet 1.0.4 (offline: see the Studio add-on bundle)
cd C:\apps\hr-api
portwright studio                      # desktop window
portwright studio --web                # browser on this PC only (127.0.0.1 + one-time token link)
portwright studio --client-path C:\tools\flet-client\flet   # pre-downloaded desktop client (offline)
```

| Screen | What you can do |
|---|---|
| Dashboard | **Getting started** checklist (each step links to its screen), counts, expiring credentials/keys, public endpoints, recent load runs, **Validate project** |
| Sources | add/edit/delete SQL, file, REST and callable sources; **Browse** copies a file into `data\`; **Test** connects or previews the first rows |
| Credentials | typed forms (database fields follow the driver), masked secrets, **Test**, **Rotate**, expiry badges, create the vault master key |
| Endpoints | **builder** for every source type: SQL table (CRUD wizard), custom SQL query (`:binds` become parameters, **Test query**), file (filter/key fields), REST upstream, Python function; parameter grid (type, location, required, default, min/max, length, allowed values, pattern); scopes or public, rate limit; **Try it** in-process; YAML, CLI command and PowerShell/curl snippets; Duplicate; next-step prompts (API key with the right scope, Try it, Create a load) |
| Loads | create load jobs, **mapping designer** (one line per table column, Auto-map, constants, date formats, clean-up/lookup options, upsert keys), **Validate**, **Preview** (dry run, before/after rows), **Run** with result summary, history and a **rejected-rows viewer** |
| API keys | create keys with a scope picker, **show-once** dialog with Copy (clipboard cleared after 60 s), revoke; users for JWT login |
| Help | the full guide – every topic in two tabs, **In Studio** and **With the CLI** (copyable PowerShell examples), search, and a CLI reference generated from the installed commands. Every screen has a **?** button to its topic |

Studio notices when `portwright.yaml` is changed elsewhere (CLI, Notepad) and offers **Reload**. Every
save is validated, written atomically and the previous version kept as `portwright.yaml.bak`.

## Running endpoints without a server
```bash
portwright call employees-list -p department_id=50 --limit 20 --table
portwright call employees-get -p employee_id=101
portwright call employees-create -p first_name=Dev -p last_name=Rao
portwright call employees-update -p employee_id=101 --body '{"department_id": 60}'
portwright call dept-headcount -o headcount.json -q      # write result to a file, exit code only
```
`call` runs the same pipeline as the server (validation, data access, audit log) inside the CLI process.
It runs with admin rights – anyone who can read the project's `.env` and vault already has them – and
exits with 0 on success and 1 on any error, so schedulers can check the result.

## Loading data into a database table (load jobs)
A load job reads rows from a file source (CSV/JSON/Excel) or any GET endpoint (all pages), maps each
source column to a target column, validates every row and writes to a table in ONE transaction.

```powershell
# target source must allow writes
portwright source add hr_rw -t sql --credential hr_oracle --read-write

# 1. create (auto-map suggests matches from the CSV header and the table; guesses are flagged REVIEW)
portwright load create transfers-to-db --from-endpoint transfer-list --to hr_rw --table hr.emp_transfers --auto-map

# 2. map source -> target where names differ  ("Source=target[:type[:format]]")
portwright load map transfers-to-db --set "Emp No=employee_id:integer" --set "Eff Date=effective_dt:date:%m/%d/%Y" `
  --set "Dept=dept_code" --transform "dept_code=trim,upper" --default "amount_usd=0" --required employee_id `
  --value "load_source=CSV" --value 'loaded_at=${now}' --mode upsert --key transfer_id

# 3. check, preview, run
portwright load validate transfers-to-db                 # vs CSV header + real table (NOT NULL, lengths, keys)
portwright load run transfers-to-db --dry-run --preview 3
portwright load run transfers-to-db                      # exit 0 = ok, 2 = loaded with rejects, 1 = failed/rolled back
portwright load history transfers-to-db
portwright load rejects 20261017-070035-419658          # rows a run rejected and why
portwright load check transfers-to-db                    # has the source changed since the last load? (exit 3 = no)
portwright load versions transfers-to-db                 # every version of the source and the runs that read it
portwright load diff transfers-to-db 3 4                 # rows added / removed / changed between two versions
portwright load run transfers-to-db --force              # load even though the source is unchanged
```

**Exit codes of `load run`** (for Stonebranch): `0` loaded · `2` loaded, some rows rejected · `1` failed,
nothing written · `3` skipped – the source has not changed since the last successful load.

### Load key, modes and kill & fill
* **Load key** (`keys`, any mode; Studio: the *Key* column of the mapping grid). Checked before anything is
  written, even when the table has no primary key: an empty key is rejected; a key found twice in the source
  is handled by `duplicate_keys` (`reject` all copies – default, `first`, `last`); in Append mode a key
  already in the table is handled by `existing_keys` (`reject` – default, `skip`, `fail`). With no key set,
  the table's primary key is used when all its columns are mapped. Upsert also checks that the key is not
  duplicated inside the table.
* **Modes**: `insert` = Append · `replace` = **Kill & fill** (DELETE all rows + INSERT in one transaction; a
  failure keeps the old rows) · `upsert`.
* **Kill & fill safety** (`safety.min_rows`, `safety.min_percent`): refuse to empty the table when the file
  has fewer rows than N, or less than X % of the rows currently in the table.

```powershell
portwright load map transfers-to-db --mode replace --key transfer_id --duplicate-keys reject --min-percent 50
portwright load map transfers-to-db --mode insert --existing-keys skip      # "insert new only"
```

### Source versioning and change detection
Every real run fingerprints the source (hash of the rows read, file size, modified time, SHA-256 of the file,
column list) and stores each distinct content as a numbered version in `logs\loads\versions\<load>.jsonl`,
with a compressed copy in `logs\loads\archive\<load>\` (last `archive_keep`, default 30). The run report
and history show the version and whether it changed since the last successful load.

| `tracking:` setting | Default | Meaning |
|---|---|---|
| `on_unchanged` | `skip` | same data as the last successful load: `skip` (exit 3), `load`, `fail` |
| `on_header_change` | `fail` | columns added/removed since that version: `fail` or `warn` |
| `stable_seconds` | `0` | wait until the file has not changed for N seconds (still being copied) |
| `max_age_hours` | none | fail if the file was last changed longer ago (yesterday's file?) |
| `archive_keep` | `30` | versions kept as copies for `load diff` (0 = fingerprint only) |

CLI: `portwright load map NAME --on-unchanged load --stable-seconds 60 --max-age-hours 26 --archive-keep 10`.
REST: `POST /api/_loads/{name}/run?force=true`.

In Studio the same job is built on the **Loads** screen: pick the file source or endpoint, the target SQL
source and table (**Browse tables**), press **Read columns**, then **Auto-map** and fix the lines the grid
flags (red = must fix, yellow = check), and tick the **Key** columns. *How rows are written* holds the mode,
the key checks and the kill & fill safety; *Source versions* holds the change-detection settings, **Check
source now**, the version list and **Compare** (rows added / removed / changed between versions).
**Preview** maps every row without writing; **Run** writes in one transaction and shows the counts; a run
skipped because the source is unchanged offers **Run anyway**. **Check sources** on the Loads list shows
for every load whether its source changed since it was last loaded.

| Setting | Values |
|---|---|
| `mode` | `insert` (append) · `replace` (DELETE + INSERT in one transaction) · `upsert` on `keys` (Oracle/SQL Server `MERGE`, Postgres/SQLite `ON CONFLICT`, MySQL `ON DUPLICATE KEY`) |
| column `type` | `string`, `integer`, `number`, `boolean`, `date`, `timestamp` (+ `format`, e.g. `%m/%d/%Y`) |
| `transform` | `trim`, `upper`, `lower`, `null_if_empty`, `{replace: [old, new]}`, `{lookup: {A: Active, _default: Other}}` |
| `value` | constant, `${now}`, `${today}`, `${run_id}` |
| `max_rejects` | bad rows allowed before the whole load is refused (default 0). Rejects go to `logs\loads\<run>_rejects.csv` with the reason |
| `unmapped_source_columns` | `ignore` (default) or `error` |

Over REST: `POST /api/_loads/{name}/run[?dry_run=true]` and `GET /api/_loads` (scope `load:run`).
Validation blocks a run when a mapped column is missing on either side, a NOT NULL column without a
default is unmapped, an auto-map guess is unconfirmed, or upsert keys are not a primary/unique key.
Text longer than the column allows becomes a reject instead of a database error.

## Filling an Active Directory group (group loads)

A load can target an **AD group** instead of a table. The source (a query endpoint or a file) gives one
e-mail address per row; each address is matched to an AD user (`mail`, then `userPrincipalName`) and the
group's members are made to match.

```powershell
portwright source add corp_ad -t ldap --server ldaps://dc01.corp.local --base-dn "DC=corp,DC=local" --credential ad_svc
portwright load create hr-staff-group --from-endpoint hr-staff-emails --to corp_ad --ad-group HR-Staff `
    --email-column email --mode sync --max-rejects 25
portwright load run hr-staff-group --dry-run     # lists + adds and - removes
portwright load run hr-staff-group
```

| Setting | Values |
|---|---|
| `mode` | `replace` kill & fill (one atomic `member` replace) · `sync` add missing + remove extra · `insert` add only |
| `ad.email_column` | source column with the addresses (default `email`) |
| `ad.match` | `[mail, userPrincipalName]` (default); `proxyAddresses` can be added |
| `ad.max_remove_percent` | refuse the run if more of the managed members would be removed (default **20**) |
| `ad.protected` / `ad.exception_group` | accounts / a group whose members are never removed |
| `ad.keep_nested_groups` | nested groups stay members (default true); computers, contacts and foreign principals always stay |
| `ad.reject_disabled` | disabled accounts are rejected (default true) |

The `ldap` source connects with **LDAPS** and a *basic* service-account credential (certificate checked
against the Windows store or `ca_file`), or with `directory: powershell` through the RSAT ActiveDirectory
module as the Windows account the process runs as. The account needs **Write Members** on the group only.
Invalid, unknown, ambiguous or disabled addresses go to the rejects file; source versioning, exit codes and
history work as for table loads. Full guide: `portwright docs ad-groups`.

## Loading folders of files (BLOB / CLOB)

A `folder` source makes every matching file a source row: `file_name`, `file_path`, `size_bytes`,
`modified`, `sha256`, `mime_type`, and the content as bytes (`content` → **binary**: Oracle BLOB, SQL Server
VARBINARY(MAX), PostgreSQL BYTEA, MySQL LONGBLOB) or text (`content_text` → **text**: CLOB / NVARCHAR(MAX) /
TEXT / LONGTEXT). With `files.read: records`, each CSV/Excel/JSON file is parsed and its rows are loaded instead.

```powershell
pw source add hr_docs -t folder --path inbound --include "*.pdf" --recursive
pw load create docs --from-source hr_docs --to hr_rw --table HR.EMP_DOCS --auto-map
pw load map docs --set "content=DOC_BLOB:binary" --on-changed replace --file-key FILE_PATH
pw load run docs --dry-run && pw load run docs
pw load files docs --ledger
```

A ledger per load (path, size, time, SHA-256) means a file is never loaded twice, even if it is renamed or
copied back. There is an optional check against a sha256 column in the target table. A changed file is
loaded as a new version, or replaces its earlier rows (`on_changed`). Each file is committed on its own;
failed files go to `rejected\` with a reason, and loaded files are moved to `processed\<date>\` (or left,
or deleted). Exit codes: 0 ok, 2 some files failed, 1 nothing loaded, 3 nothing new. Guide: `pw docs folders`.

## Activity log and moving a setup to another instance

Every setup change – from Studio, the CLI, a migration import, or a hand edit of `portwright.yaml` – is
logged in `logs\setup.jsonl` with the time, Windows user, computer, what changed and the equivalent
`pw` command (never secrets). Studio shows it on the **Activity** screen; the CLI with `pw activity`.

```powershell
pw activity --since 2026-10-01 --commands       # who changed what, and the command for it
pw config get load sccm-group                   # one object as YAML;  pw config put / remove
pw migrate script -o setup.ps1 [--since DATE | --full]   # replay script for the target
pw migrate export --include-data                # migration bundle (zip) – no secrets, keys or users
pw migrate plan  bundle.zip --vars prod.vars.yaml -c C:\apps\hr-api\portwright.yaml
pw migrate import bundle.zip --vars prod.vars.yaml -c C:\apps\hr-api\portwright.yaml
```

Values that differ per instance go in a variables file of dotted paths (`sources.hr.credential: hr_prod`,
`sources.corp_ad.servers: [ldaps://proddc01]`, `server.port: 8443`); `pw migrate vars` prints a template.
Imports back up `portwright.yaml` to `backups\`, apply in one validated save, keep existing credentials and
list the missing ones (`--prompt-credentials` asks for them). Full guide: `pw docs migration`.

## Custom functions
```python
# handlers/pricing.py
from portwright import SourceError

def quote(symbol: str, qty: int = 1, ctx=None):   # ctx is optional: principal, request_id, …
    price = {"AAPL": 190.0}.get(symbol.upper())
    if price is None:
        raise SourceError("Unknown symbol", 404)
    return {"symbol": symbol.upper(), "total": price * qty}
```

## Embedding
```python
from portwright import create_app
app = create_app("portwright.yaml")   # a normal FastAPI app – mount it, add routes, test with TestClient
```

## Tests
```bash
pip install -e .[dev] && pytest
```

## Production checklist
* Run behind TLS (Traefik/Nginx) or set `server.ssl_certfile/ssl_keyfile`.
* Use least-privilege DB accounts; keep SQL sources `read_only` unless writes are required.
* Back up `PORTWRIGHT_MASTER_KEY` (password manager) – without it the vault cannot be decrypted.
* Keep `.env` out of Git; rotate API keys (`key revoke` + `key create`).
* Set `security.expose_docs: false` if the OpenAPI schema should not be public.
* With multiple workers/instances, plug in a shared (Redis) `RateLimiter`.

## Author

Portwright is written and maintained by **Sukarna Chinchod**.
