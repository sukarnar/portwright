"""Credential management: typed, encrypted, pluggable connection secrets."""
from .manager import CredentialManager, ScopedCredentials, install_redaction
from .providers import (CredentialProvider, EncryptedFileVault, EnvProvider, HashiCorpVaultProvider,
                        KeyringProvider, generate_master_key)
from .types import (TYPES, Credential, CredentialError, build_http_auth, build_sql_url,
                    validate_credential)

__all__ = ["CredentialManager", "ScopedCredentials", "install_redaction", "CredentialProvider",
           "EncryptedFileVault", "EnvProvider", "HashiCorpVaultProvider", "KeyringProvider",
           "generate_master_key", "TYPES", "Credential", "CredentialError", "build_http_auth",
           "build_sql_url", "validate_credential"]
