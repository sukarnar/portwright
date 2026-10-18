"""Active Directory over LDAPS with ldap3 (pure Python, no RSAT, works offline)."""
from __future__ import annotations

import ssl
from pathlib import Path
from urllib.parse import urlsplit

from .base import Directory, DirectoryError, batches

MAX_ATOMIC_REPLACE = 4000          # larger member lists are changed in batches instead


class LdapDirectory(Directory):
    def __init__(self, spec, credential, project_root=".", *, connection=None):
        self.spec = spec
        self.base_dn = spec.base_dn
        self._conn = connection                 # tests inject a mock connection
        self.credential = credential
        self.root = Path(project_root)

    # ------------------------------------------------------------ connect
    def _connection(self):
        if self._conn is not None:
            return self._conn
        try:
            from ldap3 import FIRST, SIMPLE, Connection, Server, ServerPool, Tls
        except ImportError:  # pragma: no cover
            raise DirectoryError("the ldap3 package is not installed (it ships with Portwright)") from None
        if self.credential is None:
            raise DirectoryError("the ldap source has no credential (service account)")
        f = self.credential.fields
        user, password = f.get("username"), f.get("password")
        if not user or not password:
            raise DirectoryError(f"credential '{self.credential.name}' needs username and password "
                                 f"(type basic; username as svc@corp.local or CORP\\svc)")
        ca = None
        if self.spec.ca_file:
            ca = Path(self.spec.ca_file)
            if not ca.is_absolute():
                ca = self.root / ca
            if not ca.exists():
                raise DirectoryError(f"ca_file not found: {ca}")
        # certificate checked against the Windows certificate store (or ca_file), host name verified
        tls = Tls(validate=ssl.CERT_REQUIRED, ca_certs_file=str(ca) if ca else None)
        servers = []
        for url in self.spec.servers:
            u = urlsplit(url if "://" in url else "ldaps://" + url)
            secure = u.scheme.lower() == "ldaps"
            if not secure and not self.spec.allow_plain_ldap:
                raise DirectoryError(f"{url} is not encrypted; use ldaps://")
            servers.append(Server(u.hostname, port=u.port or (636 if secure else 389), use_ssl=secure,
                                  tls=tls if secure else None, connect_timeout=10, get_info="NO_INFO"))
        pool = ServerPool(servers, FIRST, active=True, exhaust=True)
        try:
            self._conn = Connection(pool, user=user, password=password, authentication=SIMPLE,
                                    auto_bind=True, raise_exceptions=True, auto_range=True,
                                    receive_timeout=60, read_only=False)
        except Exception as exc:
            raise DirectoryError(f"could not bind to AD ({', '.join(self.spec.servers)}): {_short(exc)}") from None
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.unbind()
            except Exception:
                pass
            self._conn = None

    # ---------------------------------------------------------- primitives
    def search(self, ldap_filter, attributes, size_limit=0):
        from ldap3 import SUBTREE
        conn = self._connection()
        try:
            if size_limit:
                conn.search(self.base_dn, ldap_filter, SUBTREE, attributes=attributes, size_limit=size_limit)
                entries = conn.response or []
            else:
                entries = conn.extend.standard.paged_search(self.base_dn, ldap_filter, SUBTREE,
                                                            attributes=attributes, paged_size=500,
                                                            generator=False)
        except Exception as exc:
            if "sizeLimitExceeded" in type(exc).__name__ or "sizeLimit" in str(exc):
                entries = conn.response or []
            else:
                raise DirectoryError(f"AD search failed: {_short(exc)}") from None
        out = []
        for e in entries or []:
            if e.get("type") not in (None, "searchResEntry"):
                continue
            attrs = dict(e.get("attributes") or {})
            attrs.setdefault("distinguishedName", e.get("dn"))
            if not attrs.get("distinguishedName"):
                attrs["distinguishedName"] = e.get("dn")
            out.append(attrs)
        return out

    def member_dns(self, group_dn):
        from ldap3 import BASE
        conn = self._connection()
        try:
            conn.search(group_dn, "(objectClass=*)", BASE, attributes=["member"])
        except Exception as exc:
            raise DirectoryError(f"could not read members of {group_dn}: {_short(exc)}") from None
        if not conn.response:
            raise DirectoryError(f"group {group_dn} not found")
        vals = (conn.response[0].get("attributes") or {}).get("member") or []
        return [str(v) for v in ([vals] if isinstance(vals, str) else vals)]

    def _modify(self, group_dn, op, dns):
        from ldap3 import MODIFY_ADD, MODIFY_DELETE, MODIFY_REPLACE
        code = {"add": MODIFY_ADD, "delete": MODIFY_DELETE, "replace": MODIFY_REPLACE}[op]
        conn = self._connection()
        try:
            conn.modify(group_dn, {"member": [(code, list(dns))]})
        except Exception as exc:
            name = type(exc).__name__
            if "InsufficientAccess" in name:
                raise DirectoryError(f"the service account may not change members of {group_dn} "
                                     f"(grant 'Write Members' on the group)") from None
            raise DirectoryError(f"AD change failed ({op} {len(dns)} member(s)): {_short(exc)}") from None

    def replace_members(self, group_dn, dns):
        if len(dns) > MAX_ATOMIC_REPLACE:
            raise DirectoryError(f"{len(dns)} members is too many for one atomic replace; use mode sync")
        self._modify(group_dn, "replace", dns)

    def add_members(self, group_dn, dns):
        for chunk in batches(list(dns), 500):
            self._modify(group_dn, "add", chunk)

    def remove_members(self, group_dn, dns):
        for chunk in batches(list(dns), 500):
            self._modify(group_dn, "delete", chunk)


def _short(exc: Exception) -> str:
    text = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
    return text[:300]
