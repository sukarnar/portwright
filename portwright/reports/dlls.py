"""Which DLLs a WeasyPrint DLL folder is missing (Windows), found without loading anything.

WeasyPrint opens a few GTK/Pango DLLs by name; each of those needs more DLLs, listed in its PE import
table. When one is missing anywhere in that chain Windows only says "cannot load library", so this reads
the import tables to name the missing file and who needs it. Used by ``pw doctor`` on servers without
MSYS2, and by ``pango/collect-pango.py`` to copy a complete set.
"""
from __future__ import annotations

import os
import struct
from pathlib import Path

# What WeasyPrint opens by name (weasyprint/text/ffi.py); the rest is found by following imports.
WEASYPRINT_DLLS = ("libgobject-2.0-0.dll", "libpango-1.0-0.dll", "libpangoft2-1.0-0.dll", "libharfbuzz-0.dll",
                   "libharfbuzz-subset-0.dll", "libfontconfig-1.dll")


def imports(path: Path) -> list[str]:
    """The DLL names in a PE file's import table."""
    b = path.read_bytes()
    if len(b) < 0x40 or b[:2] != b"MZ":
        raise ValueError(f"{path.name} is not a Windows DLL")
    pe = struct.unpack_from("<I", b, 0x3C)[0]
    if b[pe:pe + 4] != b"PE\0\0":
        raise ValueError(f"{path.name} is not a Windows DLL")
    nsec = struct.unpack_from("<H", b, pe + 6)[0]
    opt_size = struct.unpack_from("<H", b, pe + 20)[0]
    opt = pe + 24
    dirs = opt + (112 if struct.unpack_from("<H", b, opt)[0] == 0x20B else 96)
    imp_rva = struct.unpack_from("<I", b, dirs + 8)[0]
    if not imp_rva:
        return []
    sections = []
    for i in range(nsec):
        s = opt + opt_size + 40 * i
        vsize, va, rsize, raw = struct.unpack_from("<IIII", b, s + 8)
        sections.append((va, max(vsize, rsize), raw))

    def offset(rva: int) -> int:
        for va, size, raw in sections:
            if va <= rva < va + size:
                return rva - va + raw
        raise ValueError(f"{path.name}: damaged import table")

    names, p = [], offset(imp_rva)
    while (name_rva := struct.unpack_from("<I", b, p + 12)[0]) != 0:
        n = offset(name_rva)
        names.append(b[n:b.index(b"\0", n)].decode("ascii"))
        p += 20
    return names


def _system_dirs() -> list[Path]:
    win = Path(os.environ.get("WINDIR", r"C:\Windows"))
    return [win / "System32", win]


def missing(folders: list[Path], roots: tuple[str, ...] = WEASYPRINT_DLLS) -> dict[str, list[str]]:
    """{missing DLL: [DLLs that need it]} for the chain starting at *roots*, looking in *folders* and
    Windows. A root that is not there at all is reported as needed by 'WeasyPrint'. Empty: complete."""
    found: dict[str, Path] = {}
    for f in folders:
        if f.is_dir():
            for p in f.glob("*.dll"):
                found.setdefault(p.name.lower(), p)
    system = _system_dirs()

    def in_windows(name: str) -> bool:
        return name.lower().startswith(("api-ms-", "ext-ms-")) or any((d / name).exists() for d in system)

    out: dict[str, list[str]] = {}
    seen: set[str] = set()
    todo = [(r, "WeasyPrint") for r in roots]
    while todo:
        name, user = todo.pop()
        key = name.lower()
        if key not in found:
            if not in_windows(name):
                out.setdefault(name, [])
                if user not in out[name]:
                    out[name].append(user)
            continue
        if key in seen:
            continue
        seen.add(key)
        try:
            deps = imports(found[key])
        except (OSError, ValueError, struct.error):
            out.setdefault(name, []).append("(file is damaged)")
            continue
        todo.extend((d, found[key].name) for d in deps)
    first = lambda u: (u != "WeasyPrint", u.lower())          # noqa: E731
    return {k: sorted(v, key=first) for k, v in sorted(out.items())}


def dll_folders() -> list[Path]:
    """The folders WEASYPRINT_DLL_DIRECTORIES names (';'-separated, as WeasyPrint reads it)."""
    raw = os.environ.get("WEASYPRINT_DLL_DIRECTORIES", "")
    return [Path(p) for p in raw.split(";") if p.strip()]
