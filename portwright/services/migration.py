"""Moving a setup to another instance (dev -> test -> prod).

Two ways, both driven from the CLI (and Studio for exporting):

* Replay script – the activity log (or the whole current setup) as a PowerShell script of pw commands.
* Migration bundle – one zip with the setup, credential *metadata* (never secrets), the handlers folder,
  optionally the data folder, a variables template and a replay script. ``plan`` shows what an import
  would change on the target; ``apply`` makes the change (backup first, one validated save).

Values that differ per instance (servers, DNs, ports, credential names...) are overridden with a
variables file of dotted paths::

    sources.hr.credential: hr_oracle_prod
    sources.corp_ad.servers: [ldaps://proddc01.corp.local]
    credentials.hr_oracle.host: prod-db.corp.local
    server.port: 8443
"""
from __future__ import annotations

import copy
import hashlib
import json
import shutil
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from ..config import EndpointSpec, LoadSpec, ProjectConfig, SecuritySettings, ServerSettings, SourceSpec
from .activity import (PLURAL, SETTINGS, build_script, config_objects, entries_for_state, _changed_fields)
from .project import Project, ServiceError, _clean

BUNDLE_FORMAT = 1
KINDS = ("source", "endpoint", "load", "settings")
_TEMPLATE_SOURCE_KEYS = ("url", "servers", "base_dn", "base_url", "credential", "ca_file", "test_group")


# ============================================================ variables
def load_vars(path: str | Path | None) -> dict[str, Any]:
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        raise ServiceError(f"variables file not found: {p}")
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ServiceError(f"{p.name}: not valid YAML ({exc})") from None
    if not isinstance(data, dict):
        raise ServiceError(f"{p.name}: expected 'path: value' lines, e.g. sources.hr.credential: hr_prod")
    bad = [k for k in data if not isinstance(k, str) or "." not in k]
    if bad:
        raise ServiceError(f"{p.name}: keys must be dotted paths like sources.hr.credential, got {bad[:3]}")
    return data


def _prefix(kind: str, name: str) -> str:
    if kind == "settings":
        return f"{name}."
    if kind == "credential":
        return f"credentials.{name}."
    return f"{PLURAL[kind]}.{name}."


def apply_vars(kind: str, name: str, spec: dict, variables: dict[str, Any]) -> tuple[dict, list[str]]:
    """A copy of spec with the variables for this object applied; returns (spec, applied keys)."""
    out = copy.deepcopy(spec)
    pre = _prefix(kind, name)
    used = []
    for key, value in variables.items():
        if not key.startswith(pre):
            continue
        path = key[len(pre):].split(".")
        if kind == "credential" and path[0] != "fields":
            path = ["fields", *path]
        node = out
        for part in path[:-1]:
            if not isinstance(node.get(part), dict):
                node[part] = {}
            node = node[part]
        node[path[-1]] = value
        used.append(key)
    return out, used


