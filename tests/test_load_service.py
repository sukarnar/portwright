import sqlite3

import pytest

from portwright.services import Project, ServiceError, Services
from portwright.studio.viewmodels import (CONSTANT, MapRow, apply_suggestion, build_transforms, load_draft,
                                         mapping_rows, split_transforms, unused_sources)


@pytest.fixture
def svc(tmp_path):
    s = Services(Project.init(tmp_path, "t"))
    c = sqlite3.connect(tmp_path / "data" / "hr.db")
    c.execute("create table emp_transfers(id integer primary key, emp_no integer not null unique, "
              "emp_name varchar(10) not null, eff_dt date, active integer, loaded_by text)")
    c.commit()
    (tmp_path / "data" / "t.csv").write_text(
        "Emp No,Employee Name,Eff Date,Activ\n1,Ann,01/31/2026,Y\n2,Bob,02/01/2026,N\n3,Waytoolongname,x,Y\n")
    s.credentials.add("db", "database", {"driver": "sqlite", "database": str(tmp_path / "data" / "hr.db")})
    s.sources.add("hr", "sql", credential="db", read_write=True)
    s.sources.add("transfers", "file", path="t.csv")
    return s


def _draft(**kw):
    d = {"name": "xfer", "from": {"source": "transfers"}, "to": {"source": "hr", "table": "emp_transfers"},
         "columns": []}
    d.update(kw)
    return d


def test_inspect_suggest_and_grid_roundtrip(svc):
    ins = svc.loads.inspect(_draft())
    assert ins.header == ["Emp No", "Employee Name", "Eff Date", "Activ"] and ins.row_count == 3
    t = {c.name: c for c in ins.targets}
    assert t["emp_no"].needs_value and t["id"].primary_key and t["emp_name"].length == 10
    sug = svc.loads.suggest(_draft(), ins)
    assert {c["target"] for c in sug.columns} >= {"emp_no", "eff_dt"}
    assert sug.missing_required == ["emp_name"] and "Employee Name" in sug.unmatched
    rows = mapping_rows(ins.targets, [], [])
    assert apply_suggestion(rows, sug.columns) >= 3
    by = {r.target: r for r in rows}
    assert by["id"].status(ins.header)[0] == "neutral"           # generated integer PK
    assert by["emp_name"].status(ins.header)[0] == "bad"
    by["emp_name"].source = "Employee Name"
    by["eff_dt"].format = "%m/%d/%Y"
    by["loaded_by"].is_constant, by["loaded_by"].value = True, "${run_id}"
    by["active"].source, by["active"].type = "Activ", "integer"
    by["active"].transform = build_transforms(["trim"], "", "", "Y=1\nN=0", "", False)
    for r in rows:
        r.review = False
    assert unused_sources(ins.header, rows) == []
    d = load_draft("xfer", "", "source", "transfers", "hr", "emp_transfers", "upsert",
                   [*rows[:1], *[r for r in rows[1:]]], "1000", "1", "ignore")
    assert d["keys"] == [] and {"value": "${run_id}", "target": "loaded_by", "type": "string"} in d["columns"]
    by["emp_no"].key = True
    d = load_draft("xfer", "", "source", "transfers", "hr", "emp_transfers", "upsert", rows, "1000", "1", "ignore")
    assert d["keys"] == ["emp_no"]
    spec = svc.loads.save(d)
    assert spec.mode == "upsert" and svc.loads.list()[0]["columns"] == len(d["columns"])
    # grid rebuilt from the saved spec keeps everything
    again = mapping_rows(ins.targets, svc.loads.as_dict("xfer")["columns"], spec.keys)
    assert [r.to_column() for r in again if r.mapped] == d["columns"]


