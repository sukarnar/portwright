"""Help screen: the portwright guide with 'In Studio' / 'With the CLI' tabs and copyable commands."""
from __future__ import annotations

import flet as ft

from ... import docs
from ..common import Ctx


def code_block(text: str, lang: str, ctx: Ctx) -> ft.Control:
    async def copy(_):
        await ft.Clipboard().set(text)
        ctx.toast("Copied")
    return ft.Container(
        bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST, border_radius=8,
        padding=ft.Padding.only(left=14, top=10, right=6, bottom=10),
        content=ft.Row([
            ft.Text(text, font_family="Consolas", size=13, selectable=True, expand=True),
            ft.Column([ft.IconButton(ft.Icons.CONTENT_COPY, tooltip="Copy", icon_size=18,
                                     on_click=lambda e: ctx.page.run_task(copy, e)),
                       ft.Text(lang if lang not in ("text", "md") else "", size=10,
                               color=ft.Colors.ON_SURFACE_VARIANT)],
                      spacing=0, horizontal_alignment=ft.CrossAxisAlignment.CENTER)],
            vertical_alignment=ft.CrossAxisAlignment.START))


def render_md(md: str, ctx: Ctx) -> list[ft.Control]:
    """Markdown with each fenced code block as a copyable box."""
    out: list[ft.Control] = []
    for kind, text in docs.split_code(md):
        if kind == "md":
            out.append(ft.Markdown(text, selectable=True, extension_set=ft.MarkdownExtensionSet.GITHUB_WEB))
        else:
            out.append(code_block(text, kind, ctx))
    return out


class HelpView:
    title = "Help"

    def __init__(self, ctx: Ctx):
        self.ctx = ctx
        self.slug = "getting-started"
        self.part = "studio"
        self.query = ""
        self.search = ft.TextField(hint_text="Search the guide", dense=True, prefix_icon=ft.Icons.SEARCH,
                                   on_change=self._search, on_submit=self._search)
        self.nav = ft.Column(spacing=0, scroll=ft.ScrollMode.AUTO, expand=True)
        self.page = ft.Column(spacing=12, scroll=ft.ScrollMode.AUTO, expand=True)
        self.root = ft.Row([
            ft.Container(width=270, content=ft.Column([
                ft.Text("Help", size=24, weight=ft.FontWeight.W_600),
                ft.Text("Portwright guide – Studio and CLI (command: portwright, alias pw)", color=ft.Colors.ON_SURFACE_VARIANT, size=12),
                self.search, self.nav], spacing=10, expand=True)),
            ft.VerticalDivider(width=1),
            ft.Container(expand=True, padding=ft.Padding.only(left=16), content=self.page),
        ], expand=True, vertical_alignment=ft.CrossAxisAlignment.STRETCH)

    def build(self) -> ft.Control:
        self.refresh(update=False)
        return self.root

    def open(self, slug: str, part: str | None = None) -> None:
        self.slug = slug
        if part:
            self.part = part
        self.query = ""
        self.search.value = ""

    def refresh(self, update: bool = True) -> None:
        self._nav()
        self._page()
        if update:
            self.ctx.page.update()

    # -------------------------------------------------------------- nav
    def _nav(self) -> None:
        if self.query.strip():
            hits = docs.search(self.query)
            self.nav.controls = [ft.ListTile(title=ft.Text(title, weight=ft.FontWeight.W_600, size=14),
                                             subtitle=ft.Text(snip, size=11, max_lines=3), dense=True,
                                             on_click=lambda _, s=slug: self._go(s))
                                 for slug, title, snip in hits] or \
                                [ft.Text("Nothing found.", color=ft.Colors.ON_SURFACE_VARIANT)]
            return
        self.nav.controls = [
            ft.ListTile(title=ft.Text(t.title, size=14), dense=True, selected=t.slug == self.slug,
                        selected_tile_color=ft.Colors.SECONDARY_CONTAINER,
                        leading=ft.Icon(ft.Icons.TERMINAL if t.slug == docs.CLI_REFERENCE else ft.Icons.ARTICLE,
                                        size=18),
                        on_click=lambda _, s=t.slug: self._go(s))
            for t in docs.topics()]

    def _go(self, slug: str) -> None:
        self.slug = slug
        self.refresh()

    def _search(self, e) -> None:
        self.query = e.control.value or ""
        self._nav()
        self.ctx.page.update()

    # ------------------------------------------------------------- page
    def _page(self) -> None:
        try:
            t = docs.get(self.slug)
        except KeyError:
            t = docs.get("getting-started")
        controls: list[ft.Control] = [ft.Text(t.title, size=26, weight=ft.FontWeight.W_600)]
        controls += render_md(t.intro, self.ctx)
        parts = [p for p, txt in (("studio", t.studio), ("cli", t.cli)) if txt]
        if len(parts) == 2:
            controls.append(ft.SegmentedButton(
                segments=[ft.Segment(value="studio", label=ft.Text("In Studio"), icon=ft.Icon(ft.Icons.DESKTOP_WINDOWS)),
                          ft.Segment(value="cli", label=ft.Text("With the CLI"), icon=ft.Icon(ft.Icons.TERMINAL))],
                selected=[self.part], on_change=self._part))
        part = self.part if self.part in parts else (parts[0] if parts else None)
        if part:
            body = t.studio if part == "studio" else t.cli
            body = body.replace("## In Studio", "").replace("## With the CLI", "").strip()
            controls += render_md(body, self.ctx)
        controls.append(ft.Container(height=24))
        controls.append(ft.Text(f"Also in a terminal:  portwright docs {t.slug}", size=12,
                                color=ft.Colors.ON_SURFACE_VARIANT, font_family="Consolas", selectable=True))
        self.page.controls = controls

    def _part(self, e) -> None:
        self.part = (e.control.selected or ["studio"])[0]
        self._page()
        self.ctx.page.update()
