import sqlite3

import pytest
from typer.testing import CliRunner

from portwright import docs
from portwright.cli import app
from portwright.services import Project, ServiceError, Services
from portwright.studio.viewmodels import (ParamRow, endpoint_cli, endpoint_draft, keys_covering, result_table,
                                         set_project_state, setup_steps, sync_path_params)


@pytest.fixture
def svc(tmp_path):
    s = Services(Project.init(tmp_path, "t"))
    c = sqlite3.connect(tmp_path / "data" / "hr.db")
    c.execute("create table emp(id integer primary key, name varchar(20) not null, dept text, salary numeric(8,2))")
    c.execute("insert into emp(name, dept, salary) values ('A','ENG', 10), ('B','OPS', 20)")
    c.commit()
    (tmp_path / "data" / "t.csv").write_text("emp_no,Full Name,dept\n1,Ann,ENG\n2,Bob,OPS\n")
    s.credentials.add("db", "database", {"driver": "sqlite", "database": str(tmp_path / "data" / "hr.db")})
    s.sources.add("hr", "sql", credential="db", read_write=True)
    s.sources.add("f", "file", path="t.csv")
    return s


def test_crud_wizard_subset_typed(svc):
    cols = svc.sources.columns("hr", "emp")
    new = svc.endpoints.crud_from_table("emp", "hr", "emp", "id", cols, ["name", "dept", "salary"], ["dept"],
                                        ["list", "get", "create"])
    assert [e.name for e in new] == ["emp-list", "emp-get", "emp-create"]
    create = svc.project.config.endpoint("emp-create")
    p = {x.name: x for x in create.params}
    assert p["name"].required and not p["dept"].required and p["salary"].type == "number"
    assert svc.runtime.call("emp-list", {"dept": "OPS"}).data["items"][0]["name"] == "B"
    assert svc.runtime.call("emp-create", {"name": "C", "salary": "5"}).status == 201
    with pytest.raises(ServiceError, match="already exist"):
        svc.endpoints.crud_from_table("emp", "hr", "emp", "id", cols, ["name"], [], ["list"])


def test_query_endpoint_draft_try_validate_save(svc):
    sql = "SELECT name FROM emp WHERE dept = :dept AND CAST(salary AS REAL) >= :min_sal"
    assert svc.endpoints.binds(sql) == ["dept", "min_sal"]
    rows = [ParamRow(name="dept", required=True), ParamRow(name="min_sal", type="number", default="0")]
    d = endpoint_draft({"name": "by-dept", "source": "hr", "method": "GET", "path": "/by-dept", "query": sql,
                        "operation": "query"}, rows, public=False, scopes=["hr:read"], rate_limit="", max_limit="100")
    assert svc.runtime.validate_endpoint(d).ok
    r = svc.runtime.try_endpoint(d, {"dept": "ENG"})
    assert r.ok and r.data["items"] == [{"name": "A"}] and svc.project.config.endpoint("by-dept") is None
    bad = dict(d, query="SELECT * FROM emp WHERE x = :nope")
    v = svc.runtime.validate_endpoint(bad)
    assert not v.ok and "undeclared" in v.message
    ep, warnings = svc.endpoints.save(d)
    assert ep.max_limit == 100 and warnings == []
    assert "portwright endpoint add by-dept" in endpoint_cli(svc.endpoints.as_dict("by-dept"))
    sn = svc.runtime.snippets(d, {"dept": "ENG"})
    assert "dept=ENG" in sn["powershell"]
    # rename + method/path clash
    d2 = dict(d, name="by-dept2")
    svc.endpoints.save(d2, original="by-dept")
    assert svc.project.config.endpoint("by-dept2") and not svc.project.config.endpoint("by-dept")
    with pytest.raises(ServiceError, match="already used"):
        svc.endpoints.save(dict(d, name="other"))
    dup = svc.endpoints.duplicate("by-dept2", "by-dept3")
    assert dup.path == "/by-dept-copy"


