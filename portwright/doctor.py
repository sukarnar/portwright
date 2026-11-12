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


def dll_checks() -> list[Check]:
    """Name the DLLs WeasyPrint cannot find in WEASYPRINT_DLL_DIRECTORIES (Windows), and who needs them."""
    from .reports.dlls import dll_folders, missing
    folders = [f for f in dll_folders() if f.is_dir()]
    if not folders:
        return []
    gone = missing(folders)
    where = "; ".join(str(f) for f in folders)
    if not gone:
        return [Check("reports", "PDF: DLLs", "warn", f"every DLL WeasyPrint needs is in {where}, yet it does not "
                      "load: check they all come from the same MSYS2 install (mingw64, 64-bit)")]
    lines = [f"{dll} (needed by {', '.join(users)})" for dll, users in gone.items()]
    return [Check("reports", "PDF: missing DLLs", "warn",
                  f"{len(gone)} DLL(s) missing from {where}: " + "; ".join(lines)
                  + ". Copy them from MSYS2's mingw64\\bin (or rebuild the folder with pango\\collect-pango.py)")]


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
    weasy = pdf_backends()[0]
    if sys.platform == "win32" and weasy.installed and not weasy.ok:
        out.extend(dll_checks())
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


KNOWN_WEASYPRINT_VARS = ("WEASYPRINT_DLL_DIRECTORIES",)


def _dotenv_values(path: Path) -> dict[str, str]:
    """KEY=VALUE lines of a .env file, read the same way the project loader reads them."""
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def load_project_env(env_path: Path) -> list[Check]:
    """Load the project's .env (as every other command does) and check its PDF settings: a misspelt
    WEASYPRINT_ variable, or a value that a machine-wide setting overrides."""
    import difflib
    from .services.project import load_dotenv
    values = _dotenv_values(env_path)
    out: list[Check] = []
    for key, val in values.items():
        if key.upper().startswith("WEASYPRINT_") and key not in KNOWN_WEASYPRINT_VARS:
            close = difflib.get_close_matches(key.upper(), KNOWN_WEASYPRINT_VARS, n=1, cutoff=0.6)
            out.append(Check("reports", key, "warn", f"in {env_path.name}: WeasyPrint does not read this variable"
                             + (f"; did you mean {close[0]}?" if close else "")))
        if key in KNOWN_WEASYPRINT_VARS and os.environ.get(key) not in (None, val):
            out.append(Check("reports", key, "warn", f"{env_path.name} sets {val}, but the machine-wide value "
                             f"{os.environ[key]} wins (remove one of them)"))
    load_dotenv(env_path)
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
