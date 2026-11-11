---
title: Reports & Templates
screen: reports
---

Generate reports from any endpoint or data source in one or more formats:

| Format | Made from | Needs |
|---|---|---|
| `pdf` | the Jinja2 HTML template | `jinja2` + WeasyPrint or xhtml2pdf |
| `html` | the Jinja2 HTML template | `jinja2` |
| `xlsx` | the rows (bold header, frozen top row, filters, column widths) | nothing extra |
| `csv` | the rows (UTF-8 with BOM so Excel reads accents) | nothing extra |
| `json` | the rows (a list of objects) | nothing extra |
| `md` | the rows (a Markdown table) | nothing extra |

A report definition links a data source to its formats (and to a template when it makes PDF or
HTML). At run time the engine fetches the data once, renders every format, and writes one file
per format next to each other: `reports/output/sales.pdf`, `reports/output/sales.xlsx`, ….

<!-- studio -->

## In Studio

### Reports screen

Open the **Reports** screen from the left navigation bar (document icon). The list shows all
report definitions in the current project.

Each row has four action buttons:

| Button | Action |
|---|---|
| ▶ Play | Run the report immediately |
| `<>` Code | Open the Template Editor |
| ✏ Pencil | Edit the report definition |
| 🗑 Trash | Delete the report definition |

### Adding a report

Click **+ Add Report**. Fill in the form fields and click **Save**.

| Field | Required | Description |
|---|---|---|
| Name | Yes | Unique identifier; used as the output filename stem |
| Title | No | Heading shown inside the report |
| Template | For PDF/HTML | Path to a Jinja2 HTML template file |
| Endpoint | Either/or | REST endpoint whose response provides the data rows |
| Source | Either/or | Named data source from `portwright.yaml` |
| SQL query | SQL sources | One `SELECT`; bind values with `:name` (see below) |
| Output formats | Yes | Tick one or more: PDF, HTML, MD, CSV, XLSX, JSON |
| Output path | No | Path *stem* (defaults to `reports/output/<name>`); each format adds its suffix |
| Params | No | Default query parameters sent with every run |

Supply either an **Endpoint** or a **Source** — not both. The dropdowns are populated from the
live project configuration so only valid names appear.

### Running a report

Click ▶ on a report row. The engine will:

