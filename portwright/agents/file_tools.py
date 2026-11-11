"""Filesystem and report tools for agents and MCP servers.

Both run in-process (not through the HTTP pipeline), so they check the runtime's scopes themselves:
``fs:<root>:read`` / ``fs:<root>:write`` / ``fs:dest:<name>`` through the ``Sandbox``, ``reports:run`` for
reading and running reports, ``reports:write`` for saving templates. A filesystem tool only reaches the roots
it was given; a report tool only the reports it was given (all when none are listed).

Report data an agent reads (``report_sample``, ``report_preview``) is shaped by the AI data access policy for
the agent's model, exactly like ``pw report design``.
"""
from __future__ import annotations

import asyncio
import base64
import mimetypes
import re
from pathlib import Path
from typing import Any

from .tools import Tool, ToolError

TABLE_SUFFIXES = {".xlsx", ".xlsm", ".xls"}
_TAGS = re.compile(r"<(script|style)\b.*?</\1>|<[^>]+>", re.DOTALL | re.IGNORECASE)
_OVERWRITE = {"type": "string", "enum": ["never", "always", "rename", "if_newer"],
              "description": "When the target exists. Default: never (fail)."}
_FILES = {"description": "Files: one address/path, or a list of them (e.g. a report run's outputs)",
          "anyOf": [{"type": "string"}, {"type": "array", "items": {}}]}


def _need(rt, scope: str) -> None:
    if "*" not in rt.scopes and scope not in rt.scopes:
        raise ToolError(f"missing scope '{scope}' (add it to the agent's / server's scopes)")


def _sandbox(rt, roots):
    from ..fs import Sandbox
    return Sandbox(rt.cfg, rt.root, scopes=rt.scopes, actor=rt.actor, audit=rt.audit, allowed=roots)


async def _fs(call):
    from ..fs import FsError
    try:
        return await asyncio.to_thread(call)
    except FsError as exc:
        raise ToolError(str(exc)) from None


