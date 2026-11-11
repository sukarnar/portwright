"""Workflow report / copy / move / file steps and ${run.*} values, plus the canvas round trip."""
import asyncio
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from portwright.agents.workflow import run_info, run_workflow
from portwright.config import ProjectConfig
from portwright.services import Project, Services
from portwright.studio import graph_vm as gv


@pytest.fixture
def svc(tmp_path):
    proj = tmp_path / "proj"
    s = Services(Project.init(proj, "t"))
    db = proj / "data" / "hr.db"
    c = sqlite3.connect(db)
    c.execute("create table emp(id integer primary key, name text, dept text)")
    c.executemany("insert into emp(name, dept) values (?, ?)", [("A", "ENG"), ("B", "OPS"), ("C", "ENG")])
    c.commit()
    c.close()
    s.credentials.add("db", "database", {"driver": "sqlite", "database": str(db)})
    s.sources.add("hr", "sql", credential="db")
    share = tmp_path / "share"
    share.mkdir()
    cfg = s.project.config.model_dump(mode="json", by_alias=True)
    cfg["security"]["allowed_folders"] = [str(share)]
    cfg["destinations"] = {"finance": {"path": str(share)}}
    cfg["reports"] = [{"name": "staff", "from": {"source": "hr", "query":
                       "select name, dept from emp where (:dept is null or dept = :dept) order by id",
                       "params": {"dept": None}}, "formats": ["csv", "json"]}]
    s.project.config = ProjectConfig.model_validate(cfg)
    s.project.save(force=True)
    s.share = share
    return s


def _run(svc, steps, scopes, input=None):
    cfg = svc.project.config.model_dump(mode="json", by_alias=True)
    cfg["workflows"] = {"wf": {"steps": steps, "scopes": scopes}}
    config = ProjectConfig.model_validate(cfg)
    return asyncio.run(run_workflow(config, svc.project.root, svc.project.credentials(), "wf", input, save=False))


PIPELINE = [
    {"id": "start", "type": "input", "inputs": {"dept": "ENG"}},
    {"id": "build", "type": "report", "report": "staff", "input": {"dept": "${input.dept}"}, "formats": ["csv", "xlsx"]},
    {"id": "publish", "type": "copy", "files": "${steps.build.output.outputs}",
     "to": "dest:finance/${run.date.year}-${run.date.month}", "manifest": True},
    {"id": "done", "type": "output", "value": {"copied": "${steps.publish.output}", "rows": "${steps.build.output.row_count}"}},
]


def test_report_then_copy_to_dated_folder(svc):
    res = _run(svc, PIPELINE, ["reports:run", "fs:reports:read", "fs:dest:finance"])
    assert res.ok, res.error
    now = datetime.now().astimezone()
    folder = svc.share / f"{now:%Y}-{now:%m}"
    assert res.output["rows"] == 2
    assert sorted(p.name for p in folder.iterdir()) == ["manifest.json", "staff.csv", "staff.xlsx"]
    assert "A" in (folder / "staff.csv").read_text(encoding="utf-8-sig")
    assert [c["action"] for c in res.output["copied"]] == ["copied", "copied"]
    # the report's own files stay where the report wrote them
    assert (svc.project.root / "reports/output/staff.csv").exists()


def test_scopes_are_required(svc):
    res = _run(svc, PIPELINE, ["fs:reports:read", "fs:dest:finance"])
    assert not res.ok and "reports:run" in res.error
    res = _run(svc, PIPELINE, ["reports:run", "fs:dest:finance"])
    assert not res.ok and "fs:reports:read" in res.error           # reading the report's files is a scope too
    res = _run(svc, PIPELINE, ["reports:run", "fs:reports:read"])
    assert not res.ok and "fs:dest:finance" in res.error


def test_move_and_file_steps(svc):
    steps = [
        {"id": "note", "type": "file", "op": "write", "to": "reports:notes/${run.date.iso}.txt",
         "content": "hello ${input.who}"},
        {"id": "ls", "type": "file", "op": "list", "path": "reports:notes"},
        {"id": "ship", "type": "move", "files": "${steps.ls.output}", "to": "dest:finance/notes"},
        {"id": "read", "type": "file", "op": "read", "path": "${steps.note.output.address}", "on_error": "continue"},
    ]
    res = _run(svc, steps, ["fs:reports:write", "fs:dest:finance"], {"who": "Ada"})
    assert res.ok, res.error
    moved = list((svc.share / "notes").iterdir())
    assert len(moved) == 1 and moved[0].read_text() == "hello Ada"
    assert not list((svc.project.root / "reports/output/notes").iterdir())         # source removed
    assert not res.steps[-1].ok and "is not a file" in res.steps[-1].error         # it was moved away


