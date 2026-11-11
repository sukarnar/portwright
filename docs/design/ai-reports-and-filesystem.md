# Design: AI-authored reports, report/file workflow nodes, filesystem tools

Status: **phases 1–3 implemented**, phases 4–5 proposed · Target: portwright 0.9 · Branch: `design/ai-reports-fs-nodes`

## 1. Goals

1. **Reports as a pipeline stage**: run a report from a workflow, feed it rows from an earlier step,
   and choose one or more output formats per run (PDF, HTML, XLSX, CSV, JSON, Markdown).
2. **AI-authored templates**: give a prompt plus a data source (for example the `dept-summary`
   endpoint) and get back a working, validated Jinja2 template, registered as a report.
3. **"Ask for a report"**: one prompt makes an agent pick the query, design the template, register
   the report, run it and return the files.
4. **Delivery nodes**: copy, move, or archive report outputs into a destination folder (local, UNC
   share) with overwrite policy, checksums and a manifest.
5. **Filesystem tools**: sandboxed `fs_*` tools that agents, workflows and MCP servers share, held to
   the same scope, audit and allow-list rules as endpoint/SQL tools.

Non-goals for 0.9: email/SFTP/SharePoint delivery, DOCX output, XLSX *templates*. The interfaces below
leave room for them.

## 2. What exists today (and what it constrains)

| Area | Today | Gap |
|---|---|---|
| `reports/service.py` | `ReportSpec` (template + `from` endpoint/source + `output`) → HTML or PDF | One format per run; can't take rows from a caller; SQL sources need a `_sql` param hack |
| `reports/engine.py` | Plain `jinja2.Environment`, WeasyPrint → xhtml2pdf fallback | Not sandboxed (matters once an LLM writes templates); WeasyPrint will fetch remote URLs |
| `agents/workflow.py` | Steps: `input tool agent llm load foreach condition switch set output` | No report or file step |
| `agents/tools.py` | `ToolRuntime` + `ToolRegistry`; tool kinds endpoint / sql / load / mcp | No filesystem or report tools |
| `mcp/__init__.py` | Exposes endpoints, sources, loads | No report/filesystem exposure, text-only results |
| `security` | `data_root`, `allowed_folders` (folder sources) | No notion of writable roots or named destinations |

Bugs found while reading. Fix these in phase 1:
- `ReportFrom.endpoint` says "all pages fetched automatically", but `_fetch` makes a single call and
  unwraps `items`. It has no page loop.
- `_read_sql_source` builds a new engine on every run and `params.pop("_sql")` mutates the caller's dict.
- The `as_html` path juggling (`replace(".pdf", "")` then `with_suffix`) breaks on stems that contain dots.
- The uncommitted `OSError` fallback in `engine.py` (GTK missing on Windows) is correct. Keep it.

## 3. Architecture overview

```
                     ┌──────────────── portwright.yaml ────────────────┐
                     │ reports · filesystem.roots · destinations ·      │
                     │ tools(type: report|filesystem) · workflows ·     │
                     │ mcp_servers.expose.{reports,filesystem}          │
                     └──────────────────────┬──────────────────────────┘
                                            │
   ┌──────────────┐   ┌─────────────────────┴───────────────────────┐   ┌───────────────┐
   │ CLI / Studio │──▶│ services: ReportService · ReportAuthor ·    │◀──│ MCP server    │
   └──────────────┘   │           FileService                       │   └───────────────┘
                      └──────┬──────────────┬───────────────┬────────┘
                             │              │               │
                 reports/renderers/*   reports/author.py   fs/sandbox.py
                 (html pdf xlsx csv    (LLM → template →   (roots, path checks,
                  json md)              validate/repair)    atomic ops, audit)
                             ▲              ▲               ▲
                             └──── agents/tools.py: report_* and fs_* Tool makers ────┘
                                            ▲
                 agents/workflow.py: report · report_design · copy · move · file steps
```

