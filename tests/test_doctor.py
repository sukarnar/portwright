"""portwright doctor and the PDF backend probe."""
import json

import pytest
from typer.testing import CliRunner

from portwright.cli import app
from portwright.config import ProjectConfig
from portwright.doctor import pdf_checks, run_checks
from portwright.reports import engine
from portwright.reports.engine import PdfBackend
from portwright.services import Project, Services

WEASY_OK = PdfBackend("weasyprint", True, "version 66", "full CSS")
WEASY_BROKEN = PdfBackend("weasyprint", False, "cannot load GTK", "full CSS")
PISA_OK = PdfBackend("xhtml2pdf", True, "version 0.2", "limited CSS")
PISA_ABSENT = PdfBackend("xhtml2pdf", False, "not installed", "limited CSS", installed=False)


def use(monkeypatch, *backends):
    monkeypatch.setattr(engine, "pdf_backends", lambda: tuple(backends))


@pytest.fixture
def svc(tmp_path):
    s = Services(Project.init(tmp_path, "t"))
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates" / "t.html").write_text("<p>{{ row_count }}</p>")
    (tmp_path / "data" / "x.csv").write_text("a\n1\n")
    s.sources.add("x", "file", path="x.csv")
    cfg = s.project.config.model_dump(mode="json", by_alias=True)
    cfg["reports"] = [{"name": "monthly", "template": "templates/t.html", "from": {"source": "x"},
                       "formats": ["pdf", "xlsx"]}]
    s.project.config = ProjectConfig.model_validate(cfg)
    s.project.save()                  # the CLI test reads it from disk
    return s


def by_name(checks):
    return {c.name: c for c in checks}


def test_active_backend_prefers_weasyprint(monkeypatch):
    use(monkeypatch, WEASY_OK, PISA_OK)
    assert engine.active_pdf_backend().name == "weasyprint"
    use(monkeypatch, WEASY_BROKEN, PISA_OK)
    assert engine.active_pdf_backend().name == "xhtml2pdf"
    use(monkeypatch, WEASY_BROKEN, PISA_ABSENT)
    assert engine.active_pdf_backend() is None


def test_render_pdf_without_backend_points_to_doctor(monkeypatch, tmp_path):
    use(monkeypatch, WEASY_BROKEN, PISA_ABSENT)
    with pytest.raises(ImportError, match="portwright doctor"):
        engine.render_pdf("<p>x</p>", tmp_path / "x.pdf")


def test_pdf_checks_statuses(monkeypatch, svc):
    use(monkeypatch, WEASY_OK, PISA_ABSENT)
    c = by_name(pdf_checks(svc.project.config))
    assert c["PDF output"].status == "ok" and c["PDF: xhtml2pdf"].status == "off"

    use(monkeypatch, WEASY_BROKEN, PISA_OK)          # works, with less CSS
    c = by_name(pdf_checks(svc.project.config))
    assert c["PDF: weasyprint"].status == "warn" and c["PDF output"].status == "warn"
    assert "xhtml2pdf" in c["PDF output"].detail

    use(monkeypatch, WEASY_BROKEN, PISA_ABSENT)      # nothing works: a project with PDF reports fails
    assert by_name(pdf_checks(svc.project.config))["PDF output"].status == "fail"
    assert "monthly" in by_name(pdf_checks(svc.project.config))["PDF output"].detail
    assert by_name(pdf_checks(None))["PDF output"].status == "warn"     # no project: only a warning


def test_dll_directory_that_does_not_exist(monkeypatch, tmp_path):
    use(monkeypatch, WEASY_BROKEN, PISA_OK)
    monkeypatch.setenv("WEASYPRINT_DLL_DIRECTORIES", str(tmp_path / "nope"))
    assert by_name(pdf_checks())["WEASYPRINT_DLL_DIRECTORIES"].status == "warn"


def test_validate_warns_about_pdf_backend(monkeypatch, svc):
    use(monkeypatch, WEASY_BROKEN, PISA_ABSENT)
    assert any("cannot make PDFs: monthly" in w for w in svc.runtime.validate().warnings)
    use(monkeypatch, WEASY_BROKEN, PISA_OK)
    assert any("PDFs use xhtml2pdf" in w for w in svc.runtime.validate().warnings)
    use(monkeypatch, WEASY_OK, PISA_OK)
    assert not any("PDF" in w for w in svc.runtime.validate().warnings)


def test_run_checks_lists_core_and_addons(monkeypatch):
    use(monkeypatch, WEASY_OK, PISA_OK)
    names = by_name(run_checks())
    assert {"Python", "Jinja2 (templates)", "PDF output", "MCP", "Studio (flet)", "Keyring"} <= set(names)
    assert names["Python"].status == "ok"


def test_doctor_reads_the_project_env(monkeypatch, svc, tmp_path):
    use(monkeypatch, WEASY_OK, PISA_OK)
    monkeypatch.delenv("WEASYPRINT_DLL_DIRECTORIES", raising=False)
    monkeypatch.delenv("WEASYPRINT_ALL_DIRECTORIES", raising=False)
    dlls = tmp_path / "pango" / "bin"
    dlls.mkdir(parents=True)
    env = svc.project.root / ".env"                # first line, after a BOM as Windows PowerShell writes it
    env.write_text(f"﻿WEASYPRINT_DLL_DIRECTORIES={dlls}\n" + env.read_text() + "WEASYPRINT_ALL_DIRECTORIES=x\n",
                   encoding="utf-8")
    seen = {}
    real = engine.active_pdf_backend

    def spy():                                   # what WeasyPrint would see when it is probed
        seen["dll"] = __import__("os").environ.get("WEASYPRINT_DLL_DIRECTORIES")
        return real()
    monkeypatch.setattr(engine, "active_pdf_backend", spy)
    r = CliRunner().invoke(app, ["doctor", "-c", str(svc.project.path), "--json"])
    out = {c["name"]: c for c in json.loads(r.output)}
    assert seen["dll"] == str(dlls)
    assert "did you mean WEASYPRINT_DLL_DIRECTORIES" in out["WEASYPRINT_ALL_DIRECTORIES"]["detail"]
    monkeypatch.setenv("WEASYPRINT_DLL_DIRECTORIES", "C:/elsewhere")      # a machine-wide value wins
    r = CliRunner().invoke(app, ["doctor", "-c", str(svc.project.path), "--json"])
    details = [c["detail"] for c in json.loads(r.output) if c["name"] == "WEASYPRINT_DLL_DIRECTORIES"]
    assert any("machine-wide value C:/elsewhere wins" in d for d in details)


def test_doctor_cli_exit_code_and_json(monkeypatch, svc):
    cfg = str(svc.project.path)
    runner = CliRunner()
    use(monkeypatch, WEASY_BROKEN, PISA_OK)
    r = runner.invoke(app, ["doctor", "-c", cfg])
    assert r.exit_code == 0 and "PDF output" in r.output
    use(monkeypatch, WEASY_BROKEN, PISA_ABSENT)
    r = runner.invoke(app, ["doctor", "-c", cfg, "--json"])
    assert r.exit_code == 1
    out = {c["name"]: c for c in json.loads(r.output)}
    assert out["PDF output"]["status"] == "fail"
    r = runner.invoke(app, ["doctor", "-c", str(svc.project.root / "missing.yaml")])
    assert r.exit_code == 0 and "project checks skipped" in r.output
