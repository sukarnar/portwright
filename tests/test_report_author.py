"""AI report authoring: data access policy, validation + repair, storage, promote, ask, report_design step."""
import asyncio
import json
import sqlite3

import httpx
import pytest

from portwright.agents.llm import LlmProvider, LlmResponse
from portwright.config import ProjectConfig
from portwright.reports.author import ReportAuthor, check_template
from portwright.services import Project, ServiceError, Services

pytest.importorskip("jinja2")

GOOD = ("<!DOCTYPE html><html><head><style>@page { size: A4; } td { padding: 4px; }</style></head><body>"
        "<h1>{{ report_name }}</h1>{% if data %}<table>{% for r in data %}<tr><td>{{ r.name }}</td>"
        "<td>{{ r.dept }}</td></tr>{% endfor %}</table>{% else %}<p>No records found.</p>{% endif %}</body></html>")


def reply(template=GOOD, **kw):
    return LlmResponse(json.dumps({"title": "Staff", "description": "Staff by dept", "template": template, **kw}),
                       [], 100, 50)


class FakeLlm(LlmProvider):
    def __init__(self, script):
        super().__init__("fake", None, httpx.AsyncClient())
        self.script, self.calls = list(script), []

    async def chat(self, system, messages, tools):
        self.calls.append({"system": system, "messages": [dict(m) for m in messages]})
        return self.script.pop(0) if len(self.script) > 1 else self.script[0]


@pytest.fixture
def svc(tmp_path):
    s = Services(Project.init(tmp_path, "t"))
    db = tmp_path / "data" / "hr.db"
    c = sqlite3.connect(db)
    c.execute("create table emp(id integer primary key, name text, dept text, salary real, ssn text)")
    c.executemany("insert into emp(name, dept, salary, ssn) values (?, ?, ?, ?)",
                  [("Ann", "ENG", 100.0, "111-11"), ("Bob", "OPS", 90.0, "222-22"), ("Cy", "ENG", 80.0, "333-33")])
    c.commit()
    c.close()
    s.credentials.add("db", "database", {"driver": "sqlite", "database": str(db)})
    s.sources.add("hr", "sql", credential="db")
    cfg = s.project.config.model_dump(mode="json", by_alias=True)
    cfg["llm_models"] = {"local": {"base_url": "https://llm.test/v1", "model": "m"}}
    cfg["sources"]["hr"]["ai_exclude_columns"] = ["ssn"]
    s.project.config = ProjectConfig.model_validate(cfg)
    s.project.save(force=True)
    return s


FROM = {"source": "hr", "query": "select name, dept, salary, ssn from emp order by id"}


def design(svc, llm, name="staff", **kw):
    kw.setdefault("frm", FROM)
    author = ReportAuthor(svc.project, provider_factory=lambda n, s: llm)
    return asyncio.run(author.design(name, "Staff by department", **kw))


def sent(llm, i=0):
    return json.loads(llm.calls[i]["messages"][0]["content"])


# --------------------------------------------------------------------------- design + policy
def test_design_saves_template_registers_report_and_runs(svc, tmp_path):
    llm = FakeLlm([reply(vars={"company": "Acme"})])
    d = design(svc, llm, formats=["html", "csv"])
    assert d.action == "designed" and d.template == ".portwright/templates/staff.html"
    assert (tmp_path / ".portwright/templates/staff.html").read_text().startswith("<!DOCTYPE html>")
    spec = svc.project.config.report("staff")
    assert spec.template == d.template and spec.formats == ["html", "csv"] and spec.vars == {"company": "Acme"}
    prov = spec.provenance
    assert prov["model"] == "local" and prov["data_access"] == "full" and prov["draft"] is True
    assert prov["columns_sent"] == ["name", "dept", "salary"] and prov["excluded_columns"] == ["ssn"]
    # the reloaded file has it too
    assert Project(svc.project.path).config.report("staff").provenance["rows_sent"] == 3
    res = svc.reports.run("staff")
    assert "Ann" in res.outputs[0].path.read_text()
    # default: real rows go to the (local) model, the excluded column never does
    payload = sent(llm)
    assert payload["data"]["rows"][0] == {"name": "Ann", "dept": "ENG", "salary": 100.0}
    assert "ssn" not in llm.calls[0]["messages"][0]["content"] and "111-11" not in llm.calls[0]["messages"][0]["content"]
    assert "Filters" in llm.calls[0]["system"] and "`sum_col`" in llm.calls[0]["system"]
    audit = (tmp_path / "logs" / "audit.jsonl").read_text()
    assert '"event": "report_ai_author"' in audit and '"outcome": "saved"' in audit


