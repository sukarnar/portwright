"""Rebuild this folder from an MSYS2 install: WeasyPrint's DLLs, every DLL they depend on, and etc/fonts.

This is a BUILD step: run it on a machine that has MSYS2 with Pango installed (in the MINGW64 shell:
pacman -S mingw-w64-x86_64-pango), from the portwright folder:

    python pango\\collect-pango.py C:\\msys64\\mingw64 pango

Then copy the pango folder to the offline server and point WeasyPrint at its bin folder, e.g. in the
project's .env:  WEASYPRINT_DLL_DIRECTORIES=C:\\apps\\portwright\\pango\\bin
The offline server needs neither MSYS2 nor this script: check it there with 'pw doctor', which names any
DLL still missing.

Only DLLs found in MSYS2's bin folder are copied; Windows' own DLLs (System32, api-ms-*) are left out.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))    # the portwright folder, for its DLL reader
from portwright.reports.dlls import WEASYPRINT_DLLS, imports  # noqa: E402


def main(mingw: Path, out: Path) -> int:
    src = mingw / "bin"
    if not src.is_dir():
        print(f"{src} does not exist: give the MSYS2 mingw64 folder, e.g. C:\\msys64\\mingw64 "
              "(this script runs where MSYS2 is installed, not on the offline server)")
        return 1
    if out.resolve() / "bin" == src.resolve():
        print("the source and the target are the same folder")
        return 1
    system = Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32"
    by_lower = {p.name.lower(): p for p in src.glob("*.dll")}
    need: dict[str, Path] = {}
    unknown: dict[str, set[str]] = {}
    todo = list(WEASYPRINT_DLLS)
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
