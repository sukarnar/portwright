"""Jinja2 template editor for portwright reports.

Provides:
  - Full-screen code editor with monospace font
  - Quick-insert toolbar ({{ }}, {% if %}, {% for %}, {% set %}, {# #})
  - Click-to-insert filters and variables from the reference sidebar
  - Live HTML preview via WebView (renders actual HTML output)
  - Auto-preview toggle
  - Line / character counter in status bar
  - Find text in editor
"""
from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

import flet as ft

from ..common import Ctx, icon_btn

# ------------------------------------------------------------------ quick-insert toolbar
QUICK_INSERTS = [
    ("{{ }}", "{{ value }}", "Output a value"),
    ("{% if %}", "{% if condition %}\n\n{% endif %}", "Conditional block"),
    ("{% for %}", "{% for row in data %}\n  {{ row.field }}\n{% endfor %}", "Loop over rows"),
    ("{% set %}", "{% set var = value %}", "Assign a variable"),
    ("{# #}", "{# comment #}", "Template comment"),
    ("| filter", "{{ value | filter }}", "Apply a filter"),
]

# ------------------------------------------------------------------ reference data
VARIABLES = [
    ("data / rows", "list[dict]", "All data rows returned by the source",
     "{% for row in data %}\n  {{ row.field }}\n{% endfor %}"),
    ("record",      "dict",       "First row (single-record reports)",
     "{{ record.name }}"),
    ("row_count",   "int",        "Total number of rows",
     "{{ row_count }} records"),
    ("report_name", "str",        "Report name from config",
     "{{ report_name }}"),
    ("report_title","str",        "Report title",
     "{{ report_title }}"),
    ("generated_at","datetime",   "UTC datetime when the report was rendered",
     "{{ generated_at | datetime }}"),
    ("vars",        "dict",       "Extra variables from the report definition",
     "{{ vars.my_key }}"),
]

FILTERS = [
    ("strftime",       "fmt",                    "{{ now | strftime('%d %b %Y') }}",              "Format date/datetime with strftime"),
    ("date",           "",                        "{{ row.created | date }}",                      "YYYY-MM-DD shorthand"),
    ("datetime",       "",                        "{{ row.updated | datetime }}",                  "YYYY-MM-DD HH:MM shorthand"),
    ("currency",       "symbol='$', decimals=2",  "{{ row.amount | currency }}",                  "Format as $1,234.56"),
    ("number",         "decimals=0",              "{{ row.qty | number }}",                        "Format with thousands separator"),
    ("percent",        "decimals=1",              "{{ row.rate | percent }}",                      "0.456 → 45.6%"),
    ("nl2br",          "",                        "{{ row.notes | nl2br }}",                       "Newlines → <br>"),
    ("default_if_none","default=''",              "{{ row.val | default_if_none('N/A') }}",        "Show fallback when None"),
    ("yesno",          "yes='Yes', no='No'",      "{{ row.active | yesno }}",                     "Boolean to human label"),
    ("truncate_col",   "length=50",               "{{ row.desc | truncate_col(30) }}",             "Truncate long text"),
    ("sum_col",        "column",                  "{{ rows | sum_col('amount') }}",                "Sum a column across all rows"),
    ("avg_col",        "column",                  "{{ rows | avg_col('price') }}",                 "Average of a column"),
    ("groupby_col",    "column",                  "{% for k,grp in rows | groupby_col('dept') %}", "Group rows by column"),
]

