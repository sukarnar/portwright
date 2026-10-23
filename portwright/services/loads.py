"""Load jobs: define, map, check, preview, run, history and rejects (shared by CLI and Studio)."""
from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field
from typing import Any

import anyio

from ..config import LoadSpec, ProjectConfig
from .project import Project, ServiceError, _clean

_RUN_ID = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{6}$")


@dataclass
class TargetColumn:
    name: str
    db_type: str                 # as the database reports it, e.g. VARCHAR2(40)
    type: str                    # portwright mapping type that fits it
    nullable: bool = True
    has_default: bool = False    # server default / identity / computed
    primary_key: bool = False
    length: int | None = None

    @property
    def needs_value(self) -> bool:
        return not self.nullable and not self.has_default


@dataclass
class Inspection:
    """What the editor needs to build a mapping: the source header and the target table."""
    header: list[str] = field(default_factory=list)
    sample: list[dict[str, Any]] = field(default_factory=list)
    row_count: int = 0
    targets: list[TargetColumn] = field(default_factory=list)
    source_error: str | None = None
    target_error: str | None = None
    group: dict | None = None        # AD group target: dn, name, member counts


@dataclass
class Check:
    ok: bool
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    row_count: int = 0


@dataclass
class Suggestion:
    columns: list[dict]
    unmatched: list[str]
    missing_required: list[str]


