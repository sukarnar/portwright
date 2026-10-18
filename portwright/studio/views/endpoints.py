"""Endpoints screen: list, builder (SQL table CRUD wizard, custom query, file, REST, Python), Try it."""
from __future__ import annotations

import json
import re
import time

import flet as ft
import yaml

from ...services import ServiceError
from ..common import Ctx, cli_dialog, dropdown, empty, header, help_btn, icon_btn, mono, table, tag, text_field
from ..viewmodels import (HTTP_METHODS, METHOD_KIND, OP_METHOD, PARAM_IN, PARAM_TYPES, SQL_OPS, ParamRow, cell,
                          endpoint_cli, endpoint_draft, keys_covering, result_table, set_project_state,
                          sync_path_params)

_IDENT = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
KIND_LABEL = {"table": "SQL table operation", "query": "SQL custom query", "file": "File",
              "rest": "REST upstream", "callable": "Python function"}


def _card(title: str, body: list[ft.Control], actions: list[ft.Control] | None = None) -> ft.Control:
    return ft.Container(
        padding=20, border_radius=12, border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
        content=ft.Column([ft.Row([ft.Text(title, size=16, weight=ft.FontWeight.W_600, expand=True),
                                   *(actions or [])]), *body], spacing=14))


def _small(text: str, color=ft.Colors.ON_SURFACE_VARIANT, **kw) -> ft.Text:
    return ft.Text(text, size=12, color=color, **kw)


def _kind(ep: dict, source_type: str) -> str:
    if source_type == "sql":
        return "query" if ep.get("query") or ep.get("operation") in (None, "query") else "table"
    return {"file": "file", "rest": "rest", "callable": "callable"}.get(source_type, "query")


# =========================================================================== list
class EndpointsView:
    title = "Endpoints"

    def __init__(self, ctx: Ctx):
        self.ctx = ctx
        self.root = ft.Column(expand=True, scroll=ft.ScrollMode.AUTO, spacing=16)
        self.editor = None

    def build(self) -> ft.Control:
        self.refresh(update=False)
        return self.root

    def refresh(self, update: bool = True) -> None:
        self.root.controls = [self.editor.render()] if self.editor is not None else self._list()
        if update:
            self.ctx.page.update()

    def _list(self) -> list[ft.Control]:
        svc = self.ctx.svc
        eps = svc.endpoints.list()
        types = {s["name"]: s["type"] for s in svc.sources.list()}
        controls: list[ft.Control] = [header(
            "Endpoints", f"URLs under {svc.project.config.server.base_path} that return or change data. "
                         "Restart the server after changes.",
            [help_btn(self.ctx, "endpoints"),
             ft.FilledButton("New endpoint", icon=ft.Icons.ADD, on_click=lambda _: self.new_dialog())])]
        if not eps:
            controls.append(empty("No endpoints yet." + ("" if types else " Add a source first (Sources screen)."),
                                  ft.FilledButton("Create your first endpoint", on_click=lambda _: self.new_dialog())))
            return controls
        rows = []
        for e in eps:
            access = (tag("PUBLIC", "warn") if e["public"] else
                      ft.Row([tag(s, "info") for s in e["scopes"]] or [tag("any key", "neutral")], wrap=True, spacing=4))
            rows.append([
                tag(e["method"], METHOD_KIND.get(e["method"], "neutral")), mono(e["path"]),
                ft.Text(e["name"], weight=ft.FontWeight.W_600),
                ft.Text(f"{e['source']} ({types.get(e['source'], '?')})"), access,
                ft.Row([icon_btn(ft.Icons.PLAY_ARROW, "Try it", lambda _, n=e["name"]: self.try_dialog(n)),
                        icon_btn(ft.Icons.EDIT, "Edit", lambda _, n=e["name"]: self.open_editor(n)),
                        icon_btn(ft.Icons.CONTENT_COPY, "Duplicate", lambda _, n=e["name"]: self.duplicate(n)),
                        icon_btn(ft.Icons.TERMINAL, "Copy as PowerShell / curl / CLI",
                                 lambda _, n=e["name"]: self.snippets(n)),
                        icon_btn(ft.Icons.DELETE, "Delete", lambda _, n=e["name"]: self.delete(n), ft.Colors.ERROR)],
                       spacing=0)])
        controls.append(table(["Method", "Path", "Name", "Source", "Access", ""], rows))
        return controls

    # ------------------------------------------------------------- create
    def new_dialog(self) -> None:
        sources = self.ctx.svc.sources.list()
        if not sources:
            self.ctx.toast("Add a source first (Sources screen).", error=True)
            self.ctx.go("sources")
            return
        f_src = dropdown("Source", {s["name"]: f"{s['name']}  ({s['type']})" for s in sources}, sources[0]["name"])
        f_kind = ft.RadioGroup(value="table", content=ft.Column([
            ft.Radio(value="table", label="Table – list / get / create / update / delete (CRUD wizard)"),
            ft.Radio(value="query", label="Custom SQL query with parameters"),
            ft.Radio(value="single", label="One table operation (advanced)")]))
        sql_box = ft.Column([ft.Text("What should the endpoint do?", weight=ft.FontWeight.W_600), f_kind])
        types = {s["name"]: s["type"] for s in sources}

        def changed(_=None):
            sql_box.visible = types.get(f_src.value) == "sql"
            self.ctx.page.update()
        f_src.on_select = changed

        def go(_):
            self.ctx.close()
            src = f_src.value
            if types[src] == "sql" and f_kind.value == "table":
                self.editor = CrudWizard(self, src)
                self.refresh()
            else:
                kind = (f_kind.value if f_kind.value != "single" else "table") if types[src] == "sql" else types[src]
                self.open_editor(None, source=src, kind=kind)

        self.ctx.dialog("New endpoint", [f_src, sql_box,
                                         _small("File, REST and Python sources go straight to the editor.")],
                        [ft.TextButton("Cancel", on_click=lambda _: self.ctx.close()),
                         ft.FilledButton("Continue", on_click=go)], width=520, scroll=False)
        changed()

    def open_editor(self, name: str | None, source: str | None = None, kind: str | None = None) -> None:
        try:
            self.editor = EndpointEditor(self, name, source, kind)
        except ServiceError as exc:
            self.ctx.fail(exc)
            return
        self.refresh()
        self.editor.after_open()

    def close_editor(self) -> None:
        self.editor = None
        self.refresh()

    # ------------------------------------------------------------ actions
    def try_dialog(self, name: str) -> None:
        panel = TryPanel(self.ctx, lambda: (self.ctx.svc.endpoints.as_dict(name), name))
        self.ctx.info(f"Try it – {name}", [panel.control()], width=900)

    def snippets(self, name: str, values: dict | None = None) -> None:
        rt = self.ctx.svc.runtime
        try:
            sn = rt.snippets(name, values or {})
            add = endpoint_cli(self.ctx.svc.endpoints.as_dict(name))
        except ServiceError as exc:
            self.ctx.fail(exc)
            return
        cli_dialog(self.ctx, f"Call '{name}'", [
            ("PowerShell (server running; replace <your-key>)", sn["powershell"]),
            ("curl.exe", sn["curl"]),
            ("portwright call (no server, no key)", sn["portwright"]),
            ("Create this endpoint with the CLI", add)])

    def duplicate(self, name: str) -> None:
        names = {e["name"] for e in self.ctx.svc.endpoints.list()}
        new = f"{name}-copy"
        i = 2
        while new in names:
            new, i = f"{name}-copy{i}", i + 1
        try:
            ep = self.ctx.svc.endpoints.duplicate(name, new)
        except ServiceError as exc:
            self.ctx.fail(exc)
            return
        self.ctx.toast(f"Duplicated as '{ep.name}' ({ep.method} {ep.path}) – edit it to change the path")
        self.open_editor(ep.name)

    def delete(self, name: str) -> None:
        def go():
            self.ctx.svc.endpoints.remove(name)
            self.ctx.toast(f"Endpoint '{name}' deleted. Restart the server to apply.")
            self.refresh()
        self.ctx.confirm("Delete endpoint?", f"'{name}' will be removed from the project.", go)

    # ---------------------------------------------------------- next steps
    def next_steps(self, eps: list, scopes: list[str]) -> None:
        """After creating endpoints: offer an API key, Try it, or a load."""
        svc = self.ctx.svc
        names = [e.name for e in eps]
        protected = [e for e in eps if not e.public]
        need = sorted({s for e in protected for s in e.scopes})
        uncovered = [s for s in need if not keys_covering(svc, [s])]
        body: list[ft.Control] = [ft.Text(f"Saved: {', '.join(names)}. Restart the server to publish "
                                          f"{'them' if len(names) > 1 else 'it'}.")]
        actions: list[ft.Control] = []
        if uncovered:
            body.append(ft.Row([ft.Icon(ft.Icons.KEY, color=ft.Colors.PRIMARY),
                                ft.Text(f"No application key has scope {', '.join(uncovered)} yet (only admin '*' keys "
                                        f"can call {'them' if len(names) > 1 else 'it'}).", expand=True)]))

            def mk_key(_):
                self.ctx.close()
                self.ctx.go("access")
                view = self.ctx.view("access")
                if view is not None:
                    view.key_dialog(preset=uncovered)
            actions.append(ft.FilledButton("Create API key", icon=ft.Icons.KEY, on_click=mk_key))
        gets = [e for e in eps if e.method == "GET" and not any(p.location == "path" for p in e.params)]

        def try_(_):
            self.ctx.close()
            self.try_dialog(names[0] if len(eps) == 1 else (gets[0].name if gets else names[0]))
        actions.append(ft.OutlinedButton("Try it", icon=ft.Icons.PLAY_ARROW, on_click=try_))
        if gets and svc.loads.options()["targets"]:
            body.append(_small(f"'{gets[0].name}' can also feed a load (copy its rows into a table)."))

            def mk_load(_):
                self.ctx.close()
                self.ctx.go("loads")
                view = self.ctx.view("loads")
                if view is not None:
                    view.open_editor(None, from_endpoint=gets[0].name)
            actions.append(ft.OutlinedButton("Create a load", icon=ft.Icons.MOVE_DOWN, on_click=mk_load))
        actions.append(ft.TextButton("Done", on_click=lambda _: self.ctx.close()))
        self.ctx.dialog("Next steps", body, actions, width=600, scroll=False)