SNIPPETS = {
    "Table (all columns)": """\
{% if data %}
<table>
  <thead>
    <tr>{% for col in data[0].keys() %}<th>{{ col | replace('_',' ') | title }}</th>{% endfor %}</tr>
  </thead>
  <tbody>
    {% for row in data %}
    <tr>{% for v in row.values() %}<td>{{ v | default_if_none }}</td>{% endfor %}</tr>
    {% endfor %}
  </tbody>
</table>
{% else %}
<p><em>No records found.</em></p>
{% endif %}""",

    "Table (specific columns)": """\
{% if data %}
<table>
  <thead>
    <tr><th>Name</th><th>Amount</th><th>Date</th></tr>
  </thead>
  <tbody>
    {% for row in data %}
    <tr>
      <td>{{ row.name | default_if_none }}</td>
      <td>{{ row.amount | currency }}</td>
      <td>{{ row.created | date }}</td>
    </tr>
    {% endfor %}
  </tbody>
</table>
{% endif %}""",

    "Grouped table": """\
{% for group_key, group_rows in rows | groupby_col('department') %}
<h2>{{ group_key }}</h2>
<table>
  <thead><tr><th>Name</th><th>Amount</th></tr></thead>
  <tbody>
    {% for row in group_rows %}
    <tr><td>{{ row.name }}</td><td>{{ row.amount | currency }}</td></tr>
    {% endfor %}
    <tr class="total"><td>Total</td><td>{{ group_rows | sum_col('amount') | currency }}</td></tr>
  </tbody>
</table>
{% endfor %}""",

    "Summary cards": """\
<div class="summary">
  <div class="stat"><span class="label">Total records</span><span class="value">{{ row_count }}</span></div>
  <div class="stat"><span class="label">Total amount</span><span class="value">{{ rows | sum_col('amount') | currency }}</span></div>
  <div class="stat"><span class="label">Generated</span><span class="value">{{ generated_at | datetime }}</span></div>
</div>""",

    "Conditional row style": """\
{% for row in data %}
<tr {% if row.status == 'error' %}class="error"{% elif row.status == 'warn' %}class="warn"{% endif %}>
  <td>{{ row.name }}</td>
  <td>{{ row.status }}</td>
</tr>
{% endfor %}""",

    "For loop with index": """\
{% for row in data %}
<tr>
  <td>{{ loop.index }}</td>
  <td>{{ row.name }}</td>
  {% if loop.last %}<td colspan="2"><strong>End of list</strong></td>{% endif %}
</tr>
{% endfor %}""",

    "Full starter template": """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>{{ report_name }}</title>
<style>
  body { font-family: Arial, sans-serif; font-size: 12px; color: #222; margin: 20mm; }
  h1   { font-size: 20px; color: #1a4e8a; border-bottom: 2px solid #1a4e8a; padding-bottom: 6px; }
  .meta { color: #666; font-size: 10px; margin-bottom: 16px; }
  table { border-spacing: 0; width: 100%; margin-top: 12px; }
  th { background: #1a4e8a; color: #fff; text-align: left; padding: 6px 8px; font-size: 11px; }
  td { padding: 5px 8px; border-bottom: 1px solid #ddd; }
  tr:nth-child(even) td { background: #f5f8fc; }
  .footer { margin-top: 20px; font-size: 9px; color: #999; text-align: right; }
</style>
</head>
<body>
<h1>{{ report_name | title }}</h1>
<p class="meta">Generated: {{ generated_at | datetime }} &nbsp;|&nbsp; {{ row_count }} record(s)</p>

{% if data %}
<table>
  <thead>
    <tr>{% for col in data[0].keys() %}<th>{{ col | replace('_',' ') | title }}</th>{% endfor %}</tr>
  </thead>
  <tbody>
    {% for row in data %}
    <tr>{% for v in row.values() %}<td>{{ v | default_if_none }}</td>{% endfor %}</tr>
    {% endfor %}
  </tbody>
</table>
{% else %}
<p><em>No records found.</em></p>
{% endif %}

<p class="footer">Portwright report &bull; {{ generated_at | strftime('%Y-%m-%d %H:%M UTC') }}</p>
</body>
</html>""",
}

