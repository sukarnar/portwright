"""Studio load editor with an AD group target (headless: a fake page, synchronous background work)."""
import flet as ft

from portwright.studio.views.loads import LoadEditor, LoadsView
from portwright.studio.views.sources import SourcesView

from test_ad_groups import ad, members, svc  # noqa: F401  (fixtures)


class FakeCtx:
    def __init__(self, svc):
        self.svc, self.toasts, self.dialogs, self.errors = svc, [], [], []
        self.page = type("P", (), {"update": lambda self: None})()
        self.help = lambda *a, **k: None

    def background(self, work, done, bar=None):
        done(work())

    def toast(self, msg, error=False):
        (self.errors if error else self.toasts).append(msg)

    def fail(self, exc):
        self.errors.append(str(exc))

    def dialog(self, title, body, actions, **kw):
        self.dialogs.append((title, body, actions))

    def close(self):
        pass

    def confirm(self, title, text, on_yes, **kw):
        self.dialogs.append((title, text))
        on_yes()


def _texts(control, out=None):
    out = [] if out is None else out
    if isinstance(control, ft.Text) and control.value:
        out.append(str(control.value))
    for attr in ("controls", "content", "title", "subtitle", "label"):
        v = getattr(control, attr, None)
        for c in (v if isinstance(v, list) else [v]):
            if isinstance(c, ft.Control):
                _texts(c, out)
    if isinstance(control, ft.DataTable):
        for r in control.rows:
            for c in r.cells:
                _texts(c.content, out)
    return out


def test_group_editor_new_save_preview_run(svc, ad):  # noqa: F811
    conn, _ = ad
    ctx = FakeCtx(svc)
    view = LoadsView(ctx)
    view.refresh = lambda update=True: None
    ed = LoadEditor(view, None)
    assert ed.target_kind == "table"                      # a SQL source exists, so tables come first
    ed.f_tkind.selected = ["group"]
    ed._tkind_changed(type("E", (), {"control": ed.f_tkind})())
    assert ed.f_to.value == "corp_ad" and "Active Directory" in ed.f_to.label
    ed.from_kind = "endpoint"
    ed._fill_from_options()
    ed.f_from.value = "active-staff"
    ed.f_name.value, ed.f_group.value, ed.f_email.value = "hr-group", "HR-Staff", "email"
    ed.f_maxrem.value, ed.f_rejects.value, ed.f_protected.value = "100", "10", "svc_keep"
    ed.read_columns()
    assert ed.inspection.group["members"] == 5
    page = " ".join(_texts(ed.render()))
    assert "e-mail column 'email' found" in page and "How members are changed" in page
    assert "Column mapping" not in page
    ed.browse_groups()
    assert "HR-Staff" in " ".join(_texts(ctx.dialogs[-1][1][-1]))
    ed.preview()
    out = " ".join(_texts(ft.Column(ed.results.controls)))
    assert "would add 1 and remove 2" in out and "bob" in out and "old1" in out, out
    ed.save()
    assert not ctx.errors, ctx.errors
    spec = svc.loads.get("hr-group")
    assert spec.to.group == "HR-Staff" and spec.ad.protected == ["svc_keep"] and spec.mode == "sync"
    view.run("hr-group")
    assert "REMOVED" in ctx.dialogs[-3][1]
    title, body, _ = ctx.dialogs[-1]
    assert title.startswith("Loaded with rejected") and "Member changes" in " ".join(
        t for b in body for t in _texts(b))
    assert set(members(conn)) == {"HR-Sub", "ann", "bob", "svc_keep"}
    again = LoadEditor(view, "hr-group")                  # reopen: settings survive
    assert again.target_kind == "group" and again.f_group.value == "HR-Staff" and not again.is_dirty


def test_sources_form_has_ldap(svc):  # noqa: F811
    ctx = FakeCtx(svc)
    v = SourcesView(ctx)
    assert "ldap" in __import__("portwright.studio.viewmodels", fromlist=["x"]).SOURCE_TYPES
    assert v is not None


def test_sources_dialog_adds_ldap(svc):  # noqa: F811
    ctx = FakeCtx(svc)
    v = SourcesView(ctx)
    v.refresh = lambda update=True: None
    v.edit_dialog()
    _, body, actions = ctx.dialogs[-1]
    f_name, f_type = body[0], body[1]
    ldap = [g for g in body if isinstance(g, ft.Column) and any(
        getattr(c, "label", "") == "Base DN *" for c in g.controls)][0]
    f_name.value, f_type.value = "ad3", "ldap"
    by_label = {getattr(c, "label", None): c for c in ldap.controls}
    by_label["Domain controllers (one per line)"].value = "ldaps://dc01.corp.local\nldaps://dc02.corp.local"
    by_label["Base DN *"].value = "DC=corp,DC=local"
    by_label["Service account (credential, type basic)"].value = "ad_svc"
    actions[-1].on_click(None)
    assert not ctx.errors, ctx.errors
    s = svc.project.config.sources["ad3"]
    assert s.type == "ldap" and s.servers == ["ldaps://dc01.corp.local", "ldaps://dc02.corp.local"]
    assert ctx.dialogs[-1][0] == "Next steps"
