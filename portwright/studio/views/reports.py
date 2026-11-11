"""Reports screen: list, add, run and manage HTML/PDF report definitions."""
from __future__ import annotations

import flet as ft

from ...reports.renderers import RENDERERS
from ...services import ServiceError
from .. import report_vm as rvm
from ..common import Ctx, checkbox_rows, dropdown, empty, header, icon_btn, mono, tag, text_field, help_btn
from .report_design import DesignDialog
from .template_editor import TemplateEditorView
from .template_gallery import TemplateGalleryView


def _card(title: str, body: list[ft.Control], actions: list[ft.Control] | None = None) -> ft.Control:
    return ft.Container(
        padding=20, border_radius=12, border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
        content=ft.Column([
            ft.Row([ft.Text(title, size=16, weight=ft.FontWeight.W_600, expand=True), *(actions or [])]),
            *body,
        ], spacing=14),
    )


def _small(text: str, color=ft.Colors.ON_SURFACE_VARIANT, **kw) -> ft.Text:
    return ft.Text(text, size=12, color=color, **kw)


class ReportsView:
    title = "Reports"

    def __init__(self, ctx: Ctx):
        self.ctx = ctx
        self.root = ft.Column(expand=True, scroll=ft.ScrollMode.AUTO, spacing=16)
        self._editor: _ReportEditor | None = None
        self._template_editor: TemplateEditorView | None = None
        self._gallery: TemplateGalleryView | None = None
        self._run_dialog: ft.AlertDialog | None = None

    def build(self) -> ft.Control:
        self.refresh(update=False)
        return self.root

    def refresh(self, update: bool = True) -> None:
        if self._template_editor is not None:
            self.root.scroll = None
            self.root.controls = [self._template_editor.render()]
        elif self._gallery is not None:
            self.root.scroll = None
            self.root.controls = [self._gallery.render()]
        elif self._editor is not None:
            self.root.scroll = ft.ScrollMode.AUTO
            self.root.controls = [self._editor.render()]
        else:
            self.root.scroll = ft.ScrollMode.AUTO
            self.root.controls = self._list()
        if update:
            self.ctx.page.update()

    # ---------------------------------------------------------------- list
    def _list(self) -> list[ft.Control]:
        items = self.ctx.svc.reports.list()
        controls: list[ft.Control] = [header(
            "Reports",
            "Generate PDF, HTML, Markdown, CSV, Excel or JSON reports",
            [
                help_btn(self.ctx, "reports"),
                ft.OutlinedButton("Templates", icon=ft.Icons.PHOTO_LIBRARY,
                                  on_click=lambda _: self._open_gallery(),
                                  tooltip="Browse and manage Jinja2 templates"),
                ft.OutlinedButton("Design with AI", icon=ft.Icons.AUTO_AWESOME,
                                  on_click=lambda _: self._design(None),
                                  tooltip="Have a model write a new report's template"),
                ft.FilledButton("Add report", icon=ft.Icons.ADD,
                                on_click=lambda _: self._open_editor(None)),
            ],
        )]
        if not items:
            controls.append(empty("No reports yet",
                                  "Add a report definition, then run it to produce PDF, HTML, CSV, Excel, JSON or Markdown."))
            return controls

        rows = []
        cfg = self.ctx.svc.project.config
        for r in items:
            ai = rvm.is_ai_report(self.ctx.svc, cfg.report(r["name"]))
            tpl_cell = ft.Row([_small(r["template"] or ""), tag("AI draft", "warn")], spacing=6) if ai                 else _small(r["template"] or "")
            extra = [icon_btn(ft.Icons.PUBLISH, "Promote: copy the AI template into templates/ (git)",
                              lambda _, n=r["name"]: self._promote(n), ft.Colors.AMBER_800)] if ai else []
            rows.append(ft.DataRow(cells=[
                ft.DataCell(ft.Text(r["name"], weight=ft.FontWeight.W_500)),
                ft.DataCell(_small(r["from"])),
                ft.DataCell(_small(", ".join(r["formats"]))),
                ft.DataCell(tpl_cell),
                ft.DataCell(_small(r["description"] or "")),
                ft.DataCell(ft.Row([
                    icon_btn(ft.Icons.PLAY_ARROW, "Run report",
                             lambda _, n=r["name"]: self._show_run_dialog(n),
                             ft.Colors.GREEN_700),
                    icon_btn(ft.Icons.AUTO_AWESOME, "Design the template with AI",
                             lambda _, n=r["name"]: self._design(n), ft.Colors.PURPLE_700),
                    *extra,
                    icon_btn(ft.Icons.CODE, "Edit template",
                             lambda _, n=r["name"], t=r["template"]: self._open_template_editor(n, t),
                             ft.Colors.TERTIARY),
                    icon_btn(ft.Icons.EDIT_OUTLINED, "Edit definition",
                             lambda _, n=r["name"]: self._open_editor(n),
                             ft.Colors.PRIMARY),
                    icon_btn(ft.Icons.DELETE_OUTLINE, "Remove",
                             lambda _, n=r["name"]: self._remove(n),
                             ft.Colors.ERROR),
                ], spacing=0)),
            ]))

        controls.append(ft.DataTable(
            columns=[
                ft.DataColumn(ft.Text("Name")),
                ft.DataColumn(ft.Text("Data from")),
                ft.DataColumn(ft.Text("Formats")),
                ft.DataColumn(ft.Text("Template")),
                ft.DataColumn(ft.Text("Description")),
                ft.DataColumn(ft.Text("Actions")),
            ],
            rows=rows,
            column_spacing=20,
        ))
        controls.append(ft.Container(height=8))
        controls.append(ft.Row([
            ft.Icon(ft.Icons.INFO_OUTLINE, size=14, color=ft.Colors.ON_SURFACE_VARIANT),
            ft.Text("Click the code (<>) button to edit the Jinja2 template with syntax reference and snippets.",
                    size=11, color=ft.Colors.ON_SURFACE_VARIANT),
        ], spacing=6))
        return controls

    # ---------------------------------------------------------------- AI
    def _design(self, name: str | None) -> None:
        DesignDialog(self.ctx, name, on_done=self.refresh).open()

    def _promote(self, name: str) -> None:
        from ...reports.author import ReportAuthor

        def go():
            try:
                dest = ReportAuthor(self.ctx.svc.project, actor="studio").promote(name)
            except ServiceError as exc:
                self.ctx.fail(exc)
                return
            self.ctx.toast(f"'{name}' now uses {dest.relative_to(self.ctx.svc.project.root).as_posix()} (in git)")
            self.refresh()
        self.ctx.confirm("Promote template", f"Copy the AI template of '{name}' into templates/ so it is kept in "
                         "git, and point the report at it?", go, danger=False, yes="Promote")

    # ---------------------------------------------------------------- template gallery
    def _open_gallery(self) -> None:
        self._gallery = TemplateGalleryView(
            self.ctx,
            on_close=self._close_gallery,
            on_use=self._use_template,
        )
        self.refresh()

    def _close_gallery(self) -> None:
        self._gallery = None
        self.refresh()

    def _use_template(self, template_path: str) -> None:
        """Called from gallery when user clicks Use — pre-fills template in Add report editor."""
        self._gallery = None
        self._editor = _ReportEditor(self.ctx, None, on_done=self._close_editor,
                                     prefill_template=template_path)
        self.refresh()

    # ---------------------------------------------------------------- template editor
    def _open_template_editor(self, report_name: str, template_path: str) -> None:
        self._template_editor = TemplateEditorView(
            self.ctx, report_name, template_path,
            on_close=self._close_template_editor,
        )
        self.refresh()

    def _close_template_editor(self) -> None:
        self._template_editor = None
        self.refresh()

    # ---------------------------------------------------------------- run dialog
    def _show_run_dialog(self, name: str) -> None:
        default_fmts = set(self.ctx.svc.reports.get(name).effective_formats())
        fmt_boxes = {f: ft.Checkbox(label=f.upper(), value=f in default_fmts) for f in RENDERERS}
        output = text_field("Output path (optional, overrides config)", hint="reports/output/my-report", expand=True)
        params_field = text_field("Params (KEY=VALUE, comma-separated)", hint="dept=sales,year=2025", expand=True)
        status = ft.Text("", color=ft.Colors.ON_SURFACE_VARIANT, size=12)

        def run(_):
            params = {}
            raw = params_field.value.strip()
            if raw:
                for part in raw.split(","):
                    if "=" in part:
                        k, v = part.split("=", 1)
                        params[k.strip()] = v.strip()
            try:
                fmts = [f for f, box in fmt_boxes.items() if box.value]
                if not fmts:
                    raise ValueError("pick at least one format")
                result = self.ctx.svc.reports.run(
                    name,
                    output=output.value.strip() or None,
                    formats=fmts,
                    params=params,
                )
                lines = [f"✓ {result.row_count} rows, {result.seconds}s"]
                lines += [f"{o.format}: {o.path}" for o in result.outputs]
                lines += [f"⚠ {w}" for w in result.warnings]
                status.value = "\n".join(lines)
                status.color = ft.Colors.GREEN_700
            except Exception as exc:
                status.value = f"✗ {exc}"
                status.color = ft.Colors.ERROR
            self.ctx.page.update()

        dlg = ft.AlertDialog(
            title=ft.Text(f"Run report: {name}"),
            content=ft.Column([output, params_field, _small("Formats"),
                               checkbox_rows(fmt_boxes.values()), status],
                              spacing=12, width=460),
            actions=[
                ft.FilledButton("Run", icon=ft.Icons.PLAY_ARROW, on_click=run),
                ft.TextButton("Close", on_click=lambda _: self._close_dialog()),
            ],
        )
        self._run_dialog = dlg
        self.ctx.page.show_dialog(dlg)

    def _close_dialog(self) -> None:
        self.ctx.page.pop_dialog()
        self._run_dialog = None

    # ---------------------------------------------------------------- editor
    def _open_editor(self, name: str | None) -> None:
        spec = None
        if name:
            try:
                spec = self.ctx.svc.reports.get(name)
            except ServiceError as exc:
                self.ctx.show_error(str(exc))
                return
        self._editor = _ReportEditor(self.ctx, spec, on_done=self._close_editor)
        self.refresh()

    def _close_editor(self) -> None:
        self._editor = None
        self.refresh()

    # ---------------------------------------------------------------- remove
    def _remove(self, name: str) -> None:
        def confirm(_):
            try:
                cfg = self.ctx.svc.project.config
                cfg.reports = [r for r in cfg.reports if r.name != name]
                self.ctx.svc.project.save(force=True)
                self.ctx.page.pop_dialog()
                self.refresh()
            except Exception as exc:
                self.ctx.show_error(str(exc))

        dlg = ft.AlertDialog(
            title=ft.Text(f"Remove report '{name}'?"),
            content=ft.Text("The template file is kept; only the definition is removed."),
            actions=[
                ft.FilledButton("Remove", on_click=confirm,
                                style=ft.ButtonStyle(bgcolor=ft.Colors.ERROR)),
                ft.TextButton("Cancel", on_click=lambda _: self.ctx.page.pop_dialog()),
            ],
        )
        self.ctx.page.show_dialog(dlg)


