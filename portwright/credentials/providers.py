"""Credential providers (Strategy pattern). Each provider can look a credential up
by name; writable providers can also store, rotate and delete them.

* ``vault``     – local file, every credential encrypted with AES-256-GCM (default)
* ``env``       – environment variables ``RF_CRED_<NAME>_<FIELD>`` (containers/CI)
* ``keyring``   – OS store: Windows Credential Manager, macOS Keychain, Secret Service
* ``hashicorp`` – HashiCorp Vault KV v2 (enterprise secret store)
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import tempfile
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .types import Credential, CredentialError

_VAULT_VERSION = 1
_KEY_CHECK = b"restforge-key-check"        # stable storage id (pre-rename vaults)


def _now() -> str:
    # microsecond precision: 'updated' doubles as the rotation version stamp
    return datetime.now(timezone.utc).isoformat()


def _to_record(c: Credential) -> dict[str, Any]:
    return {"type": c.type, "description": c.description, "created": c.created, "updated": c.updated,
            "expires": c.expires, "secret_fields": c.secret_fields}


def _from_record(name: str, meta: dict[str, Any], fields: dict[str, Any], provider: str) -> Credential:
    return Credential(name=name, type=meta.get("type", "generic"), fields=fields,
                      description=meta.get("description", ""), created=meta.get("created", ""),
                      updated=meta.get("updated", ""), expires=meta.get("expires"),
                      secret_fields=meta.get("secret_fields") or [], provider=provider)


class CredentialProvider(ABC):
    name: str = ""
    writable: bool = False

    @abstractmethod
    def get(self, name: str) -> Credential | None: ...

    def list(self) -> list[Credential]:
        """Credentials with metadata only (fields may be empty)."""
        return []

    def put(self, cred: Credential) -> None:
        raise CredentialError(f"provider '{self.name}' is read-only")

    def delete(self, name: str) -> bool:
        raise CredentialError(f"provider '{self.name}' is read-only")


# --------------------------------------------------------------------------- #
# Encrypted file vault
# --------------------------------------------------------------------------- #
def generate_master_key() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()


class EncryptedFileVault(CredentialProvider):
    """JSON file; each credential's fields are an AES-256-GCM blob with a unique
    nonce and the credential name+type bound as associated data (so encrypted
    blobs cannot be swapped between entries). Metadata stays readable for listing.

    The master key comes from an env var. It is either a 32-byte urlsafe-base64
    key (``portwright cred init``) or a passphrase stretched with scrypt.
    """

    name = "vault"
    writable = True

    def __init__(self, path: str | Path, master_key_env: str = "PORTWRIGHT_MASTER_KEY"):
        self.path = Path(path)
        self.master_key_env = master_key_env
        self._aes = None

    # ------------------------------------------------------------- crypto
    def _load_file(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": _VAULT_VERSION, "salt": base64.b64encode(secrets.token_bytes(16)).decode(),
                    "key_check": None, "credentials": {}}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _cipher(self, data: dict[str, Any]):
        if self._aes is not None:
            return self._aes
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        from ..compat import getenv
        secret = getenv(self.master_key_env)
        if not secret:
            raise CredentialError(f"vault is locked: set {self.master_key_env} (run 'portwright cred init')")
        try:
            key = base64.urlsafe_b64decode(secret.encode())
            if len(key) != 32:
                raise ValueError
        except Exception:                           # passphrase -> scrypt-derived key
            if len(secret) < 16:
                raise CredentialError("master passphrase must be at least 16 characters") from None
            key = hashlib.scrypt(secret.encode(), salt=base64.b64decode(data["salt"]),
                                 n=2**15, r=8, p=1, dklen=32, maxmem=64 * 1024 * 1024)
        aes = AESGCM(key)
        if data.get("key_check"):                   # verify the key before using it
            try:
                self._decrypt(aes, data["key_check"], b"key-check")
            except Exception:
                raise CredentialError("wrong master key for this vault") from None
        else:
            data["key_check"] = self._encrypt(aes, _KEY_CHECK, b"key-check")
        self._aes = aes
        return aes

    @staticmethod
    def _encrypt(aes, plaintext: bytes, aad: bytes) -> dict[str, str]:
        nonce = secrets.token_bytes(12)
        return {"nonce": base64.b64encode(nonce).decode(),
                "ct": base64.b64encode(aes.encrypt(nonce, plaintext, aad)).decode()}

    @staticmethod
    def _decrypt(aes, blob: dict[str, str], aad: bytes) -> bytes:
        return aes.decrypt(base64.b64decode(blob["nonce"]), base64.b64decode(blob["ct"]), aad)

    @staticmethod
    def _aad(name: str, ctype: str) -> bytes:
        return f"restforge:{name}:{ctype}".encode()        # stable: binds blobs of existing vaults

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".vault-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            os.replace(tmp, self.path)             # atomic swap: no half-written vault
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    # ---------------------------------------------------------------- api
    def get(self, name: str) -> Credential | None:
        data = self._load_file()
        entry = data["credentials"].get(name)
        if entry is None:
            return None
        aes = self._cipher(data)
        try:
            fields = json.loads(self._decrypt(aes, entry["blob"], self._aad(name, entry["type"])))
        except Exception:
            raise CredentialError(f"credential '{name}' failed integrity check (tampered or wrong key)") from None
        return _from_record(name, entry, fields, self.name)

    def list(self) -> list[Credential]:
        data = self._load_file()
        return [_from_record(n, e, {k: None for k in e.get("field_names", [])}, self.name)
                for n, e in sorted(data["credentials"].items())]

    def put(self, cred: Credential) -> None:
        data = self._load_file()
        aes = self._cipher(data)
        record = _to_record(cred)
        record["field_names"] = sorted(cred.fields)
        record["blob"] = self._encrypt(aes, json.dumps(cred.fields).encode(), self._aad(cred.name, cred.type))
        data["credentials"][cred.name] = record
        self._write(data)

    def delete(self, name: str) -> bool:
        data = self._load_file()
        if data["credentials"].pop(name, None) is None:
            return False
        self._write(data)
        return True

    def rekey(self, new_secret: str) -> int:
        """Re-encrypt every credential under a new master key/passphrase."""
        creds = [self.get(c.name) for c in self.list()]
        data = self._load_file()
        data["salt"] = base64.b64encode(secrets.token_bytes(16)).decode()
        data["key_check"] = None
        data["credentials"] = {}
        os.environ[self.master_key_env] = new_secret
        self._aes = None
        self._cipher(data)
        self._write(data)
        for c in creds:
            self.put(c)
        return len(creds)


# --------------------------------------------------------------------------- #
# Environment variables
# --------------------------------------------------------------------------- #
class EnvProvider(CredentialProvider):
    """``RF_CRED_HR_DB_TYPE=database``, ``RF_CRED_HR_DB_PASSWORD=…`` → credential ``hr_db``
    (also matches ``hr-db``). Good for Docker/Kubernetes secrets and CI."""

    name = "env"
    prefix = "RF_CRED_"            # also PW_CRED_ (see get/list)
    prefixes = ("PW_CRED_", "RF_CRED_")

    @staticmethod
    def _key(name: str) -> str:
        return name.upper().replace("-", "_")

    def get(self, name: str) -> Credential | None:
        for prefix in self.prefixes:
            base = f"{prefix}{self._key(name)}_"
            ctype = os.environ.get(base + "TYPE")
            if ctype:
                break
        else:
            return None
        fields = {k[len(base):].lower(): v for k, v in os.environ.items()
                  if k.startswith(base) and k != base + "TYPE"}
        return Credential(name=name, type=ctype.lower(), fields=fields, provider=self.name)

    def list(self) -> list[Credential]:
        out = []
        seen = set()
        for k in list(os.environ):
            for prefix in self.prefixes:
                if k.startswith(prefix) and k.endswith("_TYPE"):
                    name = k[len(prefix):-5].lower()
                    c = None if name in seen else self.get(name)
                    if c:
                        seen.add(name)
                        out.append(c)
        return out


# --------------------------------------------------------------------------- #
# OS keyring (Windows Credential Manager, macOS Keychain, Linux Secret Service)
# --------------------------------------------------------------------------- #
class KeyringProvider(CredentialProvider):
    name = "keyring"
    writable = True
    _INDEX = "__restforge_index__"        # stable storage key (existing keyrings)

    def __init__(self, service: str = "restforge"):
        try:
            import keyring  # noqa: F401
        except ImportError:
            raise CredentialError("keyring provider needs: pip install keyring") from None
        import keyring as _kr
        self.kr = _kr
        self.service = service

    def _index(self) -> list[str]:
        return json.loads(self.kr.get_password(self.service, self._INDEX) or "[]")

    def get(self, name: str) -> Credential | None:
        raw = self.kr.get_password(self.service, name)
        if raw is None:
            return None
        rec = json.loads(raw)
        return _from_record(name, rec, rec.pop("fields"), self.name)

    def list(self) -> list[Credential]:
        return [c for c in (self.get(n) for n in self._index()) if c]

    def put(self, cred: Credential) -> None:
        rec = _to_record(cred) | {"fields": cred.fields}
        self.kr.set_password(self.service, cred.name, json.dumps(rec))
        idx = self._index()
        if cred.name not in idx:
            self.kr.set_password(self.service, self._INDEX, json.dumps(sorted(idx + [cred.name])))

    def delete(self, name: str) -> bool:
        if self.kr.get_password(self.service, name) is None:
            return False
        self.kr.delete_password(self.service, name)
        self.kr.set_password(self.service, self._INDEX, json.dumps([n for n in self._index() if n != name]))
        return True


# --------------------------------------------------------------------------- #
# HashiCorp Vault (KV v2)
# --------------------------------------------------------------------------- #
class HashiCorpVaultProvider(CredentialProvider):
    name = "hashicorp"
    writable = True

    def __init__(self, url: str, token: str, mount: str = "secret", path_prefix: str = "restforge",
                 namespace: str | None = None, verify_tls: bool = True):
        import httpx
        headers = {"X-Vault-Token": token}
        if namespace:
            headers["X-Vault-Namespace"] = namespace
        self.http = httpx.Client(base_url=url.rstrip("/"), headers=headers, timeout=10, verify=verify_tls)
        self.mount, self.prefix = mount.strip("/"), path_prefix.strip("/")

    def _path(self, kind: str, name: str = "") -> str:
        return f"/v1/{self.mount}/{kind}/{self.prefix}/{name}".rstrip("/")

    def get(self, name: str) -> Credential | None:
        r = self.http.get(self._path("data", name))
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise CredentialError(f"HashiCorp Vault returned HTTP {r.status_code} for '{name}'")
        payload = r.json()["data"]["data"]
        meta = json.loads(payload.pop("_restforge_meta", "{}"))
        return _from_record(name, meta | {"type": meta.get("type", payload.pop("type", "generic"))},
                            payload, self.name)

    def list(self) -> list[Credential]:
        r = self.http.request("LIST", self._path("metadata"))
        if r.status_code == 404:
            return []
        r.raise_for_status()
        return [c for c in (self.get(k) for k in r.json()["data"]["keys"] if not k.endswith("/")) if c]

    def put(self, cred: Credential) -> None:
        body = {"data": cred.fields | {"_restforge_meta": json.dumps(_to_record(cred))}}
        r = self.http.post(self._path("data", cred.name), json=body)
        if r.status_code not in (200, 204):
            raise CredentialError(f"HashiCorp Vault write failed (HTTP {r.status_code})")

    def delete(self, name: str) -> bool:
        r = self.http.delete(self._path("metadata", name))
        return r.status_code in (200, 204)
