"""Visual workflow editor: steps as draggable nodes, links drawn between them."""
from __future__ import annotations

import flet as ft
import flet.canvas as cv

from ...services import ServiceError
from .. import graph_vm as gv
from .. import report_vm as rvm
from ..common import Ctx, checkbox_rows, dropdown, text_field

NODE_STYLE = {   # type -> (label, icon, background, accent)
    "input": ("Input", ft.Icons.LOGIN, ft.Colors.TEAL_50, ft.Colors.TEAL_700),
    "tool": ("Tool", ft.Icons.BUILD, ft.Colors.BLUE_50, ft.Colors.BLUE_700),
    "agent": ("Agent", ft.Icons.SMART_TOY, ft.Colors.PURPLE_50, ft.Colors.PURPLE_700),
    "condition": ("Condition", ft.Icons.CALL_SPLIT, ft.Colors.AMBER_50, ft.Colors.AMBER_800),
    "llm": ("LLM prompt", ft.Icons.AUTO_AWESOME, ft.Colors.CYAN_50, ft.Colors.CYAN_800),
    "load": ("Run load", ft.Icons.STORAGE, ft.Colors.ORANGE_50, ft.Colors.ORANGE_800),
    "report_design": ("Design report (AI)", ft.Icons.AUTO_AWESOME_MOSAIC, ft.Colors.PURPLE_50, ft.Colors.PURPLE_900),
    "report": ("Report", ft.Icons.DESCRIPTION, ft.Colors.LIGHT_BLUE_50, ft.Colors.LIGHT_BLUE_900),
    "copy": ("Copy", ft.Icons.FILE_COPY, ft.Colors.LIGHT_GREEN_50, ft.Colors.LIGHT_GREEN_900),
    "move": ("Move", ft.Icons.DRIVE_FILE_MOVE, ft.Colors.DEEP_ORANGE_50, ft.Colors.DEEP_ORANGE_800),
    "file": ("Files", ft.Icons.FOLDER, ft.Colors.BLUE_GREY_50, ft.Colors.BLUE_GREY_700),
    "email": ("Email", ft.Icons.EMAIL, ft.Colors.RED_50, ft.Colors.RED_800),
    "foreach": ("For each", ft.Icons.REPEAT, ft.Colors.PINK_50, ft.Colors.PINK_700),
    "switch": ("Switch", ft.Icons.ALT_ROUTE, ft.Colors.DEEP_PURPLE_50, ft.Colors.DEEP_PURPLE_700),
    "set": ("Set value", ft.Icons.EDIT_NOTE, ft.Colors.GREEN_50, ft.Colors.GREEN_700),
    "output": ("Output", ft.Icons.LOGOUT, ft.Colors.INDIGO_50, ft.Colors.INDIGO_700),
}
LINK_COLOR = {"next": ft.Colors.BLUE_GREY_500, "then": ft.Colors.GREEN_600, "else": ft.Colors.RED_500,
              "default": ft.Colors.BLUE_GREY_300}
CASE_COLORS = [ft.Colors.DEEP_PURPLE_400, ft.Colors.TEAL_500, ft.Colors.ORANGE_600, ft.Colors.PINK_400, ft.Colors.INDIGO_400]


def link_color(n, port: str):
    """Colour of a port / link; switch cases each get their own."""
    if port.startswith("case:"):
        return CASE_COLORS[(list(n.ports).index(port) if port in n.ports else 0) % len(CASE_COLORS)]
    return LINK_COLOR[port]


def port_tip(port: str) -> str:
    if port.startswith("case:"):
        return f"When the value is '{port[5:]}': click, then click the step to go to"
    return {"next": "Next step: click, then click the step to go to",
            "then": "When true: click, then click the step to go to",
            "else": "When false: click, then click the step to go to",
            "default": "When no case matches: click, then click the step to go to"}[port]
PORT_R = 8
AREA_H = 420


def window_size(ctx) -> tuple[float, float]:
    """The app window (page) size; a sensible default when it is not known (tests, first paint)."""
    page = getattr(ctx, "page", None)
    w, h = getattr(page, "width", None), getattr(page, "height", None)
    return (w if isinstance(w, (int, float)) and w else 1280), (h if isinstance(h, (int, float)) and h else 820)


def _help(text: str) -> ft.Text:
    return ft.Text(text, size=12, color=ft.Colors.ON_SURFACE_VARIANT)


def _touch(*controls: ft.Control) -> None:
    """Refresh controls that are on screen; quietly skip ones that are not mounted yet (tests, first build)."""
    for c in controls:
        try:
            c.update()
        except RuntimeError:
            pass