# ======================================================================= Try it
class TryPanel:
    """Parameter inputs + Send, running the (saved or draft) endpoint in-process."""

    def __init__(self, ctx: Ctx, get_endpoint):
        self.ctx = ctx
        self.get_endpoint = get_endpoint          # -> (endpoint dict, original name or None)
        self.inputs: dict[str, ft.TextField] = {}
        self.fields = ft.Row(wrap=True, spacing=12, run_spacing=12)
        self.f_limit = text_field("limit", "20", width=100)
        self.f_body = text_field("Extra JSON body (optional)", "", multiline=True, min_lines=2, width=420,
                                 hint='e.g. {"note": "x"}')
        self.result = ft.Column(spacing=10)
        self.busy = ft.ProgressRing(width=20, height=20, visible=False)
        self.box = ft.Column(spacing=12)
        self.render_inputs()

    def control(self) -> ft.Control:
        return self.box

    def render_inputs(self) -> None:
        try:
            ep, _ = self.get_endpoint()
        except ServiceError as exc:
            self.box.controls = [ft.Text(str(exc), color=ft.Colors.ERROR)]
            return
        old, self.inputs = self.inputs, {}
        for p in ep.get("params", []):        # reuse existing fields: keeps typed values and focus
            label = p["name"] + (" *" if p.get("required") else "") + f"  ({p.get('in', 'query')})"
            hint = p.get("type", "string") + (f" · one of {p['enum']}" if p.get("enum") else "")
            f = old.get(p["name"]) or text_field(label, "", hint=hint, width=220)
            f.label, f.helper = label, hint
            self.inputs[p["name"]] = f
        self.fields.controls = list(self.inputs.values()) or [_small("This endpoint has no parameters.")]
        is_list = ep.get("method", "GET") == "GET" and (ep.get("operation") in (None, "list", "query")) \
            and not any(p.get("in") == "path" for p in ep.get("params", []))
        self.f_limit.visible = is_list
        self.f_body.visible = ep.get("method", "GET") != "GET"
        self.box.controls = [
            _small("Runs the endpoint here, without a server, through the full validation and security pipeline "
                   "(as admin). Nothing is saved; writes DO change the database."),
            self.fields, ft.Row([self.f_limit, self.f_body], wrap=True, spacing=12),
            ft.Row([ft.FilledButton("Send", icon=ft.Icons.SEND, on_click=lambda _: self.send()), self.busy,
                    ft.TextButton("Copy as PowerShell / curl", icon=ft.Icons.TERMINAL, on_click=self.copy_snippets)]),
            self.result]

    def values(self) -> dict[str, str]:
        return {k: (c.value or "").strip() for k, c in self.inputs.items() if (c.value or "").strip()}

    def send(self) -> None:
        try:
            ep, original = self.get_endpoint()
            body = json.loads(self.f_body.value) if self.f_body.visible and (self.f_body.value or "").strip() else None
            if body is not None and not isinstance(body, dict):
                raise ServiceError("the JSON body must be an object { ... }")
            limit = int(self.f_limit.value) if self.f_limit.visible and (self.f_limit.value or "").strip() else None
        except ValueError as exc:
            self.ctx.toast(f"Check the inputs: {exc}", error=True)
            return
        except ServiceError as exc:
            self.ctx.fail(exc)
            return
        values = self.values()

        def work():
            t0 = time.perf_counter()
            res = self.ctx.svc.runtime.try_endpoint(ep, values, body=body, limit=limit, original=original)
            return res, round((time.perf_counter() - t0) * 1000)

        def done(out):
            res, ms = out
            if res.ok:
                set_project_state(self.ctx.svc, tried=True)
            items: list[ft.Control] = [ft.Row([tag(f"HTTP {res.status}", "good" if res.ok else "bad"),
                                               _small(f"{ms} ms")])]
            tbl = result_table(res.data)
            if res.ok and tbl and tbl[1]:
                cols, rows = tbl
                total = res.data.get("total") if isinstance(res.data, dict) else None
                items.append(_small(f"{len(rows)} row(s)" + (f" of {total}" if total is not None else "")))
                items.append(ft.Row([table(cols, [[ft.Text(cell(r.get(c), 30)) for c in cols] for r in rows])],
                                    scroll=ft.ScrollMode.AUTO))
            else:
                items.append(ft.Container(mono(json.dumps(res.data, indent=2, default=str)[:6000], 12), padding=12,
                                          border_radius=8, bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST))
            self.result.controls = items
        self.ctx.background(work, done, self.busy)

    def copy_snippets(self, _=None) -> None:
        try:
            ep, _ = self.get_endpoint()
            sn = self.ctx.svc.runtime.snippets(ep, self.values())
        except ServiceError as exc:
            self.ctx.fail(exc)
            return
        cli_dialog(self.ctx, f"Call '{ep['name']}'", [
            ("PowerShell (server running; replace <your-key>)", sn["powershell"]), ("curl.exe", sn["curl"]),
            ("portwright call (no server, no key; endpoint must be saved)", sn["portwright"])])