JINJA2_GRAMMAR = """## Jinja2 Template Grammar

### Delimiters
| Syntax | Purpose |
|--------|---------|
| `{{ expr }}` | Output an expression |
| `{% stmt %}` | Statement (if, for, set, block…) |
| `{# comment #}` | Comment (not rendered) |

### Variables
Access dict keys with `.` or `[]`:
```
{{ row.name }}
{{ row['total amount'] }}
```

### Filters
Chain with `|`:
```
{{ row.amount | currency }}
{{ row.name | upper | truncate_col(20) }}
```

### If / elif / else
```
{% if row.active %}Active{% else %}Inactive{% endif %}
{% if score > 90 %}A{% elif score > 80 %}B{% else %}C{% endif %}
```

### For loop
```
{% for row in data %}
  {{ loop.index }} {{ row.name }}
{% endfor %}
```
Loop variables: `loop.index`, `loop.index0`, `loop.first`, `loop.last`, `loop.length`

### Set variable
```
{% set total = rows | sum_col('amount') %}
Total: {{ total | currency }}
```

### Whitespace control
Add `-` to trim whitespace:
```
{%- for row in data -%}
  {{ row.name }}
{%- endfor -%}
```

### Macros (reusable blocks)
```
{% macro badge(status) %}
  <span class="badge {{ status }}">{{ status }}</span>
{% endmacro %}
{{ badge(row.status) }}
```
"""


