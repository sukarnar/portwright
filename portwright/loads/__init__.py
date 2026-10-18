"""Load jobs: read rows (file source or endpoint), map source -> target columns, write to a SQL table."""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from ..config import LoadSpec, ProjectConfig
from .mapping import RowError, RowMapper, auto_map, normalise
from .runner import LoadError, LoadReport, LoadRunner, column_type_name

__all__ = ["LoadError", "LoadReport", "LoadRunner", "RowError", "RowMapper", "auto_map",
           "normalise", "column_type_name", "runtime_for"]


def sources_needed(config: ProjectConfig, spec: LoadSpec) -> set[str]:
    names = {spec.to.source}
    if spec.from_.source:
        names.add(spec.from_.source)
    else:
        names.add(config.endpoint(spec.from_.endpoint).source)
    return names


@asynccontextmanager
async def runtime_for(config: ProjectConfig, project_root: str | Path, names: set[str]):
    """Start only the given sources (with credentials and log redaction) for CLI use."""
    from .. import sources as _register  # noqa: F401
    from ..credentials import CredentialManager, install_redaction
    from ..security.audit import AuditLogger
    from ..sources.base import create_source

    sec = config.security
    creds = CredentialManager.from_settings(sec.credentials, project_root)
    install_redaction(creds)
    started = {}
    try:
        for n in names:
            src = create_source(n, config.sources[n], sec, str(project_root), creds)
            await src.startup()
            started[n] = src
        audit = AuditLogger(str(Path(project_root) / sec.audit_log) if sec.audit_log else None)
        yield LoadRunner(config, started, project_root, audit)
    finally:
        for src in started.values():
            await src.shutdown()
