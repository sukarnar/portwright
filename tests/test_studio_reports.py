"""Studio, phase 5: report/delivery node forms, the destination picker, Design with AI, gallery, templates."""
import asyncio
import json
import sqlite3

import flet as ft
import httpx
import pytest
import yaml

from portwright.agents.llm import LlmProvider, LlmResponse
from portwright.config import ProjectConfig
from portwright.services import Project, ServiceError, Services
from portwright.studio import graph_vm as gv
from portwright.studio import report_vm as rvm

pytest.importorskip("jinja2")

GOOD = ("<html><body><h1>{{ report_name }}</h1>{% for r in data %}<p>{{ r.name }}</p>{% endfor %}"
        "{% if not data %}<p>No records found.</p>{% endif %}</body></html>")


class FakeLlm(LlmProvider):
    def __init__(self, script):
        super().__init__("fake", None, httpx.AsyncClient())
        self.script, self.calls = list(script), []

    async def chat(self, system, messages, tools):
        self.calls.append(messages)
        return self.script.pop(0) if len(self.script) > 1 else self.script[0]


class FakeCtx:
    def __init__(self, svc):
        self.svc, self.toasts, self.dialogs, self.errors, self.infos = svc, [], [], [], []
        self.page = type("P", (), {"update": lambda self: None, "run_task": lambda self, *a: None,
                                   "show_dialog": lambda self, d: None, "pop_dialog": lambda self: None})()
        self.help = lambda *a, **k: None
        self.view = lambda k: None

    def background(self, work, done, busy=None):
        try:
            r = work()
        except Exception as exc:                 # noqa: BLE001 - like the real Ctx
            self.fail(exc)
            return
        done(r)

    def toast(self, msg, error=False):
        (self.errors if error else self.toasts).append(msg)

    def fail(self, exc):
        self.errors.append(str(exc))

    def dialog(self, title, body, actions, **kw):
        self.dialogs.append((title, body, actions))

    def info(self, title, body, width=640):
        self.infos.append((title, body))

    def close(self):
        pass

    def confirm(self, title, text, on_yes, **kw):
        on_yes()


def walk(controls):
    for c in controls:
        yield c
        for attr in ("controls", "content", "rows", "cells"):
            sub = getattr(c, attr, None)
            if isinstance(sub, list):
                yield from walk(sub)
            elif isinstance(sub, ft.Control):
                yield from walk([sub])


def find(controls, label):
    for c in walk(controls):
        if getattr(c, "label", None) == label:
            return c
    raise AssertionError(f"no control labelled {label!r}: "
                         f"{[getattr(c, 'label', None) for c in walk(controls) if getattr(c, 'label', None)]}")


def fire(ctl, value=None):
    if value is not None:
        ctl.value = value
    handler = getattr(ctl, "on_select", None) or getattr(ctl, "on_change", None)
    handler(None)


@pytest.fixture
def svc(tmp_path):
    s = Services(Project.init(tmp_path, "t"))
    db = tmp_path / "data" / "hr.db"
    c = sqlite3.connect(db)
    c.execute("create table emp(id integer primary key, name text, dept text)")
    c.executemany("insert into emp(name, dept) values (?, ?)", [("Ann", "ENG"), ("Bob", "OPS")])
    c.commit()
    c.close()
    s.credentials.add("db", "database", {"driver": "sqlite", "database": str(db)})
    s.sources.add("hr", "sql", credential="db")
    share = tmp_path / "share"
    share.mkdir()
    cfg = s.project.config.model_dump(mode="json", by_alias=True)
    cfg["security"]["allowed_folders"] = [str(share)]
    cfg["destinations"] = {"finance": {"path": str(share)}}
    cfg["llm_models"] = {"local": {"base_url": "https://llm.test/v1", "model": "m"}}
    cfg["reports"] = [{"name": "staff", "from": {"source": "hr", "query": "select name, dept from emp order by id"},
                       "formats": ["csv"]}]
    s.project.config = ProjectConfig.model_validate(cfg)
    s.project.save(force=True)
    s.share = share
    return s


