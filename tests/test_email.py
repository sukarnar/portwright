"""Email steps and `portwright mail`: allow-lists, TLS/login rules, attachments through the sandbox, audit."""
import asyncio
import email
import json
from email import policy

import pytest
from typer.testing import CliRunner

from portwright.agents.workflow import run_workflow
from portwright.cli import app
from portwright.config import MailServerSpec, ProjectConfig
from portwright.mail import MailError, Mailer, addresses
from portwright.services import Project, Services
from portwright.studio import graph_vm as gv


class FakeSmtp:
    """Records what a real smtplib connection would be asked to do."""
    sent: list = []
    log: list = []

    def __init__(self, kind, host, port, timeout, ctx, starttls=True, refuse=()):
        self.kind, self.host, self.port, self.offers_tls, self.refuse = kind, host, port, starttls, refuse
        FakeSmtp.log.append(("connect", kind, host, port))

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def ehlo(self):
        FakeSmtp.log.append(("ehlo",))

    def has_extn(self, name):
        return self.offers_tls

    def starttls(self, context=None):
        FakeSmtp.log.append(("starttls", context is not None))

    def login(self, user, password):
        FakeSmtp.log.append(("login", user, password))

    def send_message(self, msg, from_addr=None, to_addrs=None):
        FakeSmtp.sent.append((msg, from_addr, list(to_addrs)))
        return {a: (550, b"no") for a in to_addrs if a in self.refuse}


@pytest.fixture(autouse=True)
def _reset():
    FakeSmtp.sent, FakeSmtp.log = [], []


def factory(**kw):
    return lambda kind, host, port, timeout, ctx: FakeSmtp(kind, host, port, timeout, ctx, **kw)


@pytest.fixture
def svc(tmp_path):
    s = Services(Project.init(tmp_path, "t"))
    s.credentials.add("smtp_login", "basic", {"username": "svc-reports", "password": "s3cret"})
    (tmp_path / "data" / "staff.csv").write_text("name,dept\nAnn,ENG\nBob,OPS\n")
    s.sources.add("staff", "file", path="staff.csv")
    cfg = s.project.config.model_dump(mode="json", by_alias=True)
    cfg["mail_servers"] = {"corp": {"host": "smtp.corp.local", "from": "reports@corp.local",
                                    "credential": "smtp_login", "allowed_recipients": ["*@corp.local",
                                                                                       "boss@partner.com"]}}
    cfg["security"]["allowed_mail_hosts"] = ["smtp.corp.local"]
    cfg["reports"] = [{"name": "staff", "from": {"source": "staff"}, "formats": ["csv", "json"]}]
    s.project.config = ProjectConfig.model_validate(cfg)
    s.project.save(force=True)
    return s


def workflow(svc, step: dict, scopes):
    cfg = svc.project.config.model_dump(mode="json", by_alias=True)
    cfg["workflows"] = {"wf": {"scopes": scopes, "steps": [
        {"id": "build", "type": "report", "report": "staff"},
        {"id": "send", "type": "email", "server": "corp", **step}]}}
    config = ProjectConfig.model_validate(cfg)
    return asyncio.run(run_workflow(config, svc.project.root, svc.project.credentials(), "wf", {"who": "ann@corp.local"},
                                    save=False, smtp_factory=factory()))


SEND = {"to": ["${input.who}", "Team <team@corp.local>"], "cc": "boss@partner.com", "bcc": "audit@corp.local",
        "subject": "Staff ${run.date.iso}", "body": "Rows: ${steps.build.output.row_count}",
        "files": "${steps.build.output.outputs}"}
SCOPES = ["reports:run", "mail:corp", "fs:reports:read"]


def test_report_emailed_with_attachments_over_starttls(svc, tmp_path):
    r = workflow(svc, SEND, SCOPES)
    assert r.ok, r.error
    out = r.output
    assert out["to"] == ["ann@corp.local", "team@corp.local"] and out["bcc"] == ["audit@corp.local"]
    assert [a["name"] for a in out["attachments"]] == ["staff.csv", "staff.json"]
    assert FakeSmtp.log[:4] == [("connect", "plain", "smtp.corp.local", 587), ("ehlo",), ("starttls", True), ("ehlo",)]
    assert ("login", "svc-reports", "s3cret") in FakeSmtp.log
    msg, sender, rcpts = FakeSmtp.sent[0]
    assert sender == "reports@corp.local"
    assert rcpts == ["ann@corp.local", "team@corp.local", "boss@partner.com", "audit@corp.local"]
    parsed = email.message_from_bytes(msg.as_bytes(), policy=policy.default)
    assert parsed["Bcc"] is None and parsed["Cc"] == "boss@partner.com"       # bcc is never in the headers
    assert parsed["Subject"].startswith("Staff 20") and parsed["Auto-Submitted"] == "auto-generated"
    assert parsed.get_body(("plain",)).get_content().strip() == "Rows: 2"
    files = {p.get_filename(): p.get_content() for p in parsed.iter_attachments()}
    assert b"Ann" in files["staff.csv"]
    audit = [json.loads(line) for line in (tmp_path / "logs/audit.jsonl").read_text().splitlines()]
    sent = [e for e in audit if e["event"] == "mail_sent"][-1]
    assert sent["subject"].startswith("Staff") and "s3cret" not in json.dumps(audit) and "Rows: 2" not in json.dumps(audit)
    assert {a["name"] for a in sent["attachments"]} == {"staff.csv", "staff.json"}


