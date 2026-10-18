"""Shared UI helpers for Studio views (Flet 1.0)."""
from __future__ import annotations

import traceback
from typing import Any, Callable

import flet as ft

from ..services import ServiceError, Services


class Ctx:
    """What every view gets: the page, the open project's services, and UI helpers."""

    def __init__(self, page: ft.Page, svc: Services, go: Callable[[str], None],
                 help: Callable[..., None] | None = None, view: Callable[[str], Any] | None = None):
        self.page = page
        self.svc = svc
        self.go = go                      # navigate to a screen by key
        self.help = help or (lambda topic, part=None: None)   # open the Help screen at a topic
        self.view = view or (lambda key: None)                # another screen's view object

    # --------------------------------------------------------------- feedback
    def toast(self, msg: str, error: bool = False) -> None:
        self.page.show_dialog(ft.SnackBar(
            ft.Text(msg, color=ft.Colors.ON_ERROR if error else None),
            bgcolor=ft.Colors.ERROR if error else None, show_close_icon=True,
            duration=8000 if error else 4000))

    def fail(self, exc: Exception) -> None:
        if not isinstance(exc, ServiceError):
            traceback.print_exc()
        self.toast(str(exc), error=True)

    # ---------------------------------------------------------------- dialogs
    def dialog(self, title: str, body: list[ft.Control], actions: list[ft.Control], width: int = 560,
               modal: bool = True, scroll: bool = True) -> ft.AlertDialog:
        """scroll=False for short bodies: a scrolling column takes the full dialog height."""
        dlg = ft.AlertDialog(
            modal=modal, title=ft.Text(title, weight=ft.FontWeight.W_600),
            content=ft.Container(width=width, content=ft.Column(
                body, tight=True, scroll=ft.ScrollMode.AUTO if scroll else None, spacing=12,
                horizontal_alignment=ft.CrossAxisAlignment.STRETCH)),
            actions=actions, actions_alignment=ft.MainAxisAlignment.END)
        self.page.show_dialog(dlg)
        return dlg

    def close(self) -> None:
        self.page.pop_dialog()

    def confirm(self, title: str, text: str, on_yes: Callable[[], None], danger: bool = True,
                yes: str = "Delete") -> None:
        def _yes(_):
            self.close()
            self.safe(on_yes)
        self.dialog(title, [ft.Text(text)], [
            ft.TextButton("Cancel", on_click=lambda _: self.close()),
            ft.FilledButton(yes, on_click=_yes,
                            style=ft.ButtonStyle(bgcolor=ft.Colors.ERROR, color=ft.Colors.ON_ERROR) if danger else None),
        ], width=440, scroll=False)

    def info(self, title: str, body: list[ft.Control], width: int = 640) -> None:
        self.dialog(title, body, [ft.TextButton("Close", on_click=lambda _: self.close())], width, modal=False)

    # ------------------------------------------------------------ execution
    def safe(self, fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 – show every failure to the user
            self.fail(exc)
            return None

    def background(self, work: Callable[[], Any], done: Callable[[Any], None],
                   busy: ft.Control | None = None) -> None:
        """Run slow work (DB connections, file reads) off the UI thread."""
        if busy is not None:
            busy.visible = True
            self.page.update()

        def _run():
            try:
                result = work()
            except Exception as exc:  # noqa: BLE001
                result = exc
            if busy is not None:
                busy.visible = False
            if isinstance(result, Exception):
                self.fail(result)
            else:
                try:
                    done(result)
                except Exception as exc:  # noqa: BLE001
                    self.fail(exc)
            self.page.update()
        self.page.run_thread(_run)


# ------------------------------------------------------------------- widgets
def header(title: str, subtitle: str = "", actions: list[ft.Control] | None = None) -> ft.Control:
    return ft.Row([
        ft.Column([ft.Text(title, size=24, weight=ft.FontWeight.W_600),
                   ft.Text(subtitle, color=ft.Colors.ON_SURFACE_VARIANT)] if subtitle else
                  [ft.Text(title, size=24, weight=ft.FontWeight.W_600)], spacing=2, expand=True),
        *(actions or []),
    ], vertical_alignment=ft.CrossAxisAlignment.START)


def empty(text: str, action: ft.Control | None = None) -> ft.Control:
    return ft.Container(
        padding=40, alignment=ft.Alignment.CENTER, border_radius=12,
        border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
        content=ft.Column([ft.Text(text, color=ft.Colors.ON_SURFACE_VARIANT), *( [action] if action else [])],
                          horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=16))


def tag(text: str, kind: str = "neutral") -> ft.Control:
    colors = {"good": (ft.Colors.GREEN_100, ft.Colors.GREEN_900),
              "warn": (ft.Colors.AMBER_100, ft.Colors.AMBER_900),
              "bad": (ft.Colors.RED_100, ft.Colors.RED_900),
              "info": (ft.Colors.BLUE_50, ft.Colors.BLUE_900),
              "neutral": (ft.Colors.SURFACE_CONTAINER_HIGHEST, ft.Colors.ON_SURFACE_VARIANT)}[kind]
    return ft.Container(ft.Text(text, size=12, color=colors[1], weight=ft.FontWeight.W_500),
                        bgcolor=colors[0], padding=ft.Padding.symmetric(horizontal=8, vertical=2),
                        border_radius=10)


def table(columns: list[str], rows: list[list[ft.Control]]) -> ft.Control:
    return ft.Container(
        border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT), border_radius=10,
        content=ft.Row([ft.DataTable(
            columns=[ft.DataColumn(ft.Text(c, weight=ft.FontWeight.W_600)) for c in columns],
            rows=[ft.DataRow([ft.DataCell(c) for c in r]) for r in rows],
            heading_row_height=40, data_row_min_height=44, data_row_max_height=64, column_spacing=24,
            expand=True)]))