@pytest.mark.parametrize("where, level", [
    ({"report_ai": {"data_access": "profile"}}, "profile"),
    ({"llm_models": {"local": {"base_url": "https://llm.test/v1", "model": "m", "data_access": "schema"}}}, "schema"),
    ({"sources": {"hr": {"ai_data_access": "sample"}}}, "sample"),
])
def test_ceilings_lower_what_the_model_sees(svc, where, level):
    cfg = svc.project.config.model_dump(mode="json", by_alias=True)
    for k, v in where.items():
        if k == "sources":
            cfg["sources"]["hr"].update(v["hr"])
        else:
            cfg[k] = v
    svc.project.config = ProjectConfig.model_validate(cfg)
    llm = FakeLlm([reply()])
    d = design(svc, llm, data_access="full")                # an override cannot raise the level
    assert d.data_access == level
    data = sent(llm)["data"]
    assert ("rows" in data) == (level in ("sample", "full"))
    cols = {c["name"]: c for c in data["columns"]}
    assert cols["salary"]["type"] == "number"
    assert ("min" in cols["salary"]) == (level != "schema")
    assert "Ann" not in json.dumps(data) or level in ("sample", "full")


def test_override_lowers_and_none_refuses(svc):
    llm = FakeLlm([reply()])
    d = design(svc, llm, data_access="schema", sample_rows=1)
    assert d.data_access == "schema" and d.rows_sent == 0
    llm2 = FakeLlm([reply()])
    with pytest.raises(ServiceError, match="not allowed"):
        design(svc, llm2, name="nope", data_access="none")
    assert llm2.calls == []                                   # refused before any model call
    with pytest.raises(ServiceError, match="unknown data access"):
        design(svc, FakeLlm([reply()]), name="bad", data_access="everything")


def test_sample_level_truncates_long_values(svc):
    llm = FakeLlm([reply()])
    design(svc, llm, rows=[{"name": "x" * 500, "dept": "ENG"}], data_access="sample", frm=FROM)
    assert len(sent(llm)["data"]["rows"][0]["name"]) == 201


# --------------------------------------------------------------------------- validation + repair
def test_bad_template_is_sent_back_and_repaired(svc):
    llm = FakeLlm([reply(GOOD.replace("</body>", "<script>x()</script></body>")), reply()])
    d = design(svc, llm)
    assert d.repairs == 1 and len(llm.calls) == 2 and d.tokens == 300
    fix = llm.calls[1]["messages"][-1]["content"]
    assert "no <script> elements" in fix and llm.calls[1]["messages"][1]["role"] == "assistant"


def test_repairs_exhausted_keeps_a_draft_and_saves_nothing(svc, tmp_path):
    llm = FakeLlm([reply("{{ data[0].nosuch }}")])
    with pytest.raises(ServiceError, match="still fails after 2 repair"):
        design(svc, llm)
    assert len(llm.calls) == 3
    assert not (tmp_path / ".portwright/templates/staff.html").exists()
    assert svc.project.config.report("staff") is None
    assert list((tmp_path / ".portwright/drafts").glob("staff.*.html"))
    llm = FakeLlm([LlmResponse("sure, here is your report!", [], 1, 1)])
    with pytest.raises(ServiceError, match="not one JSON object"):
        design(svc, llm)


@pytest.mark.parametrize("snippet, msg", [
    ('<img src="https://x.test/a.png">', "src= may only use data: URLs"),
    ('<img src="logo.png">', "src= may only use data: URLs"),
    ("<style>body{background:url(http://x.test/b.png)}</style>", "url()"),
    ("<style>@import 'x.css';</style>", "@import"),
    ('<link rel="stylesheet" href="x.css">', "<link>"),
    ('<div onclick="x()">', "on...="),
    ("{% include 'other.html' %}", "include"),
    ("{{ ''.__class__.__mro__ }}", "rendering"),
    ("{% for r in data %}", "syntax"),
])
def test_static_and_render_checks(snippet, msg):
    errors, _ = check_template(f"<html><body>{snippet}</body></html>", [{"a": 1}])
    assert errors and msg in errors[0]


