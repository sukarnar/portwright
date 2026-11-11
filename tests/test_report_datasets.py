"""Report datasets: more queries per report, joined in memory (master-detail, lookups, several levels)."""
import asyncio
import json
import sqlite3
from decimal import Decimal

import pytest
from typer.testing import CliRunner

from portwright.cli import app
from portwright.config import ProjectConfig, ReportSpec
from portwright.reports.datasets import join, norm_key
from portwright.services import Project, ServiceError, Services
from portwright.studio import report_vm as rvm

pytest.importorskip("jinja2")

TEMPLATE = """<h1>{{ report_name }}</h1>
{% for o in rows %}<section id="o{{ o.id }}"><b>{{ o.customer_name.name if o.customer_name else '-' }}</b>
{% for l in o.lines %}<p>{{ l.product }} x{{ l.qty }}{% for s in l.serials %} [{{ s.serial }}]{% endfor %}</p>
{% else %}<i>No lines.</i>{% endfor %}</section>{% endfor %}
<footer>{{ datasets.lines | length }} lines, {{ datasets.notes | length }} notes</footer>"""


@pytest.fixture
def svc(tmp_path):
    """Orders in one database, lines and serials in another, customers in a CSV file."""
    s = Services(Project.init(tmp_path, "t"))
    sales, erp = tmp_path / "data" / "sales.db", tmp_path / "data" / "erp.db"
    c = sqlite3.connect(sales)
    c.execute("create table orders(id integer primary key, cust text, placed text)")
    c.executemany("insert into orders values (?, ?, ?)", [(1, "C1", "2026-10-02"), (2, "C2", "2026-10-05"),
                                                          (3, "C9", "2026-09-30")])
    c.commit()
    c.close()
    c = sqlite3.connect(erp)
    c.execute("create table order_lines(line_id integer primary key, ORDER_ID integer, product text, qty integer)")
    c.executemany("insert into order_lines values (?, ?, ?, ?)", [(10, 1, "Bolt", 5), (11, 1, "Nut", 7),
                                                                  (12, 3, "Washer", 1)])
    c.execute("create table serials(line_id integer, serial text)")
    c.executemany("insert into serials values (?, ?)", [(10, "S-1"), (10, "S-2")])
    c.commit()
    c.close()
    (tmp_path / "data" / "customers.csv").write_text("id,name\nC1,Acme\nC2,Globex\n")
    s.credentials.add("sales_db", "database", {"driver": "sqlite", "database": str(sales)})
    s.credentials.add("erp_db", "database", {"driver": "sqlite", "database": str(erp)})
    s.sources.add("sales", "sql", credential="sales_db")
    s.sources.add("erp", "sql", credential="erp_db")
    s.sources.add("customers", "file", path="customers.csv")
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates" / "orders.html").write_text(TEMPLATE)
    return s


def orders_spec(**extra) -> dict:
    return {"name": "orders", "template": "templates/orders.html", "formats": ["html", "xlsx", "json", "csv"],
            "from": {"source": "sales", "query": "select id, cust, placed from orders where placed >= :since "
                                                 "order by id", "params": {"since": "2026-10-01"}},
            "datasets": {
                "lines": {"from": {"source": "erp", "query": "select * from order_lines order by line_id"},
                          "attach": {"key": "order_id", "parent_key": "id"}},          # ORDER_ID: case differs
                "serials": {"from": {"source": "erp", "query": "select * from serials"},
                            "attach": {"to": "lines", "key": "line_id", "parent_key": "line_id"}},
                "customers": {"from": {"source": "customers"},
                              "attach": {"key": "id", "parent_key": "cust", "as": "customer_name", "one": True}},
                "notes": {"from": {"source": "erp", "query": "select serial as note from serials"}},
            }, **extra}


def install(svc, spec: dict):
    cfg = svc.project.config.model_dump(mode="json", by_alias=True)
    cfg["reports"] = [spec]
    svc.project.config = ProjectConfig.model_validate(cfg)
    svc.project.save(force=True)


def test_master_detail_across_databases_and_a_file(svc, tmp_path):
    install(svc, orders_spec())
    res = svc.reports.run("orders")
    assert res.row_count == 2 and res.dataset_rows == {"lines": 3, "serials": 2, "customers": 2, "notes": 2}
    assert res.warnings == [] and res.exit_code == 0
    out = {o.format: o.path for o in res.outputs}
    html = out["html"].read_text()
    assert "<b>Acme</b>" in html and "Bolt x5 [S-1] [S-2]" in html and "Nut x7" in html
    assert '<section id="o2"><b>Globex</b>\n<i>No lines.</i>' in html          # a parent without children
    assert "Washer" not in html                                                # order 3 is not in the master
    assert "3 lines, 2 notes" in html                                          # every dataset as datasets.<name>

    nested = json.loads(out["json"].read_text())
    assert [o["id"] for o in nested] == [1, 2]
    assert nested[0]["customer_name"] == {"id": "C1", "name": "Acme"}
    assert [s["serial"] for s in nested[0]["lines"][0]["serials"]] == ["S-1", "S-2"]

    header = out["csv"].read_text(encoding="utf-8-sig").splitlines()[0]
    assert header == "id,cust,placed"                                          # attached fields are not columns

    from openpyxl import load_workbook
    wb = load_workbook(out["xlsx"])
    assert wb.sheetnames == ["orders", "lines", "serials", "customers", "notes"]
    assert [c.value for c in wb["orders"][1]] == ["id", "cust", "placed"]
    assert [c.value for c in wb["lines"][1]] == ["line_id", "ORDER_ID", "product", "qty"]   # no 'serials' column


