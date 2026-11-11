"""Filesystem sandbox: address resolution, escapes, modes, scopes, copy/move/zip/delete."""
import json
import os
import sys
from pathlib import Path

import pytest

from portwright.config import ProjectConfig
from portwright.fs import FsError, Sandbox


class Audit:
    def __init__(self):
        self.events = []

    def record(self, **e):
        self.events.append(e)


@pytest.fixture
def env(tmp_path):
    share = tmp_path / "share"
    share.mkdir()
    proj = tmp_path / "proj"
    (proj / "reports" / "output").mkdir(parents=True)
    (proj / "data").mkdir()
    (proj / "inbox").mkdir()
    (proj / "reports" / "output" / "a.pdf").write_bytes(b"%PDF-a")
    (proj / "reports" / "output" / "b.xlsx").write_bytes(b"xlsx-b")
    (proj / "data" / "in.csv").write_text("x\n1\n")
    (proj / "inbox" / "note.txt").write_text("hello")
    cfg = ProjectConfig.model_validate({
        "security": {"allowed_folders": [str(share)], "audit_log": None},
        "filesystem": {"roots": {"inbox": {"path": "inbox", "mode": "read_write"},
                                 "pdfonly": {"path": "reports/output", "mode": "read_write", "patterns": ["*.pdf"]}},
                       "max_file_mb": 1},
        "destinations": {"finance": {"path": str(share), "overwrite": "rename"}},
    })
    audit = Audit()
    return {"cfg": cfg, "proj": proj, "share": share, "audit": audit,
            "sb": Sandbox(cfg, proj, audit=audit, actor="test")}


# --------------------------------------------------------------------------- resolve
def test_addresses_resolve(env):
    sb, proj, share = env["sb"], env["proj"], env["share"]
    assert sb.resolve("reports:a.pdf").path == (proj / "reports/output/a.pdf").resolve()
    assert sb.resolve("data:in.csv").path == (proj / "data/in.csv").resolve()
    pl = sb.resolve("dest:finance/2026-11")
    assert pl.path == (share / "2026-11").resolve() and pl.kind == "dest" and pl.address == "dest:finance/2026-11"
    assert sb.resolve("dest:finance").path == share.resolve()
    # absolute and project-relative paths are accepted inside a root (most specific root wins)
    assert sb.resolve(str(proj / "reports/output/a.pdf")).name == "reports"   # same folder: first root wins
    assert sb.resolve("inbox/note.txt").name == "inbox"
    assert sb.resolve({"path": str(proj / "reports/output/b.xlsx"), "format": "xlsx"}).path.name == "b.xlsx"


@pytest.mark.parametrize("bad, msg", [
    ("reports:../secret.txt", r"'\.\.'"),
    ("reports:a/../../x", r"'\.\.'"),
    ("reports:a.pdf:stream", "alternate data streams"),
    ("reports:CON", "reserved device name"),
    ("reports:nul.txt", "reserved device name"),
    ("reports:trailing.", "end with a dot"),
    ("reports:sp /x", "end with a dot or a space"),
    ("reports:a\x00b", "NUL"),
    ("reports:a?b", "not allowed"),
    ("nosuch:x", "unknown filesystem root"),
    ("dest:nope/x", "unknown destination"),
])
def test_rejects_bad_addresses(env, bad, msg):
    with pytest.raises(FsError, match=msg):
        env["sb"].resolve(bad)


def test_paths_outside_every_root_are_refused(env, tmp_path):
    (tmp_path / "elsewhere.txt").write_text("x")
    with pytest.raises(FsError, match="not inside any"):
        env["sb"].resolve(str(tmp_path / "elsewhere.txt"))
    with pytest.raises(FsError, match="not inside any"):
        env["sb"].resolve("portwright.yaml")


@pytest.mark.skipif(sys.platform == "win32" and not os.environ.get("CI_SYMLINKS"),
                    reason="symlinks need developer mode on Windows; junction test covers it")
def test_symlink_escape_is_blocked(env, tmp_path):
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "s.txt").write_text("s")
    os.symlink(tmp_path / "outside", env["proj"] / "inbox" / "link", target_is_directory=True)
    with pytest.raises(FsError, match="outside"):
        env["sb"].resolve("inbox:link/s.txt")
    assert all(e["name"] != "link" or not e["is_dir"] for e in env["sb"].list("inbox:"))


