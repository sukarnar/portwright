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

Typical choices in UAC: treat **0** and **3** as success, **2** as success with a warning (send the
rejects file), **1** as failure.

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

Wait in a batch file: `timeout /t 30 /nobreak > NUL`. Hide output: add `> NUL 2>&1` (or `-q` for `call`).

Trigger a load over HTTP (needs a key with scope `load:run`):

```powershell
Invoke-RestMethod "http://localhost:8080/api/_loads/transfers-to-db/run" -Method Post -Headers @{ "X-API-Key" = $env:PORTWRIGHT_KEY }
Invoke-RestMethod "http://localhost:8080/api/_loads/transfers-to-db/run?dry_run=true" -Method Post -Headers @{ "X-API-Key" = $env:PORTWRIGHT_KEY }
```
