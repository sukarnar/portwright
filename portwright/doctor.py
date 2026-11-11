"""``portwright doctor``: which optional parts are installed and working on this machine.

Each check is ok, warn (works, with a limitation), fail (a configured feature cannot work) or off
(an optional add-on that is not installed and nothing here needs it).
"""
from __future__ import annotations

import importlib.util
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class Check:
    area: str
    name: str
    status: str          # ok | warn | fail | off
    detail: str

    def to_dict(self) -> dict:
        return {"area": self.area, "name": self.name, "status": self.status, "detail": self.detail}


def _installed(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def _version(dist: str) -> str:
    try:
        from importlib.metadata import version
        return version(dist)
    except Exception:
        return "?"


def _addon(area: str, name: str, module: str, dist: str, install: str) -> Check:
    if _installed(module):
        return Check(area, name, "ok", f"version {_version(dist)}")
    return Check(area, name, "off", f"not installed ({install})")


def _pdf_reports(cfg: Any) -> list[str]:
    if cfg is None:
        return []
    return [r.name for r in getattr(cfg, "reports", []) if "pdf" in r.effective_formats()]


def pdf_checks(cfg: Any = None) -> list[Check]:
    """The PDF backends, and whether the project's PDF reports can be made with them."""
    from .reports.engine import active_pdf_backend, pdf_backends
    out = []
    dll = os.environ.get("WEASYPRINT_DLL_DIRECTORIES")
    for b in pdf_backends():
        if b.ok:
            out.append(Check("reports", f"PDF: {b.name}", "ok", f"{b.detail}, {b.css}"))
        else:                              # installed but broken is worth a warning; simply absent is not
            out.append(Check("reports", f"PDF: {b.name}", "warn" if b.installed else "off", b.detail))
    if dll:
        missing = [d for d in dll.split(os.pathsep) if d and not Path(d).is_dir()]
        if missing:
            out.append(Check("reports", "WEASYPRINT_DLL_DIRECTORIES", "warn",
                             f"folder not found: {', '.join(missing)}"))
    active = active_pdf_backend()
    needing = _pdf_reports(cfg)
    if active is None:
        status = "fail" if needing else "warn"
        who = f" Reports that need PDF: {', '.join(needing)}." if needing else ""
        out.append(Check("reports", "PDF output", status,
                         f"no PDF backend works, so PDF reports fail.{who} "
                         "Quickest fix: pip install xhtml2pdf (limited CSS); best: WeasyPrint with GTK/Pango"))
    elif active.name != "weasyprint":
        out.append(Check("reports", "PDF output", "warn",
                         f"PDFs use {active.name}: {active.css}. Templates using flex, grid or @page rules "
                         "will look different. Install WeasyPrint's GTK/Pango libraries for full CSS"))
    else:
        out.append(Check("reports", "PDF output", "ok", "PDFs use WeasyPrint"))
    return out


def run_checks(cfg: Any = None) -> list[Check]:
    """Every check. *cfg* (a loaded ProjectConfig) adds checks for what this project uses; None skips them."""
    py = sys.version_info
    checks = [Check("core", "Python", "ok" if py >= (3, 11) else "fail",
                    f"{py.major}.{py.minor}.{py.micro} at {sys.executable}" + ("" if py >= (3, 11) else
                                                                               " (3.11 or newer is required)"))]
    jinja = _addon("reports", "Jinja2 (templates)", "jinja2", "jinja2", "pip install portwright[reports]")
    if jinja.status == "off" and cfg is not None and any(r.template for r in getattr(cfg, "reports", [])):
        jinja.status = "fail"
    checks.append(jinja)
    checks.extend(pdf_checks(cfg))
    mcp = _addon("agents", "MCP", "mcp", "mcp", "pip install portwright[agents]")
    if mcp.status == "off" and cfg is not None and getattr(cfg, "mcp_servers", None):
        mcp.status = "fail"
    checks.append(mcp)
    checks.append(_addon("studio", "Studio (flet)", "flet", "flet", "pip install portwright[gui]"))
    checks.append(_addon("security", "Keyring", "keyring", "keyring", "pip install portwright[keyring]"))
    return checks
