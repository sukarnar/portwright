"""portwright – a declarative, secure REST API builder for SQL, files, upstream APIs and Python callables."""
__version__ = "0.7.0"

from .config import ProjectConfig  # noqa: E402
from .sources.base import DataSource, ExecutionContext, SourceError, register  # noqa: E402


def create_app(*args, **kwargs):
    from .server import create_app as _create
    return _create(*args, **kwargs)


__all__ = ["ProjectConfig", "DataSource", "ExecutionContext", "SourceError", "register", "create_app"]
