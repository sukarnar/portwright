"""Jinja2 template rendering + PDF generation engine."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .filters import ALL_FILTERS


def _jinja_env(template_dir: Path):
    try:
        from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
    except ImportError as exc:
        raise ImportError(
            "Jinja2 is required for report generation.  Install it with:\n"
            "    pip install portwright[reports]\n"
            "or: pip install jinja2"
        ) from exc

    env = Environment(
        loader=FileSystemLoader(str(template_dir)),
        autoescape=select_autoescape(["html", "htm", "xml"]),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters.update(ALL_FILTERS)
    return env


def _resolve_output_path(template: str, now: datetime) -> str:
    """Render Jinja2 expressions inside the output path string, e.g. {{ now | strftime('%Y-%m') }}."""
    try:
        from jinja2 import Environment
    except ImportError:
        return template

    env = Environment()
    env.filters.update(ALL_FILTERS)
    try:
        return env.from_string(template).render(now=now)
    except Exception:
        return template


def render_html(
    template_path: str,
    data: list[dict] | dict,
    *,
    project_root: Path,
    extra_vars: dict[str, Any] | None = None,
    report_name: str = "",
) -> str:
    """Render a Jinja2 HTML template with report data, returning the HTML string."""
    tpl_path = Path(template_path)
    if not tpl_path.is_absolute():
        tpl_path = project_root / tpl_path
    if not tpl_path.exists():
        raise FileNotFoundError(f"Template not found: {tpl_path}")

    template_dir = tpl_path.parent
    template_file = tpl_path.name

    env = _jinja_env(template_dir)
    now = datetime.now(timezone.utc)

    # Normalise: always expose `data` as a list of dicts and `record` for single-row reports
    rows: list[dict] = data if isinstance(data, list) else [data] if isinstance(data, dict) else []
    record: dict = rows[0] if rows and len(rows) == 1 else {}

    ev = extra_vars or {}
    ctx: dict[str, Any] = {
        "data": rows,
        "rows": rows,           # alias
        "record": record,
        "row_count": len(rows),
        "report_name": report_name,
        "generated_at": now,
        "now": now,
        "vars": ev,             # accessible as vars.key or vars['key'] in templates
        **ev,                   # also flat: {{ company_name }} works too
    }

    try:
        tmpl = env.get_template(template_file)
    except Exception as exc:
        raise ValueError(f"Template error in '{tpl_path.name}': {exc}") from exc

    try:
        return tmpl.render(**ctx)
    except Exception as exc:
        raise ValueError(f"Render error in '{tpl_path.name}': {exc}") from exc


def render_pdf(
    html: str,
    output_path: Path,
    *,
    base_url: str | None = None,
) -> Path:
    """Convert rendered HTML to PDF using WeasyPrint, with xhtml2pdf as a fallback."""
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Try WeasyPrint first (best quality)
    try:
        import weasyprint  # type: ignore[import]
        wp = weasyprint.HTML(string=html, base_url=base_url)
        wp.write_pdf(str(output_path))
        return output_path
    except ImportError:
        pass

    # Fallback: xhtml2pdf
    try:
        from xhtml2pdf import pisa  # type: ignore[import]
        with open(output_path, "wb") as fh:
            result = pisa.CreatePDF(html, dest=fh, encoding="utf-8")
        if result.err:
            raise RuntimeError(f"xhtml2pdf error (code {result.err})")
        return output_path
    except ImportError:
        pass

    raise ImportError(
        "A PDF backend is required.  Install one of:\n"
        "    pip install weasyprint       (recommended – full CSS support)\n"
        "    pip install xhtml2pdf        (simpler; limited CSS)"
    )
