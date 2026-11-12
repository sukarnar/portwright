"""Send email (report deliveries) through a configured SMTP server.

Safety rules, in the order they are checked:

* the server is defined in ``mail_servers`` and its host is in ``security.allowed_mail_hosts``;
* every recipient (to, cc, bcc) matches the server's ``allowed_recipients`` (default: the From domain), and
  there are at most ``max_recipients``;
* headers may not contain line breaks (no header injection);
* attachments are read through the filesystem sandbox (roots and ``fs:<root>:read`` scopes) and together
  stay under ``max_attachments_mb``;
* a login is only sent over TLS (``starttls`` must succeed, or ``ssl``); certificates are verified, with an
  optional corporate CA bundle.

Every message is audited (``mail_sent``: server, recipients, subject, attachment names and SHA-256). The body
is never logged.
"""
from __future__ import annotations

import fnmatch
import hashlib
import mimetypes
import smtplib
import ssl
from dataclasses import asdict, dataclass, field
from email.message import EmailMessage
from email.utils import formatdate, getaddresses, make_msgid
from pathlib import Path
from typing import Any, Callable

from .config import MailServerSpec, ProjectConfig


class MailError(Exception):
    pass


@dataclass
class Attachment:
    name: str
    data: bytes
    source: str = ""                       # the sandbox address it was read from

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


@dataclass
class MailResult:
    server: str
    message_id: str
    to: list[str]
    cc: list[str] = field(default_factory=list)
    bcc: list[str] = field(default_factory=list)
    subject: str = ""
    attachments: list[dict] = field(default_factory=list)
    bytes: int = 0
    sent: bool = True                      # False for a dry run

    def to_dict(self) -> dict:
        return asdict(self)


def addresses(value: Any) -> list[str]:
    """Recipients from a list, or text like "a@x.com, Name <b@y.com>; c@z.com"."""
    if value is None or value == "":
        return []
    items = value if isinstance(value, (list, tuple)) else [value]
    out: list[str] = []
    for item in items:
        if isinstance(item, dict):                         # e.g. a row with an "email" column
            item = item.get("email") or item.get("address") or ""
        text = str(item).replace(";", ",")
        if "\r" in text or "\n" in text:
            raise MailError("a recipient contains a line break")
        for _, addr in getaddresses([text]):
            if addr:
                out.append(addr.strip())
    return list(dict.fromkeys(out))


def check_host(spec: MailServerSpec, cfg: ProjectConfig) -> None:
    allowed = {h.lower() for h in cfg.security.allowed_mail_hosts}
    host = spec.host.lower()
    if host not in allowed and f"{host}:{spec.effective_port()}" not in allowed:
        raise MailError(f"mail host '{spec.host}' is not in security.allowed_mail_hosts")


def check_recipients(spec: MailServerSpec, recipients: list[str]) -> None:
    from .config import _EMAIL
    if not recipients:
        raise MailError("no recipients")
    if len(recipients) > spec.max_recipients:
        raise MailError(f"{len(recipients)} recipients is more than max_recipients ({spec.max_recipients})")
    pats = spec.recipient_patterns()
    for r in recipients:
        if not _EMAIL.match(r):
            raise MailError(f"'{r}' is not an email address")
        if not any(fnmatch.fnmatchcase(r.lower(), p) for p in pats):
            raise MailError(f"'{r}' is not an allowed recipient for this mail server (allowed: {', '.join(pats)})")


def _no_breaks(name: str, value: str) -> str:
    if "\r" in value or "\n" in value:
        raise MailError(f"the {name} contains a line break")
    return value


def build_message(spec: MailServerSpec, to: list[str], cc: list[str], subject: str, body: str, html: bool,
                  attachments: list[Attachment]) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = spec.from_
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    if spec.reply_to:
        msg["Reply-To"] = spec.reply_to
    msg["Subject"] = _no_breaks("subject", subject.strip())
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=spec.from_.split("@", 1)[1])
    msg["Auto-Submitted"] = "auto-generated"               # RFC 3834: no out-of-office replies loops
    if html:
        msg.set_content("This message is HTML; open it in a mail program that shows HTML.")
        msg.add_alternative(body, subtype="html")
    else:
        msg.set_content(body or "")
    for a in attachments:
        ctype, enc = mimetypes.guess_type(a.name)
        if ctype is None or enc is not None:
            ctype = "application/octet-stream"
        main, sub = ctype.split("/", 1)
        msg.add_attachment(a.data, maintype=main, subtype=sub, filename=_no_breaks("attachment name", a.name))
    return msg


def _tls_context(spec: MailServerSpec, root: Path) -> ssl.SSLContext:
    cafile = None
    if spec.ca_bundle:
        p = Path(spec.ca_bundle)
        p = p if p.is_absolute() else root / p
        if not p.is_file():
            raise MailError(f"ca_bundle '{spec.ca_bundle}' not found")
        cafile = str(p)
    return ssl.create_default_context(cafile=cafile)


def _login(spec: MailServerSpec, credentials: Any, name: str) -> tuple[str, str] | None:
    if not spec.credential:
        return None
    if credentials is None:
        raise MailError(f"mail server '{name}' needs the credential '{spec.credential}', but no vault is open")
    cred = credentials.get(spec.credential, consumer=f"mail:{name}")
    if cred.type != "basic":
        raise MailError(f"credential '{spec.credential}' must be of type basic (username + password), "
                        f"not {cred.type}")
    return str(cred.get("username")), str(cred.get("password"))