class LoadService:
    def __init__(self, project: Project):
        self.p = project

    @property
    def cfg(self) -> ProjectConfig:
        return self.p.config

    @property
    def dir(self):
        return self.p.root / "logs" / "loads"

    # ------------------------------------------------------------------ read
    def list(self) -> list[dict]:
        last = {}
        for r in self.history(last=10_000):
            last[r["load"]] = r
        out = []
        for l in self.cfg.loads:
            out.append({"name": l.name, "description": l.description,
                        "from_kind": "endpoint" if l.from_.endpoint else "source",
                        "from": l.from_.endpoint or l.from_.source, "to_source": l.to.source,
                        "table": l.to.target, "target_kind": "group" if l.to.group else "table",
                        "mode": l.mode, "keys": list(l.keys), "columns": len(l.columns),
                        "review": sum(1 for c in l.columns if c.review), "last_run": last.get(l.name)})
        return out

    def get(self, name: str) -> LoadSpec:
        spec = self.cfg.load_job(name)
        if spec is None:
            raise ServiceError(f"no load '{name}'")
        return spec

    def as_dict(self, name: str) -> dict:
        return self.get(name).model_dump(mode="json", by_alias=True, exclude_none=True)

    def options(self) -> dict[str, list[str]]:
        """Choices for the editor: what a load can read from and write to."""
        return {
            "endpoints": [e.name for e in self.cfg.endpoints if e.method == "GET"],
            "file_sources": [n for n, s in self.cfg.sources.items() if s.type in ("file", "folder")],
            "folder_sources": [n for n, s in self.cfg.sources.items() if s.type == "folder"],
            "targets": [n for n, s in self.cfg.sources.items() if s.type == "sql"],
            "read_only_targets": [n for n, s in self.cfg.sources.items() if s.type == "sql" and s.read_only],
            "directories": [n for n, s in self.cfg.sources.items() if s.type == "ldap"],
        }

    # ----------------------------------------------------------------- write
    def build(self, data: "dict | LoadSpec | str") -> LoadSpec:
        """Validate a (possibly unsaved) definition against the project."""
        if isinstance(data, LoadSpec):
            return data
        if isinstance(data, str):
            return self.get(data)
        try:
            spec = LoadSpec.model_validate(data)
        except Exception as exc:
            raise ServiceError(_clean(exc)) from None
        if spec.from_.endpoint:
            ep = self.cfg.endpoint(spec.from_.endpoint)
            if ep is None:
                raise ServiceError(f"no endpoint '{spec.from_.endpoint}'")
            if ep.method != "GET":
                raise ServiceError(f"endpoint '{ep.name}' is {ep.method}; loads read GET endpoints only")
        else:
            src = self.cfg.sources.get(spec.from_.source)
            if src is None or src.type not in ("file", "folder"):
                raise ServiceError(f"'{spec.from_.source}' is not a file or folder source")
        tgt = self.cfg.sources.get(spec.to.source)
        if spec.to.group:
            if tgt is None or tgt.type != "ldap":
                raise ServiceError(f"target '{spec.to.source}' is not an Active Directory (ldap) source")
        elif tgt is None or tgt.type != "sql":
            raise ServiceError(f"target '{spec.to.source}' is not a SQL source")
        return spec

    def save(self, data: dict | LoadSpec, original: str | None = None, force: bool = False) -> LoadSpec:
        """Create (original=None) or replace a load. Renaming is allowed via original."""
        spec = self.build(data)
        names = [l.name for l in self.cfg.loads]
        if original is None and spec.name in names:
            raise ServiceError(f"load '{spec.name}' already exists")
        if original is not None and original not in names:
            raise ServiceError(f"no load '{original}'")
        if original is not None and spec.name != original and spec.name in names:
            raise ServiceError(f"load '{spec.name}' already exists")
        if original is None:
            self.cfg.loads.append(spec)
        else:
            self.cfg.loads = [spec if l.name == original else l for l in self.cfg.loads]
        self.p.save(force=force)
        self.p.audit("load_saved", load=spec.name, columns=len(spec.columns), mode=spec.mode)
        return self.get(spec.name)

    def create(self, name: str, *, to: str, table: str, from_endpoint: str | None = None,
               from_source: str | None = None, mode: str = "insert", keys: list[str] | None = None,
               batch_size: int = 1000, max_rejects: int = 0, description: str = "",
               columns: list[dict] | None = None, force: bool = False) -> LoadSpec:
        return self.save({
            "name": name, "description": description, "mode": mode, "keys": keys or [],
            "batch_size": batch_size, "max_rejects": max_rejects, "to": {"source": to, "table": table},
            "from": {"endpoint": from_endpoint} if from_endpoint else {"source": from_source},
            "columns": columns or []}, force=force)

    def remove(self, name: str, force: bool = False) -> None:
        self.get(name)
        self.cfg.loads = [l for l in self.cfg.loads if l.name != name]
        self.p.save(force=force)
        self.p.audit("load_removed", load=name)

    # --------------------------------------------------------------- runtime
    def _runtime(self, spec: LoadSpec, fn):
        """Start the sources the load needs, run ``await fn(runner)``, shut them down."""
        from ..loads import runtime_for, sources_needed

        async def _go():
            async with runtime_for(self.cfg, self.p.root, sources_needed(self.cfg, spec)) as runner:
                return await fn(runner)
        try:
            return anyio.run(_go)
        except ServiceError:
            raise
        except Exception as exc:
            msg = _clean(exc)
            try:
                msg = self.p.credentials().redact(msg)
            except Exception:
                pass
            raise ServiceError(msg) from None

    def inspect(self, data: dict | LoadSpec, sample: int = 5) -> Inspection:
        """Read the source header/sample and reflect the target table in one go."""
        spec = self.build(data)
        from ..loads import column_type_name

        async def _fn(runner):
            out = Inspection()
            try:
                rows = await runner.read_rows(spec)
                out.row_count = len(rows)
                out.sample = rows[:sample]
                for r in rows[:50]:          # files can have ragged rows; collect the union in order
                    out.header += [k for k in r if k not in out.header]
            except Exception as exc:     # noqa: BLE001
                out.source_error = _clean(exc)
            if spec.to.group:
                def _group():
                    d = runner._directory(spec)
                    g = d.resolve_group(spec.to.group)
                    members = d.members(g.dn)
                    kinds: dict[str, int] = {}
                    for m in members:
                        kinds[m.kind] = kinds.get(m.kind, 0) + 1
                    return {"dn": g.dn, "name": g.name, "sam": g.sam, "members": len(members), "kinds": kinds}
                try:
                    out.group = await anyio.to_thread.run_sync(_group)
                except Exception as exc:     # noqa: BLE001
                    out.target_error = _clean(exc)
                return out
            try:
                tbl = await anyio.to_thread.run_sync(runner._target(spec)._table, spec.to.table)
                pk = {c.name for c in tbl.primary_key.columns}
                for c in tbl.columns:
                    generated = (c.server_default is not None or getattr(c, "identity", None) is not None
                                 or c.computed is not None)
                    out.targets.append(TargetColumn(
                        name=c.name, db_type=str(c.type), type=column_type_name(c), nullable=bool(c.nullable),
                        has_default=generated, primary_key=c.name in pk, length=getattr(c.type, "length", None)))
            except Exception as exc:     # noqa: BLE001
                out.target_error = _clean(exc)
            return out
        return self._runtime(spec, _fn)

    def suggest(self, data: dict | LoadSpec, inspection: Inspection | None = None) -> Suggestion:
        """Auto-map source columns to target columns (uncertain matches are flagged review)."""
        from ..loads import auto_map
        spec = self.build(data)
        ins = inspection or self.inspect(spec)
        if ins.source_error:
            raise ServiceError(f"cannot read the source: {ins.source_error}")
        if ins.target_error:
            raise ServiceError(f"cannot read the target table: {ins.target_error}")
        if not ins.header:
            raise ServiceError("the source returned no rows, so its column names are unknown")
        cols, unmatched = auto_map(ins.header, {t.name: t.type for t in ins.targets})
        mapped = {c["target"].lower() for c in cols}
        missing = [t.name for t in ins.targets if t.needs_value and t.name.lower() not in mapped
                   and not (t.primary_key and t.type == "integer"
                            and sum(1 for x in ins.targets if x.primary_key) == 1)]
        return Suggestion(cols, unmatched, missing)

    def validate(self, data: dict | LoadSpec) -> Check:
        spec = self.build(data)

        async def _fn(runner):
            return await runner.validate(spec)
        problems, warnings, rows = self._runtime(spec, _fn)
        return Check(not problems, problems, warnings, len(rows))

    def run(self, data: dict | LoadSpec | str, dry_run: bool = False, preview: int = 0, force: bool = False):
        """Run a load (a saved one by name, or a draft for dry runs). Returns a LoadReport."""
        spec = self.get(data) if isinstance(data, str) else self.build(data)
        if not dry_run and self.cfg.load_job(spec.name) != spec:
            raise ServiceError("save the load before running it")

        async def _fn(runner):
            return await runner.run(spec, dry_run=dry_run, preview=preview, force=force)
        rep = self._runtime(spec, _fn)
        if not dry_run:
            self.p.audit("load_run", load=spec.name, run_id=rep.run_id, status=rep.status)
        return rep

    # ----------------------------------------------------------- history
    def history(self, name: str | None = None, last: int = 20) -> list[dict]:
        path = self.dir / "history.jsonl"
        if not path.exists():
            return []
        runs = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                runs.append(json.loads(line))
            except ValueError:
                continue
        runs = [r for r in runs if not name or r.get("load") == name]
        return runs[-last:]

    def rejects(self, run_id: str, limit: int = 500) -> tuple[list[str], list[dict], int]:
        """Rows rejected by a run: (columns, rows, total). Columns start with record, reason."""
        if not _RUN_ID.match(run_id or ""):
            raise ServiceError(f"invalid run id '{run_id}'")
        path = self.dir / f"{run_id}_rejects.csv"
        if not path.exists():
            raise ServiceError(f"run {run_id} has no rejects file")
        with path.open(newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            cols = list(reader.fieldnames or [])
            rows, total = [], 0
            for r in reader:
                total += 1
                if len(rows) < limit:
                    rows.append(r)
        return cols, rows, total

    # ---------------------------------------------------------- versions
    def _store(self, name: str):
        from ..loads.versions import VersionStore
        self.get(name)
        return VersionStore(self.dir, name)

    def versions(self, name: str) -> list[dict]:
        """Every distinct version of the load's source, newest first, with the runs that read it."""
        store = self._store(name)
        runs: dict[str, list[dict]] = {}
        for r in self.history(name, last=100_000):
            if r.get("source_hash"):
                runs.setdefault(r["source_hash"], []).append(
                    {"run_id": r["run_id"], "status": r["status"], "started": r["started"]})
        out = []
        for v in reversed(store.list()):
            d = v.__dict__.copy()
            d["runs"] = runs.get(v.hash, [])
            d["loaded"] = any(x["status"] in ("success", "partial") for x in d["runs"])
            d["archived"] = store.archived(v)
            out.append(d)
        return out

    def is_folder(self, spec: LoadSpec) -> bool:
        s = self.cfg.sources.get(spec.from_.source) if spec.from_.source else None
        return s is not None and s.type == "folder"

    def folder_files(self, data: dict | LoadSpec | str) -> dict:
        """Every matching file of a folder load with what a run would do with it (writes nothing)."""
        spec = self.get(data) if isinstance(data, str) else self.build(data)
        if not self.is_folder(spec):
            raise ServiceError(f"load '{spec.name}' does not read a folder source")

        async def _fn(runner):
            ledger, plan = await anyio.to_thread.run_sync(runner.folder_plan, spec)
            items = [i.to_dict() for i in plan.items]
            counts: dict[str, int] = {}
            for i in items:
                counts[i["status"]] = counts.get(i["status"], 0) + 1
            return {"files": items, "counts": counts, "ledger": ledger.counts(),
                    "root": str(runner._folder_of(spec).root())}
        return self._runtime(spec, _fn)

    def ledger(self, name: str, last: int = 200, text: str | None = None) -> list[dict]:
        from ..loads.folder import Ledger
        spec = self.get(name)
        if not self.is_folder(spec):
            raise ServiceError(f"load '{name}' does not read a folder source")
        return Ledger(self.dir, name).list(last, text)

    def forget(self, name: str, ref: str | None) -> int:
        """Let a file (path, name or sha256 prefix) – or all with ref None – be loaded again."""
        from ..loads.folder import Ledger
        spec = self.get(name)
        if not self.is_folder(spec):
            raise ServiceError(f"load '{name}' does not read a folder source")
        n = Ledger(self.dir, name).forget(ref)
        self.p.audit("load_files_forgotten", load=name, ref=ref or "*", entries=n)
        return n

    def source_status(self, data: dict | LoadSpec | str) -> dict:
        """Read the source now and say whether it changed since the last successful load (writes nothing)."""
        spec = self.get(data) if isinstance(data, str) else self.build(data)
        if self.is_folder(spec):
            st = self.folder_files(spec)
            c = st["counts"]
            todo = c.get("new", 0) + c.get("changed", 0)
            return {"hash": f"{todo} file(s) to load", "rows": len(st["files"]), "file": st["root"],
                    "modified": "", "size": None, "version": None, "changed": todo > 0 if st["files"] else None,
                    "last_loaded_version": None, "last_loaded_run": None, "last_loaded_at": None,
                    "header_change": {}, "files": st["files"], "counts": c}

        async def _fn(runner):
            rows = await runner.read_rows(spec)
            fp = await anyio.to_thread.run_sync(runner.fingerprint, spec, rows)
            last = runner.last_loaded(spec.name)
            store = runner.versions(spec.name)
            known = store.find(fp.hash)
            last_v = store.find(last["source_hash"]) if last else None
            from ..loads.versions import header_change
            return {"hash": fp.hash, "rows": fp.rows, "file": fp.file, "modified": fp.modified, "size": fp.size,
                    "version": known.version if known else None,
                    "changed": None if last is None else last["source_hash"] != fp.hash,
                    "last_loaded_version": last.get("source_version") if last else None,
                    "last_loaded_run": last.get("run_id") if last else None,
                    "last_loaded_at": last.get("started") if last else None,
                    "header_change": header_change(last_v.header, fp.header) if last_v else {}}
        return self._runtime(spec, _fn)

    def diff(self, name: str, old: int, new: int, sample: int = 20) -> dict:
        """Compare two archived versions (by the load key when it is mapped from source columns)."""
        from ..loads.versions import diff_rows
        spec = self.get(name)
        store = self._store(name)
        try:
            a, b = store.read(old), store.read(new)
        except (KeyError, FileNotFoundError) as exc:
            raise ServiceError(str(exc).strip("'\"")) from None
        by_target = {c.target.lower(): c for c in spec.columns}
        key_src = [by_target[k.lower()].source for k in spec.keys
                   if k.lower() in by_target and by_target[k.lower()].source is not None]
        if len(key_src) != len(spec.keys):
            key_src = []
        out = diff_rows(a, b, key_src, sample)
        out.update(old=old, new=new)
        return out
