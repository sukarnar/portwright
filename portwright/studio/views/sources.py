from __future__ import annotations

import flet as ft

from ...services import ServiceError
from ..common import Ctx, dropdown, empty, header, icon_btn, table, tag, text_field, help_btn
from ..viewmodels import HTTP_CRED_TYPES, SOURCE_TYPES, save_source, source_values

TYPE_ICON = {"sql": ft.Icons.STORAGE, "file": ft.Icons.DESCRIPTION, "rest": ft.Icons.CLOUD, "callable": ft.Icons.CODE,
             "ldap": ft.Icons.GROUPS}


class SourcesView:
    title = "Sources"

    def __init__(self, ctx: Ctx):
        self.ctx = ctx
        self.root = ft.Column(expand=True, scroll=ft.ScrollMode.AUTO, spacing=16)
        self.picker = ft.FilePicker()

    def build(self) -> ft.Control:
        self.refresh(update=False)
        return self.root

    def refresh(self, update: bool = True) -> None:
        items = self.ctx.svc.sources.list()
        controls: list[ft.Control] = [header(
            "Sources", "Where data comes from: databases, files, upstream APIs, Python functions – and Active Directory",
            [help_btn(self.ctx, "sources"), ft.FilledButton("Add source", icon=ft.Icons.ADD, on_click=lambda _: self.edit_dialog())])]
        if not items:
            controls.append(empty("No sources yet.", ft.FilledButton("Add your first source",
                                                                     on_click=lambda _: self.edit_dialog())))
        else:
            rows = []
            for s in items:
                access = []
                if s["type"] == "sql":
                    access.append(tag("read-only", "info") if s["read_only"] else tag("read-write", "warn"))
                rows.append([
                    ft.Row([ft.Icon(TYPE_ICON[s["type"]], size=18, color=ft.Colors.PRIMARY),
                            ft.Text(s["name"], weight=ft.FontWeight.W_600)]),
                    ft.Text(s["type"]),
                    ft.Text(s["detail"] or "–", max_lines=1, overflow=ft.TextOverflow.ELLIPSIS, width=260),
                    ft.Text(s["credential"] or "–"),
                    ft.Row(access) if access else ft.Text(""),
                    ft.Text(str(len(s["endpoints"]))),
                    ft.Row([icon_btn(ft.Icons.PLAY_ARROW, "Test", lambda _, n=s["name"]: self.test(n)),
                            icon_btn(ft.Icons.EDIT, "Edit", lambda _, n=s["name"]: self.edit_dialog(n)),
                            icon_btn(ft.Icons.DELETE, "Delete", lambda _, n=s["name"]: self.delete(n),
                                     ft.Colors.ERROR)], spacing=0),
                ])
            controls.append(table(["Name", "Type", "Details", "Credential", "Access", "Endpoints", ""], rows))
        self.root.controls = controls
        if update:
            self.ctx.page.update()

    # ---------------------------------------------------------- add / edit
    def edit_dialog(self, name: str | None = None) -> None:
        svc = self.ctx.svc
        v = source_values(svc, name) if name else {"type": "sql"}
        try:
            creds = svc.credentials.list()
        except ServiceError:
            creds = []
        db_creds = [c["name"] for c in creds if c["type"] == "database"]
        http_creds = [c["name"] for c in creds if c["type"] in HTTP_CRED_TYPES]
        all_creds = [c["name"] for c in creds]

        f_name = text_field("Name", v.get("name", ""), hint="used in endpoints, e.g. hr", disabled=bool(name))
        f_type = dropdown("Type", SOURCE_TYPES, v["type"], disabled=bool(name))
        f_desc = text_field("Description (optional)", v.get("description", ""))
        # sql
        f_db_cred = dropdown("Database credential *", db_creds, v.get("credential") or (db_creds[0] if db_creds else None))
        f_rw = ft.Switch(label="Allow writes (insert / update / delete)", value=bool(v.get("read_write")))
        sql_hint = ft.Text("No database credentials yet – add one on the Credentials screen first.",
                           color=ft.Colors.ERROR, visible=not db_creds)
        # file
        f_path = text_field("File name in the data folder *", v.get("path", ""), hint="e.g. transfers.csv", expand=True)
        f_sheet = text_field("Excel sheet (optional)", v.get("sheet", ""))
        browse = ft.OutlinedButton("Browse…", icon=ft.Icons.FOLDER_OPEN, on_click=lambda _: self.ctx.page.run_task(pick))
        # rest
        f_url = text_field("Base URL *", v.get("base_url", ""), hint="https://api.example.com")
        f_allow = ft.Checkbox(label="Allow portwright to call this host", value=bool(v.get("allow_host", True)))
        f_http_cred = dropdown("Credential (optional)", ["(none)"] + http_creds, v.get("credential") or "(none)")
        f_headers = text_field("Extra headers (one per line: Name=value)", v.get("headers", ""), multiline=True,
                               min_lines=2, hint="secrets: use ${CRED:name.field}")
        # callable
        f_module = text_field("Module *", v.get("module", ""), hint="e.g. handlers.pricing (file handlers/pricing.py)")
        f_allowc = text_field("Credentials the functions may read (comma separated)", v.get("allow_credentials", ""),
                              hint="available: " + (", ".join(all_creds) or "none"))

        # ldap (Active Directory)
        basic_creds = [c["name"] for c in creds if c["type"] == "basic"]
        f_ldap_method = dropdown("Connect with", {"ldap": "LDAPS with a service account (ldap3)",
                                                  "powershell": "PowerShell AD module (Windows identity)"},
                                 v.get("directory") or "ldap", width=420)
        f_servers = text_field("Domain controllers (one per line)", v.get("servers", ""), multiline=True,
                               min_lines=2, hint="ldaps://dc01.corp.local – tried in order")
        f_base_dn = text_field("Base DN *", v.get("base_dn", ""), hint="where users and groups are searched, "
                                                                       "e.g. DC=corp,DC=local")
        f_ldap_cred = dropdown("Service account (credential, type basic)", ["(none)"] + basic_creds,
                               v.get("credential") or (basic_creds[0] if basic_creds else "(none)"))
        f_ca = text_field("CA certificate file (optional)", v.get("ca_file", ""),
                          hint="only if Windows does not trust the DC certificate; PEM file")
        f_test_group = text_field("Test group (optional)", v.get("test_group", ""),
                                  hint="Test lists this group's members, e.g. HR-Staff or its DN")
        f_plain = ft.Checkbox(label="Allow unencrypted ldap:// (not recommended)",
                              value=bool(v.get("allow_plain_ldap")))
        ldap_hint = ft.Text("Username as svc@corp.local or CORP\\svc. The account needs 'Write Members' on the "
                            "target groups only. With PowerShell, the account the service runs as is used "
                            "(the credential is optional).", size=12, color=ft.Colors.ON_SURFACE_VARIANT)

        groups = {
            "sql": ft.Column([f_db_cred, sql_hint, f_rw], spacing=10, horizontal_alignment=ft.CrossAxisAlignment.STRETCH),
            "file": ft.Column([ft.Row([f_path, browse]), f_sheet,
                               ft.Text(f"Files are read from the project's '{svc.project.config.security.data_root}' "
                                       "folder. Browse copies the file there.", size=12,
                                       color=ft.Colors.ON_SURFACE_VARIANT)], spacing=10,
                              horizontal_alignment=ft.CrossAxisAlignment.STRETCH),
            "rest": ft.Column([f_url, f_allow, f_http_cred, f_headers], spacing=10, horizontal_alignment=ft.CrossAxisAlignment.STRETCH),
            "callable": ft.Column([f_module, f_allowc], spacing=10, horizontal_alignment=ft.CrossAxisAlignment.STRETCH),
            "ldap": ft.Column([f_ldap_method, f_servers, f_base_dn, f_ldap_cred, f_test_group, f_ca, f_plain, ldap_hint], spacing=10,
                              horizontal_alignment=ft.CrossAxisAlignment.STRETCH),
        }

        def show_group(_=None):
            for k, g in groups.items():
                g.visible = k == f_type.value
            self.ctx.page.update()
        f_type.on_select = show_group

        async def pick():
            files = await self.picker.pick_files(allow_multiple=False, file_type=ft.FilePickerFileType.CUSTOM,
                                                 allowed_extensions=["csv", "json", "jsonl", "xlsx", "xlsm"])
            if not files:
                return
            f = files[0]
            if not getattr(f, "path", None):
                self.ctx.toast("Browsing works in the desktop window; in web mode copy the file into the data "
                               "folder and type its name.", error=True)
                return
            try:
                f_path.value = svc.sources.import_file(f.path)
                self.ctx.toast(f"Copied {f.name} into the data folder")
            except ServiceError as exc:
                self.ctx.fail(exc)
            self.ctx.page.update()

        def save(_):
            t = f_type.value
            values = {"name": f_name.value, "type": t, "description": f_desc.value}
            if t == "sql":
                values.update(credential=f_db_cred.value, read_write=f_rw.value, url=v.get("url"))
            elif t == "file":
                values.update(path=f_path.value, sheet=f_sheet.value)
            elif t == "rest":
                values.update(base_url=f_url.value, allow_host=f_allow.value, headers=f_headers.value,
                              credential=None if f_http_cred.value == "(none)" else f_http_cred.value)
            elif t == "ldap":
                values.update(directory=f_ldap_method.value, servers=f_servers.value, base_dn=f_base_dn.value,
                              credential=None if f_ldap_cred.value == "(none)" else f_ldap_cred.value,
                              ca_file=f_ca.value, allow_plain_ldap=f_plain.value,
                              test_group=f_test_group.value)
            else:
                values.update(module=f_module.value, allow_credentials=f_allowc.value)
            try:
                warnings = save_source(svc, values, editing=bool(name))
            except ServiceError as exc:
                self.ctx.fail(exc)
                return
            self.ctx.close()
            self.ctx.toast(f"Source '{values['name']}' saved" + (" – " + "; ".join(warnings) if warnings else "")
                           + (". Restart the server to apply." if name else ""))
            self.refresh()
            if not name:
                self.next_steps(values["name"], t)

        self.ctx.dialog("Edit source" if name else "Add source",
                        [f_name, f_type, *groups.values(), f_desc],
                        [ft.TextButton("Cancel", on_click=lambda _: self.ctx.close()),
                         ft.FilledButton("Save", on_click=save)])
        show_group()

    def next_steps(self, name: str, stype: str) -> None:
        def endpoint(_):
            self.ctx.close()
            self.ctx.go("endpoints")
            view = self.ctx.view("endpoints")
            if view is None:
                return
            if stype == "sql":
                from .endpoints import CrudWizard
                view.editor = CrudWizard(view, name)
                view.refresh()
            else:
                view.open_editor(None, source=name)

        def test(_):
            self.ctx.close()
            self.test(name)
        if stype == "ldap":
            def group_load(_):
                self.ctx.close()
                self.ctx.go("loads")
                view = self.ctx.view("loads")
                if view is not None:
                    view.open_editor(None, to_group=name)
            self.ctx.dialog("Next steps", [ft.Text(
                f"Source '{name}' is ready. Test it (binds to AD and reads the base DN), then create a load that "
                f"fills an AD group from a query or file with e-mail addresses.")],
                [ft.OutlinedButton("Test it", icon=ft.Icons.PLAY_ARROW, on_click=test),
                 ft.FilledButton("Create group load", icon=ft.Icons.GROUPS, on_click=group_load),
                 ft.TextButton("Done", on_click=lambda _: self.ctx.close())], width=560, scroll=False)
            return
        what = {"sql": "endpoints over one of its tables, or a custom query",
                "file": "an endpoint that returns the file's rows (with filters)",
                "rest": "an endpoint that forwards to this API", "callable": "an endpoint that calls a function"}[stype]
        self.ctx.dialog("Next steps", [ft.Text(f"Source '{name}' is ready. Test it, then create {what}.")],
                        [ft.OutlinedButton("Test it", icon=ft.Icons.PLAY_ARROW, on_click=test),
                         ft.FilledButton("Create endpoint", icon=ft.Icons.API, on_click=endpoint),
                         ft.TextButton("Done", on_click=lambda _: self.ctx.close())], width=560, scroll=False)

    # ------------------------------------------------------------ actions
    def test(self, name: str) -> None:
        self.ctx.toast(f"Testing '{name}'…")

        def done(r):
            if not r.ok:
                self.ctx.toast("Failed – " + r.message, error=True)
                return
            if not r.rows:
                self.ctx.toast("OK – " + r.message)
                return
            cols = r.columns
            self.ctx.info(f"'{name}': {r.message}", [
                ft.Text("Members:" if cols[:2] == ["name", "account"] else "First rows:",
                        color=ft.Colors.ON_SURFACE_VARIANT),
                table(cols, [[ft.Text(str(row.get(c, ""))) for c in cols] for row in r.rows])], width=760)
        self.ctx.background(lambda: self.ctx.svc.sources.test(name), done)

    def delete(self, name: str) -> None:
        def go():
            self.ctx.svc.sources.remove(name)
            self.ctx.toast(f"Source '{name}' deleted")
            self.refresh()
        self.ctx.confirm("Delete source?", f"'{name}' will be removed from the project (its data is not touched).", go)
