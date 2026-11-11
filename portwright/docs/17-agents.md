---
title: Agents and workflows
screen: agents
---
An **agent** is an LLM that works on a task for up to *N* steps. Each step the model may call **tools** (your
endpoints, SQL sources, or an MCP server) and read **skills** (markdown playbooks). The loop ends when the
model answers, or at `max_steps`, `max_tokens_budget` or `timeout_seconds`. A **workflow** chains tool calls,
agent runs and conditions in a fixed order.

Safety rules that always apply:

* Tool calls run as a **restricted principal** with only the agent's `scopes`. Endpoint tools go through the
  normal pipeline (authentication, scopes, rate limit, validation, audit). Missing scope = the model gets a
  403 as the tool result.
* SQL tools are **SELECT only** unless the tool sets `read_only: false` on a source that allows writes.
* The model host must be listed in `security.allowed_llm_hosts`. API keys come from the credential vault.
* Every run saves a trace in `.portwright/agent_runs/` and writes audit lines (`agent_run`, `agent_tool`).

```yaml
llm_models_include: [models/*.yaml]        # more models in separate files (optional)
llm_models:
  corp:
    provider: openai_compatible            # or: anthropic
    base_url: https://llm.corp.local/v1
    model: gpt-4o
    credential: corp_llm_key               # a bearer or api_key credential
    tls: {ca_bundle: certs/corp-ca.pem, client_cert: certs/client.pem, client_key: certs/client.key}
tools:
  employees: {type: endpoint, endpoint: emp-list}
  hr_sql:    {type: source, source: hr, max_rows: 100}
skills:
  payroll_audit: {file: skills/payroll_audit.md, description: How to audit payroll anomalies}
agents:
  analyst:
    model: corp
    instructions: You are an HR data analyst. Be brief.
    tools: [employees, hr_sql]
    skills: [payroll_audit]
    scopes: [emp:read]
    max_steps: 8
workflows:
  weekly_check:
    scopes: [emp:read]
    steps:
      - {id: pull,   type: tool,  tool: employees, input: {dept: "${input.dept}"}}
      - {id: review, type: agent, agent: analyst, input: "Review: ${steps.pull.output}"}
      - {id: alert,  type: condition, when: "${steps.review.output.flagged}", then: notify, else: end}
      - {id: notify, type: set, value: "flagged"}
    output: "${steps.review.output}"
```

Workflow references: `${input.KEY}` and `${steps.ID.output.field[0]}`. A condition jumps to the step named in
`then` / `else` (`end` stops). `on_error: continue` on a step lets the workflow go on after a failure.
An agent answer that is JSON becomes structured data for later steps.

Every step can say where to go with `next: <step id>` (or `end`); without it the following step in the
list runs. Conditions use `then` and `else`. The visual editor writes all of these, plus the node position
(`x`, `y`), so the picture is exactly what runs.

**Input and output steps.** A step of type `input` declares what the workflow is run with; an `output` step
returns the result and ends the workflow:

```yaml
steps:
  - {id: start, type: input, inputs: {dept: {description: Which department}, limit: 20}}
  - {id: pull,  type: tool,  tool: employees, input: {dept: "${input.dept}"}}
  - {id: done,  type: output, value: {rows: "${steps.pull.output}"}}
```

An input is optional when it has a default (`limit: 20`) and required when it has none (`dept`); a run without a
required value fails with "missing required input". `output` without a `value` returns the last step's result.
Without an `output` step the workflow's `output:` (or the last step) is the result, as before.

**More step types.**

```yaml
steps:
  - {id: classify, type: llm, model: corp, format: json,
     system: "Label the message.", input: "Classify: ${input.message}"}
  - {id: route, type: switch, when: "${steps.classify.output.label}",
     cases: {billing: reply_billing, bug: reply_bug}, default: human}
  - {id: sync,  type: load, load: nightly_orders}
  - {id: each,  type: foreach, items: "${steps.pull.output.rows}", do: llm, model: corp,
     input: "Summarise: ${item}", on_error: continue, max_items: 100}
```