# --------------------------------------------------------------------------- view-model
def test_node_options_and_destination_picker(svc):
    n = gv.Node("c", "copy", settings="files: x\nverify: size")
    rvm.set_opt(n, "manifest", True)
    rvm.set_opt(n, "verify", "sha256")                         # the default: removed
    rvm.set_opt(n, "files", "")                                # empty: removed
    assert yaml.safe_load(n.settings) == {"manifest": True}
    assert rvm.split_to("dest:finance/2026/${run.date.month}") == ("dest:finance", "2026/${run.date.month}")
    assert rvm.split_to("reports:a/b") == ("reports:", "a/b") and rvm.split_to("") == ("", "")
    assert rvm.join_to("dest:finance", "/x/") == "dest:finance/x" and rvm.join_to("reports:", "a") == "reports:a"
    assert rvm.join_to("dest:finance", "") == "dest:finance"
    targets = rvm.delivery_targets(svc.project.config)
    assert list(targets)[0] == "dest:finance" and "reports:" in targets and "templates:" in targets
    assert "data:" not in targets                              # read-only root
    assert rvm.parse_value("[a, b]") == ["a", "b"] and rvm.parse_value("${steps.x.output}") == "${steps.x.output}"


def test_access_choices_and_design_request(svc):
    assert list(rvm.access_choices("full")) == ["full", "sample", "profile", "schema"]
    assert list(rvm.access_choices("profile")) == ["profile", "schema"]
    cfg = svc.project.config
    ok = {"name": "new-one", "prompt": "p", "source": "hr", "query": "select 1", "formats": ["pdf"], "model": "local"}
    req = rvm.design_request(ok, cfg)
    assert req["frm"] == {"source": "hr", "query": "select 1"} and req["if_exists"] == "error"
    assert rvm.design_request({**ok, "name": "staff", "source": "", "query": ""}, cfg)["frm"] is None   # existing
    for bad, msg in [({"name": "1x"}, "Report name"), ({"prompt": ""}, "Say what"),
                     ({"source": ""}, "pick an endpoint or a source"), ({"query": ""}, "needs a query"),
                     ({"formats": []}, "at least one format"), ({"sample_rows": "lots"}, "Sample rows")]:
        with pytest.raises(ServiceError, match=msg):
            rvm.design_request({**ok, **bad}, cfg)
    level, why = rvm.effective_access(svc, "local", "staff")
    assert level == "full" and "project=full" in why


# --------------------------------------------------------------------------- canvas forms
def canvas(svc):
    from portwright.studio.views.workflow_canvas import WorkflowCanvas
    return WorkflowCanvas(FakeCtx(svc), None)


def test_copy_form_writes_options_and_suggests_report_outputs(svc):
    ed = canvas(svc)
    ed.add("report")
    rep = ed.graph.nodes[-1]
    fire(find(ed.inspector.controls, "Report"), "staff")
    assert gv._parse(rep.settings)["report"] == "staff"
    ed.add("copy")
    cp = ed.graph.nodes[-1]
    gv.connect(ed.graph, rep.id, "next", cp.id)
    cp.settings = ""
    ed.selected = cp.id
    ed.show_inspector()
    ctl = ed.inspector.controls
    use = next(c for c in walk(ctl) if isinstance(c, ft.TextButton) and "output.outputs" in str(c.content or c.text))
    use.on_click(None)
    fire(find(ctl, "Deliver to"), "dest:finance")
    fire(find(ctl, "Sub-folder (optional)"), "${run.date.year}")
    fire(find(ctl, "If the file is already there"), "rename")
    fire(find(ctl, "Write manifest.json"), True)
    fire(find(ctl, "Check each copy"), "sha256")
    opts = gv._parse(cp.settings)
    assert opts == {"files": f"${{steps.{rep.id}.output.outputs}}", "to": "dest:finance/${run.date.year}",
                    "overwrite": "rename", "manifest": True}
    assert find(ctl, "All options (YAML)").value == cp.settings
    assert gv.problems(ed.graph, [], [], [], [], ed.reports) == []
    step = next(s for s in gv.to_steps(ed.graph) if s["id"] == cp.id)
    assert step["to"] == "dest:finance/${run.date.year}" and step["manifest"] is True


def test_file_form_shows_fields_for_the_operation(svc):
    ed = canvas(svc)
    ed.add("file")
    n = ed.graph.nodes[-1]
    fire(find(ed.inspector.controls, "Operation"), "zip")
    labels = {getattr(c, "label", None) for c in walk(ed.inspector.controls)}
    assert {"Files", "Target", "If it exists"} <= labels and "Content" not in labels
    fire(find(ed.inspector.controls, "Target"), "reports:archive/x.zip")
    assert gv._parse(n.settings)["op"] == "zip" and gv._parse(n.settings)["to"] == "reports:archive/x.zip"