def test_validate_preview_run_history_rejects(svc):
    cols = [{"source": "Emp No", "target": "emp_no", "type": "integer"},
            {"source": "Employee Name", "target": "emp_name"},
            {"source": "Eff Date", "target": "eff_dt", "type": "date", "format": "%m/%d/%Y"},
            {"source": "Activ", "target": "active", "type": "integer", "transform": [{"lookup": {"Y": 1, "N": 0}}]}]
    chk = svc.loads.validate(_draft(columns=cols[:1]))
    assert not chk.ok and any("emp_name" in p for p in chk.problems)
    assert svc.loads.validate(_draft(columns=cols)).ok
    dry = svc.loads.run(_draft(columns=cols, max_rejects=5), dry_run=True, preview=2)
    assert dry.status == "dry_run" and dry.mapped == 2 and dry.rejected == 1 and len(dry.preview) == 2
    assert svc.loads.history() == []                              # dry runs are not recorded
    with pytest.raises(ServiceError, match="save the load"):
        svc.loads.run(_draft(columns=cols, max_rejects=5))
    svc.loads.save(_draft(columns=cols, max_rejects=5))
    rep = svc.loads.run("xfer")
    assert rep.status == "partial" and rep.inserted == 2
    h = svc.loads.history("xfer")
    assert h[-1]["run_id"] == rep.run_id
    rc, rows, total = svc.loads.rejects(rep.run_id)
    assert rc[:2] == ["record", "reason"] and total == 1 and rows[0]["record"] == "3"
    with pytest.raises(ServiceError, match="invalid run id"):
        svc.loads.rejects("../../etc/passwd")
    assert svc.loads.list()[0]["last_run"]["status"] == "partial"


def test_save_rules(svc):
    with pytest.raises(ServiceError, match="not a file or folder source"):
        svc.loads.build(_draft(**{"from": {"source": "hr"}}))
    svc.loads.save(_draft())
    with pytest.raises(ServiceError, match="already exists"):
        svc.loads.save(_draft())
    svc.loads.save(_draft(name="renamed"), original="xfer")
    assert [l["name"] for l in svc.loads.list()] == ["renamed"]
    with pytest.raises(ServiceError, match="still used|load"):
        svc.sources.remove("transfers")
    svc.loads.remove("renamed")
    assert svc.loads.list() == []


def test_transform_and_row_helpers():
    steps = build_transforms(["null_if_empty", "trim", "upper"], "-", "", "A=1\nB=2", "9", True)
    assert steps == ["trim", "upper", "null_if_empty", {"replace": ["-", ""]}, {"lookup": {"A": "1", "B": "2", "_default": "9"}}]
    back = split_transforms(steps)
    assert back["simple"] == ["trim", "upper", "null_if_empty"] and back["lookup"] == "A=1\nB=2" and back["use_default"]
    r = MapRow(target="x", nullable=False, in_table=True)
    assert r.status([])[0] == "bad"
    r.is_constant, r.value = True, "K"
    assert r.status([])[0] == "good" and r.to_column() == {"target": "x", "type": "string", "value": "K"}
    assert CONSTANT != r.value
    with pytest.raises(ServiceError, match="whole number"):
        load_draft("a", "", "source", "f", "hr", "t", "insert", [], "abc", "0", "ignore")


def test_load_draft_settings():
    d = load_draft("a", "", "source", "f", "hr", "t", "replace", [], "10", "0", "ignore", None, {
        "duplicate_keys": "last", "existing_keys": "skip",
        "tracking": {"on_unchanged": "load", "on_header_change": "warn", "stable_seconds": "30",
                     "max_age_hours": "", "archive_keep": "5"},
        "safety": {"min_rows": "100", "min_percent": "50"}})
    assert d["tracking"] == {"on_unchanged": "load", "on_header_change": "warn", "stable_seconds": 30,
                             "max_age_hours": None, "archive_keep": 5}
    assert d["safety"] == {"min_rows": 100, "min_percent": 50.0} and d["duplicate_keys"] == "last"
    with pytest.raises(ServiceError, match="Minimum rows"):
        load_draft("a", "", "source", "f", "hr", "t", "replace", [], "10", "0", "ignore", None,
                   {"safety": {"min_rows": "x", "min_percent": "0"}})
