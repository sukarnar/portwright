"""Design with AI: a dialog that has a model write a report's template (ReportAuthor.design).

It shows, before anything is sent, which data access level applies and lets you lower it; "What the model will
see" shows the exact data section of the prompt.
"""
from __future__ import annotations

import asyncio
import json

import flet as ft

from ...services import ServiceError
from .. import report_vm as rvm
from ..common import Ctx, checkbox_rows, dropdown, text_field


def _small(text: str, color=ft.Colors.ON_SURFACE_VARIANT) -> ft.Text:
    return ft.Text(text, size=12, color=color)


class DesignDialog:
    def __init__(self, ctx: Ctx, name: str | None = None, on_done=None):
        from ...agents.models import load_models
        from ...reports.author import builtin_templates
        self.ctx, self.on_done = ctx, on_done
        cfg = ctx.svc.project.config
        spec = cfg.report(name) if name else None
        try:
            self.models = sorted(load_models(cfg, ctx.svc.project.root))
        except Exception:                                       # noqa: BLE001 - shown as "no models"
            self.models = []
        self.f_name = text_field("Report name", name or "", disabled=bool(name), hint="a new or existing report")
        self.f_prompt = text_field("What should the report show?", "", multiline=True, min_lines=3,
                                   hint="e.g. Headcount by department: KPI cards on top, then a table")
        eps = {e.name: e.name for e in cfg.endpoints if e.method == "GET"}
        srcs = {n: n for n, s in cfg.sources.items() if s.type in ("sql", "file")}
        self.f_endpoint = dropdown("Data from endpoint", {"": "(none)", **eps}, "")
        self.f_source = dropdown("…or source", {"": "(none)", **srcs}, "")
        self.f_query = text_field("Query (SQL source)", "", multiline=True, min_lines=2,
                                  hint="one SELECT; bind values as :name")
        self.data_box = ft.Column([_small("Where its data comes from (new reports only)"), self.f_endpoint,
                                   self.f_source, self.f_query], spacing=8, visible=spec is None)
        cur = set(spec.effective_formats()) if spec else {"pdf", "html"}
        self.formats = {f: ft.Checkbox(label=f.upper(), value=f in cur) for f in rvm.FORMATS}
        self.f_model = dropdown("Model", {m: m for m in self.models}, self.models[0] if len(self.models) == 1 else None)
        self.f_base = dropdown("Start from", {"": "(nothing)", **{b: b for b in builtin_templates()}}, "")
        self.f_access = dropdown("What the model may see", {}, None)
        self.access_note = _small("")
        self.f_rows = text_field("Sample rows", "", width=140, hint=f"default {cfg.report_ai.sample_rows}")
        self.f_replace = ft.Checkbox(label="Replace an existing AI template (the old one goes to .history)",
                                     value=False)
        self.status = ft.Text("", size=12, selectable=True)
        self.busy = ft.ProgressRing(width=18, height=18, visible=False)
        for c in (self.f_model, self.f_endpoint, self.f_source):
            c.on_select = c.on_change = lambda _: self.refresh_access()
        self.f_name.on_blur = lambda _: self.name_changed()
        self.refresh_access()

    # ----------------------------------------------------------------- state
    def name_changed(self) -> None:
        exists = self.ctx.svc.project.config.report((self.f_name.value or "").strip()) is not None
        self.data_box.visible = not exists
        self.refresh_access()

    def refresh_access(self) -> None:
        name = (self.f_name.value or "").strip()
        report = name if self.ctx.svc.project.config.report(name) else None
        try:
            level, why = rvm.effective_access(self.ctx.svc, self.f_model.value or None, report,
                                              self.f_endpoint.value or None, self.f_source.value or None)
        except Exception as exc:                                # noqa: BLE001 - e.g. models file missing
            level, why = "full", f"could not check the limits: {exc}"
        self.level = level
        if level == "none":
            self.f_access.options, self.f_access.value, self.f_access.disabled = [], None, True
            self.access_note.value = f"AI authoring is not allowed for this data ({why})."
            self.access_note.color = ft.Colors.ERROR
        else:
            choices = rvm.access_choices(level)
            self.f_access.options = [ft.DropdownOption(key=k, text=v) for k, v in choices.items()]
            if self.f_access.value not in choices:
                self.f_access.value = level
            self.f_access.disabled = False
            self.access_note.value = f"Allowed: {why}. You can only lower it."
            self.access_note.color = ft.Colors.ON_SURFACE_VARIANT
        _touch(self.f_access, self.access_note)

    def values(self) -> dict:
        access = self.f_access.value if self.f_access.value != self.level else None
        return {"name": self.f_name.value, "prompt": self.f_prompt.value, "endpoint": self.f_endpoint.value,
                "source": self.f_source.value, "query": self.f_query.value,
                "formats": [f for f, b in self.formats.items() if b.value], "model": self.f_model.value,
                "base": self.f_base.value, "data_access": access, "sample_rows": self.f_rows.value,
                "replace": self.f_replace.value}

    def _author(self):
        from ...reports.author import ReportAuthor
        return ReportAuthor(self.ctx.svc.project, actor="studio")

    # ----------------------------------------------------------------- actions
    def preview(self, _=None) -> None:
        """Show exactly what the data section of the prompt would contain."""
        try:
            req = rvm.design_request({**self.values(), "prompt": self.f_prompt.value or "preview"},
                                     self.ctx.svc.project.config)
        except ServiceError as exc:
            self._say(str(exc), error=True)
            return

        def work():
            return self._author().sample(req["name"] if req["frm"] is None else None, frm=req["frm"],
                                         model=req["model"], sample_rows=req.get("sample_rows"),
                                         data_access=req["data_access"])

        def done(data):
            text = json.dumps(data, indent=1, default=str, ensure_ascii=False)
            self.ctx.info("What the model will see",
                          [_small("This is the data part of the prompt; the request and the template rules are "
                                  "added around it."),
                           ft.Text(text[:20000], selectable=True, font_family="Consolas", size=11)])
        self.ctx.background(work, done, self.busy)

    def design(self, _=None) -> None:
        try:
            req = rvm.design_request(self.values(), self.ctx.svc.project.config)
        except ServiceError as exc:
            self._say(str(exc), error=True)
            return
        name, prompt = req.pop("name"), req.pop("prompt")
        self._say("Designing… the model writes the template, then it is checked.")

        def work():
            return asyncio.run(self._author().design(name, prompt, **req))

        def done(d):
            sent = f"{d.rows_sent} row(s)" if d.rows_sent else "no row values"
            msg = (f"✓ Saved {d.template} (model {d.model}, {d.repairs} repair(s), {d.tokens} tokens). "
                   f"The model saw: data access '{d.data_access}', {sent}; columns: {', '.join(d.columns_sent)}.")
            self._say(msg + "".join(f"\n⚠ {w}" for w in d.warnings))
            self.ctx.toast(f"Report '{name}' designed")
            if self.on_done:
                self.on_done()
        self.ctx.background(work, done, self.busy)

    def _say(self, text: str, error: bool = False) -> None:
        self.status.value = text
        self.status.color = ft.Colors.ERROR if error else ft.Colors.GREEN_800
        _touch(self.status)

    # ----------------------------------------------------------------- layout
    def body(self) -> list[ft.Control]:
        no_models = [] if self.models else [_small("No models yet: add one on Agents > Models.", ft.Colors.AMBER_800)]
        return [self.f_name, self.f_prompt, self.data_box,
                _small("Formats the template must suit"), checkbox_rows(self.formats.values()),
                *no_models, self.f_model, self.f_base, self.f_access, self.access_note, self.f_rows, self.f_replace,
                _small("The template is checked (no scripts or remote files; it must render with your data and "
                       "with none) and saved to .portwright/templates/ - outside git until you promote it."),
                ft.Row([self.busy, self.status], spacing=8)]

    def open(self) -> None:
        self.ctx.dialog("Design a report with AI", self.body(), [
            ft.TextButton("What the model will see", icon=ft.Icons.VISIBILITY, on_click=self.preview),
            ft.TextButton("Close", on_click=lambda _: self.ctx.close()),
            ft.FilledButton("Design", icon=ft.Icons.AUTO_AWESOME, on_click=self.design)], width=620)


def _touch(*controls) -> None:
    for c in controls:
        try:
            c.update()
        except (RuntimeError, AssertionError):
            pass
