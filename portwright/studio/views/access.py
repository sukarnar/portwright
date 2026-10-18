from __future__ import annotations

import asyncio

import flet as ft

from ...services import ServiceError
from ..common import Ctx, dropdown, empty, header, icon_btn, mono, table, tag, text_field, help_btn
from ..viewmodels import EXPIRY_CHOICES

CLIPBOARD_CLEAR_SECONDS = 60


class AccessView:
    title = "API keys & users"

    def __init__(self, ctx: Ctx):
        self.ctx = ctx
        self.mode = "keys"
        self.root = ft.Column(expand=True, scroll=ft.ScrollMode.AUTO, spacing=16)

    def build(self) -> ft.Control:
        self.refresh(update=False)
        return self.root

    def refresh(self, update: bool = True) -> None:
        switch = ft.SegmentedButton(
            selected=[self.mode], allow_multiple_selection=False,
            segments=[ft.Segment(value="keys", label=ft.Text("API keys"), icon=ft.Icon(ft.Icons.KEY)),
                      ft.Segment(value="users", label=ft.Text("Users"), icon=ft.Icon(ft.Icons.PEOPLE))],
            on_change=self._switch)
        action = (ft.FilledButton("Create API key", icon=ft.Icons.ADD, on_click=lambda _: self.key_dialog())
                  if self.mode == "keys" else
                  ft.FilledButton("Add user", icon=ft.Icons.ADD, on_click=lambda _: self.user_dialog()))
        body = self._keys() if self.mode == "keys" else self._users()
        self.root.controls = [
            header("API keys & users", "Who may call your endpoints. Changes apply after a server restart.", [help_btn(self.ctx, "security"), action]),
            switch, body]
        if update:
            self.ctx.page.update()

    def _switch(self, e) -> None:
        sel = e.control.selected
        self.mode = (list(sel)[0] if sel else "keys")
        self.refresh()

    # ---------------------------------------------------------------- keys
    def _keys(self) -> ft.Control:
        keys = self.ctx.svc.access.keys()
        if not keys:
            return empty("No API keys yet. Create one per calling application.")
        rows = []
        for k in keys:
            status = {"active": "good", "revoked": "neutral", "expired": "bad"}[k["status"]]
            exp = "never" if not k["expires"] else (f"{k['days_left']} days" if k["days_left"] is not None else "")
            rows.append([
                ft.Text(k["name"], weight=ft.FontWeight.W_600), mono(k["id"]),
                ft.Row([tag(s, "info") for s in k["scopes"]] or [ft.Text("–")], wrap=True, spacing=4, width=260),
                tag(k["status"], status),
                tag(exp, "warn" if k["expiry_state"] == "soon" and k["status"] == "active" else "neutral"),
                icon_btn(ft.Icons.BLOCK, "Revoke", lambda _, i=k["id"], n=k["name"]: self.revoke(i, n), ft.Colors.ERROR)
                if k["status"] == "active" else ft.Text(""),
            ])
        return table(["Name", "Id", "Scopes", "Status", "Expires in", ""], rows)

    def key_dialog(self, preset: list[str] | None = None) -> None:
        name = text_field("Name *", hint="the application that will use it, e.g. reporting-app")
        boxes = [ft.Checkbox(label=s, value=s in (preset or [])) for s in self.ctx.svc.access.known_scopes()]
        expires = dropdown("Expires after", list(EXPIRY_CHOICES), "90 days")
        if len(boxes) == 1:
            scope_area: ft.Control = ft.Text("No endpoint scopes yet – create endpoints first, or tick * for admin.",
                                             color=ft.Colors.ON_SURFACE_VARIANT)
        else:
            scope_area = ft.Text("Tick only what this application needs. * = admin (everything).", size=12,
                                 color=ft.Colors.ON_SURFACE_VARIANT)

        def create(_):
            scopes = [b.label for b in boxes if b.value]
            if not scopes:
                self.ctx.toast("Tick at least one scope", error=True)
                return
            try:
                k = self.ctx.svc.access.create_key(name.value or "", scopes, EXPIRY_CHOICES[expires.value] or None)
            except ServiceError as exc:
                self.ctx.fail(exc)
                return
            self.ctx.close()
            self.refresh()
            self.show_once(k)

        self.ctx.dialog("Create API key", [name, ft.Text("Scopes", weight=ft.FontWeight.W_600), scope_area,
                                           ft.Row(boxes, wrap=True), expires],
                        [ft.TextButton("Cancel", on_click=lambda _: self.ctx.close()),
                         ft.FilledButton("Create", on_click=create)])

    def show_once(self, k) -> None:
        async def copy(_):
            await ft.Clipboard().set(k.plaintext)
            self.ctx.toast(f"Copied – the clipboard is cleared in {CLIPBOARD_CLEAR_SECONDS} seconds")

            async def clear_later():
                await asyncio.sleep(CLIPBOARD_CLEAR_SECONDS)
                try:
                    if await ft.Clipboard().get() == k.plaintext:
                        await ft.Clipboard().set("")
                except Exception:
                    pass
            self.ctx.page.run_task(clear_later)

        self.ctx.dialog("Copy this key now", [
            ft.Container(ft.Row([ft.Icon(ft.Icons.WARNING, color=ft.Colors.AMBER_800),
                                 ft.Text("This is the only time the key is shown. portwright stores only a hash of it.",
                                         expand=True)]),
                         padding=12, border_radius=8, bgcolor=ft.Colors.AMBER_50),
            ft.Container(mono(k.plaintext, 14), padding=12, border_radius=8,
                         bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST),
            ft.Text(f"Send it in the '{k.header}' header. Scopes: {', '.join(k.scopes)}. "
                    f"Expires: {k.expires[:10] if k.expires else 'never'}.", color=ft.Colors.ON_SURFACE_VARIANT),
            ft.Text("Restart the server so it accepts the new key.", color=ft.Colors.ON_SURFACE_VARIANT),
        ], [ft.OutlinedButton("Copy key", icon=ft.Icons.CONTENT_COPY, on_click=copy),
            ft.FilledButton("I have stored it", on_click=lambda _: self.ctx.close())])

    def revoke(self, key_id: str, name: str) -> None:
        def go():
            self.ctx.svc.access.revoke_key(key_id)
            self.ctx.toast(f"Key '{name}' revoked – restart the server to apply")
            self.refresh()
        self.ctx.confirm("Revoke API key?", f"Applications using '{name}' ({key_id}) will be refused after the "
                                            "next server restart.", go, yes="Revoke")

    # --------------------------------------------------------------- users
    def _users(self) -> ft.Control:
        users = self.ctx.svc.access.users()
        if not users:
            return empty("No users. Users log in with a password for a short-lived token (POST /api/auth/token).")
        rows = [[ft.Text(u["username"], weight=ft.FontWeight.W_600),
                 ft.Row([tag(s, "info") for s in u["scopes"]] or [ft.Text("–")], wrap=True, spacing=4),
                 tag("disabled" if u["disabled"] else "active", "neutral" if u["disabled"] else "good"),
                 icon_btn(ft.Icons.DELETE, "Remove", lambda _, n=u["username"]: self.remove_user(n), ft.Colors.ERROR)]
                for u in users]
        return table(["User", "Scopes", "Status", ""], rows)

    def user_dialog(self) -> None:
        username = text_field("Username *")
        pw1 = text_field("Password * (min 12 characters)", secret=True)
        pw2 = text_field("Repeat password *", secret=True)
        boxes = [ft.Checkbox(label=s, value=False) for s in self.ctx.svc.access.known_scopes()]

        def save(_):
            pw2.error = None
            if pw1.value != pw2.value:
                pw2.error = "passwords do not match"
                self.ctx.page.update()
                return
            try:
                self.ctx.svc.access.add_user((username.value or "").strip(), pw1.value or "",
                                             [b.label for b in boxes if b.value])
            except ServiceError as exc:
                self.ctx.fail(exc)
                return
            self.ctx.close()
            self.ctx.toast(f"User '{username.value}' added – restart the server to apply")
            self.refresh()

        self.ctx.dialog("Add user", [username, pw1, pw2, ft.Text("Scopes", weight=ft.FontWeight.W_600),
                                     ft.Row(boxes, wrap=True)],
                        [ft.TextButton("Cancel", on_click=lambda _: self.ctx.close()),
                         ft.FilledButton("Add", on_click=save)])

    def remove_user(self, name: str) -> None:
        def go():
            self.ctx.svc.access.remove_user(name)
            self.ctx.toast(f"User '{name}' removed")
            self.refresh()
        self.ctx.confirm("Remove user?", f"'{name}' will no longer be able to log in.", go, yes="Remove")
