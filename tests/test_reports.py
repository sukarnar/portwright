"""Reports: multi-format output, data sources, pagination, sandboxing and the PDF resource guard."""
import csv
import json
import sqlite3
from pathlib import Path

import pytest

from portwright.config import ReportFrom, ReportSpec
from portwright.reports import engine
from portwright.reports.engine import local_resource, render_html
from portwright.reports.service import output_stem
from portwright.services import Project, ServiceError, Services

pytest.importorskip("jinja2")

BUILTIN = Path(engine.__file__).parent / "templates"


@pytest.fixture
def svc(tmp_path):
    s = Services(Project.init(tmp_path, "t"))
    db = tmp_path / "data" / "hr.db"
    c = sqlite3.connect(db)
    c.execute("create table emp(id integer primary key, name text, dept text, salary real)")
    c.executemany("insert into emp(name, dept, salary) values (?, ?, ?)",
                  [(f"E{i}", "ENG" if i % 2 else "OPS", 1000 + i) for i in range(12)])
    c.execute("insert into emp(name, dept, salary) values ('=HYPERLINK(\"x\")', 'ENG', 1)")
    c.commit()
    c.close()
    (tmp_path / "data" / "sales.csv").write_text("region,amount\nN,10\nS,20\nE,30\n")
    s.credentials.add("db", "database", {"driver": "sqlite", "database": str(db)})
    s.sources.add("hr", "sql", credential="db")
    s.sources.add("sales", "file", path="sales.csv")
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates" / "t.html").write_text(
        "<h1>{{ report_name }}</h1>{% for r in data %}<p>{{ r.values() | first }}</p>{% endfor %}"
        "<i>{{ row_count }}</i>")
    return s


def _add(svc, **kw):
    frm = kw.pop("from_")
    svc.project.config.reports.append(ReportSpec(name=kw.pop("name", "r"), **{"from": ReportFrom(**frm)}, **kw))
    svc.project.save(force=True)


# --------------------------------------------------------------------------- formats
def test_multi_format_from_sql_query(svc, tmp_path):
    _add(svc, from_={"source": "hr", "query": "select name, dept, salary from emp where dept = :dept order by id",
                     "params": {"dept": "ENG"}},
         template="templates/t.html", formats=["html", "csv", "xlsx", "json", "md"],
         output="reports/out/{{ report_name }}-v1.2", columns=["name", "salary"], rename={"salary": "Pay"})
    res = svc.reports.run("r")
    assert [o.format for o in res.outputs] == ["html", "csv", "xlsx", "json", "md"]
    assert res.row_count == 7 and not res.truncated
    paths = {o.format: o.path for o in res.outputs}
    assert paths["csv"].name == "r-v1.2.csv"                 # dotted stem survives
    assert all(o.bytes > 0 and len(o.sha256) == 64 for o in res.outputs)

    html = paths["html"].read_text()
    assert "<h1>r</h1>" in html and "<i>7</i>" in html
    with open(paths["csv"], encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.reader(fh))
    assert rows[0] == ["name", "Pay"] and rows[1] == ["E1", "1001.0"]
    data = json.loads(paths["json"].read_text(encoding="utf-8"))
    assert data[0] == {"name": "E1", "Pay": 1001.0} and len(data) == 7
    md = paths["md"].read_text(encoding="utf-8")
    assert "| name | Pay |" in md and "| E1 | 1001.0 |" in md

    from openpyxl import load_workbook
    ws = load_workbook(paths["xlsx"]).active
    assert [c.value for c in ws[1]] == ["name", "Pay"]
    assert ws["B2"].value == 1001.0 and ws.freeze_panes == "A2"
    formula = [r for r in ws.iter_rows(min_row=2, values_only=True) if str(r[0]).startswith("=")]
    assert formula and ws.cell(row=ws.max_row, column=1).data_type == "s"   # stored as text, not a formula


def test_format_override_and_legacy_html_flag(svc):
    _add(svc, from_={"source": "sales"}, template="templates/t.html", output="reports/out/s.pdf")
    spec = svc.reports.get("r")
    assert spec.effective_formats() == ["pdf"]
    assert svc.reports.run("r", as_html=True).format == "html"
    res = svc.reports.run("r", formats=["csv"])
    assert res.output_path.suffix == ".csv" and res.row_count == 3


def test_data_formats_need_no_template(svc):
    _add(svc, from_={"source": "sales"}, formats=["json"])
    assert json.loads(svc.reports.run("r").output_path.read_text())[2] == {"region": "E", "amount": "30"}
    with pytest.raises(ServiceError, match="need a template"):
        svc.reports.run("r", formats=["html"])
    with pytest.raises(ValueError, match="need a 'template'"):
        ReportSpec(name="x", **{"from": {"source": "sales"}}, formats=["pdf"])
    with pytest.raises(ValueError, match="unknown report format"):
        svc.reports.run("r", formats=["docx"])


def test_pdf_renderer_gets_template_dir(svc, monkeypatch, tmp_path):
    seen = {}

    def fake_pdf(html, out, *, base_dir=None, base_url=None):
        seen.update(html=html, base_dir=base_dir)
        out.write_bytes(b"%PDF-fake")
        return out

    monkeypatch.setattr(engine, "render_pdf", fake_pdf)
    _add(svc, from_={"source": "sales"}, template="templates/t.html", formats=["pdf", "html"])
    res = svc.reports.run("r")
    assert [o.format for o in res.outputs] == ["pdf", "html"]
    assert seen["base_dir"] == tmp_path / "templates" and "<i>3</i>" in seen["html"]


