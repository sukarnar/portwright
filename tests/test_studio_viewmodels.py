import pytest

pytest.importorskip("flet")

from portwright.services import Project, ServiceError, Services
from portwright.studio.viewmodels import (Recent, credential_errors, credential_fields, dashboard, parse_kv_lines,
                                         save_source, source_values)


def test_credential_fields_follow_driver():
    assert [f.name for f in credential_fields("database", "sqlite")] == ["driver", "database"]
    ora = [f.name for f in credential_fields("database", "oracle+oracledb")]
    assert "service_name" in ora and "database" not in ora
    pg = [f.name for f in credential_fields("database", "postgresql+psycopg")]
    assert "sid" not in pg and "database" in pg
    assert next(f for f in credential_fields("database", "oracle+oracledb") if f.name == "password").secret


def test_credential_errors():
    assert credential_errors("database", {"driver": "oracle+oracledb"}) == {"host": "host or dsn is required"}
    assert credential_errors("database", {"driver": "sqlite"}) == {}
    assert credential_errors("bearer", {}) == {"token": "required"}


def test_parse_kv_lines():
    assert parse_kv_lines("A=1\n\nB = two") == {"A": "1", "B": "two"}
    with pytest.raises(ServiceError):
        parse_kv_lines("oops")


@pytest.fixture
def svc(tmp_path):
    s = Services(Project.init(tmp_path, "t", actor="studio"))
    (tmp_path / "data" / "x.csv").write_text("a\n1\n")
    s.credentials.add("db", "database", {"driver": "sqlite", "database": "data/x.db"}, expires_days=3)
    return s


def test_save_and_edit_source_round_trip(svc):
    save_source(svc, {"name": "hr", "type": "sql", "credential": "db", "read_write": True})
    v = source_values(svc, "hr")
    assert v["credential"] == "db" and v["read_write"] is True
    v["read_write"] = False
    save_source(svc, v, editing=True)
    assert svc.project.config.sources["hr"].read_only is True
    save_source(svc, {"name": "api", "type": "rest", "base_url": "https://api.x.com", "allow_host": True,
                      "headers": "X-Tenant=acme"})
    assert "api.x.com" in svc.project.config.security.allowed_upstream_hosts
    assert source_values(svc, "api")["allow_host"] is True
    with pytest.raises(ServiceError, match="credential"):
        save_source(svc, {"name": "nodb", "type": "sql"})


def test_dashboard_summary(svc):
    save_source(svc, {"name": "f", "type": "file", "path": "x.csv"})
    svc.endpoints.add("f-list", "f", "/f", public=True)
    svc.access.create_key("app", ["*"], 5)
    d = dashboard(svc)
    assert d["sources"] == 1 and d["public_endpoints"] == ["f-list"]
    assert [c["name"] for c in d["expiring"]] == ["db"] and d["keys_expiring"][0]["name"] == "app"
    assert d["vault"]["key_present"]


def test_studio_actor_in_audit(svc):
    svc.access.create_key("app", ["*"], 30)
    log = (svc.project.root / "logs" / "audit.jsonl").read_text()
    assert '"actor": "studio:' in log and '"event": "api_key_created"' in log


def test_recent_projects(tmp_path, svc):
    r = Recent(tmp_path / "studio.json")
    r.add(svc.project.path)
    r.add(svc.project.path)
    assert r.list() == [str(svc.project.path)]
