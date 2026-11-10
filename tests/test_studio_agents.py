import asyncio
import json
import sqlite3

import httpx
import pytest

from portwright.agents.llm import LlmProvider, LlmResponse, ToolCall
from portwright.services import Project, ServiceError, Services
from portwright.studio import agent_vm as vm
from portwright.studio.views.agents import AgentsView


class FakeLlm(LlmProvider):
    def __init__(self, script):
        super().__init__("fake", None, httpx.AsyncClient())
        self.script = list(script)

    async def chat(self, system, messages, tools):
        return self.script.pop(0) if len(self.script) > 1 else self.script[0]


class FakeCtx:
    def __init__(self, svc):
        self.svc, self.toasts, self.dialogs, self.errors = svc, [], [], []
        self.page = type("P", (), {"update": lambda self: None, "run_task": lambda self, *a: None})()
        self.help = lambda *a, **k: None
        self.view = lambda k: None
        self.closed = 0

    def background(self, work, done, bar=None):
        done(work())

    def toast(self, msg, error=False):
        (self.errors if error else self.toasts).append(msg)

    def fail(self, exc):
        self.errors.append(str(exc))

    def dialog(self, title, body, actions, **kw):
        self.dialogs.append((title, body, actions))

    info = lambda self, title, body, width=640: self.dialogs.append((title, body, []))      # noqa: E731

    def close(self):
        self.closed += 1

    def confirm(self, title, text, on_yes, **kw):
        on_yes()


@pytest.fixture
def svc(tmp_path):
    s = Services(Project.init(tmp_path, "t"))
    c = sqlite3.connect(tmp_path / "data" / "hr.db")
    c.execute("create table emp(id integer primary key, name text, dept text)")
    c.execute("insert into emp(name, dept) values ('Ann','ENG'), ('Bob','OPS')")
    c.commit()
    c.close()
    s.credentials.add("db", "database", {"driver": "sqlite", "database": str(tmp_path / "data" / "hr.db")})
    s.credentials.add("llm_key", "bearer", {"token": "sk-test"})
    s.sources.add("hr", "sql", credential="db", read_write=True)
    cols = s.sources.columns("hr", "emp")
    s.endpoints.crud_from_table("emp", "hr", "emp", "id", cols, ["name", "dept"], ["dept"], ["list", "get"])
    return s


def model_values(**kw):
    return {"name": "corp", "provider": "openai_compatible", "base_url": "https://llm.corp.local/v1", "model": "gpt-x",
            "credential": "llm_key", "ca_bundle": "", "client_cert": "", "client_key": "", "tls_verify": True,
            "headers": "X-Team=data", "timeout": "60", "max_tokens": "2048", "temperature": "", **kw}


def test_payloads_validate_input(svc):
    p = vm.item_payload("model", model_values(ca_bundle="certs/ca.pem", tls_verify=False))
    assert p["tls"] == {"ca_bundle": "certs/ca.pem", "verify": False} and p["headers"] == {"X-Team": "data"}
    assert p["timeout_seconds"] == 60 and "temperature" not in p
    with pytest.raises(ServiceError, match="Timeout must be a number"):
        vm.item_payload("model", model_values(timeout="soon"))
    with pytest.raises(ServiceError, match="Name=value"):
        vm.item_payload("model", model_values(headers="oops"))
    with pytest.raises(ServiceError, match="start with 'steps:'"):
        vm.item_payload("workflow", {"steps": "- id: a"})
    with pytest.raises(ServiceError, match="workflow YAML"):
        vm.item_payload("workflow", {"steps": "steps: [oops"})
    mcp = vm.item_payload("mcp", {"transport": "stdio", "scopes": "emp:read, x", "endpoints": ["emp-list"],
                                  "loads": [], "sources": "hr: emp, dept\n"})
    assert mcp["scopes"] == ["emp:read", "x"] and mcp["expose"]["sources"] == [
        {"name": "hr", "tables": ["emp", "dept"], "read_only": True}]


def test_save_every_kind_and_edit_roundtrip(svc, tmp_path):
    notes = vm.save_item(svc, "model", {"name": "corp", **model_values()}, editing=False)
    assert any("llm.corp.local" in n for n in notes)
    assert "llm.corp.local" in svc.project.config.security.allowed_llm_hosts
    vm.save_item(svc, "tool", {"name": "emps", "type": "endpoint", "endpoint": "emp-list", "write": False,
                               "max_rows": "50", "description": "list"}, False)
    vm.save_item(svc, "tool", {"name": "sql", "type": "source", "source": "hr", "write": False, "max_rows": "10"}, False)
    vm.save_item(svc, "skill", {"name": "audit", "file": "skills/audit.md", "description": "d", "text": "Step 1"}, False)
    assert (tmp_path / "skills" / "audit.md").read_text() == "Step 1" and svc.agents.skill_text("audit") == "Step 1"
    vm.save_item(svc, "agent", {"name": "bot", "model": "corp", "instructions": "hi", "tools": ["emps", "sql"],
                                "skills": ["audit"], "scopes": "emp:read", "max_steps": "4", "budget": "",
                                "timeout": "60", "description": ""}, False)
    vm.save_item(svc, "workflow", {"name": "wf", "description": "", "scopes": "emp:read", "output": "",
                                   "steps": "steps:\n  - {id: a, type: tool, tool: emps}\n"}, False)
    vm.save_item(svc, "mcp", {"name": "m", "transport": "stdio", "scopes": "emp:read", "endpoints": ["emp-list"],
                              "loads": [], "sources": "hr: emp"}, False)
    v = vm.item_values(svc, "agent", "bot")
    assert v["tools"] == ["emps", "sql"] and v["max_steps"] == "4" and v["scopes"] == "emp:read"
    assert vm.item_values(svc, "mcp", "m")["sources"] == "hr: emp"
    assert vm.item_values(svc, "model", "corp")["headers"] == "X-Team=data"
    assert yaml_steps(vm.item_values(svc, "workflow", "wf")["steps"]) == ["a"]
    with pytest.raises(ServiceError, match="already exists"):
        vm.save_item(svc, "tool", {"name": "emps", "type": "endpoint", "endpoint": "emp-list"}, False)
    with pytest.raises(ServiceError, match="unknown tool"):
        vm.save_item(svc, "agent", {"name": "bad", "model": "corp", "tools": ["ghost"], "skills": []}, False)
    assert [r["name"] for r in vm.summary_rows(svc, "agent")] == ["bot"]
    with pytest.raises(ServiceError, match="inside the project"):
        svc.agents.save_skill_file("../evil.md", "x")
    with pytest.raises(ServiceError, match="still used"):
        svc.agents.remove("tool", "emps")