def test_params_are_shared_and_truncated_details_warn(svc):
    spec = orders_spec()
    spec["datasets"]["lines"]["from"].update(query="select * from order_lines where line_id >= :min_line",
                                             max_rows=1)
    install(svc, spec)
    with pytest.raises(ServiceError, match="dataset 'lines'.*min_line"):
        svc.reports.run("orders")
    res = svc.reports.run("orders", params={"min_line": 10}, formats=["json"])
    assert res.exit_code == 2 and any("dataset 'lines' stopped after 1 rows" in w for w in res.warnings)


def test_workflow_step_can_supply_a_dataset(svc):
    from portwright.agents.workflow import run_workflow
    spec = orders_spec(formats=["json"])
    install(svc, spec)
    cfg = svc.project.config.model_dump(mode="json", by_alias=True)
    cfg["workflows"] = {"wf": {"scopes": ["reports:run"], "steps": [
        {"id": "build", "type": "report", "report": "orders",
         "datasets": {"lines": [{"order_id": "2", "product": "Gear", "qty": 3, "line_id": 99}]}}]}}
    config = ProjectConfig.model_validate(cfg)
    r = asyncio.run(run_workflow(config, svc.project.root, svc.project.credentials(), "wf", None, save=False))
    assert r.ok, r.error
    out = json.loads(open(r.output["outputs"][0]["path"], encoding="utf-8").read())
    assert out[0]["lines"] == [] and out[1]["lines"][0]["product"] == "Gear"   # "2" matches 2
    cfg["workflows"]["wf"]["steps"][0]["datasets"] = {"ghost": []}
    bad = asyncio.run(run_workflow(ProjectConfig.model_validate(cfg), svc.project.root,
                                   svc.project.credentials(), "wf", None, save=False))
    assert not bad.ok and "no dataset(s) ['ghost']" in bad.error


def test_join_rules():
    spec = ReportSpec.model_validate({"name": "r", "formats": ["json"], "from": {"source": "x"}, "datasets": {
        "c": {"from": {"source": "x"}, "attach": {"key": ["a", "b"], "parent_key": ["a", "b"]}},
        "one": {"from": {"source": "x"}, "attach": {"key": "k", "parent_key": "a", "one": True}}}})
    master = [{"a": 1, "b": "x"}, {"a": 2, "b": "y"}, {"a": None, "b": "z"}]
    m, sets, warns = join(spec, master, {"c": [{"a": Decimal("1"), "b": "x", "v": 1}, {"a": 1.0, "b": "x", "v": 2},
                                               {"a": None, "b": "z"}],
                                         "one": [{"k": "1"}, {"k": 1}]})
    assert [r["v"] for r in m[0]["c"]] == [1, 2] and m[1]["c"] == [] and m[2]["c"] == []   # null keys never match
    assert m[0]["one"] == {"k": "1"} and m[1]["one"] is None
    assert any("more than one row" in w for w in warns)
    assert "c" not in master[0]                                                # the caller's rows are not changed
    assert norm_key(7.0) == norm_key(Decimal("7.00")) == norm_key(" 7 ") == "7"
    assert norm_key(1.5) != norm_key(15)


def test_dataset_config_checks():
    base = {"name": "r", "formats": ["json"], "from": {"source": "x"}}
    bad = {
        "unknown parent": {"d": {"from": {"source": "x"}, "attach": {"to": "ghost", "key": "a", "parent_key": "a"}}},
        "reserved": {"rows": {"from": {"source": "x"}}},
        "loop": {"a": {"from": {"source": "x"}, "attach": {"to": "b", "key": "k", "parent_key": "k"}},
                 "b": {"from": {"source": "x"}, "attach": {"to": "a", "key": "k", "parent_key": "k"}}},
        "same field": {"a": {"from": {"source": "x"}, "attach": {"key": "k", "parent_key": "k", "as": "z"}},
                       "b": {"from": {"source": "x"}, "attach": {"key": "k", "parent_key": "k", "as": "z"}}},
        "key count": {"a": {"from": {"source": "x"}, "attach": {"key": ["k", "j"], "parent_key": "k"}}},
    }
    for label, ds in bad.items():
        with pytest.raises(ValueError):
            ReportSpec.model_validate({**base, "datasets": ds})
        assert label
    with pytest.raises(ValueError, match="dataset 'd' reads unknown source 'nope'"):
        ProjectConfig.model_validate({"sources": {"x": {"type": "file", "path": "x.csv"}}, "reports": [
            {**base, "datasets": {"d": {"from": {"source": "nope"}}}}]})


