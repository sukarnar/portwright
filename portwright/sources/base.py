"""Data-source port + adapter registry (Hexagonal / Ports & Adapters).

Every backend implements :class:`DataSource`. The HTTP layer never talks to a
database, file or upstream API directly – it only calls ``execute``. New
backends are added by subclassing and decorating with ``@register("type")``
(or via the ``portwright.sources`` entry-point group), without touching the core.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from typing import Any, Callable, ClassVar

from ..config import EndpointSpec, SecuritySettings, SourceSpec


class SourceError(Exception):
    """Client-safe error raised by an adapter (message may be shown to callers)."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


class NotFound(SourceError):
    def __init__(self, message: str = "Resource not found"):
        super().__init__(message, 404)


@dataclass
class ExecutionContext:
    endpoint: EndpointSpec
    params: dict[str, Any]
    principal: Any = None
    limit: int = 100
    offset: int = 0
    request_id: str = ""
    credentials: Any = None          # ScopedCredentials (callable sources only)
    extra: dict[str, Any] = field(default_factory=dict)


class DataSource(ABC):
    """Port: the contract every data source adapter fulfils."""

    type_name: ClassVar[str] = ""

    def __init__(self, name: str, spec: SourceSpec, security: SecuritySettings, project_root: str = ".",
                 credentials: Any = None):
        self.name = name
        self.spec = spec
        self.security = security
        self.project_root = project_root
        self.credentials = credentials   # CredentialManager (or None)

    def credential(self):
        """The managed credential bound to this source (``spec.credential``), if any."""
        if not self.spec.credential:
            return None
        if self.credentials is None:
            raise SourceError("credential manager not configured", 500)
        return self.credentials.get(self.spec.credential, consumer=f"source:{self.name}")

    def resolve(self, value):
        """Resolve ${ENV:..} and ${CRED:..} references in a spec value."""
        from ..config import resolve_secrets
        return resolve_secrets(value, self.credentials)

    # lifecycle ------------------------------------------------------------
    async def startup(self) -> None:  # open pools / clients
        pass

    async def shutdown(self) -> None:  # close pools / clients
        pass

    async def health(self) -> bool:
        return True

    # build-time check -----------------------------------------------------
    def validate_endpoint(self, endpoint: EndpointSpec) -> None:
        """Raise ValueError if the endpoint definition is not valid for this source."""

    # request-time ---------------------------------------------------------
    @abstractmethod
    async def execute(self, ctx: ExecutionContext) -> Any:
        """Run the operation and return JSON-serialisable data."""


# --------------------------------------------------------------------------- #
# Registry / Factory
# --------------------------------------------------------------------------- #
_REGISTRY: dict[str, type[DataSource]] = {}


def register(type_name: str) -> Callable[[type[DataSource]], type[DataSource]]:
    def deco(cls: type[DataSource]) -> type[DataSource]:
        cls.type_name = type_name
        _REGISTRY[type_name] = cls
        return cls
    return deco


def _load_plugins() -> None:
    try:
        for ep in [*entry_points(group="portwright.sources"), *entry_points(group="restforge.sources")]:
            if ep.name not in _REGISTRY:
                _REGISTRY[ep.name] = ep.load()
    except Exception:  # pragma: no cover - plugin discovery must never crash core
        pass


def create_source(name: str, spec: SourceSpec, security: SecuritySettings, project_root: str = ".",
                  credentials: Any = None) -> DataSource:
    if spec.type not in _REGISTRY:
        _load_plugins()
    try:
        cls = _REGISTRY[spec.type]
    except KeyError:
        raise ValueError(f"No adapter registered for source type '{spec.type}'") from None
    return cls(name, spec, security, project_root, credentials)


def registered_types() -> list[str]:
    _load_plugins()
    return sorted(_REGISTRY)
