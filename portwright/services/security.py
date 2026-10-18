"""Credentials (outbound secrets) and access (API keys, users)."""
from __future__ import annotations

import os

from ..compat import getenv
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ..config import ApiKeyRecord, UserRecord, credential_refs
from ..credentials import TYPES, Credential, CredentialError, generate_master_key
from ..security.hashing import generate_api_key, hash_api_key, hash_password
from .project import Project, ServiceError, set_env_line

_NAME = re.compile(r"^[a-zA-Z][a-zA-Z0-9_\-]{0,63}$")


def _expiry(days: int | None) -> str | None:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat(timespec="seconds") if days else None


def expiry_state(expires: str | None) -> tuple[str, int | None]:
    """('ok'|'soon'|'expired'|'never', days_left)."""
    if not expires:
        return "never", None
    days = (datetime.fromisoformat(expires) - datetime.now(timezone.utc)).days
    return ("expired" if days < 0 else "soon" if days < 14 else "ok"), days


# =================================================================== credentials
class CredentialService:
    def __init__(self, project: Project):
        self.p = project

    @staticmethod
    def types() -> dict:
        return TYPES

    def vault_status(self) -> dict:
        env_name = self.p.config.security.credentials.master_key_env
        return {"key_env": env_name, "key_present": bool(getenv(env_name)),
                "providers": list(self.p.config.security.credentials.providers)}

    def usage(self) -> dict[str, list[str]]:
        cfg = self.p.config
        users: dict[str, list[str]] = {}
        for sname, s in cfg.sources.items():
            names = ({s.credential} if s.credential else set()) | set(s.credentials) | \
                credential_refs(s.url) | credential_refs(s.headers)
            for n in names:
                users.setdefault(n, []).append(f"source:{sname}")
        for n in credential_refs(cfg.security.jwt.secret):
            users.setdefault(n, []).append("jwt")
        return users

    def list(self) -> list[dict]:
        try:
            creds = self.p.credentials().list()
        except CredentialError as exc:
            raise ServiceError(str(exc)) from None
        users = self.usage()
        out = []
        for c in creds:
            state, days = expiry_state(c.expires)
            out.append({"name": c.name, "type": c.type, "provider": c.provider, "fields": sorted(c.fields),
                        "expires": c.expires, "expiry_state": state, "days_left": days,
                        "used_by": users.get(c.name, []), "description": c.description})
        return out

    def get(self, name: str) -> Credential:
        try:
            return self.p.credentials().get(name)
        except CredentialError as exc:
            raise ServiceError(str(exc)) from None

    def masked(self, name: str) -> dict:
        c = self.get(name)
        return {"name": c.name, "type": c.type, "provider": c.provider, "description": c.description,
                "created": c.created, "updated": c.updated, "expires": c.expires, "fields": c.masked()}

    def add(self, name: str, type: str, fields: dict, *, secret_fields: list[str] | None = None,
            expires_days: int | None = None, provider: str | None = None, description: str = "") -> str:
        if type not in TYPES:
            raise ServiceError(f"type must be one of {', '.join(TYPES)}")
        if not _NAME.match(name):
            raise ServiceError("name: letters, digits, '_' or '-' (max 64), starting with a letter")
        mgr = self.p.credentials()
        try:
            if any(c.name == name for c in mgr.list()):
                raise ServiceError(f"credential '{name}' exists – rotate it instead")
            clean = {k: v for k, v in fields.items() if v not in (None, "")}
            where = mgr.put(Credential(name=name, type=type, fields=clean, description=description,
                                       expires=_expiry(expires_days), secret_fields=secret_fields or []),
                            provider)
            self.p.audit("credential_added", credential=name, type=type, provider=where)
            return where
        except CredentialError as exc:
            raise ServiceError(str(exc)) from None

    def rotate(self, name: str, changes: dict, *, expires_days: int | None = None) -> None:
        """Apply new field values (secrets and/or settings); blank values are ignored."""
        mgr = self.p.credentials()
        try:
            c = mgr.get(name)
            c.fields.update({k: v for k, v in changes.items() if v not in (None, "")})
            if expires_days:
                c.expires = _expiry(expires_days)
            mgr.put(c, c.provider if c.provider != "env" else None)
            self.p.audit("credential_rotated", credential=name, fields=sorted(changes))
        except CredentialError as exc:
            raise ServiceError(str(exc)) from None

    def remove(self, name: str, force: bool = False) -> None:
        users = self.usage().get(name)
        if users and not force:
            raise ServiceError(f"credential '{name}' is used by {users}; detach it first")
        try:
            if not self.p.credentials().delete(name):
                raise ServiceError(f"no credential '{name}'")
            self.p.audit("credential_removed", credential=name)
        except CredentialError as exc:
            raise ServiceError(str(exc)) from None

    def test(self, name: str, url: str | None = None) -> tuple[bool, str]:
        import httpx
        from sqlalchemy import create_engine, literal, select
        from ..credentials import build_http_auth, build_sql_url
        mgr = self.p.credentials()
        try:
            c = mgr.get(name)
            if c.type == "database":
                eng = create_engine(build_sql_url(c))
                try:
                    with eng.connect() as conn:
                        conn.execute(select(literal(1)))
                finally:
                    eng.dispose()
                return True, f"Connected to database with '{name}'"
            if c.type == "generic":
                return True, f"'{name}' decrypted OK ({len(c.fields)} fields)"
            auth, headers, params = build_http_auth(c, self.p.config.security.allowed_upstream_hosts)
            if c.type == "oauth2" and not url:
                with httpx.Client(timeout=10) as client:
                    flow = auth.auth_flow(httpx.Request("GET", "https://placeholder.invalid/"))
                    auth._store(client.send(next(flow)))
                return True, f"OAuth token obtained for '{name}'"
            if url:
                with httpx.Client(timeout=10, auth=auth, headers=headers, follow_redirects=False) as client:
                    r = client.get(url, params=params)
                return r.status_code < 400, f"GET {url} -> HTTP {r.status_code}"
            return True, f"'{name}' is valid; give a URL to test it against an endpoint"
        except Exception as exc:
            return False, mgr.redact(f"{type(exc).__name__}: {exc}")

    def init_vault(self) -> str:
        env_name = self.p.config.security.credentials.master_key_env
        if getenv(env_name):
            raise ServiceError(f"{env_name} is already set – use rekey to change it")
        key = generate_master_key()
        set_env_line(self.p.env_file, env_name, key)
        os.environ[env_name] = key
        self.p.reset_credentials()
        return env_name

    def rekey(self, new_secret: str | None = None) -> int:
        vault = next((p for p in self.p.credentials().providers if p.name == "vault"), None)
        if vault is None:
            raise ServiceError("the 'vault' provider is not enabled")
        new = new_secret or generate_master_key()
        try:
            n = vault.rekey(new)
        except CredentialError as exc:
            raise ServiceError(str(exc)) from None
        if not new_secret:
            set_env_line(self.p.env_file, self.p.config.security.credentials.master_key_env, new)
        self.p.reset_credentials()
        return n