def test_file_endpoint_and_path_params(svc):
    rows = sync_path_params("/t/{emp_no}", [ParamRow(name="dept")])
    assert [(r.name, r.location, r.required) for r in rows] == [("dept", "query", False), ("emp_no", "path", True)]
    d = endpoint_draft({"name": "t-get", "source": "f", "method": "GET", "path": "/t/{emp_no}", "key": "emp_no"},
                       [r for r in rows if r.location == "path"], public=True, scopes=["ignored"], rate_limit="5",
                       max_limit="500")
    assert d["scopes"] == [] and d["public"] and d["rate_limit"] == 5
    r = svc.runtime.try_endpoint(d, {"emp_no": "2"})
    assert r.ok and r.data["Full Name"] == "Bob"
    with pytest.raises(ServiceError, match="whole number"):
        endpoint_draft({"name": "x"}, [], public=False, scopes=[], rate_limit="abc", max_limit="5")
    with pytest.raises(ServiceError, match="used twice"):
        endpoint_draft({"name": "x"}, [ParamRow(name="a"), ParamRow(name="a")], public=False, scopes=[],
                       rate_limit="", max_limit="5")


def test_param_row_roundtrip():
    spec = ParamRow(name="n", type="integer", min="1", max="9", default="3", enum="1, 3, 5", required=True).to_spec()
    assert spec == {"name": "n", "type": "integer", "in": "query", "required": True, "default": 3,
                    "enum": [1, 3, 5], "min": 1.0, "max": 9.0, "max_length": 1024}
    back = ParamRow.from_spec(spec)
    assert (back.min, back.max, back.enum, back.default) == ("1", "9", "1, 3, 5", "3")
    with pytest.raises(ServiceError, match="must be integer"):
        ParamRow(name="n", type="integer", default="x").to_spec()


def test_setup_steps_and_keys(svc):
    steps = {s.key: s.done for s in setup_steps(svc)}
    assert steps["vault"] and steps["credential"] and steps["source"] and not steps["endpoint"] and not steps["try"]
    svc.endpoints.add("t-list", "f", "/t", scopes=["t:read"])
    assert keys_covering(svc, ["t:read"]) == []
    svc.access.create_key("app", ["t:read"], 30)
    assert keys_covering(svc, ["t:read"]) == ["app"]
    set_project_state(svc, tried=True)
    steps = {s.key: s.done for s in setup_steps(svc)}
    assert steps["endpoint"] and steps["key"] and steps["try"] and not steps["serve"]
    assert result_table({"items": [{"a": 1}, {"b": 2}]}) == (["a", "b"], [{"a": 1}, {"b": 2}])
    assert result_table({"x": 1}) is None


def test_docs_topics_search_reference_cli():
    slugs = [t.slug for t in docs.topics()]
    assert slugs[0] == "getting-started" and "loads" in slugs and slugs[-1] == "cli-reference"
    for t in docs.topics()[:-1]:
        assert t.studio and t.cli, t.slug               # every topic explains Studio and the CLI
        assert "```" in t.cli, t.slug                   # ... with CLI examples
    assert any(s == "scheduling" for s, _, _ in docs.search("exit code stonebranch"))
    ref = docs.get("cli-reference").cli
    assert "**portwright load run**" in ref and "`--force`" in ref and "**portwright endpoint add**" in ref
    kinds = [k for k, _ in docs.split_code(docs.get("scheduling").cli)]
    assert "bat" in kinds and "powershell" in kinds
    r = CliRunner().invoke(app, ["docs", "loads", "--plain", "--part", "cli"])
    assert r.exit_code == 0 and "portwright load create" in r.output
    r = CliRunner().invoke(app, ["docs", "--search", "kill fill"])
    assert "loads" in r.output
    assert CliRunner().invoke(app, ["docs", "nope"]).exit_code != 0
