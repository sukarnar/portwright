"""Activity: every setup change (who, when, how) with its pw command; replay scripts and migration bundles."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import flet as ft

from ...services import ServiceError, load_vars
from ...services.activity import render_steps
from ..common import Ctx, cli_dialog, dropdown, empty, header, help_btn, icon_btn, table, tag, text_field

KINDS = {"": "All changes", "source": "Sources", "endpoint": "Endpoints", "load": "Loads",
         "credential": "Credentials", "settings": "Settings", "api_key": "API keys", "user": "Users",
         "project": "Project / migrations"}
VIA = {"": "Anyone / anything", "studio": "Studio", "cli": "Command line", "file": "File edited by hand",
       "migrate": "Migration import"}
ACTION_KIND = {"create": "good", "update": "info", "delete": "bad"}
LIMIT = 300


def local_time(ts: str) -> str:
    try:
        return datetime.fromisoformat(ts).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return ts[:16].replace("T", " ")


def filter_entries(entries: list[dict], kind: str = "", via: str = "", text: str = "") -> list[dict]:
    out = []
    t = (text or "").strip().lower()
    for e in entries:
        if kind and e.get("kind") != kind:
            continue
        if via and not str(e.get("via", "")).startswith(via):
            continue
        if t and t not in (e.get("summary", "") + " " + e.get("user", "") + " " + e.get("name", "")).lower():
            continue
        out.append(e)
    return out


class ActivityView:
    def __init__(self, ctx: Ctx):
        self.ctx = ctx
        self.root = ft.Column(expand=True, scroll=ft.ScrollMode.AUTO, spacing=16)
        self.f_kind = dropdown("Show", KINDS, "", width=220, on_select=lambda _: self.refresh())
        self.f_via = dropdown("Made with", VIA, "", width=220, on_select=lambda _: self.refresh())
        self.f_since = text_field("Since (date)", "", hint="e.g. 2026-10-01", width=170,
                                  on_submit=lambda _: self.refresh())
        self.f_text = text_field("Search", "", width=240, on_submit=lambda _: self.refresh())

    def build(self) -> ft.Control:
        self.refresh(update=False)
        return self.root

    # ------------------------------------------------------------ list
    def refresh(self, update: bool = True) -> None:
        log = self.ctx.svc.project.activity
        since = (self.f_since.value or "").strip() or None
        entries = filter_entries(log.read(since=since), self.f_kind.value or "", self.f_via.value or "",
                                 self.f_text.value or "")
        newest = list(reversed(entries))[:LIMIT]
        controls: list[ft.Control] = [
            header("Activity", "Every setup change – who made it, when, with Studio, the CLI or a file edit – "
                               "and the pw command that does the same on another instance",
                   [help_btn(self.ctx, "migration"),
                    ft.OutlinedButton("Check / import bundle", icon=ft.Icons.UNARCHIVE, on_click=self.plan_dialog),
                    ft.OutlinedButton("Replay script", icon=ft.Icons.TERMINAL, on_click=self.script_dialog),
                    ft.FilledButton("Export migration bundle", icon=ft.Icons.ARCHIVE, on_click=self.export_dialog)]),
            ft.Row([self.f_kind, self.f_via, self.f_since, self.f_text,
                    ft.IconButton(ft.Icons.SEARCH, tooltip="Apply filters", on_click=lambda _: self.refresh())],
                   wrap=True, spacing=12, vertical_alignment=ft.CrossAxisAlignment.START)]
        if not newest:
            controls.append(empty("No changes recorded for this filter." if entries or since or self.f_kind.value
                                  else "No changes recorded yet – every change you make from now on appears here."))
        else:
            rows = []
            for e in newest:
                via = str(e.get("via", ""))
                rows.append([
                    ft.Text(local_time(e["ts"]), size=13),
                    ft.Text(e.get("user", ""), size=13),
                    tag(via.split(":")[0], "warn" if via == "file" else "info" if via == "studio" else "neutral"),
                    ft.Row([tag(e["action"], ACTION_KIND.get(e["action"], "neutral")),
                            ft.Text(e["summary"], size=13, expand=True,
                                    tooltip=e.get("note") or (via if ":" in via else None))], spacing=6, width=560),
                    icon_btn(ft.Icons.TERMINAL, "The pw command for this change", lambda _, e=e: self.show(e))
                    if e.get("steps") else ft.Text("")])
            controls.append(table(["When", "User", "Via", "Change", ""], rows))
            if len(entries) > LIMIT:
                controls.append(ft.Text(f"Showing the newest {LIMIT} of {len(entries)}. Narrow the filter, or use "
                                        f"'pw activity --last 0' for all.", size=12,
                                        color=ft.Colors.ON_SURFACE_VARIANT))
        self.root.controls = controls
        if update:
            self.ctx.page.update()

    def show(self, e: dict) -> None:
        note = f"{local_time(e['ts'])} · {e.get('user')} on {e.get('host')} · via {e.get('via')}"
        if e.get("note"):
            note += f" · {e['note']}"
        cli_dialog(self.ctx, e["summary"], [("Same change on another instance", render_steps(e["steps"]))],
                   note=note)

    # ------------------------------------------------------------ replay script
    def script_dialog(self, _=None) -> None:
        since = text_field("Changes since (date or time, empty = all logged changes)", self.f_since.value or "",
                           hint="e.g. 2026-10-01")
        full = ft.Checkbox(label="The whole current setup instead (ignores the log)", value=False)
        out = ft.Column(spacing=10)

        def build(_=None):
            try:
                text, n = self.ctx.svc.migration.script(since=(since.value or "").strip() or None,
                                                        full=bool(full.value))
            except ServiceError as exc:
                self.ctx.fail(exc)
                return None
            return text, n

        def preview(_=None):
            res = build()
            if res is None:
                return
            text, n = res
            from .help import code_block
            out.controls = [ft.Text(f"{n} logged change(s) → script below.", size=13), code_block(text, "powershell",
                                                                                                  self.ctx)]
            self.ctx.page.update()

        def save(_=None):
            res = build()
            if res is None:
                return
            text, n = res
            d = self.ctx.svc.project.root / "migrations"
            d.mkdir(exist_ok=True)
            path = d / f"setup-{datetime.now().strftime('%Y%m%d-%H%M%S')}.ps1"
            path.write_text(text.replace("\n", "\r\n"), encoding="utf-8-sig", newline="")
            self.ctx.close()
            cli_dialog(self.ctx, "Replay script saved", [
                ("Saved in the project", str(path)),
                ("Run it on the target instance", f"Set-ExecutionPolicy -Scope Process Bypass\n"
                                                  f".\\{path.name} -Config C:\\apps\\hr-api\\portwright.yaml "
                                                  f"-Vars prod.vars.yaml"),
                ("Or from the command line here", "pw migrate script -o setup.ps1"
                                                  + (f" --since {since.value.strip()}" if since.value else "")
                                                  + (" --full" if full.value else ""))],
                note="Secrets are never in the script: 'pw cred add' asks for them on the target. API keys and "
                     "users are listed as comments.")
        self.ctx.dialog("Replay script", [
            ft.Text("A PowerShell script of pw commands that makes the same changes on another instance. "
                    "Values that differ there (servers, ports, credential names) come from a variables file "
                    "(-Vars).", size=13),
            since, full, out],
            [ft.TextButton("Cancel", on_click=lambda _: self.ctx.close()),
             ft.OutlinedButton("Preview", icon=ft.Icons.PREVIEW, on_click=preview),
             ft.FilledButton("Save script", icon=ft.Icons.SAVE, on_click=save)], width=820)

    # ------------------------------------------------------------ export
    def export_dialog(self, _=None) -> None:
        data = ft.Checkbox(label="Include the files in the data folder", value=False)
        handlers = ft.Checkbox(label="Include handlers\\*.py (Python functions)", value=True)
        bar = ft.ProgressBar(visible=False)

        def go(_):
            def done(res):
                path, m = res
                self.ctx.close()
                c = m["counts"]
                cli_dialog(self.ctx, "Migration bundle exported", [
                    ("File", str(path)),
                    ("On the target: make a variables file from the template, then check and import",
                     f"pw migrate vars {path.name} > prod.vars.yaml      # edit the values for the target\n"
                     f"pw migrate plan {path.name} --vars prod.vars.yaml -c C:\\apps\\hr-api\\portwright.yaml\n"
                     f"pw migrate import {path.name} --vars prod.vars.yaml -c C:\\apps\\hr-api\\portwright.yaml"),
                ], note=f"{c['sources']} sources, {c['endpoints']} endpoints, {c['loads']} loads, "
                        f"{c['credentials']} credentials (names and non-secret fields only), {c['handlers']} handler "
                        f"file(s), {c['data_files']} data file(s). Not included: secrets, API keys, users, logs."
                        + (" Warnings: " + "; ".join(m["warnings"]) if m["warnings"] else ""))
                self.refresh()
            self.ctx.background(lambda: self.ctx.svc.migration.export(
                include_data=bool(data.value), include_handlers=bool(handlers.value)), done, bar)
        self.ctx.dialog("Export migration bundle", [
            ft.Text("One zip with the whole setup – sources, endpoints, loads, settings, credential names – for "
                    "another instance (test, prod). It is saved in the project's migrations folder.", size=13),
            ft.Text("Never included: passwords and tokens, API keys, users, logs and run history.", size=13,
                    weight=ft.FontWeight.W_600),
            handlers, data, bar],
            [ft.TextButton("Cancel", on_click=lambda _: self.ctx.close()),
             ft.FilledButton("Export", icon=ft.Icons.ARCHIVE, on_click=go)], width=620, scroll=False)

    # ------------------------------------------------------------ plan / import
    def plan_dialog(self, _=None) -> None:
        bundle = text_field("Migration bundle (.zip)", "", hint=r"e.g. C:\install\hr-api-20261019.zip", expand=True)
        vars_f = text_field("Variables file (optional)", "", hint=r"e.g. C:\install\prod.vars.yaml", expand=True)
        prune = ft.Checkbox(label="Also remove sources, endpoints and loads that are not in the bundle", value=False)
        skip = ft.Checkbox(label="Keep this instance's server and security settings", value=False)
        out = ft.Column(spacing=8)
        bar = ft.ProgressBar(visible=False)
        state: dict = {}

        def args():
            b = (bundle.value or "").strip().strip('"')
            if not b:
                raise ServiceError("enter the path of the bundle")
            v = (vars_f.value or "").strip().strip('"')
            return Path(b), load_vars(v) if v else {}

        def check(_=None):
            try:
                b, variables = args()
            except ServiceError as exc:
                self.ctx.fail(exc)
                return

            def done(plan):
                state["plan"] = plan
                out.controls = plan_controls(plan)
                apply_btn.disabled = not (plan.changes or any(f["status"] in ("new", "changed") for f in plan.files))
                self.ctx.page.update()
            self.ctx.background(lambda: self.ctx.svc.migration.plan(b, variables, prune=bool(prune.value),
                                                                    skip_settings=bool(skip.value)), done, bar)

        def do_import(_=None):
            try:
                b, variables = args()
            except ServiceError as exc:
                self.ctx.fail(exc)
                return

            def done(res):
                self.ctx.close()
                miss = [c for c in res.missing_credentials if c.get("used", True)]
                cmds = "\n".join(f"pw cred add {c['name']} -t {c['type']} "
                                 + " ".join(f"-f {k}={v}" for k, v in c["fields"].items()) for c in miss)
                cli_dialog(self.ctx, "Bundle imported", [
                    ("", f"{res.applied} change(s), {len(res.files_written)} file(s) written."
                         + (f"\nThe previous setup was saved as {res.backup}" if res.backup else "")),
                    *([("Add the missing credentials (Credentials screen, or)", cmds)] if miss else []),
                    ("Then", "pw validate\n# restart the Portwright service")])
                self.refresh()

            def go():
                self.ctx.background(lambda: self.ctx.svc.migration.apply(
                    b, variables, prune=bool(prune.value), skip_settings=bool(skip.value)), done, bar)
            n = len(state["plan"].changes) if state.get("plan") else 0
            self.ctx.confirm("Import the bundle?", f"{n} change(s) will be made to this project's setup. "
                             "portwright.yaml is backed up to the backups folder first.", go, danger=bool(prune.value),
                             yes="Import")

        apply_btn = ft.FilledButton("Import", icon=ft.Icons.UNARCHIVE, on_click=do_import, disabled=True)
        self.ctx.dialog("Check or import a migration bundle", [
            ft.Text("Check shows what would change on THIS instance – nothing is written until you press Import.",
                    size=13),
            bundle, vars_f, prune, skip, bar, out],
            [ft.TextButton("Close", on_click=lambda _: self.ctx.close()),
             ft.OutlinedButton("Check", icon=ft.Icons.FACT_CHECK, on_click=check), apply_btn], width=860)


def plan_controls(plan) -> list[ft.Control]:
    m = plan.manifest
    kind = {"create": "good", "update": "warn", "remove": "bad", "unchanged": "neutral", "only-on-target": "neutral"}
    rows = [[ft.Text(i.kind), ft.Text(i.name, weight=ft.FontWeight.W_600), tag(i.action, kind[i.action]),
             ft.Text(", ".join(i.fields[:6]) if i.action == "update" else "", size=12),
             ft.Text(", ".join(v.split(".", 2)[-1] for v in i.vars_applied), size=12)] for i in plan.items]
    rows += [[ft.Text("credential"), ft.Text(c["name"], weight=ft.FontWeight.W_600),
              tag("present" if c["status"] == "present" else "missing",
                  "neutral" if c["status"] == "present" else "warn"),
              ft.Text("secrets are never imported" if c["status"] == "present"
                      else "add it after the import", size=12), ft.Text("")] for c in plan.credentials]
    rows += [[ft.Text("file"), ft.Text(f["path"]), tag(f["status"], "good" if f["status"] == "new" else
                                                       "warn" if f["status"] == "changed" else "neutral"),
              ft.Text(""), ft.Text("")] for f in plan.files if f["status"] != "same"]
    out: list[ft.Control] = [
        ft.Text(f"'{m.get('project')}' exported {local_time(m.get('exported_at', ''))} by {m.get('exported_by')} on "
                f"{m.get('host')} (Portwright {m.get('portwright')})", size=13),
        ft.Text(f"{len(plan.changes)} change(s) to the setup.", weight=ft.FontWeight.W_600),
        ft.Row([table(["Kind", "Name", "Action", "Changed", "Variables"], rows)], scroll=ft.ScrollMode.AUTO)]
    for w in plan.warnings:
        out.append(ft.Row([ft.Icon(ft.Icons.WARNING, color=ft.Colors.AMBER_800, size=18),
                           ft.Text(w, size=13, expand=True)]))
    return out
