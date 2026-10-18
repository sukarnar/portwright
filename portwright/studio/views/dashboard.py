from __future__ import annotations

import flet as ft

from ..common import Ctx, header, tag, help_btn
from ..viewmodels import dashboard, project_state, set_project_state, setup_steps


class DashboardView:
    title = "Dashboard"

    def __init__(self, ctx: Ctx):
        self.ctx = ctx
        self.root = ft.Column(expand=True, scroll=ft.ScrollMode.AUTO, spacing=20)
        self.validation = ft.Column(spacing=6)

    def build(self) -> ft.Control:
        self.refresh(update=False)
        return self.root

    def _card(self, label: str, value, note: str, screen: str, icon) -> ft.Control:
        return ft.Container(
            width=220, padding=16, border_radius=12, border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
            on_click=lambda _: self.ctx.go(screen), ink=True,
            content=ft.Column([
                ft.Row([ft.Icon(icon, color=ft.Colors.PRIMARY), ft.Text(label, color=ft.Colors.ON_SURFACE_VARIANT)]),
                ft.Text(str(value), size=32, weight=ft.FontWeight.W_600),
                ft.Text(note, size=12, color=ft.Colors.ON_SURFACE_VARIANT),
            ], spacing=4))

    def refresh(self, update: bool = True) -> None:
        d = dashboard(self.ctx.svc)
        attention: list[ft.Control] = []
        if not d["vault"]["key_present"]:
            attention.append(self._alert("bad", f"Credential vault is locked: {d['vault']['key_env']} is not set in .env. "
                                                 "Open Credentials to create a master key."))
        if d["cred_error"]:
            attention.append(self._alert("bad", d["cred_error"]))
        for c in d["expiring"]:
            word = "expired" if c["expiry_state"] == "expired" else f"expires in {c['days_left']} days"
            attention.append(self._alert("warn" if c["expiry_state"] == "soon" else "bad",
                                         f"Credential '{c['name']}' {word} – rotate it on the Credentials screen."))
        for k in d["keys_expiring"]:
            attention.append(self._alert("warn", f"API key '{k['name']}' expires in {k['days_left']} days."))
        if d["public_endpoints"]:
            attention.append(self._alert("info", "Public endpoints (no key needed): " + ", ".join(d["public_endpoints"])))

        runs = [ft.Row([ft.Text(r["started"][:16].replace("T", " "), width=130),
                        ft.Text(r["load"], width=200),
                        tag(r["status"], {"success": "good", "partial": "warn", "failed": "bad"}.get(r["status"], "neutral")),
                        ft.Text(f"read {r['read']} · written {r['inserted'] + r['updated'] + r['upserted']} · "
                                f"rejected {r['rejected']}", color=ft.Colors.ON_SURFACE_VARIANT)])
                for r in d["runs"]]

        self.root.controls = [
            header(d["project"], d["path"], [help_btn(self.ctx, "getting-started"),
                ft.OutlinedButton("Validate project", icon=ft.Icons.FACT_CHECK, on_click=self._validate)]),
            self.validation,
            *self._checklist(),
            ft.Row([
                self._card("Sources", d["sources"], "databases, files, APIs", "sources", ft.Icons.STORAGE),
                self._card("Endpoints", d["endpoints"], f"{len(d['public_endpoints'])} public", "endpoints", ft.Icons.API),
                self._card("Credentials", d["credentials"], f"{len(d['expiring'])} need attention", "credentials",
                           ft.Icons.LOCK),
                self._card("Loads", d["loads"], "file or endpoint to table", "loads", ft.Icons.MOVE_DOWN),
                self._card("API keys", d["keys_active"], "active", "access", ft.Icons.BADGE),
            ], wrap=True, spacing=16, run_spacing=16),
            ft.Text("Needs attention", size=16, weight=ft.FontWeight.W_600),
            ft.Column(attention or [ft.Text("Nothing – all good.", color=ft.Colors.ON_SURFACE_VARIANT)], spacing=8),
            ft.Text("Recent load runs", size=16, weight=ft.FontWeight.W_600),
            ft.Column(runs or [ft.Text("No load runs yet.", color=ft.Colors.ON_SURFACE_VARIANT)], spacing=6),
        ]
        if update:
            self.ctx.page.update()

    def _checklist(self) -> list[ft.Control]:
        steps = setup_steps(self.ctx.svc)
        done = sum(s.done for s in steps)
        if project_state(self.ctx.svc).get("hide_checklist") and done == len(steps):
            return []
        nxt = next((s for s in steps if not s.done), None)
        rows = []
        for i, s in enumerate(steps, start=1):
            is_next = s is nxt
            rows.append(ft.Container(
                padding=ft.Padding.symmetric(horizontal=12, vertical=8), border_radius=8,
                bgcolor=ft.Colors.PRIMARY_CONTAINER if is_next else None, ink=True,
                on_click=lambda _, sc=s.screen, k=s.key: self._open_step(sc, k),
                content=ft.Row([
                    ft.Icon(ft.Icons.CHECK_CIRCLE if s.done else ft.Icons.RADIO_BUTTON_UNCHECKED,
                            color=ft.Colors.GREEN_700 if s.done else ft.Colors.OUTLINE, size=22),
                    ft.Column([ft.Text(f"{i}. {s.title}", weight=ft.FontWeight.W_600 if is_next else None),
                               ft.Text(s.hint, size=12, color=ft.Colors.ON_SURFACE_VARIANT)], spacing=0, expand=True),
                    ft.Icon(ft.Icons.CHEVRON_RIGHT, color=ft.Colors.ON_SURFACE_VARIANT)])))
        actions = []
        if done == len(steps):
            actions.append(ft.TextButton("Hide", on_click=self._hide_checklist))
        return [ft.Container(
            padding=16, border_radius=12, border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
            content=ft.Column([
                ft.Row([ft.Text("Getting started", size=16, weight=ft.FontWeight.W_600),
                        tag(f"{done} of {len(steps)} done", "good" if done == len(steps) else "info"),
                        ft.Container(expand=True), *actions]),
                ft.Column(rows, spacing=2)], spacing=10))]

    def _open_step(self, screen: str, key: str) -> None:
        if key == "serve":
            self.ctx.help("server", "cli")
        elif key == "try":
            self.ctx.go("endpoints")
            eps = self.ctx.svc.endpoints.list()
            view = self.ctx.view("endpoints")
            if eps and view is not None:
                view.try_dialog(eps[0]["name"])
        else:
            self.ctx.go(screen)

    def _hide_checklist(self, _=None) -> None:
        set_project_state(self.ctx.svc, hide_checklist=True)
        self.refresh()

    @staticmethod
    def _alert(kind: str, text: str) -> ft.Control:
        icon = {"bad": ft.Icons.ERROR, "warn": ft.Icons.WARNING, "info": ft.Icons.INFO}[kind]
        color = {"bad": ft.Colors.ERROR, "warn": ft.Colors.AMBER_800, "info": ft.Colors.PRIMARY}[kind]
        return ft.Row([ft.Icon(icon, color=color, size=20), ft.Text(text, expand=True)])

    def _validate(self, _=None) -> None:
        busy = ft.ProgressRing(width=18, height=18)
        self.validation.controls = [ft.Row([busy, ft.Text("Validating…")])]
        self.ctx.page.update()

        def done(v):
            items = [self._alert("info" if v.ok else "bad", v.message)]
            items += [self._alert("warn", w) for w in v.warnings]
            self.validation.controls = items
        self.ctx.background(self.ctx.svc.runtime.validate, done)
