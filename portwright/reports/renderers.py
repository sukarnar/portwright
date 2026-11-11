"""Output renderers: one per report format.

``pdf`` and ``html`` come from the Jinja2 HTML template (rendered once and shared). ``csv``, ``xlsx``,
``json`` and ``md`` are written straight from the rows; ``columns`` / ``rename`` on the report shape them.
Add a format by registering a ``Renderer`` subclass in ``RENDERERS``.
"""
from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, ClassVar


@dataclass
class ReportArtifact:
    path: Path
    format: str
    bytes: int
    sha256: str

    def to_dict(self) -> dict:
        return {"path": str(self.path), "format": self.format, "bytes": self.bytes, "sha256": self.sha256}

    @classmethod
    def of(cls, path: Path, fmt: str) -> "ReportArtifact":
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 16), b""):
                h.update(chunk)
        return cls(path, fmt, path.stat().st_size, h.hexdigest())


@dataclass
class ReportContext:
    """Everything a renderer may need for one run."""
    name: str
    rows: list[dict]
    columns: list[str] = field(default_factory=list)   # chosen column order (empty: all, first-seen order)
    rename: dict[str, str] = field(default_factory=dict)
    generated_at: datetime | None = None
    html: Callable[[], str] | None = None              # lazily renders the HTML template (pdf/html)
    template_dir: Path | None = None                   # the only folder PDF resources may load from

    def column_names(self) -> list[str]:
        if self.columns:
            return list(self.columns)
        seen: dict[str, None] = {}
        for r in self.rows:
            for k in r:
                seen.setdefault(k, None)
        return list(seen)

    def headers(self) -> list[str]:
        return [self.rename.get(c, c) for c in self.column_names()]

    def table(self) -> list[list[Any]]:
        cols = self.column_names()
        return [[r.get(c) for c in cols] for r in self.rows]

    def records(self) -> list[dict]:
        cols, heads = self.column_names(), self.headers()
        return [{h: r.get(c) for c, h in zip(cols, heads)} for r in self.rows]


def _plain(v: Any) -> Any:
    if isinstance(v, Decimal):
        return int(v) if v == v.to_integral_value() else float(v)
    if isinstance(v, (datetime, date, time)):
        return v.isoformat()
    if isinstance(v, bytes):
        return v.hex()
    return v


def _text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, (dict, list)):
        return json.dumps(v, default=str, ensure_ascii=False)
    return str(_plain(v))


class Renderer:
    format: ClassVar[str] = ""
    extension: ClassVar[str] = ""
    needs_template: ClassVar[bool] = False

    def render(self, ctx: ReportContext, out: Path) -> Path:
        raise NotImplementedError


class HtmlRenderer(Renderer):
    format, extension, needs_template = "html", ".html", True

    def render(self, ctx: ReportContext, out: Path) -> Path:
        out.write_text(ctx.html(), encoding="utf-8")
        return out


class PdfRenderer(Renderer):
    format, extension, needs_template = "pdf", ".pdf", True

    def render(self, ctx: ReportContext, out: Path) -> Path:
        from .engine import render_pdf
        return render_pdf(ctx.html(), out, base_dir=ctx.template_dir)


class CsvRenderer(Renderer):
    format, extension = "csv", ".csv"

    def render(self, ctx: ReportContext, out: Path) -> Path:
        # utf-8-sig so Excel opens non-ASCII text correctly
        with open(out, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh)
            w.writerow(ctx.headers())
            for row in ctx.table():
                w.writerow([_text(v) for v in row])
        return out


class JsonRenderer(Renderer):
    format, extension = "json", ".json"

    def render(self, ctx: ReportContext, out: Path) -> Path:
        out.write_text(json.dumps(ctx.records(), default=lambda v: _text(v), ensure_ascii=False, indent=2),
                       encoding="utf-8")
        return out


class MarkdownRenderer(Renderer):
    format, extension = "md", ".md"

    def render(self, ctx: ReportContext, out: Path) -> Path:
        def cell(v: Any) -> str:
            return _text(v).replace("|", "\\|").replace("\r", " ").replace("\n", " ")

        lines = [f"# {ctx.name}", ""]
        if ctx.generated_at:
            lines += [f"_Generated {ctx.generated_at:%Y-%m-%d %H:%M} UTC · {len(ctx.rows)} record(s)_", ""]
        heads = ctx.headers()
        if heads:
            lines.append("| " + " | ".join(cell(h) for h in heads) + " |")
            lines.append("|" + "|".join("---" for _ in heads) + "|")
            lines += ["| " + " | ".join(cell(v) for v in row) + " |" for row in ctx.table()]
        else:
            lines.append("_No records found._")
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return out


class XlsxRenderer(Renderer):
    format, extension = "xlsx", ".xlsx"
    MAX_WIDTH = 60

    def render(self, ctx: ReportContext, out: Path) -> Path:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill
        from openpyxl.utils import get_column_letter

        wb = Workbook()
        ws = wb.active
        ws.title = (ctx.name or "Report")[:31]
        heads = ctx.headers()
        ws.append(heads)
        for c in ws[1]:
            c.font = Font(bold=True, color="FFFFFF")
            c.fill = PatternFill("solid", fgColor="1A4E8A")
        widths = [len(str(h)) for h in heads]
        for row in ctx.table():
            vals = [self._cell(v) for v in row]
            ws.append(vals)
            for i, v in enumerate(vals):
                widths[i] = max(widths[i], len(str(v)) if v is not None else 0)
                if isinstance(v, str) and v.startswith("="):
                    ws.cell(ws.max_row, i + 1).data_type = "s"   # text, never a formula (no injection)
        for i, w in enumerate(widths, start=1):
            ws.column_dimensions[get_column_letter(i)].width = min(w + 2, self.MAX_WIDTH)
        if heads:
            ws.freeze_panes = "A2"
            ws.auto_filter.ref = ws.dimensions
        wb.save(out)
        return out

    @staticmethod
    def _cell(v: Any) -> Any:
        if v is None or isinstance(v, (bool, int, float, datetime, date, time)):
            if isinstance(v, datetime) and v.tzinfo is not None:
                return v.replace(tzinfo=None)              # Excel has no time zones
            return v
        if isinstance(v, Decimal):
            return _plain(v)
        return _text(v)


RENDERERS: dict[str, type[Renderer]] = {r.format: r for r in
                                        (PdfRenderer, HtmlRenderer, MarkdownRenderer, CsvRenderer,
                                         XlsxRenderer, JsonRenderer)}
EXTENSIONS = {r.extension for r in RENDERERS.values()} | {".htm"}


def renderer(fmt: str) -> Renderer:
    try:
        return RENDERERS[fmt]()
    except KeyError:
        raise ValueError(f"unknown report format '{fmt}'; choose from {', '.join(RENDERERS)}") from None
