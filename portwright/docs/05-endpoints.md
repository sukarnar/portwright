---
title: Endpoints
screen: endpoints
---
An endpoint is a URL such as `GET /api/employees/{employee_id}`. Every call goes through the same
pipeline: authentication, scope check, rate limit, parameter validation, the source, then the audit log.

| Kind | Source | What it does |
|---|---|---|
| **Table (CRUD)** | SQL | list (with filters and paging), get by key, create, update, delete on one table |
| **Custom query** | SQL | your SELECT with `:name` binds; each bind is a validated parameter |
| **File** | file | rows of a file, filterable by chosen fields, or one row by key |
| **REST upstream** | rest | forwards to an internal API path, passing only declared parameters |
| **Python function** | callable | calls `module:function` with the declared parameters |

**Parameters** have a type (string, integer, number, boolean), a location (`query` = `?name=`, `path` =
`{name}` in the path, `body` = JSON body) and optional rules: required, default, min/max, max length,
allowed values, regex pattern. Unknown parameters are rejected.

<!-- studio -->
## In Studio

1. **Endpoints > New endpoint**, pick the source. The editor adapts to the source type.
2. **SQL table**: choose *Table (CRUD)*, press **Browse tables**, then tick the columns to expose,
   the filters for *list*, the key column and the operations. **Create endpoints** generates one
   endpoint per operation (`employees-list`, `employees-get`, …) with read/write scopes.
3. **SQL query**: choose *Custom query* and write the SELECT. **Detect parameters** adds a parameter for
   every `:name` in the SQL. **Test query** runs it and shows the first rows.
4. **File**: tick the fields callers may filter on. A *key* field gives a `/{key}` single-row endpoint.
   Filter and key fields must be column names without spaces (letters, digits, `_`).
5. **REST upstream**: upstream path (may use `{params}`) and method. **Python**: `module:function`.
6. **Path & parameters**: the full URL is shown live. Add, edit or remove parameters in the grid.
7. **Access**: tick existing scopes or type a new one (`transfers:read`), or make it **Public**
   (no key – use only for harmless data). Set a rate limit and the maximum page size.
8. **Review & test**: **Validate** checks the table, columns and binds against the real source.
   **Try it** runs the endpoint without a server, through the full security pipeline, and shows the
   status, time and rows. Copy the ready-made **PowerShell**, **curl** or **portwright call** command.
9. **Save**. Studio then offers the next step: create an API key with the endpoint's scope, or a load
   that reads this endpoint.

In the list: **Try it**, **Edit**, **Duplicate**, **Copy as PowerShell** and **Delete** (refused while a
load reads the endpoint). Restart the server after changing endpoints.

<!-- cli -->
## With the CLI

```powershell
# Table: list + get only, typed fields, filters on department_id
portwright endpoint crud employees -s hr --table hr.employees --key employee_id --read-only `
  --filter department_id

# Table with writes (source must be --read-write)
portwright endpoint crud emp -s hr_rw --table hr.employees --key employee_id `
  --field first_name:string:required --field last_name:string:required --field department_id:integer

# Custom query with two parameters
portwright endpoint add dept-headcount -s hr -p /departments/{dept_id}/headcount `
  --query "SELECT COUNT(*) AS headcount FROM hr.employees WHERE department_id = :dept_id AND status = :status" `
  --param dept_id:integer:path --param status:string:query --scope hr:read

# File with filters, and a single-row endpoint by key
portwright endpoint add transfer-list -s transfers -p /transfers --filter Dept --scope transfers:read
portwright endpoint add transfer-get -s transfers -p /transfers/{emp_no} --key emp_no --scope transfers:read

# Upstream API and Python function
portwright endpoint add crm-account -s crm -p /accounts/{id} --upstream-path /v2/accounts/{id} --scope crm:read
portwright endpoint add price -s pricing -p /price --target handlers.pricing:quote `
  --param sku:string:query:required --param qty:integer:query --scope pricing:read

portwright endpoint list
portwright endpoint show employees-list
portwright endpoint set-access transfer-list --scope transfers:read
portwright endpoint set-access health-info --public
portwright endpoint remove old-endpoint
portwright validate
```