@pytest.mark.skipif(sys.platform != "win32", reason="NTFS junctions")
def test_junction_escape_is_blocked(env, tmp_path):
    import subprocess
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "s.txt").write_text("s")
    r = subprocess.run(["cmd", "/c", "mklink", "/J", str(env["proj"] / "inbox" / "j"), str(tmp_path / "outside")],
                       capture_output=True)
    if r.returncode:
        pytest.skip("could not create a junction")
    with pytest.raises(FsError, match="outside"):
        env["sb"].resolve("inbox:j/s.txt")
    assert "j" not in [e["name"] for e in env["sb"].list("inbox:", recursive=True)]


def test_config_rejects_unlisted_absolute_and_dotdot(tmp_path):
    with pytest.raises(ValueError, match="allowed_folders"):
        ProjectConfig.model_validate({"destinations": {"x1": {"path": str(tmp_path)}}})
    with pytest.raises(ValueError, match=r"'\.\.'"):
        ProjectConfig.model_validate({"filesystem": {"roots": {"up": {"path": "../x"}}}})
    with pytest.raises(ValueError, match="2-64"):
        ProjectConfig.model_validate({"filesystem": {"roots": {"C": {"path": "x"}}}})
    with pytest.raises(ValueError, match="reserved"):
        ProjectConfig.model_validate({"filesystem": {"roots": {"dest": {"path": "x"}}}})


# --------------------------------------------------------------------------- modes / scopes
def test_modes_and_patterns(env):
    sb = env["sb"]
    with pytest.raises(FsError, match="read-only"):
        sb.write_text("data:new.txt", "x")
    with pytest.raises(FsError, match="write-only"):
        sb.read_text("dest:finance/x.txt")
    with pytest.raises(FsError, match="patterns"):
        sb.write_text("pdfonly:x.txt", "x")
    assert sb.write_text("pdfonly:x.pdf", "x")["action"] == "written"


def test_scopes(env):
    sb = Sandbox(env["cfg"], env["proj"], scopes=["fs:reports:read", "fs:dest:finance"], audit=Audit())
    assert sb.read_text("reports:a.pdf")["content"] == "%PDF-a"
    sb.transfer(["reports:a.pdf"], "dest:finance")
    with pytest.raises(FsError, match="missing scope fs:inbox:read"):
        sb.read_text("inbox:note.txt")
    with pytest.raises(FsError, match="missing scope fs:reports:write"):
        sb.write_text("reports:x.txt", "x")
    with pytest.raises(FsError, match="missing scope fs:reports:write"):
        sb.transfer(["reports:b.xlsx"], "dest:finance", mode="move")
    assert (env["proj"] / "reports/output/b.xlsx").exists()
    assert Sandbox(env["cfg"], env["proj"], scopes=["*"], audit=Audit()).read_text("inbox:note.txt")["content"] == "hello"


# --------------------------------------------------------------------------- copy / move
def test_copy_with_rename_verify_and_manifest(env):
    sb, share = env["sb"], env["share"]
    outs = [{"path": str(env["proj"] / "reports/output/a.pdf"), "format": "pdf"}, "reports:b.xlsx"]
    res = sb.transfer({"outputs": outs}, "dest:finance/2026-11", manifest=True)
    assert [r["action"] for r in res] == ["copied", "copied"]
    assert (share / "2026-11" / "a.pdf").read_bytes() == b"%PDF-a"
    assert len(res[0]["sha256"]) == 64 and not list((share / "2026-11").glob("*.partial"))
    man = json.loads((share / "2026-11" / "manifest.json").read_text())
    assert [f["name"] for f in man["files"]] == ["a.pdf", "b.xlsx"]
    # destination default policy is 'rename'
    again = sb.transfer(["reports:a.pdf"], "dest:finance/2026-11")
    assert again[0]["renamed"] and Path(again[0]["dest"]).name == "a (1).pdf"
    assert [e["event"] for e in env["audit"].events].count("fs_copy") == 3


