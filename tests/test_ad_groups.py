"""Group loads into Active Directory, against an ldap3 mock directory."""
import sqlite3

import pytest
from ldap3 import MOCK_SYNC, MODIFY_REPLACE, Connection, Server

from portwright.directory.base import DirEntry, classify, escape
from portwright.directory.ldap import LdapDirectory
from portwright.directory.powershell import PowerShellDirectory
from portwright.services import Project, ServiceError, Services

BASE = "DC=corp,DC=local"
GROUP = "CN=HR-Staff,OU=Groups,DC=corp,DC=local"


def _user(conn, cn, **kw):
    dn = f"CN={cn},OU=Users,{BASE}"
    a = {"objectClass": ["top", "person", "organizationalPerson", "user"], "objectCategory": "person",
         "sAMAccountName": cn, "distinguishedName": dn, "cn": cn, "userAccountControl": 512}
    a.update(kw)
    conn.strategy.add_entry(dn, a)
    return dn


@pytest.fixture
def ad():
    c = Connection(Server("fake"), user=f"CN=svc,{BASE}", password="pw", client_strategy=MOCK_SYNC,
                   raise_exceptions=True)
    c.strategy.add_entry(f"CN=svc,{BASE}", {"userPassword": "pw", "objectClass": ["user"]})
    dns = {
        "ann": _user(c, "ann", mail="ann@corp.com"),
        "bob": _user(c, "bob", userPrincipalName="Bob@corp.com"),
        "cara": _user(c, "cara", mail="cara@corp.com", userAccountControl=514),          # disabled
        "dup1": _user(c, "dup1", mail="dup@corp.com"),
        "dup2": _user(c, "dup2", mail="dup@corp.com"),
        "old1": _user(c, "old1", mail="old1@corp.com"),
        "old2": _user(c, "old2", mail="old2@corp.com"),
        "keep": _user(c, "svc_keep", mail="keep@corp.com"),
    }
    sub = f"CN=HR-Sub,OU=Groups,{BASE}"
    c.strategy.add_entry(sub, {"objectClass": ["top", "group"], "cn": "HR-Sub", "sAMAccountName": "HR-Sub",
                               "distinguishedName": sub, "member": []})
    exc = f"CN=VIP,OU=Groups,{BASE}"
    c.strategy.add_entry(exc, {"objectClass": ["top", "group"], "cn": "VIP", "sAMAccountName": "VIP",
                               "distinguishedName": exc, "member": []})
    c.strategy.add_entry(GROUP, {"objectClass": ["top", "group"], "cn": "HR-Staff", "sAMAccountName": "HR-Staff",
                                 "distinguishedName": GROUP,
                                 "member": [dns["old1"], dns["old2"], dns["ann"], dns["keep"], sub]})
    c.bind()
    dns["sub"], dns["vip"] = sub, exc
    return c, dns


def members(conn):
    conn.search(GROUP, "(objectClass=*)", "BASE", attributes=["member"])
    return sorted(v.split(",")[0][3:] for v in conn.response[0]["attributes"]["member"])


@pytest.fixture
def svc(tmp_path, ad, monkeypatch):
    conn, _ = ad
    monkeypatch.setattr(LdapDirectory, "_connection", lambda self: conn)
    monkeypatch.setattr(LdapDirectory, "close", lambda self: None)
    s = Services(Project.init(tmp_path, "t"))
    db = tmp_path / "data" / "hr.db"
    c = sqlite3.connect(db)
    c.execute("create table staff(email text, status text)")
    c.executemany("insert into staff values (?, ?)", [
        ("ann@corp.com", "A"), (" BOB@corp.com ", "A"), ("cara@corp.com", "A"), ("dup@corp.com", "A"),
        ("nobody@corp.com", "A"), ("ann@corp.com", "A"), ("not-an-email", "A"), ("old2@corp.com", "T")])
    c.commit()
    s.credentials.add("db", "database", {"driver": "sqlite", "database": str(db)})
    s.credentials.add("ad_svc", "basic", {"username": "svc@corp.local", "password": "pw"})
    s.sources.add("hr", "sql", credential="db")
    s.endpoints.add("active-staff", "hr", "/staff/active",
                    query="SELECT email FROM staff WHERE status = 'A'")
    s.sources.add("corp_ad", "ldap", servers=["ldaps://dc01.corp.local"], base_dn=BASE, credential="ad_svc")
    s.root = tmp_path
    return s


def load(svc, **kw):
    d = {"name": "hr-group", "from": {"endpoint": "active-staff"},
         "to": {"source": "corp_ad", "group": "HR-Staff"}, "mode": "sync", "max_rejects": 10,
         "ad": {"email_column": "EMAIL", "max_remove_percent": 100}}
    for k, v in kw.items():
        if k == "ad":
            d["ad"] = {**d["ad"], **v}
        else:
            d[k] = v
    if svc.project.config.load_job("hr-group"):
        return svc.loads.save(d, original="hr-group")
    return svc.loads.save(d)


