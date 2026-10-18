import sqlite3

from typer.testing import CliRunner

from portwright.cli import app

runner = CliRunner()


def test_call_runs_endpoint_without_server(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["init", ".", "--name", "t"]).exit_code == 0
    c = sqlite3.connect(tmp_path / "data" / "t.db")
    c.execute("create table emp(id integer primary key, name text, dept text)")
    c.execute("insert into emp(name, dept) values ('A','ENG'),('B','OPS')")
    c.commit()
    for args in (["cred", "add", "db", "-t", "database", "-f", "driver=sqlite", "-f", "database=data/t.db"],
                 ["source", "add", "hr", "-t", "sql", "--credential", "db", "--read-write"],
                 ["endpoint", "crud", "emp", "-s", "hr", "--table", "emp", "--key", "id",
                  "--field", "name:string:required", "--field", "dept", "--filter", "dept"]):
        r = runner.invoke(app, args)
        assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["call", "emp-list", "-p", "dept=OPS"])
    assert r.exit_code == 0 and '"B"' in r.output and '"A"' not in r.output
    r = runner.invoke(app, ["call", "emp-create", "-p", "name=C", "-p", "dept=ENG"])
    assert r.exit_code == 0 and '"created": true' in r.output
    assert runner.invoke(app, ["call", "emp-get", "-p", "id=999"]).exit_code == 1
    assert runner.invoke(app, ["call", "emp-list", "-p", "salary=1"]).exit_code == 1
    assert "cli:" not in (tmp_path / "portwright.yaml").read_text()       # temp key never persisted