# ============================================================ one object at a time (pw config ...)
class ConfigObjects:
    """get / put / remove one source, endpoint, load or settings block, validated like any other change."""

    def __init__(self, project: Project):
        self.p = project

    @property
    def cfg(self) -> ProjectConfig:
        return self.p.config

    @staticmethod
    def _kind(kind: str) -> str:
        k = kind.lower().rstrip("s") if kind.lower() not in ("settings",) else "settings"
        if k not in KINDS:
            raise ServiceError(f"kind must be one of {', '.join(KINDS)}")
        return k

    def get(self, kind: str, name: str) -> dict:
        kind = self._kind(kind)
        obj = config_objects(self.cfg).get((kind, name))
        if obj is None:
            raise ServiceError(f"no {kind} '{name}'" + (" (settings are 'server' and 'security')"
                                                         if kind == "settings" else ""))
        return obj

    def exists(self, kind: str, name: str) -> bool:
        return (self._kind(kind), name) in config_objects(self.cfg)

    def _set(self, cfg: ProjectConfig, kind: str, name: str, spec: dict) -> None:
        try:
            if kind == "source":
                cfg.sources[name] = SourceSpec.model_validate(spec)
            elif kind == "endpoint":
                ep = EndpointSpec.model_validate({**spec, "name": name})
                cfg.endpoints = _replace(cfg.endpoints, ep)
            elif kind == "load":
                ld = LoadSpec.model_validate({**spec, "name": name})
                cfg.loads = _replace(cfg.loads, ld)
            elif name == "server":
                cfg.server = ServerSettings.model_validate(spec)
            elif name == "security":
                keep = cfg.security.model_dump(mode="json", include={"api_keys", "users"})
                cfg.security = SecuritySettings.model_validate({**spec, **keep})
            else:
                raise ServiceError("settings are 'server' and 'security'")
        except ServiceError:
            raise
        except Exception as exc:
            raise ServiceError(f"{kind} '{name}': {_clean(exc)}") from None

    def put(self, kind: str, name: str, spec: dict, variables: dict | None = None) -> tuple[str, list[str]]:
        """Create or replace. Returns (created|updated|unchanged, variables applied)."""
        kind = self._kind(kind)
        if not isinstance(spec, dict):
            raise ServiceError("the definition must be a YAML/JSON mapping")
        spec, used = apply_vars(kind, name, spec, variables or {})
        before = config_objects(self.cfg).get((kind, name))
        self._set(self.cfg, kind, name, spec)
        after = config_objects(self.cfg).get((kind, name))
        if before == after:
            self.p.config = self.p._last_good.model_copy(deep=True)
            return "unchanged", used
        self.p.save()
        return ("created" if before is None else "updated"), used

    def remove(self, kind: str, name: str, if_exists: bool = False) -> bool:
        kind = self._kind(kind)
        if kind == "settings":
            raise ServiceError("settings cannot be removed")
        if not self.exists(kind, name):
            if if_exists:
                return False
            raise ServiceError(f"no {kind} '{name}'")
        if kind == "source":
            del self.cfg.sources[name]
        elif kind == "endpoint":
            self.cfg.endpoints = [e for e in self.cfg.endpoints if e.name != name]
        else:
            self.cfg.loads = [x for x in self.cfg.loads if x.name != name]
        self.p.save()
        return True


def _replace(items: list, new) -> list:
    out, done = [], False
    for x in items:
        if x.name == new.name:
            out.append(new)
            done = True
        else:
            out.append(x)
    return out if done else out + [new]


# ============================================================ plans
@dataclass
class PlanItem:
    kind: str
    name: str
    action: str                     # create | update | unchanged | remove | only-on-target
    fields: list[str] = field(default_factory=list)
    vars_applied: list[str] = field(default_factory=list)


@dataclass
class Plan:
    bundle: str
    manifest: dict
    items: list[PlanItem] = field(default_factory=list)
    credentials: list[dict] = field(default_factory=list)       # name, type, status present|missing
    files: list[dict] = field(default_factory=list)             # path, status new|changed|same|skipped
    warnings: list[str] = field(default_factory=list)
    unused_vars: list[str] = field(default_factory=list)

    @property
    def changes(self) -> list[PlanItem]:
        return [i for i in self.items if i.action in ("create", "update", "remove")]

    def to_dict(self) -> dict:
        return {"bundle": self.bundle, "manifest": self.manifest,
                "items": [i.__dict__ for i in self.items], "credentials": self.credentials,
                "files": self.files, "warnings": self.warnings, "unused_vars": self.unused_vars}


@dataclass
class ImportResult:
    plan: Plan
    backup: str | None
    applied: int
    files_written: list[str]
    missing_credentials: list[dict]


