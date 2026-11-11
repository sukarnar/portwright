---
title: MCP servers
screen: agents
---
An **MCP server definition** publishes chosen parts of your project as MCP tools for any MCP client (Claude
Desktop, other agents). Exposed items are explicit; nothing is published by default:

* `endpoints` – each becomes a tool (same scopes, validation, rate limit and audit as the API).
* `sources` – read-only SQL: `describe_<source>` lists the allowed tables and columns, and
  `query_<source>` runs a SELECT on the listed `tables` only. The table check is a text check; the real
  boundary is the database account behind the source credential, so give it SELECT rights only.
* `loads` – `run_load_<name>` runs a data load (needs `load:run`).
* `reports` – `run_report_<name>` runs a report. Its arguments are the report's own parameters (the endpoint's
  params, or the `:name` binds of its query) plus `formats`; it returns the files written (needs `reports:run`).
* `filesystem` – `fs_<op>` tools on named folders (`list`, `stat`, `read`, `search`, `write`, `mkdir`, `copy`,
  `move`, `delete`, `zip`; default `list, stat, read`). They use the same sandbox as workflow steps and need
  `fs:<root>:read` / `fs:<root>:write` / `fs:dest:<name>`. `fs_read` returns text files as text and PDF, Excel
  and other binary files as an embedded resource, up to `max_blob_mb` (default 10). Folders exposed with
  `read` are also listed as MCP **resources** (`portwright://fs/reports:2026-11/staff.pdf`), so a client can
  attach a report file without a tool call. A `dest:<name>` folder is write-only (copy/move/write/mkdir/zip).

The server acts as a principal limited to its `scopes`. An agent can use a defined MCP server through a tool of
`type: mcp` – the tools are called in-process with the **agent's own scopes** (tool names are prefixed
`<tool>__`).

```yaml
mcp_servers:
  hr_server:
    transport: stdio                  # or http
    scopes: [emp:read]
    expose:
      endpoints: [emp-list]
      sources: [{name: hr, tables: [emp, dept]}]
  reports_server:
    scopes: [reports:run, fs:reports:read]
    max_blob_mb: 10
    expose:
      reports: [dept-headcount]
      filesystem: [{root: reports, ops: [list, read]}]
tools:
  hr_mcp: {type: mcp, server: hr_server}     # let an agent use all of it
```

Needs the optional package: `pip install "portwright[agents]"`. The `http` transport listens on
`127.0.0.1` and requires an API key or token (header `X-API-Key`) that holds the server's scopes. Put TLS in
front before exposing it beyond this PC.

<!-- studio -->
## In Studio

**Agents > MCP servers > Add**: tick the endpoints and loads to publish, and list SQL sources with their tables
(one per line, e.g. `hr: emp, dept`). The **list** button shows exactly which tools clients will see; the
**copy** button gives the Claude Desktop setting. To let an agent use the server, add a Tool of type MCP.

<!-- cli -->
## With the CLI

```powershell
portwright mcp add hr_server --endpoint emp-list --source hr --table emp --table dept --scope emp:read
portwright mcp add reports_server --report dept-headcount --fs reports:list,read --scope reports:run --scope fs:reports:read
portwright mcp tools hr_server                 # what clients will see
portwright mcp serve hr_server                 # stdio: started by the MCP client
portwright mcp serve hr_server --transport http --port 8765
```

Claude Desktop (`claude_desktop_config.json`): command `portwright`, args
`["mcp", "serve", "hr_server", "--config", "C:\\path\\to\\portwright.yaml"]`.
