"""Template Gallery screen – browse built-in templates, preview, copy to project, add custom."""
from __future__ import annotations

import shutil
from pathlib import Path

import flet as ft

from ..common import Ctx, empty, header, icon_btn, text_field

# ------------------------------------------------------------------ built-in catalogue
BUILTIN_DIR = Path(__file__).parent.parent.parent / "reports" / "templates"

BUILTIN_META = {
    "simple_table.html": {
        "title": "Simple Table",
        "description": "Straightforward data table — all columns, alternating row colours, page footer.",
        "use_case": "Any flat data list: employees, products, orders.",
        "icon": ft.Icons.TABLE_ROWS,
        "color": ft.Colors.BLUE_700,
    },
    "summary_dashboard.html": {
        "title": "Summary Dashboard",
        "description": "KPI cards at the top (auto-detected numeric columns) followed by a detail table with totals row.",
        "use_case": "Sales summaries, financial overviews, operational metrics.",
        "icon": ft.Icons.DASHBOARD,
        "color": ft.Colors.INDIGO_700,
    },
    "grouped_by_category.html": {
        "title": "Grouped by Category",
        "description": "Rows grouped by the first column into sections, each with a sub-total row.",
        "use_case": "Expenses by department, sales by region, tasks by status.",
        "icon": ft.Icons.FOLDER_COPY,
        "color": ft.Colors.TEAL_700,
    },
    "invoice.html": {
        "title": "Invoice",
        "description": "Professional invoice layout with company/client details, line items, tax and total. Pass extra vars for company name, client, invoice number.",
        "use_case": "Billing, receipts, purchase orders.",
        "icon": ft.Icons.RECEIPT_LONG,
        "color": ft.Colors.GREEN_700,
    },
    "audit_log.html": {
        "title": "Audit Log",
        "description": "Numbered log table with colour-coded action badges (create/update/delete/login) and smart date formatting.",
        "use_case": "Change history, access logs, compliance reports.",
        "icon": ft.Icons.HISTORY,
        "color": ft.Colors.RED_700,
    },
    "financial_accounting.html": {
        "title": "Financial Accounting",
        "description": "Double-entry style report grouped by account category, with debit/credit columns, sub-totals, grand total, and KPI summary cards. Adapts to whichever columns are present.",
        "use_case": "General ledger, trial balance, expense report, journal entries.",
        "icon": ft.Icons.ACCOUNT_BALANCE,
        "color": ft.Colors.DEEP_PURPLE_700,
    },
}


def _builtin_templates() -> list[dict]:
    out = []
    for fname, meta in BUILTIN_META.items():
        path = BUILTIN_DIR / fname
        out.append({
            "file": fname,
            "path": path,
            "exists": path.exists(),
            **meta,
        })
    return out


def _project_templates(project_root: Path) -> list[dict]:
    tpl_dir = project_root / "templates"
    if not tpl_dir.exists():
        return []
    return [
        {"file": p.name, "path": p, "title": p.stem.replace("_", " ").replace("-", " ").title(),
         "description": "Custom project template", "use_case": "",
         "icon": ft.Icons.DESCRIPTION, "color": ft.Colors.GREY_700, "exists": True}
        for p in sorted(tpl_dir.glob("*.html"))
    ]


