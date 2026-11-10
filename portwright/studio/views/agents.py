"""Agents screen: models, tools, skills, agents, workflows, MCP servers, and run history."""
from __future__ import annotations

import json

import flet as ft

from ...services import ServiceError
from ..agent_vm import (KINDS, PROVIDERS, TOOL_TYPES, WORKFLOW_TEMPLATE, agent_steps_rows, choices,
                        claude_desktop_config, item_values, lines, save_item, summary_rows, trace_rows)
from ..common import Ctx, dropdown, empty, header, icon_btn, mono, table, tag, text_field, help_btn

NONE = "(none)"
COLUMNS = {
    "agent": ["Name", "Model", "Uses", "Limit", ""],
    "model": ["Name", "Provider", "URL", "Model", ""],
    "tool": ["Name", "Type", "Target", "Access", ""],
    "skill": ["Name", "File", "Description", "", ""],
    "workflow": ["Name", "Steps", "Step ids", "Scopes", ""],
    "mcp": ["Name", "Transport", "Exposes", "Scopes", ""],
}
EMPTY = {
    "agent": "No agents yet. An agent is a model that uses tools and skills for up to N steps.",
    "model": "No models yet. Add the LLM your agents call (OpenAI-compatible or Anthropic).",
    "tool": "No tools yet. Tools are what agents may call: endpoints, SQL sources, MCP servers.",
    "skill": "No skills yet. A skill is a markdown playbook an agent reads when it applies.",
    "workflow": "No workflows yet. A workflow chains tool, agent and condition steps in order.",
    "mcp": "No MCP servers yet. Publish endpoints and SQL tables to MCP clients such as Claude Desktop.",
}
INTRO = {
    "agent": "Each agent has a model, tools, skills and hard limits (steps, tokens, time). Tool calls run with the "
             "agent's scopes only.",
    "model": "Add the host to the allowed LLM hosts (done on save). API keys come from a bearer / api_key credential.",
    "tool": "Endpoint tools use the normal API pipeline: scopes, rate limit, validation and audit apply.",
    "skill": "Agents see each skill's name and description, and read the full text only when it applies.",
    "workflow": "Steps run in order. Use ${input.KEY} and ${steps.ID.output} to pass data along.",
    "mcp": "Nothing is published unless listed. SQL sources are read-only; limit them to the tables you list.",
}


def _check_group(label: str, options: list[str], selected: list[str]) -> tuple[ft.Control, callable]:
    boxes = [ft.Checkbox(label=o, value=o in selected) for o in options]
    body = (ft.Row(boxes, wrap=True, spacing=16) if boxes
            else ft.Text("none defined yet", size=12, color=ft.Colors.ON_SURFACE_VARIANT))
    col = ft.Column([ft.Text(label, weight=ft.FontWeight.W_600, size=13), body], spacing=4)
    return col, lambda: [b.label for b in boxes if b.value]


def _note(text: str) -> ft.Text:
    return ft.Text(text, size=12, color=ft.Colors.ON_SURFACE_VARIANT)


