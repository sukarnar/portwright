"""Loads screen: list load jobs, design mappings, check, preview, run, history and rejects."""
from __future__ import annotations

import json

import flet as ft

from ...services import ServiceError
from ..common import Ctx, dropdown, empty, header, icon_btn, mono, table, tag, text_field, help_btn
from ..viewmodels import (CONSTANT, DATE_FORMATS, DUPLICATE_KEYS, EXISTING_KEYS, LOAD_MODES, MAP_TYPES,
                          NOT_MAPPED, ON_HEADER_CHANGE, ON_UNCHANGED, RUN_STATUS_KIND, SIMPLE_TRANSFORMS,
                          TS_FORMATS, MapRow, apply_suggestion, build_transforms, cell, load_draft,
                          mapping_rows, report_counts, source_state, split_transforms, transform_summary,
                          unused_sources)

STATUS_ICON = {"bad": (ft.Icons.ERROR, ft.Colors.ERROR), "warn": (ft.Icons.WARNING, ft.Colors.AMBER_800),
               "good": (ft.Icons.CHECK_CIRCLE, ft.Colors.GREEN_700),
               "neutral": (ft.Icons.REMOVE_CIRCLE_OUTLINE, ft.Colors.OUTLINE)}


def _card(title: str, body: list[ft.Control], actions: list[ft.Control] | None = None) -> ft.Control:
    return ft.Container(
        padding=20, border_radius=12, border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
        content=ft.Column([ft.Row([ft.Text(title, size=16, weight=ft.FontWeight.W_600, expand=True),
                                   *(actions or [])]), *body], spacing=14))


def _small(text: str, color=ft.Colors.ON_SURFACE_VARIANT, **kw) -> ft.Text:
    return ft.Text(text, size=12, color=color, **kw)


