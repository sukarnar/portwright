"""ReportService: fetch data → render each requested format → write the output files."""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import TEMPLATE_FORMATS, ReportSpec
from .renderers import EXTENSIONS, ReportArtifact, ReportContext, renderer

_READ_SQL = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)
_BIND = re.compile(r"(?<![:\w]):([A-Za-z_]\w*)")


@dataclass
class ReportResult:
    name: str
    outputs: list[ReportArtifact]
    row_count: int
    seconds: float
    truncated: bool = False          # the source had more rows than from.max_rows
    warnings: list[str] = field(default_factory=list)

    @property
    def output_path(self) -> Path:   # first output (kept for single-format callers)
        return self.outputs[0].path

    @property
    def format(self) -> str:
        return self.outputs[0].format

    def to_dict(self) -> dict:
        return {"report": self.name, "outputs": [o.to_dict() for o in self.outputs], "row_count": self.row_count,
                "seconds": self.seconds, "truncated": self.truncated, "warnings": self.warnings}


def _err(msg: str) -> Exception:
    from ..services.project import ServiceError
    return ServiceError(msg)


def output_stem(raw: str) -> str:
    """The output path without a known report extension (``a.b/x.v2.pdf`` → ``a.b/x.v2``)."""
    p = Path(raw)
    return str(p.with_suffix("")) if p.suffix.lower() in EXTENSIONS else raw


class ReportService:
    def __init__(self, project: Any):
        self._p = project

    # ------------------------------------------------------------------ list
    def list(self) -> list[dict]:
        out = []
        for r in self._p.config.reports:
            src = r.from_.endpoint and f"endpoint:{r.from_.endpoint}" or f"source:{r.from_.source}"
            out.append({
                "name": r.name,
                "description": r.description,
                "from": src,
                "template": r.template or "",
                "formats": r.effective_formats(),
                "output": r.output,
            })
        return out

    def get(self, name: str) -> ReportSpec:
        spec = self._p.config.report(name)
        if spec is None:
            raise _err(f"no report '{name}' – see 'portwright report list'")
        return spec

    # ------------------------------------------------------------------ scaffold
    def scaffold_template(self, name: str, template_path: str) -> Path:
        """Write a starter Jinja2 HTML template at *template_path*."""
        dest = Path(template_path)
        if not dest.is_absolute():
            dest = self._p.root / dest
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(STARTER_TEMPLATE.strip() + "\n", encoding="utf-8")
        return dest

    # ------------------------------------------------------------------ run
    def run(
        self,
        name: str,
        *,
        formats: list[str] | None = None,
        output: str | None = None,
        params: dict[str, Any] | None = None,
        data: list[dict] | dict | None = None,
        extra_vars: dict[str, Any] | None = None,
        as_html: bool = False,
    ) -> ReportResult:
        """Run a registered report.

        ``formats`` overrides the report's formats (``as_html=True`` is the old spelling of ``["html"]``);
        ``data`` supplies the rows directly (e.g. from a workflow step) instead of reading ``from``.
        """
        spec = self.get(name)
        t0 = time.monotonic()
        fmts = list(formats or (["html"] if as_html else spec.effective_formats()))
        truncated = False
        if data is None:
            rows, truncated = self._fetch(spec, params or {})
        else:
            rows = _as_rows(data)
        merged_vars = {**(spec.vars or {}), **(extra_vars or {})}
        return self._render(name, spec.template, rows, fmts, output or spec.output, merged_vars,
                            spec.columns, spec.rename, t0, truncated)

    def render_inline(
        self,
        rows: list[dict] | dict,
        formats: list[str],
        output: str,
        *,
        name: str = "report",
        template: str | None = None,
        vars: dict[str, Any] | None = None,
        columns: list[str] | None = None,
        rename: dict[str, str] | None = None,
    ) -> ReportResult:
        """Render rows that are not tied to a registered report."""
        return self._render(name, template, _as_rows(rows), list(formats), output, vars or {},
                            columns or [], rename or {}, time.monotonic(), False)

    # ------------------------------------------------------------------ render
    def _render(self, name: str, template: str | None, rows: list[dict], fmts: list[str], raw_output: str | None,
                vars: dict[str, Any], columns: list[str], rename: dict[str, str], t0: float,
                truncated: bool) -> ReportResult:
        if not fmts:
            raise _err("no output format given")
        fmts = list(dict.fromkeys(fmts))
        rends = [renderer(f) for f in fmts]                     # unknown format → ValueError before any work
        need = sorted(TEMPLATE_FORMATS & set(fmts))
        if need and not template:
            raise _err(f"report '{name}': format(s) {need} need a template")

        from .engine import _resolve_output_path, render_html, resolve_template_path
        now = datetime.now(timezone.utc)
        tpl_dir = resolve_template_path(template, self._p.root).parent if need else None
        cache: dict[str, str] = {}

        def html() -> str:
            if "html" not in cache:
                cache["html"] = render_html(template, rows, project_root=self._p.root, report_name=name,
                                            extra_vars=vars or None, now=now)
            return cache["html"]

        ctx = ReportContext(name, rows, columns, rename, now, html, tpl_dir)
        stem = output_stem(_resolve_output_path(raw_output or f"reports/output/{name}", now, report_name=name))
        base = Path(stem)
        if not base.is_absolute():
            base = self._p.root / base
        base.parent.mkdir(parents=True, exist_ok=True)

        outputs = []
        for r in rends:
            out = base.parent / (base.name + r.extension)
            r.render(ctx, out)
            outputs.append(ReportArtifact.of(out, r.format))
        warnings = [f"stopped after {len(rows)} rows (from.max_rows); the source has more"] if truncated else []
        return ReportResult(name, outputs, len(rows), round(time.monotonic() - t0, 2), truncated, warnings)

    # ------------------------------------------------------------------ fetch
    def _fetch(self, spec: ReportSpec, extra_params: dict[str, Any]) -> tuple[list[dict], bool]:
        """Rows for the report from its configured endpoint or source, plus a truncated flag."""
        frm = spec.from_
        merged = {**(frm.params or {}), **extra_params}
        if frm.endpoint:
            from ..services.runtime import RuntimeService
            return RuntimeService(self._p).fetch_rows(frm.endpoint, merged, paginate=frm.paginate,
                                                      max_rows=frm.max_rows, actor=f"report:{spec.name}")
        src = self._p.config.sources.get(frm.source)
        if src is None:
            raise _err(f"report references unknown source '{frm.source}'")
        if src.type == "file":
            rows = self._source(frm.source)._load()
            return list(rows[:frm.max_rows]), len(rows) > frm.max_rows
        if src.type == "sql":
            query = frm.query
            if query is None:                                   # legacy: query passed as the _sql param
                query = merged.pop("_sql", None)
            else:
                merged.pop("_sql", None)
            if not query:
                raise _err(f"report '{spec.name}': SQL sources need 'from.query' (one SELECT statement)")
            return self._read_sql(frm.source, query, merged, frm.max_rows)
        raise _err(
            f"source type '{src.type}' is not directly supported for reports; "
            "expose it via an endpoint and use 'from.endpoint' instead"
        )

    def _source(self, name: str):
        from ..sources import create_source
        cfg = self._p.config
        return create_source(name, cfg.sources[name], cfg.security, str(self._p.root), self._p.credentials())

    def _read_sql(self, source: str, query: str, params: dict[str, Any], max_rows: int) -> tuple[list[dict], bool]:
        """Run one read-only SELECT with bound parameters; the transaction is always rolled back."""
        from sqlalchemy import text
        from sqlalchemy.exc import SQLAlchemyError
        q = query.strip().rstrip(";").strip()
        if ";" in q:
            raise _err("report query: one statement only (no ';')")
        if not _READ_SQL.match(q):
            raise _err("report query must be a SELECT (or WITH … SELECT) statement")
        names = set(_BIND.findall(q))
        missing = sorted(n for n in names if n not in params)
        if missing:
            raise _err(f"report query needs param(s) {missing}")
        binds = {n: (None if params[n] == "" else params[n]) for n in names}
        src = self._source(source)
        src._build_engine()
        try:
            with src.engine.connect() as conn:
                result = conn.execution_options(stream_results=True).execute(text(q), binds)
                rows = [dict(r._mapping) for r in result.fetchmany(max_rows + 1)]
                result.close()
                conn.rollback()
        except SQLAlchemyError as exc:
            raise _err(f"report query failed: {str(getattr(exc, 'orig', exc))[:300]}") from None
        finally:
            src.engine.dispose()
        return rows[:max_rows], len(rows) > max_rows