def _schema(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


def _path_prop(roots: list[str], what: str = "A file or folder") -> dict:
    return {"type": "string", "description": f"{what}, as root:path (roots: {', '.join(roots)}), e.g. "
                                             f"{roots[0]}:sub/folder/file.pdf" if roots else what}


# --------------------------------------------------------------------------- filesystem
def fs_tools(rt, prefix: str, op_roots: dict[str, list[str]], *, blob_mb: float | None = None) -> list[Tool]:
    """One tool per op, named ``<prefix><op>``. ``op_roots`` maps each op to the roots it may use.
    ``blob_mb`` (MCP only): ``read`` returns binary files up to that size as a blob resource."""
    out: list[Tool] = []
    for op, roots in op_roots.items():
        sb = _sandbox(rt, roots)
        where = ", ".join(roots)
        maker = _FS_MAKERS[op]
        out.append(maker(sb, f"{prefix}{op}", roots, where, blob_mb))
    return out


def _fs_list(sb, name, roots, where, _blob_mb):
    async def run(a: dict) -> Any:
        return await _fs(lambda: sb.list(str(a.get("path") or ""), a.get("glob"), bool(a.get("recursive"))))
    return Tool(name, f"List a folder (roots: {where}). Up to 500 entries.",
                _schema({"path": _path_prop(roots, "A folder"), "glob": {"type": "string", "description": "e.g. *.pdf"},
                         "recursive": {"type": "boolean"}}, ["path"]), run, "filesystem")


def _fs_stat(sb, name, roots, where, _blob_mb):
    async def run(a: dict) -> Any:
        return await _fs(lambda: sb.stat(str(a.get("path") or "")))
    return Tool(name, f"Size, modified time and sha256 of a file (roots: {where}).",
                _schema({"path": _path_prop(roots)}, ["path"]), run, "filesystem")


def _fs_read(sb, name, roots, where, blob_mb):
    async def run(a: dict) -> Any:
        path = str(a.get("path") or "")

        def go():
            pl = sb.resolve(path)
            if pl.path.suffix.lower() in TABLE_SUFFIXES and not blob_mb:
                return sb.read_table(path, int(a.get("max_rows") or 50))
            mime = mimetypes.guess_type(pl.path.name)[0] or ""
            if blob_mb and mime and not _texty(mime):           # PDF, Excel, images: by type, not by content
                return _blob(sb, path, blob_mb)
            res = sb.read_text(path, int(a.get("max_chars") or 20_000))
            if res.get("binary") and blob_mb:
                return _blob(sb, path, blob_mb)
            return res
        return await _fs(go)
    extra = " Binary files come back as an embedded resource." if blob_mb else \
        " Excel files come back as rows; other binary files as size and checksum only."
    return Tool(name, f"Read a text file (roots: {where}).{extra}",
                _schema({"path": _path_prop(roots), "max_chars": {"type": "integer", "minimum": 1, "maximum": 200000},
                         "max_rows": {"type": "integer", "minimum": 1, "maximum": 500}}, ["path"]), run, "filesystem")


def _texty(mime: str) -> bool:
    return mime.startswith("text/") or mime in ("application/json", "application/xml", "application/x-yaml")


def _blob(sb, path: str, blob_mb: float) -> dict:
    pl, data = sb.read_bytes(path, int(blob_mb * 1024 * 1024))
    mime = mimetypes.guess_type(pl.path.name)[0] or "application/octet-stream"
    return {"_blob": {"uri": f"portwright://fs/{pl.address}", "mime": mime, "data": base64.b64encode(data).decode()},
            "address": pl.address, "bytes": len(data)}


def _fs_search(sb, name, roots, where, _blob_mb):
    async def run(a: dict) -> Any:
        return await _fs(lambda: sb.search(str(a.get("path") or ""), a.get("glob"), a.get("text"),
                                           int(a.get("max_results") or 50)))
    return Tool(name, f"Find files under a folder by name pattern and/or text they contain (roots: {where}).",
                _schema({"path": _path_prop(roots, "A folder"), "glob": {"type": "string"},
                         "text": {"type": "string", "description": "case-insensitive text to look for"},
                         "max_results": {"type": "integer", "minimum": 1, "maximum": 500}}, ["path"]),
                run, "filesystem")


def _fs_write(sb, name, roots, where, _blob_mb):
    async def run(a: dict) -> Any:
        return await _fs(lambda: sb.write_text(str(a.get("path") or ""), a.get("content", ""),
                                               a.get("overwrite") or "never"))
    return Tool(name, f"Write a text file, atomically (roots: {where}).",
                _schema({"path": _path_prop(roots, "The file to write"), "content": {"type": "string"},
                         "overwrite": _OVERWRITE}, ["path", "content"]), run, "filesystem")


def _fs_mkdir(sb, name, roots, where, _blob_mb):
    async def run(a: dict) -> Any:
        return await _fs(lambda: sb.mkdir(str(a.get("path") or "")))
    return Tool(name, f"Create a folder (roots: {where}).", _schema({"path": _path_prop(roots, "A folder")}, ["path"]),
                run, "filesystem")


def _fs_transfer(mode):
    def make(sb, name, roots, where, _blob_mb):
        async def run(a: dict) -> Any:
            return await _fs(lambda: sb.transfer(a.get("files"), str(a.get("to") or ""), mode=mode,
                                                 overwrite=a.get("overwrite"), manifest=bool(a.get("manifest"))))
        what = ("Copy files into a folder" if mode == "copy" else
                "Move files into a folder (each source is removed after its copy is verified)")
        return Tool(name, f"{what} (roots: {where}). Writes are atomic and sha256-checked.",
                    _schema({"files": _FILES, "to": _path_prop(roots, "The target folder (dest:<name>/sub or root:sub)"),
                             "overwrite": _OVERWRITE, "manifest": {"type": "boolean"}}, ["files", "to"]),
                    run, "filesystem")
    return make


def _fs_delete(sb, name, roots, where, _blob_mb):
    async def run(a: dict) -> Any:
        return await _fs(lambda: sb.delete(a.get("files")))
    return Tool(name, f"Delete files (moved to the trash folder unless configured otherwise; roots: {where}).",
                _schema({"files": _FILES}, ["files"]), run, "filesystem")


def _fs_zip(sb, name, roots, where, _blob_mb):
    async def run(a: dict) -> Any:
        return await _fs(lambda: sb.zip(a.get("files"), str(a.get("to") or ""), a.get("overwrite") or "never"))
    return Tool(name, f"Zip files into one archive (roots: {where}).",
                _schema({"files": _FILES, "to": _path_prop(roots, "The .zip file to write"),
                         "overwrite": _OVERWRITE}, ["files", "to"]), run, "filesystem")


_FS_MAKERS = {"list": _fs_list, "stat": _fs_stat, "read": _fs_read, "search": _fs_search, "write": _fs_write,
              "mkdir": _fs_mkdir, "copy": _fs_transfer("copy"), "move": _fs_transfer("move"),
              "delete": _fs_delete, "zip": _fs_zip}


# --------------------------------------------------------------------------- reports
def _services(rt):
    from ..reports.author import ReportAuthor
    from ..reports.service import ProjectView, ReportService
    view = ProjectView(rt.cfg, rt.root, rt.credentials)
    return ReportService(view), ReportAuthor(view, provider_factory=getattr(rt, "provider_factory", None),
                                             actor=rt.actor)


async def _svc_call(call):
    from ..services.project import ServiceError
    try:
        return await asyncio.to_thread(call)
    except (ServiceError, ValueError, FileNotFoundError) as exc:
        raise ToolError(str(exc)) from None


def _text_snapshot(html: str, cap: int = 3000) -> str:
    text = re.sub(r"\s+", " ", _TAGS.sub(" ", html)).strip()
    return text[:cap] + ("…" if len(text) > cap else "")


def report_tools(rt, prefix: str, reports: list[str], ops: list[str], model: str | None = None) -> list[Tool]:
    """``<prefix><op>`` tools. ``reports`` empty = every report. ``model``: the model whose data access applies
    (and that ``design`` uses); default the agent's model."""
    allowed = list(reports)
    model = model or getattr(rt, "agent_model", None)

    def check(name: str) -> None:
        if allowed and name not in allowed:
            raise ToolError(f"report '{name}' is not available to this tool; available: {', '.join(allowed)}")

    def names() -> list[str]:
        return allowed or [r.name for r in rt.cfg.reports]

    report_prop = {"type": "string", "description": "A report name"}
    if allowed:
        report_prop["enum"] = allowed
    from_prop = {"type": "object", "description": "Where a new report's data comes from: "
                 '{"endpoint": "<GET endpoint>", "params": {}} or {"source": "<sql source>", '
                 '"query": "SELECT ... where x = :x", "params": {"x": 1}}'}
    fmts = {"type": "array", "items": {"type": "string", "enum": ["pdf", "html", "md", "csv", "xlsx", "json"]}}
    out: list[Tool] = []

    if "sources" in ops:
        async def sources(a: dict) -> Any:
            _need(rt, "reports:run")
            reps = [{"name": r.name, "description": r.description, "formats": r.effective_formats(),
                     "from": r.from_.model_dump(exclude_defaults=True), "template": r.template}
                    for r in rt.cfg.reports if r.name in names()]
            eps = [{"name": e.name, "path": e.path, "params": [p.name for p in e.params]}
                   for e in rt.cfg.endpoints if e.method == "GET"]
            sql = [n for n, s in rt.cfg.sources.items() if s.type == "sql" and s.ai_data_access != "none"]
            return {"reports": reps, "endpoints": eps, "sql_sources": sql}
        out.append(Tool(f"{prefix}sources", "Reports you can run, and the endpoints / SQL sources new reports can use",
                        _schema({}, []), sources, "report"))

    if "sample" in ops:
        async def sample(a: dict) -> Any:
            _need(rt, "reports:run")
            if a.get("report"):
                check(a["report"])
            _, author = _services(rt)
            return await _svc_call(lambda: author.sample(a.get("report"), frm=a.get("from"), params=a.get("params"),
                                                         model=model, sample_rows=a.get("sample_rows")))
        out.append(Tool(f"{prefix}sample", "Describe a report's data: columns, types, and (when your data access "
                        "allows) sample rows. Use it before writing a template.",
                        _schema({"report": report_prop, "from": from_prop, "params": {"type": "object"},
                                 "sample_rows": {"type": "integer", "minimum": 0, "maximum": 500}}, []),
                        sample, "report"))

    if "preview" in ops:
        async def preview(a: dict) -> Any:
            _need(rt, "reports:run")
            from ..reports import policy as pol
            from ..reports.author import check_template
            from ..reports.engine import _jinja_env, template_context
            name = a.get("report")
            if name:
                check(name)
            _, author = _services(rt)
            spec = rt.cfg.report(name) if name else None
            tpl = a.get("template")
            if not tpl and spec and spec.template:
                tpl = (rt.root / spec.template).read_text(encoding="utf-8")
            if not tpl:
                raise ToolError("give 'template' (the Jinja2 HTML) and/or a 'report' whose data to use")
            frm = a.get("from") or (spec.from_ if spec else None)
            if frm is None:
                raise ToolError("give a 'report' or 'from' so the template can be rendered with real data")

            def go():
                from ..config import ReportFrom
                f = ReportFrom.model_validate(frm) if isinstance(frm, dict) else frm
                mname, mspec = author._model(model)
                p = pol.resolve(rt.cfg, model_spec=mspec, model_name=mname, source=pol.source_of(rt.cfg, f),
                                report=spec)
                rows = author._sample(name or "preview", f, a.get("params"), 20)
                errors, warnings = check_template(tpl, rows, vars=a.get("vars"),
                                                  formats=(spec.effective_formats() if spec else ["html"]),
                                                  report_name=name or "preview")
                res: dict[str, Any] = {"ok": not errors, "errors": errors, "warnings": warnings,
                                       "data_access": p.level}
                if not errors and p.level in ("sample", "full"):      # the snapshot shows data values
                    html = _jinja_env(rt.root).from_string(tpl).render(
                        **template_context(pol.drop_columns(rows, p.exclude), report_name=name or "preview",
                                           extra_vars=a.get("vars")))
                    res["text"] = _text_snapshot(html)
                return res
            return await _svc_call(go)
        out.append(Tool(f"{prefix}preview", "Check a Jinja2 HTML report template against real data before saving: "
                        "returns errors to fix (scripts, remote files, render errors) and, when allowed, the text "
                        "it renders.",
                        _schema({"template": {"type": "string"}, "report": report_prop, "from": from_prop,
                                 "params": {"type": "object"}, "vars": {"type": "object"}}, []), preview, "report"))

    if "save" in ops:
        async def save(a: dict) -> Any:
            _need(rt, "reports:write")
            name = str(a.get("report") or "")
            check(name)
            _, author = _services(rt)
            res = await _svc_call(lambda: author.save_written(
                name, str(a.get("template") or ""), frm=a.get("from"), formats=a.get("formats"), vars=a.get("vars"),
                description=str(a.get("description") or ""), written_by=rt.actor,
                if_exists="replace" if a.get("overwrite") else "error", data_access="see report_sample"))
            return res.to_dict()
        out.append(Tool(f"{prefix}save", "Save a template you wrote (it is checked first) and create/update the "
                        "report. It goes to .portwright/templates/ until a person promotes it.",
                        _schema({"report": report_prop, "template": {"type": "string"}, "from": from_prop, "formats": fmts,
                                 "vars": {"type": "object"}, "description": {"type": "string"},
                                 "overwrite": {"type": "boolean"}}, ["report", "template"]), save, "report"))

    if "run" in ops:
        async def run(a: dict) -> Any:
            _need(rt, "reports:run")
            name = str(a.get("report") or "")
            check(name)
            svc, _ = _services(rt)
            res = await _svc_call(lambda: svc.run(name, formats=a.get("formats"), params=a.get("params") or {}))
            return res.to_dict()
        out.append(Tool(f"{prefix}run", "Run a report; returns the files written (path, format, bytes, sha256).",
                        _schema({"report": report_prop, "params": {"type": "object"}, "formats": fmts}, ["report"]),
                        run, "report"))

    if "design" in ops:
        async def design(a: dict) -> Any:
            _need(rt, "reports:write")
            name = str(a.get("report") or "")
            check(name)
            _, author = _services(rt)
            from ..services.project import ServiceError
            try:
                res = await author.design(name, str(a.get("prompt") or ""), frm=a.get("from"),
                                          formats=a.get("formats"), model=model, base=a.get("base"),
                                          if_exists="replace" if a.get("overwrite") else "error")
            except (ServiceError, ValueError) as exc:
                raise ToolError(str(exc)) from None
            return res.to_dict()
        out.append(Tool(f"{prefix}design", "Have the report author model write and check a template for a report "
                        "(created when it does not exist; give 'from' then).",
                        _schema({"report": report_prop, "prompt": {"type": "string"}, "from": from_prop,
                                 "formats": fmts, "base": {"type": "string"}, "overwrite": {"type": "boolean"}},
                                ["report", "prompt"]), design, "report"))
    return out


# --------------------------------------------------------------------------- MCP: one tool per report
def report_run_tool(rt, report: str) -> Tool:
    """``run_report_<name>`` with the report's own parameters (endpoint params, or the query's :binds)."""
    spec = rt.cfg.report(report)
    props: dict[str, Any] = {}
    if spec.from_.endpoint:
        ep = rt.cfg.endpoint(spec.from_.endpoint)
        oas = {"string": "string", "integer": "integer", "number": "number", "boolean": "boolean"}
        for p in ep.params:
            props[p.name] = {"type": oas.get(p.type, "string"), **({"description": p.description} if p.description else {})}
    binds = set()
    for _, frm in spec.all_froms():                   # the master's and every dataset's query binds
        if frm.query:
            binds |= set(re.findall(r"(?<![:\w]):([A-Za-z_]\w*)", frm.query))
    for b in sorted(binds - set(props)):
        props[b] = {"type": ["string", "number", "null"]}
    props["formats"] = {"type": "array", "items": {"type": "string", "enum": ["pdf", "html", "md", "csv", "xlsx", "json"]}}

    async def run(a: dict) -> Any:
        _need(rt, "reports:run")
        formats = a.pop("formats", None)
        svc, _ = _services(rt)
        res = await _svc_call(lambda: svc.run(report, formats=formats, params=a))
        return res.to_dict()
    desc = spec.description or f"Run the report '{report}'"
    return Tool(f"run_report_{report}", f"{desc} ({', '.join(spec.effective_formats())}); returns the files written.",
                _schema(props, []), run, "report")
