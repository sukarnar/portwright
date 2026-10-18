"""Active Directory through the PowerShell ActiveDirectory module (RSAT).

Runs as the Windows identity of the process (e.g. the service account the NSSM service or the
Stonebranch agent runs as) – no password stored – or with the source's credential if one is set.
Requests and the password go to PowerShell through environment variables, never the command line.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
from urllib.parse import urlsplit

from .base import Directory, DirectoryError, batches

_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$ProgressPreference = 'SilentlyContinue'
try {
  Import-Module ActiveDirectory -ErrorAction Stop
  $req = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($env:PW_AD_REQ)) | ConvertFrom-Json
  $p = @{}
  if ($req.server) { $p['Server'] = $req.server }
  if ($env:PW_AD_USER) {
    $sec = ConvertTo-SecureString $env:PW_AD_PASS -AsPlainText -Force
    $p['Credential'] = New-Object System.Management.Automation.PSCredential($env:PW_AD_USER, $sec)
  }
  $out = $null
  switch ($req.op) {
    'search' {
      $a = @{ LDAPFilter = $req.filter; SearchBase = $req.base; Properties = @($req.attributes) }
      if ($req.size) { $a['ResultSetSize'] = [int]$req.size }
      $out = @(Get-ADObject @a @p | ForEach-Object {
        $o = @{}
        foreach ($n in $req.attributes) { $v = $_.$n; if ($v -ne $null) { $o[$n] = @($v | ForEach-Object { "$_" }) } }
        $o['distinguishedName'] = @("$($_.DistinguishedName)")
        $o
      })
    }
    'members' { $out = @((Get-ADGroup -Identity $req.group -Properties member @p).member | ForEach-Object { "$_" }) }
    'replace' {
      if (@($req.dns).Count -eq 0) { Set-ADGroup -Identity $req.group -Clear member @p }
      else { Set-ADGroup -Identity $req.group -Replace @{ member = [string[]]@($req.dns) } @p }
      $out = 'ok'
    }
    'add'    { Add-ADGroupMember -Identity $req.group -Members ([string[]]@($req.dns)) @p; $out = 'ok' }
    'remove' { Remove-ADGroupMember -Identity $req.group -Members ([string[]]@($req.dns)) -Confirm:$false @p; $out = 'ok' }
  }
  [Console]::Out.Write((@{ ok = $true; result = $out } | ConvertTo-Json -Depth 6 -Compress))
} catch {
  [Console]::Out.Write((@{ ok = $false; error = $_.Exception.Message } | ConvertTo-Json -Compress))
  exit 1
}
"""


class PowerShellDirectory(Directory):
    def __init__(self, spec, credential=None, *, runner=None):
        self.spec = spec
        self.base_dn = spec.base_dn
        self.credential = credential
        self._runner = runner or self._run_powershell
        server = spec.servers[0] if spec.servers else None
        self.server = (urlsplit(server if "://" in server else "ldap://" + server).hostname if server else None)

    @staticmethod
    def _exe() -> str:
        for name in ("powershell.exe", "pwsh.exe", "pwsh"):
            path = shutil.which(name)
            if path:
                return path
        raise DirectoryError("PowerShell was not found; use directory: ldap, or run on Windows with RSAT")

    def _run_powershell(self, request: dict) -> object:
        env = dict(os.environ)
        env["PW_AD_REQ"] = base64.b64encode(json.dumps(request).encode("utf-8")).decode()
        if self.credential is not None:
            env["PW_AD_USER"] = str(self.credential.fields.get("username") or "")
            env["PW_AD_PASS"] = str(self.credential.fields.get("password") or "")
        try:
            # -EncodedCommand: the whole script parsed at once (stdin with -Command - runs line by line)
            encoded = base64.b64encode(_SCRIPT.encode("utf-16-le")).decode()
            proc = subprocess.run([self._exe(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                                   "-EncodedCommand", encoded], capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
                                  env=env, timeout=600)
        except subprocess.TimeoutExpired:
            raise DirectoryError("PowerShell AD command timed out") from None
        text = (proc.stdout or "").strip()
        try:
            data = json.loads(text) if text else {}
        except ValueError:
            data = {}
        if not data.get("ok"):
            msg = data.get("error") or (proc.stderr or text or "PowerShell failed").strip().splitlines()[-1]
            if "ActiveDirectory" in msg and ("not loaded" in msg or "was not loaded" in msg or "not found" in msg):
                msg = "the PowerShell ActiveDirectory module (RSAT) is not installed on this server"
            raise DirectoryError(f"PowerShell AD: {msg[:300]}")
        return data.get("result")

    def _call(self, op: str, **kw):
        return self._runner({"op": op, "server": self.server, "base": self.base_dn, **kw})

    # ---------------------------------------------------------- primitives
    def search(self, ldap_filter, attributes, size_limit=0):
        res = self._call("search", filter=ldap_filter, attributes=list(attributes), size=size_limit or None)
        if isinstance(res, dict):
            res = [res]
        return list(res or [])

    def member_dns(self, group_dn):
        res = self._call("members", group=group_dn)
        if isinstance(res, str):
            res = [res]
        return [str(x) for x in (res or [])]

    def replace_members(self, group_dn, dns):
        self._call("replace", group=group_dn, dns=list(dns))

    def add_members(self, group_dn, dns):
        for chunk in batches(list(dns), 500):
            self._call("add", group=group_dn, dns=chunk)

    def remove_members(self, group_dn, dns):
        for chunk in batches(list(dns), 500):
            self._call("remove", group=group_dn, dns=chunk)
