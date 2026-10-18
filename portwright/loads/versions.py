"""Source versioning: fingerprint what a load reads, keep numbered versions and compressed copies.

A version is one distinct content of the source (same rows = same version). Runs record which
version they read (``source_version`` / ``source_hash`` in history.jsonl), so "has the file
changed since the last successful load?" is a comparison of two hashes.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any


def _canon(v: Any) -> Any:
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return str(v)
    return v


def rows_hash(rows: list[dict[str, Any]]) -> str:
    """Order-sensitive hash of the rows exactly as read (keys sorted inside each row)."""
    h = hashlib.sha256()
    for r in rows:
        h.update(json.dumps(r, sort_keys=True, default=_canon, ensure_ascii=False).encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


def header_of(rows: list[dict[str, Any]], sample: int = 50) -> list[str]:
    cols: list[str] = []
    for r in rows[:sample]:
        cols += [k for k in r if k not in cols]
    return cols


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class Fingerprint:
    hash: str                      # rows hash: decides "changed or not"
    rows: int
    header: list[str]
    kind: str                      # file | endpoint
    file: str | None = None        # file name (file sources)
    size: int | None = None
    modified: str | None = None    # file mtime, UTC ISO
    file_sha256: str | None = None

    @property
    def short(self) -> str:
        return self.hash[:12]


@dataclass
class Version:
    version: int
    first_seen: str
    hash: str
    rows: int
    header: list[str]
    kind: str
    file: str | None = None
    size: int | None = None
    modified: str | None = None
    file_sha256: str | None = None
    run_id: str | None = None       # run that first read it
    archive: str | None = None      # archive file name (may since have been pruned)
    header_change: dict = field(default_factory=dict)   # {"added": [...], "removed": [...]} vs previous


def header_change(old: list[str], new: list[str]) -> dict:
    added = [c for c in new if c not in old]
    removed = [c for c in old if c not in new]
    return {"added": added, "removed": removed} if (added or removed) else {}


class VersionStore:
    """logs/loads/versions/<load>.jsonl + logs/loads/archive/<load>/v0001.jsonl.gz"""

    def __init__(self, loads_dir: Path, load: str):
        self.load = load
        self.index = loads_dir / "versions" / f"{load}.jsonl"
        self.archive_dir = loads_dir / "archive" / load

    def list(self) -> list[Version]:
        if not self.index.exists():
            return []
        out = []
        for line in self.index.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    out.append(Version(**json.loads(line)))
                except (ValueError, TypeError):
                    continue
        return out

    def get(self, version: int) -> Version | None:
        return next((v for v in self.list() if v.version == version), None)

    def find(self, hash_: str) -> Version | None:
        return next((v for v in reversed(self.list()) if v.hash == hash_), None)

    def add(self, fp: Fingerprint, rows: list[dict], run_id: str, keep: int) -> Version:
        """Record a new version (or return the existing one with the same content)."""
        existing = self.find(fp.hash)
        if existing:
            return existing
        versions = self.list()
        prev = versions[-1] if versions else None
        v = Version(version=(prev.version + 1 if prev else 1),
                    first_seen=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    hash=fp.hash, rows=fp.rows, header=fp.header, kind=fp.kind, file=fp.file, size=fp.size,
                    modified=fp.modified, file_sha256=fp.file_sha256, run_id=run_id,
                    header_change=header_change(prev.header, fp.header) if prev else {})
        if keep > 0:
            v.archive = self._write_archive(v.version, rows)
        self.index.parent.mkdir(parents=True, exist_ok=True)
        with self.index.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(asdict(v), default=str) + "\n")
        self.prune(keep)
        return v

    # ------------------------------------------------------------- archive
    def _write_archive(self, version: int, rows: list[dict]) -> str:
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        name = f"v{version:04d}.jsonl.gz"
        tmp = self.archive_dir / (name + ".tmp")
        with gzip.open(tmp, "wt", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, default=_canon, ensure_ascii=False) + "\n")
        os.replace(tmp, self.archive_dir / name)
        return name

    def archived(self, v: Version) -> bool:
        return bool(v.archive) and (self.archive_dir / v.archive).exists()

    def read(self, version: int) -> list[dict]:
        v = self.get(version)
        if v is None:
            raise KeyError(f"no version {version}")
        if not self.archived(v):
            raise FileNotFoundError(f"version {version} has no archived copy (archive_keep, or pruned)")
        with gzip.open(self.archive_dir / v.archive, "rt", encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def prune(self, keep: int) -> None:
        if not self.archive_dir.exists():
            return
        files = sorted(self.archive_dir.glob("v*.jsonl.gz"))
        for f in files[: max(0, len(files) - keep)]:
            f.unlink(missing_ok=True)


# ------------------------------------------------------------------- diff
def diff_rows(old: list[dict], new: list[dict], key_columns: list[str], sample: int = 20) -> dict:
    """Compare two versions. With key columns: added / removed / changed rows by key.
    Without: rows present in only one version (by full content)."""
    def ident(r):
        return tuple(str(r.get(k, "")) for k in key_columns)

    def canon(r):
        return json.dumps(r, sort_keys=True, default=_canon, ensure_ascii=False)

    if key_columns:
        a = {ident(r): r for r in old}
        b = {ident(r): r for r in new}
        added = [b[k] for k in b if k not in a]
        removed = [a[k] for k in a if k not in b]
        changed = []
        for k in b:
            if k in a and canon(a[k]) != canon(b[k]):
                fields = sorted({c for c in set(a[k]) | set(b[k]) if a[k].get(c) != b[k].get(c)})
                changed.append({"key": dict(zip(key_columns, k)), "fields": fields,
                                "before": {c: a[k].get(c) for c in fields}, "after": {c: b[k].get(c) for c in fields}})
        unchanged = sum(1 for k in b if k in a) - len(changed)
        return {"by": "key", "key": key_columns, "added": len(added), "removed": len(removed),
                "changed": len(changed), "unchanged": unchanged, "added_rows": added[:sample],
                "removed_rows": removed[:sample], "changed_rows": changed[:sample]}
    from collections import Counter
    ca, cb = Counter(canon(r) for r in old), Counter(canon(r) for r in new)
    only_new = list((cb - ca).elements())
    only_old = list((ca - cb).elements())
    return {"by": "content", "key": [], "added": len(only_new), "removed": len(only_old), "changed": None,
            "unchanged": sum((ca & cb).values()), "added_rows": [json.loads(x) for x in only_new[:sample]],
            "removed_rows": [json.loads(x) for x in only_old[:sample]], "changed_rows": []}
