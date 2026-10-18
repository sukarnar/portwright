"""LoadRunner (Template Method): lock -> extract -> validate -> map -> load -> record."""
from __future__ import annotations

import csv
import json
import logging
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, time as dtime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import anyio
from sqlalchemy import (Boolean, Date, DateTime, Float, Integer, Numeric, String, and_, func, or_, select,
                        inspect as sa_inspect)
from sqlalchemy.exc import SQLAlchemyError

from ..config import ColumnMap, LoadSpec, ProjectConfig
from ..sources.base import DataSource, ExecutionContext, SourceError
from .mapping import RowError, RowMapper
from .versions import Fingerprint, VersionStore, file_sha256, header_change, header_of, rows_hash
from .writers import make_writer

log = logging.getLogger("portwright.loads")
LOCK_STALE_SECONDS = 6 * 3600


class LoadError(Exception):
    pass


@dataclass
class LoadReport:
    run_id: str
    load: str
    mode: str
    status: str = "running"            # success | partial | failed | dry_run | skipped
    read: int = 0
    mapped: int = 0
    rejected: int = 0
    dropped: int = 0                   # duplicate keys removed by duplicate_keys: first/last
    skipped: int = 0                   # rows whose key already exists (existing_keys: skip)
    inserted: int = 0
    updated: int = 0
    upserted: int = 0
    deleted: int = 0
    started: str = ""
    finished: str = ""
    seconds: float = 0.0
    rejects_file: str | None = None
    error: str | None = None
    keys: list[str] = field(default_factory=list)          # load key used for the checks
    table_rows: int | None = None                           # rows in the table before (kill & fill)
    source_hash: str | None = None
    source_version: int | None = None                       # None in dry runs of new data
    source_changed: bool | None = None                      # vs the last successful load (None = first)
    last_loaded_version: int | None = None
    last_loaded_run: str | None = None
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    preview: list[dict] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        """0 ok · 1 failed (rolled back) · 2 loaded with rejects · 3 skipped (source unchanged)"""
        if self.status == "failed":
            return 1
        if self.status == "skipped":
            return 3
        return 2 if self.rejected else 0

    def to_dict(self) -> dict:
        return asdict(self)


def column_type_name(col) -> str:
    """portwright mapping type that fits a reflected SQLAlchemy column."""
    t = col.type
    if isinstance(t, Boolean):
        return "boolean"
    if isinstance(t, Integer):
        return "integer"
    if isinstance(t, (Numeric, Float)):
        # Oracle NUMBER(p,0) reflects as Numeric with scale 0
        return "integer" if getattr(t, "scale", None) == 0 else "number"
    if isinstance(t, DateTime):
        return "timestamp"
    if isinstance(t, Date):
        return "date"
    return "string"