# ------------------------------------------------------------------ editor view
class TemplateEditorView:
    """Full-screen Jinja2 template editor with reference sidebar and WebView preview."""

    def __init__(self, ctx: Ctx, report_name: str, template_path: str, on_close):
        self.ctx = ctx
        self.report_name = report_name
        self.template_path = template_path
        self.on_close = on_close
        self._content = self._load_file()
        self._preview_tmp: str | None = None
        self._auto_preview = False
        self._sidebar_tab = "Variables"
        self._find_query = ""

        # Editor TextField
        self._editor = ft.TextField(
            value=self._content,
            multiline=True,
            min_lines=30,
            expand=True,
            text_style=ft.TextStyle(font_family="Courier New", size=13),
            border=ft.InputBorder.OUTLINE,
            content_padding=ft.Padding(left=12, top=12, right=12, bottom=12),
            on_change=self._on_edit,
        )

        # Status bar
        self._status = ft.Text("", size=11, color=ft.Colors.ON_SURFACE_VARIANT)
        self._stats = ft.Text("", size=11, color=ft.Colors.ON_SURFACE_VARIANT)

        # Preview area (HTML source with syntax colours + open-in-browser button)
        self._preview_text = ft.Text(
            "Click Preview to render with sample data.",
            font_family="Courier New", size=11,
            selectable=True, color=ft.Colors.ON_SURFACE_VARIANT,
        )
        self._preview_label = ft.Text(
            "Preview — click Preview to render",
            size=12, weight=ft.FontWeight.W_600,
            color=ft.Colors.ON_SURFACE_VARIANT,
        )
        self._open_browser_btn = ft.OutlinedButton(
            "Open in browser", icon=ft.Icons.OPEN_IN_BROWSER,
            visible=False, on_click=self._open_in_browser,
            tooltip="Open rendered HTML in your default browser",
        )

        # Find bar (hidden by default)
        self._find_field = ft.TextField(
            hint_text="Find in template…",
            dense=True,
            prefix_icon=ft.Icons.SEARCH,
            expand=True,
            on_submit=self._do_find,
            on_change=self._do_find,
        )
        self._find_count = ft.Text("", size=11, color=ft.Colors.ON_SURFACE_VARIANT)
        self._find_bar = ft.Row(
            [self._find_field, self._find_count,
             ft.IconButton(ft.Icons.CLOSE, icon_size=16,
                           on_click=self._close_find, tooltip="Close find bar")],
            spacing=6, visible=False,
        )

        # Auto-preview checkbox
        self._auto_cb = ft.Checkbox(
            label="Auto-preview on save",
            value=False,
            on_change=self._toggle_auto,
        )

        # Sidebar
        self._ref_content = ft.Column(scroll=ft.ScrollMode.AUTO, spacing=0, expand=True)
        self._build_reference()
        self._update_stats()

    # ---------------------------------------------------------------- file I/O
    def _load_file(self) -> str:
        try:
            p = Path(self.template_path)
            if not p.is_absolute():
                p = self.ctx.svc.project.root / p
            if p.exists():
                return p.read_text(encoding="utf-8")
        except Exception:
            pass
        return SNIPPETS["Full starter template"]

    def _save_file(self, _=None) -> None:
        try:
            p = Path(self.template_path)
            if not p.is_absolute():
                p = self.ctx.svc.project.root / p
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(self._editor.value or "", encoding="utf-8")
            self._set_status("✔ Saved", ft.Colors.GREEN_700)
            if self._auto_preview:
                self._run_preview()
                return
        except Exception as exc:
            self._set_status(f"✗ {exc}", ft.Colors.ERROR)
        self.ctx.page.update()

    def _on_edit(self, _) -> None:
        self._set_status("Unsaved changes", ft.Colors.AMBER_800)
        self._update_stats()
        if self._find_query:
            self._do_find(None)
        self.ctx.page.update()

    def _set_status(self, msg: str, color=ft.Colors.ON_SURFACE_VARIANT) -> None:
        self._status.value = msg
        self._status.color = color

    def _update_stats(self) -> None:
        text = self._editor.value or ""
        lines = text.count("\n") + 1 if text else 0
        chars = len(text)
        self._stats.value = f"{lines} lines  {chars} chars"

    def _toggle_auto(self, e) -> None:
        self._auto_preview = e.control.value

    # ---------------------------------------------------------------- find
    def _open_find(self, _=None) -> None:
        self._find_bar.visible = True
        self.ctx.page.update()

    def _close_find(self, _=None) -> None:
        self._find_bar.visible = False
        self._find_query = ""
        self._find_count.value = ""
        self.ctx.page.update()

    def _do_find(self, e) -> None:
        query = (e.control.value if e else self._find_query) or ""
        self._find_query = query
        if not query:
            self._find_count.value = ""
            self.ctx.page.update()
            return
        text = self._editor.value or ""
        count = len(re.findall(re.escape(query), text, re.IGNORECASE))
        self._find_count.value = f"{count} match{'es' if count != 1 else ''}"
        self.ctx.page.update()

    # ---------------------------------------------------------------- insert helpers
    def _insert(self, snippet: str) -> None:
        """Append snippet to editor content (Flet TextField has no cursor API)."""
        cur = self._editor.value or ""
        self._editor.value = cur + ("\n\n" if cur and not cur.endswith("\n") else "") + snippet
        self._set_status("Inserted", ft.Colors.ON_SURFACE_VARIANT)
        self._update_stats()
        self.ctx.page.update()

    # ---------------------------------------------------------------- preview
    def _run_preview(self, _=None) -> None:
        from ...reports.engine import render_html
        try:
            # Save current editor content to template file first so engine picks it up
            p = Path(self.template_path)
            if not p.is_absolute():
                p = self.ctx.svc.project.root / p
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(self._editor.value or "", encoding="utf-8")

            # Load vars from the report spec if available
            preview_vars: dict = {}
            try:
                spec = self.ctx.svc.reports.get(self.report_name)
                preview_vars = dict(getattr(spec, "vars", None) or {})
            except Exception:
                pass

            html = render_html(
                self.template_path,
                _sample_data(),
                project_root=self.ctx.svc.project.root,
                report_name=self.report_name,
                extra_vars=preview_vars or None,
            )
            # Write to temp file (for browser open)
            if self._preview_tmp:
                try:
                    Path(self._preview_tmp).unlink(missing_ok=True)
                except Exception:
                    pass
            with tempfile.NamedTemporaryFile(
                suffix=".html", delete=False, mode="w", encoding="utf-8"
            ) as f:
                f.write(html)
                self._preview_tmp = f.name

            # Show colourised source in the panel
            lines = html.splitlines()
            shown = "\n".join(lines[:150])
            if len(lines) > 150:
                shown += f"\n… ({len(lines) - 150} more lines — open in browser to see all)"
            self._preview_text.value = shown
            self._preview_text.color = None
            self._preview_label.value = f"Preview  ({len(lines)} lines)"
            self._preview_label.color = ft.Colors.PRIMARY
            self._open_browser_btn.visible = True
            self._set_status("✔ Preview rendered", ft.Colors.GREEN_700)
        except Exception as exc:
            self._set_status(f"✗ Preview error: {exc}", ft.Colors.ERROR)
        self.ctx.page.update()

    def _open_in_browser(self, _=None) -> None:
        if self._preview_tmp and Path(self._preview_tmp).exists():
            os.startfile(self._preview_tmp)

    # ---------------------------------------------------------------- reference sidebar
    def _build_reference(self) -> None:
        self._tab_panels = {
            "Variables": self._variables_tab(),
            "Filters":   self._filters_tab(),
            "Snippets":  self._snippets_tab(),
            "Grammar":   self._grammar_tab(),
        }
        self._active_tab = "Variables"
        self._tab_body = ft.Container(
            content=self._tab_panels["Variables"],
            expand=True,
            border=ft.Border(top=ft.BorderSide(1, ft.Colors.OUTLINE_VARIANT)),
            padding=4,
        )
        self._tab_buttons = ft.Row(
            [self._tab_btn(t) for t in self._tab_panels],
            spacing=2,
        )
        self._ref_content.controls = [self._tab_buttons, self._tab_body]

    def _tab_btn(self, label: str) -> ft.Control:
        is_active = label == self._active_tab
        return ft.TextButton(
            label,
            on_click=lambda _, l=label: self._switch_tab(l),
            style=ft.ButtonStyle(
                bgcolor=ft.Colors.PRIMARY_CONTAINER if is_active else None,
                color=ft.Colors.ON_PRIMARY_CONTAINER if is_active else ft.Colors.ON_SURFACE_VARIANT,
                padding=ft.Padding(left=10, top=4, right=10, bottom=4),
            ),
        )

    def _switch_tab(self, label: str) -> None:
        self._active_tab = label
        self._tab_body.content = self._tab_panels[label]
        self._tab_buttons.controls = [self._tab_btn(t) for t in self._tab_panels]
        self.ctx.page.update()

    def _variables_tab(self) -> ft.Control:
        rows = []
        for name, typ, desc, example in VARIABLES:
            def _click(_, ex=example):
                self._insert(ex)
            rows.append(ft.Container(
                padding=ft.Padding(left=8, top=6, right=8, bottom=6),
                border=ft.Border(bottom=ft.BorderSide(1, ft.Colors.OUTLINE_VARIANT)),
                ink=True,
                on_click=_click,
                tooltip="Click to insert example",
                content=ft.Column([
                    ft.Row([
                        ft.Text(f"{{{{ {name} }}}}", font_family="Courier New",
                                size=12, color=ft.Colors.PRIMARY, selectable=True),
                        ft.Container(expand=True),
                        ft.Text(typ, size=10, italic=True,
                                color=ft.Colors.ON_SURFACE_VARIANT),
                    ]),
                    ft.Text(desc, size=11, color=ft.Colors.ON_SURFACE_VARIANT),
                    ft.Container(
                        content=ft.Text(example, font_family="Courier New", size=10,
                                        color=ft.Colors.TERTIARY, selectable=True),
                        bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST,
                        border_radius=4,
                        padding=ft.Padding(left=6, top=3, right=6, bottom=3),
                    ),
                ], spacing=2),
            ))
        rows.append(ft.Container(
            padding=ft.Padding(left=8, top=8, right=8, bottom=4),
            content=ft.Text("Click any row to insert the example.",
                            size=10, italic=True,
                            color=ft.Colors.ON_SURFACE_VARIANT),
        ))
        return ft.Column(rows, scroll=ft.ScrollMode.AUTO, spacing=0, expand=True)

    def _filters_tab(self) -> ft.Control:
        rows = []
        for fname, args, example, desc in FILTERS:
            def _click(_, ex=example):
                self._insert(ex)
            rows.append(ft.Container(
                padding=ft.Padding(left=8, top=8, right=8, bottom=8),
                border=ft.Border(bottom=ft.BorderSide(1, ft.Colors.OUTLINE_VARIANT)),
                ink=True,
                on_click=_click,
                tooltip="Click to insert example",
                content=ft.Column([
                    ft.Row([
                        ft.Text(fname, font_family="Courier New", size=12,
                                weight=ft.FontWeight.W_600, color=ft.Colors.SECONDARY),
                        ft.Container(expand=True),
                        ft.Text(f"({args})" if args else "", size=10, italic=True,
                                color=ft.Colors.ON_SURFACE_VARIANT),
                    ]),
                    ft.Text(desc, size=11, color=ft.Colors.ON_SURFACE_VARIANT),
                    ft.Container(
                        content=ft.Text(example, font_family="Courier New", size=10,
                                        selectable=True, color=ft.Colors.TERTIARY),
                        bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST,
                        border_radius=4,
                        padding=ft.Padding(left=6, top=3, right=6, bottom=3),
                    ),
                ], spacing=3),
            ))
        rows.append(ft.Container(
            padding=ft.Padding(left=8, top=8, right=8, bottom=4),
            content=ft.Text("Click any row to insert the example.",
                            size=10, italic=True,
                            color=ft.Colors.ON_SURFACE_VARIANT),
        ))
        return ft.Column(rows, scroll=ft.ScrollMode.AUTO, spacing=0, expand=True)

    def _snippets_tab(self) -> ft.Control:
        rows = []
        for label, code in SNIPPETS.items():
            def _insert_snippet(_, c=code):
                self._insert(c)
            rows.append(ft.Container(
                padding=ft.Padding(left=8, top=6, right=8, bottom=6),
                border=ft.Border(bottom=ft.BorderSide(1, ft.Colors.OUTLINE_VARIANT)),
                ink=True,
                on_click=_insert_snippet,
                tooltip="Click to insert snippet",
                content=ft.Row([
                    ft.Icon(ft.Icons.CODE, size=14, color=ft.Colors.ON_SURFACE_VARIANT),
                    ft.Text(label, size=12, expand=True),
                    ft.Text("insert →", size=10, color=ft.Colors.PRIMARY),
                ], spacing=8),
            ))
        return ft.Column(rows, scroll=ft.ScrollMode.AUTO, spacing=0, expand=True)

    def _grammar_tab(self) -> ft.Control:
        return ft.Column([
            ft.Container(
                content=ft.Markdown(
                    JINJA2_GRAMMAR,
                    extension_set=ft.MarkdownExtensionSet.GITHUB_WEB,
                    code_theme=ft.MarkdownCodeTheme.ATOM_ONE_DARK,
                    selectable=True,
                ),
                padding=ft.Padding(left=8, top=8, right=8, bottom=8),
            )
        ], scroll=ft.ScrollMode.AUTO, expand=True)

    # ---------------------------------------------------------------- build
    def render(self) -> ft.Control:
        # ---- top toolbar ----
        toolbar = ft.Row([
            ft.Text(f"Template: {self.template_path}", size=13,
                    weight=ft.FontWeight.W_500, expand=True,
                    color=ft.Colors.ON_SURFACE_VARIANT),
            self._auto_cb,
            ft.FilledButton("Save", icon=ft.Icons.SAVE, on_click=self._save_file),
            ft.OutlinedButton("Preview", icon=ft.Icons.VISIBILITY, on_click=self._run_preview),
            ft.IconButton(ft.Icons.SEARCH, tooltip="Find in template",
                          on_click=self._open_find),
            ft.OutlinedButton("Close", icon=ft.Icons.CLOSE, on_click=lambda _: self.on_close()),
        ], spacing=8)

        # ---- quick-insert toolbar ----
        quick_btns = []
        for label, snippet, tip in QUICK_INSERTS:
            def _qi(_, s=snippet, l=label):
                self._insert(s)
                self._set_status(f"Inserted {l}", ft.Colors.ON_SURFACE_VARIANT)
                self.ctx.page.update()
            quick_btns.append(
                ft.TextButton(
                    label,
                    tooltip=tip,
                    on_click=_qi,
                    style=ft.ButtonStyle(
                        bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST,
                        color=ft.Colors.ON_SURFACE,
                        padding=ft.Padding(left=8, top=2, right=8, bottom=2),
                        shape=ft.RoundedRectangleBorder(radius=4),
                    ),
                )
            )
        quick_bar = ft.Row(
            [ft.Text("Insert:", size=11, color=ft.Colors.ON_SURFACE_VARIANT)] + quick_btns,
            spacing=4,
        )

        # ---- editor column ----
        editor_col = ft.Column([
            ft.Row([
                ft.Text("Editor", size=12, weight=ft.FontWeight.W_600,
                        color=ft.Colors.ON_SURFACE_VARIANT),
                ft.Container(expand=True),
                self._stats,
            ]),
            quick_bar,
            self._find_bar,
            self._editor,
            ft.Row([
                ft.Icon(ft.Icons.INFO_OUTLINE, size=14,
                        color=ft.Colors.ON_SURFACE_VARIANT),
                self._status,
            ], spacing=6),
        ], spacing=8, expand=2)

        # ---- preview column ----
        preview_col = ft.Column([
            ft.Row([
                self._preview_label,
                ft.Container(expand=True),
                self._open_browser_btn,
                ft.Text("Sample data", size=10,
                        color=ft.Colors.ON_SURFACE_VARIANT, italic=True),
            ]),
            ft.Container(
                content=ft.Column(
                    [self._preview_text],
                    scroll=ft.ScrollMode.AUTO,
                    expand=True,
                ),
                expand=True,
                border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
                border_radius=8,
                padding=12,
                bgcolor=ft.Colors.SURFACE_CONTAINER_LOWEST,
            ),
        ], spacing=8, expand=2)

        # ---- reference sidebar ----
        ref_col = ft.Column([
            ft.Text("Reference", size=12, weight=ft.FontWeight.W_600,
                    color=ft.Colors.ON_SURFACE_VARIANT),
            ft.Container(
                content=self._ref_content,
                expand=True,
                border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
                border_radius=8,
            ),
        ], spacing=8, expand=1)

        main_row = ft.Row(
            [editor_col, preview_col, ref_col],
            spacing=12, expand=True,
            vertical_alignment=ft.CrossAxisAlignment.START,
        )

        return ft.Column([toolbar, main_row], spacing=12, expand=True)


