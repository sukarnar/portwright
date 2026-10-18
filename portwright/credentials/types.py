"""Credential types: which fields each type has, which are secret, and how a
credential is turned into a connection (SQLAlchemy URL, HTTP auth, …)."""
from __future__ import annotations

import base64
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote_plus, urlsplit

import httpx


class CredentialError(Exception):
    """Credential missing, malformed, locked or unusable."""


@dataclass(frozen=True)
class CredentialType:
    name: str
    required: frozenset[str]
    optional: frozenset[str]
    secret: frozenset[str]
    description: str


def _t(name, required, optional, secret, description):
    return CredentialType(name, frozenset(required), frozenset(optional), frozenset(secret), description)


TYPES: dict[str, CredentialType] = {t.name: t for t in [
    _t("database", {"driver"},
       {"host", "port", "username", "password", "database", "service_name", "sid", "dsn",
        "schema", "options"},
       {"password"},
       "Database login. driver = SQLAlchemy dialect+driver, e.g. oracle+oracledb, "
       "postgresql+psycopg, mysql+pymysql, mssql+pyodbc, sqlite"),
    _t("basic", {"username", "password"}, set(), {"password"}, "HTTP Basic authentication"),
    _t("bearer", {"token"}, set(), {"token"}, "Static bearer token (Authorization: Bearer …)"),
    _t("api_key", {"key"}, {"header", "query_param"}, {"key"},
       "API key sent in a header (default X-API-Key) or as a query parameter"),
    _t("oauth2", {"token_url", "client_id", "client_secret"}, {"scope", "audience"},
       {"client_secret"}, "OAuth2 client-credentials flow; tokens are fetched and refreshed automatically"),
    _t("generic", set(), set(), set(), "Arbitrary key/value secrets, e.g. for ${CRED:name.field} or callables"),
]}


@dataclass
class Credential:
    name: str
    type: str
    fields: dict[str, Any]
    description: str = ""
    created: str = ""
    updated: str = ""
    expires: str | None = None
    secret_fields: list[str] = field(default_factory=list)   # used by 'generic'
    provider: str = ""

    def __repr__(self) -> str:  # never leak secrets through repr/logging
        return f"Credential(name={self.name!r}, type={self.type!r}, fields={sorted(self.fields)})"

    __str__ = __repr__

    @property
    def secrets(self) -> set[str]:
        t = TYPES[self.type]
        names = set(t.secret) | set(self.secret_fields)
        if self.type == "generic" and not self.secret_fields:
            names = set(self.fields)               # generic: everything is secret by default
        return {k for k in names if k in self.fields}

    def secret_values(self) -> list[str]:
        return [str(self.fields[k]) for k in self.secrets if self.fields.get(k)]

    def masked(self) -> dict[str, Any]:
        return {k: ("********" if k in self.secrets else v) for k, v in self.fields.items()}

    def is_expired(self) -> bool:
        return bool(self.expires) and datetime.fromisoformat(self.expires) < datetime.now(timezone.utc)

    def get(self, field_name: str) -> Any:
        if field_name not in self.fields:
            raise CredentialError(f"credential '{self.name}' has no field '{field_name}'")
        return self.fields[field_name]


