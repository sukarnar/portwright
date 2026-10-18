---
title: Calling endpoints
screen: endpoints
---
Three ways to call an endpoint:

| Way | Needs a server? | Needs a key? | Use for |
|---|---|---|---|
| **Try it** (Studio) | no | no | testing while building |
| `portwright call` | no | no (runs as admin, in-process) | scripts, schedulers, troubleshooting |
| HTTP (PowerShell, curl, apps) | yes | yes, unless public | applications |

List endpoints return `{"items": [...], "limit": 100, "offset": 0, "count": n}`. Use `limit` and `offset`
to page; `limit` cannot exceed the endpoint's maximum page size.

<!-- studio -->
## In Studio

* **Endpoints > Try it** (play button): fill in the parameters, press **Send**. The result shows the HTTP status, the
  time and the rows as a table (or the JSON).
* **Copy as PowerShell / curl / Portwright call** gives a ready command with your parameters;
  replace `<your-key>` with a real key.

<!-- cli -->
## With the CLI

No server needed:

```powershell
portwright call employees-list --limit 20 --table
portwright call employees-list -p department_id=50
portwright call employees-get -p employee_id=101
portwright call emp-create -p first_name=Dev -p last_name=Rao -p department_id=60
portwright call emp-update -p employee_id=101 --body '{"department_id": 70}'
portwright call transfer-list -o C:\exports\transfers.json -q      # write to a file, exit code only
```

Over HTTP with PowerShell (server running):

```powershell
$h = @{ "X-API-Key" = $env:PORTWRIGHT_KEY }
Invoke-RestMethod "http://localhost:8080/api/employees?department_id=50&limit=20" -Headers $h
Invoke-RestMethod "http://localhost:8080/api/employees/101" -Headers $h
Invoke-RestMethod "http://localhost:8080/api/emp" -Method Post -Headers $h `
  -ContentType "application/json" -Body '{"first_name":"Dev","last_name":"Rao"}'

# all rows of a list endpoint, page by page
$all = @(); $off = 0
do { $r = Invoke-RestMethod "http://localhost:8080/api/transfers?limit=500&offset=$off" -Headers $h
     $all += $r.items; $off += 500 } while ($r.count -eq 500)
$all | Export-Csv C:\exports\transfers.csv -NoTypeInformation
```

With curl (`curl.exe` on Windows):

```powershell
curl.exe -H "X-API-Key: $env:PORTWRIGHT_KEY" "http://localhost:8080/api/employees/101"
```
