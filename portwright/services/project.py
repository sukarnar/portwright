"""Project: one open portwright project (config file + .env + credential vault).

Both front-ends (CLI and Studio) work through this object, so saving is always
validated, atomic and backed up, and edits made elsewhere are detected.
"""
from __future__ import annotations

import os
import secrets
import shutil
import tempfile
from pathlib import Path

from ..config import DEFAULT_CONFIG, ConfigError, ProjectConfig
from ..credentials import CredentialManager, generate_master_key


class ServiceError(Exception):
    """A user-facing error: the message is safe and meant to be shown as-is."""


def load_dotenv(path: Path) -> None:
    """Minimal .env loader (KEY=VALUE lines). Existing env vars win."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def set_env_line(env: Path, key: str, value: str) -> None:
    lines = env.read_text(encoding="utf-8").splitlines() if env.exists() else []
    lines = [l for l in lines if not l.startswith(f"{key}=")] + [f"{key}={value}"]
    env.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        os.chmod(env, 0o600)
    except OSError:
        pass


class Project:
    def __init__(self, config_path: str | Path = DEFAULT_CONFIG, actor: str = "cli"):
        from ..compat import resolve_config
        self.path = Path(resolve_config(config_path)).resolve()
        self.root = self.path.parent
        self.actor = actor                     # "cli" or "studio" – recorded in the audit log
        load_dotenv(self.root / ".env")
        self._creds: CredentialManager | None = None
        from .activity import ActivityLog
        self.activity = ActivityLog(self.root)
        self.reload()

    def audit(self, event: str, **details) -> None:
        """Record a configuration change (never secrets) in the project's audit log."""
        try:
            import getpass
            from ..security.audit import AuditLogger
            if self.config.security.audit_log:
                AuditLogger(str(self.root / self.config.security.audit_log)).record(
                    event=event, actor=f"{self.actor}:{getpass.getuser()}", **details)
        except Exception:                     # auditing must never block the change itself
            pass

    # ------------------------------------------------------------- create
    @classmethod
    def init(cls, directory: str | Path, name: str = "portwright-project", actor: str = "cli") -> "Project":
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        cfg_path = directory / DEFAULT_CONFIG
        from ..compat import config_in
        if config_in(directory).exists():
            raise ServiceError(f"{config_in(directory)} already exists")
        ProjectConfig(project=name).save(cfg_path)
        env = directory / ".env"
        if not env.exists():
            env.write_text("# Secrets – never commit this file\n"
                           f"PORTWRIGHT_JWT_SECRET={secrets.token_urlsafe(48)}\n"
                           f"PORTWRIGHT_MASTER_KEY={generate_master_key()}\n", encoding="utf-8")
            try:
                os.chmod(env, 0o600)
            except OSError:
                pass
        gi = directory / ".gitignore"
        if not gi.exists():
            gi.write_text(".env\nlogs/\n__pycache__/\n.portwright/\n", encoding="utf-8")
        for d in ("data", "handlers", "logs"):
            (directory / d).mkdir(exist_ok=True)
        return cls(cfg_path, actor)

    # ----------------------------------------------------------- load/save
    def reload(self) -> None:
        try:
            self.config = ProjectConfig.load(self.path)
        except ConfigError as exc:
            raise ServiceError(str(exc)) from None
        self._mtime = self.path.stat().st_mtime
        self._last_good = self.config.model_copy(deep=True)
        try:                                  # log edits made to the file outside Portwright
            self.activity.check_external(self.config, ProjectConfig)
        except Exception:
            pass

    def log_credential(self, action: str, name: str, **kw) -> None:
        """Activity entry for a credential change (credentials live in the vault, not in the file)."""
        try:
            self.activity.record_credential(action, name, via=self.actor, **kw)
        except Exception:
            pass

    def _rollback(self) -> None:
        """Discard in-memory changes that could not be saved."""
        self.config = self._last_good.model_copy(deep=True)

    def changed_on_disk(self) -> bool:
        try:
            return self.path.stat().st_mtime != self._mtime
        except FileNotFoundError:
            return True

    def save(self, force: bool = False) -> None:
        """Validate the whole document, keep a .bak of the previous version, write atomically."""
        before = self._last_good
        try:
            self.config = ProjectConfig.model_validate(self.config.model_dump(mode="json", by_alias=True))
        except Exception as exc:
            self._rollback()
            raise ServiceError(_clean(exc)) from None
        if not force and self.changed_on_disk():
            self._rollback()
            raise ServiceError(f"{self.path.name} was changed outside this session – reload before saving")
        if self.path.exists():
            shutil.copy2(self.path, self.path.with_suffix(".yaml.bak"))
        fd, tmp = tempfile.mkstemp(dir=self.root, prefix=".portwright-", suffix=".yaml")
        os.close(fd)
        try:
            self.config.save(tmp)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        self._mtime = self.path.stat().st_mtime
        self._last_good = self.config.model_copy(deep=True)
        self.audit("config_saved", file=self.path.name)
        try:                                  # what changed, who did it, and the pw command for it
            self.activity.record_diff(before, self.config, via=self.actor)
        except Exception:
            pass

    # --------------------------------------------------------- credentials
    def credentials(self) -> CredentialManager:
        if self._creds is None:
            try:
                self._creds = CredentialManager.from_settings(self.config.security.credentials, self.root)
            except Exception as exc:
                raise ServiceError(str(exc)) from None
        return self._creds

    def reset_credentials(self) -> None:
        self._creds = None

    @property
    def env_file(self) -> Path:
        return self.root / ".env"


def _clean(exc: Exception) -> str:
    """Shorten pydantic errors to their messages."""
    errors = getattr(exc, "errors", None)
    if callable(errors):
        try:
            return "; ".join(str(e.get("msg", e)).removeprefix("Value error, ") for e in errors())
        except Exception:
            pass
    return str(exc)
