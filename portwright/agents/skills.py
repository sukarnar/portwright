"""Skills: markdown playbooks an agent can read on demand (name + description up front, body on request)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from ..config import ConfigError
from .spec import SkillSpec

_FRONT = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
MAX_SKILL_CHARS = 30_000


@dataclass
class Skill:
    name: str
    description: str
    body: str


def load_skill(name: str, spec: SkillSpec, root: str | Path) -> Skill:
    root = Path(root).resolve()
    path = (root / spec.file).resolve()
    if root != path and root not in path.parents:
        raise ConfigError(f"skill '{name}': file must be inside the project folder")
    if not path.is_file():
        raise ConfigError(f"skill '{name}': file '{spec.file}' not found")
    text = path.read_text(encoding="utf-8")
    meta: dict = {}
    m = _FRONT.match(text)
    if m:
        try:
            meta = yaml.safe_load(m.group(1)) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"skill '{name}': bad front-matter ({exc})") from None
        text = text[m.end():]
    return Skill(name, spec.description or str(meta.get("description", "")), text.strip()[:MAX_SKILL_CHARS])


def load_skills(names: list[str], specs: dict[str, SkillSpec], root: str | Path) -> dict[str, Skill]:
    return {n: load_skill(n, specs[n], root) for n in names}


def skill_index(skills: dict[str, Skill]) -> str:
    if not skills:
        return ""
    lines = "\n".join(f"- {s.name}: {s.description or '(no description)'}" for s in skills.values())
    return ("\n\nYou have skills (playbooks). When one applies, call the tool use_skill with its name "
            "and follow it.\nAvailable skills:\n" + lines)
