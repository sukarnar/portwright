"""Authentication (Strategy + Chain of Responsibility) and scope-based authorization.

Each :class:`AuthStrategy` inspects the request and returns a ``Principal`` or
``None``. The :class:`Authenticator` tries enabled strategies in order. Adding
OAuth2/OIDC, mTLS or LDAP means adding one strategy class.
"""
from __future__ import annotations

import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone

import jwt
from fastapi import HTTPException, Request, status

from ..config import ProjectConfig, resolve_secrets
from .hashing import DUMMY_PASSWORD_HASH, parse_key_id, verify_api_key, verify_password

ADMIN_SCOPE = "*"


@dataclass(frozen=True)
class Principal:
    subject: str
    kind: str                      # "api_key" | "jwt" | "anonymous"
    scopes: frozenset[str] = field(default_factory=frozenset)

    def has_scopes(self, required: list[str]) -> bool:
        return ADMIN_SCOPE in self.scopes or set(required).issubset(self.scopes)


ANONYMOUS = Principal("anonymous", "anonymous")


def _unauthorized(detail: str = "Not authenticated") -> HTTPException:
    return HTTPException(status.HTTP_401_UNAUTHORIZED, detail,
                         headers={"WWW-Authenticate": 'Bearer realm="portwright"'})


class AuthStrategy(ABC):
    name: str = ""

    @abstractmethod
    def authenticate(self, request: Request) -> Principal | None: ...


class ApiKeyStrategy(AuthStrategy):
    name = "api_key"

    def __init__(self, config: ProjectConfig):
        self.header = config.security.api_key_header
        self.keys = {k.id: k for k in config.security.api_keys}

    def authenticate(self, request: Request) -> Principal | None:
        presented = request.headers.get(self.header)
        if not presented:
            return None
        key_id = parse_key_id(presented)
        record = self.keys.get(key_id or "")
        if record is None or not verify_api_key(presented, record.hash):
            raise _unauthorized("Invalid API key")
        if record.disabled:
            raise _unauthorized("API key revoked")
        if record.expires and datetime.fromisoformat(record.expires) < datetime.now(timezone.utc):
            raise _unauthorized("API key expired")
        return Principal(f"key:{record.id}:{record.name}", "api_key", frozenset(record.scopes))


class JwtStrategy(AuthStrategy):
    name = "jwt"

    def __init__(self, config: ProjectConfig, credentials=None):
        self.settings = config.security.jwt
        self.secret = resolve_secrets(self.settings.secret, credentials)
        if len(self.secret) < 32:
            raise ValueError("JWT secret must be at least 32 characters")

    def authenticate(self, request: Request) -> Principal | None:
        header = request.headers.get("Authorization", "")
        if not header.lower().startswith("bearer "):
            return None
        token = header[7:].strip()
        try:
            claims = jwt.decode(
                token, self.secret, algorithms=[self.settings.algorithm],  # pinned alg: no 'none'
                audience=self.settings.audience, issuer=self.settings.issuer,
                options={"require": ["exp", "iat", "sub", "aud", "iss"]},
            )
        except jwt.ExpiredSignatureError:
            raise _unauthorized("Token expired") from None
        except jwt.PyJWTError:
            raise _unauthorized("Invalid token") from None
        scopes = claims.get("scope", "").split()
        return Principal(f"user:{claims['sub']}", "jwt", frozenset(scopes))

    def issue(self, subject: str, scopes: list[str]) -> dict:
        now = int(time.time())
        ttl = self.settings.ttl_minutes * 60
        claims = {"sub": subject, "scope": " ".join(scopes), "iat": now, "nbf": now,
                  "exp": now + ttl, "iss": self.settings.issuer,
                  "aud": self.settings.audience, "jti": uuid.uuid4().hex}
        token = jwt.encode(claims, self.secret, algorithm=self.settings.algorithm)
        return {"access_token": token, "token_type": "bearer", "expires_in": ttl}


class Authenticator:
    """Runs the strategy chain; enforces auth + scopes for an endpoint."""

    def __init__(self, config: ProjectConfig, credentials=None):
        self.config = config
        self.strategies: list[AuthStrategy] = []
        self.jwt: JwtStrategy | None = None
        for method in config.security.auth_methods:
            if method == "api_key":
                self.strategies.append(ApiKeyStrategy(config))
            elif method == "jwt":
                self.jwt = JwtStrategy(config, credentials)
                self.strategies.append(self.jwt)

    def identify(self, request: Request) -> Principal | None:
        for strategy in self.strategies:
            principal = strategy.authenticate(request)
            if principal:
                return principal
        return None

    def require(self, request: Request, scopes: list[str], public: bool) -> Principal:
        principal = self.identify(request)
        if principal is None:
            if public:
                return ANONYMOUS
            raise _unauthorized()
        if not public and not principal.has_scopes(scopes):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Insufficient scope")
        return principal

    def login(self, username: str, password: str) -> dict:
        if self.jwt is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "JWT auth not enabled")
        user = next((u for u in self.config.security.users if u.username == username), None)
        ok = verify_password(password, user.password_hash if user else DUMMY_PASSWORD_HASH)
        if not user or not ok or user.disabled:
            raise _unauthorized("Invalid credentials")
        return self.jwt.issue(user.username, user.scopes)