def test_inline_data_images_are_fine_and_empty_data_must_render():
    ok = '<img src="data:image/png;base64,AAAA"><p>{{ row_count }}</p>'
    assert check_template(ok, [{"a": 1}]) == ([], [])
    errors, _ = check_template("{{ data[0].a }}", [{"a": 1}])
    assert errors and "no rows" in errors[0]


# --------------------------------------------------------------------------- storage
def test_existing_template_rules(svc, tmp_path):
    design(svc, FakeLlm([reply()]))
    with pytest.raises(ServiceError, match="already exists"):
        design(svc, FakeLlm([reply()]))
    llm = FakeLlm([reply()])
    assert design(svc, llm, if_exists="keep").action == "kept" and llm.calls == []
    design(svc, FakeLlm([reply(GOOD.replace("<h1>", "<h1 class='v2'>"))]), if_exists="replace")
    assert "v2" in (tmp_path / ".portwright/templates/staff.html").read_text()
    assert len(list((tmp_path / ".portwright/templates/.history").glob("staff.*.html"))) == 1


def test_hand_written_template_needs_force(svc, tmp_path):
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates" / "mine.html").write_text("<p>{{ row_count }}</p>")
    cfg = svc.project.config.model_dump(mode="json", by_alias=True)
    cfg["reports"] = [{"name": "mine", "template": "templates/mine.html", "from": FROM, "formats": ["html"]}]
    svc.project.config = ProjectConfig.model_validate(cfg)
    with pytest.raises(ServiceError, match="hand-written template"):
        design(svc, FakeLlm([reply()]), name="mine")
    design(svc, FakeLlm([reply()]), name="mine", if_exists="replace")
    assert (tmp_path / "templates/mine.html").read_text() == "<p>{{ row_count }}</p>"     # untouched


def test_promote_and_validate_warning(svc, tmp_path):
    design(svc, FakeLlm([reply()]), formats=["html"])
    assert any("promote" in w for w in svc.runtime.validate().warnings)
    author = ReportAuthor(svc.project)
    dest = author.promote("staff")
    assert dest == tmp_path / "templates" / "staff.html" and dest.read_text().startswith("<!DOCTYPE")
    spec = svc.project.config.report("staff")
    assert spec.template == "templates/staff.html" and spec.provenance["draft"] is False
    assert spec.provenance["promoted_from"] == ".portwright/templates/staff.html"
    assert not any("promote" in w for w in svc.runtime.validate().warnings)
    with pytest.raises(ServiceError, match="does not use an AI template"):
        author.promote("staff")
    assert svc.reports.run("staff").row_count == 3


def test_init_adds_portwright_to_existing_gitignore(tmp_path):
    (tmp_path / "p").mkdir()
    (tmp_path / "p" / ".gitignore").write_text("node_modules/\n")
    Project.init(tmp_path / "p", "x")
    assert ".portwright/" in (tmp_path / "p" / ".gitignore").read_text().splitlines()


# --------------------------------------------------------------------------- ask
def test_ask_plans_designs_and_runs(svc):
    plan = {"name": "eng staff!", "title": "ENG staff",
            "from": {"source": "hr", "query": "select name, dept from emp where dept = :d", "params": {"d": "ENG"}},
            "formats": ["csv", "html"], "design_prompt": "Table of ENG staff"}
    bad = {**plan, "from": {"endpoint": "nope"}}
    llm = FakeLlm([LlmResponse(json.dumps(bad), [], 5, 5), LlmResponse(json.dumps(plan), [], 5, 5), reply()])
    out = asyncio.run(ReportAuthor(svc.project, provider_factory=lambda n, s: llm).ask("ENG staff list"))
    assert out["plan"]["name"] == "eng-staff" and out["design"].action == "designed"
    assert "not in the catalog" in llm.calls[1]["messages"][-1]["content"]
    catalog = json.loads(llm.calls[0]["messages"][0]["content"])["catalog"]
    assert [c["name"] for c in catalog["sql_sources"][0]["tables"][0]["columns"]] == ["id", "name", "dept", "salary"]
    assert [o.format for o in out["run"].outputs] == ["csv", "html"] and out["run"].row_count == 2
    with pytest.raises(ServiceError, match="already exists"):
        asyncio.run(ReportAuthor(svc.project, provider_factory=lambda n, s: llm).ask("x", name="eng-staff"))