# ======================================================================= editor
class EndpointEditor:
    """One endpoint: basics + kind-specific settings, path & parameters, access, review & Try it."""

    def __init__(self, view: EndpointsView, name: str | None, source: str | None = None, kind: str | None = None):
        self.view = view
        self.ctx = view.ctx
        self.svc = view.ctx.svc
        self.original = name
        self.sources = {s["name"]: s for s in self.svc.sources.list()}
        if name:
            d = self.svc.endpoints.as_dict(name)
            self.kind = _kind(d, self.sources.get(d["source"], {}).get("type", "sql"))
        else:
            src = source or next(iter(self.sources))
            stype = self.sources[src]["type"]
            self.kind = kind or _kind({}, stype)
            d = {"name": "", "source": src, "method": "GET", "path": "/", "params": []}
            if self.kind == "table":
                d.update(operation="list")
            if self.kind == "callable":
                mod = self.svc.sources.get(src).module
                d["target"] = (mod + ":") if mod else ""
        self.rows = [ParamRow.from_spec(p) for p in d.get("params", [])]
        self.file_columns: list[str] = []
        self.scopes: list[str] = list(d.get("scopes", []))

        tf = text_field
        self.f_name = tf("Endpoint name *", d.get("name", ""), hint="e.g. transfer-list", width=260)
        self.f_desc = tf("Description", d.get("description", ""), expand=True)
        self.f_source = dropdown("Source", {n: f"{n}  ({s['type']})" for n, s in self.sources.items()},
                                 d["source"], width=260, on_select=self._source_changed)
        self.f_method = dropdown("Method", HTTP_METHODS, d.get("method", "GET"), width=130,
                                 on_select=self._changed)
        self.f_path = tf("Path *", d.get("path", "/"), hint="e.g. /transfers/{emp_no}", width=360,
                         on_change=self._path_typed, on_blur=self._path_done, on_submit=self._path_done)
        self.url = _small("", font_family="Consolas")
        # sql query
        self.f_sql = ft.TextField(label="SQL (SELECT … WHERE col = :param)", value=d.get("query", ""), multiline=True,
                                  min_lines=6, max_lines=20, text_style=ft.TextStyle(font_family="Consolas", size=13),
                                  on_change=self._changed, expand=True)
        # sql table operation
        self.f_op = dropdown("Operation", SQL_OPS, d.get("operation") or "list", width=300, on_select=self._op_changed)
        self.f_table = tf("Table *", d.get("table", ""), hint="e.g. hr.employees", width=260, on_change=self._changed)
        self.f_key = tf("Key column", d.get("key", ""), hint="needed for get / update / delete", width=200,
                        on_change=self._changed)
        self.f_cols = tf("Columns returned / writable (comma separated, empty = all)", ", ".join(d.get("columns") or []),
                         expand=True, on_change=self._changed)
        # file
        self.file_filters: set[str] = set(d.get("filters") or [])
        self.file_key = d.get("key") if self.kind == "file" else None
        self.file_cols: set[str] | None = set(d["columns"]) if self.kind == "file" and d.get("columns") else None
        self.file_box = ft.Column(spacing=8)
        # rest / callable
        self.f_up_path = tf("Upstream path", d.get("upstream_path", ""), hint="e.g. /v2/accounts/{id} (empty = same path)",
                            width=360, on_change=self._changed)
        self.f_up_method = dropdown("Upstream method", ["(same)"] + HTTP_METHODS, d.get("upstream_method") or "(same)",
                                    width=170, on_select=self._changed)
        self.f_target = tf("Function *", d.get("target", ""), hint="module:function, e.g. handlers.pricing:quote",
                           width=380, on_change=self._changed)
        # access
        self.f_public = ft.Switch(label="Public – no API key needed", value=bool(d.get("public")),
                                  on_change=self._access_changed)
        self.f_new_scope = tf("Add a scope", "", hint="e.g. transfers:read", width=220, on_submit=self._add_scope)
        self.f_rate = tf("Rate limit (requests per window)", str(d.get("rate_limit") or ""), width=290,
                         hint="empty = project default", on_change=self._changed)
        self.f_max = tf("Max page size", str(d.get("max_limit", 500)), width=150, on_change=self._changed)
        self.scope_box = ft.Row(wrap=True, spacing=6)
        self.grid = ft.Column(spacing=0)
        self.results = ft.Column(spacing=8)
        self.yaml_text = mono("", 12)
        self.busy = ft.ProgressBar(visible=False)
        self.dirty_tag = ft.Container(visible=False, content=tag("unsaved changes", "warn"))
        self.try_panel = TryPanel(self.ctx, self._try_source)
        self.saved = self._state() if name else None
        self._update_url()

    # ------------------------------------------------------------- state
    def base(self) -> dict:
        d = {"name": (self.f_name.value or "").strip(), "description": self.f_desc.value or "",
             "source": self.f_source.value, "method": self.f_method.value, "path": (self.f_path.value or "").strip()}
        if self.original:
            d.update({k: v for k, v in self.svc.endpoints.as_dict(self.original).items()
                      if k in ("tags",)})
        if self.kind == "query":
            d.update(query=(self.f_sql.value or "").strip(), operation="query")
        elif self.kind == "table":
            d.update(operation=self.f_op.value, table=(self.f_table.value or "").strip(),
                     key=(self.f_key.value or "").strip() or None,
                     columns=[c.strip() for c in (self.f_cols.value or "").split(",") if c.strip()] or None)
            d["method"] = OP_METHOD[self.f_op.value]
        elif self.kind == "file":
            d["method"] = "GET"
            d["filters"] = sorted(self.file_filters)
            d["key"] = self.file_key or None
            if self.file_cols is not None and self.file_columns and self.file_cols != set(self.file_columns):
                d["columns"] = [c for c in self.file_columns if c in self.file_cols]
        elif self.kind == "rest":
            d.update(upstream_path=(self.f_up_path.value or "").strip() or None,
                     upstream_method=None if self.f_up_method.value == "(same)" else self.f_up_method.value)
        elif self.kind == "callable":
            d["target"] = (self.f_target.value or "").strip()
        return d

    def draft(self) -> dict:
        d = endpoint_draft(self.base(), self.rows, public=self.f_public.value, scopes=self.scopes,
                           rate_limit=self.f_rate.value, max_limit=self.f_max.value)
        if self.kind == "file":           # filters are optional query params
            have = {p["name"] for p in d["params"]}
            d["params"] += [{"name": f, "type": "string", "in": "query"} for f in d.get("filters", []) if f not in have]
        return d

    def _state(self) -> str | None:
        try:
            return json.dumps(self.draft(), sort_keys=True, default=str)
        except ServiceError:
            return None

    @property
    def is_dirty(self) -> bool:
        return self.saved is None or self._state() != self.saved

    def _try_source(self):
        d = self.draft()
        if not d["name"]:
            d["name"] = "draft-endpoint"
        return d, self.original

    # ---------------------------------------------------------- handlers
    def _changed(self, _=None) -> None:
        self.dirty_tag.visible = self.is_dirty
        self._update_url()
        self.ctx.page.update()

    def _update_url(self) -> None:
        cfg = self.svc.project.config.server
        method = {"table": OP_METHOD.get(self.f_op.value, "GET"), "file": "GET"}.get(self.kind, self.f_method.value)
        path = "/" + (self.f_path.value or "").strip().lstrip("/")
        self.url.value = f"{method}  http://<server>:{cfg.port}{cfg.base_path}{path}"

    def _path_typed(self, _=None) -> None:
        self._update_url()
        self.ctx.page.update()

    def _path_done(self, _=None) -> None:
        sync_path_params(self.f_path.value or "", self.rows)
        self.render_grid()
        self.try_panel.render_inputs()
        self._changed()

    def _source_changed(self, _=None) -> None:
        stype = self.sources[self.f_source.value]["type"]
        new_kind = _kind({"query": "x"} if self.kind == "query" else {"operation": "list"}, stype)
        if new_kind != self.kind:
            self.kind = new_kind
        self.file_columns = []
        self.view.refresh()
        self.after_open()

    def _op_changed(self, _=None) -> None:
        op = self.f_op.value
        if op in ("get", "update", "delete") and (self.f_key.value or "").strip():
            k = self.f_key.value.strip()
            if "{" + k + "}" not in (self.f_path.value or ""):
                self.f_path.value = (self.f_path.value or "").rstrip("/") + "/{" + k + "}"
                sync_path_params(self.f_path.value, self.rows)
        self.view.refresh()

    def _access_changed(self, _=None) -> None:
        self._render_scopes()
        self._changed()

    def _add_scope(self, _=None) -> None:
        s = (self.f_new_scope.value or "").strip()
        if s and s not in self.scopes:
            self.scopes.append(s)
        self.f_new_scope.value = ""
        self._render_scopes()
        self._changed()

    def _toggle_scope(self, s: str) -> None:
        if s in self.scopes:
            self.scopes.remove(s)
        else:
            self.scopes.append(s)
        self._render_scopes()
        self._changed()

    def after_open(self) -> None:
        if self.kind == "file":
            self.load_file_columns()

    # ------------------------------------------------------------ render
    def render(self) -> ft.Control:
        k = self.kind
        stype = self.sources.get(self.f_source.value, {}).get("type", "?")
        basics = _card("1 · What it does", [
            ft.Row([self.f_name, self.f_desc], vertical_alignment=ft.CrossAxisAlignment.START),
            ft.Row([self.f_source, tag(KIND_LABEL[k], "info")], spacing=16,
                   vertical_alignment=ft.CrossAxisAlignment.CENTER),
            *self._kind_controls(stype),
        ])
        method_ctl = self.f_method if k in ("query", "rest", "callable") else \
            tag(OP_METHOD.get(self.f_op.value, "GET") if k == "table" else "GET", "info")
        path = _card("2 · Path and parameters", [
            ft.Row([method_ctl, self.f_path], spacing=16, vertical_alignment=ft.CrossAxisAlignment.START),
            self.url,
            _small("Parameters: query = ?name=value · path = {name} in the path (always required) · body = JSON field. "
                   "Callers cannot send parameters that are not listed here."),
            self.grid,
        ], [ft.OutlinedButton("Add parameter", icon=ft.Icons.ADD, on_click=self._add_param)])
        self._render_scopes()
        access = _card("3 · Who can call it", [
            self.f_public, self.scope_box, ft.Row([self.f_rate, self.f_max], wrap=True, spacing=16,
                                                  vertical_alignment=ft.CrossAxisAlignment.START)])
        review = _card("4 · Review and test", [
            self.results, ft.Text("Try it", weight=ft.FontWeight.W_600), self.try_panel.control(),
            ft.ExpansionTile(title=ft.Text(f"Definition saved to {self.ctx.svc.project.path.name}", size=14),
                             controls=[ft.Container(self.yaml_text, padding=12)], on_change=self._show_yaml),
        ], [ft.OutlinedButton("Validate", icon=ft.Icons.FACT_CHECK, on_click=self.validate)])
        top = ft.Row([
            ft.IconButton(ft.Icons.ARROW_BACK, tooltip="Back to endpoints", on_click=self.back),
            ft.Text(f"Edit endpoint '{self.original}'" if self.original else "New endpoint", size=24,
                    weight=ft.FontWeight.W_600),
            self.dirty_tag, ft.Container(expand=True), help_btn(self.ctx, "endpoints"),
            ft.OutlinedButton("CLI", icon=ft.Icons.TERMINAL, on_click=self.show_cli,
                              tooltip="The same endpoint as a 'portwright endpoint add' command"),
            ft.FilledButton("Save", icon=ft.Icons.SAVE, on_click=lambda _: self.save()),
        ])
        self.render_grid(update=False)
        self.dirty_tag.visible = self.is_dirty
        return ft.Column([top, self.busy, basics, path, access, review], spacing=16)

    def _kind_controls(self, stype: str) -> list[ft.Control]:
        k = self.kind
        if k == "query":
            ro = self.svc.sources.get(self.f_source.value).read_only
            return [ft.Row([self.f_sql]), ft.Row([
                ft.OutlinedButton("Detect parameters", icon=ft.Icons.AUTO_FIX_HIGH, on_click=self.detect_params,
                                  tooltip="Add a parameter for every :name in the SQL"),
                ft.OutlinedButton("Test query", icon=ft.Icons.PLAY_ARROW, on_click=lambda _: self.try_panel.send()),
                _small("Source is read-only: SELECT / WITH only." if ro else
                       "Source allows writes: INSERT/UPDATE/DELETE statements return the affected row count.")],
                wrap=True)]
        if k == "table":
            ro = self.svc.sources.get(self.f_source.value).read_only
            return [ft.Row([self.f_op, self.f_table, self.f_key], wrap=True, spacing=16,
                           vertical_alignment=ft.CrossAxisAlignment.START), self.f_cols,
                    *([_small("The source is read-only: create / update / delete will be refused. "
                              "Edit the source and allow writes.", color=ft.Colors.ERROR)]
                      if ro and self.f_op.value in ("create", "update", "delete") else []),
                    _small("Tip: for a full set of endpoints over one table use New endpoint > Table (CRUD wizard).")]
        if k == "file":
            return [self.file_box]
        if k == "rest":
            return [ft.Row([self.f_up_path, self.f_up_method], wrap=True, spacing=16,
                           vertical_alignment=ft.CrossAxisAlignment.START),
                    _small("Only the parameters listed below are forwarded. The source's credential is added by "
                           "portwright; the caller's key is never passed on.")]
        return [self.f_target, _small("The function receives the parameters below as keyword arguments. "
                                      "Only modules of this source can be called.")]

    # -------------------------------------------------------------- file
    def load_file_columns(self) -> None:
        src = self.f_source.value

        def done(r):
            if not r.ok:
                self.file_box.controls = [ft.Text(f"Cannot read the file: {r.message}", color=ft.Colors.ERROR)]
                return
            self.file_columns = r.columns
            if self.file_cols is None:
                self.file_cols = set(r.columns)
            self.render_file_box()
        self.file_box.controls = [_small("Reading the file…")]
        self.ctx.background(lambda: self.svc.sources.test(src, preview_rows=3), done, self.busy)

    def render_file_box(self) -> None:
        cols = self.file_columns
        lines = [ft.Row([ft.Container(_small(h, weight=ft.FontWeight.W_600), width=w) for h, w in
                         (("Field", 220), ("Returned", 90), ("Filter", 80), ("Key", 60))])]
        for c in cols:
            ok_name = bool(_IDENT.match(c))
            lines.append(ft.Row([
                ft.Container(ft.Text(c), width=220),
                ft.Container(ft.Checkbox(value=c in (self.file_cols or set()),
                                         on_change=lambda e, c=c: self._file_set("ret", c, e.control.value)), width=90),
                ft.Container(ft.Checkbox(value=c in self.file_filters, disabled=not ok_name,
                                         tooltip=None if ok_name else "only names without spaces can be filters",
                                         on_change=lambda e, c=c: self._file_set("flt", c, e.control.value)), width=80),
                ft.Container(ft.Checkbox(value=self.file_key == c, disabled=not ok_name,
                                         on_change=lambda e, c=c: self._file_set("key", c, e.control.value)), width=60),
            ]))
        self.file_box.controls = [
            _small("Filters become optional ?name= parameters (exact match). A key field makes this a single-row "
                   "endpoint: the path gets /{key}. Names with spaces can be returned but not filtered."),
            ft.Container(ft.Column(lines, spacing=0), border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
                         border_radius=10, padding=10)]

    def _file_set(self, what: str, col: str, on: bool) -> None:
        if what == "ret":
            (self.file_cols.add if on else self.file_cols.discard)(col)
        elif what == "flt":
            (self.file_filters.add if on else self.file_filters.discard)(col)
            if on and not any(r.name == col for r in self.rows):
                self.rows.append(ParamRow(name=col))
            if not on:
                self.rows = [r for r in self.rows if not (r.name == col and r.location == "query")]
        else:
            old = self.file_key
            self.file_key = col if on else None
            path = self.f_path.value or "/"
            if old:
                path = path.replace("/{" + old + "}", "")
            if self.file_key:
                path = path.rstrip("/") + "/{" + self.file_key + "}"
            self.f_path.value = path or "/"
            sync_path_params(self.f_path.value, self.rows)
        self.render_file_box()
        self.render_grid()
        self.try_panel.render_inputs()
        self._changed()

    # -------------------------------------------------------------- grid
    def render_grid(self, update: bool = True) -> None:
        widths = [140, 115, 150, 44, 100, 60, 60, 70, 110, 100, 36]
        heads = ["Name", "Type", "Where", "Req.", "Default", "Min", "Max", "Max len", "Allowed values", "Pattern", ""]

        def line(ctls, head=False):
            return ft.Container(
                padding=ft.Padding.symmetric(horizontal=10, vertical=4),
                bgcolor=ft.Colors.SURFACE_CONTAINER_LOW if head else None,
                border=ft.Border.only(bottom=ft.BorderSide(1, ft.Colors.OUTLINE_VARIANT)),
                content=ft.Row([ft.Container(c, width=w) for c, w in zip(ctls, widths)], spacing=6,
                               vertical_alignment=ft.CrossAxisAlignment.CENTER))
        lines = [line([_small(h, weight=ft.FontWeight.W_600) for h in heads], True)]
        for i, r in enumerate(self.rows):
            def setter(attr, r=r, rerender=False):
                def _f(e):
                    setattr(r, attr, e.control.value)
                    if rerender:
                        self.render_grid()
                        self.try_panel.render_inputs()
                    self._changed()
                return _f
            path = r.location == "path"
            lines.append(line([
                text_field(None, r.name, on_change=setter("name"), on_blur=lambda e: self.try_panel.render_inputs(),
                           disabled=path),
                dropdown(None, PARAM_TYPES, r.type, on_select=setter("type")),
                dropdown(None, PARAM_IN, r.location, on_select=setter("location", rerender=True), disabled=path),
                ft.Checkbox(value=r.required or path, disabled=path, on_change=setter("required", rerender=True)),
                text_field(None, r.default, on_change=setter("default")),
                text_field(None, r.min, on_change=setter("min")),
                text_field(None, r.max, on_change=setter("max")),
                text_field(None, r.max_length, on_change=setter("max_length")),
                text_field(None, r.enum, on_change=setter("enum")),
                text_field(None, r.pattern, on_change=setter("pattern")),
                icon_btn(ft.Icons.DELETE, "Remove" if not path else "Remove {name} from the path first",
                         lambda _, i=i: self._remove_param(i), ft.Colors.ERROR) if not path else ft.Text(""),
            ]))
        if not self.rows:
            lines.append(ft.Container(_small("No parameters." + (" Use Detect parameters for :binds in the SQL."
                                                                if self.kind == "query" else "")), padding=12))
        self.grid.controls = [ft.Container(border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT), border_radius=10,
                                           content=ft.Row([ft.Column(lines, spacing=0)], scroll=ft.ScrollMode.AUTO))]
        if update:
            self.ctx.page.update()

    def _add_param(self, _=None) -> None:
        self.rows.append(ParamRow(name=f"param{len(self.rows) + 1}"))
        self.render_grid()
        self.try_panel.render_inputs()
        self._changed()

    def _remove_param(self, i: int) -> None:
        del self.rows[i]
        self.render_grid()
        self.try_panel.render_inputs()
        self._changed()

    def detect_params(self, _=None) -> None:
        binds = self.svc.endpoints.binds(self.f_sql.value or "")
        have = {r.name for r in self.rows}
        new = [b for b in binds if b not in have]
        sync_path_params(self.f_path.value or "", self.rows)
        for b in new:
            self.rows.append(ParamRow(name=b, required=True))
        unused = [r.name for r in self.rows if r.name not in binds and r.location != "path"]
        self.ctx.toast((f"Added {', '.join(new)}" if new else "No new parameters")
                       + (f". Not used in the SQL: {', '.join(unused)}" if unused else ""))
        self.render_grid()
        self.try_panel.render_inputs()
        self._changed()

    # ------------------------------------------------------------ access
    def _render_scopes(self) -> None:
        if self.f_public.value:
            self.scope_box.controls = [ft.Row([ft.Icon(ft.Icons.WARNING, color=ft.Colors.AMBER_800, size=18),
                                               ft.Text("Anyone who can reach the server can call this endpoint. "
                                                       "Use only for harmless data.", size=13)])]
            return
        known = [s for s in self.svc.access.known_scopes() if s != "*"]
        chips = [ft.Chip(label=ft.Text(s), selected=s in self.scopes, on_select=lambda _, s=s: self._toggle_scope(s))
                 for s in dict.fromkeys(known + self.scopes)]
        self.scope_box.controls = [
            ft.Text("Scopes an API key needs (all of them):", size=13), *chips, self.f_new_scope,
            ft.IconButton(ft.Icons.ADD, tooltip="Add scope", on_click=self._add_scope),
            *([_small("No scope: any valid key can call it.", color=ft.Colors.AMBER_900)] if not self.scopes else [])]

    # ------------------------------------------------------------ actions
    def _show_yaml(self, _=None) -> None:
        try:
            self.yaml_text.value = yaml.safe_dump([self.draft()], sort_keys=False, allow_unicode=True)
        except ServiceError as exc:
            self.yaml_text.value = f"# {exc}"
        self.ctx.page.update()

    def validate(self, _=None) -> None:
        try:
            d = self.draft()
        except ServiceError as exc:
            self.ctx.fail(exc)
            return
        if not d["name"]:
            d["name"] = "draft-endpoint"

        def done(v):
            warn = []
            try:
                warn = self.svc.endpoints.warnings(self.svc.endpoints.build(d))
            except ServiceError:
                pass
            self.results.controls = [ft.Row([ft.Icon(ft.Icons.CHECK_CIRCLE if v.ok else ft.Icons.ERROR,
                                                     color=ft.Colors.GREEN_700 if v.ok else ft.Colors.ERROR),
                                             ft.Text(v.message, expand=True)])] + \
                [ft.Row([ft.Icon(ft.Icons.WARNING, color=ft.Colors.AMBER_800, size=18), ft.Text(w, size=13, expand=True)])
                 for w in warn]
        self.ctx.background(lambda: self.svc.runtime.validate_endpoint(d, self.original), done, self.busy)

    def show_cli(self, _=None) -> None:
        try:
            d = self.draft()
        except ServiceError as exc:
            self.ctx.fail(exc)
            return
        d["name"] = d["name"] or "<name>"
        cli_dialog(self.ctx, "This endpoint with the CLI", [
            ("Create it", endpoint_cli(d)),
            ("Check and call it", f"portwright validate\nportwright endpoint show {d['name']}\n"
                                  f"portwright call {d['name']}" + "".join(f" -p {r.name}=<value>" for r in self.rows
                                                                          if r.required or r.location == "path"))],
            note="Endpoint params use the short form name:type:where[:required]; min/max/patterns are set in "
                 "portwright.yaml or in Studio.")

    def save(self) -> None:
        try:
            d = self.draft()
        except ServiceError as exc:
            self.ctx.fail(exc)
            return
        if not d["name"]:
            self.f_name.error = "give the endpoint a name"
            self.ctx.page.update()
            return
        self.f_name.error = None
        try:
            ep, warnings = self.svc.endpoints.save(d, original=self.original)
        except ServiceError as exc:
            self.ctx.fail(exc)
            return
        first = self.original is None
        self.original = ep.name
        self.saved = self._state()
        self.ctx.toast(f"Endpoint '{ep.name}' saved" + (" – " + "; ".join(warnings) if warnings else ""))
        self.view.refresh()
        if first:
            self.view.next_steps([ep], ep.scopes)

    def back(self, _=None) -> None:
        if self.is_dirty and (self.original or (self.f_name.value or "").strip()):
            self.ctx.confirm("Discard changes?", "Your unsaved changes to this endpoint will be lost.",
                             self.view.close_editor, yes="Discard")
            return
        self.view.close_editor()


