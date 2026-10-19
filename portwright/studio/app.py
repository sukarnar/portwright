"""Portwright Studio – desktop (or localhost web) app over the shared service layer."""
from __future__ import annotations

import asyncio
import hmac
import os
import secrets
import sys
import threading
import webbrowser
from pathlib import Path

import flet as ft

from ..compat import config_in, getenv, resolve_config
from ..config import DEFAULT_CONFIG
from ..services import Project, ServiceError, Services
from .common import Ctx, text_field
from .viewmodels import Recent
from .views.access import AccessView
from .views.activity import ActivityView
from .views.credentials import CredentialsView
from .views.dashboard import DashboardView
from .views.endpoints import EndpointsView
from .views.help import HelpView
from .views.loads import LoadsView
from .views.sources import SourcesView

SCREENS = [  # key, label, icon, view class
    ("dashboard", "Dashboard", ft.Icons.DASHBOARD, DashboardView),
    ("sources", "Sources", ft.Icons.STORAGE, SourcesView),
    ("credentials", "Credentials", ft.Icons.LOCK, CredentialsView),
    ("endpoints", "Endpoints", ft.Icons.API, EndpointsView),
    ("loads", "Loads", ft.Icons.MOVE_DOWN, LoadsView),
    ("access", "API keys", ft.Icons.BADGE, AccessView),
    ("activity", "Activity", ft.Icons.HISTORY, ActivityView),
    ("help", "Help", ft.Icons.HELP_OUTLINE, HelpView),
]


