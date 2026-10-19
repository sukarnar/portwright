"""Setup activity log: every change to a project's setup, with who made it and the pw command that does it.

Changes are found by comparing the configuration before and after each save (so Studio, the CLI and a
migration import are all covered the same way), plus explicit records for credentials, which live in
the vault rather than in portwright.yaml. Edits made to portwright.yaml outside Portwright are detected
the next time the project is opened and logged as ``via: file``.

Log:       logs/setup.jsonl   (one JSON object per line, never secrets)
Snapshot:  logs/.setup-snapshot.json   (the configuration as last logged)

Each entry carries ``steps`` – the equivalent CLI command(s) – so a replay script for another instance
can be generated from the log (see ``build_script``).
"""
from __future__ import annotations

import getpass
import hashlib
import json
import platform
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import yaml

LOG_NAME = "setup.jsonl"
SNAPSHOT_NAME = ".setup-snapshot.json"
# order in which objects are created on a target (removed in reverse)
KIND_RANK = {"settings": 0, "credential": 1, "source": 2, "endpoint": 3, "load": 4, "user": 5, "api_key": 6}
PLURAL = {"source": "sources", "endpoint": "endpoints", "load": "loads"}
SETTINGS = ("server", "security")
SECURITY_EXCLUDE = {"api_keys", "users"}   # never copied: secrets / per-instance identities


# ============================================================ objects of a configuration
def config_objects(cfg) -> dict[tuple[str, str], dict]:
    """(kind, name) -> compact JSON-able spec, for everything a migration or replay moves."""
    out: dict[tuple[str, str], dict] = {}
    out[("settings", "server")] = cfg.server.model_dump(mode="json", exclude_defaults=True)
    out[("settings", "security")] = cfg.security.model_dump(mode="json", exclude_defaults=True,
                                                            exclude=SECURITY_EXCLUDE)
    for name, s in cfg.sources.items():
        out[("source", name)] = s.model_dump(mode="json", by_alias=True, exclude_defaults=True)
    for e in cfg.endpoints:
        out[("endpoint", e.name)] = e.model_dump(mode="json", by_alias=True, exclude_defaults=True)
    for ld in cfg.loads:
        out[("load", ld.name)] = ld.model_dump(mode="json", by_alias=True, exclude_defaults=True)
    return out


def identity_objects(cfg) -> dict[tuple[str, str], dict]:
    """API keys and users – logged, but only as commands to run by hand on a target."""
    out: dict[tuple[str, str], dict] = {}
    for k in cfg.security.api_keys:
        out[("api_key", k.id)] = {"name": k.name, "scopes": list(k.scopes), "expires": k.expires,
                                  "created": k.created, "disabled": bool(k.disabled)}
    for u in cfg.security.users:
        out[("user", u.username)] = {"scopes": list(u.scopes), "disabled": bool(u.disabled)}
    return out


def _all_objects(cfg) -> dict[tuple[str, str], dict]:
    return {**config_objects(cfg), **identity_objects(cfg)}


def _changed_fields(old: dict, new: dict) -> list[str]:
    return sorted(k for k in set(old) | set(new) if old.get(k) != new.get(k))


@dataclass
class Change:
    kind: str
    name: str
    action: str                       # create | update | delete
    fields: list[str] = field(default_factory=list)
    spec: dict | None = None          # state after the change (None for delete)


def diff(old_cfg, new_cfg) -> list[Change]:
    old = _all_objects(old_cfg) if old_cfg is not None else {}
    new = _all_objects(new_cfg)
    out: list[Change] = []
    for key in new:
        if key not in old:
            out.append(Change(key[0], key[1], "create", sorted(new[key]), new[key]))
        elif old[key] != new[key]:
            if key[0] == "api_key" and new[key]["disabled"] and not old[key]["disabled"]:
                out.append(Change(key[0], key[1], "delete", ["disabled"], new[key]))     # revoked
            else:
                out.append(Change(key[0], key[1], "update", _changed_fields(old[key], new[key]), new[key]))
    for key in old:
        if key not in new:
            out.append(Change(key[0], key[1], "delete", [], None))
    return sorted(out, key=lambda c: (KIND_RANK.get(c.kind, 9) if c.action != "delete"
                                      else 20 - KIND_RANK.get(c.kind, 9), c.name))


# ============================================================ commands (steps)
_SAFE = re.compile(r"^[A-Za-z0-9_\-./:=@,+\\]+$")
_FLAG = re.compile(r"^--?[a-z][a-z-]*$")