class TemplateGalleryView:
    """Browse built-in + project templates, preview source, copy to project, create blank."""

    def __init__(self, ctx: Ctx, on_close, on_use=None):
        self.ctx = ctx
        self.on_close = on_close
        self.on_use = on_use   # callback(template_path: str) when user clicks Use
        self._selected: dict | None = None
        self._preview_content = ft.Column(scroll=ft.ScrollMode.AUTO, expand=True, spacing=0)
        self._list_col = ft.Column(scroll=ft.ScrollMode.AUTO, expand=True, spacing=8)
        self._new_name = text_field("New template name", hint="my-report", expand=True)
        self._status = ft.Text("", size=11, color=ft.Colors.ON_SURFACE_VARIANT)
        self._refresh_list()

    # ---------------------------------------------------------------- list
    def _refresh_list(self) -> None:
        builtins = _builtin_templates()
        project  = _project_templates(self.ctx.svc.project.root)

        controls: list[ft.Control] = []

        controls.append(ft.Text("Built-in templates", size=12,
                                weight=ft.FontWeight.W_600,
                                color=ft.Colors.ON_SURFACE_VARIANT))
        for t in builtins:
            controls.append(self._template_card(t, builtin=True))

        controls.append(ft.Divider())
        controls.append(ft.Text("Project templates  (templates/ folder)",
                                size=12, weight=ft.FontWeight.W_600,
                                color=ft.Colors.ON_SURFACE_VARIANT))
        if project:
            for t in project:
                controls.append(self._template_card(t, builtin=False))
        else:
            controls.append(ft.Text("No custom templates yet — copy a built-in or create a blank one below.",
                                    size=11, color=ft.Colors.ON_SURFACE_VARIANT, italic=True))

        self._list_col.controls = controls

    def _template_card(self, t: dict, builtin: bool) -> ft.Control:
        is_sel = self._selected and self._selected["file"] == t["file"]
        actions = []
        if builtin:
            actions.append(ft.TextButton(
                "Copy to project",
                icon=ft.Icons.COPY,
                on_click=lambda _, tmpl=t: self._copy_to_project(tmpl),
            ))
        if self.on_use:
            actions.append(ft.FilledButton(
                "Use",
                icon=ft.Icons.CHECK,
                on_click=lambda _, tmpl=t: self._use(tmpl),
            ))

        return ft.Container(
            border=ft.Border.all(2 if is_sel else 1,
                                 t["color"] if is_sel else ft.Colors.OUTLINE_VARIANT),
            border_radius=8,
            padding=12,
            bgcolor=ft.Colors.SURFACE_CONTAINER_LOW if is_sel else None,
            on_click=lambda _, tmpl=t: self._select(tmpl),
            content=ft.Column([
                ft.Row([
                    ft.Icon(t["icon"], color=t["color"], size=20),
                    ft.Text(t["title"], size=13, weight=ft.FontWeight.W_600, expand=True),
                    *actions,
                    icon_btn(ft.Icons.CODE, "View source",
                             lambda _, tmpl=t: self._preview(tmpl),
                             ft.Colors.ON_SURFACE_VARIANT),
                ], spacing=8),
                ft.Text(t["description"], size=11, color=ft.Colors.ON_SURFACE_VARIANT),
                ft.Text(t["use_case"], size=10, color=t["color"], italic=True) if t["use_case"] else ft.Container(),
            ], spacing=4),
        )

    # ---------------------------------------------------------------- actions
    def _select(self, t: dict) -> None:
        self._selected = t
        self._refresh_list()
        self._preview(t)
        self.ctx.page.update()

    def _preview(self, t: dict) -> None:
        try:
            src = t["path"].read_text(encoding="utf-8") if t["exists"] else "(file not found)"
        except Exception as exc:
            src = f"Error reading file: {exc}"
        self._preview_content.controls = [
            ft.Text(f"Source: {t['file']}", size=12, weight=ft.FontWeight.W_600,
                    color=ft.Colors.PRIMARY),
            ft.Container(
                content=ft.Text(src, font_family="Courier New", size=10, selectable=True),
                bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST,
                border_radius=6,
                padding=10,
            ),
        ]
        self.ctx.page.update()

    def _copy_to_project(self, t: dict) -> None:
        dest_dir = self.ctx.svc.project.root / "templates"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / t["file"]
        try:
            shutil.copy2(t["path"], dest)
            self._status.value = f"✓ Copied to templates/{t['file']}"
            self._status.color = ft.Colors.GREEN_700
            self._refresh_list()
        except Exception as exc:
            self._status.value = f"✗ {exc}"
            self._status.color = ft.Colors.ERROR
        self.ctx.page.update()

    def _use(self, t: dict) -> None:
        # Prefer the project copy if it exists, otherwise use built-in path
        project_copy = self.ctx.svc.project.root / "templates" / t["file"]
        path = f"templates/{t['file']}" if project_copy.exists() else str(t["path"])
        if self.on_use:
            self.on_use(path)

    def _create_blank(self, _) -> None:
        name = (self._new_name.value or "").strip()
        if not name:
            self._new_name.error_text = "Enter a name"
            self.ctx.page.update()
            return
        if not name.endswith(".html"):
            name += ".html"
        dest = self.ctx.svc.project.root / "templates" / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            self._status.value = f"✗ templates/{name} already exists"
            self._status.color = ft.Colors.ERROR
            self.ctx.page.update()
            return
        # Copy simple_table as the blank starter
        starter = BUILTIN_DIR / "simple_table.html"
        if starter.exists():
            shutil.copy2(starter, dest)
        else:
            dest.write_text("<!DOCTYPE html>\n<html>\n<body>\n<!-- your template -->\n</body>\n</html>\n",
                            encoding="utf-8")
        self._status.value = f"✓ Created templates/{name}"
        self._status.color = ft.Colors.GREEN_700
        self._new_name.value = ""
        self._refresh_list()
        self.ctx.page.update()

    # ---------------------------------------------------------------- render
    def render(self) -> ft.Control:
        toolbar = ft.Row([
            ft.Text("Template Gallery", size=16, weight=ft.FontWeight.W_600, expand=True),
            ft.OutlinedButton("Close", icon=ft.Icons.CLOSE,
                              on_click=lambda _: self.on_close()),
        ], spacing=8)

        new_row = ft.Row([
            ft.Icon(ft.Icons.ADD_CIRCLE_OUTLINE, color=ft.Colors.PRIMARY, size=18),
            ft.Text("Create blank template:", size=12),
            self._new_name,
            ft.FilledButton("Create", icon=ft.Icons.CREATE, on_click=self._create_blank),
            self._status,
        ], spacing=8)

        list_panel = ft.Container(
            content=self._list_col,
            expand=2,
            border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
            border_radius=8,
            padding=12,
        )

        preview_panel = ft.Container(
            content=ft.Column([
                ft.Text("Template source preview", size=12,
                        weight=ft.FontWeight.W_600,
                        color=ft.Colors.ON_SURFACE_VARIANT),
                ft.Container(
                    content=self._preview_content,
                    expand=True,
                    bgcolor=ft.Colors.SURFACE_CONTAINER_LOWEST,
                    border_radius=6,
                    padding=8,
                ),
            ], spacing=8, expand=True),
            expand=3,
            border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
            border_radius=8,
            padding=12,
        )

        return ft.Column([
            toolbar,
            new_row,
            ft.Row([list_panel, preview_panel], spacing=12, expand=True,
                   vertical_alignment=ft.CrossAxisAlignment.START),
        ], spacing=12, expand=True)
