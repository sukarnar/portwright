"""Setup activity log, pw config, replay scripts and migration bundles."""
import json
import sqlite3
import zipfile

import pytest
from typer.testing import CliRunner

from portwright.cli import app
from portwright.services import Project, ServiceError, Services


def _project(path, name="hr-api", actor="cli"):
    s = Services(Project.init(path, name, actor=actor))
    db = path / "data" / "hr.db"
    c = sqlite3.connect(db)
    c.execute("create table staff(id integer primary key, email text, status text)")
    c.commit()
    (path / "data" / "t.csv").write_text("id,email\n1,a@x.com\n")
    return s, db


@pytest.fixture
def dev(tmp_path):
    s, db = _project(tmp_path / "dev", actor="studio")
    s.credentials.add("hr_db", "database", {"driver": "sqlite", "database": str(db)})
    s.credentials.add("ad_svc", "basic", {"username": "svc@corp.local", "password": "S3cret-pass!"})
    s.sources.add("hr", "sql", credential="hr_db", read_write=True)
    s.sources.add("trans", "file", path="t.csv")
    s.sources.add("corp_ad", "ldap", servers=["ldaps://devdc.corp.local"], base_dn="DC=corp,DC=local",
                  credential="ad_svc", test_group="CM.VAHN.SCCM")
    s.endpoints.add("staff", "hr", "/staff", query="SELECT email FROM staff WHERE status = 'A'", scopes=["hr:read"])
    s.loads.save({"name": "grp", "from": {"endpoint": "staff"}, "to": {"source": "corp_ad", "group": "CM.VAHN.SCCM"},
                  "mode": "sync"})
    (s.project.root / "handlers" / "calc.py").write_text("def f():\n    return 1\n")
    return s


def test_activity_log_records_everything_without_secrets(dev, tmp_path):
    log = dev.project.activity
    entries = log.read()
    kinds = [(e["kind"], e["name"], e["action"], e["via"]) for e in entries]
    assert ("project", "hr-api", "create", "baseline") in kinds
    assert ("credential", "ad_svc", "create", "studio") in kinds and ("source", "corp_ad", "create", "studio") in kinds
    assert ("load", "grp", "create", "studio") in kinds
    raw = log.path.read_text()
    assert "S3cret-pass!" not in raw                                  # secrets never logged
    cred = next(e for e in entries if e["name"] == "ad_svc")
    assert cred["spec"]["fields"] == {"username": "svc@corp.local"} and cred["spec"]["secret_fields"] == ["password"]
    assert cred["steps"][0]["args"][:5] == ["cred", "add", "ad_svc", "-t", "basic"]
    # an update lists the changed fields; a key shows its name; a hand edit is detected on open
    dev.sources.set_credential("hr", "ad_svc")
    upd = log.read()[-1]
    assert upd["action"] == "update" and upd["fields"] == ["credential"] and upd["summary"].endswith("credential")
    dev.access.create_key("app1", ["hr:read"])
    assert "API key 'app1'" in log.read()[-1]["summary"]
    p = dev.project.path
    p.write_text(p.read_text().replace("port: 8080", "port: 8099"))
    Project(p)                                                         # e.g. the next CLI command
    last = log.read()[-1]
    assert (last["via"], last["kind"], last["name"], last["fields"]) == ("file", "settings", "server", ["port"])
    assert len(Project(p).activity.read()) == len(log.read())          # not logged twice


def test_config_objects_get_put_remove_and_vars(dev):
    obj = dev.objects
    spec = obj.get("source", "corp_ad")
    assert spec["test_group"] == "CM.VAHN.SCCM"
    assert obj.put("source", "corp_ad", spec)[0] == "unchanged"
    action, used = obj.put("source", "corp_ad", spec, {"sources.corp_ad.servers": ["ldaps://prod.corp.local"],
                                                       "sources.hr.credential": "x"})
    assert action == "updated" and used == ["sources.corp_ad.servers"]
    assert dev.project.config.sources["corp_ad"].servers == ["ldaps://prod.corp.local"]
    assert obj.put("settings", "server", {"port": 9000})[0] == "updated" and dev.project.config.server.port == 9000
    with pytest.raises(ServiceError, match="source 'zz'"):
        obj.put("source", "zz", {"type": "sql"})                       # validated
    with pytest.raises(ServiceError, match="read by load"):
        dev.endpoints.remove("staff") if hasattr(dev.endpoints, "remove") else obj.remove("endpoint", "staff")
    assert obj.remove("endpoint", "nope", if_exists=True) is False
    ep = obj.get("endpoint", "staff")
    assert obj.put("endpoint", "staff2", {**ep, "path": "/staff2"})[0] == "created"


