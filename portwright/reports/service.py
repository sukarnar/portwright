"""ReportService: orchestrates data fetching + template rendering + PDF output."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass
class ReportResult:
    name: str
    output_path: Path
    format: str          # "pdf" or "html"
    row_count: int
    seconds: float


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
                "template": r.template,
                "output": r.output,
            })
        return out

    def get(self, name: str):
        cfg = self._p.config
        spec = next((r for r in cfg.reports if r.name == name), None)
        if spec is None:
            from ..services.project import ServiceError
            raise ServiceError(f"no report '{name}' – see 'portwright report list'")
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
        output: str | None = None,
        as_html: bool = False,
        params: dict[str, str] | None = None,
        extra_vars: dict[str, Any] | None = None,
    ) -> ReportResult:
        import time
        from ..services.project import ServiceError

        spec = self.get(name)
        t0 = time.monotonic()

        # Fetch data -------------------------------------------------------
        rows = self._fetch(spec, params or {})

        # Render HTML template ---------------------------------------------
        from .engine import render_html, _resolve_output_path
        merged_vars = {**(getattr(spec, "vars", None) or {}), **(extra_vars or {})}
        html = render_html(
            spec.template,
            rows,
            project_root=self._p.root,
            report_name=name,
            extra_vars=merged_vars or None,
        )

        # Resolve output path (may contain Jinja2 expressions) -------------
        now = datetime.now(timezone.utc)
        raw_output = output or spec.output or f"reports/output/{name}"
        resolved = _resolve_output_path(raw_output, now)
        if as_html and not resolved.endswith(".html"):
            resolved = resolved.replace(".pdf", "") + ".html"
        out_path = Path(resolved)
        if not out_path.is_absolute():
            out_path = self._p.root / out_path

        # Write output -----------------------------------------------------
        if as_html or resolved.endswith(".html"):
            out_path.with_suffix(".html").parent.mkdir(parents=True, exist_ok=True)
            out_path = out_path.with_suffix(".html")
            out_path.write_text(html, encoding="utf-8")
            fmt = "html"
        else:
            out_path = out_path.with_suffix(".pdf")
            from .engine import render_pdf
            base_url = (self._p.root / spec.template).parent.as_uri() if spec.template else None
            render_pdf(html, out_path, base_url=base_url)
            fmt = "pdf"

        seconds = round(time.monotonic() - t0, 2)
        return ReportResult(name=name, output_path=out_path, format=fmt,
                            row_count=len(rows), seconds=seconds)

    # ------------------------------------------------------------------ helpers
    def _fetch(self, spec: Any, extra_params: dict[str, str]) -> list[dict]:
        """Pull all rows for the report from its configured source."""
        from ..services.project import ServiceError
        from ..services.runtime import RuntimeService

        merged_params = {**(spec.from_.params or {}), **extra_params}

        if spec.from_.endpoint:
            rt = RuntimeService(self._p)
            result = rt.call(spec.from_.endpoint, merged_params, actor="report")
            if not result.ok:
                raise ServiceError(
                    f"endpoint '{spec.from_.endpoint}' returned HTTP {result.status}: {result.data}"
                )
            data = result.data
            # Unwrap paginated {items: [...]} or a plain list
            if isinstance(data, list):
                rows = data
            elif isinstance(data, dict) and "items" in data:
                rows = data["items"]
            else:
                rows = [data]
            return rows

        if spec.from_.source:
            return self._fetch_from_source(spec.from_.source, merged_params)

        raise ServiceError("report has no 'from' configured")

    def _fetch_from_source(self, source_name: str, params: dict) -> list[dict]:
        """Read rows directly from a file/SQL source (bypassing HTTP)."""
        from ..services.project import ServiceError

        cfg = self._p.config
        src = cfg.sources.get(source_name)
        if src is None:
            raise ServiceError(f"report references unknown source '{source_name}'")

        if src.type == "file":
            return self._read_file_source(src)
        if src.type == "sql":
            return self._read_sql_source(src, source_name, params)

        raise ServiceError(
            f"source type '{src.type}' is not directly supported for reports; "
            "expose it via an endpoint and use 'from.endpoint' instead"
        )

    def _read_file_source(self, src: Any) -> list[dict]:
        from ..sources.files import FileSource  # type: ignore[import]
        fs = FileSource(src, root=self._p.root / self._p.config.security.data_root)
        return fs.read_all()

    def _read_sql_source(self, src: Any, source_name: str, params: dict) -> list[dict]:
        """Execute the first query endpoint that uses this source, or run a raw count."""
        from sqlalchemy import text, create_engine
        from ..config import resolve_secrets
        url = resolve_secrets(src.url or "", self._p.credentials)
        engine = create_engine(url)
        # Raw SQL from params if supplied, otherwise just return metadata
        raw_sql = params.pop("_sql", None)
        if not raw_sql:
            raise ValueError(
                "SQL sources need a query. Use 'from.endpoint' for a pre-defined query, "
                "or pass _sql='SELECT ...' as a report param."
            )
        with engine.connect() as conn:
            result = conn.execute(text(raw_sql), params)
            cols = list(result.keys())
            return [dict(zip(cols, row)) for row in result.fetchall()]


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
