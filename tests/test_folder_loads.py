"""Folder sources: files -> BLOB/CLOB rows or parsed records, ledger, version/replace, archive, Studio."""
import os
import sqlite3
import time

import pytest
from typer.testing import CliRunner

from portwright.cli import app
from portwright.services import Project, ServiceError, Services

OLD = time.time() - 3600


def put(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
    os.utime(path, (OLD, OLD))


@pytest.fixture
def proj(tmp_path):
    s = Services(Project.init(tmp_path / "p", "t"))
    db = s.project.root / "data" / "t.db"
    c = sqlite3.connect(db)
    c.execute("create table docs(id integer primary key, file_name varchar(200) not null, file_path varchar(400), "
              "sha varchar(64), content blob, body clob, run varchar(40))")
    c.execute("create table emp(id integer, name varchar(5) not null, src_file varchar(200))")
    c.commit()
    s.credentials.add("db", "database", {"driver": "sqlite", "database": str(db)})
    s.sources.add("t", "sql", credential="db", read_write=True)
    inbox = s.project.root / "data" / "inbox"
    put(inbox / "a.pdf", b"%PDF-1.4 \x00\xff\x10")
    put(inbox / "b.txt", "héllo – CLOB")
    put(inbox / "sub" / "c.txt", "nested")
    put(inbox / "~lock.txt", "skip me")
    s.sources.add("inbox", "folder", path="inbox", include=["*.pdf", "*.txt"], exclude=["~*"], recursive=True)
    s.loads.save({"name": "docs", "from": {"source": "inbox"}, "to": {"source": "t", "table": "docs"},
                  "columns": [{"target": "file_name", "source": "file_name"},
                              {"target": "file_path", "source": "file_path"},
                              {"target": "sha", "source": "sha256"},
                              {"target": "content", "source": "content", "type": "binary"},
                              {"target": "body", "source": "content_text", "type": "text"},
                              {"target": "run", "value": "${run_id}"}]})
    return s, c, inbox


def files_in(folder):
    return sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file())


def test_document_load_ledger_archive_and_versions(proj):
    s, c, inbox = proj
    assert s.sources.test("inbox").message.startswith("3 matching file(s)")
    ins = s.loads.inspect("docs")
    assert {t.name: t.type for t in ins.targets}["content"] == "binary" and "content_text" in ins.header
    dry = s.loads.run("docs", dry_run=True, preview=3)
    assert dry.status == "dry_run" and dry.files_loaded == 3 and dry.preview[0]["target"]["content"].endswith("bytes>")
    assert c.execute("select count(*) from docs").fetchone()[0] == 0 and len(files_in(inbox)) == 4
    rep = s.loads.run("docs")
    assert rep.status == "success" and rep.inserted == 3 and rep.exit_code == 0
    rows = dict(c.execute("select file_path, content from docs").fetchall())
    assert rows["a.pdf"] == b"%PDF-1.4 \x00\xff\x10"
    assert c.execute("select body from docs where file_name = 'b.txt'").fetchone()[0] == "héllo – CLOB"
    day = time.strftime("%Y-%m-%d")
    assert files_in(inbox) == sorted([f"processed/{day}/a.pdf", f"processed/{day}/b.txt",
                                      f"processed/{day}/sub/c.txt", "~lock.txt"])
    again = s.loads.run("docs")
    assert again.status == "skipped" and again.exit_code == 3
    # the same content back (even renamed) is skipped and tidied away; new content, same name -> new version
    put(inbox / "renamed.pdf", b"%PDF-1.4 \x00\xff\x10")
    put(inbox / "b.txt", "version 2")
    rep = s.loads.run("docs")
    st = {f["file"]: (f["status"], f["reason"]) for f in rep.files}
    assert st["renamed.pdf"][0] == "skipped" and "as a.pdf" in st["renamed.pdf"][1]
    assert st["b.txt"] == ("loaded", "new version")
    assert [r[0] for r in c.execute("select body from docs where file_name='b.txt' order by id")] == \
        ["héllo – CLOB", "version 2"]
    led = s.loads.ledger("docs")
    assert led[0]["path"] == "b.txt" and led[0]["action"] == "new version"
    # forget -> loaded again
    put(inbox / "c2.txt", "nested")                    # same content as sub/c.txt
    assert s.loads.folder_files("docs")["counts"] == {"skipped": 1}
    assert s.loads.forget("docs", "sub/c.txt") == 1
    st = s.loads.folder_files("docs")                  # the ledger forgot it, but the table still has it
    assert st["counts"] == {"skipped": 1} and "target table" in st["files"][0]["reason"]


def test_replace_target_check_limits_and_failures(proj, tmp_path):
    s, c, inbox = proj
    d = s.loads.as_dict("docs")
    d["files"] = {"on_changed": "replace", "file_key": "file_path", "after_load": "leave"}
    s.loads.save(d, original="docs")
    s.loads.run("docs")
    put(inbox / "b.txt", "replaced!")
    rep = s.loads.run("docs")
    assert rep.deleted == 1 and rep.inserted == 1
    assert [r[0] for r in c.execute("select body from docs where file_name='b.txt'")] == ["replaced!"]
    # replace needs a key
    d["files"] = {"on_changed": "replace"}
    with pytest.raises(ServiceError, match="file_key"):
        s.loads.save(d, original="docs")
    # target check: a file whose sha256 is already in the table is skipped (loaded by someone else)
    import hashlib
    c.execute("insert into docs(file_name, sha) values ('x', ?)", (hashlib.sha256(b"elsewhere").hexdigest(),))
    c.commit()
    put(inbox / "e.txt", "elsewhere")
    st = {f["file"]: f for f in s.loads.run("docs").files}
    assert st["e.txt"]["status"] == "skipped" and "target table" in st["e.txt"]["reason"]
    # too big and a database failure -> rejected folder with a reason, others still load
    s.sources.add("inbox", "folder", path="inbox", include=["*.txt", "*.bin"], max_file_mb=0.001, replace=True,
                  recursive=True)
    put(inbox / "big.bin", b"x" * 5000)
    d["files"] = {"after_load": "move"}
    s.loads.save(d, original="docs")
    rep = s.loads.run("docs")
    big = next(f for f in rep.files if f["file"] == "big.bin")
    assert big["status"] == "too_big" and big["moved_to"].startswith("rejected/")
    assert (inbox / big["moved_to"]).with_name("big.bin.reason.txt").exists()