def test_ask_skips_sources_closed_to_ai(svc):
    cfg = svc.project.config.model_dump(mode="json", by_alias=True)
    cfg["sources"]["hr"]["ai_data_access"] = "none"
    svc.project.config = ProjectConfig.model_validate(cfg)
    with pytest.raises(ServiceError, match="no endpoint or SQL source"):
        asyncio.run(ReportAuthor(svc.project, provider_factory=lambda n, s: FakeLlm([reply()])).ask("anything"))


# --------------------------------------------------------------------------- workflow step
def test_report_design_step_then_report_step(svc):
    from portwright.agents.workflow import run_workflow
    cfg = svc.project.config.model_dump(mode="json", by_alias=True)
    cfg["workflows"] = {"wf": {"scopes": ["reports:write", "reports:run"], "steps": [
        {"id": "design", "type": "report_design", "model": "local", "report": "staff", "from": FROM,
         "formats": ["html"], "input": "Staff for ${input.who}"},
        {"id": "build", "type": "report", "report": "staff"},
    ]}}
    config = ProjectConfig.model_validate(cfg)
    llm = FakeLlm([reply()])

    def go():
        return asyncio.run(run_workflow(config, svc.project.root, svc.project.credentials(), "wf", {"who": "HR"},
                                        provider_factory=lambda n, s: llm, save=False))
    res = go()
    assert res.ok, res.error
    assert res.steps[0].output["action"] == "designed" and res.steps[0].tokens == 150
    assert sent(llm)["request"] == "Staff for HR"
    assert res.steps[1].output["outputs"][0]["format"] == "html"
    res = go()                                               # overwrite: never → the template is reused
    assert res.ok and res.steps[0].output["action"] == "kept" and len(llm.calls) == 1
    cfg["workflows"]["wf"]["scopes"] = ["reports:run"]
    res = asyncio.run(run_workflow(ProjectConfig.model_validate(cfg), svc.project.root, svc.project.credentials(),
                                   "wf", {"who": "HR"}, provider_factory=lambda n, s: llm, save=False))
    assert not res.ok and "reports:write" in res.error


def test_report_design_step_validation(svc):
    base = svc.project.config.model_dump(mode="json", by_alias=True)
    for step, msg in [({"id": "d", "type": "report_design", "report": "x", "input": "p"}, "needs 'model'"),
                      ({"id": "d", "type": "report_design", "model": "local", "report": "x"}, "needs 'input'"),
                      ({"id": "d", "type": "report_design", "model": "local", "report": "x", "input": "p"},
                       "does not exist yet"),
                      ({"id": "d", "type": "report_design", "model": "local", "report": "x", "input": "p",
                        "from": FROM, "overwrite": "rename"}, "never")]:
        with pytest.raises(ValueError, match=msg):
            ProjectConfig.model_validate({**base, "workflows": {"w": {"steps": [step]}}})


# --------------------------------------------------------------------------- CLI
def test_cli_design_promote_and_ask(svc, tmp_path, monkeypatch):
    from typer.testing import CliRunner
    import portwright.agents.llm as llm_mod
    from portwright.cli import app
    script = FakeLlm([reply()])
    monkeypatch.setattr(llm_mod, "create_provider", lambda *a, **k: script)
    monkeypatch.chdir(tmp_path)
    run = CliRunner().invoke
    r = run(app, ["report", "design", "staff", "-P", "Staff table", "-s", "hr", "-q", FROM["query"], "-f", "html",
                  "--run"])
    assert r.exit_code == 0, r.output
    assert "the model saw: data access 'full', 3 row(s), columns: name, dept, salary" in r.output
    assert "staff.html" in r.output
    r = run(app, ["report", "design", "staff", "-P", "again"])
    assert r.exit_code == 1 and "--force" in r.output
    r = run(app, ["report", "design", "staff", "-P", "again", "--data-access", "bogus", "--force"])
    assert r.exit_code == 1 and "unknown data access" in r.output
    r = run(app, ["report", "promote", "staff"])
    assert r.exit_code == 0 and (tmp_path / "templates" / "staff.html").exists()
    plan = {"name": "by-dept", "from": {"source": "hr", "query": "select dept, count(*) n from emp group by dept"},
            "formats": ["json"], "design_prompt": "Counts per dept"}
    script.script = [LlmResponse(json.dumps(plan), [], 1, 1), reply("<p>{{ row_count }}</p>")]
    r = run(app, ["report", "ask", "How many people per department?", "--no-run"])
    assert r.exit_code == 0, r.output
    assert "Report 'by-dept' created from source hr" in r.output