class AgentsView:
    title = "Agents"

    def __init__(self, ctx: Ctx):
        self.ctx = ctx
        self.section = "agent"
        self.root = ft.Column(expand=True, scroll=ft.ScrollMode.ALWAYS, spacing=16)

    def build(self) -> ft.Control:
        self.refresh(update=False)
        return self.root

    # ------------------------------------------------------------- listing
    def refresh(self, update: bool = True) -> None:
        kind = self.section
        label = dict(KINDS)[kind]
        actions: list[ft.Control] = [help_btn(self.ctx, "agents"),
                                     ft.OutlinedButton("Check setup", icon=ft.Icons.FACT_CHECK, on_click=self.check)]
        if kind == "workflow":
            actions.append(ft.OutlinedButton("Add visually", icon=ft.Icons.ACCOUNT_TREE,
                                             on_click=lambda _: self.visual_editor(None)))
        if kind != "history":
            actions.append(ft.FilledButton("Add", icon=ft.Icons.ADD, on_click=lambda _: self.edit_dialog(kind)))
        controls: list[ft.Control] = [
            header("Agents", "LLM agents, workflows and MCP servers built from your endpoints and sources", actions),
            ft.SegmentedButton(segments=[ft.Segment(value=k, label=ft.Text(t)) for k, t in KINDS],
                               selected=[kind], on_change=self._switch),
            _note(INTRO.get(kind, "Saved runs of agents and workflows. Open one to see every step and tool call."))]
        controls += self._history() if kind == "history" else self._list(kind, label)
        self.root.controls = controls
        if update:
            self.ctx.page.update()

    def _switch(self, e) -> None:
        self.section = (e.control.selected or [self.section])[0]
        self.refresh()

    def _list(self, kind: str, label: str) -> list[ft.Control]:
        try:
            items = summary_rows(self.ctx.svc, kind)
        except ServiceError as exc:
            return [ft.Text(str(exc), color=ft.Colors.ERROR)]
        if not items:
            return [empty(EMPTY[kind], ft.FilledButton(f"Add {label.lower().rstrip('s')}",
                                                       on_click=lambda _: self.edit_dialog(kind)))]
        rows = []
        for it in items:
            n = it["name"]
            acts: list[ft.Control] = []
            if kind == "agent":
                acts.append(icon_btn(ft.Icons.PLAY_ARROW, "Run", lambda _, n=n: self.run_agent_dialog(n)))
            elif kind == "workflow":
                acts += [icon_btn(ft.Icons.PLAY_ARROW, "Run", lambda _, n=n: self.run_workflow_dialog(n)),
                         icon_btn(ft.Icons.ACCOUNT_TREE, "Open in the visual editor", lambda _, n=n: self.visual_editor(n))]
            elif kind == "model":
                acts.append(icon_btn(ft.Icons.NETWORK_CHECK, "Test the connection", lambda _, n=n: self.test_model(n)))
            elif kind == "mcp":
                acts += [icon_btn(ft.Icons.LIST, "Tools it publishes", lambda _, n=n: self.mcp_tools(n)),
                         icon_btn(ft.Icons.CONTENT_COPY, "Claude Desktop setting", lambda _, n=n: self.mcp_config(n))]
            if not it["file"]:
                acts += [icon_btn(ft.Icons.EDIT, "Edit", lambda _, n=n: self.edit_dialog(kind, n)),
                         icon_btn(ft.Icons.DELETE, "Delete", lambda _, n=n: self.delete(kind, n), ft.Colors.ERROR)]
            else:
                acts.append(tag("from file", "neutral"))
            rows.append([ft.Text(n, weight=ft.FontWeight.W_600), ft.Text(it["a"]),
                         ft.Text(it["b"] or "–", max_lines=1, overflow=ft.TextOverflow.ELLIPSIS, width=280),
                         ft.Text(it["c"]), ft.Row(acts, spacing=0)])
        return [table(COLUMNS[kind], rows)]

    # --------------------------------------------------------- add / edit
    def edit_dialog(self, kind: str, name: str | None = None) -> None:
        svc, ch = self.ctx.svc, choices(self.ctx.svc)
        try:
            v = item_values(svc, kind, name) if name else {}
        except ServiceError as exc:
            self.ctx.fail(exc)
            return
        f_name = text_field("Name", name or "", hint="letters, digits, - and _", disabled=bool(name))
        body, collect = getattr(self, f"_form_{kind}")(v, ch)

        def save(_):
            values = {"name": f_name.value, **collect()}
            try:
                notes = save_item(svc, kind, values, editing=bool(name))
            except ServiceError as exc:
                self.ctx.fail(exc)
                return
            self.ctx.close()
            self.ctx.toast(f"{dict(KINDS)[kind].rstrip('s')} '{values['name'].strip()}' saved"
                           + (" – " + "; ".join(notes) if notes else ""))
            self.refresh()

        width = 760 if kind in ("workflow", "skill") else 620
        self.ctx.dialog(("Edit " if name else "Add ") + dict(KINDS)[kind].lower().rstrip("s"), [f_name, *body],
                        [ft.TextButton("Cancel", on_click=lambda _: self.ctx.close()),
                         ft.FilledButton("Save", on_click=save)], width=width)

    def _form_model(self, v: dict, ch: dict):
        f_provider = dropdown("Provider", PROVIDERS, v.get("provider", "openai_compatible"))
        f_url = text_field("Base URL *", v.get("base_url", ""), hint="https://llm.corp.local/v1")
        f_model = text_field("Model / deployment name *", v.get("model", ""), hint="e.g. gpt-4o")
        f_cred = dropdown("API key (bearer or api_key credential)", [NONE] + ch["credentials"],
                          v.get("credential") or NONE)
        f_ca = text_field("Corporate CA certificate (PEM, optional)", v.get("ca_bundle", ""), hint="certs/corp-ca.pem")
        f_cert = text_field("Client certificate (mutual TLS, optional)", v.get("client_cert", ""))
        f_key = text_field("Client private key", v.get("client_key", ""))
        f_verify = ft.Switch(label="Verify the server certificate (turn off only for testing)",
                             value=bool(v.get("tls_verify", True)))
        f_headers = text_field("Extra headers (Name=value per line)", v.get("headers", ""), multiline=True, min_lines=2,
                               hint="secrets: ${CRED:name.field}")
        f_timeout = text_field("Timeout (s)", v.get("timeout", "120"), width=140)
        f_tokens = text_field("Max tokens", v.get("max_tokens", "4096"), width=140)
        f_temp = text_field("Temperature", v.get("temperature", ""), width=140, hint="optional")
        body = [f_provider, f_url, f_model, f_cred, _note("Files are relative to the project folder."), f_ca,
                ft.Row([f_cert, f_key]), f_verify, f_headers, ft.Row([f_timeout, f_tokens, f_temp])]
        return body, lambda: {"provider": f_provider.value, "base_url": f_url.value, "model": f_model.value,
                              "credential": None if f_cred.value == NONE else f_cred.value, "ca_bundle": f_ca.value,
                              "client_cert": f_cert.value, "client_key": f_key.value, "tls_verify": f_verify.value,
                              "headers": f_headers.value, "timeout": f_timeout.value, "max_tokens": f_tokens.value,
                              "temperature": f_temp.value}

    def _form_tool(self, v: dict, ch: dict):
        f_type = dropdown("Type", TOOL_TYPES, v.get("type", "endpoint"))
        f_ep = dropdown("Endpoint", ch["endpoints"], v.get("endpoint") or (ch["endpoints"] or [None])[0])
        f_src = dropdown("SQL source", ch["sql_sources"], v.get("source") or (ch["sql_sources"] or [None])[0])
        f_srv = dropdown("MCP server", ch["mcp_servers"], v.get("server") or (ch["mcp_servers"] or [None])[0])
        f_write = ft.Switch(label="Allow non-SELECT statements (needs a source that allows writes)",
                            value=bool(v.get("write")))
        f_rows = text_field("Rows returned per call", v.get("max_rows", "200"), width=200)
        f_desc = text_field("Description (what the model sees)", v.get("description", ""), multiline=True, min_lines=2)
        groups = {
            "endpoint": [f_ep] if ch["endpoints"] else [_note("No endpoints yet. Create one on the Endpoints screen.")],
            "source": [f_src, f_write, f_rows] if ch["sql_sources"] else [
                _note("No SQL sources yet. Add a database source on the Sources screen, then come back.")],
            "mcp": [f_srv] if ch["mcp_servers"] else [_note("No MCP servers yet. Define one on the MCP servers tab.")]}
        holder = ft.Column(groups[f_type.value], spacing=10)

        def switch(_=None):
            holder.controls = groups[f_type.value]
            self.ctx.page.update()
        f_type.on_select = switch
        return [f_type, holder, f_desc], lambda: {
            "type": f_type.value, "endpoint": f_ep.value, "source": f_src.value, "server": f_srv.value,
            "write": f_write.value, "max_rows": f_rows.value, "description": f_desc.value}

    def _form_skill(self, v: dict, ch: dict):
        f_file = text_field("File *", v.get("file", ""), hint="skills/payroll_audit.md (inside the project)")
        f_desc = text_field("Description (shown to the model)", v.get("description", ""))
        f_text = text_field("Skill text (markdown)", v.get("text", ""), multiline=True, min_lines=10, max_lines=18,
                            hint="Saved to the file above. Leave empty to keep an existing file.")
        return [f_file, f_desc, f_text], lambda: {"file": f_file.value, "description": f_desc.value, "text": f_text.value}

    def _form_agent(self, v: dict, ch: dict):
        f_model = dropdown("Model *", ch["models"], v.get("model") or (ch["models"] or [None])[0])
        f_instr = text_field("Instructions (system prompt)", v.get("instructions", ""), multiline=True, min_lines=4)
        g_tools, tools = _check_group("Tools it may use", ch["tools"], v.get("tools", []))
        g_skills, skills = _check_group("Skills it may read", ch["skills"], v.get("skills", []))
        f_scopes = text_field("Scopes for tool calls (comma separated)", v.get("scopes", ""), hint="e.g. emp:read")
        f_steps = text_field("Max steps", v.get("max_steps", "8"), width=140)
        f_budget = text_field("Token budget", v.get("budget", ""), width=160, hint="optional")
        f_time = text_field("Timeout (s)", v.get("timeout", "300"), width=140)
        f_desc = text_field("Description", v.get("description", ""))
        hint = (_note("No models yet – add one on the Models tab first.") if not ch["models"] else
                _note("An agent can only call endpoints its scopes allow."))
        return [f_model, f_instr, g_tools, g_skills, f_scopes, ft.Row([f_steps, f_budget, f_time]), f_desc, hint], \
            lambda: {"model": f_model.value, "instructions": f_instr.value, "tools": tools(), "skills": skills(),
                     "scopes": f_scopes.value, "max_steps": f_steps.value, "budget": f_budget.value,
                     "timeout": f_time.value, "description": f_desc.value}

    def _form_workflow(self, v: dict, ch: dict):
        f_desc = text_field("Description", v.get("description", ""))
        f_scopes = text_field("Scopes for the workflow's own tool steps", v.get("scopes", ""), hint="e.g. emp:read")
        f_steps = text_field("Steps (YAML)", v.get("steps", WORKFLOW_TEMPLATE), multiline=True, min_lines=12,
                             max_lines=20, text_style=ft.TextStyle(font_family="Consolas", size=13))
        f_out = text_field("Output (optional, YAML)", v.get("output", ""), hint='e.g. "${steps.review.output}"')
        avail = _note("Tools: " + (", ".join(ch["tools"]) or "none") + "   Agents: " + (", ".join(ch["agents"]) or "none"))
        return [f_desc, f_scopes, avail, f_steps, f_out], lambda: {
            "description": f_desc.value, "scopes": f_scopes.value, "steps": f_steps.value, "output": f_out.value}

    def _form_mcp(self, v: dict, ch: dict):
        f_desc = text_field("Description", v.get("description", ""))
        f_tr = dropdown("Transport", {"stdio": "stdio (started by the MCP client, e.g. Claude Desktop)",
                                      "http": "http (needs an API key header)"}, v.get("transport", "stdio"))
        f_scopes = text_field("Scopes the server acts with", v.get("scopes", ""), hint="e.g. emp:read")
        g_ep, eps = _check_group("Endpoints to publish", ch["endpoints"], v.get("endpoints", []))
        f_src = text_field("SQL sources and their tables (one per line: source: table1, table2)", v.get("sources", ""),
                           multiline=True, min_lines=2, hint="available sources: " + (", ".join(ch["sql_sources"]) or "none"))
        g_loads, loads = _check_group("Loads to publish as run_load_<name>", ch["loads"], v.get("loads", []))
        return [f_desc, f_tr, f_scopes, g_ep, f_src, g_loads], lambda: {
            "description": f_desc.value, "transport": f_tr.value, "scopes": f_scopes.value, "endpoints": eps(),
            "sources": f_src.value, "loads": loads()}

    # ------------------------------------------------------------- actions
    def delete(self, kind: str, name: str) -> None:
        def go():
            self.ctx.svc.agents.remove(kind, name)
            self.ctx.toast(f"'{name}' deleted")
            self.refresh()
        self.ctx.confirm("Delete?", f"'{name}' will be removed from the project.", go)

    def check(self, _=None) -> None:
        problems = self.ctx.svc.agents.validate()
        body = ([ft.Row([ft.Icon(ft.Icons.CHECK_CIRCLE, color=ft.Colors.GREEN), ft.Text("Everything checks out.")])]
                if not problems else [ft.Row([ft.Icon(ft.Icons.ERROR, color=ft.Colors.ERROR, size=18),
                                              ft.Text(p, expand=True)], vertical_alignment=ft.CrossAxisAlignment.START)
                                      for p in problems])
        self.ctx.info("Check setup", body, width=640)

    def test_model(self, name: str) -> None:
        self.ctx.toast(f"Calling '{name}'…")

        def done(r):
            self.ctx.toast(f"'{name}' answered: {r['reply'][:80]!r} ({r['tokens']} tokens)")
        self.ctx.background(lambda: self.ctx.svc.agents.test_model(name), done)

    def visual_editor(self, name: str | None) -> None:
        from .workflow_canvas import open_editor
        try:
            open_editor(self.ctx, name, on_saved=self.refresh)
        except ServiceError as exc:
            self.ctx.fail(exc)

    def run_agent_dialog(self, name: str) -> None:
        task = text_field("Task", "", multiline=True, min_lines=3, hint="What should the agent do?")
        bar = ft.ProgressBar(visible=False)
        log = ft.Column(spacing=2)
        out = ft.Column(spacing=10)

        def go(_):
            if not (task.value or "").strip():
                task.error = "Describe the task"
                self.ctx.page.update()
                return
            task.error = None
            log.controls, out.controls = [], []

            def on_step(st):
                for c in st.tool_calls:
                    log.controls.append(ft.Text(f"step {st.n}: {c.name}  {'ok' if c.ok else 'ERROR'}", size=12,
                                                color=None if c.ok else ft.Colors.ERROR))
                self.ctx.page.update()

            def done(r):
                head = [tag(r.stopped_by, "good" if r.ok else "bad"), tag(f"{len(r.steps)} steps"),
                        tag(f"{r.tokens} tokens"), tag(f"{r.seconds}s")]
                out.controls = [ft.Row(head, wrap=True, spacing=6)]
                if r.error:
                    out.controls.append(ft.Text(r.error, color=ft.Colors.ERROR))
                if r.answer:
                    out.controls.append(ft.Text(r.answer, selectable=True))
                rows = agent_steps_rows(r)
                if any(x[1] != "answer" for x in rows):
                    out.controls.append(table(["Step", "Tool", "Arguments", "Result"],
                                              [[ft.Text(c, size=12, max_lines=3, overflow=ft.TextOverflow.ELLIPSIS)
                                                for c in row] for row in rows]))
                out.controls.append(_note(f"Saved as run {r.run_id}"))
            self.ctx.background(lambda: self.ctx.svc.agents.run_agent(name, task.value, on_step=on_step), done, bar)

        self.ctx.dialog(f"Run agent '{name}'", [task, ft.FilledButton("Run", icon=ft.Icons.PLAY_ARROW, on_click=go),
                                                bar, log, out],
                        [ft.TextButton("Close", on_click=lambda _: self.ctx.close())], width=820, modal=False)

    def run_workflow_dialog(self, name: str) -> None:
        try:
            declared = self.ctx.svc.agents.workflow_inputs(name)
        except ServiceError:
            declared = {}
        prefill = "\n".join(f"{k}={'' if d['default'] is None else d['default']}" for k, d in declared.items())
        inp = text_field("Input (KEY=VALUE per line, read as ${input.KEY})", prefill, multiline=True, min_lines=2,
                         hint="; ".join(f"{k}: {d['description']}" for k, d in declared.items() if d["description"]) or None)
        bar = ft.ProgressBar(visible=False)
        out = ft.Column(spacing=10)

        def go(_):
            data = {}
            for ln in (inp.value or "").splitlines():
                if ln.strip():
                    if "=" not in ln:
                        inp.error = f"'{ln.strip()}': use KEY=VALUE"
                        self.ctx.page.update()
                        return
                    k, x = ln.split("=", 1)
                    data[k.strip()] = x.strip()
            inp.error = None
            out.controls = []

            def done(r):
                out.controls = [ft.Row([tag(r.status, "good" if r.ok else "bad")]),
                                *([ft.Text(r.error, color=ft.Colors.ERROR)] if r.error else []),
                                table(["Step", "Type", "Result", "Seconds"],
                                      [[ft.Text(s.id), ft.Text(s.type),
                                        ft.Text("ok" if s.ok else "FAILED " + s.error, max_lines=2), ft.Text(str(s.seconds))]
                                       for s in r.steps])]
                result = r.output if isinstance(r.output, str) else json.dumps(r.output, indent=2, default=str)
                out.controls += [ft.Text("Output", weight=ft.FontWeight.W_600), mono(result or "–")]
            self.ctx.background(lambda: self.ctx.svc.agents.run_workflow(name, data), done, bar)

        self.ctx.dialog(f"Run workflow '{name}'", [inp, ft.FilledButton("Run", icon=ft.Icons.PLAY_ARROW, on_click=go),
                                                   bar, out],
                        [ft.TextButton("Close", on_click=lambda _: self.ctx.close())], width=820, modal=False)

    def mcp_tools(self, name: str) -> None:
        try:
            tools = self.ctx.svc.agents.mcp_tools(name)
        except ServiceError as exc:
            self.ctx.fail(exc)
            return
        self.ctx.info(f"Tools published by '{name}'", [
            table(["Tool", "Kind", "What"], [[ft.Text(t["tool"]), ft.Text(t["kind"]), ft.Text(t["description"])]
                                             for t in tools]) if tools else ft.Text("Nothing is exposed yet.")])

    def mcp_config(self, name: str) -> None:
        from .help import code_block
        snippet = claude_desktop_config(self.ctx.svc.project.path, name)
        self.ctx.info("Use it from Claude Desktop", [
            _note("Add this under \"mcpServers\" in claude_desktop_config.json, then restart Claude Desktop. "
                  "Needs: pip install \"portwright[agents]\"."), code_block(snippet, "json", self.ctx),
            _note(f"Or run it yourself: portwright mcp serve {name}")], width=700)

    # -------------------------------------------------------------- history
    def _history(self) -> list[ft.Control]:
        runs = self.ctx.svc.agents.runs(50)
        if not runs:
            return [empty("No runs yet. Run an agent or workflow and it shows up here.")]
        rows = [[ft.Text(r["run_id"], size=12), ft.Text(r["kind"] or ""), ft.Text(r["name"] or ""),
                 tag(str(r["stopped_by"]), "good" if r["stopped_by"] in ("final", "ok") else "bad"),
                 ft.Text(str(r["tokens"] or "–")),
                 icon_btn(ft.Icons.VISIBILITY, "Open", lambda _, i=r["run_id"]: self.open_run(i))] for r in runs]
        return [table(["Run", "Kind", "Name", "Ended", "Tokens", ""], rows)]

    def open_run(self, run_id: str) -> None:
        try:
            t = self.ctx.svc.agents.run_trace(run_id)
        except ServiceError as exc:
            self.ctx.fail(exc)
            return
        head = [tag(str(t.get("stopped_by") or t.get("status")), "neutral"),
                *([tag(f"{t['tokens']} tokens")] if t.get("tokens") else [])]
        final = t.get("answer") if t.get("kind") == "agent" else t.get("output")
        body: list[ft.Control] = [ft.Row(head, spacing=6)]
        if t.get("task") or t.get("input"):
            body.append(_note("Task: " + str(t.get("task") or t.get("input"))))
        if t.get("error"):
            body.append(ft.Text(str(t["error"]), color=ft.Colors.ERROR))
        if final:
            body.append(ft.Text(final if isinstance(final, str) else json.dumps(final, indent=2, default=str),
                                selectable=True))
        cols = ["Id", "Type", "Result", "Detail"] if t.get("kind") == "workflow" else ["Step", "Tool", "Arguments", "Result"]
        body.append(table(cols, [[ft.Text(c, size=12, max_lines=3, overflow=ft.TextOverflow.ELLIPSIS) for c in row]
                                 for row in trace_rows(t)]))
        self.ctx.info(f"{t.get('kind', 'run').capitalize()} '{t.get('name')}'", body, width=860)
