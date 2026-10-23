"""Folder source: the files in a directory, for loads (each file -> a document row, or its parsed rows).

Security: the folder is a name inside ``security.data_root``, or an absolute / UNC path listed in
``security.allowed_folders``. Symlinks that lead outside the folder are ignored. Read-only except for
moving loaded files to the archive / rejected folders, which a load does.
"""
from __future__ import annotations

import fnmatch
import hashlib
import mimetypes
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import Any

from .base import DataSource, ExecutionContext, SourceError, register

# what each file looks like as a source row (document mode); records mode adds these to every row
FILE_COLUMNS = ["file_name", "file_path", "folder", "extension", "size_bytes", "modified", "created",
                "sha256", "mime_type", "content", "content_text"]
LARGE_COLUMNS = ("content", "content_text")


@dataclass
class FileInfo:
    path: Path               # absolute
    rel: str                 # relative to the folder, with / separators
    size: int
    modified: float
    created: float

    @property
    def name(self) -> str:
        return self.path.name

    def meta(self) -> dict[str, Any]:
        rel_dir = self.rel.rpartition("/")[0]
        return {"file_name": self.path.name, "file_path": self.rel, "folder": rel_dir,
                "extension": self.path.suffix.lower(), "size_bytes": self.size,
                "modified": datetime.fromtimestamp(self.modified, timezone.utc).replace(tzinfo=None,
                                                                                         microsecond=0),
                "created": datetime.fromtimestamp(self.created, timezone.utc).replace(tzinfo=None, microsecond=0),
                "mime_type": mimetypes.guess_type(self.path.name)[0] or "application/octet-stream"}


def sha256_of(path: Path, chunk: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            b = fh.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _is_absolute(p: str) -> bool:
    return PureWindowsPath(p).is_absolute() or p.startswith(("/", "\\\\")) or bool(PureWindowsPath(p).drive)


def _norm(p: str) -> str:
    return str(p).replace("\\", "/").rstrip("/").lower()


@register("folder")
class FolderSource(DataSource):
    def root(self) -> Path:
        raw = (self.spec.path or "").strip()
        if not raw:
            raise SourceError(f"folder source '{self.name}' has no path", 500)
        if _is_absolute(raw):
            allowed = [_norm(a) for a in self.security.allowed_folders]
            n = _norm(raw)
            if not any(n == a or n.startswith(a + "/") for a in allowed):
                raise SourceError(f"folder '{raw}' is outside the data folder and not in "
                                  f"security.allowed_folders; add it there (Sources > folder > Allow), "
                                  f"or use a folder inside '{self.security.data_root}'", 500)
            return Path(raw)
        try:
            base = (Path(self.project_root) / self.security.data_root).resolve()
            target = (base / raw).resolve()
        except OSError as exc:
            raise SourceError(f"invalid folder path {raw!r}: {exc}", 500) from None
        if target != base and base not in target.parents:
            raise SourceError(f"folder '{raw}' is outside the data folder '{self.security.data_root}'", 500)
        return target

    def special_dirs(self, extra: list[str] | None = None) -> set[str]:
        """Archive / rejected folders inside the root are never scanned."""
        return {x.strip("/\\").lower() for x in (extra or []) if x and not _is_absolute(x)}

    def list_files(self, skip_dirs: set[str] | None = None) -> list[FileInfo]:
        root = self.root()
        if not root.is_dir():
            raise SourceError(f"folder not found: {root}", 500)
        skip = {d.lower() for d in (skip_dirs or set())}
        it = root.rglob("*") if self.spec.recursive else root.glob("*")
        out: list[FileInfo] = []
        root_r = root.resolve()
        for p in it:
            try:
                if not p.is_file() or p.name.startswith(".") or p.name.endswith(".reason.txt"):
                    continue
                rel = p.relative_to(root).as_posix()
                first = rel.split("/")[0].lower() if "/" in rel else ""
                if first in skip:
                    continue
                if p.is_symlink() and root_r not in p.resolve().parents:
                    continue
                name = p.name
                if not any(fnmatch.fnmatch(name.lower(), pat.lower()) or fnmatch.fnmatch(rel.lower(), pat.lower())
                           for pat in (self.spec.include or ["*"])):
                    continue
                if any(fnmatch.fnmatch(name.lower(), pat.lower()) or fnmatch.fnmatch(rel.lower(), pat.lower())
                       for pat in self.spec.exclude):
                    continue
                st = p.stat()
                out.append(FileInfo(p, rel, st.st_size, st.st_mtime, getattr(st, "st_birthtime", st.st_ctime)))
            except OSError:
                continue
        return sorted(out, key=lambda f: (f.modified, f.rel))

    # ------------------------------------------------------------- not an endpoint source
    def validate_endpoint(self, endpoint) -> None:
        raise ValueError(f"source '{self.name}' is a folder of files; it is read by loads, not endpoints "
                         f"(use a file source for one CSV/Excel file behind an endpoint)")

    async def execute(self, ctx: ExecutionContext) -> Any:  # pragma: no cover - never routed
        raise SourceError("not an endpoint source", 500)

    async def health(self) -> bool:
        try:
            return self.root().is_dir()
        except SourceError:
            return False