def test_records_mode_and_bad_file(proj):
    s, c, _ = proj
    recs = s.project.root / "data" / "recs"
    put(recs / "e1.csv", "id,name\n1,ann\n2,bob\n")
    put(recs / "e2.csv", "id,name\n3,toolongname\n")
    put(recs / "e3.csv", b"id,name\n4,J\xf6rg\n")                  # Windows-1252, decoded automatically
    s.sources.add("recs", "folder", path="recs", include=["*.csv"])
    s.loads.save({"name": "emp", "from": {"source": "recs"}, "to": {"source": "t", "table": "emp"},
                  "files": {"read": "records"},
                  "columns": [{"target": "id", "source": "id", "type": "integer"}, {"target": "name", "source": "name"},
                              {"target": "src_file", "source": "file_name"}]})
    assert s.loads.inspect("emp").header[:3] == ["id", "name", "file_name"]
    rep = s.loads.run("emp")
    st = {f["file"]: f["status"] for f in rep.files}
    assert st == {"e1.csv": "loaded", "e2.csv": "failed", "e3.csv": "loaded"} and rep.exit_code == 2
    assert sorted(c.execute("select id, name, src_file from emp")) == [(1, "ann", "e1.csv"), (2, "bob", "e1.csv"),
                                                                       (4, "Jörg", "e3.csv")]
    assert rep.rejects_file and "e2.csv" in open(rep.rejects_file, encoding="utf-8").read()


def test_security_and_endpoints(proj, tmp_path):
    s, _, _ = proj
    outside = tmp_path / "share"
    put(outside / "x.xml", "<a/>")
    warn = s.sources.add("share", "folder", path=str(outside))
    assert "allowed_folders" in warn[0]
    assert not s.sources.test("share").ok
    s.sources.allow_folder(str(outside))
    assert s.sources.test("share").ok
    with pytest.raises(ServiceError, match="folder source"):
        s.endpoints.add("f", "share", "/f")
    s.sources.add("bad", "folder", path="../..")
    r = s.sources.test("bad")
    assert not r.ok and "outside the data folder" in r.message
    assert "sources.share.path" in s.migration.vars_template()


def test_cli_folder_commands(proj):
    s, c, inbox = proj
    cfg = str(s.project.path)
    r = CliRunner()
    out = r.invoke(app, ["load", "files", "docs", "-c", cfg])
    assert "3 new" in out.output, out.output
    out = r.invoke(app, ["load", "run", "docs", "-c", cfg])
    assert out.exit_code == 0 and "loaded=3" in out.output and "processed/" in out.output, out.output
    out = r.invoke(app, ["load", "files", "docs", "--ledger", "-c", cfg])
    assert "a.pdf" in out.output and "loaded" in out.output
    out = r.invoke(app, ["load", "map", "docs", "--on-changed", "replace", "--file-key", "file_path",
                         "--after-load", "leave", "-c", cfg])
    assert out.exit_code == 0, out.output
    assert "on_changed=replace" in r.invoke(app, ["load", "show", "docs", "-c", cfg]).output
    out = r.invoke(app, ["load", "forget", "docs", "a.pdf", "-c", cfg])
    assert "1 ledger entry removed" in out.output


def test_studio_folder_editor(proj):
    from test_studio_ad_editor import FakeCtx, _texts
    import flet as ft
    from portwright.studio.views.loads import LoadEditor, LoadsView
    s, c, inbox = proj
    ctx = FakeCtx(s)
    ctx.info = lambda title, body, width=640: ctx.dialogs.append((title, body, []))
    view = LoadsView(ctx)
    view.refresh = lambda update=True: None
    ed = LoadEditor(view, "docs")
    assert ed.is_folder
    page = " ".join(_texts(ed.render()))
    assert "2 · Files" in page and "Files in the folder" in page and "How rows are written" not in page
    ed.check_source()
    assert "3 new" in " ".join(_texts(ed.folder_line))
    ed.preview()
    assert "3 file(s) would be loaded" in " ".join(_texts(ft.Column(ed.results.controls)))
    ed.f_onchg.value, ed.f_filekey.value, ed.f_after.value = "replace", "file_path", "leave"
    ed.save()
    assert not ctx.errors and s.loads.get("docs").files.on_changed == "replace"
    view.run("docs")
    title, body, _ = ctx.dialogs[-1]
    assert title == "Load finished" and "a.pdf" in " ".join(t for b in body for t in _texts(b))
    ed.original = "docs"
    ed.show_ledger()
    assert "Files loaded by 'docs'" == ctx.dialogs[-1][0]
    # new editor: picking a folder source switches to the Files settings
    ed2 = LoadEditor(view, None)
    ed2.f_from.value = "inbox"
    assert ed2.is_folder and "Files" in " ".join(_texts(ed2.render()))
