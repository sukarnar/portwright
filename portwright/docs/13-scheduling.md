---
title: Scheduling and exit codes
screen: loads
---
Schedulers such as Stonebranch UAC run Portwright commands and decide by the **exit code**.

| Command | 0 | 1 | 2 | 3 |
|---|---|---|---|---|
| `portwright load run` | loaded | failed, nothing written | loaded, some rows rejected | skipped – source unchanged |
| `portwright load check` | changed / never loaded | error | – | unchanged |
| `portwright call` | success | error | – | – |
| `portwright validate` | valid | problems | – | – |
| `portwright report run` | done | failed | done, with warnings (rows cut off at `from.max_rows`) | – |
| `portwright workflow run` | done | failed | done, with warnings (see below) | – |
| `portwright agent run` | answered | error, or stopped by a limit (steps, tokens, time) | – | – |
| `portwright doctor` | nothing the project uses is broken | something the project uses cannot work | – | – |

Typical choices in UAC: treat **0** and **3** as success, **2** as success with a warning (send the
rejects file, or look at the run), **1** as failure.

A workflow exits with **2** when it finished but not everything worked:

* a step failed and the workflow went on because the step has `on_error: continue`
  (a fallback path, for example);
* items of a `foreach` step failed (with `on_error: continue`);
* a `report` step's report hit `from.max_rows`.

A failed `condition` or `switch` is not a warning: it takes its `else` / `default` branch.
`--json` output has the list under `warnings` and the code under `exit_code`.

<!-- studio -->
## In Studio

* Define and test the load in **Loads**, then schedule the CLI command shown here.
* After runs, the **Dashboard** and **Loads** show the latest runs, rejects and source versions.

<!-- cli -->
## With the CLI

A Windows command task in UAC (batch):

```bat
@echo off
cd /d C:\apps\hr-api
call C:\apps\portwright\.venv\Scripts\portwright.exe load run transfers-to-db > NUL
set RC=%ERRORLEVEL%
if %RC%==3 exit /b 0
exit /b %RC%
```

The same in PowerShell, refreshing the file first:

```powershell
Copy-Item \\fileserver\exports\transfers.csv C:\apps\hr-api\data\transfers.csv -Force
Set-Location C:\apps\hr-api
& C:\apps\portwright\.venv\Scripts\portwright.exe load run transfers-to-db --json | Out-File logs\last-run.json
exit $LASTEXITCODE
```

A monthly report delivered to a share, as a workflow (a `report` step, then a `copy` step to a
destination), with the run log kept for the operators:

```powershell
Set-Location C:\apps\hr-api
& C:\apps\portwright\.venv\Scripts\portwright.exe workflow run monthly_sales --input month=2026-10 --json |
  Out-File "logs\monthly_sales-$(Get-Date -Format yyyyMMdd-HHmmss).json"
exit $LASTEXITCODE          # 0 delivered, 2 delivered with warnings, 1 failed
```

Or just the report, in a batch task:

```bat
cd /d C:\apps\hr-api
call C:\apps\portwright\.venv\Scripts\portwright.exe report run monthly_sales -f pdf,xlsx > NUL
exit /b %ERRORLEVEL%
```

Run a report and email it from a scheduler, without a workflow (`portwright mail send` exits 0 when sent,
1 on any error, such as a recipient the mail server doesn't allow):

```powershell
& C:\apps\portwright\.venv\Scripts\portwright.exe report run monthly_sales -f pdf
if ($LASTEXITCODE -eq 1) { exit 1 }
& C:\apps\portwright\.venv\Scripts\portwright.exe mail send corp --to finance@corp.local `
  -s "Monthly sales $(Get-Date -Format yyyy-MM)" -b @mail\monthly.txt -a reports:monthly_sales.pdf
exit $LASTEXITCODE
```

Wait in a batch file: `timeout /t 30 /nobreak > NUL`. Hide output: add `> NUL 2>&1` (or `-q` for `call`).

Trigger a load over HTTP (needs a key with scope `load:run`):

```powershell
Invoke-RestMethod "http://localhost:8080/api/_loads/transfers-to-db/run" -Method Post -Headers @{ "X-API-Key" = $env:PORTWRIGHT_KEY }
Invoke-RestMethod "http://localhost:8080/api/_loads/transfers-to-db/run?dry_run=true" -Method Post -Headers @{ "X-API-Key" = $env:PORTWRIGHT_KEY }
```