# ------------------------------------------------------------------ sample data helper
def _sample_data() -> list[dict]:
    """Generic employee sample — also doubles as accounting rows via compatible keys."""
    return [
        {"id": 1, "account_code": "1001", "account_name": "Cash",           "category": "Assets",      "description": "Opening balance",         "date": "2024-01-01", "reference": "OB-001",  "debit": 50000.00, "credit":     0.00, "balance":  50000.00},
        {"id": 2, "account_code": "1002", "account_name": "Accounts Rec.",  "category": "Assets",      "description": "Invoice INV-042",          "date": "2024-01-10", "reference": "INV-042", "debit": 12300.50, "credit":     0.00, "balance":  62300.50},
        {"id": 3, "account_code": "2001", "account_name": "Accounts Pay.",  "category": "Liabilities", "description": "Supplier payment",         "date": "2024-01-12", "reference": "PO-018",  "debit":     0.00, "credit":  8500.00, "balance": -8500.00},
        {"id": 4, "account_code": "2002", "account_name": "Tax Payable",    "category": "Liabilities", "description": "GST Q1",                   "date": "2024-01-31", "reference": "TAX-Q1",  "debit":     0.00, "credit":  1230.05, "balance": -1230.05},
        {"id": 5, "account_code": "4001", "account_name": "Sales Revenue",  "category": "Revenue",     "description": "Product sales Jan",        "date": "2024-01-31", "reference": "REV-JAN", "debit":     0.00, "credit": 24600.00, "balance":  24600.00},
        {"id": 6, "account_code": "5001", "account_name": "Cost of Goods",  "category": "Expenses",    "description": "COGS Jan",                 "date": "2024-01-31", "reference": "COGS-JAN","debit": 10500.00, "credit":     0.00, "balance": -10500.00},
        {"id": 7, "account_code": "5002", "account_name": "Salaries",       "category": "Expenses",    "description": "Payroll run 2024-01",      "date": "2024-01-31", "reference": "PAY-001", "debit": 18200.00, "credit":     0.00, "balance": -18200.00},
        {"id": 8, "account_code": "5003", "account_name": "Office Supplies","category": "Expenses",    "description": "Stationery and consumables","date": "2024-01-15", "reference": "EXP-007", "debit":   320.75, "credit":     0.00, "balance":   -320.75},
    ]
