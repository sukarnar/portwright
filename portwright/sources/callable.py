"""Custom Python callable adapter – expose any function as an endpoint.

    def get_quote(symbol: str, ctx=None) -> dict: ...
    async def recompute(region: str) -> dict: ...

Security properties:
* Only modules matching ``security.allowed_callable_modules`` (exact module or
  ``prefix.`` package) can be imported – arbitrary import strings are refused.
* The function receives only validated, declared params; it receives ``ctx``
  (incl. the authenticated principal) only if it declares a ``ctx`` argument.
* ``ctx.credentials`` exposes ONLY the credentials listed in the source's
  ``credentials:`` list:  ``ctx.credentials.field("crm_api", "token")``.
"""
from __future__ import annotations

import importlib
import inspect
import sys
from pathlib import Path
from typing import Any, Callable

import anyio

from ..config import EndpointSpec
from .base import DataSource, ExecutionContext, register


def module_allowed(module: str, allowed: list[str]) -> bool:
    for entry in allowed:
        if entry.endswith("."):
            if module.startswith(entry):
                return True
        elif module == entry or module.startswith(entry + "."):
            return True
    return False


@register("callable")
class CallableSource(DataSource):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._funcs: dict[str, Callable] = {}
        root = str(Path(self.project_root).resolve())
        if root not in sys.path:  # allow project-local modules (e.g. handlers/)
            sys.path.insert(0, root)

    def _resolve(self, target: str) -> Callable:
        if target in self._funcs:
            return self._funcs[target]
        module_name, _, func_name = target.partition(":")
        if not module_name or not func_name:
            raise ValueError(f"target '{target}' must look like 'package.module:function'")
        if not module_allowed(module_name, self.security.allowed_callable_modules):
            raise ValueError(f"module '{module_name}' is not in security.allowed_callable_modules")
        if func_name.startswith("_"):
            raise ValueError("private functions cannot be exposed")
        fn = getattr(importlib.import_module(module_name), func_name, None)
        if not callable(fn):
            raise ValueError(f"'{target}' is not callable")
        self._funcs[target] = fn
        return fn

    def validate_endpoint(self, ep: EndpointSpec) -> None:
        if not ep.target:
            raise ValueError(f"[{ep.name}] callable endpoint needs 'target'")
        target = ep.target if ":" in ep.target or not self.spec.module else f"{self.spec.module}:{ep.target}"
        ep.target = target
        self._resolve(target)

    async def execute(self, ctx: ExecutionContext) -> Any:
        fn = self._resolve(ctx.endpoint.target)
        sig = inspect.signature(fn)
        kwargs = {k: v for k, v in ctx.params.items() if k in sig.parameters}
        if "ctx" in sig.parameters:
            if self.spec.credentials and self.credentials is not None:
                ctx.credentials = self.credentials.scoped(self.spec.credentials, f"source:{self.name}")
            kwargs["ctx"] = ctx
        if inspect.iscoroutinefunction(fn):
            return await fn(**kwargs)
        return await anyio.to_thread.run_sync(lambda: fn(**kwargs))
