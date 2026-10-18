"""The portwright guide: Markdown topics shared by Studio (Help screen), the CLI (`portwright docs`)
and the README. Each topic has an intro plus "In Studio" and "With the CLI" parts."""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

DOCS_DIR = Path(__file__).parent
CLI_REFERENCE = "cli-reference"


@dataclass
class Topic:
    slug: str
    title: str
    screen: str | None
    intro: str
    studio: str
    cli: str

    @property
    def text(self) -> str:
        return "\n\n".join(x for x in (self.intro, self.studio, self.cli) if x)


def _parse(path: Path) -> Topic:
    raw = path.read_text(encoding="utf-8")
    meta: dict[str, str] = {}
    m = re.match(r"^---\n(.*?)\n---\n", raw, re.S)
    if m:
        for line in m.group(1).splitlines():
            k, _, v = line.partition(":")
            meta[k.strip()] = v.strip()
        raw = raw[m.end():]
    intro, studio, cli = raw, "", ""
    if "<!-- studio -->" in raw:
        intro, _, rest = raw.partition("<!-- studio -->")
        studio, _, cli = rest.partition("<!-- cli -->")
    elif "<!-- cli -->" in raw:
        intro, _, cli = raw.partition("<!-- cli -->")
    slug = re.sub(r"^\d+-", "", path.stem)
    return Topic(slug, meta.get("title", slug), meta.get("screen") or None, intro.strip(), studio.strip(),
                 cli.strip())


@lru_cache(maxsize=1)
def topics() -> list[Topic]:
    out = [_parse(p) for p in sorted(DOCS_DIR.glob("[0-9][0-9]-*.md"))]
    out.append(Topic(CLI_REFERENCE, "CLI reference", None,
                     "Every command and option, generated from the installed portwright.", "", ""))
    return out


def get(slug: str) -> Topic:
    for t in topics():
        if t.slug == slug:
            if slug == CLI_REFERENCE and not t.cli:
                t.cli = cli_reference()
            return t
    raise KeyError(slug)


def for_screen(screen: str) -> str:
    """The topic that documents a Studio screen."""
    first = {"dashboard": "getting-started", "sources": "sources", "credentials": "credentials",
             "endpoints": "endpoints", "loads": "loads", "access": "security"}
    return first.get(screen, "getting-started")


def search(query: str) -> list[tuple[str, str, str]]:
    """(slug, title, snippet) of topics containing every word of the query."""
    words = [w.lower() for w in query.split() if w.strip()]
    if not words:
        return []
    hits = []
    for t in topics():
        body = get(t.slug).text if t.slug == CLI_REFERENCE else t.text
        low = (t.title + "\n" + body).lower()
        if all(w in low for w in words):
            i = low.find(words[0])
            start = max(0, i - 60)
            snippet = re.sub(r"\s+", " ", (t.title + "\n" + body)[start:start + 160]).strip()
            hits.append((t.slug, t.title, ("…" if start else "") + snippet + "…"))
    return hits


def split_code(md: str) -> list[tuple[str, str]]:
    """[(kind, text)] with kind 'md' or a fenced code language ('powershell', 'bat', '' …)."""
    parts: list[tuple[str, str]] = []
    pos = 0
    for m in re.finditer(r"^```([\w-]*)\n(.*?)^```\s*$", md, re.S | re.M):
        if m.start() > pos and md[pos:m.start()].strip():
            parts.append(("md", md[pos:m.start()].strip()))
        parts.append((m.group(1) or "text", m.group(2).rstrip("\n")))
        pos = m.end()
    if md[pos:].strip():
        parts.append(("md", md[pos:].strip()))
    return parts


def cli_reference() -> str:
    """Markdown for every CLI command, built from the Typer app itself (never out of date)."""
    import typer
    from ..cli import app

    root = typer.main.get_command(app)
    lines: list[str] = []

    def kind(p) -> str:
        return getattr(p, "param_type_name", "")

    def walk(cmd, prefix: str) -> None:
        if hasattr(cmd, "list_commands"):           # a group (Typer vendors click, so duck-type)
            if prefix:
                lines.append(f"### portwright {prefix}\n\n{(cmd.help or '').strip()}\n")
            for name in cmd.list_commands(None):
                walk(cmd.get_command(None, name), f"{prefix} {name}".strip())
            return
        args = " ".join(f"<{p.name.upper()}>" if p.required else f"[{p.name.upper()}]"
                        for p in cmd.params if kind(p) == "argument")
        lines.append(f"**portwright {prefix}** {args}".rstrip())
        lines.append("")
        if cmd.help:
            lines.append(re.sub(r"\s+", " ", cmd.help.strip()))
            lines.append("")
        opts = [p for p in cmd.params if kind(p) == "option" and p.name != "help"]
        if opts:
            lines.append("| Option | Meaning |")
            lines.append("|---|---|")
            for o in opts:
                flags = ", ".join(f"`{x}`" for x in list(o.opts) + list(o.secondary_opts))
                is_flag = getattr(o, "is_flag", False)
                default = "" if o.default in (None, False, [], ()) or is_flag or callable(o.default) \
                    else f" (default `{o.default}`)"
                helptext = re.sub(r"\s+", " ", (o.help or "").replace("|", "/")).strip()
                lines.append(f"| {flags} | {helptext}{default} |")
            lines.append("")
    walk(root, "")
    return "\n".join(lines)
