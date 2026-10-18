"""Credential hashing. Plaintext secrets are shown once and never persisted."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

_SCRYPT = {"n": 2**14, "r": 8, "p": 1, "dklen": 32}


# ------------------------------------------------------------------ API keys
def generate_api_key() -> tuple[str, str]:
    """Return ``(key_id, plaintext_key)``. Format: ``pw_<id>_<secret>`` (older keys: ``rf_``)."""
    key_id = secrets.token_hex(4)
    secret = secrets.token_urlsafe(32)          # 256 bits of entropy
    return key_id, f"pw_{key_id}_{secret}"


def hash_api_key(plaintext: str) -> str:
    # High-entropy random keys => a fast hash is appropriate (no brute force risk).
    return hashlib.sha256(plaintext.encode()).hexdigest()


def parse_key_id(plaintext: str) -> str | None:
    parts = plaintext.split("_", 2)
    if len(parts) == 3 and parts[0] in ("pw", "rf") and len(parts[1]) == 8:
        return parts[1]
    return None


def verify_api_key(plaintext: str, stored_hash: str) -> bool:
    return hmac.compare_digest(hash_api_key(plaintext), stored_hash)


# ----------------------------------------------------------------- passwords
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(dk).decode()


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, salt_b64, dk_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        dk = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt_b64), **_SCRYPT)
        return hmac.compare_digest(dk, base64.b64decode(dk_b64))
    except Exception:
        return False


# Pre-computed dummy hash so unknown usernames cost the same time (no user enumeration).
DUMMY_PASSWORD_HASH = hash_password(secrets.token_hex(8))