def test_foreach_copy_and_inline_report(svc):
    steps = [
        {"id": "rep", "type": "report", "formats": ["json"], "data": [{"k": 1}, {"k": 2}],
         "output_path": "reports/output/inline"},
        {"id": "each", "type": "foreach", "items": "${steps.rep.output.outputs}", "do": "copy",
         "files": "${item}", "to": "dest:finance/each-${index}"},
    ]
    res = _run(svc, steps, ["reports:run", "fs:reports:read", "fs:dest:finance"])
    assert res.ok, res.error
    assert json.loads((svc.share / "each-0" / "inline.json").read_text()) == [{"k": 1}, {"k": 2}]


def test_config_checks_step_targets(svc):
    base = svc.project.config.model_dump(mode="json", by_alias=True)
    bad = [
        ([{"id": "r", "type": "report", "report": "nope"}], "unknown report"),
        ([{"id": "c", "type": "copy", "files": ["reports:x"], "to": "dest:nowhere/x"}], "unknown destination"),
        ([{"id": "c", "type": "copy", "files": ["reports:x"], "to": "vault:x"}], "unknown filesystem root"),
        ([{"id": "c", "type": "copy", "files": ["reports:x"]}], "needs 'files' and 'to'"),
        ([{"id": "f", "type": "file", "op": "zip", "files": ["a"]}], "needs to"),
        ([{"id": "f", "type": "file"}], "needs 'op'"),
    ]
    for steps, msg in bad:
        with pytest.raises(ValueError, match=msg):
            ProjectConfig.model_validate({**base, "workflows": {"w": {"steps": steps}}})
    ok = [{"id": "c", "type": "copy", "files": ["reports:x"], "to": "dest:${input.where}/x"}]   # chosen at run time
    ProjectConfig.model_validate({**base, "workflows": {"w": {"steps": ok}}})


def test_run_info_is_zero_padded():
    info = run_info("abc", datetime(2026, 3, 4, 5, 6, 7, tzinfo=timezone.utc))
    local = datetime(2026, 3, 4, 5, 6, 7, tzinfo=timezone.utc).astimezone()
    assert info["date"] == {"year": f"{local:%Y}", "month": f"{local:%m}", "day": f"{local:%d}",
                            "iso": f"{local:%Y-%m-%d}"}
    assert info["started_at"] == "2026-03-04T05:06:07+00:00" and info["id"] == "abc"


def test_canvas_round_trip_keeps_new_step_options():
    g = gv.from_steps(PIPELINE)
    assert [n.type for n in g.nodes] == ["input", "report", "copy", "output"]
    steps = gv.to_steps(g)
    by = {s["id"]: s for s in steps}
    assert by["build"]["report"] == "staff" and by["build"]["formats"] == ["csv", "xlsx"]
    assert by["build"]["input"] == {"dept": "${input.dept}"}
    assert by["publish"]["manifest"] is True and by["publish"]["to"].startswith("dest:finance/")
    assert by["publish"]["next"] == "done"
    from portwright.agents.spec import WorkflowSpec
    WorkflowSpec.model_validate({"steps": steps})                      # still a valid workflow


def test_canvas_new_nodes_and_problems():
    g = gv.Graph()
    c = gv.add_node(g, "copy")
    m = gv.add_node(g, "move")
    gv.connect(g, c.id, "next", m.id)
    probs = gv.problems(g, [], [])
    assert any("choose the folder to copy" in p for p in probs)
    assert any("choose the folder to move" in p for p in probs)
    c.settings = "files: [reports:a.pdf]\nto: dest:finance/x"
    m.settings = "files: [reports:a.pdf]\nto: dest:finance/y"
    assert gv.problems(g, [], []) == []
    out = gv.to_steps(g)
    assert out[0]["type"] == "copy" and out[0]["to"] == "dest:finance/x" and out[1]["type"] == "move"
    c.settings = "- not a mapping"
    assert any("YAML mapping" in p for p in gv.problems(g, [], []))
