---
title: Reports & Templates
screen: reports
---

Generate HTML or PDF reports from any endpoint or data source using Jinja2 HTML templates.
A report definition links a data source to a template file. At run time the engine fetches
the data, renders the template, and writes the output file.

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
| Template | Yes | Path to a Jinja2 HTML template file |
| Endpoint | Either/or | REST endpoint whose response provides the data rows |
| Source | Either/or | Named data source from `portwright.yaml` |
| Output format | Yes | `html` or `pdf` |
| Output path | No | Destination folder (defaults to `./reports/`) |
| Params | No | Default query parameters sent with every run |

Supply either an **Endpoint** or a **Source** — not both. The dropdowns are populated from the
live project configuration so only valid names appear.

### Running a report

Click ▶ on a report row. The engine will:

1. Fetch data from the configured endpoint or source.
2. Render the Jinja2 template with that data.
3. Write the HTML or PDF file to the output path.

A success toast shows the output file path. On failure the error is shown in a dialog.

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
| `--open` | Open the output file after generation |
| `--param key=value` | Pass an extra query parameter (repeatable) |
| `--format html\|pdf` | Override the output format |
| `--out <path>` | Override the output file path |

```
portwright report add <name> --template <path> --endpoint <name> [--format pdf]
```
Add a report definition from the CLI.

```
portwright report remove <name>
```
Remove a report definition.

### Jinja2 template variables

| Variable | Type | Description |
|---|---|---|
| `data` / `rows` | list of dicts | Rows from the data source |
| `row_count` | int | Number of rows |
| `report_name` | str | Report definition name |
| `report_title` | str | Report title |
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

### Troubleshooting

| Symptom | Fix |
|---|---|
| `cannot load library libgobject-2.0-0` | WeasyPrint needs GTK. Run: `pip uninstall weasyprint -y && pip install xhtml2pdf` |
| `Ignoring CSS: border-collapse` | xhtml2pdf limitation — use `border-spacing: 0` instead |
| Endpoint auth error | Run `portwright endpoint set-access <name> --public` |
| `No module named 'portwright'` | `.pth` file must point to the **parent** of the `portwright/` folder |