class Studio:
    def __init__(self, page: ft.Page, config_path: str | None):
        self.page = page
        self.recent = Recent()
        self.svc: Services | None = None
        self.ctx: Ctx | None = None
        self.views: dict = {}
        self.current = "dashboard"
        self.body = ft.Container(expand=True, padding=ft.Padding.symmetric(horizontal=28, vertical=20))
        self.banner = ft.Container(visible=False)
        page.title = "Portwright Studio"
        page.theme_mode = ft.ThemeMode.LIGHT
        page.theme = ft.Theme(color_scheme_seed=ft.Colors.INDIGO)
        page.padding = 0
        try:
            page.window.width, page.window.height = 1280, 820
            page.window.min_width, page.window.min_height = 960, 640
        except Exception:
            pass
        if config_path and Path(config_path).exists():
            self.open_project(config_path)
        else:
            self.show_welcome(config_path)

    # ---------------------------------------------------------- welcome
    def show_welcome(self, suggested: str | None = None) -> None:
        picker = ft.FilePicker()
        path = text_field("Project file (portwright.yaml)", suggested or "", expand=True)
        new_dir = text_field("New project folder", hint=r"e.g. C:\apps\hr-api", expand=True)
        new_name = text_field("Project name", "my-api")

        async def browse_file(_):
            files = await picker.pick_files(allow_multiple=False, file_type=ft.FilePickerFileType.CUSTOM,
                                            allowed_extensions=["yaml", "yml"])
            if files and getattr(files[0], "path", None):
                path.value = files[0].path
                self.page.update()

        async def browse_dir(_):
            d = await picker.get_directory_path(dialog_title="Folder for the new project")
            if d:
                new_dir.value = d
                self.page.update()

        def do_open(p):
            if not p:
                return
            if Path(p).is_dir():
                p = str(config_in(p))
            self.open_project(p)

        def do_create(_):
            if not new_dir.value:
                new_dir.error = "choose a folder"
                self.page.update()
                return
            try:
                proj = Project.init(new_dir.value, new_name.value or "my-api", actor="studio")
            except ServiceError as exc:
                self.page.show_dialog(ft.SnackBar(ft.Text(str(exc)), bgcolor=ft.Colors.ERROR))
                return
            self.open_project(str(proj.path))

        desktop = not self.page.web
        recent = [ft.ListTile(leading=ft.Icon(ft.Icons.FOLDER), title=ft.Text(Path(p).parent.name),
                              subtitle=ft.Text(p, size=12), on_click=lambda _, p=p: do_open(p))
                  for p in self.recent.list()]
        self.page.controls.clear()
        self.page.add(ft.Container(padding=48, expand=True, content=ft.Column([
            ft.Row([ft.Icon(ft.Icons.HUB, size=40, color=ft.Colors.PRIMARY),
                    ft.Text("Portwright Studio", size=30, weight=ft.FontWeight.W_600)]),
            ft.Text("Open a project or create a new one.", color=ft.Colors.ON_SURFACE_VARIANT),
            ft.Divider(),
            ft.Text("Open project", size=18, weight=ft.FontWeight.W_600),
            ft.Row([path] + ([ft.OutlinedButton("Browse…", icon=ft.Icons.FOLDER_OPEN, on_click=browse_file)]
                             if desktop else []) +
                   [ft.FilledButton("Open", on_click=lambda _: do_open(path.value))], width=900),
            *([ft.Text("Recent", weight=ft.FontWeight.W_600), ft.Column(recent, width=900)] if recent else []),
            ft.Divider(),
            ft.Text("Create project", size=18, weight=ft.FontWeight.W_600),
            ft.Row([new_dir] + ([ft.OutlinedButton("Browse…", icon=ft.Icons.FOLDER_OPEN, on_click=browse_dir)]
                                if desktop else []), width=900),
            ft.Row([new_name, ft.FilledButton("Create", icon=ft.Icons.CREATE_NEW_FOLDER, on_click=do_create)]),
        ], spacing=14, scroll=ft.ScrollMode.AUTO)))
        self.page.update()

    # ------------------------------------------------------------ shell
    def open_project(self, config_path: str) -> None:
        try:
            self.svc = Services.open(config_path, actor="studio")
        except ServiceError as exc:
            self.show_welcome(config_path)
            self.page.show_dialog(ft.SnackBar(ft.Text(str(exc)), bgcolor=ft.Colors.ERROR, show_close_icon=True))
            return
        self.recent.add(self.svc.project.path)
        self.ctx = Ctx(self.page, self.svc, self.go, help=self.open_help, view=lambda k: self.views.get(k))
        self.views = {key: cls(self.ctx) for key, _, _, cls in SCREENS}
        self.rail = ft.NavigationRail(
            selected_index=0, label_type=ft.NavigationRailLabelType.ALL, min_width=96,
            leading=ft.Container(ft.Icon(ft.Icons.HUB, color=ft.Colors.PRIMARY, size=32), padding=ft.Padding.only(top=12)),
            trailing=ft.Container(ft.IconButton(ft.Icons.LOGOUT, tooltip="Close project", on_click=self._close),
                                  padding=ft.Padding.only(top=24)),
            destinations=[ft.NavigationRailDestination(icon=icon, label=label) for _, label, icon, _ in SCREENS],
            on_change=lambda e: self.go(SCREENS[e.control.selected_index][0]))
        self.page.controls.clear()
        self.page.title = f"Portwright Studio – {self.svc.project.config.project}"
        self.page.add(ft.Row([self.rail, ft.VerticalDivider(width=1),
                              ft.Column([self.banner, self.body], expand=True, spacing=0)], expand=True))
        self.go("dashboard")
        self.page.run_task(self._watch_file)

    def go(self, key: str) -> None:
        self.current = key
        idx = [s[0] for s in SCREENS].index(key)
        self.rail.selected_index = idx
        view = self.views[key]
        self.body.content = view.build()
        self.page.update()

    def open_help(self, topic: str, part: str | None = None) -> None:
        self.views["help"].open(topic, part)
        self.go("help")

    def _close(self, _=None) -> None:
        self.svc = None
        self.show_welcome()

    async def _watch_file(self) -> None:
        """Offer a reload when portwright.yaml is changed outside Studio (CLI, Notepad, …)."""
        svc = self.svc
        while self.svc is svc and svc is not None:
            await asyncio.sleep(3)
            if self.svc is not svc:
                return
            if svc.project.changed_on_disk() and not self.banner.visible:
                self.banner.content = ft.Container(
                    bgcolor=ft.Colors.AMBER_100, padding=ft.Padding.symmetric(horizontal=20, vertical=10),
                    content=ft.Row([ft.Icon(ft.Icons.SYNC, color=ft.Colors.AMBER_900),
                                    ft.Text(f"{svc.project.path.name} was changed outside Studio.", expand=True,
                                            color=ft.Colors.AMBER_900),
                                    ft.FilledButton("Reload", on_click=self._reload)]))
                self.banner.visible = True
                self.page.update()

    def _reload(self, _=None) -> None:
        try:
            self.svc.project.reload()
        except ServiceError as exc:
            self.ctx.fail(exc)
            return
        self.banner.visible = False
        self.go(self.current)
        self.ctx.toast("Project reloaded")