def ps_quote(value: Any) -> str:
    """Quote one argument for PowerShell (single quotes, '' escapes) unless it is plainly safe."""
    s = str(value)
    if s and (_FLAG.match(s) or (_SAFE.match(s) and not s.startswith("-"))):
        return s
    return "'" + s.replace("'", "''") + "'"


def spec_yaml(spec: dict) -> str:
    text = yaml.safe_dump(spec, sort_keys=False, allow_unicode=True, default_flow_style=False, width=1000)
    return "\n".join(("  " + line if line.startswith("'@") else line) for line in text.splitlines())


def steps_for(c: Change) -> list[dict]:
    """The pw command(s) that bring a target to the state after this change.

    A step is {"args": [...], "stdin": text|None, "manual": bool, "note": str|None, "ensure": name|None}.
    manual steps are written as comments in replay scripts (they create new secrets on the target).
    """
    if c.kind in ("source", "endpoint", "load", "settings"):
        if c.action == "delete":
            return [{"args": ["config", "remove", c.kind, c.name, "--if-exists"]}]
        return [{"args": ["config", "put", c.kind, c.name], "stdin": spec_yaml(c.spec or {})}]
    if c.kind == "api_key":
        s = c.spec or {}
        if c.action == "delete":
            return [{"args": ["key", "revoke", "<key-id-on-target>"], "manual": True,
                     "note": f"API key '{s.get('name', c.name)}' ({c.name}) was revoked or removed"}]
        if c.action == "create":
            args = ["key", "create", s.get("name") or c.name]
            for sc in s.get("scopes") or []:
                args += ["--scope", sc]
            args += ["--expires-days", str(_days(s.get("created"), s.get("expires")))]
            return [{"args": args, "manual": True,
                     "note": "creates a NEW key on the target – give the printed key to the calling application"}]
        return [{"args": ["key", "list"], "manual": True, "note": f"API key '{s.get('name')}' changed: "
                                                                  + ", ".join(c.fields)}]
    if c.kind == "user":
        if c.action == "delete":
            return [{"args": ["user", "remove", c.name], "manual": True}]
        args = ["user", "add", c.name]
        for sc in (c.spec or {}).get("scopes") or []:
            args += ["--scope", sc]
        return [{"args": args, "manual": True, "note": "prompts for the password"}]
    return []


def credential_steps(action: str, name: str, ctype: str | None = None, fields: dict | None = None,
                     changed: Iterable[str] = ()) -> list[dict]:
    if action == "delete":
        return [{"args": ["cred", "remove", name]}]
    if action == "update":
        args = ["cred", "rotate", name]
        for k, v in (fields or {}).items():
            if k in set(changed):
                args += ["-f", f"{k}={v}"]
        return [{"args": args, "note": "prompts for the new secret values"}]
    args = ["cred", "add", name, "-t", ctype or "generic"]
    for k, v in (fields or {}).items():
        args += ["-f", f"{k}={v}"]
    return [{"args": args, "ensure": name, "note": "prompts for the secret values (they are never exported)"}]


def _days(created: str | None, expires: str | None) -> int:
    if not expires:
        return 0
    try:
        a = datetime.fromisoformat(created) if created else datetime.now(timezone.utc)
        return max(1, round((datetime.fromisoformat(expires) - a).total_seconds() / 86400))
    except ValueError:
        return 90


def render_step(step: dict, exe: str = "pw") -> str:
    """One step as a command a person can read or paste."""
    cmd = " ".join([exe] + [ps_quote(a) if not a.startswith("<") else a for a in step["args"]])
    if step.get("stdin") is not None:
        cmd = "@'\n" + step["stdin"].rstrip("\n") + "\n'@ | " + cmd
    if step.get("manual"):
        cmd = "\n".join("# " + line for line in cmd.splitlines())
    if step.get("note"):
        cmd = f"# {step['note']}\n{cmd}"
    return cmd


def render_steps(steps: list[dict]) -> str:
    return "\n".join(render_step(s) for s in steps)


# ============================================================ the log
def _who() -> tuple[str, str]:
    try:
        user = getpass.getuser()
    except Exception:
        user = "unknown"
    return user, platform.node() or "unknown"


def _summary(kind: str, name: str, action: str, fields: list[str]) -> str:
    label = {"settings": "settings", "api_key": "API key", "credential": "credential"}.get(kind, kind)
    verb = {"create": "created", "update": "changed", "delete": "removed"}[action]
    if kind == "api_key" and action == "delete":
        verb = "revoked"
    text = f"{label} '{name}' {verb}"
    if action == "update" and fields:
        shown = fields[:6]
        text += ": " + ", ".join(shown) + (f" (+{len(fields) - 6} more)" if len(fields) > 6 else "")
    return text


