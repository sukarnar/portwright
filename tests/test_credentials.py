import json
import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient

from portwright.config import ApiKeyRecord, ConfigError, EndpointSpec, ProjectConfig, SourceSpec
from portwright.credentials import (Credential, CredentialError, CredentialManager, EncryptedFileVault,
                                   EnvProvider, build_sql_url, generate_master_key)
from portwright.security.hashing import generate_api_key, hash_api_key
from portwright.server import create_app


@pytest.fixture
def vault(tmp_path, monkeypatch):
    monkeypatch.setenv("PORTWRIGHT_MASTER_KEY", generate_master_key())
    monkeypatch.setenv("PORTWRIGHT_JWT_SECRET", "j" * 48)
    return EncryptedFileVault(tmp_path / ".portwright" / "credentials.vault")


def test_vault_roundtrip_and_no_plaintext(vault):
    vault.put(Credential("db", "database", {"driver": "postgresql+psycopg", "host": "h",
                                            "username": "u", "password": "p@ss/w:rd#"}))
    assert "p@ss" not in vault.path.read_text()
    assert vault.get("db").fields["password"] == "p@ss/w:rd#"
    assert "p@ss" not in repr(vault.get("db"))


def test_wrong_key_and_tamper_detected(vault, monkeypatch):
    vault.put(Credential("a", "bearer", {"token": "secret-token"}))
    data = json.loads(vault.path.read_text())
    # swap type -> associated data no longer matches
    data["credentials"]["a"]["type"] = "generic"
    vault.path.write_text(json.dumps(data))
    with pytest.raises(CredentialError, match="integrity"):
        EncryptedFileVault(vault.path).get("a")
    monkeypatch.setenv("PORTWRIGHT_MASTER_KEY", generate_master_key())
    with pytest.raises(CredentialError, match="wrong master key"):
        EncryptedFileVault(vault.path).get("a")


def test_passphrase_and_rekey(vault, monkeypatch):
    monkeypatch.setenv("PORTWRIGHT_MASTER_KEY", "a long passphrase here!")
    v = EncryptedFileVault(vault.path)
    v.put(Credential("a", "bearer", {"token": "t1"}))
    assert v.rekey(generate_master_key()) == 1
    assert EncryptedFileVault(vault.path).get("a").fields["token"] == "t1"


def test_sql_url_escaping_oracle():
    url = build_sql_url(Credential("o", "database", {"driver": "oracle+oracledb", "host": "db", "port": "1521",
                                                     "service_name": "PDB1", "username": "u", "password": "a@b#c/d"}))
    assert url.render_as_string(hide_password=False) == "oracle+oracledb://u:a%40b%23c%2Fd@db:1521?service_name=PDB1"


def test_env_provider_and_chain(vault, monkeypatch):
    monkeypatch.setenv("RF_CRED_CRM_TYPE", "api_key")
    monkeypatch.setenv("RF_CRED_CRM_KEY", "k-123")
    vault.put(Credential("crm", "api_key", {"key": "from-vault"}))
    assert CredentialManager([EnvProvider(), vault]).get("crm").fields["key"] == "k-123"
    assert CredentialManager([vault, EnvProvider()]).get("crm").fields["key"] == "from-vault"


def test_expired_credential_rejected(vault):
    vault.put(Credential("x", "bearer", {"token": "t"}, expires="2000-01-01T00:00:00+00:00"))
    with pytest.raises(CredentialError, match="expired"):
        CredentialManager([vault]).get("x")


def _project(tmp_path, vault, sources, endpoints):
    key_id, key = generate_api_key()
    cfg = ProjectConfig(sources=sources, endpoints=endpoints)
    cfg.security.api_keys.append(ApiKeyRecord(id=key_id, name="t", hash=hash_api_key(key), scopes=["*"],
                                              created="2026-01-01T00:00:00+00:00"))
    cfg.security.audit_log = None
    cfg.security.allowed_upstream_hosts = ["api.example.com", "login.example.com"]
    return cfg, {"X-API-Key": key}


def test_sql_source_uses_credential(tmp_path, vault):
    db = tmp_path / "t.db"
    c = sqlite3.connect(db)
    c.execute("create table t(id integer)")
    c.execute("insert into t values (1)")
    c.commit()
    vault.put(Credential("db", "database", {"driver": "sqlite", "database": str(db)}))
    cfg, H = _project(tmp_path, vault, {"s": SourceSpec(type="sql", credential="db")},
                      [EndpointSpec(name="t", path="/t", source="s", query="select id from t", scopes=["r"])])
    with TestClient(create_app(cfg, project_root=tmp_path, credentials=CredentialManager([vault]))) as cl:
        assert cl.get("/api/t", headers=H).json()["items"] == [{"id": 1}]


def test_missing_credential_fails_fast(tmp_path, vault):
    cfg, _ = _project(tmp_path, vault, {"s": SourceSpec(type="sql", credential="nope")}, [])
    with pytest.raises(ConfigError, match="'nope' not found"):
        create_app(cfg, project_root=tmp_path, credentials=CredentialManager([vault]))