def test_sync_dry_run_then_run(svc, ad):
    conn, dns = ad
    load(svc)
    assert svc.loads.validate("hr-group").ok
    dry = svc.loads.run("hr-group", dry_run=True)
    assert dry.status == "dry_run" and (dry.added, dry.removed) == (1, 3)     # +bob; -old1 -old2 -svc_keep
    reasons = [r["reason"] for r in svc.loads.rejects(dry.run_id)[1]]
    assert len(reasons) == 4 and any("disabled" in x for x in reasons) and any("matches 2" in x for x in reasons)
    assert any("no AD user" in x for x in reasons) and any("not an e-mail" in x for x in reasons)
    assert dry.dropped == 1 and dry.kept == 1                                 # repeated ann; nested group kept
    assert members(conn) == ["HR-Sub", "ann", "old1", "old2", "svc_keep"]           # dry run changed nothing
    rep = svc.loads.run("hr-group")
    assert rep.status == "partial" and rep.exit_code == 2 and (rep.added, rep.removed) == (1, 3)
    assert set(members(conn)) == {"HR-Sub", "ann", "bob"}


def test_protected_exception_group_and_kill_fill(svc, ad):
    conn, dns = ad
    conn.modify(dns["vip"], {"member": [(MODIFY_REPLACE, [dns["old1"]])]})     # old1 is a VIP: never removed
    load(svc, mode="replace", ad={"protected": ["svc_keep"], "exception_group": "VIP"})
    rep = svc.loads.run("hr-group")
    assert rep.status == "partial" and rep.removed == 1 and rep.added == 1, rep.error
    assert set(members(conn)) == {"HR-Sub", "ann", "bob", "old1", "svc_keep"}


def test_add_only_and_safety_limits(svc, ad):
    conn, _ = ad
    load(svc, mode="insert")
    rep = svc.loads.run("hr-group")
    assert rep.removed == 0 and rep.added == 1
    assert set(members(conn)) == {"HR-Sub", "ann", "bob", "old1", "old2", "svc_keep"}
    load(svc, mode="sync", ad={"max_remove_percent": 20})
    rep = svc.loads.run("hr-group", force=True)
    assert rep.status == "failed" and "max_remove_percent" in rep.error and "not changed" in rep.error
    assert "old1" in members(conn)
    load(svc, mode="replace", safety={"min_rows": 10}, ad={"max_remove_percent": 100})
    rep = svc.loads.run("hr-group", force=True)
    assert rep.status == "failed" and "min_rows" in rep.error


def test_nested_groups_removable_and_unchanged_skip(svc, ad):
    conn, _ = ad
    load(svc, mode="sync", ad={"keep_nested_groups": False, "protected": ["keep@corp.com"]})
    rep = svc.loads.run("hr-group")
    assert "HR-Sub" not in members(conn) and "svc_keep" in members(conn), rep.error
    again = svc.loads.run("hr-group")
    assert again.status == "skipped" and again.exit_code == 3


def test_config_rules_and_service_helpers(svc, ad):
    with pytest.raises(ServiceError, match="sync.*AD group|AD group"):
        svc.loads.save({"name": "x", "from": {"endpoint": "active-staff"},
                        "to": {"source": "hr", "table": "staff"}, "mode": "sync"})
    with pytest.raises(ServiceError, match="ldap"):
        svc.loads.save({"name": "y", "from": {"endpoint": "active-staff"},
                        "to": {"source": "hr", "group": "HR-Staff"}, "mode": "sync"})
    with pytest.raises(ServiceError, match="not encrypted"):
        svc.sources.add("plain", "ldap", servers=["ldap://dc01"], base_dn=BASE, credential="ad_svc")
    assert svc.sources.test("corp_ad").ok
    assert [g["name"] for g in svc.sources.ad_groups("corp_ad", "HR")] == ["HR-Staff", "HR-Sub"]
    info = svc.sources.ad_group("corp_ad", "HR-Staff")
    assert info["members"] == 5 and info["kinds"] == {"user": 4, "group": 1}
    assert svc.sources.ad_user("corp_ad", "bob@corp.com")[0]["sam"] == "bob"      # via UPN
    ins = svc.loads.inspect(load(svc))
    assert ins.group["members"] == 5 and "email" in ins.header