1. Fetch data from the configured endpoint or source.
2. Render each ticked format (the dialog starts with the report's own formats).
3. Write one file per format to the output path.

The dialog lists every file written. On failure the error is shown in the dialog.

**Access note:** If the endpoint requires authentication, make sure the project server is
running (`portwright serve`) and the endpoint allows public access, or run:

```
portwright endpoint set-access <endpoint-name> --public
```

### Template Gallery

Click the gallery icon in the report toolbar to browse built-in and project templates.

**Built-in templates:**

| Template | Best for |
|---|---|
| `simple_table.html` | Any flat data set — all columns, alternating row shading |
| `summary_dashboard.html` | Numeric data — auto KPI cards + totals row |
| `grouped_by_category.html` | Categorised data — group headers and per-group sub-totals |
| `invoice.html` | Line-item billing — company/client headers, tax, total due |
| `audit_log.html` | Event logs — colour-coded action badges (create/update/delete/login) |

Click **Use** to pre-fill the template path in the report editor, or **Copy to project** to
copy the file to `<project root>/templates/` for editing.

### Template Editor

Click `<>` on a report row to open the full-screen editor.

| Panel | Description |
|---|---|
| Left — Editor | Jinja2 source. **Save** persists the file; **Preview** renders with sample data. |
| Centre — Preview | Live HTML preview rendered with dummy data. |
| Right — Reference | Four tabs: Variables, Filters, Snippets, Grammar. |

Custom templates are saved to `<project root>/templates/` and appear in the gallery under
**Project Templates**.

<!-- cli -->

## With the CLI

```
portwright report list
```
List all report definitions in the current project.

```
portwright report show <name>
```
Show the full definition for a report.

```
portwright report run <name>
```
Run a report. Options:

| Option | Description |
|---|---|
| `--format pdf,xlsx` / `-f pdf -f xlsx` | Override the formats (repeatable or comma-separated) |
| `--html` | HTML only (same as `--format html`) |
| `--param key=value` | Pass a parameter to the endpoint or SQL query (repeatable) |
| `--var key=value` | Extra template variable (repeatable) |
| `--output <path>` | Override the output path stem |
| `--open` | Open the first output file after generation |

```
portwright report add <name> --endpoint <name> --format pdf,xlsx --template <path>
portwright report add <name> --source hr --format xlsx,csv     --query "select dept, count(*) n from employees where (:dept is null or dept = :dept) group by dept"
```
Add a report definition from the CLI. `--template` is only needed for PDF or HTML.

### Report definition in `portwright.yaml`

```yaml
reports:
  - name: dept-headcount
    template: templates/dept_headcount.html   # only for pdf / html
    from:
      endpoint: dept-summary        # or: source: hr  + query: "select …"
      params: {dept: null}
      paginate: true                # read every page of a paginated endpoint (default)
      max_rows: 50000               # stop here; the run reports a warning when it does
    formats: [pdf, xlsx]            # default: pdf (or html when output ends in .html)
    output: reports/output/{{ now | strftime('%Y-%m') }}/{{ report_name }}
    columns: [dept, n]              # csv / xlsx / json / md: order and subset of columns
    rename: {n: Headcount}          # csv / xlsx / json / md: header names
    vars: {company_name: Acme}
```

- **SQL queries** run as one read-only `SELECT` (or `WITH … SELECT`); `;` and write statements are
  refused. Values are always bound, never pasted into the SQL: `:dept` takes `--param dept=ENG`, and
  an empty value binds `NULL`, so `(:dept is null or dept = :dept)` makes a filter optional.
- **Endpoints** are read page by page (`limit`/`offset`) until the last page, up to `max_rows`.
  Set `paginate: false` to read only the first page.
- Excel cells that start with `=` are stored as text, so data can't turn into a formula.

Remove a report definition with:

```
portwright report remove <name>
```

### Jinja2 template variables

| Variable | Type | Description |
|---|---|---|
| `data` / `rows` | list of dicts | Rows from the data source |
| `row_count` | int | Number of rows |
| `report_name` | str | Report definition name |
| `record` | dict | The single row, when there is exactly one |
| `generated_at` | datetime | UTC timestamp of this run |
| `vars` | dict | Extra variables from the definition |

### Custom filters

| Filter | Example | Description |
|---|---|---|
| `datetime` | `{{ ts \| datetime }}` | Format as `YYYY-MM-DD HH:MM` |
| `date` | `{{ ts \| date }}` | Date part only |
| `strftime` | `{{ ts \| strftime('%d %b %Y') }}` | Custom strftime format |
| `currency` | `{{ v \| currency }}` | Format as `$1,234.56` |
| `number` | `{{ v \| number(2) }}` | Round and format |
| `default_if_none` | `{{ x \| default_if_none('-') }}` | Fallback for `None` |
| `truncate_col` | `{{ t \| truncate_col(60) }}` | Truncate long strings |
| `sum_col` | `{{ rows \| sum_col('amount') }}` | Sum a column |
| `groupby_col` | `{% for k, grp in rows \| groupby_col('dept') %}` | Group rows by column |

### Template safety

Templates render in Jinja2's **sandbox**: they can format and loop over data but cannot reach Python
internals (for example `''.__class__`). PDF rendering only loads images and stylesheets that are
inline (`data:` URLs) or files inside the template's own folder; `http(s)://` URLs, network shares and
files elsewhere are skipped. Put logos and CSS next to the template, e.g. `templates/img/logo.png`.

### Troubleshooting

| Symptom | Fix |
|---|---|
| `cannot load library libgobject-2.0-0` | WeasyPrint needs GTK. Install xhtml2pdf (`pip install xhtml2pdf`) – portwright falls back to it automatically |
| Image or stylesheet missing in the PDF | Move it into the template's folder; remote and outside files are blocked |
| `format(s) ['pdf'] need a template` | Add `template:` or pick a data format (xlsx, csv, json, md) |
| `Ignoring CSS: border-collapse` | xhtml2pdf limitation — use `border-spacing: 0` instead |
| Endpoint auth error | Run `portwright endpoint set-access <name> --public` |
| `No module named 'portwright'` | `.pth` file must point to the **parent** of the `portwright/` folder |