class _ReportEditor:
    def __init__(self, ctx: Ctx, spec, on_done, prefill_template: str | None = None):
        self.ctx = ctx
        self.spec = spec
        self.on_done = on_done
        self._is_new = spec is None

        # Load available endpoints and sources from project config
        cfg = ctx.svc.project.config
        # endpoints is list[EndpointSpec], sources is dict[str, SourceSpec]
        endpoint_names = [e.name for e in cfg.endpoints] if hasattr(cfg, "endpoints") else []
        source_names   = list(cfg.sources.keys())         if hasattr(cfg, "sources")   else []

        cur_endpoint = spec.from_.endpoint if spec and spec.from_.endpoint else None
        cur_source   = spec.from_.source   if spec and spec.from_.source   else None

        self.name        = text_field("Name *", spec.name if spec else "", expand=True)
        self.description = text_field("Description", spec.description if spec else "", expand=True)
        default_tpl = prefill_template or ((spec.template or "") if spec else "templates/report.html")
        self.template    = text_field("Template path (needed for PDF/HTML)", default_tpl, expand=True)
        cur_fmts = set(spec.effective_formats()) if spec else {"pdf"}
        self.formats = {f: ft.Checkbox(label=f.upper(), value=f in cur_fmts) for f in RENDERERS}
        self.query = text_field("SQL query (SQL sources only; bind values with :name)",
                                (spec.from_.query or "") if spec else "", expand=True, multiline=True)

        # Endpoint dropdown
        ep_options = [ft.DropdownOption(key="", text="(none)")] + [ft.DropdownOption(key=n, text=n) for n in endpoint_names]
        self.endpoint = ft.Dropdown(
            label="Endpoint",
            hint_text="Pick an endpoint as data source",
            value=cur_endpoint or "",
            options=ep_options,
            expand=True,
            on_select=lambda e: self._on_endpoint_change(e),
        )

        # Source dropdown
        src_options = [ft.DropdownOption(key="", text="(none)")] + [ft.DropdownOption(key=n, text=n) for n in source_names]
        self.source = ft.Dropdown(
            label="Source",
            hint_text="Pick a source as data source",
            value=cur_source or "",
            options=src_options,
            expand=True,
            on_select=lambda e: self._on_source_change(e),
        )

        # Helper labels showing type info
        self._ep_hint  = ft.Text("", size=11, color=ft.Colors.ON_SURFACE_VARIANT, italic=True)
        self._src_hint = ft.Text("", size=11, color=ft.Colors.ON_SURFACE_VARIANT, italic=True)
        self._update_hints(cfg, cur_endpoint, cur_source)

        self.output = text_field("Output path", spec.output or "" if spec else "", expand=True,
                                 hint="reports/output/{{ now | strftime('%Y-%m') }}/sales")
        self.error = ft.Text("", color=ft.Colors.ERROR, size=12)

    def _on_endpoint_change(self, e) -> None:
        val = e.data or ""
        if val:
            self.source.value = ""          # clear source when endpoint chosen
        self._update_hints(self.ctx.svc.project.config, val or None, None)
        self.ctx.page.update()

    def _on_source_change(self, e) -> None:
        val = e.data or ""
        if val:
            self.endpoint.value = ""        # clear endpoint when source chosen
        self._update_hints(self.ctx.svc.project.config, None, val or None)
        self.ctx.page.update()

    def _update_hints(self, cfg, ep_name, src_name) -> None:
        if ep_name:
            ep = next((e for e in cfg.endpoints if e.name == ep_name), None)
            if ep:
                self._ep_hint.value = f"  {ep.method} {ep.path}  (source: {ep.source})"
            else:
                self._ep_hint.value = ""
        else:
            self._ep_hint.value = ""

        if src_name:
            src = cfg.sources.get(src_name)
            if src:
                detail = getattr(src, "url", None) or getattr(src, "path", None) or ""
                self._src_hint.value = f"  type: {src.type}  {detail}"
            else:
                self._src_hint.value = ""
        else:
            self._src_hint.value = ""

    def render(self) -> ft.Control:
        title = "Add report" if self._is_new else f"Edit report: {self.spec.name}"
        return _card(title, [
            ft.Row([self.name, self.description], spacing=12),
            self.template,
            _small("Data feed — choose an endpoint OR a source (not both):"),
            ft.Column([
                ft.Row([self.endpoint, self.source], spacing=12),
                ft.Row([self._ep_hint, self._src_hint], spacing=24),
            ], spacing=4),
            self.output,
            self.query,
            _small("Output formats:"),
            checkbox_rows(self.formats.values(), 6),
            _small("Output path supports Jinja2 e.g. reports/{{ now | strftime('%Y-%m') }}/sales  "
                   "(each format adds its own suffix: .pdf, .xlsx, …)"),
            self.error,
        ], actions=[
            ft.FilledButton("Save", icon=ft.Icons.SAVE, on_click=self._save),
            ft.OutlinedButton("Cancel", on_click=lambda _: self.on_done()),
        ])

    def _save(self, _) -> None:
        from ...config import ReportFrom, ReportSpec
        name = self.name.value.strip()
        template = self.template.value.strip()
        endpoint = (self.endpoint.value or "").strip() or None
        source   = (self.source.value   or "").strip() or None

        if not name:
            self.error.value = "Name is required"
            self.ctx.page.update()
            return
        formats = [f for f, box in self.formats.items() if box.value]
        if not formats:
            self.error.value = "Pick at least one output format"
            self.ctx.page.update()
            return
        if not endpoint and not source:
            self.error.value = "Give an endpoint or a source"
            self.ctx.page.update()
            return
        if endpoint and source:
            self.error.value = "Give only one of endpoint or source"
            self.ctx.page.update()
            return

        try:
            old = self.spec
            query = (self.query.value or "").strip() or None
            frm = (old.from_.model_dump() if old else {})          # keep params, paginate, max_rows
            frm.update(endpoint=endpoint, source=source, query=query if source else None)
            new_spec = ReportSpec(
                name=name,
                description=self.description.value.strip(),
                template=template or None,
                **{"from": ReportFrom(**frm)},
                formats=formats,
                output=self.output.value.strip() or None,
                vars=dict(old.vars) if old else {},
                columns=list(old.columns) if old else [],
                rename=dict(old.rename) if old else {},
            )
            cfg = self.ctx.svc.project.config
            cfg.reports = [r for r in cfg.reports if r.name != (self.spec.name if self.spec else name)]
            cfg.reports.append(new_spec)
            self.ctx.svc.project.save(force=True)
            self.on_done()
        except Exception as exc:
            self.error.value = str(exc)
            self.ctx.page.update()


