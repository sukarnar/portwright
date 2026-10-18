from __future__ import annotations

import flet as ft

from ...services import ServiceError
from ..common import Ctx, dropdown, empty, header, icon_btn, table, tag, text_field, help_btn
from ..viewmodels import DB_DRIVERS, EXPIRY_CHOICES, credential_errors, credential_fields

TYPE_LABELS = {"database": "Database login", "basic": "HTTP Basic (user + password)", "bearer": "Bearer token",
               "api_key": "API key", "oauth2": "OAuth2 client credentials", "generic": "Generic secret values"}


class CredentialsView:
    title = "Credentials"

    def __init__(self, ctx: Ctx):
        self.ctx = ctx
        self.root = ft.Column(expand=True, scroll=ft.ScrollMode.AUTO, spacing=16)

    def build(self) -> ft.Control:
        self.refresh(update=False)
        return self.root

    def refresh(self, update: bool = True) -> None:
        svc = self.ctx.svc.credentials
        vault = svc.vault_status()
        controls: list[ft.Control] = [header(
            "Credentials", "Logins and tokens portwright uses to reach your databases and APIs – stored encrypted",
            [help_btn(self.ctx, "credentials"), ft.FilledButton("Add credential", icon=ft.Icons.ADD, on_click=lambda _: self.add_dialog(),
                             disabled=not vault["key_present"])])]
        if not vault["key_present"]:
            controls.append(ft.Container(
                padding=16, border_radius=10, bgcolor=ft.Colors.ERROR_CONTAINER,
                content=ft.Row([ft.Icon(ft.Icons.LOCK, color=ft.Colors.ON_ERROR_CONTAINER),
                                ft.Text(f"The vault is locked: {vault['key_env']} is not in this project's .env.",
                                        expand=True, color=ft.Colors.ON_ERROR_CONTAINER),
                                ft.FilledButton("Create master key", on_click=self._init_vault)])))
        try:
            creds = svc.list()
        except ServiceError as exc:
            controls.append(ft.Text(str(exc), color=ft.Colors.ERROR))
            creds = []
        if not creds:
            controls.append(empty("No credentials yet.", ft.FilledButton(
                "Add your first credential", on_click=lambda _: self.add_dialog(), disabled=not vault["key_present"])))
        else:
            rows = []
            for c in creds:
                exp = {"never": tag("no expiry"), "ok": tag(f"{c['days_left']} days", "good"),
                       "soon": tag(f"{c['days_left']} days", "warn"), "expired": tag("EXPIRED", "bad")}[c["expiry_state"]]
                rows.append([
                    ft.Text(c["name"], weight=ft.FontWeight.W_600),
                    ft.Text(TYPE_LABELS.get(c["type"], c["type"])),
                    ft.Text(c["provider"]), exp,
                    ft.Text(", ".join(c["used_by"]) or "–", color=ft.Colors.ON_SURFACE_VARIANT),
                    ft.Row([icon_btn(ft.Icons.PLAY_ARROW, "Test", lambda _, n=c["name"]: self.test(n)),
                            icon_btn(ft.Icons.SYNC, "Rotate / edit", lambda _, n=c["name"]: self.rotate_dialog(n)),
                            icon_btn(ft.Icons.DELETE, "Delete", lambda _, n=c["name"]: self.delete(n),
                                     ft.Colors.ERROR)], spacing=0),
                ])
            controls.append(table(["Name", "Type", "Stored in", "Expiry", "Used by", ""], rows))
        self.root.controls = controls
        if update:
            self.ctx.page.update()

    # ----------------------------------------------------------------- add
    def add_dialog(self) -> None:
        name = text_field("Name", hint="e.g. hr_oracle – sources refer to it by this name")
        ctype = dropdown("Type", TYPE_LABELS, "database")
        expires = dropdown("Rotate after", list(EXPIRY_CHOICES), "Never")
        desc = text_field("Description (optional)")
        fields_box = ft.Column(spacing=10, horizontal_alignment=ft.CrossAxisAlignment.STRETCH)
        inputs: dict[str, ft.TextField] = {}
        generic_box = ft.Column(spacing=10, horizontal_alignment=ft.CrossAxisAlignment.STRETCH)

        def render_fields(_=None):
            keep = {k: c.value for k, c in inputs.items()}          # keep what was typed when switching driver
            driver = keep.get("driver") or (DB_DRIVERS[0] if ctype.value == "database" else None)
            inputs.clear()
            fields_box.controls = []
            for f in credential_fields(ctype.value, driver):
                if f.options:
                    ctl = dropdown(f.label + (" *" if f.required else ""), f.options, driver or f.options[0],
                                   on_select=render_fields)
                else:
                    ctl = text_field(f.label + (" *" if f.required else ""), value=keep.get(f.name) or "",
                                     hint=f.hint, secret=f.secret)
                inputs[f.name] = ctl
                fields_box.controls.append(ctl)
            generic_box.visible = ctype.value == "generic"
            self.ctx.page.update()

        generic_lines = text_field("Values (one per line: name=value)", multiline=True, min_lines=3,
                                   hint="All values are encrypted. Use them as ${CRED:<name>.<field>}")
        generic_box.controls = [generic_lines]
        ctype.on_select = render_fields
        render_fields_initial = True

        def save(_):
            values = {k: (c.value or "").strip() for k, c in inputs.items()}
            for k, c in inputs.items():
                c.error = None
            if ctype.value == "generic":
                from ..viewmodels import parse_kv_lines
                try:
                    values = parse_kv_lines(generic_lines.value)
                except ServiceError as exc:
                    generic_lines.error = str(exc)
                    self.ctx.page.update()
                    return
                if not values:
                    generic_lines.error = "add at least one name=value line"
                    self.ctx.page.update()
                    return
            errs = credential_errors(ctype.value, values)
            for k, msg in errs.items():
                if k in inputs:
                    inputs[k].error = msg
            if errs:
                self.ctx.page.update()
                return

            def done(where):
                self.ctx.close()
                self.ctx.toast(f"Credential '{name.value}' saved in the {where}")
                self.refresh()
            days = EXPIRY_CHOICES[expires.value] or None
            self.ctx.background(lambda: self.ctx.svc.credentials.add(
                (name.value or "").strip(), ctype.value, values, expires_days=days, description=desc.value or "",
                secret_fields=list(values) if ctype.value == "generic" else None), done)

        self.ctx.dialog("Add credential", [
            name, ctype, fields_box, generic_box, ft.Row([expires], wrap=True), desc,
            ft.Text("Secrets are encrypted in this project's vault and never shown again.",
                    size=12, color=ft.Colors.ON_SURFACE_VARIANT)],
            [ft.TextButton("Cancel", on_click=lambda _: self.ctx.close()),
             ft.FilledButton("Save", on_click=save)])
        if render_fields_initial:
            render_fields()

    # -------------------------------------------------------------- rotate
    def rotate_dialog(self, name: str) -> None:
        svc = self.ctx.svc.credentials
        try:
            cred = svc.get(name)
        except ServiceError as exc:
            self.ctx.fail(exc)
            return
        defs = credential_fields(cred.type, cred.fields.get("driver")) if cred.type != "generic" else []
        inputs: dict[str, ft.TextField] = {}
        body: list[ft.Control] = [ft.Text(f"{TYPE_LABELS.get(cred.type, cred.type)} · stored in {cred.provider}",
                                          color=ft.Colors.ON_SURFACE_VARIANT)]
        if cred.type == "generic":
            for k in sorted(cred.fields):
                inputs[k] = text_field(k, secret=True, hint="leave blank to keep the current value")
                body.append(inputs[k])
        for f in defs:
            if f.secret:
                inputs[f.name] = text_field(f"New {f.label.lower()}", secret=True,
                                            hint="leave blank to keep the current value")
            else:
                inputs[f.name] = text_field(f.label, value=str(cred.fields.get(f.name, "") or ""), hint=f.hint)
            body.append(inputs[f.name])
        expires = dropdown("Reset expiry to", ["Keep current"] + list(EXPIRY_CHOICES), "Keep current")
        body += [expires, ft.Text("Running servers pick up the change within a few minutes – no restart needed.",
                                  size=12, color=ft.Colors.ON_SURFACE_VARIANT)]

        def save(_):
            changes = {k: (c.value or "").strip() for k, c in inputs.items()}
            days = None if expires.value == "Keep current" else (EXPIRY_CHOICES[expires.value] or None)

            def done(_):
                self.ctx.close()
                self.ctx.toast(f"Credential '{name}' updated")
                self.refresh()
            self.ctx.background(lambda: svc.rotate(name, changes, expires_days=days), done)

        self.ctx.dialog(f"Rotate / edit '{name}'", body,
                        [ft.TextButton("Cancel", on_click=lambda _: self.ctx.close()),
                         ft.FilledButton("Save", on_click=save)])

    # ------------------------------------------------------------ actions
    def test(self, name: str) -> None:
        self.ctx.toast(f"Testing '{name}'…")

        def done(res):
            ok, msg = res
            self.ctx.toast(("OK – " if ok else "Failed – ") + msg, error=not ok)
        self.ctx.background(lambda: self.ctx.svc.credentials.test(name), done)

    def delete(self, name: str) -> None:
        def go():
            self.ctx.svc.credentials.remove(name)
            self.ctx.toast(f"Credential '{name}' deleted")
            self.refresh()
        self.ctx.confirm("Delete credential?", f"'{name}' will be removed from the vault. This cannot be undone.", go)

    def _init_vault(self, _=None) -> None:
        def go():
            env = self.ctx.svc.credentials.init_vault()
            self.ctx.toast(f"Master key created and saved to .env as {env}. Back it up in a password manager.")
            self.refresh()
        self.ctx.safe(go)
