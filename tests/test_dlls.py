"""Naming the DLLs a WeasyPrint DLL folder is missing (pw doctor on servers without MSYS2)."""
import shutil
import sys
from pathlib import Path

import pytest

from portwright.doctor import dll_checks
from portwright.reports.dlls import WEASYPRINT_DLLS, imports, missing

PANGO = Path(__file__).resolve().parent.parent / "pango" / "bin"
windows_bundle = pytest.mark.skipif(sys.platform != "win32" or not (PANGO / "libpango-1.0-0.dll").exists(),
                                    reason="needs Windows and the repository's pango/bin")


def test_imports_rejects_files_that_are_not_dlls(tmp_path):
    (tmp_path / "x.dll").write_bytes(b"not a dll")
    with pytest.raises(ValueError, match="not a Windows DLL"):
        imports(tmp_path / "x.dll")


@windows_bundle
def test_the_bundled_set_is_complete():
    assert "libglib-2.0-0.dll" in [d.lower() for d in imports(PANGO / "libpango-1.0-0.dll")]
    assert missing([PANGO]) == {}


@windows_bundle
def test_a_missing_dependency_is_named_with_who_needs_it(tmp_path):
    copy = tmp_path / "bin"
    shutil.copytree(PANGO, copy)
    (copy / "libglib-2.0-0.dll").unlink()
    (copy / "libfontconfig-1.dll").unlink()              # one WeasyPrint opens itself
    gone = missing([copy])
    assert set(gone) == {"libglib-2.0-0.dll", "libfontconfig-1.dll"}
    assert "libpango-1.0-0.dll" in gone["libglib-2.0-0.dll"]
    assert gone["libfontconfig-1.dll"] == ["WeasyPrint", "libpangoft2-1.0-0.dll"]   # opened directly, and a dependency
    (copy / "libffi-8.dll").write_bytes(b"truncated")
    assert missing([copy])["libffi-8.dll"] == ["(file is damaged)"]


@windows_bundle
def test_doctor_lists_them(tmp_path, monkeypatch):
    copy = tmp_path / "bin"
    shutil.copytree(PANGO, copy)
    (copy / "libthai-0.dll").unlink()
    monkeypatch.setenv("WEASYPRINT_DLL_DIRECTORIES", str(copy))
    [c] = dll_checks()
    assert c.name == "PDF: missing DLLs" and "libthai-0.dll (needed by libpango-1.0-0.dll)" in c.detail
    monkeypatch.setenv("WEASYPRINT_DLL_DIRECTORIES", str(PANGO))
    assert "yet it does not load" in dll_checks()[0].detail          # complete, so the cause is elsewhere
    monkeypatch.delenv("WEASYPRINT_DLL_DIRECTORIES")
    assert dll_checks() == []


def test_weasyprint_roots_are_listed():
    assert "libpango-1.0-0.dll" in WEASYPRINT_DLLS and len(WEASYPRINT_DLLS) == 6
