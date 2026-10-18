---
title: Credentials
screen: credentials
---
Credentials hold logins and tokens that Portwright uses to reach your systems. They are encrypted
(AES-256-GCM) in the project vault, never written to `portwright.yaml`, never shown again after saving,
and removed from log lines.

| Type | Fields | Used for |
|---|---|---|
| `database` | driver, host, port, service_name / sid / dsn / database, username, password, schema | SQL sources |
| `basic` | username, password | upstream APIs |
| `bearer` | token | upstream APIs |
| `api_key` | header, key | upstream APIs |
| `oauth2` | token_url, client_id, client_secret, scope | upstream APIs (token fetched and refreshed for you) |
| `generic` | any name=value pairs | `${CRED:name.field}` in headers, or Python functions |

<!-- studio -->
## In Studio

1. **Credentials > Add credential**. If the vault is locked, press **Create master key** first.
2. Choose the type. For a database, choose the driver first – the form shows the fields that driver needs
   (Oracle: host, port, service name or SID, user, password).
3. Optionally choose **Rotate after** to get a reminder before the password expires.
4. **Save**, then the **Test** (play) button logs in and reports success or the database error.
5. **Rotate / edit** changes a password; running servers pick it up within a few minutes.
6. **Used by** shows which sources use the credential; it cannot be deleted while in use.

<!-- cli -->
## With the CLI

```powershell
portwright cred init                                   # create the master key (once per project)
portwright cred types                                  # fields of every type

# Oracle – the password is asked for (hidden input)
portwright cred add hr_oracle -t database -f driver=oracle+oracledb -f host=dbhost.corp.local `
  -f port=1521 -f service_name=HRPDB -f username=hr_api --expires-days 90

# SQL Server, PostgreSQL
portwright cred add dw -t database -f driver=mssql+pyodbc -f host=sqlhost -f database=DW -f username=etl
portwright cred add pg -t database -f driver=postgresql+psycopg -f host=pghost -f database=app -f username=app

# Upstream APIs
portwright cred add crm_token -t bearer
portwright cred add erp -t oauth2 -f token_url=https://login.corp.local/oauth2/token -f client_id=portwright

# Non-interactive (scheduler / CI): take the secret from an environment variable
portwright cred add hr_oracle -t database -f driver=oracle+oracledb -f host=dbhost -f service_name=HRPDB `
  -f username=hr_api --secret-from-env password=HR_DB_PASSWORD

portwright cred list
portwright cred test hr_oracle
portwright cred rotate hr_oracle                       # new password, prompted
portwright cred show hr_oracle                         # secrets masked
portwright cred remove old_login
portwright cred rekey                                  # new master key, re-encrypts the vault
```