# ======================================================================== access
@dataclass
class NewKey:
    id: str
    name: str
    plaintext: str
    scopes: list[str]
    expires: str | None
    header: str


class AccessService:
    def __init__(self, project: Project):
        self.p = project

    @property
    def sec(self):
        return self.p.config.security

    def known_scopes(self) -> list[str]:
        scopes = {s for e in self.p.config.endpoints for s in e.scopes}
        if self.p.config.loads:
            scopes.add("load:run")
        return sorted(scopes) + ["*"]

    def keys(self) -> list[dict]:
        out = []
        for k in self.sec.api_keys:
            state, days = expiry_state(k.expires)
            out.append({"id": k.id, "name": k.name, "scopes": k.scopes, "expires": k.expires,
                        "status": "revoked" if k.disabled else ("expired" if state == "expired" else "active"),
                        "expiry_state": state, "days_left": days, "created": k.created})
        return out

    def create_key(self, name: str, scopes: list[str], expires_days: int | None = 90) -> NewKey:
        if not name.strip():
            raise ServiceError("give the key a name (e.g. the calling application)")
        key_id, plaintext = generate_api_key()
        now = datetime.now(timezone.utc)
        rec = ApiKeyRecord(id=key_id, name=name.strip(), hash=hash_api_key(plaintext), scopes=scopes,
                           created=now.isoformat(), expires=_expiry(expires_days))
        self.sec.api_keys.append(rec)
        self.p.save()
        self.p.audit("api_key_created", key_id=key_id, name=rec.name, scopes=scopes)
        return NewKey(key_id, rec.name, plaintext, scopes, rec.expires, self.sec.api_key_header)

    def revoke_key(self, key_id: str) -> None:
        for k in self.sec.api_keys:
            if k.id == key_id:
                k.disabled = True
                self.p.save()
                self.p.audit("api_key_revoked", key_id=key_id)
                return
        raise ServiceError(f"no key '{key_id}'")

    def users(self) -> list[dict]:
        return [{"username": u.username, "scopes": u.scopes, "disabled": u.disabled} for u in self.sec.users]

    def add_user(self, username: str, password: str, scopes: list[str]) -> None:
        if len(password) < 12:
            raise ServiceError("password must be at least 12 characters")
        if any(u.username == username for u in self.sec.users):
            raise ServiceError(f"user '{username}' exists")
        try:
            self.sec.users.append(UserRecord(username=username, password_hash=hash_password(password),
                                             scopes=scopes))
        except Exception as exc:
            raise ServiceError(str(exc)) from None
        self.p.save()

    def remove_user(self, username: str) -> None:
        before = len(self.sec.users)
        self.sec.users = [u for u in self.sec.users if u.username != username]
        if len(self.sec.users) == before:
            raise ServiceError(f"no user '{username}'")
        self.p.save()
