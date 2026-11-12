"""The built-in library: system prompts, skills and sample tools to start agents and workflow steps from.

Files live in ``portwright/library``:

* ``prompts/<id>.md`` - a system prompt for an agent (``instructions``) or an LLM step (``system``). Front
  matter: title, description, category, use ([agent, llm]), output (text | json), tools and skills it works
  well with (library ids), example_input.
* ``skills/<id>.md`` - a markdown playbook, copied into the project's ``skills/`` folder when added.
* ``tools.yaml`` - sample tool definitions with ``{placeholders}`` for what the project has to choose
  (a SQL source, an endpoint, a destination).

Everything is copied into the project when used, so a project never changes when portwright is upgraded.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

LIBRARY_DIR = Path(__file__).resolve().parent.parent / "library"
KINDS = ("prompt", "skill", "tool")
NEEDS = ("sql_source", "endpoint", "destination")       # what a sample tool can ask the project for


class LibraryError(ValueError):
    pass


@dataclass
class LibraryItem:
    kind: str                    # prompt | skill | tool
    id: str
    title: str
    description: str
    category: str
    text: str = ""               # prompt / skill body (markdown); empty for tools
    use: list[str] = field(default_factory=list)          # prompts: agent and/or llm
    output: str = "text"                                   # prompts: text | json
    tools: list[str] = field(default_factory=list)        # prompts: sample tools that fit
    skills: list[str] = field(default_factory=list)       # prompts: skills that fit
    example_input: str = ""
    name: str = ""                                          # tools: suggested tool name
    needs: list[str] = field(default_factory=list)        # tools: sql_source / endpoint / destination
    spec: dict = field(default_factory=dict)               # tools: the tool definition with {placeholders}

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v not in ("", [], {})}


def _front_matter(path: Path) -> tuple[dict, str]:
    raw = path.read_text(encoding="utf-8")
    m = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n", raw, re.S)
    if not m:
        raise LibraryError(f"{path.name}: missing front matter (--- title: ... ---)")
    meta = yaml.safe_load(m.group(1)) or {}
    return meta, raw[m.end():].strip() + "\n"


def _md_items(kind: str, folder: Path) -> list[LibraryItem]:
    out = []
    for p in sorted(folder.glob("*.md")):
        meta, body = _front_matter(p)
        out.append(LibraryItem(
            kind=kind, id=p.stem, title=str(meta.get("title") or p.stem), description=str(meta.get("description", "")),
            category=str(meta.get("category", "general")), text=body, use=list(meta.get("use") or []),
            output=str(meta.get("output", "text")), tools=list(meta.get("tools") or []),
            skills=list(meta.get("skills") or []), example_input=str(meta.get("example_input", ""))))
    return out


@lru_cache(maxsize=1)
def _all() -> tuple[LibraryItem, ...]:
    items = _md_items("prompt", LIBRARY_DIR / "prompts") + _md_items("skill", LIBRARY_DIR / "skills")
    for t in yaml.safe_load((LIBRARY_DIR / "tools.yaml").read_text(encoding="utf-8")) or []:
        items.append(LibraryItem(kind="tool", id=t["id"], title=t["title"], description=t.get("description", ""),
                                 category=t.get("category", "general"), name=t.get("name", t["id"]),
                                 needs=list(t.get("needs") or []), spec=dict(t["spec"])))
    return tuple(items)


def items(kind: str | None = None, category: str | None = None, query: str | None = None,
          use: str | None = None) -> list[LibraryItem]:
    """Library items, optionally of one kind / category, usable as (agent | llm), or matching words."""
    if kind and kind not in KINDS:
        raise LibraryError(f"unknown library kind '{kind}' (use: {', '.join(KINDS)})")
    words = [w.lower() for w in (query or "").split()]
    out = []
    for it in _all():
        if kind and it.kind != kind or category and it.category != category or use and use not in it.use:
            continue
        hay = f"{it.id} {it.title} {it.description} {it.category} {it.text}".lower()
        if all(w in hay for w in words):
            out.append(it)
    return out


def get(kind: str, item_id: str) -> LibraryItem:
    for it in _all():
        if it.kind == kind and it.id == item_id:
            return it
    known = ", ".join(i.id for i in _all() if i.kind == kind)
    raise LibraryError(f"no library {kind} '{item_id}'; available: {known}")


def find(ref: str) -> LibraryItem:
    """An item by 'kind/id' or by a plain id that only one kind uses."""
    if "/" in ref:
        kind, item_id = ref.split("/", 1)
        return get(kind.rstrip("s"), item_id)
    hits = [i for i in _all() if i.id == ref]
    if not hits:
        raise LibraryError(f"nothing in the library is called '{ref}' (see: portwright library list)")
    if len(hits) > 1:
        raise LibraryError(f"'{ref}' is a {' and a '.join(h.kind for h in hits)}; say which: "
                           + ", ".join(f"{h.kind}/{h.id}" for h in hits))
    return hits[0]


def categories(kind: str | None = None) -> list[str]:
    return sorted({i.category for i in _all() if not kind or i.kind == kind})


def tool_spec(item: LibraryItem, choices: dict[str, str]) -> dict:
    """The sample tool's definition with its placeholders filled from *choices* (sql_source=…, ...)."""
    missing = [n for n in item.needs if not choices.get(n)]
    if missing:
        flags = ", ".join("--" + n.replace("_", "-").replace("sql-source", "source") for n in missing)
        raise LibraryError(f"the sample tool '{item.id}' needs {', '.join(missing)} ({flags})")

    def fill(v: Any) -> Any:
        if isinstance(v, str):
            for k, val in choices.items():
                v = v.replace("{" + k + "}", str(val))
            return v
        if isinstance(v, list):
            return [fill(x) for x in v]
        if isinstance(v, dict):
            return {k: fill(x) for k, x in v.items()}
        return v
    return fill(item.spec)