class ActivityLog:
    def __init__(self, root: Path, log_dir: str = "logs"):
        self.dir = Path(root) / log_dir
        self.path = self.dir / LOG_NAME
        self.snapshot_path = self.dir / SNAPSHOT_NAME

    # ---------------------------------------------------------- write
    def write(self, kind: str, name: str, action: str, *, via: str, fields: list[str] | None = None,
              spec: dict | None = None, steps: list[dict] | None = None, summary: str | None = None,
              note: str | None = None) -> dict:
        user, host = _who()
        entry = {"id": uuid.uuid4().hex[:12], "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "user": user, "host": host, "via": via, "kind": kind, "name": name, "action": action,
                 "fields": fields or [], "summary": summary or _summary(kind, name, action, fields or []),
                 "spec": spec, "steps": steps or []}
        if note:
            entry["note"] = note
        self.dir.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        return entry

    def record_diff(self, old_cfg, new_cfg, via: str, note: str | None = None) -> list[dict]:
        out = []
        for c in diff(old_cfg, new_cfg):
            label = None
            if c.kind == "api_key":
                key_name = (c.spec or {}).get("name") or c.name
                label = _summary(c.kind, f"{key_name}", c.action, c.fields) + f" (id {c.name})"
            out.append(self.write(c.kind, c.name, c.action, via=via, fields=c.fields, spec=c.spec,
                                  steps=steps_for(c), note=note, summary=label))
        self.save_snapshot(new_cfg)
        return out

    def record_credential(self, action: str, name: str, *, via: str, ctype: str | None = None,
                          fields: dict | None = None, secret_names: Iterable[str] = (),
                          changed: Iterable[str] = ()) -> dict:
        spec = None
        if action != "delete":
            spec = {"type": ctype, "fields": dict(fields or {}), "secret_fields": sorted(secret_names)}
        return self.write("credential", name, action, via=via, fields=sorted(changed) if changed else
                          sorted((fields or {}).keys()), spec=spec,
                          steps=credential_steps(action, name, ctype, fields, changed))

    # ---------------------------------------------------------- snapshot
    def _digest(self, cfg) -> str:
        return hashlib.sha256(json.dumps(cfg.model_dump(mode="json", by_alias=True), sort_keys=True,
                                         default=str).encode()).hexdigest()

    def save_snapshot(self, cfg) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.snapshot_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"digest": self._digest(cfg),
                                   "config": cfg.model_dump(mode="json", by_alias=True)}, default=str),
                       encoding="utf-8")
        tmp.replace(self.snapshot_path)

    def check_external(self, cfg, config_cls) -> list[dict]:
        """On open: log edits made to the file outside Portwright since the last logged state."""
        if not self.snapshot_path.exists():
            self.write("project", cfg.project, "create", via="baseline",
                       summary=f"activity log started: {len(cfg.sources)} source(s), {len(cfg.endpoints)} "
                               f"endpoint(s), {len(cfg.loads)} load(s)")
            self.save_snapshot(cfg)
            return []
        try:
            snap = json.loads(self.snapshot_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.save_snapshot(cfg)
            return []
        if snap.get("digest") == self._digest(cfg):
            return []
        try:
            old = config_cls.model_validate(snap["config"])
        except Exception:
            old = None
        return self.record_diff(old, cfg, via="file", note="portwright.yaml was edited outside Portwright")

    # ---------------------------------------------------------- read
    def read(self, *, since: str | None = None, kind: str | None = None, name: str | None = None,
             text: str | None = None, last: int | None = None) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        with self.path.open(encoding="utf-8") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if since and e.get("ts", "") < since:
                    continue
                if kind and e.get("kind") != kind:
                    continue
                if name and e.get("name") != name:
                    continue
                if text and text.lower() not in json.dumps(e).lower():
                    continue
                out.append(e)
        return out[-last:] if last else out


# ============================================================ replay script
SCRIPT_HEAD = r"""# {title}
# Generated {when} by {user} on {host} from {root}
# {count}
#
# Run on the target instance (Portwright installed, project created with 'pw init'):
#   Set-ExecutionPolicy -Scope Process Bypass
#   .\{file} -Config C:\apps\hr-api\portwright.yaml [-Vars prod.vars.yaml] [-SkipCredentials]
# Secrets are never in this script: 'pw cred add' prompts for them (existing credentials are kept).
# API keys and users are listed as comments - create them by hand on the target.
param(
  [string]$Config = "portwright.yaml",
  [string]$Vars = "",
  [string]$Pw = "pw",
  [switch]$SkipCredentials
)
$ErrorActionPreference = "Stop"
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$common = @("-c", $Config)
$withVars = $common + $(if ($Vars) { @("--vars", $Vars) } else { @() })
function Assert-Ok([string]$what) { if ($LASTEXITCODE -ne 0) { throw "failed: $what (exit $LASTEXITCODE)" } }
function Test-Credential([string]$name) {
  $eap = $ErrorActionPreference; $ErrorActionPreference = "Continue"
  try { & $Pw cred show $name @common *> $null } catch { }
  $found = ($LASTEXITCODE -eq 0); $ErrorActionPreference = $eap; return $found
}
"""


def _script_step(step: dict) -> str:
    args = " ".join(ps_quote(a) for a in step["args"])
    put = step["args"][:2] == ["config", "put"]
    cred = step["args"][:1] == ["cred"]
    extra = "@withVars" if (put or step["args"][:2] == ["cred", "add"]) else "@common"
    what = " ".join(step["args"][:4]).replace('"', "'")
    lines: list[str] = []
    if step.get("note"):
        lines.append(f"# {step['note']}")
    if step.get("manual"):
        lines.append("# " + "pw " + " ".join(ps_quote(a) if not a.startswith("<") else a for a in step["args"]))
        return "\n".join(lines)
    if step.get("stdin") is not None:
        body = step["stdin"].rstrip("\n")
        lines.append(f"@'\n{body}\n'@ | & $Pw {args} {extra}; Assert-Ok \"{what}\"")
    elif step.get("ensure"):
        n = ps_quote(step["ensure"])
        lines.append(f"if (-not (Test-Credential {n})) {{ & $Pw {args} {extra}; Assert-Ok \"{what}\" }}"
                     f" else {{ Write-Host \"credential {step['ensure']} exists - kept\" }}")
    else:
        lines.append(f"& $Pw {args} {extra}; Assert-Ok \"{what}\"")
    text = "\n".join(lines)
    if cred:
        text = "if (-not $SkipCredentials) {\n" + "\n".join("  " + l for l in text.splitlines()) + "\n}"
    return text


def consolidate(entries: list[dict]) -> list[dict]:
    """Keep the last change of each object; order creates by dependency, removes in reverse."""
    last: dict[tuple[str, str], dict] = {}
    for e in entries:
        if e.get("kind") in ("project",) or not e.get("steps"):
            continue
        last[(e["kind"], e["name"])] = e
    puts = sorted((e for e in last.values() if e["action"] != "delete"),
                  key=lambda e: (KIND_RANK.get(e["kind"], 9), e["ts"]))
    dels = sorted((e for e in last.values() if e["action"] == "delete"),
                  key=lambda e: (-KIND_RANK.get(e["kind"], 9), e["ts"]))
    # loads/endpoints go first (frees routes and references), then creates, then sources/credentials
    early = [e for e in dels if e["kind"] in ("load", "endpoint")]
    late = [e for e in dels if e["kind"] not in ("load", "endpoint")]
    return [*early, *puts, *late]


def build_script(entries: list[dict], *, title: str, root: str, file: str = "setup.ps1",
                 consolidated: bool = True) -> str:
    user, host = _who()
    items = consolidate(entries) if consolidated else [e for e in entries if e.get("steps")]
    head = SCRIPT_HEAD
    for k, v in {"title": title, "when": datetime.now().strftime("%Y-%m-%d %H:%M"), "user": user, "host": host,
                 "root": root, "file": file, "count": f"{len(items)} change(s)"}.items():
        head = head.replace("{" + k + "}", str(v))
    body = []
    for e in items:
        body.append(f"\n# --- {e['summary']}  ({e['ts'][:16].replace('T', ' ')} UTC, {e['user']} via {e['via']})")
        for st in e["steps"]:
            body.append(_script_step(st))
    tail = ("\n\n& $Pw validate @common; Assert-Ok \"validate\"\n"
            "Write-Host \"Setup applied. Restart the Portwright service to load it.\"\n")
    return head + "\n".join(body) + tail


def entries_for_state(cfg, credentials: list[dict]) -> list[dict]:
    """Synthetic 'create' entries for the whole current setup (for a full replay script)."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    user, _ = _who()
    out = []
    for c in credentials:
        out.append({"ts": now, "user": user, "via": "state", "kind": "credential", "name": c["name"],
                    "action": "create", "summary": f"credential '{c['name']}' ({c['type']})",
                    "steps": credential_steps("create", c["name"], c["type"], c.get("fields"))})
    for c in diff(None, cfg):
        if c.kind in ("settings",) and not c.spec:
            continue
        out.append({"ts": now, "user": user, "via": "state", "kind": c.kind, "name": c.name, "action": "create",
                    "summary": _summary(c.kind, c.name, "create", []).replace(" created", ""),
                    "steps": steps_for(c)})
    return out
