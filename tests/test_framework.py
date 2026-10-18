import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient

from portwright.config import ApiKeyRecord, ConfigError, EndpointSpec, ParamSpec, ProjectConfig, SourceSpec
from portwright.security.hashing import generate_api_key, hash_api_key
from portwright.server import create_app

SECRET = "x" * 48


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("PORTWRIGHT_JWT_SECRET", SECRET)
    (tmp_path / "data").mkdir()
    db = tmp_path / "data" / "t.db"
    c = sqlite3.connect(db)
    c.execute("create table items(id integer primary key, name text, secret text)")
    c.execute("insert into items(name, secret) values ('a','s1'),('b','s2')")
    c.commit()
    (tmp_path / "data" / "rows.json").write_text('[{"k":"1","v":"x"},{"k":"2","v":"y"}]')
    (tmp_path / "outside.csv").write_text("a\n1\n")
    (tmp_path / "hmod.py").write_text("def hello(name: str):\n    return {'hi': name}\n")
    monkeypatch.setenv("DB", f"sqlite:///{db}")

    key_id, key = generate_api_key()
    cfg = ProjectConfig(
        sources={
            "db": SourceSpec(type="sql", url="${ENV:DB}", read_only=True),
            "rw": SourceSpec(type="sql", url="${ENV:DB}", read_only=False),
            "f": SourceSpec(type="file", path="rows.json"),
            "fn": SourceSpec(type="callable", module="hmod"),
            "up": SourceSpec(type="rest", base_url="https://api.example.com"),
        },
        endpoints=[
            EndpointSpec(name="items", path="/items", source="db", operation="list", table="items",
                         key="id", columns=["id", "name"], scopes=["r"]),
            EndpointSpec(name="item-create", path="/items", method="POST", source="rw", operation="create",
                         table="items", key="id", columns=["id", "name"], scopes=["w"],
                         params=[ParamSpec(name="name", location="body", required=True)]),
            EndpointSpec(name="rows", path="/rows/{k}", source="f", key="k", scopes=["r"]),
            EndpointSpec(name="hello", path="/hello", source="fn", target="hello", public=True,
                         params=[ParamSpec(name="name", required=True, max_length=10)], rate_limit=2),
            EndpointSpec(name="up", path="/up/{id}", source="up", upstream_path="/things/{id}", scopes=["r"],
                         params=[ParamSpec(name="id", type="integer", location="path", required=True)]),
        ],
    )
    cfg.security.api_keys.append(ApiKeyRecord(id=key_id, name="t", hash=hash_api_key(key),
                                              scopes=["r"], created="2026-01-01T00:00:00+00:00"))
    cfg.security.allowed_callable_modules = ["hmod"]
    cfg.security.allowed_upstream_hosts = ["api.example.com"]
    cfg.security.audit_log = None
    return cfg, tmp_path, key


def client(cfg, root):
    return TestClient(create_app(cfg, project_root=root))


def test_auth_and_scopes(project):
    cfg, root, key = project
    with client(cfg, root) as c:
        assert c.get("/api/items").status_code == 401
        r = c.get("/api/items", headers={"X-API-Key": key})
        assert r.status_code == 200
        assert all("secret" not in i for i in r.json()["items"])          # column allow-list
        assert c.post("/api/items", json={"name": "z"}, headers={"X-API-Key": key}).status_code == 403


def test_jwt_flow(project):
    cfg, root, _ = project
    from portwright.config import UserRecord
    from portwright.security.hashing import hash_password
    cfg.security.users.append(UserRecord(username="u", password_hash=hash_password("pw-long-enough"), scopes=["w"]))
    with client(cfg, root) as c:
        tok = c.post("/api/auth/token", json={"username": "u", "password": "pw-long-enough"}).json()["access_token"]
        r = c.post("/api/items", json={"name": "z"}, headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 201
        assert c.post("/api/items", json={"name": "z", "secret": "x"},
                      headers={"Authorization": f"Bearer {tok}"}).status_code == 422
        assert c.get("/api/items", headers={"Authorization": "Bearer abc"}).status_code == 401


def test_file_and_callable(project):
    cfg, root, key = project
    with client(cfg, root) as c:
        assert c.get("/api/rows/2", headers={"X-API-Key": key}).json() == {"k": "2", "v": "y"}
        assert c.get("/api/rows/9", headers={"X-API-Key": key}).status_code == 404
        assert c.get("/api/hello?name=bob").json() == {"hi": "bob"}
        assert c.get("/api/hello?name=" + "x" * 50).status_code == 422          # max_length
        assert c.get("/api/hello?name=bob").status_code == 429                  # rate_limit=2


def test_rest_upstream(project):
    cfg, root, key = project
    seen = {}

    def handler(req: httpx.Request):
        seen["url"] = str(req.url)
        seen["auth"] = req.headers.get("x-api-key")
        return httpx.Response(200, json={"id": 7})

    app = create_app(cfg, project_root=root)
    with TestClient(app) as c:
        app.state.sources["up"].client = httpx.AsyncClient(
            base_url="https://api.example.com", transport=httpx.MockTransport(handler))
        assert c.get("/api/up/7", headers={"X-API-Key": key}).json() == {"id": 7}
    assert seen["url"] == "https://api.example.com/things/7"
    assert seen["auth"] is None                                     # caller creds never forwarded


def test_guards(project):
    cfg, root, _ = project
    # SSRF: host not allow-listed
    cfg.security.allowed_upstream_hosts = []
    with pytest.raises(ConfigError, match="allowed_upstream_hosts"):
        create_app(cfg, project_root=root)
    cfg.security.allowed_upstream_hosts = ["api.example.com"]
    # path traversal
    cfg.sources["f"].path = "../outside.csv"
    with pytest.raises(ConfigError, match="outside the data folder"):
        create_app(cfg, project_root=root)
    cfg.sources["f"].path = "rows.json"
    # callable allow-list
    cfg.security.allowed_callable_modules = []
    with pytest.raises(ConfigError, match="allowed_callable_modules"):
        create_app(cfg, project_root=root)
    cfg.security.allowed_callable_modules = ["hmod"]
    # writes on read-only source
    cfg.endpoints.append(EndpointSpec(name="bad", path="/bad", method="POST", source="db",
                                      query="delete from items", scopes=["w"]))
    with pytest.raises(ConfigError, match="read-only"):
        create_app(cfg, project_root=root)


def test_undeclared_bind_rejected(project):
    cfg, root, _ = project
    cfg.endpoints.append(EndpointSpec(name="q", path="/q", source="db",
                                      query="select * from items where id = :id", scopes=["r"]))
    with pytest.raises(ConfigError, match="undeclared"):
        create_app(cfg, project_root=root)
