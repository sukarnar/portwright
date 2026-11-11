"""Jinja2 template rendering + PDF generation engine.

Templates render in a ``SandboxedEnvironment``: they can format data but cannot reach Python internals
(``__class__``, ``__globals__``…). This matters because templates may be written by an AI author.
PDF backends may only load resources that are inline (``data:``) or files under the template's folder,
so a template cannot make the server fetch remote URLs.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from .filters import ALL_FILTERS


def _sandbox():
    try:
        from jinja2.sandbox import SandboxedEnvironment
    except ImportError as exc:
        raise ImportError(
            "Jinja2 is required for report generation.  Install it with:\n"
            "    pip install portwright[reports]\n"
            "or: pip install jinja2"
        ) from exc
    return SandboxedEnvironment


def _jinja_env(template_dir: Path):
    from jinja2 import FileSystemLoader, StrictUndefined, select_autoescape
    env = _sandbox()(
        loader=FileSystemLoader(str(template_dir)),
        autoescape=select_autoescape(["html", "htm", "xml"]),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters.update(ALL_FILTERS)
    return env


def _resolve_output_path(template: str, now: datetime, **extra: Any) -> str:
    """Render Jinja2 expressions inside the output path string, e.g. {{ now | strftime('%Y-%m') }}."""
    try:
        env = _sandbox()()
    except ImportError:
        return template
    env.filters.update(ALL_FILTERS)
    try:
        return env.from_string(template).render(now=now, **extra)
    except Exception:
        return template


def template_context(data: list[dict] | dict, *, report_name: str = "",
                     extra_vars: dict[str, Any] | None = None, now: datetime | None = None) -> dict[str, Any]:
    """The variables every report template sees."""
    now = now or datetime.now(timezone.utc)
    rows: list[dict] = data if isinstance(data, list) else [data] if isinstance(data, dict) else []
    record: dict = rows[0] if rows and len(rows) == 1 else {}
    ev = extra_vars or {}
    return {
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


def resolve_template_path(template_path: str | Path, project_root: Path) -> Path:
    tpl_path = Path(template_path)
    if not tpl_path.is_absolute():
        tpl_path = project_root / tpl_path
    if not tpl_path.exists():
        raise FileNotFoundError(f"Template not found: {tpl_path}")
    return tpl_path


def render_html(
    template_path: str,
    data: list[dict] | dict,
    *,
    project_root: Path,
    extra_vars: dict[str, Any] | None = None,
    report_name: str = "",
    now: datetime | None = None,
) -> str:
    """Render a Jinja2 HTML template with report data, returning the HTML string."""
    tpl_path = resolve_template_path(template_path, project_root)
    env = _jinja_env(tpl_path.parent)
    ctx = template_context(data, report_name=report_name, extra_vars=extra_vars, now=now)
    try:
        tmpl = env.get_template(tpl_path.name)
    except Exception as exc:
        raise ValueError(f"Template error in '{tpl_path.name}': {exc}") from exc
    try:
        return tmpl.render(**ctx)
    except Exception as exc:
        raise ValueError(f"Render error in '{tpl_path.name}': {exc}") from exc


# --------------------------------------------------------------------------- #
# Resource guard for PDF backends
# --------------------------------------------------------------------------- #
def local_resource(url: str, base_dir: Path | None) -> Path | None:
    """The local file a resource URL may load, or None when it must be blocked.

    Allowed: ``file:`` URLs and plain/relative paths that resolve inside *base_dir*.
    Everything else (http, https, ftp, file outside base_dir, UNC) is blocked. ``data:`` URLs are handled
    by the caller because they need no file.
    """
    if base_dir is None or not url:
        return None
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    if scheme == "file":
        if parsed.netloc and parsed.netloc.lower() != "localhost":
            return None                                        # file://server/share → UNC, blocked
        candidate = Path(url2pathname(unquote(parsed.path)))
    elif scheme == "" or (len(scheme) == 1 and os.name == "nt"):   # relative path, or C:\...
        if url.startswith(("\\\\", "//")):
            return None
        candidate = Path(unquote(url))
        if not candidate.is_absolute():
            candidate = base_dir / candidate
    else:
        return None
    try:
        base = base_dir.resolve()
        target = candidate.resolve()
    except OSError:
        return None
    if os.path.commonpath([str(base), str(target)]) != str(base):
        return None
    return target if target.is_file() else None


def _weasy_fetcher(base_dir: Path | None):
    def fetch(url: str, *args, **kwargs):
        import weasyprint  # type: ignore[import]
        if url.lower().startswith("data:"):
            return weasyprint.default_url_fetcher(url, *args, **kwargs)
        path = local_resource(url, base_dir)
        if path is None:
            raise ValueError(f"blocked resource (only files beside the template may load): {url[:120]}")
        return weasyprint.default_url_fetcher(path.as_uri(), *args, **kwargs)
    return fetch


def _pisa_link_callback(base_dir: Path | None):
    def link(uri: str, rel: str) -> str:
        if uri.lower().startswith("data:"):
            return uri
        path = local_resource(uri, base_dir)
        return str(path) if path else ""
    return link


def render_pdf(
    html: str,
    output_path: Path,
    *,
    base_dir: Path | None = None,
    base_url: str | None = None,
) -> Path:
    """Convert rendered HTML to PDF using WeasyPrint, with xhtml2pdf as a fallback.

    *base_dir* is the only folder resources (images, CSS) may come from; ``base_url`` is accepted for
    compatibility and turned into *base_dir* when it is a ``file:`` URL.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if base_dir is None and base_url and base_url.startswith("file:"):
        base_dir = Path(url2pathname(unquote(urlparse(base_url).path)))

    # Try WeasyPrint first (best quality)
    try:
        import weasyprint  # type: ignore[import]
        wp = weasyprint.HTML(string=html, base_url=(base_dir.resolve().as_uri() + "/") if base_dir else None,
                             url_fetcher=_weasy_fetcher(base_dir))
        wp.write_pdf(str(output_path))
        return output_path
    except (ImportError, OSError):      # OSError: WeasyPrint is installed but the GTK/Pango libraries are missing
        pass

    # Fallback: xhtml2pdf
    try:
        from xhtml2pdf import pisa  # type: ignore[import]
        with open(output_path, "wb") as fh:
            result = pisa.CreatePDF(html, dest=fh, encoding="utf-8", link_callback=_pisa_link_callback(base_dir))
        if result.err:
            raise RuntimeError(f"xhtml2pdf error (code {result.err})")
        return output_path
    except ImportError:
        pass

    raise ImportError(
        "A PDF backend is required.  Install one of:\n"
        "    pip install weasyprint       (recommended – full CSS support)\n"
        "    pip install xhtml2pdf        (simpler; limited CSS; works without GTK)\n"
        "On Windows WeasyPrint also needs the GTK3 runtime ('cannot load library libgobject-2.0-0')."
    )
