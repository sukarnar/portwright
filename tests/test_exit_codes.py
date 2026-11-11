"""Exit codes for schedulers: 0 done, 1 failed, 2 done with warnings (report run, workflow run)."""
import asyncio
import json
import sqlite3

import httpx
import pytest
from typer.testing import CliRunner

from portwright.agents.llm import LlmError, LlmProvider, LlmResponse
from portwright.agents.workflow import StepResult, WorkflowResult, run_workflow
from portwright.cli import app
from portwright.config import ProjectConfig
from portwright.services import Project, Services

STAFF = {"source": "hr", "query": "select name, dept from emp order by id"}


@pytest.fixture
def svc(tmp_path):
    s = Services(Project.init(tmp_path, "t"))
    db = tmp_path / "data" / "hr.db"
    c = sqlite3.connect(db)
    c.execute("create table emp(id integer primary key, name text, dept text)")
    c.executemany("insert into emp(name, dept) values (?, ?)", [("A", "ENG"), ("B", "OPS"), ("C", "ENG")])
    c.commit()
    c.close()
    s.credentials.add("db", "database", {"driver": "sqlite", "database": str(db)})
    s.sources.add("hr", "sql", credential="db")
    return s


def configure(svc, *, max_rows=None, workflows=None):
    cfg = svc.project.config.model_dump(mode="json", by_alias=True)
    frm = {**STAFF, **({"max_rows": max_rows} if max_rows else {})}
    cfg["reports"] = [{"name": "staff", "from": frm, "formats": ["csv"]}]
    if workflows:
        cfg["workflows"] = workflows
    svc.project.config = ProjectConfig.model_validate(cfg)
    svc.project.save(force=True)
    return svc.project.config


def run(config, svc, name="wf", provider=None):
    return asyncio.run(run_workflow(config, svc.project.root, svc.project.credentials(), name, None,
                                    provider_factory=(lambda n, s: provider) if provider else None, save=False))


class _Flaky(LlmProvider):
    """Answers every prompt except those containing 'boom'."""

    def __init__(self):
        super().__init__("m", None, httpx.AsyncClient())

    async def chat(self, system, messages, tools):
        if "boom" in messages[-1]["content"]:
            raise LlmError("boom")
        return LlmResponse("ok", [], 1, 1)

    async def aclose(self):
        pass


def test_workflow_result_exit_codes():
    ok = WorkflowResult("w", "ok", None, [StepResult("a", "set", True)])
    assert ok.exit_code == 0 and ok.warnings == []
    failed = WorkflowResult("w", "failed", None, [StepResult("a", "tool", False, error="x")], error="x")
    assert failed.exit_code == 1
    continued = WorkflowResult("w", "ok", None, [StepResult("a", "tool", False, error="403"),
                                                 StepResult("b", "set", True)])
    assert continued.exit_code == 2 and "step 'a' failed and the workflow continued: 403" in continued.warnings
    branch = WorkflowResult("w", "ok", None, [StepResult("c", "condition", False, error="bad ref")])
    assert branch.exit_code == 0                      # a failed condition takes its else branch
    d = continued.to_dict()
    assert d["exit_code"] == 2 and d["warnings"] == continued.warnings


def test_report_step_hitting_max_rows_is_a_warning(svc):
    config = configure(svc, max_rows=2, workflows={"wf": {"scopes": ["reports:run"], "steps": [
        {"id": "build", "type": "report", "report": "staff"}]}})
    r = run(config, svc)
    assert r.ok and r.exit_code == 2
    assert any("build" in w and "max_rows" in w for w in r.warnings)
    full = configure(svc, workflows={"wf": {"scopes": ["reports:run"], "steps": [
        {"id": "build", "type": "report", "report": "staff"}]}})
    assert run(full, svc).exit_code == 0


def test_foreach_item_failures_are_a_warning(svc):
    config = configure(svc, workflows={"wf": {"steps": [
        {"id": "rows", "type": "set", "value": ["a", "boom", "c"]},
        {"id": "each", "type": "foreach", "items": "${steps.rows.output}", "do": "llm", "model": "m",
         "input": "${item}", "on_error": "continue"}]}})
    cfg = config.model_dump(mode="json", by_alias=True)
    cfg["llm_models"] = {"m": {"base_url": "https://llm.test/v1", "model": "x"}}
    r = run(ProjectConfig.model_validate(cfg), svc, provider=_Flaky())
    assert r.ok and r.exit_code == 2 and r.steps[1].warnings == ["1 of 3 item(s) failed"]


def test_report_run_cli_exit_codes(svc):
    configure(svc, max_rows=2)
    cfg = str(svc.project.path)
    r = CliRunner().invoke(app, ["report", "run", "staff", "-c", cfg, "--json"])
    assert r.exit_code == 2, r.output
    out = json.loads(r.output)
    assert out["truncated"] is True and out["exit_code"] == 2
    configure(svc)
    assert CliRunner().invoke(app, ["report", "run", "staff", "-c", cfg]).exit_code == 0
    assert CliRunner().invoke(app, ["report", "run", "ghost", "-c", cfg]).exit_code == 1


def test_workflow_run_cli_exit_codes(svc):
    configure(svc, workflows={
        "clean": {"steps": [{"id": "a", "type": "set", "value": "hi"}]},
        "fallback": {"steps": [{"id": "build", "type": "report", "report": "staff", "on_error": "continue"},
                               {"id": "b", "type": "set", "value": "fallback"}]},       # no reports:run scope
        "broken": {"steps": [{"id": "build", "type": "report", "report": "staff"}]}})
    cfg = str(svc.project.path)
    runner = CliRunner()
    assert runner.invoke(app, ["workflow", "run", "clean", "-c", cfg]).exit_code == 0
    r = runner.invoke(app, ["workflow", "run", "fallback", "-c", cfg])
    assert r.exit_code == 2 and "with warnings" in r.output and "reports:run" in r.output
    r = runner.invoke(app, ["workflow", "run", "fallback", "-c", cfg, "--json"])
    assert r.exit_code == 2 and json.loads(r.output)["exit_code"] == 2
    assert runner.invoke(app, ["workflow", "run", "broken", "-c", cfg]).exit_code == 1
