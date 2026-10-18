import csv
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Column, Integer, MetaData, String, Table
from sqlalchemy.dialects import mssql, oracle
from typer.testing import CliRunner

from portwright.cli import app
from portwright.config import ColumnMap, LoadSpec, ProjectConfig
from portwright.loads.mapping import RowError, RowMapper, auto_map, cast
from portwright.loads.writers import MergeUpsertWriter

runner = CliRunner()


# ------------------------------------------------------------------ mapping
def _spec(cols, **kw):
    return LoadSpec.model_validate({"name": "t", "from": {"source": "f"}, "to": {"source": "db", "table": "x"},
                                    "columns": cols, **kw})


def test_mapper_rename_cast_transform_constants():
    m = RowMapper(_spec([
        {"source": "Emp No", "target": "emp_id", "type": "integer", "required": True},
        {"source": "Eff Date", "target": "eff_dt", "type": "date", "format": "%m/%d/%Y"},
        {"source": "Dept", "target": "dept", "transform": ["trim", "upper", {"lookup": {"ENG": "E", "_default": "?"}}]},
        {"source": "Amt", "target": "amt", "type": "number", "default": "0"},
        {"target": "src", "value": "CSV"},
        {"target": "run", "value": "${run_id}"},
    ]), "R1")
    out = m.map_row({"Emp No": "1,001", "Eff Date": "10/01/2026", "Dept": " eng ", "Amt": ""})
    assert out["emp_id"] == 1001 and str(out["eff_dt"]) == "2026-10-01"
    assert out["dept"] == "E" and str(out["amt"]) == "0" and out["src"] == "CSV" and out["run"] == "R1"


def test_mapper_collects_all_errors_and_never_expands_data():
    m = RowMapper(_spec([{"source": "A", "target": "a", "type": "integer", "required": True},
                         {"source": "B", "target": "b", "type": "date"},
                         {"source": "C", "target": "c"}]), "R")
    with pytest.raises(RowError) as e:
        m.map_row({"A": "", "B": "31/31/2026", "C": "x"})
    assert "required" in str(e.value) and "not a valid date" in str(e.value)
    assert m.map_row({"A": "1", "B": "", "C": "${now}"})["c"] == "${now}"     # data is not a constant


def test_cast_rejects_fractional_integer():
    with pytest.raises(RowError):
        cast("1.5", ColumnMap(source="x", target="x", type="integer"))


def test_auto_map_exact_and_fuzzy():
    cols, unmatched = auto_map(["Transfer ID", "Amount USD", "Emp Numbr", "Zzz"],
                               {"transfer_id": "integer", "amount_usd": "number", "emp_number": "integer"})
    by = {c["source"]: c for c in cols}
    assert by["Transfer ID"]["target"] == "transfer_id" and "review" not in by["Transfer ID"]
    assert by["Emp Numbr"]["target"] == "emp_number" and by["Emp Numbr"]["review"] is True
    assert unmatched == ["Zzz"]


def test_spec_rules():
    with pytest.raises(Exception, match="needs 'keys'"):
        _spec([{"source": "a", "target": "a"}], mode="upsert")
    with pytest.raises(Exception, match="mapped twice"):
        _spec([{"source": "a", "target": "a"}, {"source": "b", "target": "A"}])


@pytest.mark.parametrize("dialect,needle", [(oracle.dialect(), "FROM dual"), (mssql.dialect(), "MERGE INTO")])
def test_merge_statement_is_bound_and_quoted(dialect, needle):
    t = Table("emp", MetaData(), Column("id", Integer, primary_key=True), Column("name", String(10)),
              schema="hr")
    w = MergeUpsertWriter(SimpleNamespace(dialect=dialect), t, ["id"])
    sql = w._statement(["id", "name"])
    assert needle in sql and ":p0" in sql and ":p1" in sql
    assert "WHEN MATCHED THEN UPDATE SET" in sql and "WHEN NOT MATCHED THEN INSERT" in sql


# --------------------------------------------------------------- end to end
@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["init", ".", "--name", "t"]).exit_code == 0
    c = sqlite3.connect(tmp_path / "data" / "hr.db")
    c.execute("create table tgt(rec_id integer primary key, emp integer not null, label varchar(4))")
    c.commit()
    with open(tmp_path / "data" / "in.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Rec ID", "Employee", "Label"])
        w.writerows([[i, 1000 + i, f"L{i % 10}"] for i in range(1, 1201)])   # > 2 pages of 500
    for args in (["cred", "add", "db", "-t", "database", "-f", "driver=sqlite", "-f", "database=data/hr.db"],
                 ["source", "add", "hr", "-t", "sql", "--credential", "db", "--read-write"],
                 ["source", "add", "inp", "-t", "file", "--path", "in.csv"],
                 ["endpoint", "add", "in-list", "-s", "inp", "-p", "/in", "--scope", "in:read"],
                 ["load", "create", "l1", "--from-endpoint", "in-list", "--to", "hr", "--table", "tgt"],
                 ["load", "map", "l1", "--set", "Rec ID=rec_id:integer", "--set", "Employee=emp:integer",
                  "--set", "Label=label", "--mode", "upsert", "--key", "rec_id"]):
        r = runner.invoke(app, args)
        assert r.exit_code == 0, r.output
    return tmp_path


def test_cli_load_reads_all_pages_and_is_idempotent(project):
    r = runner.invoke(app, ["load", "run", "l1"])
    assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["load", "run", "l1"])          # same file again: skipped, exit 3
    assert r.exit_code == 3 and "SKIPPED" in r.output, r.output
    r = runner.invoke(app, ["load", "run", "l1", "--force"])   # upsert again: no duplicates
    assert r.exit_code == 0, r.output
    n = sqlite3.connect(project / "data" / "hr.db").execute("select count(*) from tgt").fetchone()[0]
    assert n == 1200


def test_cli_validate_flags_missing_required(project):
    runner.invoke(app, ["load", "map", "l1", "--remove", "emp", "--mode", "insert"])
    r = runner.invoke(app, ["load", "validate", "l1"])
    assert r.exit_code == 1 and "required column 'emp'" in r.output


def test_rest_trigger_requires_scope(project):
    cfg = ProjectConfig.load(project / "portwright.yaml")
    from portwright.cli import load_dotenv
    load_dotenv(project / ".env")
    with TestClient(__import__("portwright.server", fromlist=["create_app"]).create_app(
            cfg, project_root=project)) as c:
        assert c.post("/api/_loads/l1/run").status_code == 401
    r = runner.invoke(app, ["key", "create", "loader", "--scope", "load:run"])
    key = [w for w in r.output.split() if w.startswith("pw_")][0]
    cfg = ProjectConfig.load(project / "portwright.yaml")
    with TestClient(__import__("portwright.server", fromlist=["create_app"]).create_app(
            cfg, project_root=project)) as c:
        res = c.post("/api/_loads/l1/run?dry_run=true", headers={"X-API-Key": key})
        assert res.status_code == 200 and res.json()["status"] == "dry_run" and res.json()["mapped"] == 1200