def icon_btn(icon, tip: str, on_click, color=None) -> ft.IconButton:
    return ft.IconButton(icon=icon, tooltip=tip, on_click=on_click, icon_color=color, icon_size=20)


def text_field(label: str | None, value: str = "", hint: str = "", secret: bool = False, **kw) -> ft.TextField:
    return ft.TextField(label=label, value=value, helper=hint or None, password=secret,
                        can_reveal_password=secret, dense=True, **kw)


def dropdown(label: str | None, options: list[str] | dict[str, str], value: str | None = None, **kw) -> ft.Dropdown:
    opts = options.items() if isinstance(options, dict) else [(o, o) for o in options]
    return ft.Dropdown(label=label, value=value, dense=True,
                       options=[ft.DropdownOption(key=k, text=t) for k, t in opts], **kw)


def mono(text: str, size: int = 13) -> ft.Control:
    return ft.Text(text, font_family="Consolas", size=size, selectable=True)


def help_btn(ctx: "Ctx", topic: str, part: str | None = None) -> ft.Control:
    """The '?' button in a screen header: opens the matching Help topic."""
    return ft.IconButton(ft.Icons.HELP_OUTLINE, tooltip="Help for this screen",
                         on_click=lambda _: ctx.help(topic, part))


def cli_dialog(ctx: "Ctx", title: str, commands: list[tuple[str, str]], note: str = "") -> None:
    """Show equivalent CLI commands, each with a Copy button. commands = [(label, command), ...]"""
    from .views.help import code_block
    body: list[ft.Control] = []
    if note:
        body.append(ft.Text(note, size=12, color=ft.Colors.ON_SURFACE_VARIANT))
    for label, cmd in commands:
        if label:
            body.append(ft.Text(label, weight=ft.FontWeight.W_600, size=13))
        body.append(code_block(cmd, "powershell", ctx))
    body.append(ft.Text("Run in PowerShell in the project folder after activating portwright "
                        "(C:\\apps\\portwright\\.venv\\Scripts\\Activate.ps1).", size=12,
                        color=ft.Colors.ON_SURFACE_VARIANT))
    ctx.info(title, body, width=820)