# ---------------------------------------------------------------- launcher
def _find_client(explicit: Path | None) -> str | None:
    """Offline desktop client: --client-path, PORTWRIGHT_FLET_CLIENT, or <venv>/flet-client/flet."""
    candidates = [explicit, getenv("PORTWRIGHT_FLET_CLIENT"), Path(sys.prefix) / "flet-client" / "flet",
                  Path(sys.prefix) / "flet-client"]
    for c in candidates:
        if c and (Path(c) / ("flet.exe" if os.name == "nt" else "flet")).exists():
            return str(c)
    return None


def _offline_guard() -> None:
    """Studio never installs or downloads anything at run time (offline / SSL-inspected networks).

    Flet would otherwise pip-install a missing or mismatched flet-web / flet-desktop from PyPI.
    """
    import flet.utils.pip as flet_pip

    def _refuse(package: str, *_a, **_k):
        raise SystemExit(
            f"{package} {ft.version.flet_version} is not installed in this Python environment.\n"
            "Portwright Studio never downloads anything. Install the Studio add-on offline:\n"
            "  .\\install-studio-offline.ps1      (from portwright-studio-addon-windows.zip)")
    flet_pip.install_flet_package = _refuse


def _has(module: str) -> bool:
    import importlib.util
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def launch(config_path: str | None = DEFAULT_CONFIG, web: bool = False, port: int = 8550,
           client_path: Path | None = None, allow_download: bool = False, open_browser: bool = True) -> None:
    if config_path:
        config_path = resolve_config(config_path)
    if not allow_download:
        _offline_guard()
    if not web:
        client = _find_client(client_path)
        if client:
            os.environ["FLET_VIEW_PATH"] = client          # use the pre-downloaded client (offline servers)
        elif client_path:
            raise SystemExit(f"flet{'.exe' if os.name == 'nt' else ''} not found in {client_path}")
        elif not allow_download:
            # Flet would download the desktop client from GitHub here – which fails behind company
            # proxies / certificate inspection. Use the browser instead: nothing to download or run.
            print("The Studio desktop window (flet.exe) is not installed, and Portwright does not download it.\n"
                  "Opening Studio in your browser instead (this PC only).\n"
                  "For the window: install-studio-offline.ps1 -ClientZip <flet-windows.zip>, or --client-path.",
                  flush=True)
            web = True
        if not web and not _has("flet_desktop"):
            print("flet-desktop is not installed - opening Studio in your browser instead.", flush=True)
            web = True
    if web and not _has("flet_web") and not allow_download:
        raise SystemExit("flet-web is not installed in this Python environment. Portwright Studio never "
                         "downloads anything - install the Studio add-on (install-studio-offline.ps1).")
    token = secrets.token_urlsafe(24) if web else None

    def main(page: ft.Page):
        if token is not None:
            from urllib.parse import parse_qs, urlsplit
            given = (parse_qs(urlsplit(page.route or "").query).get("token") or [None])[0]
            if not given or not hmac.compare_digest(given, token):
                page.add(ft.Container(padding=40, content=ft.Text(
                    "Access denied. Open Studio with the link printed in the console.", size=18)))
                page.update()
                return
        Studio(page, config_path)

    if web:
        url = f"http://127.0.0.1:{port}/?token={token}"
        os.environ["FLET_DISPLAY_URL_PREFIX"] = "portwright-studio"     # we open the browser ourselves (with token)
        print(f"Portwright Studio (web mode, this computer only): {url}", flush=True)
        if open_browser:
            threading.Timer(2.0, lambda: webbrowser.open(url)).start()
        ft.run(main, host="127.0.0.1", port=port, view=ft.AppView.WEB_BROWSER, no_cdn=True)
    else:
        ft.run(main)