@pytest.mark.parametrize("step, scopes, error", [
    ({**SEND, "to": "eve@evil.example"}, SCOPES, "not an allowed recipient"),
    ({**SEND, "bcc": "eve@evil.example"}, SCOPES, "not an allowed recipient"),
    (SEND, ["reports:run", "fs:reports:read"], "missing scope mail:corp"),
    (SEND, ["reports:run", "mail:corp"], "missing scope fs:reports:read"),
    ({**SEND, "subject": "Hi\r\nBcc: eve@evil.example"}, SCOPES, "line break"),
    ({**SEND, "files": "C:/Windows/win.ini"}, SCOPES, "not inside any filesystem root"),
])
def test_email_step_refuses(svc, step, scopes, error):
    r = workflow(svc, step, scopes)
    assert not r.ok and error in r.error and not FakeSmtp.sent


def test_server_rules(svc, tmp_path):
    cfg = svc.project.config
    with pytest.raises(ValueError, match="unencrypted"):
        MailServerSpec.model_validate({"host": "x", "from": "a@corp.local", "credential": "c", "security": "none"})
    with pytest.raises(ValueError, match="not an email address"):
        MailServerSpec.model_validate({"host": "x", "from": "nobody"})
    relay = MailServerSpec.model_validate({"host": "relay", "from": "r@corp.local", "security": "none"})
    assert relay.effective_port() == 25 and relay.recipient_patterns() == ["*@corp.local"]   # From domain by default
    mailer = Mailer(cfg, tmp_path, svc.project.credentials(), smtp_factory=factory())
    cfg.security.allowed_mail_hosts = []
    with pytest.raises(MailError, match="not in security.allowed_mail_hosts"):
        mailer.send("corp", to="a@corp.local", subject="x")
    cfg.security.allowed_mail_hosts = ["smtp.corp.local"]
    no_tls = Mailer(cfg, tmp_path, svc.project.credentials(), smtp_factory=factory(starttls=False))
    with pytest.raises(MailError, match="does not offer STARTTLS"):
        no_tls.send("corp", to="a@corp.local", subject="x")
    refused = Mailer(cfg, tmp_path, svc.project.credentials(), smtp_factory=factory(refuse={"b@corp.local"}))
    with pytest.raises(MailError, match="refused: b@corp.local"):
        refused.send("corp", to="a@corp.local, b@corp.local", subject="x")
    before = len(FakeSmtp.sent)
    dry = mailer.send("corp", to="a@corp.local", subject="x", dry_run=True, save_to=tmp_path / "m.eml")
    assert not dry.sent and len(FakeSmtp.sent) == before                    # a dry run never connects
    assert b"Subject: x" in (tmp_path / "m.eml").read_bytes()
    assert addresses(["a@x.com; B <b@y.com>", {"email": "c@z.com"}, "a@x.com"]) == ["a@x.com", "b@y.com", "c@z.com"]


def test_config_checks_email_steps():
    base = {"mail_servers": {"corp": {"host": "h", "from": "a@corp.local"}}}
    with pytest.raises(ValueError, match="unknown mail server 'ghost'"):
        ProjectConfig.model_validate({**base, "workflows": {"w": {"steps": [
            {"id": "s", "type": "email", "server": "ghost", "to": "a@corp.local", "subject": "x"}]}}})
    with pytest.raises(ValueError, match="needs to, subject"):
        ProjectConfig.model_validate({**base, "workflows": {"w": {"steps": [
            {"id": "s", "type": "email", "server": "corp"}]}}})
    with pytest.raises(ValueError, match="one folder, not a list"):
        ProjectConfig.model_validate({"workflows": {"w": {"steps": [
            {"id": "s", "type": "copy", "files": "a", "to": ["x", "y"]}]}}})


def test_cli_mail_add_list_test_send(svc, tmp_path, monkeypatch):
    import portwright.mail as mail
    monkeypatch.setattr(mail.smtplib, "SMTP", lambda host, port, timeout=None: FakeSmtp("plain", host, port, timeout, None))
    cfg, runner = str(svc.project.path), CliRunner()
    r = runner.invoke(app, ["mail", "add", "hq", "--host", "mail.corp.local", "--from", "pw@corp.local",
                            "--credential", "smtp_login", "-c", cfg])
    assert r.exit_code == 0, r.output
    loaded = ProjectConfig.load(cfg)
    assert "mail.corp.local" in loaded.security.allowed_mail_hosts
    assert "hq" in runner.invoke(app, ["mail", "list", "-c", cfg]).output
    r = runner.invoke(app, ["mail", "test", "hq", "--to", "me@corp.local", "-c", cfg])
    assert r.exit_code == 0, r.output
    assert FakeSmtp.sent[-1][2] == ["me@corp.local"]
    (tmp_path / "reports" / "output").mkdir(parents=True, exist_ok=True)
    (tmp_path / "reports" / "output" / "x.pdf").write_bytes(b"%PDF-1.4 test")
    r = runner.invoke(app, ["mail", "send", "hq", "--to", "me@corp.local", "-s", "Report", "-a",
                            "reports:x.pdf", "--json", "-c", cfg])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["attachments"][0]["name"] == "x.pdf"
    r = runner.invoke(app, ["mail", "send", "hq", "--to", "eve@evil.example", "-s", "x", "-c", cfg])
    assert r.exit_code == 1 and "not an allowed recipient" in r.output
    assert runner.invoke(app, ["mail", "remove", "hq", "-c", cfg]).exit_code == 0


def test_canvas_email_node_checks():
    g = gv.Graph()
    n = gv.add_node(g, "email")
    problems = " ".join(gv.problems(g, [], []))
    assert "choose the mail server" in problems and "who gets the message" in problems
    n.settings = "server: corp\nto: [a@corp.local]\nsubject: Hi\nfiles: ${steps.r.output.outputs}"
    assert not [p for p in gv.problems(g, [], []) if n.id in p]
    step = gv.to_steps(g)[0]
    assert step["type"] == "email" and step["to"] == ["a@corp.local"] and step["server"] == "corp"
