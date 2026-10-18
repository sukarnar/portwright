import sqlite3

import pytest

from portwright.services import Project, Services


@pytest.fixture
def svc(tmp_path):
    s = Services(Project.init(tmp_path, "t"))
    c = sqlite3.connect(tmp_path / "data" / "hr.db")
    c.execute("create table t(emp_no integer, name varchar(10))")              # no primary key
    c.execute("create table p(emp_no integer primary key, name varchar(10))")
    c.execute("insert into t values (1, 'old')")
    c.commit()
    s.credentials.add("db", "database", {"driver": "sqlite", "database": str(tmp_path / "data" / "hr.db")})
    s.sources.add("hr", "sql", credential="db", read_write=True)
    s.sources.add("f", "file", path="in.csv")
    s.root = tmp_path
    return s


def write(svc, text):
    (svc.root / "data" / "in.csv").write_text(text)


COLS = [{"source": "No", "target": "emp_no", "type": "integer"}, {"source": "Name", "target": "name"}]


def save(svc, table="t", **kw):
    d = {"name": "l", "from": {"source": "f"}, "to": {"source": "hr", "table": table}, "columns": COLS,
         "max_rejects": 100}
    d.update(kw)
    if svc.project.config.load_job("l"):
        return svc.loads.save(d, original="l")
    return svc.loads.save(d)


def rows(svc, table="t"):
    return sqlite3.connect(svc.root / "data" / "hr.db").execute(f"select * from {table} order by 1, 2").fetchall()


def test_key_checks_insert(svc):
    write(svc, "No,Name\n1,A\n2,B\n2,B2\n,X\n3,C\n")
    save(svc, keys=["emp_no"])
    rep = svc.loads.run("l")
    reasons = [r["reason"] for r in svc.loads.rejects(rep.run_id)[1]]
    assert rep.status == "partial" and rep.inserted == 1 and rep.rejected == 4
    assert any("already exists" in x for x in reasons) and sum("duplicate key" in x for x in reasons) == 2
    assert any("key column empty" in x for x in reasons)
    assert rows(svc) == [(1, "old"), (3, "C")]


def test_duplicate_last_and_existing_skip_and_fail(svc):
    write(svc, "No,Name\n1,A\n5,B\n5,B2\n")
    save(svc, keys=["emp_no"], duplicate_keys="last", existing_keys="skip")
    rep = svc.loads.run("l")
    assert (rep.inserted, rep.dropped, rep.skipped, rep.rejected, rep.exit_code) == (1, 1, 1, 0, 0)
    assert (5, "B2") in rows(svc)
    write(svc, "No,Name\n5,Z\n6,Y\n")
    save(svc, keys=["emp_no"], existing_keys="fail")
    rep = svc.loads.run("l")
    assert rep.status == "failed" and "already exist" in rep.error and (6, "Y") not in rows(svc)


def test_table_primary_key_used_when_no_key_set(svc):
    write(svc, "No,Name\n1,A\n1,B\n2,C\n")
    save(svc, table="p")
    rep = svc.loads.run("l")
    assert rep.keys == ["emp_no"] and rep.rejected == 2 and rows(svc, "p") == [(2, "C")]
    assert any("primary key" in w for w in rep.warnings)


def test_kill_and_fill_safety(svc):
    write(svc, "No,Name\n7,A\n")
    save(svc, mode="replace", safety={"min_rows": 2})
    rep = svc.loads.run("l")
    assert rep.status == "failed" and "min_rows" in rep.error and rows(svc) == [(1, "old")]
    save(svc, mode="replace", safety={"min_percent": 50})
    assert svc.loads.run("l", dry_run=True).table_rows == 1
    rep = svc.loads.run("l")
    assert rep.status == "success" and rep.deleted == 1 and rows(svc) == [(7, "A")]


def test_versions_skip_force_header_change_diff(svc):
    write(svc, "No,Name\n10,A\n11,B\n")
    save(svc, mode="upsert", keys=["emp_no"], table="p")
    r1 = svc.loads.run("l")
    assert r1.status == "success" and r1.source_version == 1 and r1.source_changed is None
    st = svc.loads.source_status("l")
    assert st["changed"] is False and st["version"] == 1
    r2 = svc.loads.run("l")
    assert r2.status == "skipped" and r2.exit_code == 3 and r2.inserted == r2.upserted == 0
    assert svc.loads.run("l", dry_run=True).source_changed is False
    assert svc.loads.run("l", force=True).status == "success"
    write(svc, "No,Name\n10,A\n11,BB\n12,C\n")
    r3 = svc.loads.run("l")
    assert r3.source_changed is True and r3.source_version == 2 and r3.status == "success"
    d = svc.loads.diff("l", 1, 2)
    assert (d["added"], d["removed"], d["changed"], d["unchanged"]) == (1, 0, 1, 1)
    assert d["changed_rows"][0]["fields"] == ["Name"]
    vs = svc.loads.versions("l")
    assert [v["version"] for v in vs] == [2, 1] and vs[0]["loaded"] and vs[1]["archived"]
    write(svc, "No,Name,Extra\n10,A,x\n")
    r4 = svc.loads.run("l")
    assert r4.status == "failed" and "columns changed" in r4.error
    assert svc.loads.run("l", force=True).status == "success"
    assert svc.loads.versions("l")[0]["header_change"] == {"added": ["Extra"], "removed": []}


def test_archive_pruned(svc):
    save(svc, tracking={"archive_keep": 1}, mode="replace")
    for i in range(3):
        write(svc, f"No,Name\n{20 + i},A\n")
        svc.loads.run("l")
    vs = svc.loads.versions("l")
    assert [v["archived"] for v in vs] == [True, False, False]


def test_composite_key_existing_check(svc):
    write(svc, "No,Name\n1,old\n1,new\n")
    save(svc, keys=["emp_no", "name"])
    rep = svc.loads.run("l")
    assert rep.inserted == 1 and rep.rejected == 1 and rows(svc) == [(1, "new"), (1, "old")]