def test_replay_script_from_log_and_full(dev):
    text, n = dev.migration.script()
    assert n >= 6 and "S3cret" not in text
    i = {k: text.index(k) for k in ("cred add hr_db", "config put source hr", "config put endpoint staff",
                                    "config put load grp")}
    assert i["cred add hr_db"] < i["config put source hr"] < i["config put endpoint staff"] < i["config put load grp"]
    assert "Test-Credential" in text and "param(" in text and "@withVars" in text
    dev.sources.set_credential("trans", None) if False else None
    full, _ = dev.migration.script(full=True)
    assert "config put source corp_ad" in full and "test_group: CM.VAHN.SCCM" in full
    since, n2 = dev.migration.script(since="2999-01-01")
    assert n2 == 0 and "0 change(s)" in since


def test_export_plan_import_roundtrip(dev, tmp_path):
    path, manifest = dev.migration.export(tmp_path / "b.zip", include_data=True)
    assert manifest["counts"] == {"sources": 3, "endpoints": 1, "loads": 1, "credentials": 2, "handlers": 1,
                                  "data_files": 2}
    with zipfile.ZipFile(path) as z:
        allz = b"".join(z.read(n) for n in z.namelist())
        assert b"S3cret-pass!" not in allz and b"api_keys" not in z.read("setup/portwright.export.yaml")
        assert b"sources.corp_ad.servers" in z.read("vars.template.yaml")
    prod, _ = _project(tmp_path / "prod")
    variables = {"sources.corp_ad.servers": ["ldaps://proddc01.corp.local"], "server.port": 8443,
                 "credentials.hr_db.database": "C:/prod/hr.db", "sources.nope.x": 1}
    plan = prod.migration.plan(path, variables)
    acts = {(i.kind, i.name): i.action for i in plan.items}
    assert acts[("source", "corp_ad")] == "create" and acts[("settings", "server")] == "update"
    assert {c["name"]: c["status"] for c in plan.credentials} == {"ad_svc": "missing", "hr_db": "missing"}
    assert next(c for c in plan.credentials if c["name"] == "hr_db")["fields"]["database"] == "C:/prod/hr.db"
    files = {f["path"]: f["status"] for f in plan.files}
    assert files["handlers/calc.py"] == "new" and files["data/t.csv"] == "same"
    assert plan.unused_vars == ["sources.nope.x"]
    res = prod.migration.apply(path, variables)
    cfg = prod.project.config
    assert cfg.sources["corp_ad"].servers == ["ldaps://proddc01.corp.local"] and cfg.server.port == 8443
    assert res.backup and (tmp_path / "prod" / "handlers" / "calc.py").exists()
    assert "data/hr.db" not in res.files_written                     # existing data is not overwritten
    via = {e["via"] for e in prod.project.activity.read()}
    assert "migrate:b.zip" in via
    again = prod.migration.plan(path, variables)
    assert not again.changes                                          # idempotent
    # prune removes what the bundle does not have; settings can be kept
    prod.sources.add("extra", "file", path="t.csv")
    pr = prod.migration.plan(path, variables, prune=True, skip_settings=True)
    assert {(i.kind, i.name): i.action for i in pr.items}[("source", "extra")] == "remove"
    # a damaged bundle is refused
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(path) as z, zipfile.ZipFile(bad, "w") as w:
        for n in z.namelist():
            w.writestr(n, b"evil" if n == "handlers/calc.py" else z.read(n))
    with pytest.raises(ServiceError, match="damaged"):
        prod.migration.plan(bad)


