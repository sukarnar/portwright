"""Old name of Portwright. ``import restforge`` / ``restforge.x.y`` resolve to ``portwright`` modules
(the same module objects, so registries and settings are shared)."""
import importlib
import importlib.abc
import importlib.util
import sys

import portwright as _pw


class _Alias(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def find_spec(self, name, path=None, target=None):
        if name.startswith("restforge."):
            return importlib.util.spec_from_loader(name, self)
        return None

    def create_module(self, spec):
        module = importlib.import_module("portwright" + spec.name[len("restforge"):])
        spec.loader_state = module.__spec__          # the real spec, restored in exec_module
        return module

    def exec_module(self, module):
        real = getattr(module.__spec__, "loader_state", None)
        if real is not None:
            module.__spec__ = real                   # keep portwright's own spec (relative imports)


if not any(isinstance(f, _Alias) for f in sys.meta_path):
    sys.meta_path.insert(0, _Alias())
sys.modules[__name__] = _pw