@pytest.mark.parametrize("ctype,fields,expect", [
    ("bearer", {"token": "tok"}, lambda r: r.headers["authorization"] == "Bearer tok"),
    ("basic", {"username": "u", "password": "p"}, lambda r: r.headers["authorization"].startswith("Basic ")),
    ("api_key", {"key": "k", "query_param": "apikey"}, lambda r: r.url.params["apikey"] == "k"),
])
def test_rest_static_auth(tmp_path, vault, monkeypatch, ctype, fields, expect):
    seen = []
    _mock_http(monkeypatch, lambda r: seen.append(r) or httpx.Response(200, json={"ok": True}))
    vault.put(Credential("up", ctype, fields))
    cfg, H = _project(tmp_path, vault, {"api": SourceSpec(type="rest", base_url="https://api.example.com",
                                                          credential="up")},
                      [EndpointSpec(name="x", path="/x", source="api", scopes=["r"])])
    with TestClient(create_app(cfg, project_root=tmp_path, credentials=CredentialManager([vault]))) as cl:
        assert cl.get("/api/x", headers=H).json() == {"ok": True}
    assert expect(seen[-1])
    assert "x-api-key" not in {k.lower() for k in seen[-1].headers if seen[-1].headers[k] == H["X-API-Key"]}


def test_rest_oauth2_fetches_and_refreshes(tmp_path, vault, monkeypatch):
    tokens = iter(["t1", "t2"])
    calls = []

    def handler(r: httpx.Request):
        if r.url.host == "login.example.com":
            calls.append("token")
            assert r.headers["authorization"].startswith("Basic ")
            return httpx.Response(200, json={"access_token": next(tokens), "expires_in": 3600})
        calls.append(r.headers["authorization"])
        if r.headers["authorization"] == "Bearer t1" and len(calls) > 2:
            return httpx.Response(401)              # upstream revoked t1 -> refresh
        return httpx.Response(200, json={"ok": True})

    _mock_http(monkeypatch, handler)
    vault.put(Credential("oa", "oauth2", {"token_url": "https://login.example.com/oauth/token",
                                          "client_id": "id", "client_secret": "sec", "scope": "read"}))
    cfg, H = _project(tmp_path, vault, {"api": SourceSpec(type="rest", base_url="https://api.example.com",
                                                          credential="oa")},
                      [EndpointSpec(name="x", path="/x", source="api", scopes=["r"])])
    with TestClient(create_app(cfg, project_root=tmp_path, credentials=CredentialManager([vault]))) as cl:
        assert cl.get("/api/x", headers=H).status_code == 200
        assert cl.get("/api/x", headers=H).status_code == 200
    assert calls == ["token", "Bearer t1", "Bearer t1", "token", "Bearer t2"]


def test_oauth_token_host_must_be_allowlisted(tmp_path, vault):
    vault.put(Credential("oa", "oauth2", {"token_url": "https://evil.example.net/token",
                                          "client_id": "id", "client_secret": "s"}))
    cfg, _ = _project(tmp_path, vault, {"api": SourceSpec(type="rest", base_url="https://api.example.com",
                                                          credential="oa")},
                      [EndpointSpec(name="x", path="/x", source="api", scopes=["r"])])
    with pytest.raises(ConfigError, match="not in security.allowed_upstream_hosts"):
        create_app(cfg, project_root=tmp_path, credentials=CredentialManager([vault]))


def test_cred_reference_in_headers(tmp_path, vault, monkeypatch):
    seen = []
    _mock_http(monkeypatch, lambda r: seen.append(r) or httpx.Response(200, json={}))
    vault.put(Credential("g", "generic", {"tenant": "acme", "sig": "zzz"}))
    cfg, H = _project(tmp_path, vault, {"api": SourceSpec(
        type="rest", base_url="https://api.example.com",
        headers={"X-Tenant": "${CRED:g.tenant}", "X-Sig": "${CRED:g.sig}"})},
        [EndpointSpec(name="x", path="/x", source="api", scopes=["r"])])
    with TestClient(create_app(cfg, project_root=tmp_path, credentials=CredentialManager([vault]))) as cl:
        cl.get("/api/x", headers=H)
    assert seen[-1].headers["x-tenant"] == "acme" and seen[-1].headers["x-sig"] == "zzz"


def _mock_http(monkeypatch, handler):
    orig = httpx.AsyncClient.__init__

    def init(self, *a, **kw):
        kw["transport"] = httpx.MockTransport(handler)
        orig(self, *a, **kw)
    monkeypatch.setattr(httpx.AsyncClient, "__init__", init)


def test_hashicorp_provider_kv2():
    from portwright.credentials import HashiCorpVaultProvider
    store = {}

    def handler(r: httpx.Request):
        assert r.headers["x-vault-token"] == "s.tok"
        path = r.url.path
        if r.method == "POST":
            store[path] = json.loads(r.content)["data"]
            return httpx.Response(200, json={})
        if r.method == "GET":
            return httpx.Response(200, json={"data": {"data": dict(store[path])}}) if path in store \
                else httpx.Response(404)
        return httpx.Response(204)

    p = HashiCorpVaultProvider("https://vault.corp", "s.tok")
    p.http = httpx.Client(base_url="https://vault.corp", headers={"X-Vault-Token": "s.tok"},
                          transport=httpx.MockTransport(handler))
    p.put(Credential("crm", "bearer", {"token": "abc"}))
    assert "/v1/secret/data/restforge/crm" in store
    got = CredentialManager([p]).get("crm")
    assert got.type == "bearer" and got.fields == {"token": "abc"}
    assert p.get("missing") is None
