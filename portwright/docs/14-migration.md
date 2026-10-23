---
title: Activity log and migration
screen: activity
---
Every change to a project's setup is recorded in `logs\setup.jsonl` – whether it was made in Studio,
with the CLI, by a migration import, or by editing `portwright.yaml` by hand (detected the next time the
project is opened). Each entry has the time, the Windows user and computer, how it was made, what
changed, and the **pw command** that makes the same change on another instance. Secrets are never logged.

| Logged | Command recorded |
|---|---|
| sources, endpoints, loads | `pw config put <kind> <name>` with the definition as YAML, or `pw config remove` |
| server / security settings | `pw config put settings server` / `security` (API keys and users are not part of it) |
| credentials | `pw cred add <name> -t <type> -f <non-secret fields>` – secrets are asked for on the target |
| API keys, users | written as comments: a key is always created new on each instance |

**Moving a setup to another instance** (dev → test → prod) – two ways:

1. **Replay script** – `pw migrate script` turns the log (all of it, or `--since` a date) into a PowerShell
   script; `--full` scripts the whole current setup instead. Each object appears once with its latest
   definition, in dependency order (credentials → sources → endpoints → loads).
2. **Migration bundle** – `pw migrate export` writes one zip: the setup, credential names and non-secret
   fields, `handlers\`, optionally `data\`, a variables template and a replay script. On the target,
   `pw migrate plan` shows exactly what would be created, changed or removed; `pw migrate import` backs up
   `portwright.yaml` to `backups\`, applies everything in one validated save and lists missing credentials.

**Values that differ per instance** go in a variables file of dotted paths (start from the template the
bundle contains, `pw migrate vars`):

```yaml
server.port: 8443
sources.hr.credential: hr_oracle_prod
sources.corp_ad.servers: [ldaps://proddc01.corp.local]
sources.corp_ad.base_dn: DC=corp,DC=local
loads.sccm-group.to.group: CM.VAHN.SCCM
credentials.hr_oracle.host: prod-db.corp.local
```

Pass it with `--vars` (plan, import, `config put`, `cred add`) or `-Vars` (the replay script).

**Never moved**: passwords and tokens (prompted or added on the target), API keys and users (create them
on the target), logs and run history. Credentials that already exist on the target are kept as they are.

<!-- studio -->
## In Studio

1. **Activity** lists every change, newest first. Filter by kind, by how it was made (Studio, command line,
   file edited by hand, migration), by date or text. The terminal button shows the pw command of a change.
2. **Replay script** – choose *changes since* a date (or *the whole current setup*), **Preview**, then
   **Save script**: it is written to the project's `migrations` folder with the command to run it.
3. **Export migration bundle** – choose whether to include the data folder; the zip goes to `migrations\`
   and the next steps for the target are shown.
4. On the target: **Check / import bundle** – the zip and the variables file; **Check** lists every create,
   change and removal (nothing is written); **Import** applies it after a confirmation, with a backup.
   Add missing credentials on the Credentials screen, then restart the service.

<!-- cli -->
## With the CLI

```powershell
# the log
pw activity                                  # newest 50
pw activity --since 2026-10-01 --kind load --commands
pw activity --search corp_ad --last 0

# one object at a time
pw config get source hr
pw config get load sccm-group > sccm-group.yaml
pw config put load sccm-group -f sccm-group.yaml --vars prod.vars.yaml
pw config remove endpoint old-report --if-exists

# replay script
pw migrate script -o setup.ps1                       # everything in the log
pw migrate script -o changes.ps1 --since 2026-10-15  # only recent changes
pw migrate script -o full.ps1 --full                 # the whole current setup
#   on the target:
.\setup.ps1 -Config C:\apps\hr-api\portwright.yaml -Vars prod.vars.yaml [-SkipCredentials]

# migration bundle
pw migrate export --include-data                     # migrations\hr-api-<time>.zip
pw migrate vars hr-api-20261019-101500.zip > prod.vars.yaml
pw migrate plan hr-api-20261019-101500.zip --vars prod.vars.yaml -c C:\apps\hr-api\portwright.yaml
pw migrate import hr-api-20261019-101500.zip --vars prod.vars.yaml -c C:\apps\hr-api\portwright.yaml
pw migrate import bundle.zip --init --prompt-credentials -c D:\apps\hr-api\portwright.yaml   # new instance
```

Import options: `--prune` also removes objects that are not in the bundle, `--skip-settings` keeps the
target's server and security settings, `--overwrite-data` replaces data files that differ, `--yes` skips
the confirmation (for a Stonebranch task). Exit code 0 = imported or nothing to do, 1 = refused/failed.
