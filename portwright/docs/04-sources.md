---
title: Sources
screen: sources
---
A source is where data comes from. Endpoints and loads refer to sources by name.

| Type | What it reads | Notes |
|---|---|---|
| `sql` | any database SQLAlchemy supports (Oracle, SQL Server, PostgreSQL, MySQL, SQLite) | read-only by default; **Allow writes** for create/update/delete endpoints and as a load target |
| `file` | CSV, JSON, JSON Lines, Excel in the project's `data` folder | always read-only; re-read automatically when the file changes |
| `rest` | an upstream HTTP API | the host must be allow-listed; credentials are added by Portwright, callers' keys are never forwarded |
| `callable` | functions in a Python module in `handlers\` | only functions you name are callable |
| `folder` | the files in a directory (data folder, or an allowed absolute / UNC path) | read by loads: each file to a BLOB/CLOB row, or each CSV/Excel file's rows – see *Folders of files* |
| `ldap` | Active Directory (users and groups) | the **target** of AD group loads; LDAPS with a service account, or the PowerShell AD module |

<!-- studio -->
## In Studio

1. **Sources > Add source**, choose the type.
2. **SQL**: pick the database credential. Switch on **Allow writes** only if endpoints must change data
   or a load writes into this database.
3. **File**: type the file name inside the data folder (for example `transfers.csv`). In the desktop window
   **Browse…** copies a file from anywhere into the data folder. For Excel, give the sheet name.
4. **REST**: base URL (e.g. `https://hr-api.corp.local/v2`), tick *Allow Portwright to call this host*,
   choose a credential, add extra headers as `Name=value` lines.
5. **Callable**: the module, e.g. `handlers.pricing` for `handlers\pricing.py`.
6. **Active Directory (ldap)**: domain controllers (`ldaps://dc01.corp.local`, one per line), the base DN
   (`DC=corp,DC=local`) and a *basic* credential for the service account. See *Active Directory groups*.
7. **Test** (play button) connects to the database, or shows the first rows of a file.

To refresh a file source, copy the new file over the old one in `data\` – nothing else is needed.

<!-- cli -->
## With the CLI

```powershell
portwright source add hr -t sql --credential hr_oracle                 # read-only
portwright source add hr_rw -t sql --credential hr_oracle --read-write  # for writes and loads
portwright source add transfers -t file --path transfers.csv
portwright source add budget -t file --path budget.xlsx --sheet FY27
portwright source add crm -t rest --base-url https://crm.corp.local/api --allow-host --credential crm_token
portwright source add pricing -t callable --module handlers.pricing --allow-credential erp
portwright source add corp_ad -t ldap --server ldaps://dc01.corp.local --server ldaps://dc02.corp.local `
    --base-dn "DC=corp,DC=local" --credential ad_svc --test-group HR-Staff

portwright source list
portwright source test transfers          # shows the first rows
portwright source test hr                 # connects to the database
portwright source set-credential hr new_login
portwright source remove old_source       # refused while endpoints or loads use it
```

Refresh a file from another folder (e.g. in a scheduled job):

```powershell
Copy-Item \\fileserver\exports\transfers.csv C:\apps\hr-api\data\transfers.csv -Force
```