# ================================================================= CRUD wizard
class CrudWizard:
    """SQL table -> list/get/create/update/delete endpoints, typed from the real columns."""

    def __init__(self, view: EndpointsView, source: str):
        self.view = view
        self.ctx = view.ctx
        self.svc = view.ctx.svc
        self.columns: list[dict] = []
        self.expose: set[str] = set()
        self.filters: set[str] = set()
        sql = [s["name"] for s in self.svc.sources.list() if s["type"] == "sql"]
        self.f_source = dropdown("SQL source", sql, source, width=240, on_select=lambda _: self.view.refresh())
        self.f_table = text_field("Table *", "", hint="e.g. hr.employees", width=280, on_submit=self.read)
        self.f_res = text_field("Resource name *", "", hint="used in names and the path, e.g. employees", width=280,
                                on_change=self._changed)
        self.f_key = dropdown("Key column", [], None, width=220, on_select=self._changed)
        self.ops = {o: ft.Checkbox(label=o, value=o in ("list", "get"), on_change=self._changed) for o in SQL_OPS}
        self.f_rs = text_field("Read scope", "", width=220, hint="default <resource>:read", on_change=self._changed)
        self.f_ws = text_field("Write scope", "", width=220, hint="default <resource>:write", on_change=self._changed)
        self.f_pub = ft.Switch(label="Reads are public (no key)", value=False, on_change=self._changed)
        self.grid = ft.Column(spacing=0)
        self.plan = ft.Column(spacing=4)
        self.busy = ft.ProgressBar(visible=False)

    def read(self, _=None) -> None:
        src, tbl = self.f_source.value, (self.f_table.value or "").strip()
        if not tbl:
            self.f_table.error = "enter or browse a table"
            self.ctx.page.update()
            return
        self.f_table.error = None

        def done(cols):
            self.columns = cols
            self.expose = {c["name"] for c in cols}
            pk = [c["name"] for c in cols if c["primary_key"]]
            self.f_key.options = [ft.DropdownOption(key=c["name"], text=c["name"]) for c in cols]
            self.f_key.value = pk[0] if pk else (cols[0]["name"] if cols else None)
            if not (self.f_res.value or "").strip():
                self.f_res.value = re.sub(r"[^a-zA-Z0-9]+", "-", tbl.rpartition(".")[2].lower()).strip("-")
            self.view.refresh(update=False)
        self.ctx.background(lambda: self.svc.sources.columns(src, tbl), done, self.busy)

    def browse(self, _=None) -> None:
        src = self.f_source.value

        def done(names):
            lst = ft.Column(spacing=0, height=380, scroll=ft.ScrollMode.AUTO)
            flt = text_field("Filter", autofocus=True)

            def pick(n):
                self.ctx.close()
                self.f_table.value = n
                self.read()

            def fill(_=None):
                q = (flt.value or "").lower()
                lst.controls = [ft.ListTile(title=ft.Text(n), dense=True, on_click=lambda _, n=n: pick(n))
                                for n in names if q in n.lower()][:300]
                self.ctx.page.update()
            flt.on_change = fill
            self.ctx.dialog(f"Tables in '{src}'", [flt, lst], [ft.TextButton("Cancel", on_click=lambda _: self.ctx.close())],
                            width=460)
            fill()
        self.ctx.background(lambda: self.svc.sources.tables(src), done, self.busy)

    def _changed(self, _=None) -> None:
        self._render_plan()
        self.ctx.page.update()

    def _set(self, which: set, col: str, on: bool) -> None:
        (which.add if on else which.discard)(col)
        self._changed()

    def render(self) -> ft.Control:
        ro = self.svc.sources.get(self.f_source.value).read_only
        for o in ("create", "update", "delete"):
            self.ops[o].disabled = ro
            if ro:
                self.ops[o].value = False
        lines = [ft.Row([ft.Container(_small(h, weight=ft.FontWeight.W_600), width=w)
                         for h, w in (("Column", 220), ("Type", 160), ("", 160), ("Expose", 80), ("Filter on list", 110))])]
        for c in self.columns:
            marks = ([tag("PK", "info")] if c["primary_key"] else []) + \
                    ([tag("NOT NULL")] if not c["nullable"] else [])
            lines.append(ft.Row([
                ft.Container(ft.Text(c["name"], weight=ft.FontWeight.W_600), width=220),
                ft.Container(_small(c["type"]), width=160), ft.Container(ft.Row(marks, spacing=4), width=160),
                ft.Container(ft.Checkbox(value=c["name"] in self.expose,
                                         on_change=lambda e, n=c["name"]: self._set(self.expose, n, e.control.value)),
                             width=80),
                ft.Container(ft.Checkbox(value=c["name"] in self.filters,
                                         on_change=lambda e, n=c["name"]: self._set(self.filters, n, e.control.value)),
                             width=110)]))
        self.grid.controls = [ft.Container(ft.Column(lines, spacing=2), padding=10, border_radius=10,
                                           border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT))] if self.columns else \
            [_small("Press Read columns (or Browse tables) to list the table's columns.")]
        self._render_plan()
        top = ft.Row([ft.IconButton(ft.Icons.ARROW_BACK, tooltip="Back", on_click=lambda _: self.view.close_editor()),
                      ft.Text("New table endpoints (CRUD)", size=24, weight=ft.FontWeight.W_600),
                      ft.Container(expand=True), help_btn(self.ctx, "endpoints"),
                      ft.OutlinedButton("CLI", icon=ft.Icons.TERMINAL, on_click=self.show_cli),
                      ft.FilledButton("Create endpoints", icon=ft.Icons.SAVE, on_click=self.create)])
        return ft.Column([top, self.busy,
            _card("1 · Table", [ft.Row([self.f_source, self.f_table,
                                        ft.OutlinedButton("Browse tables", icon=ft.Icons.TABLE_CHART, on_click=self.browse)],
                                       wrap=True, spacing=16, vertical_alignment=ft.CrossAxisAlignment.START)],
                  [ft.FilledTonalButton("Read columns", icon=ft.Icons.SYNC, on_click=self.read)]),
            _card("2 · Columns", [_small("Expose = returned by reads and writable by create/update. Filters become "
                                         "optional ?column= parameters of the list endpoint."),
                                  ft.Row([self.f_res, self.f_key], wrap=True, spacing=16,
                                         vertical_alignment=ft.CrossAxisAlignment.START), self.grid]),
            _card("3 · Operations and access", [
                ft.Row([ft.Container(cb, width=120) for cb in self.ops.values()], wrap=True),
                *([_small("The source is read-only, so only list and get are possible. Edit the source and allow "
                          "writes for create / update / delete.", color=ft.Colors.AMBER_900)] if ro else []),
                ft.Row([self.f_rs, self.f_ws, self.f_pub], wrap=True, spacing=16,
                       vertical_alignment=ft.CrossAxisAlignment.START)]),
            _card("4 · Endpoints that will be created", [self.plan]),
        ], spacing=16)

    def _plan(self) -> list[tuple[str, str, str, str]]:
        res = (self.f_res.value or "").strip() or "<resource>"
        key = self.f_key.value or "<key>"
        rs = (self.f_rs.value or "").strip() or f"{res}:read"
        ws = (self.f_ws.value or "").strip() or f"{res}:write"
        paths = {"list": ("GET", f"/{res}"), "get": ("GET", f"/{res}/{{{key}}}"), "create": ("POST", f"/{res}"),
                 "update": ("PATCH", f"/{res}/{{{key}}}"), "delete": ("DELETE", f"/{res}/{{{key}}}")}
        out = []
        for o, cb in self.ops.items():
            if cb.value:
                m, p = paths[o]
                scope = ("PUBLIC" if self.f_pub.value else rs) if o in ("list", "get") else ws
                out.append((m, p, f"{res}-{o}", scope))
        return out

    def _render_plan(self) -> None:
        base = self.svc.project.config.server.base_path
        self.plan.controls = [ft.Row([ft.Container(tag(m, METHOD_KIND[m]), width=80), ft.Container(mono(base + p), width=340),
                                      ft.Container(ft.Text(n), width=200), tag(s, "warn" if s == "PUBLIC" else "info")])
                              for m, p, n, s in self._plan()] or [_small("Tick at least one operation.")]

    def show_cli(self, _=None) -> None:
        res = (self.f_res.value or "<resource>").strip()
        key = self.f_key.value or "<key>"
        types = {c["name"]: c["type"] for c in self.columns}
        from ...services.catalog import _param_type
        fields = "".join(f" --field {c}:{_param_type(types.get(c, ''))}" for c in sorted(self.expose) if c != key)
        flt = "".join(f" --filter {f}" for f in sorted(self.filters))
        ro = not any(self.ops[o].value for o in ("create", "update", "delete"))
        cmd = (f"portwright endpoint crud {res} -s {self.f_source.value} --table {self.f_table.value or '<table>'} "
               f"--key {key} --key-type {_param_type(types.get(key, ''))}{fields}{flt}" + (" --read-only" if ro else ""))
        if (self.f_rs.value or "").strip():
            cmd += f" --read-scope {self.f_rs.value.strip()}"
        if (self.f_ws.value or "").strip():
            cmd += f" --write-scope {self.f_ws.value.strip()}"
        cli_dialog(self.ctx, "These endpoints with the CLI", [("Create them", cmd)],
                   note="The CLI creates either list+get (--read-only) or all five operations.")

    def create(self, _=None) -> None:
        if not self.columns:
            self.ctx.toast("Read the table's columns first.", error=True)
            return
        res = (self.f_res.value or "").strip()
        if not res:
            self.f_res.error = "give the resource a name"
            self.ctx.page.update()
            return
        ops = [o for o, cb in self.ops.items() if cb.value]
        try:
            new = self.svc.endpoints.crud_from_table(
                res, self.f_source.value, (self.f_table.value or "").strip(), self.f_key.value, self.columns,
                [c["name"] for c in self.columns if c["name"] in self.expose],
                [c["name"] for c in self.columns if c["name"] in self.filters], ops, (self.f_rs.value or "").strip() or None,
                (self.f_ws.value or "").strip() or None, public_read=bool(self.f_pub.value))
        except ServiceError as exc:
            self.ctx.fail(exc)
            return
        self.view.close_editor()
        self.view.next_steps(new, sorted({s for e in new for s in e.scopes}))