class LoadRunner:
    def __init__(self, config: ProjectConfig, sources: dict[str, DataSource], project_root: str | Path,
                 audit=None):
        self.config = config
        self.sources = sources
        self.root = Path(project_root)
        self.audit = audit
        self.dir = self.root / "logs" / "loads"

    # ---------------------------------------------------------------- extract
    async def read_rows(self, spec: LoadSpec) -> list[dict[str, Any]]:
        f = spec.from_
        if f.source:
            src = self.sources[f.source]
            if src.type_name != "file":
                raise LoadError(f"'from.source' must be a file source; use 'from.endpoint' for '{src.type_name}'")
            return list(await anyio.to_thread.run_sync(src._load))

        ep = self.config.endpoint(f.endpoint)
        if ep.method != "GET":
            raise LoadError(f"endpoint '{ep.name}' is {ep.method}; loads read GET endpoints only")
        from ..builder import build_param_model
        unknown = set(f.params) - {p.name for p in ep.params}
        if unknown:
            raise LoadError(f"endpoint '{ep.name}' has no params {sorted(unknown)}")
        try:
            params = build_param_model(ep).model_validate(dict(f.params)).model_dump()
        except Exception as exc:
            raise LoadError(f"invalid params for endpoint '{ep.name}': {exc}") from None
        src = self.sources[ep.source]
        rows: list[dict[str, Any]] = []
        offset, limit = 0, ep.max_limit
        while True:
            data = await src.execute(ExecutionContext(ep, params, None, limit, offset, "load"))
            if isinstance(data, dict) and isinstance(data.get("items"), list):
                rows.extend(data["items"])
                if len(data["items"]) < limit:
                    break
                offset += limit
            elif isinstance(data, list):
                rows.extend(data)
                break
            elif isinstance(data, dict):
                rows.append(data)
                break
            else:
                raise LoadError(f"endpoint '{ep.name}' did not return rows")
        return rows

    # --------------------------------------------------------------- validate
    def _target(self, spec: LoadSpec):
        src = self.sources.get(spec.to.source)
        if src is None or src.type_name != "sql":
            raise LoadError(f"target '{spec.to.source}' is not a SQL source")
        return src

    def inspect_target(self, spec: LoadSpec) -> tuple[Any, dict[str, str], list[str], list[str], list[ColumnMap]]:
        """Return (table, mapping-target -> real column name, problems, warnings, effective columns)."""
        problems: list[str] = []
        warnings: list[str] = []
        src = self._target(spec)
        if src.spec.read_only:
            problems.append(f"target source '{spec.to.source}' is read-only; add it with --read-write "
                            f"or set read_only: false")
        try:
            table = src._table(spec.to.table)
        except SourceError as exc:
            raise LoadError(str(exc)) from None
        real = {c.name.lower(): c.name for c in table.columns}
        names: dict[str, str] = {}
        effective: list[ColumnMap] = []
        for cm in spec.columns:
            col_name = real.get(cm.target.lower())
            if col_name is None:
                problems.append(f"target column '{cm.target}' does not exist in {spec.to.table}")
                continue
            names[cm.target] = col_name
            col = table.c[col_name]
            if cm.review:
                problems.append(f"auto-mapped '{cm.source}' -> '{cm.target}' needs review "
                                f"(confirm with: portwright load map {spec.name} --set \"{cm.source}={cm.target}\")")
            # type compatibility
            want = column_type_name(col)
            if cm.value is None and cm.type == "string" and want in ("integer", "number", "date", "timestamp"):
                warnings.append(f"'{cm.target}' is {col.type} but mapped as string; set type: {want}")
            if cm.type == "number" and want == "integer":
                warnings.append(f"'{cm.target}' is an integer column; decimals would be rejected by the DB")
            # length: enforce the column's limit so overflows become clean rejects
            length = getattr(col.type, "length", None)
            eff = cm
            if isinstance(col.type, String) and length and cm.type == "string":
                if cm.max_length is None or cm.max_length > length:
                    eff = cm.model_copy(update={"max_length": length})
            effective.append(eff)
        mapped = {n.lower() for n in names.values()}
        pk = {c.name for c in table.primary_key.columns}
        for col in table.columns:
            if col.name.lower() in mapped or col.nullable or col.server_default is not None:
                continue
            if getattr(col, "identity", None) is not None or col.computed is not None:
                continue
            if col.name in pk and len(pk) == 1 and isinstance(col.type, Integer):
                warnings.append(f"'{col.name}' (primary key) is not mapped; assuming the database generates it")
                continue
            problems.append(f"required column '{col.name}' (NOT NULL, no default) is not mapped")
        if spec.mode == "upsert":
            keys = {names.get(k, k).lower() for k in spec.keys}
            uniques = [{c.lower() for c in pk}] if pk else []
            try:
                insp = sa_inspect(src.engine)
                schema, _, tbl = spec.to.table.rpartition(".")
                for uc in insp.get_unique_constraints(tbl, schema=schema or src._default_schema):
                    uniques.append({c.lower() for c in uc["column_names"]})
                for ix in insp.get_indexes(tbl, schema=schema or src._default_schema):
                    if ix.get("unique"):
                        uniques.append({c.lower() for c in ix["column_names"] if c})
            except Exception:  # pragma: no cover - some dialects lack these
                pass
            if keys not in uniques:
                msg = (f"upsert keys {sorted(spec.keys)} are not a primary key or unique constraint "
                       f"on {spec.to.table}")
                if src.engine.dialect.name in ("sqlite", "postgresql", "mysql", "mariadb"):
                    problems.append(msg + " (required by this database for upsert)")
                else:
                    dup = self._table_duplicate(src, table, [names.get(k, k) for k in spec.keys])
                    if dup:
                        problems.append(f"{spec.to.table} already has several rows with the same key "
                                        f"({dup}); upsert would update all of them – clean the table or "
                                        f"choose other key columns")
                    else:
                        warnings.append(msg + "; checked: the table has no duplicate keys today")
        return table, names, problems, warnings, effective

    @staticmethod
    def check_source_columns(spec: LoadSpec, rows: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
        problems, warnings = [], []
        if not rows:
            warnings.append("source returned no rows")
            return problems, warnings
        header = list(rows[0].keys())
        used = {c.source for c in spec.columns if c.source is not None}
        missing = sorted(used - set(header))
        if missing:
            problems.append(f"source columns not found: {missing}; available: {header}")
        extra = [h for h in header if h not in used]
        if extra:
            (problems if spec.unmapped_source_columns == "error" else warnings).append(
                f"unmapped source columns {'not allowed' if spec.unmapped_source_columns == 'error' else 'ignored'}: {extra}")
        return problems, warnings

    async def validate(self, spec: LoadSpec) -> tuple[list[str], list[str], list[dict]]:
        rows = await self.read_rows(spec)
        _, _, p1, w1, _ = await anyio.to_thread.run_sync(self.inspect_target, spec)
        p2, w2 = self.check_source_columns(spec, rows)
        return p1 + p2, w1 + w2, rows

    # -------------------------------------------------------------------- run
    def _lock(self, name: str) -> Path:
        self.dir.mkdir(parents=True, exist_ok=True)
        lock = self.dir / f"{name}.lock"
        if lock.exists() and time.time() - lock.stat().st_mtime > LOCK_STALE_SECONDS:
            lock.unlink(missing_ok=True)
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise LoadError(f"load '{name}' is already running (lock {lock}); "
                            f"delete the lock file if no run is active") from None
        os.write(fd, f"{os.getpid()} {datetime.now(timezone.utc).isoformat()}".encode())
        os.close(fd)
        return lock

    def _write_rejects(self, report: LoadReport, rejects: list[tuple[int, str, dict]]) -> None:
        if not rejects:
            return
        path = self.dir / f"{report.run_id}_rejects.csv"
        cols: list[str] = []
        for _, _, row in rejects:
            cols += [k for k in row if k not in cols]
        with path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["record", "reason", *cols])
            for n, reason, row in rejects:
                w.writerow([n, reason, *[row.get(c, "") for c in cols]])
        report.rejects_file = str(path)

    def _record(self, report: LoadReport) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        entry = {k: v for k, v in report.to_dict().items() if k != "preview"}
        with (self.dir / "history.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, default=str) + "\n")
        if self.audit:
            self.audit.record(event="load", load=report.load, run_id=report.run_id, status=report.status,
                              read=report.read, rejected=report.rejected,
                              written=report.inserted + report.updated + report.upserted)

    # ------------------------------------------------------------ key checks
    @staticmethod
    def _table_duplicate(src, table, cols: list[str]) -> str | None:
        """One key value that occurs more than once in the table, as text, or None."""
        try:
            c = [table.c[n] for n in cols]
            q = select(*c, func.count().label("n")).group_by(*c).having(func.count() > 1).limit(1)
            with src.engine.connect() as conn:
                row = conn.execute(q).first()
        except Exception as exc:  # noqa: BLE001 – a check must not hide the real problem
            log.debug("duplicate-key check failed: %s", exc)
            return None
        if row is None:
            return None
        return ", ".join(f"{n}={v}" for n, v in zip(cols, row[:-1])) + f" appears {row[-1]} times"

    @staticmethod
    def _existing_keys(src, table, cols: list[str], values: list[tuple]) -> set[tuple]:
        """Which of ``values`` (key tuples, real column order) are already in the table."""
        found: set[tuple] = set()
        c = [table.c[n] for n in cols]
        uniq = list(dict.fromkeys(values))
        step = 500 if len(c) == 1 else 100
        with src.engine.connect() as conn:
            for b in range(0, len(uniq), step):
                chunk = uniq[b:b + step]
                if len(c) == 1:
                    cond = c[0].in_([v[0] for v in chunk])
                else:
                    cond = or_(*[and_(*[col == v for col, v in zip(c, vals)]) for vals in chunk])
                for row in conn.execute(select(*c).where(cond)):
                    found.add(tuple(_kn(x) for x in row))
        return found

    def _table_count(self, spec: LoadSpec, table) -> int:
        with self._target(spec).engine.connect() as conn:
            return int(conn.execute(select(func.count()).select_from(table)).scalar() or 0)

    # -------------------------------------------------------- source checks
    def _file_of(self, spec: LoadSpec):
        """The file source behind a load (direct, or behind its endpoint), or None."""
        name = spec.from_.source or self.config.endpoint(spec.from_.endpoint).source
        src = self.sources.get(name)
        return src if src is not None and src.type_name == "file" else None

    async def _check_file(self, spec: LoadSpec, report: LoadReport, wait: bool) -> None:
        src = self._file_of(spec)
        if src is None:
            return
        t = spec.tracking
        try:
            path = src._resolved_path()
            st = path.stat()
        except (OSError, SourceError):
            return                                  # read_rows reports a missing file clearly
        if t.max_age_hours:
            age_h = (time.time() - st.st_mtime) / 3600
            if age_h > t.max_age_hours:
                raise LoadError(f"source file {path.name} was last changed {age_h:.1f} hours ago "
                                f"(max_age_hours = {t.max_age_hours}); is today's file missing?")
        if t.stable_seconds:
            deadline = time.time() + max(60, 3 * t.stable_seconds)
            while True:
                st = path.stat()
                young = time.time() - st.st_mtime
                if young >= t.stable_seconds:
                    break
                if not wait:
                    report.warnings.append(f"{path.name} changed {young:.0f}s ago; a real run waits until it "
                                           f"is {t.stable_seconds}s unchanged")
                    break
                if time.time() > deadline:
                    raise LoadError(f"{path.name} is still changing (being copied?); gave up waiting")
                await anyio.sleep(min(5, t.stable_seconds - young + 0.5))

    def fingerprint(self, spec: LoadSpec, rows: list[dict]) -> Fingerprint:
        fp = Fingerprint(hash=rows_hash(rows), rows=len(rows), header=header_of(rows),
                         kind="file" if spec.from_.source else "endpoint")
        src = self._file_of(spec)
        if src is not None:
            try:
                path = src._resolved_path()
                st = path.stat()
                fp.file, fp.size = path.name, st.st_size
                fp.modified = datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(timespec="seconds")
                fp.file_sha256 = file_sha256(path)
            except (OSError, SourceError):
                pass
        return fp

    def versions(self, name: str) -> VersionStore:
        return VersionStore(self.dir, name)

    def last_loaded(self, name: str) -> dict | None:
        """The latest run of this load that wrote data (success or partial)."""
        path = self.dir / "history.jsonl"
        if not path.exists():
            return None
        last = None
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("load") == name and r.get("status") in ("success", "partial") and r.get("source_hash"):
                last = r
        return last

    # -------------------------------------------------------------------- run
    async def run(self, spec: LoadSpec, dry_run: bool = False, preview: int = 0, force: bool = False) -> LoadReport:
        run_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        report = LoadReport(run_id=run_id, load=spec.name, mode=spec.mode,
                            started=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        t0 = time.perf_counter()
        lock = None
        record = not dry_run
        try:
            if not dry_run:
                lock = self._lock(spec.name)
            # 1. extract (a file must be complete and recent enough)
            await self._check_file(spec, report, wait=not dry_run)
            rows = await self.read_rows(spec)
            report.read = len(rows)

            # 2. version the source and compare with the last successful load
            fp = await anyio.to_thread.run_sync(self.fingerprint, spec, rows)
            store = self.versions(spec.name)
            report.source_hash = fp.hash
            last = self.last_loaded(spec.name)
            last_v = store.find(last["source_hash"]) if last else None
            if last:
                report.source_changed = last["source_hash"] != fp.hash
                report.last_loaded_version = last.get("source_version")
                report.last_loaded_run = last.get("run_id")
            known = store.find(fp.hash)
            if not dry_run:
                known = await anyio.to_thread.run_sync(store.add, fp, rows, run_id, spec.tracking.archive_keep)
            report.source_version = known.version if known else None
            if last_v is not None:
                hc = header_change(last_v.header, fp.header)
                if hc:
                    msg = (f"source columns changed since v{last_v.version} (last loaded): "
                           + "; ".join(f"{k}: {', '.join(v)}" for k, v in hc.items()))
                    if spec.tracking.on_header_change == "fail" and not force:
                        raise LoadError(msg + " – check the mapping, then run with --force (Studio: "
                                              "Run anyway) or set on_header_change: warn")
                    report.warnings.append(msg)
            if report.source_changed is False:
                where = f"v{report.last_loaded_version} (run {report.last_loaded_run})"
                if dry_run:
                    report.warnings.append(f"source unchanged since the last load {where}"
                                           + ("; a real run would skip it" if spec.tracking.on_unchanged == "skip"
                                              else ""))
                elif not force and spec.tracking.on_unchanged == "skip":
                    report.status = "skipped"
                    report.error = f"source unchanged since the last load {where}; nothing to do"
                    return report
                elif not force and spec.tracking.on_unchanged == "fail":
                    raise LoadError(f"source unchanged since the last load {where}")
                else:
                    report.warnings.append(f"source unchanged since the last load {where}; loading anyway")

            # 3. validate mapping against source header and target table
            table, names, p1, w1, effective = await anyio.to_thread.run_sync(self.inspect_target, spec)
            p2, w2 = self.check_source_columns(spec, rows)
            report.problems, report.warnings = p1 + p2, report.warnings + w1 + w2
            if report.problems:
                raise LoadError("mapping is not valid:\n  - " + "\n  - ".join(report.problems))

            # 4. map every row first – nothing is written if there are too many rejects
            mapper = RowMapper(spec.model_copy(update={"columns": effective}), run_id)
            mapped_rows: list[tuple[int, dict, dict]] = []
            rejects: list[tuple[int, str, dict]] = []
            for i, row in enumerate(rows, start=1):
                try:
                    mapped_rows.append((i, row, mapper.map_row(row)))
                except RowError as exc:
                    rejects.append((i, str(exc), row))

            # 5. load key checks: empty, duplicated in the source, already in the table
            key_targets = self._key_targets(spec, table, names, report)
            if key_targets:
                mapped_rows = await self._check_keys(spec, table, names, key_targets, mapped_rows, rejects, report)
            rejects.sort(key=lambda r: r[0])
            for i, row, mapped in mapped_rows:
                if len(report.preview) >= preview:
                    break
                report.preview.append({"record": i, "source": row, "target": mapped})
            good = [{names[k]: v for k, v in m.items()} for _, _, m in mapped_rows]
            report.mapped, report.rejected = len(good), len(rejects)
            self.dir.mkdir(parents=True, exist_ok=True)
            self._write_rejects(report, rejects)
            if report.rejected > spec.max_rejects:
                raise LoadError(f"{report.rejected} row(s) rejected (max_rejects = {spec.max_rejects}); "
                                f"nothing was written. See {report.rejects_file}")

            # 6. kill & fill safety: never empty the table for a short or empty file
            if spec.mode == "replace":
                report.table_rows = await anyio.to_thread.run_sync(self._table_count, spec, table)
                sf = spec.safety
                if len(good) < sf.min_rows:
                    raise LoadError(f"kill & fill refused: {len(good)} row(s) to load, safety.min_rows is "
                                    f"{sf.min_rows}; the table ({report.table_rows} rows) was not touched")
                if sf.min_percent and report.table_rows and len(good) * 100 < report.table_rows * sf.min_percent:
                    pct = len(good) * 100 / report.table_rows
                    raise LoadError(f"kill & fill refused: {len(good)} row(s) would replace {report.table_rows} "
                                    f"({pct:.0f}%, safety.min_percent is {sf.min_percent:g}%); "
                                    f"the table was not touched")
            if dry_run:
                report.status = "dry_run"
                return report

            # 7. load – one transaction for the whole run
            keys = [names[k] if k in names else k for k in spec.keys]
            target = self._target(spec)

            def _write() -> dict:
                target._refresh_if_rotated()
                with target.engine.begin() as conn:
                    writer = make_writer(spec.mode, conn, table, keys)
                    writer.start()
                    for b in range(0, len(good), spec.batch_size):
                        writer.write(good[b:b + spec.batch_size])
                    return writer.counts

            try:
                counts = await anyio.to_thread.run_sync(_write)
            except SQLAlchemyError as exc:
                orig = getattr(exc, "orig", None) or exc
                first = str(orig).strip().splitlines()[0][:300] if str(orig).strip() else type(orig).__name__
                # one line at error level; the traceback (which contains row values) only at debug level
                log.error("load %s run %s failed: %s", spec.name, run_id, first)
                log.debug("load %s run %s traceback", spec.name, run_id, exc_info=exc)
                raise LoadError(f"database error, all changes rolled back: {first}") from None
            for k, v in counts.items():
                setattr(report, k, v)
            report.status = "partial" if report.rejected else "success"
            return report
        except (LoadError, SourceError) as exc:
            report.status, report.error = "failed", str(exc)
            return report
        finally:
            report.finished = datetime.now(timezone.utc).isoformat(timespec="seconds")
            report.seconds = round(time.perf_counter() - t0, 2)
            if lock:
                lock.unlink(missing_ok=True)
            if record:
                self._record(report)

    def _key_targets(self, spec: LoadSpec, table, names: dict[str, str], report: LoadReport) -> list[str]:
        """Mapping target names of the load key: spec.keys, or else the table's primary key if mapped."""
        by_lower = {t.lower(): t for t in names}
        if spec.keys:
            out = [by_lower[k.lower()] for k in spec.keys if k.lower() in by_lower]
        else:
            pk = [c.name for c in table.primary_key.columns]
            real_to_target = {v.lower(): k for k, v in names.items()}
            out = [real_to_target[c.lower()] for c in pk if c.lower() in real_to_target]
            if not pk or len(out) != len(pk):
                return []
            report.warnings.append(f"no load key set; using the table's primary key ({', '.join(pk)}) "
                                   f"for duplicate checks")
        report.keys = out
        return out

    async def _check_keys(self, spec, table, names, key_targets, mapped_rows, rejects, report):
        def label(m):
            return ", ".join(f"{k}={m.get(k)}" for k in key_targets)

        def kv(m):
            return tuple(_kn(m[k]) for k in key_targets)

        with_key = []
        for i, row, m in mapped_rows:
            if any(m.get(k) is None for k in key_targets):
                rejects.append((i, f"key column empty ({label(m)})", row))
            else:
                with_key.append((i, row, m))
        groups: dict[tuple, list] = {}
        for item in with_key:
            groups.setdefault(kv(item[2]), []).append(item)
        kept: list[tuple[int, dict, dict]] = []
        for items in groups.values():
            if len(items) == 1:
                kept.append(items[0])
            elif spec.duplicate_keys == "reject":   # we cannot know which copy is right: reject all
                recs = ", ".join(str(i) for i, _, _ in items)
                for i, row, m in items:
                    rejects.append((i, f"duplicate key ({label(m)}) in source records {recs}", row))
            else:
                kept.append(items[0] if spec.duplicate_keys == "first" else items[-1])
                report.dropped += len(items) - 1
        kept.sort(key=lambda t: t[0])
        if spec.mode == "insert" and kept:
            cols = [names[k] for k in key_targets]
            values = [tuple(m[k] for k in key_targets) for _, _, m in kept]
            try:
                found = await anyio.to_thread.run_sync(self._existing_keys, self._target(spec), table, cols, values)
            except SQLAlchemyError as exc:
                raise LoadError(f"could not check existing keys in {spec.to.table}: "
                                f"{str(getattr(exc, 'orig', exc)).splitlines()[0][:200]}") from None
            if found:
                if spec.existing_keys == "fail":
                    raise LoadError(f"{len(found)} key(s) already exist in {spec.to.table} "
                                    f"(existing_keys: fail); nothing was written")
                still = []
                for i, row, m in kept:
                    if kv(m) in found:
                        if spec.existing_keys == "skip":
                            report.skipped += 1
                        else:
                            rejects.append((i, f"key ({label(m)}) already exists in {spec.to.table}", row))
                    else:
                        still.append((i, row, m))
                kept = still
        return kept


def _kn(v: Any) -> Any:
    """Normalise a key value so source and database values compare equal (Oracle DATE, NUMBER, CHAR)."""
    if isinstance(v, datetime) and v.tzinfo is None and v.time() == dtime(0):
        return v.date()
    if isinstance(v, Decimal) and v == v.to_integral_value():
        return int(v)
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, str):
        return v.rstrip()
    return v
