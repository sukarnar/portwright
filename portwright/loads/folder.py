"""Folder loads: every file in a folder source -> rows in a table, one transaction per file.

* document mode – one row per file: file_name, file_path, size, modified, sha256, mime_type, and the
  content as bytes (map to BLOB / VARBINARY(MAX) / BYTEA) or as text (map to CLOB / NVARCHAR(MAX) / TEXT).
* records mode – each CSV / Excel / JSON file is parsed; every row also gets the file_* columns.

The ledger (logs/loads/<load>.files.db, SQLite) remembers every file loaded – path, size, time, SHA-256 –
so a file is never loaded twice, even if it is copied back or renamed. Optionally the target table is
checked too (a column mapped from sha256). A file with a known name but new content is "changed":
loaded as another version, or replacing the rows loaded from that file (files.on_changed).
"""
from __future__ import annotations

import shutil
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..sources.files import decode_text, parse_file
from ..sources.folder import FILE_COLUMNS, FileInfo, sha256_of, _is_absolute


# ============================================================ ledger
class Ledger:
    """Files a load has seen. Statuses: loaded, in_target (found in the table), failed."""

    def __init__(self, directory: Path, load: str):
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / f"{load}.files.db"
        with closing(self._db()) as db, db:
            db.execute("""create table if not exists files (
                id integer primary key, path text not null, file_name text, sha256 text not null,
                size integer, modified text, status text not null, action text, rows integer,
                run_id text, at text not null, moved_to text, reason text)""")
            db.execute("create index if not exists files_sha on files(sha256, status)")
            db.execute("create index if not exists files_path on files(path, status)")

    def _db(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    def by_hash(self, sha: str) -> dict | None:
        with closing(self._db()) as db:
            r = db.execute("select * from files where sha256 = ? and status in ('loaded', 'in_target') "
                           "order by id desc limit 1", (sha,)).fetchone()
        return dict(r) if r else None

    def by_path(self, rel: str) -> dict | None:
        with closing(self._db()) as db:
            r = db.execute("select * from files where lower(path) = lower(?) and status in ('loaded', 'in_target') "
                           "order by id desc limit 1", (rel,)).fetchone()
        return dict(r) if r else None

    def add(self, f: FileInfo, sha: str, status: str, *, action: str | None = None, rows: int = 0,
            run_id: str | None = None, moved_to: str | None = None, reason: str | None = None) -> None:
        with closing(self._db()) as db, db:
            db.execute("insert into files (path, file_name, sha256, size, modified, status, action, rows, run_id, "
                       "at, moved_to, reason) values (?,?,?,?,?,?,?,?,?,?,?,?)",
                       (f.rel, f.name, sha, f.size,
                        datetime.fromtimestamp(f.modified, timezone.utc).isoformat(timespec="seconds"),
                        status, action, rows, run_id, datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        moved_to, reason))

    def set_moved(self, sha: str, run_id: str, moved_to: str) -> None:
        with closing(self._db()) as db, db:
            db.execute("update files set moved_to = ? where sha256 = ? and run_id = ?", (moved_to, sha, run_id))

    def list(self, last: int = 200, text: str | None = None) -> list[dict]:
        q, args = "select * from files", []
        if text:
            q += " where lower(path) like ? or sha256 like ?"
            args = [f"%{text.lower()}%", f"{text.lower()}%"]
        q += " order by id desc limit ?"
        args.append(last)
        with closing(self._db()) as db:
            return [dict(r) for r in db.execute(q, args).fetchall()]

    def forget(self, ref: str | None) -> int:
        """Remove ledger entries for a file (path, name or sha256 prefix), or all with ref None."""
        with closing(self._db()) as db, db:
            if ref is None:
                return db.execute("delete from files").rowcount
            return db.execute("delete from files where lower(path) = lower(?) or lower(file_name) = lower(?) "
                              "or (length(?) >= 8 and sha256 like ?)", (ref, ref, ref, ref.lower() + "%")).rowcount

    def counts(self) -> dict[str, int]:
        with closing(self._db()) as db:
            return {r[0]: r[1] for r in db.execute("select status, count(*) from files group by status")}


# ============================================================ planning
@dataclass
class FileItem:
    info: FileInfo
    sha256: str | None = None
    status: str = "new"          # new | changed | skipped | waiting | too_big | loaded | failed | would_load
    reason: str = ""
    previous: dict | None = None  # ledger entry of the same path (changed files)
    rows: int = 0
    rejected: int = 0
    deleted: int = 0
    moved_to: str | None = None

    def to_dict(self) -> dict:
        return {"file": self.info.rel, "size": self.info.size, "sha256": (self.sha256 or "")[:16],
                "status": self.status, "reason": self.reason, "rows": self.rows, "rejected": self.rejected,
                "deleted": self.deleted, "moved_to": self.moved_to,
                "modified": datetime.fromtimestamp(self.info.modified, timezone.utc).isoformat(timespec="seconds")}


@dataclass
class FolderPlan:
    items: list[FileItem] = field(default_factory=list)

    @property
    def to_load(self) -> list[FileItem]:
        return [i for i in self.items if i.status in ("new", "changed")]

    def count(self, *statuses: str) -> int:
        return sum(1 for i in self.items if i.status in statuses)


def skip_dirs(spec) -> set[str]:
    out = set()
    for d in (spec.files.archive_folder, spec.files.rejected_folder):
        if d and not _is_absolute(d):
            out.add(d.strip("/\\").split("/")[0].split("\\")[0].lower())
    return out


def plan_files(spec, source, ledger: Ledger, *, force: bool = False, now: float | None = None,
               in_target=None) -> FolderPlan:
    """Classify every matching file. ``in_target(shas) -> set`` checks the target table (optional)."""
    import time
    now = now or time.time()
    files = source.list_files(skip_dirs(spec))
    max_bytes = source.spec.max_file_mb * 1024 * 1024
    plan = FolderPlan()
    for f in files:
        item = FileItem(f)
        plan.items.append(item)
        if f.size > max_bytes:
            item.status, item.reason = "too_big", f"{f.size / 1048576:.1f} MB is over max_file_mb " \
                                                  f"({source.spec.max_file_mb:g})"
            continue
        stable = spec.tracking.stable_seconds
        if stable and now - f.modified < stable:
            item.status, item.reason = "waiting", f"changed {now - f.modified:.0f}s ago (still being copied?)"
            continue
        try:
            item.sha256 = sha256_of(f.path)
        except OSError as exc:
            item.status, item.reason = "failed", f"cannot read: {exc}"
            continue
        seen = ledger.by_hash(item.sha256)
        if seen and not force:
            item.status = "skipped"
            item.reason = (f"already loaded (run {seen['run_id']}" + (f", as {seen['path']}" if seen["path"] != f.rel
                                                                       else "") + ")")
            if seen["status"] == "in_target":
                item.reason = "already in the target table"
            continue
        prev = ledger.by_path(f.rel)
        if prev and prev["sha256"] != item.sha256:
            item.status, item.previous = "changed", prev
            item.reason = f"new content for a file loaded in run {prev['run_id']} – " + \
                          ("replaces its rows" if spec.files.on_changed == "replace" else "loaded as a new version")
        elif seen and force:
            item.status, item.previous = "changed", seen
            item.reason = "already loaded – loading again (force)"
    if in_target is not None:
        cand = [i for i in plan.items if i.status == "new"]
        found = in_target([i.sha256 for i in cand]) if cand else set()
        for i in cand:
            if i.sha256 in found:
                i.status, i.reason = "skipped", "already in the target table (same sha256)"
                ledger.add(i.info, i.sha256, "in_target", reason="found in target table")
    if spec.files.max_files:
        n = 0
        for i in plan.items:
            if i.status in ("new", "changed"):
                n += 1
                if n > spec.files.max_files:
                    i.status, i.reason = "waiting", f"max_files ({spec.files.max_files}) reached – next run"
    return plan


# ============================================================ rows of one file
def used_sources(spec) -> set[str]:
    return {c.source for c in spec.columns if c.source}


def file_rows(spec, item: FileItem) -> list[dict[str, Any]]:
    meta = item.info.meta()
    meta["sha256"] = item.sha256
    used = used_sources(spec)
    if spec.files.read == "records":
        rows = parse_file(item.info.path, None, spec.files.encoding)
        return [_merge(r, meta) for r in rows]
    data = item.info.path.read_bytes() if ("content" in used or "content_text" in used) else None
    if "content" in used:
        meta["content"] = data
    if "content_text" in used:
        meta["content_text"] = decode_text(data or b"", spec.files.encoding)
    return [meta]


def _merge(row: dict, meta: dict) -> dict:
    out = dict(row)
    for k, v in meta.items():
        out.setdefault(k, v)          # a column of the file itself wins over the file_* columns
    return out


def sample_rows(spec, source, limit: int = 5) -> list[dict]:
    """Rows to show the editor (header, sample) without reading big contents."""
    files = [f for f in source.list_files(skip_dirs(spec))
             if f.size <= source.spec.max_file_mb * 1024 * 1024][:max(limit, 1)]
    if spec.files.read == "records":
        for f in files:
            try:
                rows = parse_file(f.path, None, spec.files.encoding)
            except Exception:  # noqa: BLE001 – try the next file
                continue
            meta = f.meta()
            meta["sha256"] = "…"
            return [_merge(r, meta) for r in rows]
        return []
    out = []
    for f in files:
        m = f.meta()
        m.update(sha256="(computed when loading)", content=f"<{f.size} bytes>", content_text=f"<text of {f.name}>")
        out.append({k: m.get(k) for k in FILE_COLUMNS})
    return out


def safe_value(v: Any, width: int = 200) -> Any:
    """For previews and rejects files: never dump file contents."""
    if isinstance(v, (bytes, bytearray, memoryview)):
        return f"<{len(v)} bytes>"
    if isinstance(v, str) and len(v) > width:
        return v[:width] + f"… ({len(v)} characters)"
    return v


def safe_row(row: dict) -> dict:
    return {k: safe_value(v) for k, v in row.items()}


# ============================================================ after the load
def _unique(dest: Path) -> Path:
    if not dest.exists():
        return dest
    stem, suf, n = dest.stem, dest.suffix, 1
    while True:
        cand = dest.with_name(f"{stem}_{n}{suf}")
        if not cand.exists():
            return cand
        n += 1


def _base(root: Path, folder: str) -> Path:
    return Path(folder) if _is_absolute(folder) else root / folder


def move_file(spec, root: Path, item: FileItem, kind: str) -> str | None:
    """Archive (kind=done) or set aside (kind=rejected) a file. Returns where it went."""
    cfg = spec.files
    if kind == "done":
        if cfg.after_load == "leave":
            return None
        if cfg.after_load == "delete":
            item.info.path.unlink()
            return "(deleted)"
        base = _base(root, cfg.archive_folder)
    else:
        if not cfg.rejected_folder:
            return None
        base = _base(root, cfg.rejected_folder)
    if cfg.dated_folders:
        base = base / datetime.now().strftime("%Y-%m-%d")
    dest = _unique(base / Path(item.info.rel))
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(item.info.path), str(dest))
    if kind == "rejected" and item.reason:
        try:
            dest.with_name(dest.name + ".reason.txt").write_text(item.reason + "\n", encoding="utf-8")
        except OSError:
            pass
    try:
        return dest.relative_to(root).as_posix()
    except ValueError:
        return str(dest)
