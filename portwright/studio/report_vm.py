"""View-model helpers for the report / delivery parts of Studio (no UI code; unit-tested).

* node options: the Report / Design report / Copy / Move / Files canvas nodes keep their options in
  ``Node.settings`` (YAML). These helpers read and write single options so the forms never lose a key
  they do not show.
* the destination picker: where Copy / Move can deliver (destinations, writable roots).
* the Design with AI dialog: turning its fields into ``ReportAuthor.design`` arguments, and what the model
  would be allowed to see.
* AI templates for the gallery.
"""
from __future__ import annotations

import re
from typing import Any

import yaml

from ..agents.spec import DATA_ACCESS_ORDER
from ..services import ServiceError
from . import graph_vm as gv

FORMATS = ("pdf", "html", "xlsx", "csv", "json", "md")
OVERWRITE_LABELS = {"": "Destination's default", "never": "Never (fail)", "always": "Replace",
                    "rename": "Keep both (a (1).pdf)", "if_newer": "Replace if newer"}
VERIFY_LABELS = {"sha256": "SHA-256 (default)", "size": "Size only", "none": "Don't check"}
FILE_OPS = {"write": "Write a file", "read": "Read a file", "list": "List a folder", "mkdir": "Create a folder",
            "delete": "Delete (to trash)", "zip": "Zip files"}
FILE_OP_FIELDS = {"write": ("to", "content", "overwrite"), "read": ("path",), "list": ("path", "glob", "recursive"),
                  "mkdir": ("to",), "delete": ("files",), "zip": ("files", "to", "overwrite")}
# option -> its default: setting an option to its default removes it from the YAML
DEFAULTS: dict[str, Any] = {"create_dirs": True, "verify": "sha256", "manifest": False, "recursive": False,
                            "overwrite": None}


# --------------------------------------------------------------------------- node options
def node_opts(n: gv.Node) -> dict:
    v = gv._parse(n.settings)
    return dict(v) if isinstance(v, dict) else {}


def set_opt(n: gv.Node, key: str, value: Any) -> None:
    """Set one option in the node's settings; empty values and defaults are removed."""
    opts = node_opts(n)
    empty = value is None or value == "" or value == [] or value == {}
    if empty or (key in DEFAULTS and value == DEFAULTS[key]):
        opts.pop(key, None)
    else:
        opts[key] = value
    n.settings = yaml.safe_dump(opts, sort_keys=False, allow_unicode=True).strip() if opts else ""


def parse_value(text: str) -> Any:
    """A form field's text as a value: YAML lists/mappings, ${...} references and plain text."""
    t = (text or "").strip()
    if not t:
        return None
    if t.startswith(("[", "{")):
        try:
            return yaml.safe_load(t)
        except yaml.YAMLError:
            return t
    return t


def show_value(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, (list, dict)):
        return yaml.safe_dump(v, default_flow_style=True, sort_keys=False).strip()
    return str(v)


def upstream_reports(g: gv.Graph, node_id: str) -> list[str]:
    """Report nodes that lead straight into this node (for 'files: ${steps.<id>.output.outputs}')."""
    return [src for (src, _port), dst in g.links.items() if dst == node_id
            and g.node(src) is not None and g.node(src).type == "report"]


def suggested_files(g: gv.Graph, node_id: str) -> str:
    ups = upstream_reports(g, node_id)
    return f"${{steps.{ups[0]}.output.outputs}}" if ups else ""


# --------------------------------------------------------------------------- destination picker
def delivery_targets(cfg) -> dict[str, str]:
    """{base address: label} for Copy / Move: destinations first, then writable roots."""
    out: dict[str, str] = {}
    for name, d in sorted(cfg.destinations.items()):
        out[f"dest:{name}"] = f"{name} - destination ({d.path})"
    from ..fs.sandbox import all_roots
    for name, r in all_roots(cfg).items():
        if r.mode in ("write", "read_write"):
            out[f"{name}:"] = f"{name} - folder ({r.path})"
    return out


def split_to(to: str) -> tuple[str, str]:
    """'dest:finance/2026/x' -> ('dest:finance', '2026/x'); 'reports:a/b' -> ('reports:', 'a/b')."""
    t = (to or "").strip()
    m = re.match(r"^dest:([^/\\]*)[/\\]?(.*)$", t)
    if m:
        return f"dest:{m.group(1)}", m.group(2)
    m = re.match(r"^([A-Za-z][\w\-]+):(.*)$", t)
    if m:
        return f"{m.group(1)}:", m.group(2).lstrip("/\\")
    return "", t


