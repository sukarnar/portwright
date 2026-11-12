"""portwright – a declarative, secure REST API builder for SQL, files, upstream APIs and Python callables."""


def _version() -> str:
    """The installed package's version (pyproject.toml), so --version and bundles never drift from it.
    Running from a source folder that was never pip-installed falls back to the version below."""
    try:
        from importlib.metadata import PackageNotFoundError, version
        return version("portwright")
    except PackageNotFoundError:
        return "0.8.0"


__version__ = _version()

from .config import ProjectConfig  # noqa: E402
from .sources.base import DataSource, ExecutionContext, SourceError, register  # noqa: E402


def create_app(*args, **kwargs):
    from .server import create_app as _create
    return _create(*args, **kwargs)


__all__ = ["ProjectConfig", "DataSource", "ExecutionContext", "SourceError", "register", "create_app"]
