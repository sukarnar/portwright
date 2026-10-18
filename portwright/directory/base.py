"""The directory port and shared logic (entry classification, batched lookups)."""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Iterable

USER_ATTRS = ["distinguishedName", "objectClass", "sAMAccountName", "mail", "userPrincipalName", "displayName",
              "userAccountControl", "cn"]
BATCH = 50                                  # values per OR-filter
_DN = re.compile(r"^\s*(CN|OU|DC|UID)=", re.I)


class DirectoryError(Exception):
    pass


@dataclass
class DirEntry:
    dn: str
    kind: str = "other"            # user | group | computer | contact | other
    sam: str | None = None
    mail: str | None = None
    upn: str | None = None
    name: str | None = None
    disabled: bool = False
    proxies: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        who = self.name or self.sam or self.dn
        return f"{who} <{self.mail}>" if self.mail else who


def looks_like_dn(text: str) -> bool:
    return bool(_DN.match(text or "")) and "=" in text and "," in text


def escape(value: str) -> str:
    """RFC 4515 filter escaping."""
    out = []
    for ch in value:
        if ch in "\\*()\0":
            out.append("\\%02x" % ord(ch))
        else:
            out.append(ch)
    return "".join(out)


def classify(raw: dict) -> DirEntry:
    """Build a DirEntry from an attribute dict (keys case-insensitive, values str or list)."""
    a = {k.lower(): v for k, v in raw.items()}

    def one(key):
        v = a.get(key.lower())
        if isinstance(v, (list, tuple)):
            v = v[0] if v else None
        return str(v) if v not in (None, "", []) else None

    classes = a.get("objectclass") or []
    if isinstance(classes, str):
        classes = [classes]
    classes = {str(c).lower() for c in classes}
    if "group" in classes:
        kind = "group"
    elif "computer" in classes:
        kind = "computer"
    elif "contact" in classes:
        kind = "contact"
    elif "user" in classes or "person" in classes or "inetorgperson" in classes:
        kind = "user"
    else:
        kind = "other"
    uac = one("userAccountControl")
    try:
        disabled = bool(int(uac) & 2) if uac else False
    except ValueError:
        disabled = False
    proxies = a.get("proxyaddresses") or []
    if isinstance(proxies, str):
        proxies = [proxies]
    return DirEntry(dn=one("distinguishedName") or "", kind=kind, sam=one("sAMAccountName"), mail=one("mail"),
                    upn=one("userPrincipalName"), name=one("displayName") or one("cn"), disabled=disabled,
                    proxies=[str(p) for p in proxies])


def batches(items: list, size: int = BATCH) -> Iterable[list]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


class Directory(ABC):
    """What group loads need from a directory. Implementations: LdapDirectory, PowerShellDirectory."""

    base_dn: str

    # -- primitive: an LDAP filter search under base_dn, returning attribute dicts
    @abstractmethod
    def search(self, ldap_filter: str, attributes: list[str], size_limit: int = 0) -> list[dict]:
        ...

    @abstractmethod
    def member_dns(self, group_dn: str) -> list[str]:
        """Distinguished names in the group's member attribute (all of them, any size)."""

    @abstractmethod
    def replace_members(self, group_dn: str, dns: list[str]) -> None:
        """Set the member attribute to exactly ``dns`` in one operation (kill & fill)."""

    @abstractmethod
    def add_members(self, group_dn: str, dns: list[str]) -> None:
        ...

    @abstractmethod
    def remove_members(self, group_dn: str, dns: list[str]) -> None:
        ...

    def close(self) -> None:
        pass

    # -- shared logic
    def test(self) -> str:
        rows = self.search("(objectClass=domain)", ["distinguishedName"], size_limit=1) or \
            self.search("(objectClass=*)", ["distinguishedName"], size_limit=1)
        return f"Connected; {self.base_dn} is readable" if rows is not None else "Connected"

    def resolve_group(self, ref: str) -> DirEntry:
        ref = (ref or "").strip()
        if not ref:
            raise DirectoryError("no group given")
        if looks_like_dn(ref):
            flt = f"(&(objectClass=group)(distinguishedName={escape(ref)}))"
        else:
            v = escape(ref)
            flt = f"(&(objectClass=group)(|(sAMAccountName={v})(cn={v})(name={v})))"
        found = [classify(r) for r in self.search(flt, USER_ATTRS + ["member"], size_limit=5)]
        if not found:
            raise DirectoryError(f"AD group '{ref}' not found under {self.base_dn}")
        if len(found) > 1:
            raise DirectoryError(f"'{ref}' matches {len(found)} groups: "
                                 + "; ".join(f.dn for f in found[:3]) + " – use the full DN")
        return found[0]

    def search_groups(self, text: str, limit: int = 50) -> list[DirEntry]:
        v = escape(text.strip()) if text and text.strip() else ""
        flt = (f"(&(objectClass=group)(|(cn=*{v}*)(sAMAccountName=*{v}*)(description=*{v}*)))" if v
               else "(objectClass=group)")
        out = [classify(r) for r in self.search(flt, USER_ATTRS, size_limit=limit)]
        return sorted(out, key=lambda e: (e.name or e.dn).lower())[:limit]

    def find_users(self, emails: list[str], attributes: list[str]) -> dict[str, list[DirEntry]]:
        """e-mail (lower-case) -> matching user entries, trying each attribute in order."""
        result: dict[str, list[DirEntry]] = {e: [] for e in emails}
        todo = list(emails)
        for attr in attributes:
            if not todo:
                break
            for chunk in batches(todo):
                if attr == "proxyAddresses":
                    ors = "".join(f"(proxyAddresses=smtp:{escape(e)})" for e in chunk)
                else:
                    ors = "".join(f"({attr}={escape(e)})" for e in chunk)
                flt = f"(&(objectCategory=person)(objectClass=user)(|{ors}))"
                for raw in self.search(flt, USER_ATTRS + ["proxyAddresses"]):
                    ent = classify(raw)
                    if attr == "mail":
                        keys = [ent.mail]
                    elif attr == "userPrincipalName":
                        keys = [ent.upn]
                    else:
                        keys = [p.split(":", 1)[1] for p in ent.proxies if p.lower().startswith("smtp:")]
                    for k in keys:
                        k = (k or "").strip().lower()
                        if k in result and ent.dn not in {x.dn for x in result[k]}:
                            result[k].append(ent)
            todo = [e for e in todo if not result[e]]
        return result

    def members(self, group_dn: str) -> list[DirEntry]:
        """Direct members with their kind; members not found under base_dn come back as kind 'other'."""
        dns = self.member_dns(group_dn)
        known: dict[str, DirEntry] = {}
        for chunk in batches(dns):
            ors = "".join(f"(distinguishedName={escape(d)})" for d in chunk)
            for raw in self.search(f"(|{ors})", USER_ATTRS):
                ent = classify(raw)
                known[ent.dn.lower()] = ent
        return [known.get(d.lower()) or DirEntry(dn=d, kind="other") for d in dns]


def open_directory(spec, credential, project_root=".") -> Directory:
    """Factory: the adapter named by the source's ``directory`` setting."""
    if spec.directory == "powershell":
        from .powershell import PowerShellDirectory
        return PowerShellDirectory(spec, credential)
    from .ldap import LdapDirectory
    return LdapDirectory(spec, credential, project_root)