def test_overwrite_policies(env):
    sb = env["sb"]
    sb.transfer(["reports:a.pdf"], "inbox:out")
    with pytest.raises(FsError, match="already exists"):
        sb.transfer(["reports:a.pdf"], "inbox:out")                    # roots default to 'never'
    assert sb.transfer(["reports:a.pdf"], "inbox:out", overwrite="always")[0]["replaced"]
    assert sb.transfer(["reports:a.pdf"], "inbox:out", overwrite="if_newer")[0]["action"] == "skipped"
    with pytest.raises(FsError, match="create_dirs is off"):
        sb.transfer(["reports:a.pdf"], "inbox:missing/dir", create_dirs=False)
    with pytest.raises(FsError, match="is a file"):
        sb.transfer(["reports:a.pdf"], "inbox:note.txt")


def test_move_removes_source_only_after_copy(env):
    sb, proj, share = env["sb"], env["proj"], env["share"]
    res = sb.transfer(["inbox:note.txt"], "dest:finance")
    assert res[0]["action"] == "copied" and (proj / "inbox/note.txt").exists()
    (proj / "inbox" / "m.txt").write_text("move me")
    res = sb.transfer(["inbox:m.txt"], "dest:finance", mode="move")
    assert res[0]["action"] == "moved" and not (proj / "inbox/m.txt").exists()
    assert (share / "m.txt").read_text() == "move me"
    with pytest.raises(FsError, match="read-only"):
        sb.transfer(["data:in.csv"], "dest:finance", mode="move")
    assert (proj / "data/in.csv").exists()


def test_move_keeps_copy_when_source_delete_fails(env, monkeypatch):
    sb, proj = env["sb"], env["proj"]
    (proj / "inbox" / "locked.txt").write_text("x")
    real_unlink = Path.unlink

    def unlink(self, *a, **kw):
        if self.name == "locked.txt":
            raise PermissionError(13, "in use")
        return real_unlink(self, *a, **kw)

    monkeypatch.setattr(Path, "unlink", unlink)
    res = sb.transfer(["inbox:locked.txt"], "dest:finance", mode="move")
    assert res[0]["action"] == "copied" and "could not be removed" in res[0]["warning"]
    assert (env["share"] / "locked.txt").exists() and (proj / "inbox/locked.txt").exists()


def test_failed_verify_leaves_nothing(env, monkeypatch):
    from portwright.fs import sandbox as mod
    monkeypatch.setattr(mod, "sha256_file", lambda p: "0" * 64)
    with pytest.raises(FsError, match="verification failed"):
        env["sb"].transfer(["reports:a.pdf"], "dest:finance")
    assert not list(env["share"].iterdir())


def test_size_limit(env):
    big = env["proj"] / "inbox" / "big.bin"
    big.write_bytes(b"0" * (1024 * 1024 + 1))
    with pytest.raises(FsError, match="limit"):
        env["sb"].transfer(["inbox:big.bin"], "dest:finance")


# --------------------------------------------------------------------------- other ops
def test_write_read_list_mkdir_zip_delete(env):
    sb, proj = env["sb"], env["proj"]
    w = sb.write_text("inbox:notes/a.json", {"k": 1})
    assert w["action"] == "written" and json.loads((proj / "inbox/notes/a.json").read_text()) == {"k": 1}
    assert sb.read_text("inbox:notes/a.json")["content"].startswith("{")
    assert sb.read_text("reports:a.pdf").get("content") == "%PDF-a"
    (proj / "reports/output/bin.dat").write_bytes(b"\xff\xfe\x00\x80")
    assert sb.read_text("reports:bin.dat")["binary"] is True
    assert sb.mkdir("inbox:x/y")["address"] == "inbox:x/y"
    names = [e["name"] for e in sb.list("inbox:", recursive=True)]
    assert {"note.txt", "a.json", "y"} <= set(names)
    z = sb.zip(["reports:a.pdf", "reports:b.xlsx"], "inbox:archive/out.zip")
    import zipfile
    assert sorted(zipfile.ZipFile(z["path"]).namelist()) == ["a.pdf", "b.xlsx"]
    with pytest.raises(FsError, match=r"\.zip"):
        sb.zip(["reports:a.pdf"], "inbox:archive/out.tar")
    d = sb.delete(["inbox:note.txt"])
    assert d[0]["action"] == "trashed" and not (proj / "inbox/note.txt").exists()
    assert list((proj / ".portwright" / "trash").rglob("note.txt"))
    with pytest.raises(FsError, match="root 'inbox' itself"):
        sb.delete("inbox:")
