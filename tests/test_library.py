"""The built-in library: system prompts, skills and sample tools, and adding them to a project."""
import sqlite3

import pytest
from typer.testing import CliRunner

from portwright.agents import library as lib
from portwright.agents.spec import ToolSpec
from portwright.agents.templates import AGENT_TEMPLATES
from portwright.cli import app
from portwright.config import ProjectConfig
from portwright.services import Project, ServiceError, Services
from portwright.studio import agent_vm as vm


@pytest.fixture
def svc(tmp_path):
    s = Services(Project.init(tmp_path, "t"))
    c = sqlite3.connect(tmp_path / "data" / "hr.db")
    c.execute("create table emp(id integer primary key, name text)")
    c.commit()
    c.close()
    s.credentials.add("db", "database", {"driver": "sqlite", "database": str(tmp_path / "data" / "hr.db")})
    s.sources.add("hr", "sql", credential="db")
    cfg = s.project.config.model_dump(mode="json", by_alias=True)
    cfg["llm_models"] = {"local": {"base_url": "https://llm.test/v1", "model": "m"}}
    cfg["security"]["allowed_llm_hosts"] = ["llm.test"]
    s.project.config = ProjectConfig.model_validate(cfg)
    s.project.save(force=True)
    return s


def test_every_item_is_well_formed():
    prompts, skills, tools = lib.items("prompt"), lib.items("skill"), lib.items("tool")
    assert len(prompts) >= 10 and len(skills) >= 6 and len(tools) >= 6
    tool_ids, skill_ids = {t.id for t in tools}, {s.id for s in skills}
    for p in prompts:
        assert p.title and p.description and p.text.strip(), p.id
        assert set(p.use) <= {"agent", "llm"} and p.use, p.id
        assert p.output in ("text", "json"), p.id
        assert set(p.tools) <= tool_ids, f"{p.id}: unknown tools {set(p.tools) - tool_ids}"
        assert set(p.skills) <= skill_ids, f"{p.id}: unknown skills {set(p.skills) - skill_ids}"
        if p.output == "json":
            assert "JSON object" in p.text, p.id                   # says exactly what to reply with
        low = p.text.lower()                                       # every prompt guards against injected data
        assert "untrusted" in low or "not instructions" in low, p.id
    for s in skills:
        assert s.text.startswith("# "), s.id
    for t in tools:
        spec = lib.tool_spec(t, {"sql_source": "hr", "endpoint": "emp-list", "destination": "finance"})
        ToolSpec.model_validate(spec)                              # a valid tool once its needs are filled
        assert not any("{" + n + "}" in str(spec) for n in lib.NEEDS), t.id      # every placeholder filled
    assert len({(i.kind, i.id) for i in lib.items()}) == len(lib.items())


def test_find_and_filters():
    assert lib.find("anomaly-check").kind == "prompt"
    assert lib.find("skill/safe-sql").title == "Safe SQL"
    with pytest.raises(lib.LibraryError, match="nothing in the library"):
        lib.find("ghost")
    assert all("llm" in p.use for p in lib.items("prompt", use="llm"))
    assert {p.id for p in lib.items(query="anomalies")} >= {"anomaly-check"}
    assert lib.items("tool", category="reports")


def test_report_author_template_uses_the_library_prompt():
    assert AGENT_TEMPLATES["report-author"]["agent"]["instructions"] == lib.get("prompt", "report-author").text.strip()


def test_add_skill_and_tool_from_the_library(svc, tmp_path):
    assert svc.agents.add_skill_from_library("safe-sql", "sql_rules") == "sql_rules"
    text = (tmp_path / "skills" / "sql_rules.md").read_text(encoding="utf-8")
    assert "library: skill/safe-sql" in text and "# Safe SQL" in text
    assert svc.project.config.skills["sql_rules"].file == "skills/sql_rules.md"
    with pytest.raises(ServiceError, match="already exists"):
        svc.agents.add_skill_from_library("safe-sql", "sql_rules")
    assert svc.agents.add_tool_from_library("sql-readonly") == "sql"          # the only SQL source is picked
    t = svc.project.config.tools["sql"]
    assert t.type == "source" and t.source == "hr" and t.max_rows == 100 and "hr database" in t.description
    with pytest.raises(ServiceError, match="needs endpoint"):
        svc.agents.add_tool_from_library("api-lookup", "lookup")
    svc.agents.add_tool_from_library("report-outputs-reader", "fs")
    assert svc.project.config.tools["fs"].roots == ["reports"]


def test_studio_adds_an_agent_with_its_skills(svc):
    assert "anomaly-check" in vm.library_options(svc, "agent")
    made = vm.add_from_library(svc, "agent", "anomaly-check", "checker", model="local", with_skills=True)
    assert made[-1] == "agent checker" and "skill data-quality-checks" in made
    a = svc.project.config.agents["checker"]
    assert a.instructions.startswith("You check business data") and "json-replies" in a.skills
    assert vm.need_choices(svc, ["sql_source", "destination"]) == {"sql_source": ["hr"], "destination": []}
    with pytest.raises(ServiceError, match="Choose a model"):
        vm.add_from_library(svc, "agent", "data-analyst", "x")


def test_cli(svc):
    cfg, run = str(svc.project.path), CliRunner().invoke
    out = run(app, ["library", "list", "--kind", "prompt", "--use", "llm"]).output
    assert "classify-route" in out and "data-analyst" not in out                # agent-only prompt filtered out
    assert run(app, ["library", "show", "classify-route", "--text"]).output.startswith("You classify one item")
    assert "tool add sql --from sql-readonly --source" in run(app, ["library", "show", "sql-readonly"]).output
    r = run(app, ["skill", "add", "guard", "--from", "untrusted-data", "-c", cfg])
    assert r.exit_code == 0, r.output
    r = run(app, ["tool", "add", "q", "--from", "sql-readonly", "--source", "hr", "-c", cfg])
    assert r.exit_code == 0, r.output
    r = run(app, ["agent", "add", "analyst", "--model", "local", "--prompt", "data-analyst", "--tool", "q",
                  "--skill", "guard", "-c", cfg])
    assert r.exit_code == 0, r.output
    assert "safe-sql" in r.output                                              # suggests the skill it lacks
    agent = ProjectConfig.load(cfg).agents["analyst"]
    assert agent.instructions.startswith("You are a careful data analyst") and agent.description
    assert run(app, ["tool", "add", "z", "-c", cfg]).exit_code != 0              # neither --type nor --from
