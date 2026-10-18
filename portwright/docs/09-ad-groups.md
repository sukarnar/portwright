---
title: Active Directory groups
screen: loads
---
A load can write into an **Active Directory group** instead of a table: the source (a SQL query
endpoint or a file) gives one **e-mail address per row**, Portwright finds the AD user for each address
and makes the group's members match. One load manages one group.

**Modes**

| Mode | What it does |
|---|---|
| Kill & fill (`replace`) | the group's member list is set to exactly the matched users (plus the kept members) in **one** AD change – if it fails the group is unchanged |
| Sync (`sync`) | adds the missing users, removes users no longer in the source (batches of 500); members already correct are not touched |
| Add only (`insert`) | adds the missing users; nobody is removed |

Kill & fill and sync give the same result; sync changes only the difference, which is easier to audit
and better for very large groups (over 4000 members kill & fill automatically falls back to sync batches).

**Matching** – each address is trimmed and compared without case against `mail`, then
`userPrincipalName` (optionally also `proxyAddresses` aliases). A row is **rejected** (rejects file, with the
reason) when the address is empty or invalid, no AD user has it, more than one user has it, or the account
is disabled (*reject disabled*, on by default). A repeated address is dropped. More rejects than
**Max rejects** → the group is not changed.

**Never removed** – only user accounts are managed. Always kept: computers, contacts, members from other
domains (foreign security principals). Also kept: nested groups (*keep nested groups*, on by default), the
**protected** list (sAMAccountName, e-mail, UPN or DN) and every member of the **exception group**.

**Safety** – the run is refused, and the group not touched, when more than **max_remove_percent** (default
**20 %**) of the managed members would be removed, or when fewer valid users than *min rows* / *min %* are
found. Raise the limit on purpose for a planned clean-up, then set it back.

**Connecting to AD** – an `ldap` source:

- `directory: ldap` (default): LDAPS (port 636) with a **service account** stored as a *basic* credential
  (`svc_portwright@corp.local` or `CORP\svc_portwright`). The DC certificate is checked against the Windows
  certificate store, or `ca_file`. Plain `ldap://` is refused unless `allow_plain_ldap: true`.
- `directory: powershell`: uses the RSAT **ActiveDirectory** PowerShell module and the Windows account the
  process runs as (the service or the Stonebranch agent account) – no password stored. The credential is
  optional.

The account needs only **read** on users and **Write Members** on the target group(s) – delegate it on
the group (*Properties > Security > Advanced > Add > Write Members*), not Domain Admin.

Every run is versioned like other loads: an unchanged source is skipped (exit code **3**), the history
records added / removed counts, and the run report lists every member added and removed.

<!-- studio -->
## In Studio

1. **Sources > Add source**, type **Active Directory (ldap)**: domain controllers, base DN, the service
   account credential (or *PowerShell*). **Test** binds and reads the base DN.
2. **Loads > New load**, **Write to: AD group**. Choose the source rows (endpoint with the e-mail query, or a
   file), the **e-mail column**, the AD source and the group (**Browse groups** searches AD).
   **Check group and source** shows the group's DN and member count and whether the e-mail column exists.
3. **How members are changed**: mode, max rejects, matching, reject disabled, protected accounts,
   exception group, keep nested groups, and the safety limit (*Refuse if more than % removed*).
4. **Preview (dry run)** looks up every address and lists each user that **would be added or removed** –
   nothing is changed. **View rejected rows** shows addresses that did not match.
5. **Save**, then **Run** (it asks for confirmation). The report shows added / removed / kept and the list
   of changes.

<!-- cli -->
## With the CLI

```powershell
# the service account and the AD source
portwright cred add ad_svc -t basic                    # username svc_portwright@corp.local + password
portwright source add corp_ad -t ldap --server ldaps://dc01.corp.local --server ldaps://dc02.corp.local `
    --base-dn "DC=corp,DC=local" --credential ad_svc
portwright source test corp_ad
portwright source ad-groups corp_ad -s HR               # find groups
portwright source ad-group corp_ad HR-Staff             # DN and member counts
portwright source ad-user corp_ad ann.lee@corp.com      # which AD user an address matches

# or with the PowerShell AD module and the service's own Windows account
portwright source add corp_ad_ps -t ldap --directory powershell --server dc01.corp.local --base-dn "DC=corp,DC=local"

# the endpoint with the addresses, and the load
portwright endpoint add hr-staff-emails --source hr --path /staff/emails `
    --query "SELECT email_address AS email FROM hr.employees WHERE status = 'A'"
portwright load create hr-staff-group --from-endpoint hr-staff-emails --to corp_ad `
    --ad-group HR-Staff --email-column email --mode sync --max-rejects 25
portwright load map hr-staff-group --max-remove-percent 20 --protect svc_report --exception-group HR-Staff-Exceptions
portwright load show hr-staff-group

portwright load run hr-staff-group --dry-run           # lists + adds and - removes, changes nothing
portwright load run hr-staff-group                     # exit 0 ok, 2 with rejects, 1 failed/refused, 3 unchanged
portwright load history hr-staff-group
```

In `portwright.yaml`:

```yaml
sources:
  corp_ad:
    type: ldap
    servers: [ldaps://dc01.corp.local, ldaps://dc02.corp.local]
    base_dn: DC=corp,DC=local
    credential: ad_svc
loads:
  - name: hr-staff-group
    from: {endpoint: hr-staff-emails}
    to: {source: corp_ad, group: HR-Staff}
    mode: sync                      # replace = kill & fill | sync | insert = add only
    max_rejects: 25
    ad:
      email_column: email
      match: [mail, userPrincipalName]
      max_remove_percent: 20
      protected: [svc_report]
      exception_group: HR-Staff-Exceptions
      keep_nested_groups: true
      reject_disabled: true
```

**First real run**: always do a `--dry-run` and read the remove list. If the run is refused by the 20 %
limit, check the source query first; only raise `--max-remove-percent` when the removal is expected.