class LoadsView:
    title = "Loads"

    def __init__(self, ctx: Ctx):
        self.ctx = ctx
        self.root = ft.Column(expand=True, scroll=ft.ScrollMode.AUTO, spacing=16)
        self.editor: LoadEditor | None = None
        self.status: dict[str, dict | str] = {}     # load -> source_status() result, or an error text

    def build(self) -> ft.Control:
        self.refresh(update=False)
        return self.root

    def refresh(self, update: bool = True) -> None:
        if self.editor is not None:
            self.root.controls = [self.editor.render()]
        else:
            self.root.controls = self._list()
        if update:
            self.ctx.page.update()

    # -------------------------------------------------------------- list
    def _list(self) -> list[ft.Control]:
        svc = self.ctx.svc.loads
        items = svc.list()
        controls: list[ft.Control] = [header(
            "Loads", "Copy rows from a file or endpoint into a database table, with column mapping",
            [help_btn(self.ctx, "loads"), ft.OutlinedButton("Check sources", icon=ft.Icons.FIND_REPLACE, on_click=lambda _: self.check_all(),
                               tooltip="Read every source and show whether it changed since its last load"),
             ft.FilledButton("New load", icon=ft.Icons.ADD, on_click=lambda _: self.open_editor(None))])]
        if not items:
            opts = svc.options()
            hint = ("First add a SQL source with writes allowed (Sources screen) and a file source or GET endpoint."
                    if not opts["targets"] or not (opts["file_sources"] or opts["endpoints"]) else "")
            controls.append(empty("No load jobs yet." + (" " + hint if hint else ""),
                                  ft.FilledButton("Create your first load", on_click=lambda _: self.open_editor(None))))
            return controls
        rows = []
        for l in items:
            r = l["last_run"]
            last = (ft.Row([tag(r["status"], RUN_STATUS_KIND.get(r["status"], "neutral")),
                            _small(r["started"][:16].replace("T", " "))], spacing=6) if r else _small("never run"))
            mapped = ft.Row([ft.Text(str(l["columns"]))] + ([tag(f"{l['review']} to review", "warn")]
                                                            if l["review"] else []), spacing=6)
            rows.append([
                ft.Text(l["name"], weight=ft.FontWeight.W_600),
                ft.Text(("endpoint " if l["from_kind"] == "endpoint" else "file ") + l["from"]),
                ft.Text(f"{l['to_source']}.{l['table']}"),
                ft.Text(l["mode"] + (f" ({', '.join(l['keys'])})" if l["keys"] else "")),
                mapped, self._source_badge(l["name"]), last,
                ft.Row([icon_btn(ft.Icons.EDIT, "Edit mapping", lambda _, n=l["name"]: self.open_editor(n)),
                        icon_btn(ft.Icons.PLAY_ARROW, "Run", lambda _, n=l["name"]: self.run(n)),
                        icon_btn(ft.Icons.HISTORY, "History", lambda _, n=l["name"]: self.history(n)),
                        icon_btn(ft.Icons.DELETE, "Delete", lambda _, n=l["name"]: self.delete(n), ft.Colors.ERROR)],
                       spacing=0)])
        controls.append(table(["Name", "Reads from", "Writes to", "Mode", "Columns", "Source", "Last run", ""], rows))
        runs = list(reversed(svc.history(last=10)))
        if runs:
            controls += [ft.Text("Recent runs", size=16, weight=ft.FontWeight.W_600), self._runs_table(runs)]
        return controls

    def _source_badge(self, name: str) -> ft.Control:
        st = self.status.get(name)
        if st is None:
            return _small("–", tooltip="Press 'Check sources'")
        if isinstance(st, str):
            return ft.Container(tag("not readable", "bad"), tooltip=st)
        kind, text = source_state(st)
        return ft.Container(tag(text, kind), tooltip=f"{st['rows']} rows · hash {st['hash'][:12]}"
                            + (f" · file modified {st['modified'][:16].replace('T', ' ')}" if st.get("modified") else ""))

    def check_all(self) -> None:
        names = [l["name"] for l in self.ctx.svc.loads.list()]
        self.ctx.toast("Reading the sources…")

        def work():
            out = {}
            for n in names:
                try:
                    out[n] = self.ctx.svc.loads.source_status(n)
                except ServiceError as exc:
                    out[n] = str(exc)
            return out

        def done(res):
            self.status.update(res)
            changed = [n for n, st in res.items() if isinstance(st, dict) and st["changed"] is not False]
            self.ctx.toast(f"{len(changed)} of {len(res)} source(s) changed or never loaded")
            if self.editor is None:
                self.refresh(update=False)
        self.ctx.background(work, done)

    def _runs_table(self, runs: list[dict]) -> ft.Control:
        rows = []
        for r in runs:
            written = r.get("inserted", 0) + r.get("updated", 0) + r.get("upserted", 0)
            err = (r.get("error") or "").splitlines()[0] if r.get("error") else ""
            rows.append([
                ft.Text(r["started"][:19].replace("T", " ")), ft.Text(r["load"]),
                ft.Container(tag(r["status"], RUN_STATUS_KIND.get(r["status"], "neutral")), tooltip=err or None),
                ft.Text(str(r["read"])), ft.Text(str(written)),
                ft.Text(str(r["rejected"]), color=ft.Colors.ERROR if r["rejected"] else None),
                ft.Text(f"v{r['source_version']}" if r.get("source_version") else "–"),
                ft.Text(f"{r['seconds']}s"),
                icon_btn(ft.Icons.RULE, "View rejected rows", lambda _, i=r["run_id"]: self.rejects(i))
                if r.get("rejects_file") else ft.Text(""),
            ])
        return table(["Started (UTC)", "Load", "Status", "Read", "Written", "Rejected", "Source", "Time", ""], rows)

    # ----------------------------------------------------------- editor
    def open_editor(self, name: str | None, from_endpoint: str | None = None) -> None:
        opts = self.ctx.svc.loads.options()
        if name is None and not opts["targets"]:
            self.ctx.toast("Add a SQL source first (Sources screen) – loads write into a database table.", error=True)
            return
        try:
            self.editor = LoadEditor(self, name)
        except ServiceError as exc:
            self.ctx.fail(exc)
            return
        if from_endpoint:
            ed = self.editor
            ed.from_kind = "endpoint"
            ed.f_kind.selected = ["endpoint"]
            ed._fill_from_options()
            ed.f_from.value = from_endpoint
            ed.f_name.value = f"{from_endpoint}-to-db"
        self.refresh()
        if name is not None:
            self.editor.read_columns()

    def close_editor(self) -> None:
        self.editor = None
        self.refresh()

    # ---------------------------------------------------------- actions
    def run(self, name: str, after=None, force: bool = False) -> None:
        spec = self.ctx.svc.loads.get(name)
        text = f"Load rows into {spec.to.source}.{spec.to.table} ({LOAD_MODES[spec.mode].split(' – ')[0]})."
        if spec.mode == "replace":
            text += ("\n\nKill & fill DELETES every row in the table first, in the same transaction – if the "
                     "insert fails the old rows are kept.")
            if spec.safety.min_rows or spec.safety.min_percent:
                text += (f" Safety: at least {spec.safety.min_rows} rows and {spec.safety.min_percent:g}% of the "
                         f"current row count.")
        if spec.keys:
            text += f"\n\nLoad key: {', '.join(spec.keys)} (duplicates: {spec.duplicate_keys})."
        if force:
            text += "\n\nRun anyway: the unchanged-source and column-change checks are skipped."
        text += "\n\nAll rows are written in one transaction; if anything fails nothing is changed."
        self.ctx.confirm(f"Run load '{name}'?", text, lambda: self._run(name, after, force),
                         danger=spec.mode == "replace", yes="Run anyway" if force else "Run load")

    def _run(self, name: str, after=None, force: bool = False) -> None:
        bar = ft.ProgressBar(width=420)
        self.ctx.dialog(f"Running '{name}'…", [
            ft.Text("Reading the source, mapping every row, then writing in one transaction."), bar], [], width=460,
            scroll=False)

        def done(rep):
            self.ctx.close()
            self.show_report(rep, after)
            self.status.pop(name, None)
            if after:
                after()
            else:
                self.refresh(update=False)

        def failed_close(work):
            def _w():
                try:
                    return work()
                except Exception:
                    self.ctx.close()
                    raise
            return _w
        self.ctx.background(failed_close(lambda: self.ctx.svc.loads.run(name, force=force)), done)

    def show_report(self, rep, after=None) -> None:
        d = rep.to_dict()
        title = {"success": "Load finished", "partial": "Loaded with rejected rows", "failed": "Load failed",
                 "dry_run": "Dry run finished", "skipped": "Skipped – source unchanged"}[rep.status]
        body: list[ft.Control] = [
            ft.Row([tag(rep.status.upper(), RUN_STATUS_KIND[rep.status]),
                    _small(f"run {rep.run_id} · {rep.mode} · {rep.seconds}s")]),
            ft.Row([self._count(k, v) for k, v in report_counts(d)], wrap=True, spacing=12)]
        if rep.source_hash:
            kind, text = source_state(d)
            ver = f"v{rep.source_version}" if rep.source_version else "new data"
            body.append(ft.Row([_small("Source"), tag(ver, "info"), tag(text, kind),
                                _small(f"hash {rep.source_hash[:12]}")], wrap=True, spacing=6))
        if rep.keys or rep.table_rows is not None:
            body.append(_small(" · ".join(([f"key: {', '.join(rep.keys)}"] if rep.keys else [])
                                          + ([f"table had {rep.table_rows} rows before kill & fill"]
                                             if rep.table_rows is not None else []))))
        if rep.status == "skipped":
            body.append(ft.Text(rep.error or "", size=13))
        elif rep.error:
            body.append(ft.Container(mono(rep.error, 12), bgcolor=ft.Colors.ERROR_CONTAINER, padding=12,
                                     border_radius=8))
        if rep.status == "failed":
            body.append(_small("Nothing was written – the transaction was rolled back."))
            if rep.rejected and "max_rejects" in (rep.error or ""):
                body.append(ft.Row([ft.Icon(ft.Icons.LIGHTBULB, color=ft.Colors.PRIMARY, size=18), ft.Text(
                    "Fix those rows in the source, or raise 'Max rejects' to load the good rows and keep the bad "
                    "ones in the rejects file.", size=13, expand=True)]))
            low = (rep.error or "").lower()
            if any(k in low for k in ("unique", "ora-00001", "duplicate", "primary key")):
                body.append(ft.Row([ft.Icon(ft.Icons.LIGHTBULB, color=ft.Colors.PRIMARY, size=18), ft.Text(
                    "Some rows already exist in the table. Use Upsert mode (tick the key columns) to update them, "
                    "or Replace mode to reload the whole table.", size=13, expand=True)]))
        for w in rep.warnings:
            body.append(ft.Row([ft.Icon(ft.Icons.WARNING, color=ft.Colors.AMBER_800, size=18),
                                ft.Text(w, expand=True, size=13)]))
        actions = []
        if rep.rejects_file:
            actions.append(ft.OutlinedButton("View rejected rows", icon=ft.Icons.RULE,
                                             on_click=lambda _: self.rejects(rep.run_id)))
        if rep.status == "skipped" or (rep.status == "failed" and "columns changed" in (rep.error or "")):
            def again(_):
                self.ctx.close()
                self.run(rep.load, after=after, force=True)
            actions.append(ft.OutlinedButton("Run anyway", icon=ft.Icons.PLAY_ARROW, on_click=again))
        actions.append(ft.TextButton("Close", on_click=lambda _: self.ctx.close()))
        self.ctx.dialog(title, body, actions, width=620, modal=False, scroll=len(rep.warnings) > 4)

    @staticmethod
    def _count(label: str, value: int) -> ft.Control:
        return ft.Container(
            padding=ft.Padding.symmetric(horizontal=14, vertical=8), border_radius=10,
            border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
            content=ft.Column([ft.Text(str(value), size=20, weight=ft.FontWeight.W_600,
                                       color=ft.Colors.ERROR if label == "rejected" and value else None),
                               _small(label)], spacing=0, horizontal_alignment=ft.CrossAxisAlignment.CENTER))

    def history(self, name: str) -> None:
        runs = list(reversed(self.ctx.svc.loads.history(name, last=50)))
        body = [self._runs_table(runs)] if runs else [ft.Text("No runs yet.")]
        self.ctx.info(f"History – {name}", body, width=900)

    def rejects(self, run_id: str) -> None:
        try:
            cols, rows, total = self.ctx.svc.loads.rejects(run_id, limit=200)
        except ServiceError as exc:
            self.ctx.fail(exc)
            return
        data_cols = [c for c in cols if c not in ("record", "reason")]
        grid = table(["Record", "Reason", *data_cols], [
            [ft.Text(r["record"]), ft.Text(r["reason"], color=ft.Colors.ERROR, width=280),
             *[ft.Text(cell(r.get(c), 24)) for c in data_cols]] for r in rows])
        body = [_small(f"{total} rejected row(s)" + (f" – showing the first {len(rows)}" if total > len(rows) else "")
                       + f". File: logs/loads/{run_id}_rejects.csv"),
                ft.Row([grid], scroll=ft.ScrollMode.AUTO)]
        self.ctx.info(f"Rejected rows – run {run_id}", body, width=980)

    def versions_table(self, name: str, versions: list[dict]) -> ft.Control:
        rows = []
        for i, v in enumerate(versions):
            prev = versions[i + 1] if i + 1 < len(versions) else None
            hc = "; ".join(f"{k}: {', '.join(c)}" for k, c in v["header_change"].items())
            loaded = [r for r in v["runs"] if r["status"] in ("success", "partial")]
            rows.append([
                ft.Text(f"v{v['version']}", weight=ft.FontWeight.W_600),
                ft.Text(v["first_seen"][:16].replace("T", " ")),
                ft.Text(str(v["rows"])),
                ft.Text(v.get("file") or v["kind"]),
                _small((v.get("modified") or "")[:16].replace("T", " ") or "–"),
                mono(v["hash"][:12], 12),
                tag(f"loaded ×{len(loaded)}", "good") if loaded else tag("not loaded"),
                ft.Container(tag("columns changed", "warn"), tooltip=hc) if hc else ft.Text(""),
                ft.Row([icon_btn(ft.Icons.COMPARE_ARROWS, f"Compare v{prev['version']} with v{v['version']}",
                                 lambda _, a=prev["version"], b=v["version"]: self.diff(name, a, b))]
                       if prev and v["archived"] and prev["archived"] else [_small("" if v["archived"] else "no copy")]),
            ])
        return table(["Version", "First seen (UTC)", "Rows", "Source", "File modified", "Hash", "Loaded",
                      "Columns", ""], rows)

    def diff(self, name: str, old: int, new: int) -> None:
        def done(d):
            by = f"compared by key {', '.join(d['key'])}" if d["key"] else \
                "compared by whole rows – set a load key (mapped from a source column) to see changed rows"
            body: list[ft.Control] = [
                _small(by),
                ft.Row([self._count("added", d["added"]), self._count("removed", d["removed"]),
                        *([self._count("changed", d["changed"])] if d["changed"] is not None else []),
                        self._count("unchanged", d["unchanged"])], spacing=12, wrap=True)]

            def rows_table(title, items, color):
                if not items:
                    return []
                cols: list[str] = []
                for r in items:
                    cols += [k for k in r if k not in cols]
                return [ft.Text(title, weight=ft.FontWeight.W_600, color=color),
                        ft.Row([table(cols, [[ft.Text(cell(r.get(c), 24)) for c in cols] for r in items])],
                               scroll=ft.ScrollMode.AUTO)]
            body += rows_table("Added rows", d["added_rows"], ft.Colors.GREEN_800)
            body += rows_table("Removed rows", d["removed_rows"], ft.Colors.ERROR)
            if d["changed_rows"]:
                body += [ft.Text("Changed rows", weight=ft.FontWeight.W_600, color=ft.Colors.AMBER_900),
                         table(["Key", "Column", "Before", "After"], [
                             [ft.Text(", ".join(f"{k}={v}" for k, v in c["key"].items())), ft.Text(f),
                              ft.Text(cell(c["before"][f], 30)), ft.Text(cell(c["after"][f], 30))]
                             for c in d["changed_rows"] for f in c["fields"]])]
            self.ctx.info(f"{name}: v{old} compared with v{new}", body, width=900)
        self.ctx.background(lambda: self.ctx.svc.loads.diff(name, old, new), done)

    def delete(self, name: str) -> None:
        def go():
            self.ctx.svc.loads.remove(name)
            self.ctx.toast(f"Load '{name}' deleted (the table and its data were not touched)")
            self.refresh()
        self.ctx.confirm("Delete load?", f"The definition of '{name}' will be removed. "
                                         "The target table and its data are not touched.", go)


