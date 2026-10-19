---
title: Troubleshooting
screen: dashboard
---
| Message | What to do |
|---|---|
| *The filename, directory name or volume label syntax is incorrect* | a file source path contains a full path or bad characters. Use only the file name inside `data\` (e.g. `transfers.csv`). |
| *File … is outside the data folder* | copy the file into the project's `data` folder and use just its name |
| *vault is locked / master key missing* | `.env` lacks `PORTWRIGHT_MASTER_KEY` – restore `.env` from backup, or `portwright cred init` for a new project |
| *401 Unauthorized* | no or wrong `X-API-Key`; check `portwright key list` (expired? revoked?) |
| *403 Forbidden* | the key lacks the endpoint's scope – create a key with that scope |
| *422* | a parameter is missing or invalid; the response says which |
| *source … is read-only* | edit the source and switch on **Allow writes** (`--read-write`) |
| load *skipped – source unchanged* | the same file was loaded already; replace the file, or **Run anyway** / `--force` |
| load *columns changed* | the file's header changed; check the mapping, then **Run anyway** / `--force` |
| load *n row(s) rejected* | open **View rejected rows** / `portwright load rejects <run>`; fix the data or raise Max rejects |
| Studio shows an old screen after an update | close every Studio window (and browser tab), check `pip show portwright`, start it again |
| A list shows only some rows | list endpoints are paged; use `limit`/`offset`, or `portwright call ... --limit 10000` |

**Renamed from restforge.** Projects created before the rename work unchanged: an existing
`restforge.yaml` is used when there is no `portwright.yaml`, `RESTFORGE_*` variables in `.env` are
read, the vault in `.restforge\` is found, API keys starting `rf_` stay valid (new ones start `pw_`),
and the `restforge` command still runs. An install in `C:\apps\restforge` can stay there.

<!-- studio -->
## In Studio

* **Dashboard > Validate project** lists configuration problems with the exact reason.
* **Sources > Test** and **Credentials > Test** check connections one by one.
* A yellow bar at the top means `portwright.yaml` was changed elsewhere – press **Reload**.

<!-- cli -->
## With the CLI

```powershell
portwright --help                       # is the command found? (activate the venv first)
pw --version                            # short alias, same command
where.exe portwright                    # which installation is used
pip show portwright                     # installed version
portwright validate
portwright source test hr
portwright cred test hr_oracle
portwright call employees-list --limit 1
Get-Content C:\apps\hr-api\logs\audit.jsonl -Tail 20
```
