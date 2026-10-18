---
title: Security and API keys
screen: access
---
Endpoints are **protected by default**. A caller needs an API key (or a JWT for a user) whose scopes
include the endpoint's scope. `*` is the admin scope. An endpoint marked **public** needs no key.

* API keys look like `rf_<id>_<secret>`. Only a hash is stored – the key is shown **once**.
* Keys expire (default 90 days) and can be revoked at any time; revoked keys stop working immediately
  after a server restart.
* The key goes in the header `X-API-Key` (configurable in `portwright.yaml`, `security.api_key_header`).
* Rate limits apply per key (or per IP for public endpoints).
* Every call is written to `logs\audit.jsonl`: who, which endpoint, status, time – never data or secrets.

<!-- studio -->
## In Studio

1. **API keys > Create API key**. Name it after the application (e.g. `reporting-app`).
2. Tick only the scopes the application needs. `*` = everything (admin) – avoid for applications.
3. Choose the expiry and press **Create**. Copy the key from the dialog – it is not shown again.
   The clipboard is cleared automatically after 60 seconds.
4. **Revoke** stops a key. **Users** (second tab) are for people who log in for a JWT.
5. Change who may call an endpoint in **Endpoints > Edit > Access**.

<!-- cli -->
## With the CLI

```powershell
portwright key create reporting-app --scope employees:read --scope transfers:read
portwright key create stonebranch --scope load:run --expires-days 365
portwright key create admin-tool --scope "*" --expires-days 30
portwright key list
portwright key revoke 8f3k2l1q                  # the id from 'key list'

portwright endpoint set-access employees-list --scope employees:read
portwright endpoint set-access health-info --public

portwright user add jsmith --scope employees:read     # password prompted; JWT via POST /api/auth/token
portwright user remove jsmith
```

Store a key for scripts in an environment variable instead of in the script:

```powershell
[Environment]::SetEnvironmentVariable("PORTWRIGHT_KEY", "rf_xxx_yyy", "User")
```
