import sqlite3

import pytest

from portwright.services import Project, ServiceError, Services


@pytest.fixture
def svc(tmp_path):
    s = Services(Project.init(tmp_path, "t"))
    c = sqlite3.connect(tmp_path / "data" / "hr.db")
    c.execute("create table emp(id integer primary key, name varchar(20) not null, dept text)")
    c.execute("insert into emp(name, dept) values ('A','ENG'), ('B','OPS')")
    c.commit()
    (tmp_path / "data" / "t.csv").write_text("a,b\n1,2\n")
    s.credentials.add("db", "database", {"driver": "sqlite", "database": str(tmp_path / "data" / "hr.db")})
    s.sources.add("hr", "sql", credential="db", read_write=True)
    return s


def test_sources_test_and_introspect(svc):
    assert svc.sources.test("hr").ok
    assert svc.sources.tables("hr") == ["emp"]
    cols = {c["name"]: c for c in svc.sources.columns("hr", "emp")}
    assert cols["id"]["primary_key"] and cols["name"]["nullable"] is False
    svc.sources.add("f", "file", path="t.csv")
    r = svc.sources.test("f")
    assert r.ok and r.columns == ["a", "b"] and r.rows == [{"a": "1", "b": "2"}]


def test_import_file_copies_into_data(svc, tmp_path):
    src = tmp_path / "elsewhere.csv"
    src.write_text("x\n1\n")
    assert svc.sources.import_file(src) == "elsewhere.csv"
    assert (tmp_path / "data" / "elsewhere.csv").read_text() == "x\n1\n"


def test_endpoint_crud_access_call_and_snippets(svc):
    svc.endpoints.crud("emp", "hr", "emp", "id", fields=["name:string:required", "dept"], filters=["dept"])
    assert svc.runtime.validate().ok
    r = svc.runtime.call("emp-list", {"dept": "OPS"})
    assert r.ok and [i["name"] for i in r.data["items"]] == ["B"]
    assert svc.runtime.call("emp-create", {"name": "C"}).status == 201
    with pytest.raises(ServiceError, match="unknown param"):
        svc.runtime.call("emp-list", {"salary": "1"})
    svc.endpoints.set_access("emp-list", public=True)
    assert svc.project.config.endpoint("emp-list").public
    sn = svc.runtime.snippets("emp-get", {"id": "7"})
    assert "/api/emp/7" in sn["curl"] and "Invoke-RestMethod" in sn["powershell"]
    with pytest.raises(ServiceError, match="still used"):
        svc.sources.remove("hr")


def test_keys_users_and_scopes(svc):
    svc.endpoints.crud("emp", "hr", "emp", "id", read_only=True)
    assert "emp:read" in svc.access.known_scopes()
    k = svc.access.create_key("app", ["emp:read"], 30)
    assert k.plaintext.startswith("pw_") and k.plaintext not in svc.project.path.read_text()
    svc.access.revoke_key(k.id)
    assert svc.access.keys()[0]["status"] == "revoked"
    with pytest.raises(ServiceError, match="12 characters"):
        svc.access.add_user("bob", "short", [])


def test_credentials_list_rotate_remove(svc):
    creds = svc.credentials.list()
    assert creds[0]["name"] == "db" and creds[0]["used_by"] == ["source:hr"]
    with pytest.raises(ServiceError, match="used by"):
        svc.credentials.remove("db")
    svc.credentials.add("tok", "bearer", {"token": "secret-value-123"}, expires_days=5)
    assert svc.credentials.masked("tok")["fields"]["token"] == "********"
    assert next(c for c in svc.credentials.list() if c["name"] == "tok")["expiry_state"] == "soon"
    svc.credentials.rotate("tok", {"token": "new-secret-456"})
    assert svc.credentials.get("tok").fields["token"] == "new-secret-456"
    svc.credentials.remove("tok")


def test_save_is_atomic_backed_up_and_detects_outside_edits(svc):
    p = svc.project
    p.config.project = "renamed"
    p.save()
    assert p.path.with_suffix(".yaml.bak").exists()
    p.path.write_text(p.path.read_text() + "\n# edited elsewhere\n")
    p.config.project = "again"
    with pytest.raises(ServiceError, match="changed outside"):
        p.save()
    p.reload()
    assert p.config.project == "renamed"


def test_invalid_save_is_refused_and_file_untouched(svc):
    before = svc.project.path.read_text()
    with pytest.raises(ServiceError):
        svc.endpoints.add("x", "nope", "/x")
    assert svc.project.path.read_text() == before
    assert svc.project.config.endpoint("x") is None            # memory rolled back too
    svc.sources.add("f", "file", path="t.csv")                 # later saves still work
