"""Active Directory source: a load *target* for group membership (not served as endpoints)."""
from __future__ import annotations

from typing import Any

from .base import DataSource, ExecutionContext, SourceError, register


@register("ldap")
class LdapSource(DataSource):
    _dir = None

    def validate_endpoint(self, endpoint) -> None:
        raise ValueError(f"source '{self.name}' is an Active Directory connection; it can be the target of "
                         f"a group load, not an endpoint")

    async def execute(self, ctx: ExecutionContext) -> Any:  # pragma: no cover - never routed
        raise SourceError("not an endpoint source", 500)

    def directory(self):
        """The directory client (connects on first use, not at server start)."""
        if self._dir is None:
            from ..directory import open_directory
            cred = None
            if self.spec.credential:
                cred = self.credential()
            self._dir = open_directory(self.spec, cred, self.project_root)
        return self._dir

    async def shutdown(self) -> None:
        if self._dir is not None:
            self._dir.close()
            self._dir = None