- `llm`: one model call with a prompt (`input`) and an optional `system` prompt, and no tool loop. With
  `format: json` the reply is parsed, so later steps can use `${steps.classify.output.label}`; a reply that is not
  JSON fails the step. Token use is recorded on the step.
- `load`: runs a defined data load and returns its result. The workflow needs the scope `load:run`.
- `foreach`: runs a `tool`, `agent`, `llm`, `load`, `report`, `copy` or `move` once per item of a list and returns
  the list of results.
  Inside it `${item}` and `${index}` are available. `items` can be a list or a SQL tool result (`{rows: [...]}`).
  A list longer than `max_items` (default 50, up to 1000) stops the step instead of running them all. With
  `on_error: continue` a failed item becomes `{error: ...}` and the loop goes on.
- `switch`: jumps by value. `when` is the value, `cases` maps each value to a step id (`end` stops), and `default`
  is used when nothing matches (without a default the next step in the list runs). Matching ignores case and
  surrounding spaces.

**Reports and files.** Run a report, then deliver its files to a folder:

```yaml
destinations:                         # write-only delivery folders, reached as the account running portwright
  finance: {path: '\\fileserver\finance\reports', overwrite: rename}   # must be in security.allowed_folders

workflows:
  monthly-headcount:
    scopes: [reports:run, fs:reports:read, fs:dest:finance]
    steps:
      - {id: start,   type: input, inputs: {dept: null}}
      - {id: build,   type: report, report: dept-headcount, input: {dept: "${input.dept}"}, formats: [pdf, xlsx]}
      - {id: publish, type: copy, files: "${steps.build.output.outputs}",
         to: "dest:finance/${run.date.year}-${run.date.month}", manifest: true}
      - {id: done,    type: output, value: "${steps.publish.output}"}
```

- `report`: runs a saved report (`input` = its params; `formats`, `vars` and `output_path` override the report's
  own). `data: ${steps.q.output.rows}` uses rows from an earlier step instead of the report's source; without
  `report`, `formats` + `data` (+ `template` for pdf/html) make an inline report. The output is
  `{outputs: [{path, format, bytes, sha256}], row_count, ...}`. Needs the scope `reports:run`.
- `copy` / `move`: put `files` into the folder `to`. `files` takes a report's `outputs`, a `file list` result,
  paths, or addresses like `reports:2026-11/a.pdf`. `to` is a folder: `dest:<name>/sub/folder` or `<root>:folder`.
  - Each file is written to `name.partial`, checked (`verify: sha256` by default; `size` or `none`), then renamed,
    so a half-written file never appears in the folder.
  - `overwrite`: `never` (fail), `always`, `rename` (`a (1).pdf`) or `if_newer`. Default: the destination's
    `overwrite`, else `never`. `create_dirs: false` refuses missing folders; `manifest: true` writes
    `manifest.json` with names, sizes and checksums.
  - `move` removes each source only after its copy is verified. If the source can't be removed (file in use),
    the entry says `copied` with a warning instead of failing. The source root must be writable.
  - The output lists `{src, dest, address, bytes, sha256, action}` per file (`action`: copied, moved, skipped).