One implementation per capability. The CLI, Studio, workflow steps, agent tools and MCP all call
the same service. This follows the rule `tools.py` already sets ("one ToolRegistry is the single
source of truth").

## 4. Reports: multi-format output

### 4.1 Renderer registry — `portwright/reports/renderers/`

```python
class Renderer(Protocol):
    format: str                 # "pdf"
    extension: str              # ".pdf"
    needs_template: bool        # pdf/html/md: yes · csv/xlsx/json: no (template optional)
    def render(self, ctx: ReportContext, out: Path) -> RenderedFile: ...

RENDERERS = {"html": HtmlRenderer, "pdf": PdfRenderer, "md": MarkdownRenderer,
             "csv": CsvRenderer, "xlsx": XlsxRenderer, "json": JsonRenderer}
```

- `ReportContext` = rows, columns (with inferred types), vars, report name, `now`, and the rendered
  HTML (computed once and shared by html and pdf).
- `XlsxRenderer` uses `openpyxl`, which is already a core dependency. It writes a header row, typed cells,
  auto widths, a frozen header, and optional `sheets:` grouping by a column.
- `CsvRenderer`/`JsonRenderer` are data-only. `columns:` / `rename:` in the spec control the shape.
- `MarkdownRenderer` writes a GFM table with a title line. Rendering from a `.md.j2` template is deferred,
  because a report has one template today and that template is HTML.
- Plugins can add renderers later, for example `docx`, through an entry point (`portwright.renderers`).

### 4.2 Spec changes (backward compatible)

```yaml
reports:
  - name: dept-headcount
    template: .portwright/templates/dept_headcount.html   # AI-generated (see §5.4); hand-written → templates/
    from:
      endpoint: dept-summary          # existing
      params: {dept: null}
      # NEW alternatives:
      # source: hr
      # query: "select dept, count(*) n from employees group by dept"   # replaces the _sql hack
      paginate: true                  # NEW: follow limit/offset until exhausted (cap: max_rows)
      max_rows: 50000
    formats: [pdf, xlsx]              # NEW; default derived from `output` suffix → today's behaviour
    output: reports/output/{{ now | strftime('%Y-%m') }}/{{ report_name }}   # stem, no extension
    columns: [dept, n]                # NEW optional: order/subset for data formats
    rename: {n: Headcount}
    vars: {company_name: Acme}
    provenance:                       # NEW, written by the AI author, read-only for humans
      generated_by: report_author
      model: corp-gpt
      prompt: "Headcount by department as KPI cards and a table"
      schema_hash: 9f2c…
```

### 4.3 Service API

```python
ReportService.run(name, *, formats=None, params=None, data=None, vars=None,
                  output=None) -> ReportResult
ReportService.render_inline(template: str | Path | None, rows, formats, output, vars) -> ReportResult
```

- `data=`: rows supplied by the caller (a workflow step). The service skips `_fetch`.
- `ReportResult.outputs: list[ReportArtifact]` with `path, format, bytes, sha256`.
  `output_path`/`format` stay as properties (the first artifact) so current callers keep working.

## 5. AI report-template authoring

### 5.1 `ReportAuthor` — `portwright/reports/author.py`

Inputs: `prompt`, a data spec (`from` block or inline rows), `formats`, `model` (from
`llm_models`), optional `base` gallery template, `sample_rows` (default from `report_ai.sample_rows`), optional `data_access` override (§5.5).

Pipeline:

1. **Profile data**: fetch up to `sample_rows` and infer each column's name, type, null rate,
   min/max/distinct count, and example values. The data access policy (§5.5) decides how much of
   this reaches the model. By default the model sees real rows.
2. **Build the prompt** from a fixed system prompt (`reports/prompts/author.md`) covering:
   - the template contract: `data`/`rows`, `record`, `row_count`, `vars`, `generated_at`, `report_name`;
   - the list of custom filters, generated from `filters.ALL_FILTERS` docstrings so it stays current;
   - PDF constraints: inline CSS only, no JS, no remote assets, page-break rules, and a note that
     xhtml2pdf ignores flex/grid;
   - the chosen `base` template as a worked example;
   - the column profile and the user's prompt.
   The model must return JSON: `{template, vars, columns, description, title}`.
3. **Validate** (deterministic, no model):
   - parse in `jinja2.sandbox.SandboxedEnvironment` with `StrictUndefined`;
   - test-render against the sample rows and against `[]` (the "no records" path);
   - static checks: no `<script>`, `on*=` handlers, `javascript:` URLs, remote `src`/`href`/`@import`/
     `url(http…)`, `{% include/import/extends %}` outside `templates/` and `.portwright/templates/`;
   - if `pdf` was requested, render a PDF to a temp file to prove the backend accepts it.
4. **Repair loop**: on failure, send the exact error back to the model, at most `max_repairs`
   times (default 2). Then fail with the last error and keep the draft in `.portwright/drafts/`.
5. **Persist**: write `.portwright/templates/<name>.html` (see §5.4) and upsert the `ReportSpec` with
   `provenance`. It never overwrites an existing template without `--force`. The previous version
   goes to `.portwright/templates/.history/<name>.<timestamp>.html`.

It reuses `load_models`/`create_provider` and the JSON-reply handling of the `llm` step. No new LLM
plumbing is needed.

### 5.2 Hardening the engine (all templates, not only AI ones)

- Switch `render_html` to `SandboxedEnvironment`. Built-in templates already work under it.
- Give WeasyPrint a `url_fetcher` that only allows `data:` and `file:` under the template folder
  and blocks remote fetches (SSRF). xhtml2pdf gets a `link_callback` with the same rule.
- Cap the render time and output size. *Implemented in phase 3 for AI templates as size limits: a template
  may be at most 200,000 characters and a test render at most 10 MB of HTML. Jinja's sandbox already caps
  `range()`, and `from.max_rows` bounds the input, so no separate timer was added.*

### 5.3 "Ask for a report" (agent end-to-end)

*Phase 3 ships `pw report ask` as a fixed pipeline, not a tool-using agent. A planning call sees a catalog of
GET endpoints and SQL tables (names and columns only, sources at level `none` left out) and returns
`{name, from, formats, design_prompt}`. That plan is checked (with up to `max_repairs` corrections), then goes
through `design`, then the report runs. The agent below needs the report and filesystem tools, so it moves
to phase 4.*

A built-in agent template `report_author` whose tools are the report and filesystem tools from §7:

```
list_report_sources → sample_data → draft_template → preview_report → save_report → run_report
```

`preview_report` returns validation errors plus a text rendering of the result, so the model can
check its own output. `save_report` needs scope `reports:write`. `run_report` needs `reports:run`.
A deterministic `report_design` workflow step (§6.2) covers the same path without agent autonomy.
Prefer it for scheduled jobs.

CLI:

```bash
pw report design dept-headcount --from-endpoint dept-summary \
   --prompt "Headcount by department: KPI cards on top, table below, highlight depts > 50" \
   --format pdf,xlsx --model corp-gpt --base summary_dashboard
pw report ask "Monthly salary cost by department for 2026 as PDF" --endpoint dept-summary
```

### 5.4 Where templates live

| Location | In git | Who writes it | Purpose |
|---|---|---|---|
| `templates/` | yes | people (Studio editor, gallery "Copy to project") | reviewed templates that production reports use |
| `.portwright/templates/` | no (`.portwright/` also holds the vault and run traces) | the AI author only | drafts and AI-generated templates, plus `.history/` |
| `portwright/reports/templates/` | package | shipped | built-in gallery |

- The AI author can only write to `.portwright/templates/`. It cannot touch `templates/`. Its
  filesystem root (`ai_templates`, §7.1) is the only writable template root it gets.
- `pw report promote <name>` (Studio: a **Promote to project** button) copies the AI template into
  `templates/`. It points the `ReportSpec` there and removes the `provenance.draft` flag. The
  `provenance` block stays, so the template's origin is still visible in git review.
- `pw validate` warns about any report whose template is under `.portwright/`. Such a report works
  locally but won't exist on another checkout until it is promoted.
- `pw init` adds `.portwright/` to the project's `.gitignore` if it isn't there.

### 5.5 AI data access policy

The configured models run locally and have no internet exposure, so **the AI may see real data
by default**. Each level below can only restrict access further. The effective policy is the most
restrictive one across levels.

Access levels, from most to least access:

| Level | What the model receives |
|---|---|
| `full` | up to `sample_rows` real rows, values untruncated |
| `sample` | up to `sample_rows` real rows, long values truncated to 200 chars |
| `profile` | column names, types and stats (null rate, min/max, distinct count), no row values |
| `schema` | column names and types only |
| `none` | AI authoring is refused for this data, so the template must be written by hand |

Where it is set:

```yaml
report_ai:                       # top-level: `reports:` is already the list of reports
  data_access: full              # project ceiling (default: full)
  sample_rows: 50                # default rows per authoring run (cap: 500)
  max_repairs: 2
  templates_dir: .portwright/templates

llm_models:
  corp-gpt:
    base_url: https://llm.corp.local/v1
    data_access: full            # per-model ceiling; set e.g. 'schema' for any externally hosted model

sources:
  hr:
    ai_data_access: profile      # per-source ceiling (applies to endpoints on this source too)
    ai_exclude_columns: [salary, ssn]   # always dropped before profiling, whatever the level
```

The same setting also exists on a single report (`ai_data_access`), on a `report_design` step
(`data_access:`) and on the CLI (`pw report design … --data-access schema --sample-rows 10`). At
these levels it can only lower the effective access, never raise it above a ceiling.

- `ai_exclude_columns` also applies to endpoint data (matched on the response field names).
- The resolved level and the columns sent are written to the template's `provenance`
  (`data_access: sample, columns_sent: [...]`) and to the audit log (`event=report_ai_author`). This
  shows exactly what each model saw.
- Studio's Design-with-AI dialog shows the effective level, with a dropdown limited to that level
  or lower, plus a preview of the payload before sending.
- Agents using `report_*` tools are subject to the same policy. `sample_data` returns data shaped
  by the effective level, so an agent can't get around it through tool calls. A plain SQL or endpoint tool on the
  same agent is governed by that tool's own scopes, as today.

## 6. New workflow nodes

`StepType` gains `report`, `report_design`, `copy`, `move` and `file`. `foreach.do` gains `report`,
`copy` and `move`.

### 6.1 `report`: run a report

```yaml
- id: build
  type: report
  report: dept-headcount              # a registered report …
  # template: templates/x.html        # … or inline (template + data)
  data: ${steps.query.output.rows}    # optional: skip the report's own fetch
  input: {dept: ${input.dept}}        # params for the report's source
  formats: [pdf, xlsx]                # overrides spec.formats
  vars: {period: ${input.period}}
```
Output: `{outputs: [{path, format, bytes, sha256}], row_count, seconds}`.
Later steps reference `${steps.build.output.outputs}`.

### 6.2 `report_design`: AI-author a template

```yaml
- id: design
  type: report_design
  model: corp-gpt
  report: dept-headcount              # name to create/update
  from: {endpoint: dept-summary}
  input: "Headcount by department as KPI cards and a table"   # the prompt (templated)
  formats: [pdf]
  sample_rows: 20
  overwrite: false                    # true → keeps history copy
```
Output: `{template, report, repairs, tokens}`. The step records tokens like `llm` steps do.

### 6.3 `copy` and `move`: deliver outputs to a destination folder

Copy and move are separate step types and separate canvas nodes. They share their fields and one
implementation (`Sandbox.transfer(mode=...)`).

```yaml
- id: publish
  type: copy                          # or: move
  files: ${steps.build.output.outputs}   # list of paths, or artifact dicts (uses .path)
  to: dest:finance_share/${run.date.year}/${run.date.month}
  overwrite: rename                   # never | always | rename | if_newer   (default never → error)
  create_dirs: true
  verify: sha256                      # re-hash at destination
  manifest: true                      # writes manifest.json beside the files
```
Output: `[{src, dest, address, bytes, sha256, action: copied|moved|skipped, renamed, replaced}]`.

- Writes are atomic: write `name.partial`, fsync, then rename. A crash never leaves a half-written
  PDF on the share.
- `move` deletes the source only after the destination copy is verified. A cross-volume move (local
  disk → UNC share) is a verified copy followed by a delete, never a bare rename. If the delete
  fails, the step reports `action: copied` with a warning rather than failing.
- `move` needs write mode on the source root as well as the destination.
- Destination access uses the identity of the process running portwright (the service account or
  scheduler account). Destinations have no credentials of their own in 0.9 (see §10).

### 6.4 `file`: other filesystem operations

```yaml
- id: bundle
  type: file
  op: zip                             # write | read | list | mkdir | delete | zip
  files: ${steps.build.output.outputs}
  to: reports:archive/${run.date.iso}.zip
```

- `zip` bundles `files` into one archive at `to`.
- `write` takes `content:` (text/JSON). This lets an LLM step's output be saved as a file.
- `delete` moves files to trash (§7.1).
- **New scope variables**: `${run.id}`, `${run.date}` (`year month day iso`, local time, zero padded),
  `${run.time}`, and `${run.started_at}` (UTC). Today
  only the report output path supports dates (Jinja), so workflows have no date tokens.

### 6.5 Example: query → design once → render → publish

```yaml
workflows:
  monthly-headcount:
    scopes: [reports:read, reports:run, fs:reports:read, fs:finance_share:write]
    steps:
      - {id: start, type: input, inputs: {dept: null}}
      - id: has_template
        type: condition
        when: ${input.redesign}
        then: design
        else: build
      - {id: design, type: report_design, model: corp-gpt, report: dept-headcount,
         from: {endpoint: dept-summary}, input: "KPI cards + table, highlight > 50", next: build}
      - {id: build, type: report, report: dept-headcount, input: {dept: ${input.dept}}, formats: [pdf, xlsx]}
      - {id: publish, type: copy, files: ${steps.build.output.outputs},
         to: "dest:finance_share/${run.date.year}-${run.date.month}", overwrite: rename, manifest: true}
      - {id: done, type: output, value: ${steps.publish.output}}
```

### 6.6 Studio

- `graph_vm.TYPES`/`OUT_PORTS` and the canvas palette get five nodes:

  | Node | Icon | Colour |
  |---|---|---|
  | **Report** | `DESCRIPTION` | indigo |
  | **Design report (AI)** | `AUTO_AWESOME` | purple |
  | **Copy** | `FILE_COPY` | teal |
  | **Move** | `DRIVE_FILE_MOVE` | orange (so a destructive move stands out from a copy) |
  | **Files** | `FOLDER` | grey, with an op dropdown for write/read/list/mkdir/delete/zip |

- Copy and Move nodes have the same form: a files picker that suggests `${steps.<report>.output.outputs}`
  from the upstream Report node, a destination picker (from `destinations` and writable roots)
  plus a sub-path box, and the overwrite, verify and manifest options. The Move node also shows a
  note: "source files are removed after a verified copy".
- `graph_vm` validation flags a Move whose source root is read-only.
- Reports screen: a **Design with AI** button opens a dialog with prompt, source picker, formats
  and model, and shows a live preview of the validated template. The gallery marks AI-made
  templates (under `.portwright/templates/`) with a badge, shows their provenance, and has a
  **Promote to project** action.
- New workflow templates in `agents/templates.py`: *query → report → copy*, and
  *ask an agent for a report*.

## 7. Filesystem sandbox, tools and MCP

### 7.1 Config

```yaml
filesystem:
  roots:                               # named, allow-listed places; tools never take raw paths
    reports:   {path: reports/output, mode: read_write}
    templates: {path: templates,      mode: read_write, patterns: ["*.html", "*.md.j2", "*.css"]}
    ai_templates: {path: .portwright/templates, mode: read_write, patterns: ["*.html", "*.md.j2", "*.css"]}
    data:      {path: data,           mode: read}
  max_file_mb: 50
  trash: .portwright/trash             # delete = move here (unless allow_delete: hard)
destinations:                          # write-only targets for `file` steps / fs_copy
  finance_share: {path: '\\fileserver\finance\reports', mode: write, overwrite: rename}
```
`destinations` paths must also appear in `security.allowed_folders`. That check happens in
`ProjectConfig` validation, so an absolute path can't reach the config without an admin decision.
`data_root` becomes the implicit `data` root. Destinations take no credentials: they are reached as
the account running portwright, so that account needs write access to the share.

The `report_author` agent's default tool config gets `ai_templates` as its only writable root. It
can read `templates/` (as examples) but cannot write there.

### 7.2 `portwright/fs/sandbox.py`

One class, `Sandbox(roots, audit)`. It is the only code that touches the disk for fs tools and file steps.

- `resolve("reports:2026-11/x.pdf")` works on `root:relative` addresses. It calls
  `Path.resolve(strict=False)`, then checks containment with `os.path.commonpath`. It rejects `..`,
  absolute inputs, NUL, NTFS alternate data streams (`:` after the drive), reserved device names
  (`CON`, `NUL`…), and symlinks or junctions that leave the root. Each check has a test.
- It enforces modes (read / write / read_write), `patterns`, and the size cap.
- Writes are atomic (temp file + `os.replace`). `delete` moves to trash unless `allow_delete: hard`.
- Every op is audited: `event=fs_<op>, actor, root, path, bytes, sha256`.

### 7.3 Tools (`agents/tools.py`)

New `ToolSpec` types:

```yaml
tools:
  files:   {type: filesystem, roots: [reports, templates], ops: [list, stat, read, write, copy, search]}
  reports: {type: report, reports: [dept-headcount], ops: [run, preview, design]}
```

| Tool | Args | Notes |
|---|---|---|
| `fs_list` | `path`, `glob?`, `recursive?` | capped at 500 entries |
| `fs_stat` | `path` | size, mtime, sha256 |
| `fs_read` | `path`, `max_chars?` | text; CSV/XLSX rendered as a row preview via `FileSource`; binary → metadata only |
| `fs_write` | `path`, `content`, `overwrite?` | text only, write roots only |
| `fs_copy` / `fs_move` | `src`, `dest`, `overwrite?` | `dest` may be `dest:<name>/…` |
| `fs_mkdir`, `fs_delete`, `fs_zip`, `fs_search` | … | `fs_search` = glob + optional text grep, capped |
| `report_run` | `report`, `params?`, `formats?` | returns artifacts |
| `report_preview` | `template` (text) or `report`, `sample_rows?` | validation errors + text snapshot |
| `report_design` | `report`, `prompt`, `from`, `formats?` | wraps `ReportAuthor` |
| `report_sources` | — | endpoints/sources the caller's scopes can read |

**Scopes.** Endpoint tools are authorised by the scoped API key. Fs and report tools run
in-process, so `ToolRuntime` checks `rt.scopes` itself before each call. The scopes are
`fs:<root>:read`, `fs:<root>:write`, `fs:dest:<name>`, `reports:run`, `reports:write`, and `*` still works.

### 7.4 MCP

```yaml
mcp_servers:
  analyst:
    transport: http
    scopes: [reports:run, fs:reports:read]
    expose:
      endpoints: [dept-summary]
      reports: [dept-headcount]            # → report_run_<name>, report_preview
      filesystem: [{root: reports, ops: [list, read, stat]}]
```

- `mcp_tools()` and `describe_server()` learn the two new sections, so `pw mcp list` shows them.
- **Binary results**: `fs_read` of a PDF or XLSX under `mcp.max_blob_mb` returns an MCP
  `EmbeddedResource` (base64 blob with a MIME type) instead of text. `_call` changes from
  "always TextContent" to "TextContent | EmbeddedResource".
- **Resources** (optional, phase 4): `list_resources` publishes `portwright://reports/<name>/latest`
  and `portwright://fs/<root>/<path>`, so MCP clients can attach outputs without a tool call.

## 8. Security summary

| Risk | Mitigation |
|---|---|
| LLM-written template runs code | `SandboxedEnvironment`, static checks, no include outside templates root |
| Template or PDF fetches remote URLs (SSRF, beaconing) | restricted `url_fetcher` / `link_callback` |
| Sensitive data reaching a model | data access levels with project, model and source ceilings, `ai_exclude_columns`, and an audit record of what was sent (§5.5) |
| Prompt injection through data rows sent to the model | system prompt marks data as untrusted, `sample` level truncates values, and the author gets **no tools**: it only returns JSON, which is validated before use |
| Path traversal / share escape | named roots + `Sandbox.resolve` checks + `allowed_folders` gate for destinations |
| Agent deletes or overwrites outputs | `delete` → trash, `overwrite: never` default, history copies for templates |
| Unattributed changes | audit events for every fs op, report run and template generation, plus `provenance` on specs |

## 9. Implementation plan

| Phase | Scope | Main files | Tests |
|---|---|---|---|
| 1 ✅ | Renderer registry, `formats`, `data=`, pagination, `from.query`, sandboxed env, URL fetcher, bug fixes (incl. file-source reports, which called a missing `FileSource.read_all`) | `reports/renderers.py`, `reports/service.py`, `reports/engine.py`, `config.py` | `test_reports.py` (each format, multi-format, pagination, sandbox escape attempts) |
| 2 ✅ | `fs/sandbox.py`, `filesystem`/`destinations` config, `copy`/`move`/`file` steps, `${run.*}` scope, `report` step | `fs/`, `agents/spec.py`, `agents/workflow.py`, `config.py` | `test_fs_sandbox.py` (traversal, ADS, junction, UNC), move = verified copy then delete (incl. cross-volume, delete failure), `test_workflow_files.py` (report → copy pipeline, scopes, foreach, canvas round trip). Also done early: Report / Copy / Move / Files canvas nodes with a YAML *Settings* field, and the canvas keeps any step keys it has no field for, so saving never drops them |
| 3 ✅ | `ReportAuthor`, `report_design` step (+ canvas node), `.portwright/templates/` storage, `pw report design/ask/promote`, `pw validate` warning, `pw init` adds `.portwright/` to an existing `.gitignore`, AI data access policy (§5.5) | `reports/author.py`, `reports/policy.py`, `reports/prompts/author.md`, `config.py`, `agents/spec.py`, `cli.py` (the `report_author` agent template moves to phase 4 with the tools it needs) | `test_report_author.py`, using a fake `provider_factory`: valid reply, invalid → repair, repairs exhausted; author cannot write `templates/`; promote; policy: most restrictive level wins, excluded columns never in payload, override cannot raise above ceiling |
| 4 | fs/report tool types, MCP expose + blob results + resources | `agents/tools.py`, `mcp/__init__.py` | scope enforcement per tool, `describe_server` output |
| 5 | Studio: Report / Design / Copy / Move / Files nodes, destination picker, Design-with-AI dialog, gallery badges + promote, docs 16/17/18 | `studio/graph_vm.py`, `studio/views/*`, `docs/*.md` | `test_studio_*` viewmodel tests |

Phases 1–2 ship value without any LLM: multi-format reports copied to a share on a schedule.
New optional extra: none needed for xlsx/csv/json/md. `reports` keeps jinja2 + weasyprint.

## 10. Decisions and open questions

Decided (2026-11-11):

- **Copy and move are separate nodes** (§6.3, §6.6). They are separate `copy` and `move` step types in YAML,
  with separate canvas nodes. The general `file` node covers the remaining operations.
- **No destination credentials in 0.9.** The service account running portwright is enough.
  A `credential:` field on destinations (SMB as another user) is deferred. The config shape leaves room for it.
- **Template locations** (§5.4). Hand-written templates live in `templates/`, in git. AI-generated
  templates live in `.portwright/templates/`, outside git, until someone promotes them with
  `pw report promote`.
- **AI may see real data** (§5.5). The models run locally with no internet exposure, so the
  default level is `full`. Project, model, source, report, step and CLI settings can restrict it
  down to `schema` or `none`, and `ai_exclude_columns` removes specific columns.

No open questions remain.
