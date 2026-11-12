"""Rebuild this folder from an MSYS2 install: WeasyPrint's DLLs, every DLL they depend on, and etc/fonts.

Run on a machine with MSYS2 and Pango installed (in the MINGW64 shell: pacman -S mingw-w64-x86_64-pango):

    python pango\\collect-pango.py C:\\msys64\\mingw64 pango

Then copy the folder to the offline server and point WeasyPrint at its bin folder, for example in the
project's .env:  WEASYPRINT_DLL_DIRECTORIES=C:\\apps\\portwright\\pango\\bin
Check with:  pw doctor   (PDF: weasyprint should show ✓). This script warns about any DLL it could not find.

Only DLLs found in MSYS2's bin folder are copied; Windows' own DLLs (System32, api-ms-*) are left out.
"""
from __future__ import annotations

import os
import shutil
import struct
import sys
from pathlib import Path

# What WeasyPrint opens by name (weasyprint/text/ffi.py); everything else is found by following imports.
ROOTS = ["libgobject-2.0-0.dll", "libpango-1.0-0.dll", "libpangoft2-1.0-0.dll", "libharfbuzz-0.dll",
         "libharfbuzz-subset-0.dll", "libfontconfig-1.dll"]


def imports(path: Path) -> list[str]:
    """The DLL names in a PE file's import table."""
    b = path.read_bytes()
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
        raise ValueError(f"{path.name}: bad import table")

    names, p = [], offset(imp_rva)
    while (name_rva := struct.unpack_from("<I", b, p + 12)[0]) != 0:
        n = offset(name_rva)
        names.append(b[n:b.index(b"\0", n)].decode("ascii"))
        p += 20
    return names


def main(mingw: Path, out: Path) -> int:
    src = mingw / "bin"
    system = Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32"
    by_lower = {p.name.lower(): p for p in src.glob("*.dll")}
    need: dict[str, Path] = {}
    unknown: dict[str, set[str]] = {}
    todo = list(ROOTS)
    while todo:
        name = todo.pop()
        key = name.lower()
        if key in need:
            continue
        if key not in by_lower:
            print(f"{name} is not in {src}: install Pango first (pacman -S mingw-w64-x86_64-pango)")
            return 1
        need[key] = by_lower[key]
        for dep in imports(by_lower[key]):
            d = dep.lower()
            if d in by_lower:
                todo.append(dep)
            elif not (d.startswith(("api-ms-", "ext-ms-")) or (system / dep).exists()):
                unknown.setdefault(dep, set()).add(name)
    bin_out = out / "bin"
    bin_out.mkdir(parents=True, exist_ok=True)
    for old in bin_out.glob("*.dll"):
        if old.name.lower() not in need:
            old.unlink()                                  # a leftover from an older set
    for p in sorted(need.values()):
        shutil.copy2(p, bin_out / p.name)
    shutil.copytree(mingw / "etc" / "fonts", out / "etc" / "fonts", dirs_exist_ok=True)
    size = sum(p.stat().st_size for p in bin_out.glob("*.dll"))
    print(f"{len(need)} DLLs ({size / 1048576:.1f} MB) and etc/fonts written to {out}")
    for dep, users in sorted(unknown.items()):
        print(f"warning: {dep} (needed by {', '.join(sorted(users))}) is in neither MSYS2 nor Windows")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(2)
    sys.exit(main(Path(sys.argv[1]), Path(sys.argv[2])))
