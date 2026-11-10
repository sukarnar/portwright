"""Model registry: inline ``llm_models`` plus models loaded from ``llm_models_include`` files."""
from __future__ import annotations

import glob
from pathlib import Path

import yaml

from ..config import ConfigError, ProjectConfig
from .spec import LlmModelSpec


def load_models(config: ProjectConfig, root: str | Path) -> dict[str, LlmModelSpec]:
    """All models by name. An included file maps names to model specs (optionally under ``llm_models:``).
    Inline models win over included ones; a name defined in two files is an error."""
    root = Path(root)
    models: dict[str, LlmModelSpec] = {}
    origin: dict[str, str] = {}
    for pattern in config.llm_models_include:
        base = Path(pattern)
        files = sorted(glob.glob(str(base if base.is_absolute() else root / pattern)))
        if not files:
            raise ConfigError(f"llm_models_include '{pattern}' matches no files")
        for f in files:
            try:
                data = yaml.safe_load(Path(f).read_text(encoding="utf-8")) or {}
                data = data.get("llm_models", data)
                if not isinstance(data, dict):
                    raise ValueError("expected a mapping of model name to settings")
                for name, spec in data.items():
                    if name in origin:
                        raise ConfigError(f"model '{name}' is defined in both {origin[name]} and {f}")
                    models[name] = LlmModelSpec.model_validate(spec)
                    origin[name] = f
            except ConfigError:
                raise
            except Exception as exc:  # noqa: BLE001 - report which file is wrong
                raise ConfigError(f"model file '{f}': {exc}") from exc
    models.update(config.llm_models)
    return models


def check_agent_models(config: ProjectConfig, root: str | Path) -> list[str]:
    """Problems with agent -> model references (models may live in included files)."""
    try:
        models = load_models(config, root)
    except ConfigError as exc:
        return [str(exc)]
    out = [f"agent '{n}' uses unknown model '{a.model}'" for n, a in config.agents.items() if a.model not in models]
    for n, w in config.workflows.items():
        for st in w.steps:
            if st.model and (st.type == "llm" or (st.type == "foreach" and st.do == "llm")) and st.model not in models:
                out.append(f"workflow '{n}' step '{st.id}' uses unknown model '{st.model}'")
    return out