# ======================================================================= editor
class LoadEditor:
    """Edit one load: settings, the mapping grid, checks, preview, save and run."""

    def __init__(self, view: LoadsView, name: str | None):
        self.view = view
        self.ctx = view.ctx
        self.svc = view.ctx.svc.loads
        self.original = name
        self.opts = self.svc.options()
        if name:
            spec = self.svc.get(name)
            d = spec.model_dump(mode="json", by_alias=True, exclude_none=True)
            self.from_kind = "endpoint" if spec.from_.endpoint else "source"
            self.params = dict(spec.from_.params)
            self.rows = mapping_rows([], d.get("columns", []), spec.keys)
            for r in self.rows:
                r.in_table = True            # unknown until the table is read
            vals = dict(name=spec.name, description=spec.description, from_name=spec.from_.endpoint or spec.from_.source,
                        to=spec.to.source, table=spec.to.table, mode=spec.mode, batch=str(spec.batch_size),
                        max_rejects=str(spec.max_rejects), unmapped=spec.unmapped_source_columns)
            st = spec.model_dump(include={"duplicate_keys", "existing_keys", "tracking", "safety"})
        else:
            self.from_kind = "source" if self.opts["file_sources"] else "endpoint"
            self.params = {}
            self.rows: list[MapRow] = []
            writable = [t for t in self.opts["targets"] if t not in self.opts["read_only_targets"]]
            vals = dict(name="", description="", from_name=None, to=(writable or self.opts["targets"] or [None])[0],
                        table="", mode="insert", batch="1000", max_rejects="0", unmapped="ignore")
            from ...config import LoadSafety, SourceTracking
            st = {"duplicate_keys": "reject", "existing_keys": "reject",
                  "tracking": SourceTracking().model_dump(), "safety": LoadSafety().model_dump()}
        tr, sf = st["tracking"], st["safety"]
        self.inspection = None
        self.check = None
        self.dry = None
        self.busy = ft.ProgressBar(visible=False)
        self.only_problems = False

        # settings controls (created once so typed values survive re-renders)
        self.f_name = text_field("Load name *", vals["name"], hint="letters, digits, - and _", width=260)
        self.f_desc = text_field("Description", vals["description"], expand=True)
        self.f_kind = ft.SegmentedButton(
            segments=[ft.Segment(value="source", label=ft.Text("File source"), icon=ft.Icon(ft.Icons.DESCRIPTION)),
                      ft.Segment(value="endpoint", label=ft.Text("Endpoint"), icon=ft.Icon(ft.Icons.API))],
            selected=[self.from_kind], on_change=self._kind_changed)
        self.f_from = dropdown("Read from *", [], vals["from_name"], width=260, on_select=self._source_changed)
        self.f_to = dropdown("Target SQL source *", self.opts["targets"], vals["to"], width=260,
                             on_select=self._target_changed)
        self.f_table = text_field("Target table *", vals["table"], hint="e.g. emp_transfers or hr.emp_transfers",
                                  width=300, on_blur=self._table_changed, on_submit=self._table_changed)
        self.f_mode = dropdown("Mode", LOAD_MODES, vals["mode"], width=390, on_select=self._mode_changed)
        self.f_batch = text_field("Batch size", vals["batch"], width=120, on_change=self._dirty)
        self.f_rejects = text_field("Max rejects", vals["max_rejects"], width=150,
                                    hint="bad rows allowed", on_change=self._dirty)
        self.f_unmapped = dropdown("Extra source columns", {"ignore": "Ignore them", "error": "Fail the load"},
                                   vals["unmapped"], width=200, on_select=self._dirty)
        # key checks, kill & fill safety, source tracking
        self.f_dup = dropdown("Same key twice in the source", DUPLICATE_KEYS, st["duplicate_keys"], width=250,
                              on_select=self._dirty)
        self.f_exist = dropdown("Key already in the table", EXISTING_KEYS, st["existing_keys"], width=230,
                                on_select=self._dirty)
        self.f_min_rows = text_field("Refuse if fewer rows than", str(sf["min_rows"]), width=200,
                                     hint="0 = no limit", on_change=self._dirty)
        self.f_min_pct = text_field("… or below % of current rows", f"{sf['min_percent']:g}", width=220,
                                    hint="e.g. 50; 0 = no limit", on_change=self._dirty)
        self.f_unch = dropdown("Source unchanged since last load", ON_UNCHANGED, tr["on_unchanged"], width=270,
                               on_select=self._dirty)
        self.f_hdr = dropdown("Source columns changed", ON_HEADER_CHANGE, tr["on_header_change"], width=220,
                              on_select=self._dirty)
        self.f_stable = text_field("Wait until file unchanged (s)", str(tr["stable_seconds"]), width=210,
                                   hint="0 = don't wait", on_change=self._dirty)
        self.f_maxage = text_field("Max file age (hours)", "" if tr.get("max_age_hours") is None
                                   else f"{tr['max_age_hours']:g}", width=200, hint="empty = no limit",
                                   on_change=self._dirty)
        self.f_keep = text_field("Copies to keep", str(tr["archive_keep"]), width=160,
                                 hint="0 = fingerprint only", on_change=self._dirty)
        self.source_line = ft.Row(wrap=True, spacing=8)
        for f in (self.f_name, self.f_desc):
            f.on_change = self._dirty
        self._fill_from_options()
        self.grid = ft.Column(spacing=0)
        self.summary = ft.Row(wrap=True, spacing=8)
        self.results = ft.Column(spacing=10)
        self.dirty_tag = ft.Container(visible=False, content=tag("unsaved changes", "warn"))
        self.saved_state = self._state() if name else None
        self._last_target = (None, None)

    # ------------------------------------------------------------ state
    def _state(self) -> str | None:
        try:
            return json.dumps(self.draft(), sort_keys=True, default=str)
        except ServiceError:
            return None

    def draft(self) -> dict:
        settings = {
            "duplicate_keys": self.f_dup.value, "existing_keys": self.f_exist.value,
            "tracking": {"on_unchanged": self.f_unch.value, "on_header_change": self.f_hdr.value,
                         "stable_seconds": self.f_stable.value, "max_age_hours": self.f_maxage.value,
                         "archive_keep": self.f_keep.value},
            "safety": {"min_rows": self.f_min_rows.value, "min_percent": self.f_min_pct.value}}
        return load_draft(self.f_name.value, self.f_desc.value, self.from_kind, self.f_from.value, self.f_to.value,
                          self.f_table.value, self.f_mode.value, self.rows, self.f_batch.value,
                          self.f_rejects.value, self.f_unmapped.value,
                          self.params if self.from_kind == "endpoint" else None, settings)

    @property
    def is_dirty(self) -> bool:
        return self.saved_state is None or self._state() != self.saved_state

    def _dirty(self, _=None) -> None:
        self.dirty_tag.visible = self.is_dirty
        self.ctx.page.update()

    # --------------------------------------------------------- handlers
    def _fill_from_options(self) -> None:
        names = self.opts["file_sources"] if self.from_kind == "source" else self.opts["endpoints"]
        self.f_from.options = [ft.DropdownOption(key=n, text=n) for n in names]
        if self.f_from.value not in names:
            self.f_from.value = names[0] if names else None
        self.f_from.helper_text = None if names else (
            "no file sources yet – add one on Sources" if self.from_kind == "source" else "no GET endpoints yet")

    def _kind_changed(self, e) -> None:
        self.from_kind = (e.control.selected or ["source"])[0]
        self._fill_from_options()
        self._source_changed()

    def _source_changed(self, _=None) -> None:
        self.params = {} if self.from_kind == "source" else self.params
        self.read_columns(quiet=True)

    def _target_changed(self, _=None) -> None:
        self._table_changed()

    def _table_changed(self, _=None) -> None:
        where = (self.f_to.value, (self.f_table.value or "").strip())
        if where != self._last_target and where[1]:
            self.read_columns(quiet=True)
        else:
            self._dirty()

    def _mode_changed(self, _=None) -> None:
        self.view.refresh()                 # mode decides which settings are shown

    # ---------------------------------------------------------- render
    def render(self) -> ft.Control:
        ro = self.f_to.value in self.opts["read_only_targets"]
        settings = _card("1 · Source and target", [
            ft.Row([self.f_name, self.f_desc], vertical_alignment=ft.CrossAxisAlignment.START),
            ft.Row([ft.Column([_small("Read rows from"), self.f_kind], spacing=4), self.f_from],
                   wrap=True, spacing=16, vertical_alignment=ft.CrossAxisAlignment.END),
            ft.Row([self.f_to, self.f_table,
                    ft.OutlinedButton("Browse tables", icon=ft.Icons.TABLE_CHART, on_click=self.browse_tables)],
                   wrap=True, spacing=16, vertical_alignment=ft.CrossAxisAlignment.START),
            *([ft.Row([ft.Icon(ft.Icons.WARNING, color=ft.Colors.AMBER_800, size=18),
                       ft.Text(f"'{self.f_to.value}' is read-only. Edit it on the Sources screen and switch on "
                               "'Allow writes' before running this load.", size=13, expand=True)])] if ro else []),
            *([_small(f"Endpoint parameters (from {self.ctx.svc.project.path.name}): " +
                      ", ".join(f"{k}={v}" for k, v in self.params.items()))] if self.params else []),
        ], [ft.FilledTonalButton("Read columns", icon=ft.Icons.SYNC, on_click=lambda _: self.read_columns())])

        mode = self.f_mode.value
        how = _card("2 · How rows are written", [
            ft.Row([self.f_mode, self.f_batch, self.f_rejects, self.f_unmapped], wrap=True, spacing=16,
                   vertical_alignment=ft.CrossAxisAlignment.START),
            _small({"insert": "Append adds the rows. With a load key, rows whose key is already in the table are "
                              "handled as set below – nothing is overwritten.",
                    "replace": "Kill & fill deletes every row of the table and inserts the file, in ONE transaction: "
                               "if anything fails, the old rows are kept.",
                    "upsert": "Upsert updates the rows whose key matches and inserts the others. The key is "
                              "required."}[mode]),
            ft.Text("Load key checks", weight=ft.FontWeight.W_600),
            _small("Tick the key columns in the mapping grid (column 'Key'). The key is checked even when the table "
                   "has no primary key: empty keys and repeated keys are rejected before anything is written."),
            ft.Row([self.f_dup, *([self.f_exist] if mode == "insert" else [])], wrap=True, spacing=16,
                   vertical_alignment=ft.CrossAxisAlignment.START),
            *([ft.Text("Kill & fill safety", weight=ft.FontWeight.W_600),
               _small("Protects the table from an empty or half-copied file: the load is refused and the table "
                      "is not touched."),
               ft.Row([self.f_min_rows, self.f_min_pct], wrap=True, spacing=16,
                      vertical_alignment=ft.CrossAxisAlignment.START)] if mode == "replace" else []),
        ])

        versions = _card("5 · Source versions and change detection", [
            _small("Every run fingerprints the source (row hash, size, modified time, columns) and keeps each "
                   "distinct content as a numbered version, so you can see whether the file changed before loading."),
            ft.Row([self.f_unch, self.f_hdr], wrap=True, spacing=16, vertical_alignment=ft.CrossAxisAlignment.START),
            ft.Row([self.f_stable, self.f_maxage, self.f_keep], wrap=True, spacing=16,
                   vertical_alignment=ft.CrossAxisAlignment.START),
            self.source_line,
            *self._versions_body(),
        ], [ft.OutlinedButton("Check source now", icon=ft.Icons.FIND_REPLACE, on_click=self.check_source,
                              tooltip="Read the source and compare it with the last successful load")])

        mapping = _card("3 · Column mapping", [
            _small("One line per column of the target table. Choose the source column (or a constant) that fills it. "
                   "Auto-map guesses have a yellow warning icon – check each one and press Confirm."),
            self.summary, self.grid,
        ], [ft.OutlinedButton("Auto-map", icon=ft.Icons.AUTO_FIX_HIGH, on_click=self.auto_map,
                              tooltip="Match source columns to table columns by name"),
            ft.TextButton("Clear", icon=ft.Icons.CLEAR_ALL, on_click=self.clear),
            ft.Switch(label="Only lines that need attention", value=self.only_problems, on_change=self._filter)])

        checks = _card("4 · Check and preview", [
            _small("Validate checks the mapping against the real source and table. Preview maps every row "
                   "without writing anything and shows the first rows before and after mapping."),
            self.results,
        ], [ft.OutlinedButton("Validate", icon=ft.Icons.FACT_CHECK, on_click=self.validate),
            ft.OutlinedButton("Preview (dry run)", icon=ft.Icons.PREVIEW, on_click=self.preview)])

        top = ft.Row([
            ft.IconButton(ft.Icons.ARROW_BACK, tooltip="Back to loads", on_click=self.back),
            ft.Text(f"Edit load '{self.original}'" if self.original else "New load", size=24,
                    weight=ft.FontWeight.W_600),
            self.dirty_tag, ft.Container(expand=True), help_btn(self.ctx, "loads"),
            ft.OutlinedButton("CLI", icon=ft.Icons.TERMINAL, on_click=self.show_cli,
                              tooltip="The same load with the command line / for a scheduler"),
            ft.OutlinedButton("Run", icon=ft.Icons.PLAY_ARROW, on_click=self.run),
            ft.FilledButton("Save", icon=ft.Icons.SAVE, on_click=lambda _: self.save()),
        ])
        self.render_grid(update=False)
        self.dirty_tag.visible = self.is_dirty
        return ft.Column([top, self.busy, settings, how, mapping, checks, versions], spacing=16)

    def _versions_body(self) -> list[ft.Control]:
        if not self.original:
            return [_small("Versions appear here after the first run of the saved load.")]
        try:
            vs = self.svc.versions(self.original)
        except ServiceError:
            return []
        if not vs:
            return [_small("No versions yet – the first real run records version 1.")]
        return [ft.Row([self.view.versions_table(self.original, vs[:30])], scroll=ft.ScrollMode.AUTO)]

    def check_source(self, _=None) -> None:
        d = self._draft_or_fail()
        if d is None:
            return
        if self.original:
            d["name"] = self.original          # compare with this load's history

        def done(st):
            self.view.status[d["name"]] = st
            kind, text = source_state(st)
            ver = f"v{st['version']}" if st["version"] else "new content (not loaded yet)"
            items: list[ft.Control] = [_small("Source now:"), tag(ver, "info"), tag(text, kind),
                                       _small(f"{st['rows']} rows · hash {st['hash'][:12]}"
                                              + (f" · {st['file']} modified {st['modified'][:16].replace('T', ' ')} UTC"
                                                 if st.get("file") else ""))]
            for k, cols in st["header_change"].items():
                items.append(tag(f"columns {k}: {', '.join(cols)}", "warn"))
            if st["changed"] is False and self.f_unch.value == "skip":
                items.append(_small("A run would be skipped (exit code 3) – use Run anyway to load it again."))
            self.source_line.controls = items
        self.ctx.background(lambda: self.svc.source_status(d), done, self.busy)

    def render_grid(self, update: bool = True) -> None:
        ins = self.inspection
        header_cols = ins.header if ins else None
        upsert = True                      # the key column is shown in every mode
        widths = [26, 190, 200, 120, 160, 44, 140, 96]
        heads = ["", "Target column", "Source", "Type", "Format / value", "Key", "Options", ""]
        lines: list[ft.Control] = [self._line([_small(h, weight=ft.FontWeight.W_600) for h in heads], widths,
                                              head=True)]
        shown = 0
        for r in self.rows:
            kind, msg = r.status(header_cols)
            if self.only_problems and kind in ("good", "neutral"):
                continue
            shown += 1
            ctl = self._row_controls(r, kind, msg, header_cols or [], upsert)
            lines.append(self._line(ctl, widths))
        if not self.rows:
            lines.append(ft.Container(padding=20, content=ft.Text(
                "Press 'Read columns' to list the columns of the target table." if not ins else
                (ins.target_error and f"Cannot read the table: {ins.target_error}") or "The table has no columns.",
                color=ft.Colors.ON_SURFACE_VARIANT)))
        elif not shown:
            lines.append(ft.Container(padding=20, content=ft.Text("Nothing needs attention.",
                                                                  color=ft.Colors.ON_SURFACE_VARIANT)))
        self.grid.controls = [ft.Container(border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT), border_radius=10,
                                           content=ft.Row([ft.Column(lines, spacing=0)], scroll=ft.ScrollMode.AUTO))]
        self._render_summary()
        if update:
            self.dirty_tag.visible = self.is_dirty
            self.ctx.page.update()

    @staticmethod
    def _line(controls: list[ft.Control], widths: list[int], head: bool = False) -> ft.Control:
        return ft.Container(
            padding=ft.Padding.symmetric(horizontal=12, vertical=6),
            bgcolor=ft.Colors.SURFACE_CONTAINER_LOW if head else None,
            border=ft.Border.only(bottom=ft.BorderSide(1, ft.Colors.OUTLINE_VARIANT)),
            content=ft.Row([ft.Container(c, width=w) for c, w in zip(controls, widths)], spacing=8,
                           vertical_alignment=ft.CrossAxisAlignment.CENTER))

    def _row_controls(self, r: MapRow, kind: str, msg: str, header_cols: list[str], upsert: bool) -> list:
        icon, color = STATUS_ICON[kind]
        marks = []
        if r.primary_key:
            marks.append(tag("PK", "info"))
        if r.needs_value:
            marks.append(tag("NOT NULL", "neutral"))
        target = ft.Column([ft.Text(r.target, weight=ft.FontWeight.W_600, selectable=True),
                            ft.Row([_small(r.db_type or "?"), *marks], spacing=4, wrap=True)], spacing=2)

        options = [NOT_MAPPED, CONSTANT] + header_cols
        if r.source is not None and r.source not in header_cols:
            options.append(r.source)
        current = CONSTANT if r.is_constant else (r.source if r.source is not None else NOT_MAPPED)
        src = dropdown(None, {o: (f"{o}  (missing)" if o == r.source and o not in header_cols and header_cols else o)
                            for o in options}, current, on_select=lambda e, r=r: self._set_source(r, e.control.value))
        src.border_color = ft.Colors.AMBER_700 if r.review else None

        typ = dropdown(None, MAP_TYPES, r.type, on_select=lambda e, r=r: self._set(r, type=e.control.value, review=None))
        if r.is_constant:
            third = text_field(None, str(r.value if r.value is not None else ""), hint="${now} ${today} ${run_id}",
                               on_change=lambda e, r=r: self._set(r, value=e.control.value, render=False),
                               on_blur=lambda e: self.render_grid())
        elif r.type in ("date", "timestamp") and r.source is not None:
            fmts = DATE_FORMATS if r.type == "date" else TS_FORMATS
            third = dropdown(None, {"": "ISO (auto)", **{f: f for f in fmts},
                                  **({r.format: r.format} if r.format and r.format not in fmts else {})},
                             r.format or "", on_select=lambda e, r=r: self._set(r, format=e.control.value or None))
        else:
            third = ft.Text("")
        key = ft.Checkbox(value=r.key, disabled=not r.mapped,
                          tooltip="Part of the load key" if r.mapped else "Map the column first",
                          on_change=lambda e, r=r: self._set(r, key=e.control.value))
        notes = []
        if r.transform:
            notes.append(transform_summary(r.transform))
        if r.default not in (None, ""):
            notes.append(f"default {r.default!r}")
        if r.required:
            notes.append("required")
        opt = _small(" · ".join(notes) or "–", max_lines=2, overflow=ft.TextOverflow.ELLIPSIS)
        actions = [icon_btn(ft.Icons.TUNE, "Transforms, default, required…", lambda _, r=r: self.details(r))]
        if r.review:
            actions.insert(0, ft.TextButton("Confirm", on_click=lambda _, r=r: self._set(r, review=False)))
        return [ft.Icon(icon, color=color, size=20, tooltip=msg), target, src, typ, third, key, opt,
                ft.Row(actions, spacing=0)]

    def _render_summary(self) -> None:
        ins = self.inspection
        mapped = sum(1 for r in self.rows if r.mapped)
        bad = sum(1 for r in self.rows if r.status(ins.header if ins else None)[0] == "bad")
        review = sum(1 for r in self.rows if r.review and r.mapped)
        items = [tag(f"{len([r for r in self.rows if r.in_table])} table columns"), tag(f"{mapped} mapped", "info")]
        if bad:
            items.append(tag(f"{bad} problem(s)", "bad"))
        if review:
            items.append(tag(f"{review} to review", "warn"))
        keys = [r.target for r in self.rows if r.key and r.mapped]
        if keys:
            items.append(tag("key: " + ", ".join(keys), "info"))
        elif self.f_mode.value == "upsert":
            items.append(tag("upsert needs key columns – tick 'Key'", "bad"))
        else:
            pk = [r.target for r in self.rows if r.primary_key]
            items.append(tag("no load key – the table's primary key is used" if pk and all(
                r.mapped for r in self.rows if r.primary_key) else "no load key – duplicates are not checked",
                "neutral" if pk else "warn"))
        if ins:
            if ins.source_error:
                items.append(tag("source not readable", "bad"))
                items.append(_small(ins.source_error, color=ft.Colors.ERROR))
            else:
                items.append(tag(f"source: {len(ins.header)} columns, {ins.row_count} rows"))
                unused = unused_sources(ins.header, self.rows)
                if unused:
                    items.append(_small("not loaded: " + ", ".join(unused)))
        self.summary.controls = items

    def _filter(self, e) -> None:
        self.only_problems = bool(e.control.value)
        self.render_grid()

    # --------------------------------------------------------- editing
    def _set(self, r: MapRow, render: bool = True, **changes) -> None:
        for k, v in changes.items():
            if k == "review" and v is None:
                r.review = False
                continue
            setattr(r, k, v)
        if render:
            self.render_grid()
        else:
            self.dirty_tag.visible = self.is_dirty
            self.ctx.page.update()

    def _set_source(self, r: MapRow, value: str) -> None:
        if value == NOT_MAPPED:
            r.source, r.is_constant, r.value, r.key = None, False, None, False
        elif value == CONSTANT:
            r.source, r.is_constant = None, True
            r.value = r.value if r.value is not None else ""
        else:
            r.source, r.is_constant, r.value = value, False, None
        r.review = False
        self.render_grid()

    def clear(self, _=None) -> None:
        for r in self.rows:
            r.source, r.is_constant, r.value, r.key, r.review = None, False, None, False, False
            r.transform, r.default, r.required, r.format = [], None, False, None
        self.render_grid()

    def details(self, r: MapRow) -> None:
        t = split_transforms(r.transform)
        checks = {s: ft.Checkbox(label=s, value=s in t["simple"]) for s in SIMPLE_TRANSFORMS}
        rep_from = text_field("Replace text", t["replace_from"], width=200)
        rep_to = text_field("with", t["replace_to"], width=200)
        lookup = text_field("Code lookup (one per line: source value=loaded value)", t["lookup"], multiline=True,
                            min_lines=3, hint="e.g. Y=1 and N=0. Values not listed are rejected unless a default is set.")
        use_def = ft.Checkbox(label="Values not in the lookup become:", value=t["use_default"])
        look_def = text_field("", t["lookup_default"], width=200, hint="leave empty for NULL")
        default = text_field("Default when the source cell is blank", "" if r.default is None else str(r.default))
        required = ft.Switch(label="Required – reject the row when there is no value", value=r.required)
        fmt = text_field("Date format (Python strptime)", r.format or "", hint="%m/%d/%Y  %d-%b-%Y  %Y%m%d %H%M%S",
                         visible=r.type in ("date", "timestamp"))
        maxlen = text_field("Max length (optional)", "" if r.max_length is None else str(r.max_length), width=200,
                            visible=r.type == "string")

        def apply(_):
            try:
                steps = build_transforms([s for s, c in checks.items() if c.value], (rep_from.value or ""),
                                         rep_to.value or "", lookup.value or "", look_def.value or "",
                                         bool(use_def.value))
            except ServiceError as exc:
                lookup.error = str(exc)
                self.ctx.page.update()
                return
            ml = (maxlen.value or "").strip()
            if ml and not ml.isdigit():
                maxlen.error = "a whole number"
                self.ctx.page.update()
                return
            r.transform = steps
            r.default = default.value if (default.value or "") != "" else None
            r.required = bool(required.value)
            r.format = (fmt.value or "").strip() or None
            r.max_length = int(ml) if ml else None
            self.ctx.close()
            self.render_grid()

        self.ctx.dialog(f"Options for '{r.target}'", [
            _small(f"{r.db_type or 'column'} · source: " + ("constant" if r.is_constant else (r.source or "not mapped"))),
            ft.Text("Clean-up (applied in this order)", weight=ft.FontWeight.W_600),
            ft.Row([ft.Container(c, width=140) for c in checks.values()], wrap=True),
            ft.Row([rep_from, rep_to]), lookup, ft.Row([use_def, look_def]),
            ft.Text("Value rules", weight=ft.FontWeight.W_600), default, required, fmt, maxlen,
        ], [ft.TextButton("Cancel", on_click=lambda _: self.ctx.close()),
            ft.FilledButton("Apply", on_click=apply)], width=620)

    # -------------------------------------------------------- service calls
    def _draft_or_fail(self, need_name: bool = False) -> dict | None:
        try:
            d = self.draft()
        except ServiceError as exc:
            self.ctx.fail(exc)
            return None
        if not d["name"]:
            if need_name:
                self.f_name.error = "give the load a name"
                self.ctx.page.update()
                return None
            d["name"] = "draft"
        self.f_name.error = None
        if not d["from"].get(self.from_kind):
            self.ctx.toast("Choose what to read from.", error=True)
            return None
        if not d["to"]["table"]:
            self.f_table.error = "enter the target table"
            self.ctx.page.update()
            return None
        self.f_table.error = None
        return d

    def read_columns(self, quiet: bool = False) -> None:
        """Read the source header and the target table, then rebuild the grid (keeping the mapping)."""
        if quiet and not ((self.f_table.value or "").strip() and self.f_from.value and self.f_to.value):
            self._dirty()
            return
        d = self._draft_or_fail()
        if d is None:
            return
        self._last_target = (self.f_to.value, (self.f_table.value or "").strip())
        d["columns"], d["keys"], d["mode"] = [], [], "insert"     # the mapping is not needed to read columns
        current = [r.to_column() for r in self.rows if r.mapped]
        keys = [r.target for r in self.rows if r.key]

        def done(ins):
            self.inspection = ins
            if ins.target_error:
                self.ctx.toast(f"Cannot read the table: {ins.target_error}", error=True)
            elif ins.source_error:
                self.ctx.toast(f"Cannot read the source: {ins.source_error}", error=True)
            self.rows = mapping_rows(ins.targets, current, keys) if not ins.target_error else self.rows
            if self.original is None and not keys and any(r.primary_key for r in self.rows):
                for r in self.rows:             # suggest the table's primary key as the load key
                    r.key = r.primary_key
            self.render_grid(update=False)
        self.ctx.background(lambda: self.svc.inspect(d), done, self.busy)

    def auto_map(self, _=None) -> None:
        d = self._draft_or_fail()
        if d is None:
            return
        d["columns"], d["keys"], d["mode"] = [], [], "insert"

        def work():
            ins = self.inspection if self.inspection and not self.inspection.target_error else self.svc.inspect(d)
            return ins, self.svc.suggest(d, ins)

        def done(res):
            ins, sug = res
            if self.inspection is not ins:
                current = [r.to_column() for r in self.rows if r.mapped]
                self.inspection = ins
                self.rows = mapping_rows(ins.targets, current, [r.target for r in self.rows if r.key])
            n = apply_suggestion(self.rows, sug.columns)
            guesses = sum(1 for r in self.rows if r.review and r.mapped)
            left = unused_sources(ins.header, self.rows)
            if n == 0:
                msg = "Nothing new to fill – mapped lines are kept (press Clear first to redo them)"
            else:
                msg = f"Auto-map filled {n} line(s)" + (f", {guesses} are guesses to review" if guesses else "")
            if left:
                msg += f". Source columns still not loaded: {', '.join(left)}"
            self.ctx.toast(msg)
            self.render_grid(update=False)
        self.ctx.background(work, done, self.busy)

    def validate(self, _=None) -> None:
        d = self._draft_or_fail()
        if d is None:
            return

        def done(chk):
            self.check = chk
            self._show_check(chk.problems, chk.warnings,
                             f"Mapping is valid – {len(d['columns'])} columns mapped, the source has "
                             f"{chk.row_count} rows." if chk.ok else None)
        self.ctx.background(lambda: self.svc.validate(d), done, self.busy)

    def _show_check(self, problems: list[str], warnings: list[str], ok_text: str | None,
                    extra: list[ft.Control] | None = None) -> None:
        items: list[ft.Control] = []
        if ok_text:
            items.append(ft.Row([ft.Icon(ft.Icons.CHECK_CIRCLE, color=ft.Colors.GREEN_700),
                                 ft.Text(ok_text, expand=True)]))
        for p in problems:
            items.append(ft.Row([ft.Icon(ft.Icons.ERROR, color=ft.Colors.ERROR, size=18),
                                 ft.Text(p.replace("portwright load map", "the mapping grid:  portwright load map"),
                                         expand=True, size=13)]))
        for w in warnings:
            items.append(ft.Row([ft.Icon(ft.Icons.WARNING, color=ft.Colors.AMBER_800, size=18),
                                 ft.Text(w, expand=True, size=13)]))
        self.results.controls = items + (extra or [])

    def preview(self, _=None) -> None:
        d = self._draft_or_fail()
        if d is None:
            return

        def done(rep):
            self.dry = rep
            extra: list[ft.Control] = [ft.Row([LoadsView._count(k, v) for k, v in report_counts(rep.to_dict())],
                                              wrap=True, spacing=12)]
            if rep.rejects_file:
                extra.append(ft.OutlinedButton(f"View the {rep.rejected} rejected row(s)", icon=ft.Icons.RULE,
                                               on_click=lambda _: self.view.rejects(rep.run_id)))
            if rep.preview:
                src_cols: list[str] = []
                for p in rep.preview:
                    src_cols += [k for k in p["source"] if k not in src_cols]
                tgt_cols = [c["target"] for c in d["columns"]]
                extra += [ft.Text("Before – source rows", weight=ft.FontWeight.W_600),
                          ft.Row([table(["#", *src_cols], [[ft.Text(str(p["record"])),
                                                            *[ft.Text(cell(p["source"].get(c))) for c in src_cols]]
                                                           for p in rep.preview])], scroll=ft.ScrollMode.AUTO),
                          ft.Text("After – rows as they will be written", weight=ft.FontWeight.W_600),
                          ft.Row([table(["#", *tgt_cols], [[ft.Text(str(p["record"])),
                                                            *[ft.Text(cell(p["target"].get(c))) for c in tgt_cols]]
                                                           for p in rep.preview])], scroll=ft.ScrollMode.AUTO)]
            ok = None
            if rep.status == "dry_run":
                ok = (f"Dry run OK – {rep.mapped} of {rep.read} rows would be loaded"
                      + (f", {rep.rejected} rejected (allowed: {d['max_rejects']})" if rep.rejected else "") + ".")
            problems = list(rep.problems) or ([rep.error] if rep.error else [])
            self._show_check(problems, rep.warnings, ok, extra)
        self.ctx.background(lambda: self.svc.run(d, dry_run=True, preview=5), done, self.busy)

    def browse_tables(self, _=None) -> None:
        target = self.f_to.value
        if not target:
            self.ctx.toast("Choose the target SQL source first.", error=True)
            return

        def done(names):
            if not names:
                self.ctx.toast(f"No tables found in '{target}'.", error=True)
                return
            flt = text_field("Filter", autofocus=True)
            lst = ft.Column(spacing=0, height=380, scroll=ft.ScrollMode.AUTO)

            def pick(n):
                self.ctx.close()
                self.f_table.value = n
                self.f_table.error = None
                self.read_columns()

            def fill(_=None):
                q = (flt.value or "").lower()
                lst.controls = [ft.ListTile(title=ft.Text(n), dense=True, on_click=lambda _, n=n: pick(n),
                                            leading=ft.Icon(ft.Icons.TABLE_CHART, size=18))
                                for n in names if q in n.lower()][:300]
                self.ctx.page.update()
            flt.on_change = fill
            self.ctx.dialog(f"Tables in '{target}'", [flt, lst],
                            [ft.TextButton("Cancel", on_click=lambda _: self.ctx.close())], width=460)
            fill()
        self.ctx.background(lambda: self.ctx.svc.sources.tables(target), done, self.busy)

    def save(self, then=None) -> None:
        d = self._draft_or_fail(need_name=True)
        if d is None:
            return
        review = [r.target for r in self.rows if r.review and r.mapped]
        try:
            spec = self.svc.save(d, original=self.original)
        except ServiceError as exc:
            self.ctx.fail(exc)
            return
        renamed = self.original and self.original != spec.name
        self.original = spec.name
        self.saved_state = self._state()
        self.ctx.toast(f"Load '{spec.name}' saved" + (" (renamed)" if renamed else "")
                       + (f" – {len(review)} line(s) still marked for review; running is blocked until confirmed"
                          if review else ""))
        if then:
            then()
        else:
            self.view.refresh()

    def run(self, _=None) -> None:
        if self.is_dirty:
            self.ctx.confirm("Save changes first?", "The load runs the saved definition. Save your changes and run?",
                             lambda: self.save(then=lambda: self.view.run(self.original, after=self._after_run)),
                             danger=False, yes="Save and run")
            return
        self.view.run(self.original, after=self._after_run)

    def _after_run(self) -> None:
        self.view.refresh(update=False)

    def show_cli(self, _=None) -> None:
        from ..common import cli_dialog
        n = self.original or (self.f_name.value or "<load-name>")
        root = str(self.ctx.svc.project.root)
        cli_dialog(self.ctx, f"Load '{n}' from the command line", [
            ("Check and preview", f"cd {root}\nportwright load show {n}\nportwright load validate {n}\n"
                                  f"portwright load run {n} --dry-run --preview 3"),
            ("Run it (exit 0 loaded, 2 with rejects, 1 failed, 3 unchanged)",
             f"portwright load run {n}\nportwright load run {n} --force       # even if the source is unchanged"),
            ("Source versions", f"portwright load check {n}\nportwright load versions {n}\nportwright load history {n}"),
            ("Stonebranch / batch task",
             f"@echo off\ncd /d {root}\ncall C:\\apps\\portwright\\.venv\\Scripts\\portwright.exe load run {n} > NUL\n"
             f"set RC=%ERRORLEVEL%\nif %RC%==3 exit /b 0\nexit /b %RC%"),
        ], note="" if self.original and not self.is_dirty else "Save the load first – the CLI runs the saved definition.")

    def back(self, _=None) -> None:
        if self.is_dirty and (self.original or self.rows):
            self.ctx.confirm("Discard changes?", "Your unsaved changes to this load will be lost.",
                             self.view.close_editor, yes="Discard")
            return
        self.view.close_editor()
