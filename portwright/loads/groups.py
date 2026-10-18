"""Group loads: source rows with e-mail addresses -> members of one Active Directory group.

Modes
  replace (kill & fill) – the member attribute is set to the new list in ONE LDAP operation
  sync                  – add the missing users, remove the users no longer in the source
  insert  (add only)    – add the missing users, never remove anybody

Only user accounts are managed. Nested groups (unless keep_nested_groups is off), computers,
contacts, foreign principals, protected entries and members of the exception group are never removed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..directory import DirectoryError, DirEntry

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class GroupLoadError(Exception):
    pass


@dataclass
class GroupPlan:
    group: DirEntry
    desired: dict[str, DirEntry] = field(default_factory=dict)     # dn(lower) -> user wanted in the group
    current: list[DirEntry] = field(default_factory=list)
    kept: list[DirEntry] = field(default_factory=list)             # current members never removed
    to_add: list[DirEntry] = field(default_factory=list)
    to_remove: list[DirEntry] = field(default_factory=list)
    unchanged: int = 0

    @property
    def managed_current(self) -> int:
        return len(self.current) - len(self.kept)

    def final_dns(self) -> list[str]:
        seen, out = set(), []
        for e in [*self.kept, *self.desired.values()]:
            if e.dn.lower() not in seen:
                seen.add(e.dn.lower())
                out.append(e.dn)
        return out


def email_column(spec, rows: list[dict]) -> str:
    """The real header name for the configured e-mail column (case-insensitive)."""
    want = spec.ad.email_column
    for r in rows[:50]:
        for k in r:
            if str(k).lower() == want.lower():
                return k
    raise GroupLoadError(f"source has no column '{want}' (the e-mail column); "
                         f"columns: {list(rows[0].keys()) if rows else []}")


def collect_emails(spec, rows: list[dict], rejects: list, report) -> dict[str, int]:
    """email(lower) -> first record number. Bad addresses are rejected, repeats dropped."""
    if not rows:
        return {}
    col = email_column(spec, rows)
    out: dict[str, int] = {}
    for i, row in enumerate(rows, start=1):
        raw = row.get(col)
        email = str(raw).strip().lower() if raw is not None else ""
        if not email:
            rejects.append((i, f"empty e-mail ({col})", row))
        elif not _EMAIL.match(email):
            rejects.append((i, f"not an e-mail address: {raw!r}", row))
        elif email in out:
            report.dropped += 1
        else:
            out[email] = i
    return out


def plan(spec, directory, emails: dict[str, int], rows: list[dict], rejects: list) -> GroupPlan:
    ad = spec.ad
    group = directory.resolve_group(spec.to.group)
    p = GroupPlan(group=group)
    found = directory.find_users(list(emails), list(ad.match))
    for email, rec in emails.items():
        matches = found.get(email) or []
        row = rows[rec - 1]
        if not matches:
            rejects.append((rec, f"no AD user with {' or '.join(ad.match)} = {email}", row))
        elif len(matches) > 1:
            rejects.append((rec, f"{email} matches {len(matches)} AD users: "
                                 + "; ".join(m.sam or m.dn for m in matches[:3]), row))
        elif matches[0].disabled and ad.reject_disabled:
            rejects.append((rec, f"AD account {matches[0].sam or matches[0].dn} is disabled", row))
        else:
            p.desired[matches[0].dn.lower()] = matches[0]

    p.current = directory.members(group.dn)
    protected = {x.strip().lower() for x in ad.protected if x.strip()}
    exception: set[str] = set()
    if ad.exception_group:
        exc_group = directory.resolve_group(ad.exception_group)
        exception = {d.lower() for d in directory.member_dns(exc_group.dn)}
    for m in p.current:
        ids = {m.dn.lower(), (m.sam or "").lower(), (m.mail or "").lower(), (m.upn or "").lower()} - {""}
        keep = (m.kind not in ("user",) and not (m.kind == "group" and not ad.keep_nested_groups)) \
            or bool(ids & protected) or m.dn.lower() in exception
        if keep:
            p.kept.append(m)
    current_dns = {m.dn.lower() for m in p.current}
    kept_dns = {m.dn.lower() for m in p.kept}
    p.to_add = [e for d, e in p.desired.items() if d not in current_dns]
    p.unchanged = sum(1 for d in p.desired if d in current_dns)
    if spec.mode in ("replace", "sync"):
        p.to_remove = [m for m in p.current if m.dn.lower() not in p.desired and m.dn.lower() not in kept_dns]
    return p


def check_safety(spec, p: GroupPlan) -> None:
    sf, ad = spec.safety, spec.ad
    if spec.mode == "insert":
        return
    if len(p.desired) < sf.min_rows:
        raise GroupLoadError(f"refused: {len(p.desired)} valid user(s) in the source, safety.min_rows is "
                             f"{sf.min_rows}; the group was not changed")
    base = p.managed_current
    if base and sf.min_percent and len(p.desired) * 100 < base * sf.min_percent:
        raise GroupLoadError(f"refused: {len(p.desired)} user(s) would replace {base} "
                             f"(below safety.min_percent {sf.min_percent:g}%); the group was not changed")
    if base and p.to_remove:
        pct = len(p.to_remove) * 100 / base
        if pct > ad.max_remove_percent:
            raise GroupLoadError(f"refused: would remove {len(p.to_remove)} of {base} members ({pct:.0f}%), "
                                 f"limit max_remove_percent is {ad.max_remove_percent:g}%; the group was not "
                                 f"changed. Check the source, or raise the limit for this run")


def apply(spec, directory, p: GroupPlan, report) -> None:
    gdn = p.group.dn
    if not p.to_add and not p.to_remove:
        return
    if spec.mode == "replace":
        try:
            directory.replace_members(gdn, p.final_dns())
        except DirectoryError as exc:
            if "too many" not in str(exc):
                raise
            report.warnings.append(f"{exc}; applied as add/remove batches instead")
            directory.add_members(gdn, [e.dn for e in p.to_add])
            directory.remove_members(gdn, [e.dn for e in p.to_remove])
    else:
        directory.add_members(gdn, [e.dn for e in p.to_add])
        report.added = len(p.to_add)
        if p.to_remove:
            directory.remove_members(gdn, [e.dn for e in p.to_remove])
    report.added, report.removed = len(p.to_add), len(p.to_remove)


def changes(p: GroupPlan, limit: int) -> list[dict]:
    out = [{"action": "add", "member": e.label, "account": e.sam, "dn": e.dn} for e in p.to_add]
    out += [{"action": "remove", "member": e.label, "account": e.sam, "dn": e.dn} for e in p.to_remove]
    return out[:limit] if limit else out