# --------------------------------------------------------------------------- data in
def test_rows_supplied_by_caller(svc):
    _add(svc, from_={"source": "sales"}, formats=["json"])
    res = svc.reports.run("r", data={"rows": [{"a": 1}, {"a": 2}], "count": 2})     # a SQL tool result
    assert res.row_count == 2 and json.loads(res.output_path.read_text()) == [{"a": 1}, {"a": 2}]
    inline = svc.reports.render_inline([{"x": "y"}], ["csv", "md"], "reports/out/inline")
    assert [o.path.name for o in inline.outputs] == ["inline.csv", "inline.md"]


def test_sql_query_rules(svc):
    _add(svc, name="w", from_={"source": "hr", "query": "delete from emp"}, formats=["json"])
    with pytest.raises(ServiceError, match="must be a SELECT"):
        svc.reports.run("w")
    _add(svc, name="two", from_={"source": "hr", "query": "select 1; select 2"}, formats=["json"])
    with pytest.raises(ServiceError, match="one statement"):
        svc.reports.run("two")
    _add(svc, name="p", from_={"source": "hr", "query": "select * from emp where dept = :dept"}, formats=["json"])
    with pytest.raises(ServiceError, match=r"needs param\(s\) \['dept'\]"):
        svc.reports.run("p")
    # empty value binds NULL, so '(:dept is null or ...)' style queries work
    _add(svc, name="opt", from_={"source": "hr", "query": "select count(*) n from emp where (:dept is null or dept = :dept)"},
         formats=["json"])
    assert json.loads(svc.reports.run("opt", params={"dept": ""}).output_path.read_text()) == [{"n": 13}]


def test_legacy_sql_param_not_mutated(svc):
    _add(svc, from_={"source": "hr"}, formats=["json"])
    params = {"_sql": "select count(*) n from emp"}
    res = svc.reports.run("r", params=params)
    assert json.loads(res.output_path.read_text()) == [{"n": 13}]
    assert params == {"_sql": "select count(*) n from emp"}


def test_query_needs_sql_source(svc):
    cfg = svc.project.config.model_dump(mode="json", by_alias=True)
    cfg["reports"] = [{"name": "bad", "from": {"source": "sales", "query": "select 1"}, "formats": ["json"]}]
    from portwright.config import ProjectConfig
    with pytest.raises(ValueError, match="needs a SQL source"):
        ProjectConfig.model_validate(cfg)


def test_endpoint_reads_every_page_and_truncates(svc):
    svc.endpoints.crud("emp", "hr", "emp", "id", read_only=True)
    ep = svc.project.config.endpoint("emp-list")
    ep.max_limit = 5                                             # force several pages
    svc.project.save(force=True)
    _add(svc, from_={"endpoint": "emp-list"}, formats=["json"])
    res = svc.reports.run("r")
    assert res.row_count == 13 and not res.truncated
    _add(svc, name="cap", from_={"endpoint": "emp-list", "max_rows": 7}, formats=["json"])
    res = svc.reports.run("cap")
    assert res.row_count == 7 and res.truncated and res.warnings
    _add(svc, name="one", from_={"endpoint": "emp-list", "paginate": False}, formats=["json"])
    assert svc.reports.run("one").row_count == 5


# --------------------------------------------------------------------------- sandbox
def test_templates_are_sandboxed(tmp_path):
    (tmp_path / "evil.html").write_text("{{ ''.__class__.__mro__[1].__subclasses__() }}")
    with pytest.raises(ValueError, match="Render error"):
        render_html("evil.html", [], project_root=tmp_path)
    (tmp_path / "ok.html").write_text("{{ data | length }} {{ now | strftime('%Y') }}")
    assert render_html("ok.html", [{"a": 1}], project_root=tmp_path).startswith("1 ")


@pytest.mark.parametrize("name", sorted(p.name for p in BUILTIN.glob("*.html")))
def test_builtin_templates_render_in_sandbox(name):
    rows = [{"id": 1, "name": "A", "category": "X", "amount": 10.5, "date": "2024-01-01", "action": "create",
             "user": "u", "description": "d", "quantity": 2, "unit_price": 5.25, "debit": 1, "credit": 0,
             "balance": 1, "account_code": "1", "account_name": "Cash", "reference": "R",
             "qty": 2, "price": 5.25, "total": 10.5}]
    keys = ("client_address client_email client_name company_address company_email company_name currency "
            "invoice_number notes payment_terms period_end period_start").split()
    v = {k: "x" for k in keys} | {"tax_rate": 10}
    html = render_html(str(BUILTIN / name), rows, project_root=BUILTIN, report_name="t", extra_vars=v)
    assert "<html" in html.lower()


def test_output_stem():
    assert output_stem("a.b/x.v2.pdf") == str(Path("a.b/x.v2"))
    assert output_stem("reports/x.v2") == "reports/x.v2"
    assert output_stem("r.XLSX") == "r"


def test_pdf_resource_guard(tmp_path):
    base = tmp_path / "tpl"
    (base / "img").mkdir(parents=True)
    logo = base / "img" / "logo.png"
    logo.write_bytes(b"x")
    (tmp_path / "secret.txt").write_text("s")
    assert local_resource("img/logo.png", base) == logo.resolve()
    assert local_resource(logo.as_uri(), base) == logo.resolve()
    assert local_resource("../secret.txt", base) is None
    assert local_resource((tmp_path / "secret.txt").as_uri(), base) is None
    assert local_resource("https://example.com/x.png", base) is None
    assert local_resource("http://169.254.169.254/latest", base) is None
    assert local_resource("file://server/share/x.png", base) is None
    assert local_resource("//server/share/x.png", base) is None
    assert local_resource("img/missing.png", base) is None
    assert local_resource("img/logo.png", None) is None