def test_cli_activity_config_and_migrate(dev, tmp_path):
    r = CliRunner()
    cfg = str(dev.project.path)
    out = r.invoke(app, ["activity", "--kind", "source", "--commands", "-c", cfg])
    assert out.exit_code == 0 and "source 'corp_ad' created" in out.output and "pw config put source" in out.output
    out = r.invoke(app, ["config", "get", "load", "grp", "-c", cfg])
    assert "group: CM.VAHN.SCCM" in out.output
    vf = tmp_path / "v.yaml"
    vf.write_text("loads.grp.to.group: CM.PROD.SCCM\n")
    out = r.invoke(app, ["config", "put", "load", "grp", "--vars", str(vf), "-c", cfg], input=out.output)
    assert out.exit_code == 0 and "updated (variables: to.group)" in out.output, out.output
    out = r.invoke(app, ["migrate", "script", "-o", str(tmp_path / "s.ps1"), "-c", cfg])
    assert out.exit_code == 0 and (tmp_path / "s.ps1").read_bytes().startswith(b"\xef\xbb\xbf")
    bundle = tmp_path / "x.zip"
    assert r.invoke(app, ["migrate", "export", "-o", str(bundle), "-c", cfg]).exit_code == 0
    assert "server.port" in r.invoke(app, ["migrate", "vars", str(bundle)]).output
    target = tmp_path / "new" / "portwright.yaml"
    out = r.invoke(app, ["migrate", "import", str(bundle), "--init", "--yes", "--prompt-credentials",
                         "-c", str(target)], input="pw1\npw1\n")
    assert out.exit_code == 0, out.output
    assert "Imported" in out.output and "credential 'ad_svc' stored" in out.output
    t = Services.open(target)
    assert {c["name"] for c in t.credentials.list()} >= {"ad_svc", "hr_db"}
    assert t.credentials.get("ad_svc").fields["username"] == "svc@corp.local"
    out = r.invoke(app, ["migrate", "plan", str(bundle), "-c", str(target)])
    assert "0 change(s)" in out.output


def test_studio_activity_screen(dev, tmp_path):
    from test_studio_ad_editor import FakeCtx, _texts
    import flet as ft
    from portwright.studio.views.activity import ActivityView, filter_entries
    ctx = FakeCtx(dev)
    ctx.info = lambda title, body, width=640: ctx.dialogs.append((title, body, []))
    v = ActivityView(ctx)
    page = " ".join(_texts(v.build()))
    assert "source 'corp_ad' created" in page
    v.f_kind.value = "credential"
    v.refresh()
    assert "source 'corp_ad'" not in " ".join(_texts(v.root))
    e = [x for x in dev.project.activity.read() if x["name"] == "grp"][-1]
    v.show(e)
    assert "config put load grp" in " ".join(_texts(ft.Column(ctx.dialogs[-1][1])))
    v.script_dialog()
    _, body, actions = ctx.dialogs[-1]
    actions[-1].on_click(None)                                   # Save script
    assert list((dev.project.root / "migrations").glob("setup-*.ps1"))
    v.export_dialog()
    ctx.dialogs[-1][2][-1].on_click(None)                        # Export
    zips = list((dev.project.root / "migrations").glob("*.zip"))
    assert len(zips) == 1
    prod, _ = _project(tmp_path / "prod2")
    pctx = FakeCtx(prod)
    pctx.info = lambda title, body, width=640: pctx.dialogs.append((title, body, []))
    pv = ActivityView(pctx)
    pv.build()
    pv.plan_dialog()
    _, body, actions = pctx.dialogs[-1]
    body[1].value = str(zips[0])
    actions[1].on_click(None)                                    # Check
    assert "missing" in " ".join(_texts(ft.Column(body))) and not actions[2].disabled
    actions[2].on_click(None)                                    # Import (FakeCtx confirms)
    assert "corp_ad" in prod.project.config.sources and pctx.dialogs[-1][0] == "Bundle imported"
    assert filter_entries([{"via": "migrate:x.zip", "summary": "a"}], via="migrate")
