"""Backward compatibility with projects created under the old name (restforge).

Existing projects keep working unchanged after the rename:
* ``restforge.yaml`` is used when there is no ``portwright.yaml`` in the folder;
* ``RESTFORGE_*`` environment variables (e.g. in an existing .env) are read when the
  matching ``PORTWRIGHT_*`` one is not set;
* the old vault location ``.restforge/credentials.vault`` is used when it exists and the new one does not;
* API keys starting ``rf_`` stay valid (new keys start ``pw_``);
* ``import restforge`` and the ``restforge`` command still work.
"""
from __future__ import annotations

import os
from pathlib import Path

CONFIG_NAME = "portwright.yaml"
LEGACY_CONFIG_NAME = "restforge.yaml"
ENV_PREFIX, LEGACY_ENV_PREFIX = "PORTWRIGHT_", "RESTFORGE_"
VAULT_PATH, LEGACY_VAULT_PATH = ".portwright/credentials.vault", ".restforge/credentials.vault"


def legacy_env_name(name: str) -> str | None:
    return LEGACY_ENV_PREFIX + name[len(ENV_PREFIX):] if name.startswith(ENV_PREFIX) else None


def getenv(name: str, default: str | None = None) -> str | None:
    """os.environ.get with a fallback from PORTWRIGHT_X to the old RESTFORGE_X."""
    if name in os.environ:
        return os.environ[name]
    old = legacy_env_name(name)
    if old and old in os.environ:
        return os.environ[old]
    return default


def has_env(name: str) -> bool:
    return getenv(name) is not None


def config_in(directory: str | Path) -> Path:
    """The project file in a folder: portwright.yaml, else an existing restforge.yaml."""
    d = Path(directory)
    new, old = d / CONFIG_NAME, d / LEGACY_CONFIG_NAME
    return old if not new.exists() and old.exists() else new


def resolve_config(path: str | Path) -> str:
    """Map the default config name to an existing legacy file when needed."""
    p = Path(path)
    if p.name == CONFIG_NAME and not p.exists():
        old = p.with_name(LEGACY_CONFIG_NAME)
        if old.exists():
            return str(old)
    return str(path)


def vault_path(project_root: str | Path, configured: str) -> Path:
    root = Path(project_root)
    p = root / configured
    if configured == VAULT_PATH and not p.exists() and (root / LEGACY_VAULT_PATH).exists():
        return root / LEGACY_VAULT_PATH
    return p