def yaml_steps(text):
    import yaml
    return [s["id"] for s in yaml.safe_load(text)["steps"]]


def click_save(ctx):
    title, body, actions = ctx.dialogs[-1]
    actions[-1].on_click(None)


def set_value(control, value):
    control.value = value


def test_view_builds_every_section_and_edits(svc):
    ctx = FakeCtx(svc)
    view = AgentsView(ctx)
    view.build()
    for kind, _ in vm.KINDS:
        view.section = kind
        view.refresh(update=False)
    assert ctx.errors == []
    # add a model through the real dialog
    view.section = "model"
    view.edit_dialog("model")
    title, body, actions = ctx.dialogs[-1]
    fields = {getattr(c, "label", None): c for c in body}
    fields["Name"].value = "viewmodel"
    fields["Base URL *"].value = "https://llm.example.com/v1"
    fields["Model / deployment name *"].value = "m1"
    click_save(ctx)
    assert ctx.errors == [] and "viewmodel" in svc.project.config.llm_models
    view.refresh(update=False)
    # editing keeps the name locked and saves changes
    view.edit_dialog("model", "viewmodel")
    assert ctx.dialogs[-1][1][0].disabled
    # every kind's dialog builds
    for kind in ("tool", "skill", "agent", "workflow", "mcp"):
        view.edit_dialog(kind)
    view.check()
    assert ctx.errors == []


def test_run_agent_and_history_from_the_view(svc, monkeypatch):
    vm.save_item(svc, "model", {"name": "corp", **model_values()}, False)
    vm.save_item(svc, "tool", {"name": "emps", "type": "endpoint", "endpoint": "emp-list"}, False)
    vm.save_item(svc, "agent", {"name": "bot", "model": "corp", "tools": ["emps"], "skills": [], "scopes": "emp:read",
                                "max_steps": "4", "timeout": "60"}, False)
    llm = FakeLlm([LlmResponse("", [ToolCall("c1", "emps", {"dept": "OPS"})], 10, 5), LlmResponse("Bob", [], 10, 5)])
    monkeypatch.setattr("portwright.agents.loop.create_provider", lambda *a, **k: llm)
    ctx = FakeCtx(svc)
    view = AgentsView(ctx)
    view.run_agent_dialog("bot")
    title, body, actions = ctx.dialogs[-1]
    task, run_btn = body[0], body[1]
    run_btn.on_click(None)                                   # empty task is refused
    assert task.error
    task.value = "who is in OPS?"
    run_btn.on_click(None)
    out = body[-1]
    flat = json.dumps(vm.agent_steps_rows(svc.agents.run_agent("bot", "again", provider_factory=lambda n, s: FakeLlm(
        [LlmResponse("done", [], 1, 1)]))))
    assert "answer" in flat
    assert out.controls and ctx.errors == []
    runs = svc.agents.runs()
    assert runs and runs[0]["name"] == "bot"
    view.section = "history"
    view.refresh(update=False)
    view.open_run(runs[0]["run_id"])
    assert ctx.errors == [] and ctx.dialogs[-1][0].startswith("Agent")
    traces = [vm.trace_rows(svc.agents.run_trace(r["run_id"])) for r in runs]
    assert any(r[1] == "emps" and r[3].startswith("ok") for rows in traces for r in rows)
    with pytest.raises(ServiceError, match="no saved run"):
        svc.agents.run_trace("nope")


def test_mcp_helpers(svc):
    vm.save_item(svc, "mcp", {"name": "m", "transport": "stdio", "scopes": "emp:read", "endpoints": ["emp-list"],
                              "loads": [], "sources": "hr: emp"}, False)
    cfg = json.loads(vm.claude_desktop_config(svc.project.path, "m"))
    assert cfg["mcpServers"]["m"]["args"][:3] == ["mcp", "serve", "m"]
    ctx = FakeCtx(svc)
    view = AgentsView(ctx)
    view.mcp_tools("m")
    view.mcp_config("m")
    assert ctx.errors == [] and len(ctx.dialogs) == 2
