"""CredentialManager: one façade over a chain of providers, with caching,
expiry checks, least-privilege scoping and log redaction."""
from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .providers import (CredentialProvider, EncryptedFileVault, EnvProvider, HashiCorpVaultProvider,
                        KeyringProvider, _now)
from .types import Credential, CredentialError, validate_credential

from ..compat import vault_path as compat_vault

log = logging.getLogger("portwright.credentials")


class CredentialManager:
    def __init__(self, providers: list[CredentialProvider], cache_ttl: int = 300):
        if not providers:
            raise CredentialError("at least one credential provider is required")
        self.providers = providers
        self.cache_ttl = cache_ttl
        self._cache: dict[str, tuple[float, Credential]] = {}
        self._lock = threading.Lock()
        self._secret_values: set[str] = set()
        self.usage: dict[str, set[str]] = {}          # credential -> consumers (for `cred where`)

    # ------------------------------------------------------------ factory
    @classmethod
    def from_settings(cls, settings, project_root: str | Path = ".") -> "CredentialManager":
        from ..config import resolve_secrets          # ENV only – no recursion into CRED
        providers: list[CredentialProvider] = []
        for name in settings.providers:
            if name == "vault":
                providers.append(EncryptedFileVault(compat_vault(project_root, settings.vault_path),
                                                    settings.master_key_env))
            elif name == "env":
                providers.append(EnvProvider())
            elif name == "keyring":
                providers.append(KeyringProvider(settings.keyring_service))
            elif name == "hashicorp":
                h = settings.hashicorp
                providers.append(HashiCorpVaultProvider(resolve_secrets(h.url), resolve_secrets(h.token),
                                                        h.mount, h.path_prefix, h.namespace, h.verify_tls))
        return cls(providers, settings.cache_ttl_seconds)

    # -------------------------------------------------------------- reads
    def get(self, name: str, consumer: str | None = None) -> Credential:
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(name)
            if hit and now - hit[0] < self.cache_ttl:
                cred = hit[1]
            else:
                cred = None
                for p in self.providers:
                    cred = p.get(name)
                    if cred:
                        break
                if cred is None:
                    raise CredentialError(f"credential '{name}' not found in providers "
                                          f"{[p.name for p in self.providers]}")
                validate_credential(cred)
                self._cache[name] = (now, cred)
                self._secret_values.update(v for v in cred.secret_values() if len(v) >= 4)
        if cred.is_expired():
            raise CredentialError(f"credential '{name}' expired on {cred.expires}; rotate it")
        if consumer:
            self.usage.setdefault(name, set()).add(consumer)
        return cred

    def field(self, name: str, field_name: str) -> Any:
        return self.get(name).get(field_name)

    def list(self) -> list[Credential]:
        seen: dict[str, Credential] = {}
        for p in self.providers:
            for c in p.list():
                seen.setdefault(c.name, c)              # first provider wins (same as get)
        return sorted(seen.values(), key=lambda c: c.name)

    def scoped(self, allowed: list[str], consumer: str) -> "ScopedCredentials":
        return ScopedCredentials(self, set(allowed), consumer)

    def invalidate(self, name: str | None = None) -> None:
        with self._lock:
            if name:
                self._cache.pop(name, None)
            else:
                self._cache.clear()

    # ------------------------------------------------------------- writes
    def _writable(self, provider: str | None) -> CredentialProvider:
        for p in self.providers:
            if p.writable and (provider is None or p.name == provider):
                return p
        raise CredentialError(f"no writable credential provider{f' named {provider!r}' if provider else ''}")

    def put(self, cred: Credential, provider: str | None = None) -> str:
        validate_credential(cred)
        target = self._writable(provider)
        existing = target.get(cred.name)
        cred.created = existing.created if existing else (cred.created or _now())
        cred.updated = _now()
        target.put(cred)
        self.invalidate(cred.name)
        return target.name

    def delete(self, name: str, provider: str | None = None) -> bool:
        self.invalidate(name)
        return self._writable(provider).delete(name)

    # ---------------------------------------------------------- redaction
    def redact(self, text: str) -> str:
        for secret in sorted(self._secret_values, key=len, reverse=True):
            if secret in text:
                text = text.replace(secret, "***")
        return text


class ScopedCredentials:
    """What a callable endpoint sees as ``ctx.credentials``: only the credentials
    its source explicitly lists (least privilege)."""

    def __init__(self, manager: CredentialManager, allowed: set[str], consumer: str):
        self._m, self._allowed, self._consumer = manager, allowed, consumer

    def get(self, name: str) -> Credential:
        if name not in self._allowed:
            raise CredentialError(f"'{self._consumer}' is not allowed to use credential '{name}'")
        return self._m.get(name, consumer=self._consumer)

    def field(self, name: str, field_name: str) -> Any:
        return self.get(name).get(field_name)

    def __repr__(self) -> str:
        return f"ScopedCredentials(allowed={sorted(self._allowed)})"


class RedactingFilter(logging.Filter):
    """Scrubs any loaded secret value from log messages and tracebacks."""

    _URL_PW = re.compile(r"(://[^:/@\s]+:)([^@\s]+)(@)")

    def __init__(self, manager: CredentialManager):
        super().__init__()
        self.manager = manager

    def _clean(self, text: str) -> str:
        return self._URL_PW.sub(r"\1***\3", self.manager.redact(text))

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = self._clean(record.getMessage())
            record.args = ()
            if record.exc_info:
                record.exc_text = self._clean(logging.Formatter().formatException(record.exc_info))
                record.exc_info = None
        except Exception:  # pragma: no cover - never break logging
            pass
        return True


def install_redaction(manager: CredentialManager) -> None:
    flt = RedactingFilter(manager)
    for name in ("portwright", "portwright.audit", "portwright.credentials", "uvicorn", "uvicorn.error",
                 "uvicorn.access", "sqlalchemy", "httpx"):
        lg = logging.getLogger(name)
        if not any(isinstance(f, RedactingFilter) for f in lg.filters):
            lg.addFilter(flt)
        for h in lg.handlers:
            if not any(isinstance(f, RedactingFilter) for f in h.filters):
                h.addFilter(flt)
    root = logging.getLogger()
    for h in root.handlers:
        if not any(isinstance(f, RedactingFilter) for f in h.filters):
            h.addFilter(flt)


def expiring_soon(creds: list[Credential], days: int = 14) -> list[Credential]:
    now = datetime.now(timezone.utc)
    return [c for c in creds if c.expires and (datetime.fromisoformat(c.expires) - now).days < days]
