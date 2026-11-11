"""Sandboxed filesystem access for workflow steps (and, later, fs tools / MCP).

Nothing here takes a raw path from a caller and opens it. Every path is an *address*:

* ``reports:2026-11/sales.pdf`` – a file under the named root ``reports``
* ``dest:finance/2026-11``      – a folder under the destination ``finance`` (write-only)
* ``C:\\proj\\reports\\output\\x.pdf`` or ``reports/output/x.pdf`` – an absolute or project-relative path,
  accepted only when it lies inside a configured root (so report outputs can be passed straight on)

``Sandbox.resolve`` rejects ``..``, NUL, NTFS alternate data streams, reserved device names, trailing
dots/spaces, and anything (including symlinks and junctions) that ends up outside its root. Each root has a
mode (read / write / read_write) and optional file-name patterns; destinations are write-only. Writes are
atomic (``.partial`` file, fsync, rename), deletes go to a trash folder by default, and every operation is
written to the audit log.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shutil
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from ..config import FsRoot, ProjectConfig, is_absolute_path, path_allowed

ADMIN_SCOPE = "*"
_ADDRESS = re.compile(r"^([A-Za-z][A-Za-z0-9_\-]+):(.*)$", re.DOTALL)
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
_CHUNK = 1 << 20
MAX_LIST = 500
MAX_READ_CHARS = 200_000


class FsError(Exception):
    """A filesystem operation was refused or failed; the message is safe to show."""


@dataclass
class Place:
    """A resolved address."""
    kind: str            # "root" | "dest"
    name: str            # root or destination name
    base: Path           # the root folder (resolved)
    path: Path           # the target (resolved)
    readable: bool
    writable: bool
    patterns: tuple[str, ...] = ()
    delete: str = "none"

    @property
    def address(self) -> str:
        """``reports:a/b.pdf`` for roots, ``dest:finance/a/b.pdf`` for destinations."""
        rel = Path(os.path.relpath(self.path, self.base)).as_posix()
        rel = "" if rel == "." else rel
        if self.kind == "dest":
            return f"dest:{self.name}" + (f"/{rel}" if rel else "")
        return f"{self.name}:{rel}"


def all_roots(config: ProjectConfig) -> dict[str, FsRoot]:
    """Configured roots plus the implicit ones: reports (read_write), templates (read_write), data (read)."""
    out = {
        "reports": FsRoot(path="reports/output", mode="read_write"),
        "templates": FsRoot(path="templates", mode="read_write"),
        "data": FsRoot(path=config.security.data_root, mode="read"),
    }
    out.update(config.filesystem.roots)
    return out


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _check_parts(rel: str) -> list[str]:
    if "\x00" in rel:
        raise FsError("path contains a NUL character")
    parts = [p for p in re.split(r"[\\/]+", rel) if p not in ("", ".")]
    for p in parts:
        if p == "..":
            raise FsError("'..' is not allowed in paths")
        if ":" in p:
            raise FsError(f"'{p}': ':' is not allowed in file names (alternate data streams)")
        if p != p.rstrip(" ."):
            raise FsError(f"'{p}': names may not end with a dot or a space")
        if p.split(".")[0].lower() in _RESERVED:
            raise FsError(f"'{p}' is a reserved device name")
        if any(ord(c) < 32 for c in p) or any(c in '<>"|?*' for c in p):
            raise FsError(f"'{p}' contains characters that are not allowed in file names")
    return parts


def _inside(base: Path, target: Path) -> bool:
    try:
        return os.path.commonpath([os.path.normcase(str(base)), os.path.normcase(str(target))]) == \
            os.path.normcase(str(base))
    except ValueError:                       # different drives
        return False


class Sandbox:
    def __init__(self, config: ProjectConfig, project_root: str | Path, *, scopes: Iterable[str] | None = None,
                 actor: str = "portwright", audit: Any = None, allowed: Iterable[str] | None = None):
        """``scopes=None`` means unrestricted (local admin use, e.g. the CLI); otherwise every operation needs
        ``fs:<root>:read`` / ``fs:<root>:write`` / ``fs:dest:<name>`` (or ``*``). ``allowed`` (root names and
        ``dest:<name>``) limits the places at all - a tool only reaches the roots it was given."""
        self.allowed = None if allowed is None else frozenset(allowed)
        self.cfg = config
        self.project = Path(project_root).resolve()
        self.scopes = None if scopes is None else frozenset(scopes)
        self.actor = actor
        if audit is None:
            from ..security.audit import AuditLogger
            log = config.security.audit_log
            audit = AuditLogger(str(self.project / log) if log else None)
        self.audit = audit
        self.max_bytes = config.filesystem.max_file_mb * 1024 * 1024

    # ------------------------------------------------------------------ roots
    def roots(self) -> dict[str, FsRoot]:
        return all_roots(self.cfg)

    def _base(self, raw: str, what: str) -> Path:
        if is_absolute_path(raw):
            if not path_allowed(raw, self.cfg.security.allowed_folders):
                raise FsError(f"{what}: '{raw}' is not in security.allowed_folders")
            base = Path(raw)
        else:
            _check_parts(raw)
            base = self.project / raw
        try:
            return base.resolve()
        except OSError as exc:
            raise FsError(f"{what}: invalid path ({exc.strerror or exc})") from None

    def _place(self, kind: str, name: str, rel: str) -> Place:
        if kind == "dest":
            spec = self.cfg.destinations.get(name)
            if spec is None:
                raise FsError(f"unknown destination '{name}'"
                              + (f"; defined: {', '.join(sorted(self.cfg.destinations))}" if self.cfg.destinations else ""))
            base, readable, writable, patterns, delete = self._base(spec.path, f"destination '{name}'"), False, True, (), "none"
        else:
            spec = self.roots().get(name)
            if spec is None:
                raise FsError(f"unknown filesystem root '{name}'; defined: {', '.join(sorted(self.roots()))}")
            base = self._base(spec.path, f"root '{name}'")
            readable, writable = spec.mode in ("read", "read_write"), spec.mode in ("write", "read_write")
            patterns, delete = tuple(spec.patterns), spec.delete
        key = f"dest:{name}" if kind == "dest" else name
        if self.allowed is not None and key not in self.allowed:
            raise FsError(f"'{key}' is not available here; allowed: {', '.join(sorted(self.allowed)) or 'none'}")
        parts = _check_parts(rel)
        try:
            target = base.joinpath(*parts).resolve() if parts else base
        except OSError as exc:
            raise FsError(f"invalid path '{rel}' ({exc.strerror or exc})") from None
        if not _inside(base, target):
            raise FsError(f"'{rel}' leads outside '{name}' (symlink or junction?)")
        return Place(kind, name, base, target, readable, writable, patterns, delete)

    def resolve(self, address: str | Path | dict) -> Place:
        """Turn an address (or a report artifact dict with ``path``) into a checked ``Place``."""
        if isinstance(address, dict):
            if "dest" in address and "path" not in address:          # a copy result: deliver it onwards
                address = address["dest"]
            else:
                address = address.get("path", "")
        address = str(address).strip()
        if not address:
            raise FsError("empty path")
        m = _ADDRESS.match(address)                 # names are 2+ characters, so 'C:\...' is a drive path
        if m:
            name, rest = m.group(1), m.group(2)
            if name == "dest":
                body = rest.lstrip("/\\")
                dname = re.split(r"[\\/]", body, maxsplit=1)[0]
                return self._place("dest", dname, body[len(dname):])
            return self._place("root", name, rest)
        return self._from_path(address)

    def _from_path(self, raw: str) -> Place:
        """An absolute or project-relative path: accepted when it is inside a configured root."""
        if "\x00" in raw:
            raise FsError("path contains a NUL character")
        if not is_absolute_path(raw):
            _check_parts(raw)
        p = Path(raw) if is_absolute_path(raw) else self.project / raw
        try:
            target = p.resolve()
        except OSError as exc:
            raise FsError(f"invalid path ({exc.strerror or exc})") from None
        best: tuple[int, str, str] | None = None
        for kind, items in (("root", self.roots().items()), ("dest", self.cfg.destinations.items())):
            for name, spec in items:
                try:
                    base = self._base(spec.path, name)
                except FsError:
                    continue
                if _inside(base, target) and (best is None or len(str(base)) > best[0]):
                    best = (len(str(base)), kind, name)
        if best is None:
            raise FsError(f"'{raw}' is not inside any filesystem root or destination")
        _, kind, name = best
        base = self._base((self.roots()[name] if kind == "root" else self.cfg.destinations[name]).path, name)
        rel = os.path.relpath(target, base)
        return self._place(kind, name, "" if rel == "." else rel)

    # ------------------------------------------------------------------ checks
    def _scope_ok(self, needed: list[str]) -> bool:
        return self.scopes is None or ADMIN_SCOPE in self.scopes or any(s in self.scopes for s in needed)

    def _need_read(self, pl: Place) -> None:
        if not pl.readable:
            raise FsError(f"'{pl.name}' is write-only" if pl.kind == "dest" else f"root '{pl.name}' is not readable")
        if not self._scope_ok([f"fs:{pl.name}:read", f"fs:{pl.name}:write"]):
            raise FsError(f"missing scope fs:{pl.name}:read")

    def _need_write(self, pl: Place) -> None:
        if not pl.writable:
            raise FsError(f"root '{pl.name}' is read-only")
        needed = [f"fs:dest:{pl.name}"] if pl.kind == "dest" else [f"fs:{pl.name}:write"]
        if not self._scope_ok(needed):
            raise FsError(f"missing scope {needed[0]}")

    def _pattern_ok(self, pl: Place, name: str) -> None:
        if pl.patterns and not any(fnmatch.fnmatch(name.lower(), p.lower()) for p in pl.patterns):
            raise FsError(f"'{name}' does not match the file patterns allowed in '{pl.name}': {list(pl.patterns)}")

    def _record(self, op: str, **fields: Any) -> None:
        self.audit.record(event=f"fs_{op}", actor=self.actor, **fields)

    # ------------------------------------------------------------------ read ops
    def stat(self, address: Any) -> dict:
        pl = self.resolve(address)
        self._need_read(pl)
        if not pl.path.exists():
            raise FsError(f"'{pl.address}' does not exist")
        st = pl.path.stat()
        out = {"path": str(pl.path), "address": pl.address, "is_dir": pl.path.is_dir(), "bytes": st.st_size,
               "modified": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat()}
        if pl.path.is_file():
            out["sha256"] = sha256_file(pl.path)
        return out

    def list(self, address: Any, glob: str | None = None, recursive: bool = False) -> list[dict]:
        pl = self.resolve(address)
        self._need_read(pl)
        if not pl.path.is_dir():
            raise FsError(f"'{pl.address}' is not a folder")
        it = pl.path.rglob(glob or "*") if recursive else pl.path.glob(glob or "*")
        out = []
        for p in sorted(it):
            try:
                rp = p.resolve()
            except OSError:
                continue
            if not _inside(pl.base, rp):                        # symlink / junction leading out: skip
                continue
            st = p.stat()
            out.append({"name": p.name, "path": str(p), "address": self._addr(pl, rp), "is_dir": p.is_dir(),
                        "bytes": st.st_size if p.is_file() else 0,
                        "modified": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat()})
            if len(out) >= MAX_LIST:
                break
        self._record("list", path=pl.address, count=len(out))
        return out

    def read_text(self, address: Any, max_chars: int = MAX_READ_CHARS) -> dict:
        pl = self.resolve(address)
        self._need_read(pl)
        if not pl.path.is_file():
            raise FsError(f"'{pl.address}' is not a file")
        size = pl.path.stat().st_size
        if size > self.max_bytes:
            raise FsError(f"'{pl.address}' is {size:,} bytes; the limit is {self.max_bytes:,}")
        raw = pl.path.read_bytes()
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            self._record("read", path=pl.address, bytes=size, binary=True)
            return {"path": str(pl.path), "address": pl.address, "bytes": size, "binary": True,
                    "sha256": hashlib.sha256(raw).hexdigest()}
        cap = min(max_chars, MAX_READ_CHARS)
        self._record("read", path=pl.address, bytes=size)
        return {"path": str(pl.path), "address": pl.address, "bytes": size, "content": text[:cap],
                "truncated": len(text) > cap}

    def search(self, address: Any, glob: str | None = None, text: str | None = None,
               max_results: int = 50) -> list[dict]:
        """Files under a folder matching *glob*; with *text*, only text files containing it (with line hits)."""
        pl = self.resolve(address)
        self._need_read(pl)
        if not pl.path.is_dir():
            raise FsError(f"'{pl.address}' is not a folder")
        needle = (text or "").lower()
        cap = max(1, min(int(max_results or 50), MAX_LIST))
        out: list[dict] = []
        for p in sorted(pl.path.rglob(glob or "*")):
            try:
                rp = p.resolve()
            except OSError:
                continue
            if not p.is_file() or not _inside(pl.base, rp):
                continue
            entry = {"name": p.name, "address": self._addr(pl, rp), "bytes": p.stat().st_size}
            if needle:
                if entry["bytes"] > min(self.max_bytes, 5 * 1024 * 1024):
                    continue
                try:
                    lines = rp.read_text(encoding="utf-8-sig").splitlines()
                except (UnicodeDecodeError, OSError):
                    continue
                hits = [{"line": i + 1, "text": ln.strip()[:200]} for i, ln in enumerate(lines) if needle in ln.lower()]
                if not hits:
                    continue
                entry["matches"] = hits[:5]
            out.append(entry)
            if len(out) >= cap:
                break
        self._record("search", path=pl.address, glob=glob, text=bool(text), count=len(out))
        return out

    def read_table(self, address: Any, max_rows: int = 50) -> dict:
        """The first rows of a CSV / Excel / JSON file (Excel cannot be read as text)."""
        from ..sources.files import parse_file
        pl = self.resolve(address)
        self._need_read(pl)
        if not pl.path.is_file():
            raise FsError(f"'{pl.address}' is not a file")
        size = pl.path.stat().st_size
        if size > self.max_bytes:
            raise FsError(f"'{pl.address}' is {size:,} bytes; the limit is {self.max_bytes:,}")
        try:
            rows = parse_file(pl.path)
        except Exception as exc:                                     # noqa: BLE001 - parser messages vary
            raise FsError(f"could not read '{pl.address}' as a table: {str(exc)[:200]}") from None
        cols: dict[str, None] = {}
        for r in rows[:max_rows]:
            for k in r:
                cols.setdefault(k, None)
        self._record("read", path=pl.address, bytes=size, table=True)
        return {"address": pl.address, "columns": list(cols), "row_count": len(rows),
                "rows": [{k: (v if isinstance(v, (str, int, float, bool)) or v is None else str(v)) for k, v in r.items()}
                         for r in rows[:max_rows]], "truncated": len(rows) > max_rows}

    def read_bytes(self, address: Any, max_bytes: int) -> tuple[Place, bytes]:
        pl = self.resolve(address)
        self._need_read(pl)
        if not pl.path.is_file():
            raise FsError(f"'{pl.address}' is not a file")
        size = pl.path.stat().st_size
        if size > min(max_bytes, self.max_bytes):
            raise FsError(f"'{pl.address}' is {size:,} bytes; the limit here is {min(max_bytes, self.max_bytes):,}")
        data = pl.path.read_bytes()
        self._record("read", path=pl.address, bytes=size, blob=True)
        return pl, data

    def read_files(self, files: Any, max_total: int) -> list[tuple[Place, bytes]]:
        """Read several files (paths, addresses, report outputs) for one use, e.g. email attachments.
        Each needs read access; together they may not exceed *max_total* bytes."""
        items = _as_list(files)
        places = [self._readable_file(i) for i in items]
        total = sum(pl.path.stat().st_size for pl in places)
        if total > max_total:
            raise FsError(f"the files are {total:,} bytes together; the limit here is {max_total:,}")
        out = []
        for pl in places:
            data = pl.path.read_bytes()
            self._record("read", path=pl.address, bytes=len(data), blob=True)
            out.append((pl, data))
        return out

    @staticmethod
    def _addr(pl: Place, p: Path) -> str:
        return Place(pl.kind, pl.name, pl.base, p, pl.readable, pl.writable).address

    # ------------------------------------------------------------------ write ops
    def mkdir(self, address: Any) -> dict:
        pl = self.resolve(address)
        self._need_write(pl)
        pl.path.mkdir(parents=True, exist_ok=True)
        self._record("mkdir", path=pl.address)
        return {"path": str(pl.path), "address": pl.address}

    def write_text(self, address: Any, content: Any, overwrite: str = "never") -> dict:
        pl = self.resolve(address)
        self._need_write(pl)
        self._pattern_ok(pl, pl.path.name)
        text = content if isinstance(content, str) else json.dumps(content, default=str, ensure_ascii=False, indent=2)
        data = text.encode("utf-8")
        if len(data) > self.max_bytes:
            raise FsError(f"content is {len(data):,} bytes; the limit is {self.max_bytes:,}")
        dest, action = self._target(pl.path, overwrite, src_mtime=time.time())
        if action == "skipped":
            return {"path": str(dest), "address": self._addr(pl, dest), "action": "skipped"}
        dest.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(dest, data)
        digest = hashlib.sha256(data).hexdigest()
        self._record("write", path=self._addr(pl, dest), bytes=len(data), sha256=digest)
        return {"path": str(dest), "address": self._addr(pl, dest), "bytes": len(data), "sha256": digest,
                "action": action}

    def delete(self, files: Any) -> list[dict]:
        out = []
        for item in _as_list(files):
            pl = self.resolve(item)
            self._need_write(pl)
            if pl.path == pl.base:
                raise FsError(f"refusing to delete the root '{pl.name}' itself")
            if not pl.path.exists():
                out.append({"path": str(pl.path), "action": "missing"})
                continue
            if pl.delete == "none":
                raise FsError(f"deleting is turned off for '{pl.name}'")
            if pl.delete == "trash":
                trash = self.project / self.cfg.filesystem.trash / datetime.now().strftime("%Y-%m-%d") / pl.name
                trash.mkdir(parents=True, exist_ok=True)
                dest = _free_name(trash / pl.path.name)
                shutil.move(str(pl.path), str(dest))
                action = "trashed"
            elif pl.path.is_dir():
                shutil.rmtree(pl.path)
                action = "deleted"
            else:
                pl.path.unlink()
                action = "deleted"
            self._record("delete", path=pl.address, action=action)
            out.append({"path": str(pl.path), "address": pl.address, "action": action})
        return out

    def zip(self, files: Any, to: Any, overwrite: str = "never") -> dict:
        srcs = [self._readable_file(f) for f in _as_list(files)]
        if not srcs:
            raise FsError("nothing to zip")
        pl = self.resolve(to)
        self._need_write(pl)
        if not pl.path.name.lower().endswith(".zip"):
            raise FsError("the zip target must end in .zip")
        self._pattern_ok(pl, pl.path.name)
        dest, action = self._target(pl.path, overwrite, src_mtime=time.time())
        if action == "skipped":
            return {"path": str(dest), "action": "skipped"}
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".partial")
        names: set[str] = set()
        try:
            with zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED) as zf:
                for s in srcs:
                    arc = s.path.name
                    n = 1
                    while arc in names:
                        arc = f"{s.path.stem} ({n}){s.path.suffix}"
                        n += 1
                    names.add(arc)
                    zf.write(s.path, arc)
            os.replace(part, dest)
        finally:
            part.unlink(missing_ok=True)
        digest, size = sha256_file(dest), dest.stat().st_size
        self._record("zip", path=self._addr(pl, dest), files=len(srcs), bytes=size, sha256=digest)
        return {"path": str(dest), "address": self._addr(pl, dest), "files": len(srcs), "bytes": size,
                "sha256": digest, "action": action}

    # ------------------------------------------------------------------ copy / move
    def transfer(self, files: Any, to: Any, *, mode: str = "copy", overwrite: str | None = None,
                 create_dirs: bool = True, verify: str = "sha256", manifest: bool = False) -> list[dict]:
        """Copy (or move) files into the folder *to*. A move deletes each source only after its copy is
        written and verified; if that delete fails the entry says ``copied`` with a warning."""
        if mode not in ("copy", "move"):
            raise FsError(f"unknown transfer mode '{mode}'")
        srcs = [self._readable_file(f) for f in _as_list(files)]
        if not srcs:
            raise FsError("no files to " + mode)
        if mode == "move":
            for s in srcs:
                if not s.writable:
                    raise FsError(f"cannot move out of '{s.name}': it is read-only (use copy)")
                if not self._scope_ok([f"fs:{s.name}:write"]):
                    raise FsError(f"missing scope fs:{s.name}:write (needed to remove moved files)")
        dst = self.resolve(to)
        self._need_write(dst)
        if dst.path.exists() and not dst.path.is_dir():
            raise FsError(f"'{dst.address}' is a file; copy/move 'to' must be a folder")
        if not dst.path.exists():
            if not create_dirs:
                raise FsError(f"folder '{dst.address}' does not exist (create_dirs is off)")
            dst.path.mkdir(parents=True, exist_ok=True)
        policy = overwrite or (self.cfg.destinations[dst.name].overwrite if dst.kind == "dest" else "never")

        out: list[dict] = []
        for s in srcs:
            self._pattern_ok(dst, s.path.name)
            size = s.path.stat().st_size
            if size > self.max_bytes:
                raise FsError(f"'{s.address}' is {size:,} bytes; the limit is {self.max_bytes:,}")
            target, action = self._target(dst.path / s.path.name, policy, src_mtime=s.path.stat().st_mtime)
            entry = {"src": str(s.path), "dest": str(target), "address": self._addr(dst, target), "bytes": size}
            if action == "skipped":
                entry.update(action="skipped", sha256=sha256_file(s.path))
                out.append(entry)
                continue
            digest = _copy_atomic(s.path, target, verify)
            entry.update(sha256=digest, action="moved" if mode == "move" else "copied",
                         renamed=action == "renamed", replaced=action == "replaced")
            if mode == "move":
                try:
                    s.path.unlink()
                except OSError as exc:
                    entry.update(action="copied", warning=f"copied, but the source could not be removed: "
                                                          f"{exc.strerror or exc}")
            self._record(mode, src=s.address, dest=entry["address"], bytes=size, sha256=digest,
                         action=entry["action"])
            out.append(entry)
        if manifest:
            body = {"generated_at": datetime.now(timezone.utc).isoformat(), "actor": self.actor,
                    "files": [{"name": Path(e["dest"]).name, "bytes": e["bytes"], "sha256": e["sha256"],
                               "source": e["src"], "action": e["action"]} for e in out]}
            _atomic_write(dst.path / "manifest.json", json.dumps(body, indent=2).encode("utf-8"))
        return out

    def _readable_file(self, item: Any) -> Place:
        pl = self.resolve(item)
        self._need_read(pl)
        if not pl.path.is_file():
            raise FsError(f"'{pl.address}' is not a file" if pl.path.exists() else f"'{pl.address}' does not exist")
        return pl

    @staticmethod
    def _target(dest: Path, policy: str, src_mtime: float) -> tuple[Path, str]:
        """Where to write under the overwrite policy: (path, written | replaced | renamed | skipped)."""
        if not dest.exists():
            return dest, "written"
        if dest.is_dir():
            raise FsError(f"'{dest.name}' exists as a folder")
        if policy == "always":
            return dest, "replaced"
        if policy == "rename":
            return _free_name(dest), "renamed"
        if policy == "if_newer":
            return (dest, "replaced") if src_mtime > dest.stat().st_mtime else (dest, "skipped")
        raise FsError(f"'{dest.name}' already exists (overwrite: never); choose always, rename or if_newer")


# --------------------------------------------------------------------------- helpers
def _as_list(value: Any) -> list:
    if value is None or value == "":
        return []
    if isinstance(value, dict):
        for key in ("outputs", "files", "items", "rows"):           # report / copy / list results
            if isinstance(value.get(key), list):
                return value[key]
        return [value]
    if isinstance(value, (list, tuple)):
        return list(value)
    if isinstance(value, str):
        s = value.strip()
        if s.startswith("["):
            try:
                v = json.loads(s)
                if isinstance(v, list):
                    return v
            except ValueError:
                pass
        return [s]
    return [value]


def _free_name(dest: Path) -> Path:
    n = 1
    cand = dest
    while cand.exists():
        cand = dest.with_name(f"{dest.stem} ({n}){dest.suffix}")
        n += 1
    return cand


def _fsync(fh) -> None:
    fh.flush()
    try:
        os.fsync(fh.fileno())
    except OSError:                       # some network filesystems refuse fsync; the rename still applies
        pass


def _atomic_write(dest: Path, data: bytes) -> None:
    part = dest.with_name(dest.name + ".partial")
    try:
        with open(part, "wb") as fh:
            fh.write(data)
            _fsync(fh)
        os.replace(part, dest)
    finally:
        part.unlink(missing_ok=True)


def _copy_atomic(src: Path, dest: Path, verify: str) -> str:
    """Stream *src* to ``dest.partial``, verify, then rename into place. Returns the source sha256."""
    part = dest.with_name(dest.name + ".partial")
    h = hashlib.sha256()
    try:
        with open(src, "rb") as fi, open(part, "wb") as fo:
            for chunk in iter(lambda: fi.read(_CHUNK), b""):
                h.update(chunk)
                fo.write(chunk)
            _fsync(fo)
        digest = h.hexdigest()
        if verify == "sha256" and sha256_file(part) != digest:
            raise FsError(f"verification failed for '{dest.name}': the copy differs from the source")
        if verify == "size" and part.stat().st_size != src.stat().st_size:
            raise FsError(f"verification failed for '{dest.name}': sizes differ")
        try:
            shutil.copystat(src, part)
        except OSError:
            pass
        os.replace(part, dest)
        return digest
    finally:
        part.unlink(missing_ok=True)