def _as_rows(data: Any) -> list[dict]:
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        data = data["items"]
    elif isinstance(data, dict) and isinstance(data.get("rows"), list):    # a SQL tool result
        data = data["rows"]
    if isinstance(data, dict):
        return [data]
    if not isinstance(data, list):
        raise _err("report data must be a list of rows (objects)")
    return [r if isinstance(r, dict) else {"value": r} for r in data]


STARTER_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>{{ report_name }}</title>
<style>
  body { font-family: Arial, Helvetica, sans-serif; font-size: 12px; color: #222; margin: 20mm; }
  h1   { font-size: 20px; color: #1a4e8a; border-bottom: 2px solid #1a4e8a; padding-bottom: 6px; }
  .meta { color: #666; font-size: 10px; margin-bottom: 16px; }
  table { border-collapse: collapse; width: 100%; margin-top: 12px; }
  th    { background: #1a4e8a; color: #fff; text-align: left; padding: 6px 8px; font-size: 11px; }
  td    { padding: 5px 8px; border-bottom: 1px solid #ddd; }
  tr:nth-child(even) td { background: #f5f8fc; }
  .footer { margin-top: 20px; font-size: 9px; color: #999; text-align: right; }
  .total  { font-weight: bold; background: #eef2f8; }
</style>
</head>
<body>

<h1>{{ report_name | title }}</h1>
<p class="meta">Generated: {{ generated_at | datetime }}  &nbsp;|&nbsp; {{ row_count }} record(s)</p>

{% if data %}
<table>
  <thead>
    <tr>
      {% for col in data[0].keys() %}
      <th>{{ col | replace('_', ' ') | title }}</th>
      {% endfor %}
    </tr>
  </thead>
  <tbody>
    {% for row in data %}
    <tr>
      {% for value in row.values() %}
      <td>{{ value | default_if_none }}</td>
      {% endfor %}
    </tr>
    {% endfor %}
  </tbody>
</table>
{% else %}
<p><em>No records found.</em></p>
{% endif %}

<p class="footer">Portwright report &bull; {{ generated_at | strftime('%Y-%m-%d %H:%M UTC') }}</p>

</body>
</html>
"""
