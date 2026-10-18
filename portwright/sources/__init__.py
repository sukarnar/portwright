"""Built-in data source adapters. Importing this package registers them."""
from . import callable, files, rest, sql  # noqa: F401  (registration side-effects)
from .base import DataSource, ExecutionContext, NotFound, SourceError, create_source, register, registered_types

__all__ = ["DataSource", "ExecutionContext", "NotFound", "SourceError",
           "create_source", "register", "registered_types"]