# ============================================================ the service
class MigrationService:
    def __init__(self, project: Project):
        self.p = project
        self.objects = ConfigObjects(project)

    # ----------------------------------------------------- credentials (metadata)
    def credential_metadata(self) -> tuple[list[dict], list[str]]:
        warnings: list[str] = []
        out = []
        try:
            mgr = self.p.credentials()
            for c in mgr.list():
                try:
                    full = mgr.get(c.name)
                    secret = full.secrets
                    fields = {k: v for k, v in full.fields.items() if k not in secret}
                    names = sorted(secret)
                except Exception:
                    fields, names = {}, []
                out.append({"name": c.name, "type": c.type, "description": c.description or "",
                            "fields": fields, "secret_fields": names})
        except Exception as exc:     # noqa: BLE001
            warnings.append(f"credentials not listed: {exc}")
        return sorted(out, key=lambda c: c["name"]), warnings

    # ----------------------------------------------------- variables template
    def vars_template(self) -> str:
        cfg = self.p.config
        lines = ["# Values that usually differ between instances. Copy to e.g. prod.vars.yaml, change the",
                 "# values for the target, and pass it with --vars (import, config put, setup.ps1 -Vars).",
                 "# Remove the lines that are the same on the target. Keys are dotted paths.", ""]

        def add(key, value):
            lines.append(yaml.safe_dump({key: value}, default_flow_style=None, sort_keys=False,
                                        width=1000).rstrip())
        add("server.host", cfg.server.host)
        add("server.port", cfg.server.port)
        for k in ("ssl_certfile", "ssl_keyfile"):
            if getattr(cfg.server, k):
                add(f"server.{k}", getattr(cfg.server, k))
        if cfg.security.allowed_upstream_hosts:
            add("security.allowed_upstream_hosts", list(cfg.security.allowed_upstream_hosts))
        if cfg.security.cors_origins:
            add("security.cors_origins", list(cfg.security.cors_origins))
        for name, s in cfg.sources.items():
            lines.append(f"\n# source {name} ({s.type})")
            for k in _TEMPLATE_SOURCE_KEYS:
                v = getattr(s, k, None)
                if v not in (None, "", []):
                    add(f"sources.{name}.{k}", v)
        for ld in cfg.loads:
            if ld.to.group:
                lines.append(f"\n# load {ld.name}")
                add(f"loads.{ld.name}.to.group", ld.to.group)
        creds, _ = self.credential_metadata()
        for c in creds:
            if c["fields"]:
                lines.append(f"\n# credential {c['name']} ({c['type']}) – secrets are prompted, never stored here")
                for k, v in c["fields"].items():
                    add(f"credentials.{c['name']}.{k}", v)
        return "\n".join(lines) + "\n"

    # ----------------------------------------------------- replay scripts
    def script(self, *, since: str | None = None, kind: str | None = None, name: str | None = None,
               full: bool = False, file: str = "setup.ps1") -> tuple[str, int]:
        root = str(self.p.root)
        if full:
            creds, _ = self.credential_metadata()
            entries = entries_for_state(self.p.config, creds)
            title = f"Portwright setup for '{self.p.config.project}' – full replay of the current setup"
        else:
            entries = self.p.activity.read(since=since, kind=kind, name=name)
            title = (f"Portwright setup for '{self.p.config.project}' – changes from the activity log"
                     + (f" since {since}" if since else ""))
        text = build_script(entries, title=title, root=root, file=file)
        return text, sum(1 for e in entries if e.get("steps"))

    # ----------------------------------------------------- export
    def export(self, out: str | Path | None = None, *, include_data: bool = False,
               include_handlers: bool = True) -> tuple[Path, dict]:
        cfg = self.p.config
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        out = Path(out) if out else self.p.root / "migrations" / f"{cfg.project}-{stamp}.zip"
        out.parent.mkdir(parents=True, exist_ok=True)
        objects = config_objects(cfg)
        setup = {"settings": {n: objects[("settings", n)] for n in SETTINGS},
                 "sources": {n: objects[("source", n)] for n in cfg.sources},
                 "endpoints": [objects[("endpoint", e.name)] for e in cfg.endpoints],
                 "loads": [objects[("load", x.name)] for x in cfg.loads]}
        creds, warnings = self.credential_metadata()
        script, _ = self.script(full=True)
        files: dict[str, bytes] = {
            "setup/portwright.export.yaml": yaml.safe_dump(setup, sort_keys=False, allow_unicode=True).encode(),
            "setup/credentials.yaml": yaml.safe_dump({"credentials": creds}, sort_keys=False,
                                                     allow_unicode=True).encode(),
            "vars.template.yaml": self.vars_template().encode(),
            "setup.ps1": script.replace("\n", "\r\n").encode("utf-8-sig"),
        }
        if include_handlers:
            files.update(self._folder("handlers", (".py",)))
        if include_data:
            files.update(self._folder(cfg.security.data_root, None, prefix="data"))
        from .. import __version__
        from .activity import _who
        user, host = _who()
        manifest = {"format": BUNDLE_FORMAT, "portwright": __version__, "project": cfg.project,
                    "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "exported_by": user, "host": host, "source_root": str(self.p.root),
                    "counts": {"sources": len(cfg.sources), "endpoints": len(cfg.endpoints),
                               "loads": len(cfg.loads), "credentials": len(creds),
                               "handlers": sum(1 for k in files if k.startswith("handlers/")),
                               "data_files": sum(1 for k in files if k.startswith("data/"))},
                    "not_included": ["secrets (credential values)", "API keys", "users", "logs and run history"],
                    "warnings": warnings,
                    "sha256": {k: hashlib.sha256(v).hexdigest() for k, v in files.items()}}
        files["README.txt"] = _README.format(project=cfg.project, name=out.name).replace("\n", "\r\n").encode()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("manifest.json", json.dumps(manifest, indent=2))
            for k, v in files.items():
                z.writestr(k, v)
        self.p.activity.write("project", cfg.project, "update", via=self.p.actor,
                              summary=f"migration bundle exported: {out.name}")
        return out, manifest

    def _folder(self, rel: str, suffixes, prefix: str | None = None) -> dict[str, bytes]:
        base = self.p.root / rel
        out: dict[str, bytes] = {}
        if not base.is_dir():
            return out
        for f in sorted(base.rglob("*")):
            if not f.is_file() or "__pycache__" in f.parts or f.name.startswith("."):
                continue
            if suffixes and f.suffix.lower() not in suffixes:
                continue
            out[f"{prefix or rel}/{f.relative_to(base).as_posix()}"] = f.read_bytes()
        return out

    # ----------------------------------------------------- read a bundle
    @staticmethod
    def read_bundle(path: str | Path) -> tuple[dict, dict, list[dict], dict[str, bytes]]:
        p = Path(path)
        if not p.exists():
            raise ServiceError(f"bundle not found: {p}")
        try:
            with zipfile.ZipFile(p) as z:
                names = z.namelist()
                if "manifest.json" not in names or "setup/portwright.export.yaml" not in names:
                    raise ServiceError(f"{p.name} is not a Portwright migration bundle")
                manifest = json.loads(z.read("manifest.json"))
                setup = yaml.safe_load(z.read("setup/portwright.export.yaml")) or {}
                creds = (yaml.safe_load(z.read("setup/credentials.yaml")) or {}).get("credentials", []) \
                    if "setup/credentials.yaml" in names else []
                files = {}
                for n in names:
                    pp = PurePosixPath(n)
                    if n.endswith("/") or ".." in pp.parts or pp.is_absolute():
                        continue
                    if pp.parts[0] in ("handlers", "data"):
                        files[n] = z.read(n)
        except zipfile.BadZipFile:
            raise ServiceError(f"{p.name} is not a zip file") from None
        if manifest.get("format", 0) > BUNDLE_FORMAT:
            raise ServiceError(f"{p.name} was made by a newer Portwright ({manifest.get('portwright')}); "
                               f"upgrade this instance first")
        for n, digest in (manifest.get("sha256") or {}).items():
            if n in files and hashlib.sha256(files[n]).hexdigest() != digest:
                raise ServiceError(f"{p.name}: {n} is damaged (checksum mismatch)")
        return manifest, setup, creds, files

    # ----------------------------------------------------- plan
    def _bundle_objects(self, setup: dict, variables: dict) -> tuple[dict, dict[tuple[str, str], list[str]]]:
        objs: dict[tuple[str, str], dict] = {}
        used: dict[tuple[str, str], list[str]] = {}
        for n, spec in (setup.get("settings") or {}).items():
            objs[("settings", n)] = spec or {}
        for n, spec in (setup.get("sources") or {}).items():
            objs[("source", n)] = spec
        for spec in setup.get("endpoints") or []:
            objs[("endpoint", spec["name"])] = spec
        for spec in setup.get("loads") or []:
            objs[("load", spec["name"])] = spec
        for key in list(objs):
            objs[key], used[key] = apply_vars(key[0], key[1], objs[key], variables)
        return objs, used

    def _desired_config(self, setup: dict, variables: dict, prune: bool, skip_settings: bool):
        """The target configuration after the import (validated), and the bundle's objects."""
        objs, used = self._bundle_objects(setup, variables)
        cfg = self.p.config.model_copy(deep=True)
        for (kind, name), spec in objs.items():
            if kind == "settings" and skip_settings:
                continue
            self.objects._set(cfg, kind, name, spec)
        if prune:
            cfg.sources = {n: s for n, s in cfg.sources.items() if ("source", n) in objs}
            cfg.endpoints = [e for e in cfg.endpoints if ("endpoint", e.name) in objs]
            cfg.loads = [x for x in cfg.loads if ("load", x.name) in objs]
        try:
            cfg = ProjectConfig.model_validate(cfg.model_dump(mode="json", by_alias=True))
        except Exception as exc:
            raise ServiceError(f"the imported setup is not valid on this instance: {_clean(exc)}") from None
        return cfg, objs, used

    def plan(self, bundle: str | Path, variables: dict | None = None, *, prune: bool = False,
             skip_settings: bool = False, overwrite_data: bool = False) -> Plan:
        variables = variables or {}
        manifest, setup, creds, files = self.read_bundle(bundle)
        new_cfg, objs, used = self._desired_config(setup, variables, prune, skip_settings)
        plan = Plan(bundle=str(bundle), manifest=manifest)
        current = config_objects(self.p.config)
        after = config_objects(new_cfg)
        for key in sorted(set(current) | set(after), key=lambda k: (["settings", "source", "endpoint", "load"]
                                                                    .index(k[0]), k[1])):
            if key not in after:
                plan.items.append(PlanItem(key[0], key[1], "remove"))
            elif key not in current:
                plan.items.append(PlanItem(key[0], key[1], "create", sorted(after[key]), used.get(key, [])))
            elif key not in objs:
                plan.items.append(PlanItem(key[0], key[1], "only-on-target"))
            elif current[key] != after[key]:
                plan.items.append(PlanItem(key[0], key[1], "update", _changed_fields(current[key], after[key]),
                                           used.get(key, [])))
            else:
                plan.items.append(PlanItem(key[0], key[1], "unchanged", [], used.get(key, [])))
        applied = {k for v in used.values() for k in v}
        try:
            have = {c.name for c in self.p.credentials().list()}
        except Exception as exc:     # noqa: BLE001
            have = set()
            plan.warnings.append(f"cannot read this instance's credentials: {exc}")
        from ..config import credential_refs
        needed = {s.credential for s in new_cfg.sources.values() if s.credential}
        needed |= {c for s in new_cfg.sources.values() for c in s.credentials}
        needed |= credential_refs(new_cfg.model_dump(mode="json", by_alias=True))
        for c in creds:
            c2, u = apply_vars("credential", c["name"], {"fields": c.get("fields", {})}, variables)
            applied |= set(u)
            plan.credentials.append({**c, "fields": c2["fields"],
                                     "status": "present" if c["name"] in have else "missing",
                                     "used": c["name"] in needed})
        for n in sorted(needed - {c["name"] for c in creds} - have):
            plan.credentials.append({"name": n, "type": "?", "fields": {}, "secret_fields": [],
                                     "status": "missing", "used": True})
            plan.warnings.append(f"credential '{n}' is used but not in the bundle and not on this instance")
        for rel, data in sorted(files.items()):
            dest = self._dest(rel)
            if not dest.exists():
                st = "new"
            elif dest.read_bytes() == data:
                st = "same"
            elif rel.startswith("data/") and not overwrite_data:
                st = "skipped"
            else:
                st = "changed"
            plan.files.append({"path": rel, "status": st})
        plan.unused_vars = sorted(k for k in variables if k not in applied)
        if plan.unused_vars:
            plan.warnings.append("variables that match nothing in the bundle: " + ", ".join(plan.unused_vars[:8]))
        return plan

    def _dest(self, rel: str) -> Path:
        parts = PurePosixPath(rel).parts
        base = self.p.root / (self.p.config.security.data_root if parts[0] == "data" else "handlers")
        dest = (base / Path(*parts[1:])).resolve()
        if not str(dest).startswith(str(base.resolve())):
            raise ServiceError(f"refused to write outside the project: {rel}")
        return dest

    # ----------------------------------------------------- import
    def apply(self, bundle: str | Path, variables: dict | None = None, *, prune: bool = False,
              skip_settings: bool = False, overwrite_data: bool = False) -> ImportResult:
        variables = variables or {}
        plan = self.plan(bundle, variables, prune=prune, skip_settings=skip_settings,
                         overwrite_data=overwrite_data)
        manifest, setup, _creds, files = self.read_bundle(bundle)
        new_cfg, _, _ = self._desired_config(setup, variables, prune, skip_settings)
        backup = None
        if self.p.path.exists():
            bdir = self.p.root / "backups"
            bdir.mkdir(exist_ok=True)
            backup = bdir / f"{self.p.path.stem}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.yaml"
            shutil.copy2(self.p.path, backup)
        written = []
        actor = self.p.actor
        self.p.actor = f"migrate:{Path(bundle).name}"
        try:
            if plan.changes:
                self.p.config = new_cfg
                self.p.save()
            for f in plan.files:
                if f["status"] in ("new", "changed"):
                    dest = self._dest(f["path"])
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(files[f["path"]])
                    written.append(f["path"])
            self.p.activity.write("project", self.p.config.project, "update", via=self.p.actor,
                                  summary=f"migration bundle imported from {manifest.get('host')} "
                                          f"({manifest.get('source_root')}): {len(plan.changes)} change(s), "
                                          f"{len(written)} file(s)")
        finally:
            self.p.actor = actor
        missing = [c for c in plan.credentials if c["status"] == "missing"]
        return ImportResult(plan, str(backup) if backup else None, len(plan.changes), written, missing)


_README = """Portwright migration bundle: {project}
=========================================

Contents
  manifest.json                  who exported it, when, from where; checksums
  setup/portwright.export.yaml   sources, endpoints, loads and settings (no API keys or users)
  setup/credentials.yaml         credential NAMES, types and non-secret fields - never passwords
  vars.template.yaml             values that usually differ per instance (servers, DNs, ports, ...)
  setup.ps1                      the same setup as a script of pw commands
  handlers/, data/               project files (data only if exported with --include-data)

Import on the target instance
  1. Copy {name} to the target server.
  2. Copy vars.template.yaml (unzip it, or: pw migrate vars {name}) to prod.vars.yaml and set the
     target values; delete the lines that are the same.
  3. See what would change:
        pw migrate plan {name} --vars prod.vars.yaml -c C:\\apps\\hr-api\\portwright.yaml
  4. Apply it (portwright.yaml is backed up to backups\\ first):
        pw migrate import {name} --vars prod.vars.yaml -c C:\\apps\\hr-api\\portwright.yaml
     A new project folder:  add --init.   Prompt for missing credential secrets: --prompt-credentials
  5. pw validate, then restart the Portwright service.
"""