SmtpFactory = Callable[..., Any]


def deliver(spec: MailServerSpec, name: str, msg: EmailMessage, recipients: list[str], *, root: Path,
            credentials: Any = None, smtp_factory: SmtpFactory | None = None) -> None:
    """Hand the message to the server. ``smtp_factory(kind, host, port, timeout, context)`` is for tests."""
    ctx = _tls_context(spec, root)
    login = _login(spec, credentials, name)
    host, port = spec.host, spec.effective_port()

    def make(kind: str):
        if smtp_factory is not None:
            return smtp_factory(kind, host, port, spec.timeout_seconds, ctx)
        if kind == "ssl":
            return smtplib.SMTP_SSL(host, port, timeout=spec.timeout_seconds, context=ctx)
        return smtplib.SMTP(host, port, timeout=spec.timeout_seconds)

    try:
        with make("ssl" if spec.security == "ssl" else "plain") as smtp:
            if spec.security == "starttls":
                smtp.ehlo()
                if not smtp.has_extn("starttls"):
                    raise MailError(f"mail server '{name}' does not offer STARTTLS; set security: ssl, or none "
                                    "for an internal relay without login")
                smtp.starttls(context=ctx)
                smtp.ehlo()
            if login:
                smtp.login(*login)
            refused = smtp.send_message(msg, from_addr=spec.from_, to_addrs=recipients)
            if refused:
                raise MailError(f"the server refused: {', '.join(sorted(refused))}")
    except MailError:
        raise
    except smtplib.SMTPAuthenticationError:
        raise MailError(f"mail server '{name}' rejected the login (check credential '{spec.credential}')") from None
    except (smtplib.SMTPException, OSError) as exc:
        raise MailError(f"sending through '{name}' ({host}:{port}) failed: {type(exc).__name__}: "
                        f"{str(exc)[:200]}") from None


class Mailer:
    """Send one message through a configured server; checks, builds, delivers and audits."""

    def __init__(self, config: ProjectConfig, root: str | Path, credentials: Any = None, *, actor: str = "cli",
                 scopes: list[str] | None = None, smtp_factory: SmtpFactory | None = None):
        self.cfg, self.root, self.credentials = config, Path(root), credentials
        self.actor, self.scopes, self.smtp_factory = actor, scopes, smtp_factory

    def server(self, name: str) -> MailServerSpec:
        spec = self.cfg.mail_servers.get(name)
        if spec is None:
            raise MailError(f"unknown mail server '{name}'"
                            + (f"; defined: {', '.join(sorted(self.cfg.mail_servers))}" if self.cfg.mail_servers
                               else " (add one with 'portwright mail add')"))
        return spec

    def send(self, server: str, *, to: Any, subject: str, body: str = "", cc: Any = None, bcc: Any = None,
             html: bool = False, attachments: list[Attachment] | None = None, dry_run: bool = False,
             save_to: Path | None = None) -> MailResult:
        if self.scopes is not None and "*" not in self.scopes and f"mail:{server}" not in self.scopes:
            raise MailError(f"missing scope mail:{server} (add it to the workflow's scopes)")
        spec = self.server(server)
        check_host(spec, self.cfg)
        to_l, cc_l, bcc_l = addresses(to), addresses(cc), addresses(bcc)
        everyone = list(dict.fromkeys(to_l + cc_l + bcc_l))
        if not to_l:
            raise MailError("no 'to' recipients")
        check_recipients(spec, everyone)
        atts = list(attachments or [])
        total = sum(len(a.data) for a in atts)
        if total > spec.max_attachments_mb * 1024 * 1024:
            raise MailError(f"attachments are {total / 1048576:.1f} MB; the limit for '{server}' is "
                            f"{spec.max_attachments_mb} MB (zip them, or copy them to a share instead)")
        msg = build_message(spec, to_l, cc_l, str(subject or ""), str(body or ""), html, atts)
        raw = msg.as_bytes()
        if save_to is not None:
            save_to.parent.mkdir(parents=True, exist_ok=True)
            save_to.write_bytes(raw)
        if not dry_run:
            deliver(spec, server, msg, everyone, root=self.root, credentials=self.credentials,
                    smtp_factory=self.smtp_factory)
        res = MailResult(server, str(msg["Message-ID"]), to_l, cc_l, bcc_l, str(msg["Subject"]),
                         [{"name": a.name, "bytes": len(a.data), "sha256": a.sha256, "source": a.source}
                          for a in atts], len(raw), not dry_run)
        self._audit(res)
        return res

    def _audit(self, res: MailResult) -> None:
        log = self.cfg.security.audit_log
        if not log:
            return
        from .security.audit import AuditLogger
        AuditLogger(str(self.root / log)).record(
            event="mail_sent" if res.sent else "mail_dry_run", actor=self.actor, server=res.server,
            message_id=res.message_id, to=res.to, cc=res.cc, bcc=res.bcc, subject=res.subject,
            attachments=[{k: a[k] for k in ("name", "bytes", "sha256")} for a in res.attachments], bytes=res.bytes)