def test_helpers_and_powershell_adapter():
    assert escape("a*b(c)\\") == "a\\2ab\\28c\\29\\5c"
    e = classify({"distinguishedName": "CN=x", "objectClass": "group"})
    assert e.kind == "group" and isinstance(e, DirEntry)
    calls = []

    def runner(req):
        calls.append(req)
        if req["op"] == "search":
            return [{"distinguishedName": ["CN=HR-Staff,DC=corp,DC=local"], "objectClass": ["group"],
                     "cn": ["HR-Staff"]}]
        if req["op"] == "members":
            return "CN=ann,DC=corp,DC=local"
        return "ok"

    class Spec:
        base_dn, servers = BASE, ["ldaps://dc01.corp.local:636"]
    d = PowerShellDirectory(Spec(), runner=runner)
    assert d.resolve_group("HR-Staff").dn.startswith("CN=HR-Staff")
    assert d.member_dns("CN=HR-Staff,DC=corp,DC=local") == ["CN=ann,DC=corp,DC=local"]
    d.replace_members("CN=HR-Staff,DC=corp,DC=local", ["CN=a", "CN=b"])
    assert calls[-1] == {"op": "replace", "server": "dc01.corp.local", "base": BASE,
                         "group": "CN=HR-Staff,DC=corp,DC=local", "dns": ["CN=a", "CN=b"]}


def test_cli_ad_group_load(svc, ad):
    from typer.testing import CliRunner
    from portwright.cli import app
    conn, _ = ad
    cfg = str(svc.project.path)
    r = CliRunner()
    out = r.invoke(app, ["source", "add", "ad2", "-t", "ldap", "--server", "ldaps://dc01.corp.local",
                         "--base-dn", BASE, "--credential", "ad_svc", "-c", cfg])
    assert out.exit_code == 0, out.output
    assert "HR-Staff" in r.invoke(app, ["source", "ad-groups", "ad2", "-s", "HR", "-c", cfg]).output
    assert "bob" in r.invoke(app, ["source", "ad-user", "ad2", "bob@corp.com", "-c", cfg]).output
    out = r.invoke(app, ["load", "create", "grp", "--from-endpoint", "active-staff", "--to", "ad2",
                         "--ad-group", "HR-Staff", "--mode", "sync", "--max-rejects", "10", "-c", cfg])
    assert out.exit_code == 0, out.output
    out = r.invoke(app, ["load", "map", "grp", "--max-remove-percent", "100", "--protect", "svc_keep", "-c", cfg])
    assert out.exit_code == 0 and "svc_keep" in out.output, out.output
    out = r.invoke(app, ["load", "run", "grp", "--dry-run", "-c", cfg])
    assert "would add=1" in out.output and "- old1" in out.output and "+ bob" in out.output, out.output
    out = r.invoke(app, ["load", "run", "grp", "-c", cfg])
    assert out.exit_code == 2, out.output                       # loaded, some rows rejected
    assert set(members(conn)) == {"HR-Sub", "ann", "bob", "svc_keep"}
    assert "added=1" in r.invoke(app, ["load", "history", "grp", "-c", cfg]).output
    assert "AD group" in r.invoke(app, ["load", "show", "grp", "-c", cfg]).output


def test_test_group_lists_members(svc, ad):
    r = svc.sources.test("corp_ad")
    assert r.ok and "set a test group" in r.message and not r.rows
    svc.sources.set_test_group("corp_ad", "HR-Staff")
    assert svc.project.config.sources["corp_ad"].test_group == "HR-Staff"
    r = svc.sources.test("corp_ad")
    assert r.ok and "5 member(s)" in r.message, r.message
    assert [x["account"] for x in r.rows] == ["ann", "old1", "old2", "svc_keep", "HR-Sub"]
    assert r.rows[-1]["kind"] == "group" and r.rows[-1]["status"] == "kept"
    svc.sources.set_test_group("corp_ad", "Nope")
    assert not svc.sources.test("corp_ad").ok
    from typer.testing import CliRunner
    from portwright.cli import app
    cfg = str(svc.project.path)
    out = CliRunner().invoke(app, ["source", "set-test-group", "corp_ad", "HR-Staff", "-c", cfg])
    assert out.exit_code == 0, out.output
    out = CliRunner().invoke(app, ["source", "test", "corp_ad", "-c", cfg])
    assert "5 member(s)" in out.output and "old1@corp.com" in out.output, out.output
    out = CliRunner().invoke(app, ["source", "ad-group", "corp_ad", "HR-Staff", "--members", "-c", cfg])
    assert "svc_keep" in out.output and "HR-Sub" in out.output, out.output


def test_endpoints_refuse_ldap_sources(svc):
    with pytest.raises(ServiceError, match="Active Directory source"):
        svc.endpoints.add("g", "corp_ad", "/g")
    with pytest.raises(ServiceError, match="Active Directory source"):
        svc.endpoints.save({"name": "g", "source": "corp_ad", "path": "/g"})
    from test_studio_ad_editor import FakeCtx
    from portwright.studio.views.endpoints import EndpointsView
    ctx = FakeCtx(svc)
    ctx.go = ctx.view = lambda *a: None
    v = EndpointsView(ctx)
    v.refresh = lambda update=True: None
    v.open_editor(None, source="corp_ad")
    assert v.editor is None and "Active Directory source" in ctx.errors[-1]
    v.open_editor(None)                                    # picks the sql source, never the AD one
    assert v.editor.f_source.value == "hr"