def test_missing_join_column_is_explained(svc):
    spec = orders_spec(formats=["json"])
    spec["datasets"]["lines"]["attach"]["key"] = "orderid"
    install(svc, spec)
    with pytest.raises(ServiceError, match="dataset 'lines' has no column 'orderid'"):
        svc.reports.run("orders")


def test_cli_dataset_add_list_remove(svc):
    install(svc, {"name": "orders", "formats": ["json"], "from": orders_spec()["from"]})
    cfg, runner = str(svc.project.path), CliRunner()
    r = runner.invoke(app, ["report", "dataset", "add", "orders", "lines", "-s", "erp", "-q",
                            "select * from order_lines", "--key", "order_id", "--parent-key", "id", "-c", cfg])
    assert r.exit_code == 0, r.output
    assert "lines" in runner.invoke(app, ["report", "dataset", "list", "orders", "-c", cfg]).output
    out = json.loads(runner.invoke(app, ["report", "run", "orders", "--json", "-c", cfg]).output)
    assert out["dataset_rows"] == {"lines": 3}
    r = runner.invoke(app, ["report", "dataset", "add", "orders", "rows", "-s", "erp", "-c", cfg])
    assert r.exit_code != 0                                                    # reserved name
    assert runner.invoke(app, ["report", "dataset", "remove", "orders", "lines", "-c", cfg]).exit_code == 0
    assert not ProjectConfig.load(cfg).report("orders").datasets


def test_ai_author_sees_each_dataset_under_its_own_policy(svc, tmp_path):
    import httpx
    from portwright.agents.llm import LlmProvider, LlmResponse
    from portwright.reports.author import ReportAuthor

    class Llm(LlmProvider):
        def __init__(self):
            super().__init__("fake", None, httpx.AsyncClient())
            self.prompts = []

        async def chat(self, system, messages, tools):
            self.prompts.append(json.loads(messages[0]["content"]))
            return LlmResponse(json.dumps({"title": "Orders", "template": TEMPLATE}), [], 10, 10)

    install(svc, orders_spec(template=None, formats=["json"]))
    cfg = svc.project.config.model_dump(mode="json", by_alias=True)
    cfg["llm_models"] = {"local": {"base_url": "https://llm.test/v1", "model": "m"}}
    cfg["sources"]["erp"]["ai_exclude_columns"] = ["qty"]
    cfg["sources"]["customers"]["ai_data_access"] = "schema"
    svc.project.config = ProjectConfig.model_validate(cfg)
    svc.project.save(force=True)
    llm = Llm()
    d = asyncio.run(ReportAuthor(svc.project, provider_factory=lambda n, s: llm)
                    .design("orders", "Orders with their lines", formats=["html"], if_exists="replace"))
    data = llm.prompts[0]["data"]
    lines = data["datasets"]["lines"]
    assert lines["attached"] == {"to": "master", "as": "lines", "one": False, "key": ["order_id"],
                                 "parent_key": ["id"]}
    assert "qty" not in [c["name"] for c in lines["columns"]]                  # excluded on its own source
    assert all("qty" not in r for r in lines.get("rows", []))
    cust = data["datasets"]["customers"]
    assert cust["data_access"] == "schema" and "rows" not in cust               # the file source allows schema only
    assert d.data_access == "schema" and "lines.product" in d.columns_sent       # strictest level is recorded
    assert svc.project.config.report("orders").datasets                         # the datasets are kept
    out = svc.reports.run("orders")
    assert "Bolt x5" in out.outputs[0].path.read_text()


def test_studio_editor_keeps_datasets_and_ai_settings(svc):
    spec = orders_spec(ai_data_access="schema", provenance={"generated_by": "x"})
    install(svc, spec)
    old = svc.project.config.report("orders")
    text = rvm.datasets_yaml(old)
    assert rvm.parse_datasets(text) and "attach" in text
    values = {"name": "orders", "description": "d", "template": old.template, "endpoint": None,
              "source": "sales", "query": old.from_.query, "formats": ["json"], "output": ""}
    kept = rvm.edited_report(old, {**values, "datasets": rvm.parse_datasets(text)})
    assert list(kept.datasets) == list(old.datasets) and kept.ai_data_access == "schema"
    assert kept.provenance == {"generated_by": "x"} and kept.from_.params == {"since": "2026-10-01"}
    assert not rvm.edited_report(old, {**values, "datasets": {}}).datasets
    with pytest.raises(ServiceError, match="Datasets"):
        rvm.parse_datasets("- just a list")