class WorkflowCanvas:
    """Edits one workflow's steps. ``save`` writes them back through the agents service."""

    def __init__(self, ctx: Ctx, name: str | None, on_saved=None):
        self.ctx = ctx
        self.svc = ctx.svc.agents
        self.name = name
        self.on_saved = on_saved
        cfg = ctx.svc.project.config
        self.tools, self.agents = list(cfg.tools), list(cfg.agents)
        self.models = [m["name"] for m in self.svc.list("model")]
        self.loads = [l.name for l in cfg.loads]
        self.cfg = cfg
        self.reports = [r.name for r in cfg.reports]
        self.get_endpoints = [e.name for e in cfg.endpoints if e.method == "GET"]
        self.sql_sources = [n for n, s in cfg.sources.items() if s.type == "sql"]
        self.base: dict = {}
        steps: list[dict] = []
        if name:
            spec = self.svc.get("workflow", name).model_dump(mode="json", by_alias=True, exclude_none=True)
            steps = spec.pop("steps")
            self.base = spec
        self.graph = gv.from_steps(steps)
        self.selected: str | None = self.graph.start
        self.linking: tuple[str, str] | None = None
        self.cards: dict[str, ft.GestureDetector] = {}
        self.f_name = text_field("Workflow name", name or "", width=240, disabled=bool(name))
        self.f_template = None
        if not name:                                          # templates are for starting a new workflow
            self.f_template = dropdown("Start from a template", {t["id"]: t["title"] for t in self.svc.templates()},
                                       None, width=300, on_select=lambda e: self.load_template(e.control.value))
        self.f_big = ft.IconButton(ft.Icons.OPEN_IN_FULL, tooltip="Fill the window", on_click=self.toggle_size)
        self.hint = ft.Text("Start from a template, or add steps with the buttons above." if not self.graph.nodes else "",
                            size=12, color=ft.Colors.ON_SURFACE_VARIANT)
        self.problems = ft.Column(spacing=2)
        self.big, self.dialog = False, None
        self.area_h = self.fit_sizes()[1]
        self.inspector = ft.Column(spacing=10, width=300, height=self.area_h, scroll=ft.ScrollMode.ALWAYS)
        self.link_layer = cv.Canvas(shapes=[])
        self.stack = ft.Stack(controls=[])
        # horizontal scroll on the outside so its bar sits at the bottom of the visible area (not below the canvas)
        self.col = ft.Column([self.stack], scroll=ft.ScrollMode.ALWAYS, height=self.area_h - 2)
        self.area = ft.Container(content=ft.Row([self.col], scroll=ft.ScrollMode.ALWAYS,
                                                vertical_alignment=ft.CrossAxisAlignment.START),
                                 height=self.area_h, expand=True,
                                 border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT), border_radius=10,
                                 bgcolor=ft.Colors.SURFACE_CONTAINER_LOWEST)
        self.rebuild()

    # --------------------------------------------------------------- size
    def fit_sizes(self) -> tuple[int, int]:
        """(dialog width, canvas height) for the current window; 'big' uses all of it."""
        pw, ph = window_size(self.ctx)
        if self.big:
            return int(max(640, pw - 40)), int(max(320, ph - 250))
        return int(max(640, min(1220, pw - 96))), int(max(320, min(640, ph - 330)))

    def toggle_size(self, _=None) -> None:
        """Switch between the normal editor size and filling the window."""
        self.big = not self.big
        self.f_big.icon = ft.Icons.CLOSE_FULLSCREEN if self.big else ft.Icons.OPEN_IN_FULL
        self.f_big.tooltip = "Back to normal size" if self.big else "Fill the window"
        self.apply_size()

    def apply_size(self) -> None:
        width, self.area_h = self.fit_sizes()
        self.col.height, self.area.height, self.inspector.height = self.area_h - 2, self.area_h, self.area_h
        if self.dialog is not None:
            self.dialog.content.width = width
            _touch(self.dialog)
        _touch(self.f_big, self.area, self.inspector)

    # --------------------------------------------------------------- layout
    def body(self) -> list[ft.Control]:
        bar = ft.Row([
            self.f_name,
            *([self.f_template] if self.f_template else []),
            *[ft.OutlinedButton(NODE_STYLE[t][0], icon=NODE_STYLE[t][1], tooltip=f"Add step: {NODE_STYLE[t][0]}",
                                on_click=lambda _, t=t: self.add(t)) for t in gv.TYPES],
            ft.IconButton(ft.Icons.AUTO_FIX_HIGH, tooltip="Tidy layout", on_click=self.tidy),
            self.f_big],
            wrap=True, spacing=8, vertical_alignment=ft.CrossAxisAlignment.END)
        return [bar, self.hint, ft.Row([self.area, self.inspector], vertical_alignment=ft.CrossAxisAlignment.START,
                                       spacing=14), self.problems]

    def rebuild(self) -> None:
        """Recreate every node card and link (after add / delete / rename / tidy)."""
        w, h = gv.canvas_size(self.graph)
        self.stack.width, self.stack.height = w, h
        self.link_layer.width, self.link_layer.height = w, h
        self.numbers = gv.step_numbers(self.graph)            # run order, from the links
        self.cards = {n.id: self._card(n) for n in self.graph.nodes}
        ports: list[ft.Control] = []
        for n in self.graph.nodes:
            ports += self._ports(n)
        self.stack.controls = [self.link_layer, *self.cards.values(), *ports]
        self.draw_links()
        self.show_inspector()
        self.show_problems()
        _touch(self.stack)

    def _card(self, n: gv.Node) -> ft.GestureDetector:
        label, icon, bg, accent = NODE_STYLE[n.type]
        sel = n.id == self.selected
        num = getattr(self, "numbers", {}).get(n.id)
        badge = ft.Container(
            ft.Text(str(num) if num else "–", size=11, weight=ft.FontWeight.W_700,
                    color=ft.Colors.WHITE if num else ft.Colors.BLACK_54),
            bgcolor=accent if num else ft.Colors.GREY_300, border_radius=9, width=22, height=18,
            alignment=ft.Alignment.CENTER,
            tooltip=f"Step {num} in run order" if num else "Not reached: no link leads here from the start")
        card = ft.Container(
            width=gv.NODE_W, height=gv.node_h(n), bgcolor=bg, border_radius=10, padding=ft.Padding.symmetric(horizontal=10, vertical=8),
            border=ft.Border.all(3 if sel else 1, accent if sel else ft.Colors.OUTLINE_VARIANT),
            content=ft.Column([
                ft.Row([badge, ft.Icon(icon, size=16, color=accent),
                        ft.Text(n.id, weight=ft.FontWeight.W_600, size=13, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS,
                                expand=True, color=ft.Colors.BLACK_87),
                        *([ft.Icon(ft.Icons.FLAG, size=14, color=ft.Colors.GREEN_700, tooltip="Start")]
                          if n.id == self.graph.start else [])], spacing=6),
                ft.Text(self._summary(n), size=11, color=ft.Colors.BLACK_54, max_lines=2,
                        overflow=ft.TextOverflow.ELLIPSIS)], spacing=4))
        return ft.GestureDetector(content=card, left=n.x, top=n.y, mouse_cursor=ft.MouseCursor.MOVE,
                                  on_tap=lambda _, i=n.id: self.click(i),
                                  on_pan_update=lambda e, i=n.id: self.drag(i, e))

    @staticmethod
    def _summary(n: gv.Node) -> str:
        names = ", ".join(k for k in (gv._parse(n.inputs) if isinstance(gv._parse(n.inputs), dict) else {}))
        opts = gv._parse(n.settings) if isinstance(gv._parse(n.settings), dict) else {}
        to = str(opts.get("to") or "").strip()
        to = to if to.rstrip("/:") not in ("", "dest") else "choose a folder"
        fmts = opts.get("formats")
        fmts = ", ".join(fmts) if isinstance(fmts, list) else (fmts or "")
        return {"report_design": (opts.get("report") or "name the report") + " · " + (n.model or "choose a model"),
                "report": (opts.get("report") or "inline report") + (f" · {fmts}" if fmts else ""),
                "copy": f"→ {to}", "move": f"→ {to} (removes sources)",
                "file": str(opts.get("op") or "choose an operation") + " " + str(opts.get("to") or opts.get("path") or ""),
                "email": ("to " + rvm.show_value(opts.get("to")) if opts.get("to") else "choose recipients")
                         + (f" · {opts['server']}" if opts.get("server") else ""),
                "input": names or "declare the inputs", "tool": n.tool or "choose a tool",
                "agent": n.agent or "choose an agent", "condition": n.when or "enter a condition",
                "llm": (n.model or "choose a model") + (" · JSON" if n.format == "json" else ""),
                "load": n.load or "choose a load",
                "foreach": f"each of {n.items or '…'}: " + (n.target() or (n.do if not n.target_kind
                                                                             else f"choose a {n.target_kind}")),
                "switch": ("on " + n.when) if n.when else "enter the value to switch on",
                "set": (n.value or "enter a value"), "output": (n.value or "the last step's result")}[n.type]

    def _ports(self, n: gv.Node) -> list[ft.Control]:
        out: list[ft.Control] = []
        px, py = gv.port_point(n, None)
        out.append(ft.Container(width=2 * PORT_R, height=2 * PORT_R, left=px - PORT_R, top=py - PORT_R,
                                border_radius=PORT_R, bgcolor=ft.Colors.BLUE_GREY_100,
                                border=ft.Border.all(1, ft.Colors.BLUE_GREY_400), tooltip="Input",
                                on_click=lambda _, i=n.id: self.click(i)))
        for port in n.ports:
            x, y = gv.port_point(n, port)
            active = self.linking == (n.id, port)
            out.append(ft.Container(
                width=2 * PORT_R, height=2 * PORT_R, left=x - PORT_R, top=y - PORT_R, border_radius=PORT_R,
                bgcolor=link_color(n, port), border=ft.Border.all(3 if active else 1, ft.Colors.BLACK if active else ft.Colors.WHITE),
                tooltip=port_tip(port),
                on_click=lambda _, i=n.id, p=port: self.start_link(i, p)))
        return out

    def draw_links(self) -> None:
        shapes: list[cv.Shape] = []
        for (src, port), dst in self.graph.links.items():
            a, b = self.graph.node(src), self.graph.node(dst)
            if a is None or b is None:
                continue
            p0, p3 = gv.port_point(a, port), gv.port_point(b, None)
            c1, c2 = gv.link_curve(p0, p3)
            color = link_color(a, port)
            shapes.append(cv.Path([cv.Path.MoveTo(*p0), cv.Path.CubicTo(c1[0], c1[1], c2[0], c2[1], p3[0], p3[1])],
                                  paint=ft.Paint(color=color, stroke_width=2.5, style=ft.PaintingStyle.STROKE)))
            shapes.append(cv.Circle(p3[0] - 3, p3[1], 4, paint=ft.Paint(color=color, style=ft.PaintingStyle.FILL)))
        self.link_layer.shapes = shapes

    # ----------------------------------------------------------- interaction
    def add(self, kind: str) -> None:
        n = gv.add_node(self.graph, kind, 40 + 30 * len(self.graph.nodes) % 300, 40 + 40 * len(self.graph.nodes) % 260)
        self.selected = n.id
        self.rebuild()

    def tidy(self, _=None) -> None:
        gv.auto_layout(self.graph)
        self.rebuild()

    def click(self, node_id: str) -> None:
        """Select a node, or finish a link that was started from an output port."""
        if self.linking:
            src, port = self.linking
            self.linking = None
            gv.connect(self.graph, src, port, node_id)
            self.set_hint("")
        self.selected = node_id
        self.rebuild()

    def start_link(self, node_id: str, port: str) -> None:
        if self.linking == (node_id, port):                   # second click cancels
            self.linking = None
            self.set_hint("")
            self.rebuild()
            return
        self.linking = (node_id, port)
        self.selected = node_id
        self.set_hint(f"Now click the step that '{node_id}' ({port}) should go to – or click the port again to cancel.")
        self.rebuild()

    def drag(self, node_id: str, e) -> None:
        n = self.graph.node(node_id)
        if n is None:
            return
        n.x = max(0, n.x + e.local_delta.x)
        n.y = max(0, n.y + e.local_delta.y)
        card = self.cards[node_id]
        card.left, card.top = n.x, n.y
        self.move_ports(n)                                    # ports travel with their node
        self.draw_links()
        _touch(card, self.link_layer)

    def move_ports(self, n: gv.Node) -> None:
        """Re-place the port handles of one node (they are separate stack children)."""
        idx = list(self.graph.nodes).index(n)
        per = 1 + len(n.ports)
        start = 1 + len(self.cards) + sum(1 + len(m.ports) for m in self.graph.nodes[:idx])
        handles = self.stack.controls[start:start + per]
        spots = [gv.port_point(n, None)] + [gv.port_point(n, p) for p in n.ports]
        for h, (x, y) in zip(handles, spots):
            h.left, h.top = x - PORT_R, y - PORT_R
            _touch(h)

    def set_hint(self, text: str) -> None:
        self.hint.value = text
        _touch(self.hint)

    # --------------------------------------------------------------- inspector
    def show_inspector(self) -> None:
        n = self.graph.node(self.selected) if self.selected else None
        if n is None:
            self.inspector.controls = [ft.Text("Select a step to edit it, or add one above.",
                                               color=ft.Colors.ON_SURFACE_VARIANT)]
            _touch(self.inspector)
            return
        label, icon, _, accent = NODE_STYLE[n.type]
        f_id = text_field("Step id", n.id)

        def rename(_):
            try:
                gv.rename_node(self.graph, n.id, f_id.value)
            except ValueError as exc:
                f_id.error = str(exc)
                _touch(f_id)
                return
            self.selected = (f_id.value or n.id).strip()
            self.rebuild()
        f_id.on_blur = rename
        f_id.on_submit = rename
        fields: list[ft.Control] = [ft.Row([ft.Icon(icon, color=accent), ft.Text(label, weight=ft.FontWeight.W_600)]), f_id]

        def bind(field: str, ctl):
            def changed(_):
                setattr(n, field, ctl.value or "")
                self.refresh_card(n)
                self.show_problems()
            ctl.on_change = changed
            if isinstance(ctl, ft.Dropdown):
                ctl.on_select = changed
            return ctl

        if n.type == "input":
            fields += [bind("inputs", text_field("Inputs (YAML)", n.inputs, multiline=True, min_lines=4,
                                                 hint="name: default value  -  leave a name empty to make it required")),
                       ft.Text("Read them in any step as ${input.name}. The workflow is run with these values.",
                               size=12, color=ft.Colors.ON_SURFACE_VARIANT)]
        elif n.type == "output":
            fields += [bind("value", text_field("Result", n.value, multiline=True, min_lines=3,
                                                hint="${steps.x.output}, or a YAML mapping. Empty = the last step's result")),
                       ft.Text("Reaching an output step ends the workflow with this result.", size=12,
                               color=ft.Colors.ON_SURFACE_VARIANT)]
        elif n.type == "llm":
            fields += self._llm_fields(n, bind)
        elif n.type == "load":
            fields += [self._pick(n, bind, "load", "Load", self.loads, "No loads yet. Define one on the Loads screen."),
                       ft.Text("Needs the scope load:run on the workflow (Workflows > Add > Scopes).", size=12,
                               color=ft.Colors.ON_SURFACE_VARIANT)]
        elif n.type == "foreach":
            fields += self._foreach_fields(n, bind)
        elif n.type in gv.SETTINGS_TYPES:
            fields += self._settings_fields(n, bind)
        elif n.type == "switch":
            fields += [bind("when", text_field("Value to switch on", n.when, multiline=True, min_lines=2,
                                               hint="e.g. ${steps.classify.output}")),
                       self._cases_field(n),
                       ft.Text(f"One value per line (up to {gv.MAX_CASES}). Matching ignores upper/lower case. "
                               "The grey port is used when nothing matches.", size=12, color=ft.Colors.ON_SURFACE_VARIANT)]
        elif n.type == "tool":
            fields += [bind("tool", dropdown("Tool", self.tools, n.tool or None)),
                       *self._quick_tool(n),
                       bind("input", text_field("Arguments (YAML)", n.input, multiline=True, min_lines=3,
                                                hint="e.g. dept: ${input.dept}"))]
        elif n.type == "agent":
            fields += [bind("agent", dropdown("Agent", self.agents, n.agent or None)),
                       bind("input", text_field("Task for the agent", n.input, multiline=True, min_lines=3,
                                                hint="e.g. Review ${steps.pull.output}"))]
        elif n.type == "condition":
            fields += [bind("when", text_field("Condition", n.when, multiline=True, min_lines=2,
                                               hint="${steps.x.output.ok} == yes  –  or just a ${...} value")),
                       ft.Text("Green port = true, red port = false.", size=12, color=ft.Colors.ON_SURFACE_VARIANT)]
        else:
            fields += [bind("value", text_field("Value", n.value, multiline=True, min_lines=2,
                                                hint="text, YAML or ${...}"))]
        for port in n.ports:
            tgt = self.graph.links.get((n.id, port))
            fields.append(ft.Row([
                ft.Container(width=10, height=10, border_radius=5, bgcolor=link_color(n, port)),
                ft.Text((f"{port} → {tgt}" if tgt else f"{port} → end").replace("case:", "is "), size=12, expand=True),
                *([ft.IconButton(ft.Icons.LINK_OFF, icon_size=16, tooltip="Remove this link",
                                 on_click=lambda _, p=port: self.unlink(n.id, p))] if tgt else [])], spacing=6))
        if n.type not in ("input", "output", "switch"):
            fields.append(bind("on_error", dropdown("If this step fails",
                                                    {"fail": "Stop the workflow", "continue": "Carry on"}, n.on_error)))
        fields.append(ft.Row([
            ft.OutlinedButton("Make start", icon=ft.Icons.FLAG, disabled=n.id == self.graph.start,
                              on_click=lambda _: self.make_start(n.id)),
            ft.OutlinedButton("Delete", icon=ft.Icons.DELETE, style=ft.ButtonStyle(color=ft.Colors.ERROR),
                              on_click=lambda _: self.delete(n.id))], wrap=True))
        self.inspector.controls = fields
        _touch(self.inspector)

    # ------------------------------------------------------- new node inspectors
    def _pick(self, n: gv.Node, bind, field: str, label: str, options: list[str], empty: str):
        if not options:
            return ft.Text(empty, size=12, color=ft.Colors.AMBER_800)
        return bind(field, dropdown(label, options, getattr(n, field) or None))

    def _llm_fields(self, n: gv.Node, bind) -> list[ft.Control]:
        f_input = bind("input", text_field("Prompt", n.input, multiline=True, min_lines=3,
                                           hint="e.g. Summarise ${steps.pull.output}"))
        f_system = bind("system", text_field("System prompt (optional)", n.system, multiline=True, min_lines=2))
        f_format = bind("format", dropdown("Reply as", {"text": "Plain text", "json": "JSON (parsed for later steps)"},
                                           n.format or "text"))
        try:
            prompts = {i["id"]: f"{i['title']} - {i['category']}" for i in self.svc.library("prompt", use="llm")}
        except ServiceError:
            prompts = {}
        f_lib = dropdown("System prompt from the library", {"": "(write my own)", **prompts}, "")

        def use_prompt(_):
            if not f_lib.value:
                return
            it = self.svc.library_item(f_lib.value, "prompt")
            n.system = f_system.value = it["text"].strip()
            n.format = f_format.value = "json" if it.get("output") == "json" else "text"
            if not (n.input or "").strip() and it.get("example_input"):
                n.input = f_input.value = it["example_input"]       # a starting point to edit
            _touch(f_system, f_format, f_input)
            self.refresh_card(n)
            self.show_problems()
        f_lib.on_select = use_prompt
        return [self._pick(n, bind, "model", "Model", self.models, "No models yet. Add one on the Models tab."),
                f_lib, f_input, f_system, f_format]

    _SETTINGS_HELP = {
        "report_design": "report: the report to design (it is created when it does not exist; then add from: "
                         "{endpoint: <name>} or {source: <sql>, query: ...}). formats: [pdf, xlsx]. base: a gallery "
                         "template to start from. overwrite: never reuses an existing AI template, always designs "
                         "again. data_access / sample_rows can only lower what the model sees. Needs reports:write.",
        "report": "report: <name> runs a saved report (formats: [pdf, xlsx] overrides its formats; "
                  "data: ${steps.q.output.rows} uses rows from an earlier step). Needs the scope reports:run. "
                  "Later steps use ${steps.<id>.output.outputs}.",
        "copy": "files: what to copy (a report step's ${steps.<id>.output.outputs}, paths or root:addresses). "
                "to: a folder, e.g. dest:finance/${run.date.year}-${run.date.month}. "
                "overwrite: never | always | rename | if_newer. manifest: true writes manifest.json. "
                "Needs the scope fs:dest:<name> (or fs:<root>:write).",
        "move": "Like Copy, but each source file is removed after its copy is written and verified. "
                "The source folder must be writable and the workflow needs fs:<root>:write for it.",
        "file": "op: write (to, content) | read (path) | list (path, glob, recursive) | mkdir (to) | "
                "delete (files; goes to the trash folder) | zip (files, to: reports:archive/x.zip).",
        "email": "Sends a message through a mail server (portwright mail add). to / cc / bcc: a list or "
                 "\"a@corp.local, b@corp.local\"; only addresses the server allows. files: attachments, e.g. "
                 "${steps.<report>.output.outputs}. html: true sends body as HTML. Needs the scope "
                 "mail:<server>, and fs:reports:read for report outputs.",
    }

    def _settings_fields(self, n: gv.Node, bind) -> list[ft.Control]:
        """A form for the node's options; 'All options (YAML)' below shows (and edits) everything."""
        opts = rvm.node_opts(n)
        box = text_field("All options (YAML)", n.settings, multiline=True, min_lines=3,
                         hint="the fields above write these; edit here for options they do not show")

        def put(key: str, value) -> None:
            rvm.set_opt(n, key, value)
            box.value = n.settings
            _touch(box)
            self.refresh_card(n)
            self.show_problems()

        def on(ctl, key: str, conv=lambda v: v):
            def changed(_):
                put(key, conv(ctl.value))
            ctl.on_change = changed
            if isinstance(ctl, ft.Dropdown):
                ctl.on_select = changed
            return ctl

        def commit_yaml(_):
            n.settings = box.value or ""
            self.refresh_card(n)
            self.show_problems()
            self.show_inspector()
        box.on_blur = commit_yaml
        form = {"report": self._form_report, "report_design": self._form_design, "copy": self._form_transfer,
                "move": self._form_transfer, "file": self._form_file, "email": self._form_email}[n.type]
        return [*form(n, opts, on, put, bind), _help(self._SETTINGS_HELP[n.type]), box]

    def _formats_row(self, opts: dict, put, label: str) -> list[ft.Control]:
        if isinstance(opts.get("formats"), str):              # e.g. ${input.formats}: keep it as text
            f = text_field("Formats", opts["formats"], hint="pdf,xlsx or ${input.formats}")
            f.on_change = lambda _: put("formats", (f.value or "").strip() or None)
            return [f]
        cur = set(opts.get("formats") or [])
        boxes = {f: ft.Checkbox(label=f.upper(), value=f in cur) for f in rvm.FORMATS}

        def changed(_):
            put("formats", [f for f, b in boxes.items() if b.value])
        for b in boxes.values():
            b.on_change = changed
        return [_help(label), checkbox_rows(boxes.values())]

    def _form_report(self, n: gv.Node, opts: dict, on, put, bind) -> list[ft.Control]:
        out: list[ft.Control] = []
        if self.reports:
            out.append(on(dropdown("Report", {r: r for r in self.reports}, opts.get("report") or None), "report"))
        else:
            out.append(ft.Text("No reports yet. Add one on the Reports screen.", size=12, color=ft.Colors.AMBER_800))
        out.append(bind("input", text_field("Report params (YAML)", n.input, multiline=True, min_lines=2,
                                            hint="e.g. dept: ${input.dept}")))
        out += self._formats_row(opts, put, "Formats (none ticked: the report's own)")
        out += [on(text_field("Rows from an earlier step (optional)", rvm.show_value(opts.get("data")),
                              hint="e.g. ${steps.query.output.rows} - instead of the report's source"),
                   "data", rvm.parse_value),
                on(text_field("Output path (optional)", opts.get("output_path") or "",
                              hint="e.g. reports/output/${run.date.iso}/headcount"), "output_path")]
        return out

    def _form_transfer(self, n: gv.Node, opts: dict, on, put, bind) -> list[ft.Control]:
        verb = n.type
        files = text_field(f"Files to {verb}", rvm.show_value(opts.get("files")),
                           hint="a report step's outputs, paths, or root:path addresses")
        on(files, "files", rvm.parse_value)
        out: list[ft.Control] = [files]
        suggestion = rvm.suggested_files(self.graph, n.id)
        if suggestion and not opts.get("files"):
            def use(_):
                files.value = suggestion
                _touch(files)
                put("files", suggestion)
            out.append(ft.TextButton(f"Use {suggestion}", icon=ft.Icons.SUBDIRECTORY_ARROW_RIGHT, on_click=use))
        targets = rvm.delivery_targets(self.cfg)
        base, sub = rvm.split_to(str(opts.get("to") or ""))
        if base and base not in targets:
            targets[base] = base
        dest = dropdown("Deliver to", targets, base or None)
        f_sub = text_field("Sub-folder (optional)", sub, hint="e.g. ${run.date.year}-${run.date.month}")

        def to_changed(_):
            put("to", rvm.join_to(dest.value or "", f_sub.value or ""))
        dest.on_select = dest.on_change = to_changed
        f_sub.on_change = to_changed
        out += [dest, f_sub]
        if not self.cfg.destinations:
            out.append(_help("No destinations yet: add one under 'destinations:' in portwright.yaml; a share's path "
                             "must also be in security.allowed_folders."))
        out += [on(dropdown("If the file is already there", rvm.OVERWRITE_LABELS, opts.get("overwrite") or ""),
                   "overwrite", lambda v: v or None),
                on(dropdown("Check each copy", rvm.VERIFY_LABELS, opts.get("verify") or "sha256"), "verify"),
                on(ft.Checkbox(label="Write manifest.json", value=bool(opts.get("manifest"))),
                   "manifest"),
                on(ft.Checkbox(label="Create missing folders", value=opts.get("create_dirs", True) is not False),
                   "create_dirs")]
        if verb == "move":
            out.append(ft.Text("Source files are removed after a verified copy. The source folder must be "
                               "writable.", size=12, color=ft.Colors.DEEP_ORANGE_800))
        return out

    def _form_email(self, n: gv.Node, opts: dict, on, put, bind) -> list[ft.Control]:
        servers = sorted(self.cfg.mail_servers)
        out: list[ft.Control] = []
        if servers:
            out.append(on(dropdown("Mail server", {s: f"{s} ({self.cfg.mail_servers[s].host})" for s in servers},
                                   opts.get("server") or (servers[0] if len(servers) == 1 else None)), "server"))
            if len(servers) == 1 and not opts.get("server"):
                rvm.set_opt(n, "server", servers[0])
            spec = self.cfg.mail_servers.get(opts.get("server") or servers[0])
            if spec:
                out.append(_help(f"From {spec.from_}; allowed recipients: {', '.join(spec.recipient_patterns())}"))
        else:
            out.append(ft.Text("No mail servers yet: portwright mail add <name> --host … --from …",
                               size=12, color=ft.Colors.AMBER_800))
        for key, label, hint in (("to", "To", "a@corp.local, b@corp.local or ${input.recipients}"),
                                 ("cc", "Cc (optional)", ""), ("bcc", "Bcc (optional)", "")):
            out.append(on(text_field(label, rvm.show_value(opts.get(key)), hint=hint), key, rvm.parse_value))
        out.append(on(text_field("Subject", opts.get("subject") or "", hint="e.g. Headcount ${run.date.iso}"),
                      "subject"))
        out.append(on(text_field("Message", rvm.show_value(opts.get("body")), multiline=True, min_lines=3,
                                 hint="plain text; ${steps.<id>.output} values are filled in"), "body"))
        out.append(on(ft.Checkbox(label="The message is HTML", value=bool(opts.get("html"))), "html",
                      lambda v: True if v else None))
        files = text_field("Attachments", rvm.show_value(opts.get("files")),
                           hint="a report step's ${steps.<id>.output.outputs}, paths or root:path addresses")
        on(files, "files", rvm.parse_value)
        out.append(files)
        suggestion = rvm.suggested_files(self.graph, n.id)
        if suggestion and not opts.get("files"):
            def use(_):
                files.value = suggestion
                _touch(files)
                put("files", suggestion)
            out.append(ft.TextButton(f"Attach {suggestion}", icon=ft.Icons.ATTACH_FILE, on_click=use))
        return out

    def _form_file(self, n: gv.Node, opts: dict, on, put, bind) -> list[ft.Control]:
        op = opts.get("op") or ""
        f_op = dropdown("Operation", rvm.FILE_OPS, op or None)

        def op_changed(_):
            put("op", f_op.value)
            self.show_inspector()                             # different fields per operation
        f_op.on_select = f_op.on_change = op_changed
        out: list[ft.Control] = [f_op]
        fields = {
            "to": lambda: on(text_field("Target", opts.get("to") or "", hint="root:path, e.g. reports:notes/today.txt"),
                             "to"),
            "path": lambda: on(text_field("Path", opts.get("path") or "", hint="root:path, e.g. reports:2026-11"),
                               "path"),
            "files": lambda: on(text_field("Files", rvm.show_value(opts.get("files")),
                                           hint="${steps.x.output.outputs}, a list, or one root:path"),
                                "files", rvm.parse_value),
            "content": lambda: on(text_field("Content", rvm.show_value(opts.get("content")), multiline=True,
                                             min_lines=3, hint="text, or ${steps.x.output} (written as JSON)"),
                                  "content", rvm.parse_value),
            "glob": lambda: on(text_field("Only names like", opts.get("glob") or "", hint="e.g. *.pdf"), "glob"),
            "recursive": lambda: on(ft.Checkbox(label="Include sub-folders", value=bool(opts.get("recursive"))),
                                    "recursive"),
            "overwrite": lambda: on(dropdown("If it exists", {k: v for k, v in rvm.OVERWRITE_LABELS.items() if k},
                                             opts.get("overwrite") or "never"), "overwrite",
                                    lambda v: None if v == "never" else v),
        }
        out += [fields[f]() for f in rvm.FILE_OP_FIELDS.get(op, ())]
        return out

    def _form_design(self, n: gv.Node, opts: dict, on, put, bind) -> list[ft.Control]:
        pick = self._pick(n, bind, "model", "Model", self.models, "No models yet. Add one on the Models tab.")
        if isinstance(pick, ft.Dropdown):                      # the model changes what it may see: redraw
            first = pick.on_select

            def model_changed(e):
                first(e)
                self.show_inspector()
            pick.on_select = model_changed
        out: list[ft.Control] = [pick]
        name = text_field("Report name", opts.get("report") or "", hint="an existing report, or a new name")
        on(name, "report")
        name.on_blur = lambda _: self.show_inspector()          # new name: show where its data comes from
        out.append(name)
        report = (opts.get("report") or "").strip()
        frm = opts.get("from") if isinstance(opts.get("from"), dict) else {}
        if report and report in self.reports:
            out.append(_help(f"Updates the template of the existing report '{report}'."))
        else:
            f_ep = dropdown("Data from endpoint", {"": "(none)", **{e: e for e in self.get_endpoints}},
                            frm.get("endpoint") or "")
            f_src = dropdown("…or SQL source", {"": "(none)", **{s: s for s in self.sql_sources}}, frm.get("source") or "")
            f_q = text_field("Query (SQL source)", frm.get("query") or "", multiline=True, min_lines=2,
                             hint="one SELECT; bind values as :name")

            def from_changed(_):
                ep, src, q = f_ep.value or "", f_src.value or "", (f_q.value or "").strip()
                put("from", {"endpoint": ep} if ep else ({"source": src, **({"query": q} if q else {})} if src else None))
            for c in (f_ep, f_src):
                c.on_select = c.on_change = from_changed
            f_q.on_change = from_changed
            out += [_help("A new report: say where its data comes from."), f_ep, f_src, f_q]
        out.append(bind("input", text_field("What should the report show?", n.input, multiline=True, min_lines=3,
                                            hint="e.g. Headcount by department as KPI cards and a table")))
        out += self._formats_row(opts, put, "Formats the template must suit")
        from ...reports.author import builtin_templates
        out.append(on(dropdown("Start from", {"": "(nothing)", **{b: b for b in builtin_templates()}},
                               opts.get("base") or ""), "base", lambda v: v or None))
        out.append(on(dropdown("When an AI template exists", {"never": "Reuse it (no model call)",
                                                             "always": "Design again every run"},
                               opts.get("overwrite") or "never"), "overwrite", lambda v: None if v == "never" else v))
        try:
            level, why = rvm.effective_access(self.ctx.svc, n.model or None, report if report in self.reports else None,
                                              frm.get("endpoint"), frm.get("source"))
        except Exception:                                    # noqa: BLE001 - models may not load yet
            level, why = "full", "data access 'full'"
        if level == "none":
            out.append(ft.Text(f"AI authoring is not allowed for this data ({why}).", size=12, color=ft.Colors.ERROR))
        else:
            out.append(on(dropdown("What the model may see", rvm.access_choices(level),
                                   opts.get("data_access") or level),
                          "data_access", lambda v: None if v == level else v))
            out.append(_help(f"Allowed here: {why}. You can only lower it."))
        out.append(on(text_field("Sample rows (optional)", str(opts.get("sample_rows") or ""), width=160),
                      "sample_rows", lambda v: int(v) if str(v).strip().isdigit() else None))
        return out

    def _foreach_fields(self, n: gv.Node, bind) -> list[ft.Control]:
        if n.do not in gv.DO_KINDS:          # report / copy / move per item: edit as YAML
            return [bind("items", text_field("List to loop over", n.items, multiline=True, min_lines=2)),
                    bind("settings", text_field(f"Settings for each item ({n.do}, YAML)", n.settings,
                                                multiline=True, min_lines=4,
                                                hint="use ${item} and ${index}"))]
        def kind_changed(e):
            n.do = e.control.value or "tool"
            self.rebuild()
        kind = dropdown("Run for each item", {"tool": "A tool", "agent": "An agent", "llm": "An LLM prompt",
                                              "load": "A load"}, n.do or "tool", on_select=kind_changed)
        pools = {"tool": ("tool", "Tool", self.tools), "agent": ("agent", "Agent", self.agents),
                 "llm": ("model", "Model", self.models), "load": ("load", "Load", self.loads)}
        field, label, options = pools[n.do or "tool"]
        out = [bind("items", text_field("List to loop over", n.items, multiline=True, min_lines=2,
                                        hint="e.g. ${steps.query.output.rows}")),
               kind, self._pick(n, bind, field, label, options, f"None defined yet for '{label}'.")]
        if n.do != "load":
            out.append(bind("input", text_field("Input for each item", n.input, multiline=True, min_lines=3,
                                                hint="use ${item} and ${index}, e.g. Summarise: ${item}")))
        if n.do == "llm":
            out += [bind("system", text_field("System prompt (optional)", n.system, multiline=True, min_lines=2)),
                    bind("format", dropdown("Reply as", {"text": "Plain text", "json": "JSON"}, n.format or "text"))]
        out.append(bind("max_items", text_field("Most items allowed", n.max_items, width=160,
                                                hint="longer lists stop the step instead of running")))
        out.append(ft.Text("The result is a list, one entry per item. With 'Carry on' on failure, a failed item "
                           "becomes {error: ...}.", size=12, color=ft.Colors.ON_SURFACE_VARIANT))
        return out

    def _cases_field(self, n: gv.Node) -> ft.TextField:
        f = text_field("Cases (one per line)", n.cases, multiline=True, min_lines=3, max_lines=6)

        def changed(_):
            n.cases = f.value or ""

        def commit(_):
            n.cases = f.value or ""
            gv.prune_links(self.graph, n)
            self.rebuild()
        f.on_change = changed
        f.on_blur = commit
        return f

    # ---------------------------------------------------------------- templates
    def load_template(self, template_id: str) -> None:
        """Replace the (new, unsaved) workflow with a template; tool/agent are filled in when there is just one."""
        try:
            t = self.svc.template(template_id)
        except ServiceError as exc:
            self.ctx.fail(exc)
            return
        self.graph = gv.from_steps(t["steps"])
        gv.prefill_choices(self.graph, self.tools, self.agents, self.models, self.loads, self.reports)
        self.base = {**self.base, "description": t["description"]}
        self.selected, self.linking = self.graph.start, None
        if not (self.f_name.value or "").strip():
            self.f_name.value = template_id.replace("-", "_")
            _touch(self.f_name)
        needs = gv.blank_choices(self.graph)
        self.set_hint(t["description"] + (f"  Pick the {', '.join(needs)} on the steps marked 'choose'." if needs else ""))
        self.rebuild()

    # ------------------------------------------------------- tools from sources
    def _quick_tool(self, n: gv.Node) -> list[ft.Control]:
        """Pick a SQL source or an endpoint right on the node; the tool behind it is created for you."""
        cfg = self.ctx.svc.project.config
        sources = [s for s, sp in cfg.sources.items() if sp.type == "sql"]
        endpoints = [e.name for e in cfg.endpoints]
        out: list[ft.Control] = []
        if not self.tools:
            out.append(ft.Text("No tools yet. Pick a SQL source or an endpoint below and the tool is created for you.",
                               size=12, color=ft.Colors.AMBER_800))
        if sources:
            out.append(dropdown("Query a SQL source", sources, None, on_select=lambda e: self.tool_from_source(n, e.control.value)))
        else:
            out.append(ft.Text("No SQL sources yet. Add one on the Sources screen to query it here.", size=12,
                               color=ft.Colors.ON_SURFACE_VARIANT))
        if endpoints:
            out.append(dropdown("Call an endpoint", endpoints, None, on_select=lambda e: self.tool_from_endpoint(n, e.control.value)))
        return out

    def _ensure_tool(self, name: str, spec: dict) -> str:
        if name not in self.ctx.svc.project.config.tools:
            self.svc.set("tool", name, spec)
        self.tools = list(self.ctx.svc.project.config.tools)
        return name

    def tool_from_source(self, n: gv.Node, source: str) -> None:
        try:
            name = self._ensure_tool(f"{source}_sql", {"type": "source", "source": source, "read_only": True,
                                                       "description": f"Read-only SELECT on {source}"})
        except ServiceError as exc:
            self.ctx.fail(exc)
            return
        n.tool = name
        if not (n.input or "").strip():
            n.input = "query: select * from your_table"
        self.rebuild()

    def tool_from_endpoint(self, n: gv.Node, endpoint: str) -> None:
        cfg = self.ctx.svc.project.config
        name = endpoint if endpoint not in cfg.tools else f"{endpoint}_call"
        existing = cfg.tools.get(name)
        if existing is not None and not (existing.type == "endpoint" and existing.endpoint == endpoint):
            name = f"{endpoint}_endpoint"
        try:
            name = self._ensure_tool(name, {"type": "endpoint", "endpoint": endpoint})
        except ServiceError as exc:
            self.ctx.fail(exc)
            return
        n.tool = name
        self.rebuild()

    def refresh_card(self, n: gv.Node) -> None:
        card = self._card(n)
        old = self.cards[n.id]
        old.content = card.content
        self.cards[n.id] = old
        _touch(old)

    def unlink(self, node_id: str, port: str) -> None:
        gv.disconnect(self.graph, node_id, port)
        self.rebuild()

    def make_start(self, node_id: str) -> None:
        gv.set_start(self.graph, node_id)
        self.rebuild()

    def delete(self, node_id: str) -> None:
        gv.delete_node(self.graph, node_id)
        self.selected = self.graph.start
        self.linking = None
        self.rebuild()

    def show_problems(self) -> None:
        probs = gv.problems(self.graph, self.tools, self.agents, self.models, self.loads, self.reports)
        self.problems.controls = [ft.Row([ft.Icon(ft.Icons.WARNING_AMBER, size=16, color=ft.Colors.AMBER_800),
                                          ft.Text(p, size=12)], spacing=6) for p in probs]
        _touch(self.problems)

    # ------------------------------------------------------------------ save
    def save(self, _=None, announce: bool = True) -> bool:
        name = (self.f_name.value or "").strip()
        if not name:
            self.f_name.error = "Name the workflow"
            _touch(self.f_name)
            return False
        steps = gv.to_steps(self.graph)
        try:
            self.svc.set("workflow", name, {**self.base, "steps": steps}, replace=bool(self.name))
        except ServiceError as exc:
            self.ctx.fail(exc)
            return False
        self.name = name
        if announce:
            self.ctx.toast(f"Workflow '{name}' saved")
        return True


def open_editor(ctx: Ctx, name: str | None, on_saved=None) -> WorkflowCanvas:
    ed = WorkflowCanvas(ctx, name, on_saved)

    def save(_):
        if ed.save(announce=False):
            ctx.close()                                       # close first: a toast would sit on top of the dialog
            ctx.toast(f"Workflow '{ed.name}' saved")
            if on_saved:
                on_saved()
    ed.dialog = ctx.dialog("Workflow editor" + (f" – {name}" if name else ""), ed.body(),
                           [ft.TextButton("Cancel", on_click=lambda _: ctx.close()), ft.FilledButton("Save", on_click=save)],
                           width=ed.fit_sizes()[0], scroll=True)
    return ed