def validate_credential(cred: Credential) -> None:
    if cred.type not in TYPES:
        raise CredentialError(f"unknown credential type '{cred.type}' (choose: {', '.join(TYPES)})")
    t = TYPES[cred.type]
    missing = t.required - set(cred.fields)
    if missing:
        raise CredentialError(f"credential '{cred.name}' ({cred.type}) is missing fields {sorted(missing)}")
    if cred.type != "generic":
        unknown = set(cred.fields) - t.required - t.optional
        if unknown:
            raise CredentialError(f"credential '{cred.name}': unknown fields {sorted(unknown)} "
                                  f"for type '{cred.type}'")
    if cred.type == "database" and not str(cred.fields["driver"]).startswith("sqlite"):
        if not (cred.fields.get("host") or cred.fields.get("dsn")):
            raise CredentialError(f"credential '{cred.name}': 'host' or 'dsn' is required")


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #
def build_sql_url(cred: Credential):
    """Build a SQLAlchemy URL. Special characters in passwords are escaped automatically."""
    from sqlalchemy.engine import URL

    if cred.type != "database":
        raise CredentialError(f"credential '{cred.name}' is type '{cred.type}', expected 'database'")
    f = cred.fields
    query: dict[str, str] = {}
    opts = f.get("options") or {}
    if isinstance(opts, str):                      # "k=v;k2=v2"
        opts = dict(p.split("=", 1) for p in opts.split(";") if "=" in p)
    query.update({str(k): str(v) for k, v in opts.items()})
    database = f.get("database")
    if f.get("service_name"):
        query["service_name"] = str(f["service_name"])     # Oracle service name
    elif f.get("sid"):
        database = f["sid"]                                  # Oracle SID
    host = f.get("dsn") or f.get("host")                     # Oracle TNS alias via dsn
    return URL.create(
        drivername=str(f["driver"]), username=f.get("username"), password=f.get("password"),
        host=host, port=int(f["port"]) if f.get("port") else None,
        database=database, query=query,
    )


class OAuth2ClientCredentials(httpx.Auth):
    """httpx auth flow: fetch a client-credentials token, cache it, refresh before
    expiry and once more on a 401."""

    requires_response_body = True

    def __init__(self, cred: Credential, allowed_hosts: list[str]):
        host = (urlsplit(cred.fields["token_url"]).hostname or "").lower()
        if host not in [h.lower() for h in allowed_hosts]:
            raise CredentialError(f"OAuth token host '{host}' is not in security.allowed_upstream_hosts")
        if urlsplit(cred.fields["token_url"]).scheme != "https":
            raise CredentialError("OAuth token_url must use https")
        self.cred = cred
        self._token: str | None = None
        self._expires_at = 0.0

    def _token_request(self) -> httpx.Request:
        f = self.cred.fields
        data = {"grant_type": "client_credentials"}
        if f.get("scope"):
            data["scope"] = f["scope"]
        if f.get("audience"):
            data["audience"] = f["audience"]
        # RFC 6749 §2.3.1: form-encode id/secret, then HTTP Basic
        basic = base64.b64encode(
            f"{quote_plus(f['client_id'])}:{quote_plus(f['client_secret'])}".encode()).decode()
        return httpx.Request("POST", f["token_url"], data=data,
                             headers={"Accept": "application/json", "Authorization": f"Basic {basic}"})

    def _store(self, response: httpx.Response) -> None:
        if response.status_code != 200:
            raise CredentialError(f"OAuth token request failed (HTTP {response.status_code})")
        body = response.json()
        self._token = body["access_token"]
        self._expires_at = time.monotonic() + int(body.get("expires_in", 300)) - 30  # refresh early

    def auth_flow(self, request: httpx.Request):
        if not self._token or time.monotonic() >= self._expires_at:
            self._store((yield self._token_request()))
        request.headers["Authorization"] = f"Bearer {self._token}"
        response = yield request
        if response.status_code == 401:                       # token revoked/rotated upstream
            self._store((yield self._token_request()))
            request.headers["Authorization"] = f"Bearer {self._token}"
            yield request


def build_http_auth(cred: Credential, allowed_hosts: list[str]) -> tuple[httpx.Auth | None, dict, dict]:
    """Return ``(auth, extra_headers, extra_query_params)`` for an HTTP client."""
    f = cred.fields
    if cred.type == "basic":
        return httpx.BasicAuth(f["username"], f["password"]), {}, {}
    if cred.type == "bearer":
        return None, {"Authorization": f"Bearer {f['token']}"}, {}
    if cred.type == "api_key":
        if f.get("query_param"):
            return None, {}, {f["query_param"]: f["key"]}
        return None, {f.get("header") or "X-API-Key": f["key"]}, {}
    if cred.type == "oauth2":
        return OAuth2ClientCredentials(cred, allowed_hosts), {}, {}
    raise CredentialError(f"credential '{cred.name}' of type '{cred.type}' cannot be used for HTTP auth")