def test_design_form_limits_data_access(svc):
    cfg = svc.project.config.model_dump(mode="json", by_alias=True)
    cfg["sources"]["hr"]["ai_data_access"] = "profile"
    svc.project.config = ProjectConfig.model_validate(cfg)
    ed = canvas(svc)
    ed.add("report_design")
    n = ed.graph.nodes[-1]
    n.model = "local"
    n.settings = "report: staff"
    ed.show_inspector()
    access = find(ed.inspector.controls, "What the model may see")
    assert [o.key for o in access.options] == ["profile", "schema"]
    fire(access, "schema")
    assert gv._parse(n.settings)["data_access"] == "schema"
    fire(access, "profile")                                   # back to the allowed level: option removed
    assert "data_access" not in gv._parse(n.settings)


def test_formats_from_an_input_stay_text(svc):
    ed = canvas(svc)
    ed.add("report")
    n = ed.graph.nodes[-1]
    n.settings = "report: staff\nformats: ${input.formats}"
    ed.show_inspector()
    f = find(ed.inspector.controls, "Formats")
    assert f.value == "${input.formats}"
    fire(f, "pdf,xlsx")
    assert gv._parse(n.settings)["formats"] == "pdf,xlsx"


# --------------------------------------------------------------------------- template
def test_report_and_deliver_template_runs(svc):
    from portwright.agents.templates import get_template
    from portwright.agents.workflow import run_workflow
    g = gv.from_steps(get_template("report-and-deliver")["steps"])
    assert gv.blank_choices(g) == ["report"]
    gv.prefill_choices(g, [], [], [], [], ["staff"])           # the only report is filled in
    assert gv.problems(g, [], [], [], [], ["staff"]) == []
    cfg = svc.project.config.model_dump(mode="json", by_alias=True)
    cfg["workflows"] = {"w": {"steps": gv.to_steps(g), "scopes": ["reports:run", "fs:reports:read",
                                                                  "fs:reports:write"]}}
    config = ProjectConfig.model_validate(cfg)
    res = asyncio.run(run_workflow(config, svc.project.root, svc.project.credentials(), "w",
                                   {"formats": "csv,json"}, save=False))
    assert res.ok, res.error
    files = res.output["files"]
    assert sorted(f["dest"].rsplit(".", 1)[1] for f in files) == ["csv", "json"]
    folder = next((svc.project.root / "reports/output/delivered").iterdir())
    assert (folder / "manifest.json").exists()


# --------------------------------------------------------------------------- Design with AI / list / gallery
def test_design_dialog_preview_design_and_promote(svc, monkeypatch, tmp_path):
    import portwright.agents.llm as llm_mod
    from portwright.studio.views.report_design import DesignDialog
    from portwright.studio.views.reports import ReportsView
    from portwright.studio.views.template_gallery import TemplateGalleryView
    llm = FakeLlm([LlmResponse(json.dumps({"title": "T", "template": GOOD}), [], 10, 5)])
    monkeypatch.setattr(llm_mod, "create_provider", lambda *a, **k: llm)
    ctx = FakeCtx(svc)
    dlg = DesignDialog(ctx, None)
    dlg.f_name.value = "people"
    dlg.name_changed()
    assert dlg.data_box.visible and dlg.f_model.value == "local"             # one model: preselected
    dlg.f_source.value, dlg.f_query.value = "hr", "select name, dept from emp"
    dlg.f_prompt.value = "People list"
    dlg.f_access.value = "schema"
    dlg.preview()
    title, body = ctx.infos[-1]
    shown = json.loads(body[-1].value)
    assert title == "What the model will see" and shown["data_access"] == "schema" and "rows" not in shown
    dlg.f_access.value = "full"
    for f, box in dlg.formats.items():
        box.value = f in ("html", "csv")
    dlg.design()
    assert ctx.errors == [] and "Saved .portwright/templates/people.html" in dlg.status.value
    spec = svc.project.config.report("people")
    assert spec.formats == ["html", "csv"] and spec.provenance["data_access"] == "full"
    # the list marks it and offers Promote
    view = ReportsView(ctx)
    view.build()
    texts = [getattr(c, "value", None) for c in walk(view.root.controls)]
    assert "AI draft" in texts
    gallery = TemplateGalleryView(ctx, on_close=lambda: None)
    cards = [getattr(c, "value", "") for c in walk(gallery._list_col.controls)]
    assert "people.html" in cards and any("report people" in (t or "") for t in cards)
    assert "“People list”" in cards
    view._promote("people")
    assert svc.project.config.report("people").template == "templates/people.html"
    assert (tmp_path / "templates" / "people.html").exists() and ctx.toasts[-1].startswith("'people' now uses")
    # existing report: data fields hidden, existing AI template needs 'replace'
    dlg2 = DesignDialog(ctx, "staff")
    assert not dlg2.data_box.visible