- `file`: `op: write` (`to`, `content`; a value that isn't text is written as JSON), `read` (`path`), `list`
  (`path`, `glob`, `recursive`), `mkdir` (`to`), `delete` (`files`; moved to `.portwright/trash` by default) or
  `zip` (`files`, `to: reports:archive/x.zip`).
- `${run.id}`, `${run.started_at}` (UTC) and `${run.date.year|month|day|iso}`, `${run.time}` (local time,
  zero padded) are available in every step for naming folders and files.

**Filesystem roots.** Steps only reach named folders. `reports` (`reports/output`, read-write), `templates`
(read-write) and `data` (the data folder, read-only) always exist; add more under `filesystem.roots`:

```yaml
filesystem:
  roots:
    inbox: {path: inbox, mode: read_write, patterns: ["*.pdf", "*.xlsx"], delete: trash}   # none | trash | hard
  max_file_mb: 50
```

Paths with `..`, alternate data streams (`a.txt:x`), device names (`CON`, `NUL`), or symlinks/junctions leading
out of a root are refused. Absolute or UNC root and destination paths must be listed in
`security.allowed_folders`. Every operation is written to the audit log (`fs_copy`, `fs_move`, `fs_write`, ...).

Scopes: `fs:<root>:read` (or `fs:<root>:write`, which includes reading) for files a step reads,
`fs:<root>:write` for files it writes or moves away, `fs:dest:<name>` to deliver to a destination, and `*` for
everything. In the workflow canvas, **Report**, **Copy**, **Move** and **Files** nodes take their options as
YAML under *Settings*.

**Templates.** Ready-made workflows to start from: `hello` (needs no tools), `ask-an-agent`,
`query-and-summarise`, `check-and-alert`, `draft-and-review`, `with-fallback`, `compare-two-sources`,
`classify-and-route`, `summarise-each-row` and `load-then-report`.
Their tool, agent, model and load steps are left blank for you to pick (a single choice is filled in).

<!-- studio -->
## In Studio

Open **Agents**. The tabs follow the order you build things in:

1. **Models** > Add: provider, base URL, model name, the API-key credential, and for a corporate gateway the CA
   certificate and (for mutual TLS) client certificate and key. The **plug** button makes one real call to
   prove URL, certificates and key work. The host is added to the allowed LLM hosts when you save.
2. **Tools** > Add: pick an endpoint, a SQL source (SELECT only unless you allow writes) or an MCP server.
3. **Skills** > Add: a markdown playbook; write the text in the dialog and it is saved to the file.
4. **Agents** > Add: choose the model, tick tools and skills, set scopes and limits (steps, tokens, time).
   The **play** button runs it: tool calls appear as they happen, then the answer and a step table.
5. **Workflows**: **Add visually** (or the tree icon on a row) opens the node editor: add Tool, Agent,
   Condition and Set nodes, drag them where you like, click an output port (blue = next, green = true,
   red = false) and then the step it should go to. Select a node to edit its tool, agent, arguments or
   condition on the right. On a Tool node, **Query a SQL source** or **Call an endpoint** picks it straight away
   and creates the tool behind it. **Input** and **Output** nodes declare what the workflow takes and returns; **LLM prompt**, **Run load**,
   **For each** and **Switch** nodes cover single model calls, loads, loops and multi-way branches
   (a Switch node grows one port per case, one case per line).
   For a new workflow, **Start from a template** loads a ready-made layout. **Add** edits the same steps as YAML.
   **Play** asks for the input values (pre-filled with the defaults from the Input node).
6. **Run history** lists saved runs; **eye** opens every step and tool call.

**Check setup** lists problems: unknown models, missing skill or certificate files, hosts not allowed.

<!-- cli -->
## With the CLI

```powershell
portwright cred add corp_llm_key -t bearer                       # the API key (prompted, hidden)
portwright model add corp --base-url https://llm.corp.local/v1 --model gpt-4o --credential corp_llm_key `
  --ca-bundle certs/corp-ca.pem --client-cert certs/client.pem --client-key certs/client.key
portwright model test corp                                       # one real call: URL, certificates, key

portwright tool add employees -t endpoint --endpoint emp-list
portwright tool add hr_sql -t source --source hr
portwright skill add payroll_audit --file skills/payroll_audit.md
portwright agent add analyst --model corp --tool employees --tool hr_sql --skill payroll_audit `
  --scope emp:read --max-steps 8 --instructions "You are an HR data analyst."

portwright agent run analyst "Which department has the highest average salary?" -v
portwright agent runs                                            # saved traces
portwright workflow templates                                  # ready-made workflows
portwright workflow templates hello > hello.yaml                 # print one as YAML, then edit it
portwright workflow add weekly_check --file weekly_check.yaml
portwright workflow run weekly_check --input dept=ENG
```

Over HTTP (scopes `agent:run` / `workflow:run`): `POST /api/_agents/NAME/run` with `{"task": "..."}` and
`POST /api/_workflows/NAME/run` with `{"input": {...}}`. Responses leave out tool results.