def join_to(base: str, sub: str) -> str:
    sub = (sub or "").strip().strip("/\\")
    if not base:
        return sub
    if base.startswith("dest:"):
        return base + (f"/{sub}" if sub else "")
    return base + sub


# --------------------------------------------------------------------------- Design with AI
def access_choices(effective: str) -> dict[str, str]:
    """Data access levels the user may pick: the effective level or lower (never higher)."""
    labels = {"full": "Real rows", "sample": "Real rows, long values cut", "profile": "Column statistics only",
              "schema": "Column names and types only"}
    top = DATA_ACCESS_ORDER.index(effective)
    return {lv: labels[lv] for lv in reversed(DATA_ACCESS_ORDER[1:top + 1])}


def effective_access(svc, model: str | None, report: str | None = None, endpoint: str | None = None,
                     source: str | None = None) -> tuple[str, str]:
    """(level, explanation) for a design run - what the Design with AI dialog shows before anything is sent."""
    from ..agents.models import load_models
    from ..config import ReportFrom
    from ..reports import policy as pol
    cfg = svc.project.config
    models = load_models(cfg, svc.project.root)
    spec = cfg.report(report) if report else None
    frm = spec.from_ if spec else (ReportFrom(endpoint=endpoint) if endpoint else
                                   ReportFrom(source=source) if source else None)
    p = pol.resolve(cfg, model_spec=models.get(model) if model else None, model_name=model or "",
                    source=pol.source_of(cfg, frm), report=spec)
    return p.level, p.explain()


def design_request(values: dict, cfg) -> dict:
    """Arguments for ReportAuthor.design from the dialog's fields (raises ServiceError with a clear message)."""
    name = (values.get("name") or "").strip()
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9_\-]{0,63}$", name):
        raise ServiceError("Report name: letters, digits, - and _, starting with a letter")
    prompt = (values.get("prompt") or "").strip()
    if not prompt:
        raise ServiceError("Say what the report should show")
    existing = cfg.report(name)
    frm = None
    if not existing:
        ep, src = (values.get("endpoint") or "").strip(), (values.get("source") or "").strip()
        query = (values.get("query") or "").strip()
        if bool(ep) == bool(src):
            raise ServiceError("A new report needs its data: pick an endpoint or a source")
        if src and cfg.sources[src].type == "sql" and not query:
            raise ServiceError("A SQL source needs a query (one SELECT)")
        frm = {"endpoint": ep} if ep else {"source": src, **({"query": query} if query else {})}
    fmts = [f for f in values.get("formats") or [] if f in FORMATS]
    if not fmts:
        raise ServiceError("Pick at least one format")
    out: dict[str, Any] = {"name": name, "prompt": prompt, "frm": frm, "formats": fmts,
                           "model": (values.get("model") or None), "base": (values.get("base") or None),
                           "data_access": (values.get("data_access") or None),
                           "if_exists": "replace" if values.get("replace") else "error"}
    rows = str(values.get("sample_rows") or "").strip()
    if rows:
        if not rows.isdigit() or int(rows) > 500:
            raise ServiceError("Sample rows: a whole number up to 500")
        out["sample_rows"] = int(rows)
    return out


# --------------------------------------------------------------------------- gallery
def ai_templates(svc) -> list[dict]:
    """AI templates (report_ai.templates_dir) with the report that uses each and its provenance."""
    cfg, root = svc.project.config, svc.project.root
    folder = root / cfg.report_ai.templates_dir
    if not folder.is_dir():
        return []
    users: dict[str, Any] = {}
    for r in cfg.reports:
        if r.template:
            users[r.template.replace("\\", "/")] = r
    out = []
    for p in sorted(folder.glob("*.html")):
        rel = p.relative_to(root).as_posix()
        spec = users.get(rel)
        prov = (spec.provenance or {}) if spec else {}
        out.append({"file": p.name, "path": p, "rel": rel, "report": spec.name if spec else None,
                    "model": prov.get("model") or prov.get("generated_by") or "", "data_access": prov.get("data_access", ""),
                    "generated_at": prov.get("generated_at", ""), "prompt": prov.get("prompt", ""),
                    "columns_sent": prov.get("columns_sent") or []})
    return out


def is_ai_report(svc, spec) -> bool:
    from ..reports.author import ReportAuthor
    return ReportAuthor(svc.project).is_ai_template(spec)
