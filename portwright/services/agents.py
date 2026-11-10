"""Agents, tools, skills, workflows, models and MCP servers: manage and run (shared by CLI and Studio)."""
from __future__ import annotations

import asyncio
from typing import Any

from pydantic import ValidationError

from ..agents.loop import AgentResult, run_agent
from ..agents.models import check_agent_models, load_models
from ..agents.runs import list_runs
from ..agents.skills import load_skills
from ..agents.spec import AgentSpec, LlmModelSpec, McpServerSpec, SkillSpec, ToolSpec, WorkflowSpec
from ..agents.templates import get_template, list_templates, template_yaml
from ..agents.workflow import WorkflowResult, declared_inputs, run_workflow
from ..config import ConfigError, ProjectConfig
from .project import Project, ServiceError, _clean

# section name (as used by the CLI) -> (ProjectConfig attribute, spec class)
SECTIONS: dict[str, tuple[str, type]] = {
    "model": ("llm_models", LlmModelSpec), "tool": ("tools", ToolSpec), "skill": ("skills", SkillSpec),
    "agent": ("agents", AgentSpec), "workflow": ("workflows", WorkflowSpec), "mcp": ("mcp_servers", McpServerSpec)}


def _run(coro):
    try:
        return asyncio.run(coro)
    except ConfigError as exc:
        raise ServiceError(str(exc)) from None


class AgentService:
    def __init__(self, project: Project):
        self.p = project

    @property
    def cfg(self) -> ProjectConfig:
        return self.p.config

    # ------------------------------------------------------------ definitions
    def _section(self, kind: str) -> tuple[dict, type]:
        if kind not in SECTIONS:
            raise ServiceError(f"unknown kind '{kind}' (use: {', '.join(SECTIONS)})")
        attr, cls = SECTIONS[kind]
        return getattr(self.cfg, attr), cls

    def list(self, kind: str) -> list[dict]:
        items = self._section(kind)[0]
        out = [{"name": n, **s.model_dump(mode="json", exclude_none=True)} for n, s in items.items()]
        if kind == "model":                      # models that live in llm_models_include files
            try:
                for n, s in load_models(self.cfg, self.p.root).items():
                    if n not in items:
                        out.append({"name": n, "file": True, **s.model_dump(mode="json", exclude_none=True)})
            except ConfigError as exc:
                raise ServiceError(str(exc)) from None
        return out

    # ------------------------------------------------------------- templates
    def templates(self) -> list[dict]:
        return list_templates()

    def template(self, template_id: str) -> dict:
        try:
            return get_template(template_id)
        except KeyError:
            raise ServiceError(f"no template '{template_id}' (see: {', '.join(t['id'] for t in list_templates())})") from None

    def template_yaml(self, template_id: str) -> str:
        self.template(template_id)
        return template_yaml(template_id)

    def workflow_inputs(self, name: str) -> dict[str, dict]:
        """Inputs declared by the workflow's input node(s): {name: {default, required, description}}."""
        out: dict[str, dict] = {}
        for st in self.get("workflow", name).steps:
            if st.type == "input":
                out.update(declared_inputs(st.inputs))
        return out

    def get(self, kind: str, name: str):
        items, _ = self._section(kind)
        if name in items:
            return items[name]
        if kind == "model":
            try:
                return load_models(self.cfg, self.p.root)[name]
            except (KeyError, ConfigError):
                pass
        raise ServiceError(f"no {kind} '{name}'")

    def set(self, kind: str, name: str, data: dict, replace: bool = False) -> None:
        """Add or (replace=True) overwrite a definition. The whole project is re-validated before saving."""
        items, cls = self._section(kind)
        if name in items and not replace:
            raise ServiceError(f"{kind} '{name}' already exists")
        try:
            spec = cls.model_validate(data)
        except ValidationError as exc:
            raise ServiceError(_clean(exc)) from None
        old = items.get(name)
        items[name] = spec
        try:
            self.p.save()
        except Exception:
            if old is None:
                items.pop(name, None)
            else:
                items[name] = old
            raise

    def remove(self, kind: str, name: str) -> None:
        items, _ = self._section(kind)
        if name not in items:
            raise ServiceError(f"no {kind} '{name}'")
        used = self._used_by(kind, name)
        if used:
            raise ServiceError(f"{kind} '{name}' is still used by {used}")
        del items[name]
        self.p.save()

    def _used_by(self, kind: str, name: str) -> list[str]:
        c = self.cfg
        if kind == "tool":
            return [f"agent {n}" for n, a in c.agents.items() if name in a.tools] + [
                f"workflow {n}" for n, w in c.workflows.items() if any(s.tool == name for s in w.steps)]
        if kind == "skill":
            return [f"agent {n}" for n, a in c.agents.items() if name in a.skills]
        if kind == "agent":
            return [f"workflow {n}" for n, w in c.workflows.items() if any(s.agent == name for s in w.steps)]
        if kind == "model":
            return [f"agent {n}" for n, a in c.agents.items() if a.model == name]
        if kind == "mcp":
            return [f"tool {n}" for n, t in c.tools.items() if t.type == "mcp" and t.server == name]
        return []

    # --------------------------------------------------------------- checking
    def validate(self) -> list[str]:
        """Problems that need files or models to check: model references, skill files, TLS files, LLM hosts."""
        problems = check_agent_models(self.cfg, self.p.root)
        try:
            load_skills(list(self.cfg.skills), self.cfg.skills, self.p.root)
        except ConfigError as exc:
            problems.append(str(exc))
        try:
            from ..agents.llm.base import check_llm_host
            for n, m in load_models(self.cfg, self.p.root).items():
                try:
                    check_llm_host(m.base_url, self.cfg.security)
                except ConfigError as exc:
                    problems.append(f"model '{n}': {exc}")
                for f in (m.tls.ca_bundle, m.tls.client_cert, m.tls.client_key):
                    if f and not (self.p.root / f).is_file():
                        problems.append(f"model '{n}': TLS file '{f}' not found")
                if m.credential and m.credential not in {c.name for c in self.p.credentials().list()}:
                    problems.append(f"model '{n}': credential '{m.credential}' does not exist")
        except ConfigError as exc:
            problems.append(str(exc))
        return problems

    def test_model(self, name: str, prompt: str = "Reply with the single word: ready") -> dict:
        """One real call to the model: proves URL, certificates, key and model name work."""
        from ..agents.llm import create_provider

        async def go():
            models = load_models(self.cfg, self.p.root)
            if name not in models:
                raise ServiceError(f"no model '{name}'")
            provider = create_provider(name, models[name], self.cfg.security, self.p.root, self.p.credentials())
            try:
                r = await provider.chat("", [{"role": "user", "content": prompt}], [])
            finally:
                await provider.aclose()
            return {"reply": r.text[:500], "tokens": r.total_tokens}
        try:
            return asyncio.run(go())
        except ConfigError as exc:
            raise ServiceError(str(exc)) from None
        except Exception as exc:  # noqa: BLE001 - LlmError messages are already safe
            raise ServiceError(str(exc)) from None

    # ----------------------------------------------------------------- running
    def run_agent(self, name: str, task: str, **kw: Any) -> AgentResult:
        if name not in self.cfg.agents:
            raise ServiceError(f"no agent '{name}'")
        self.p.audit("agent_started", agent=name)
        return _run(run_agent(self.cfg, self.p.root, self.p.credentials(), name, task, **kw))

    def run_workflow(self, name: str, input: Any = None, **kw: Any) -> WorkflowResult:
        if name not in self.cfg.workflows:
            raise ServiceError(f"no workflow '{name}'")
        self.p.audit("workflow_started", workflow=name)
        return _run(run_workflow(self.cfg, self.p.root, self.p.credentials(), name, input, **kw))

    def runs(self, limit: int = 20) -> list[dict]:
        return list_runs(self.p.root, limit)

    def run_trace(self, run_id: str) -> dict:
        """The saved trace (steps, tool calls, answer) of one run."""
        import json
        from ..agents.runs import RUNS_DIR
        found = sorted((self.p.root / RUNS_DIR).glob(f"{run_id}_*.json")) if run_id else []
        if not found:
            raise ServiceError(f"no saved run '{run_id}'")
        return json.loads(found[0].read_text(encoding="utf-8"))

    # ------------------------------------------------------------ skill files
    def _skill_path(self, file: str):
        root = self.p.root.resolve()
        path = (root / file).resolve()
        if path == root or root not in path.parents:
            raise ServiceError("the skill file must be inside the project folder")
        if path.suffix.lower() not in (".md", ".markdown", ".txt"):
            raise ServiceError("the skill file must be a .md file")
        return path

    def skill_text(self, name: str) -> str:
        path = self._skill_path(self.get("skill", name).file)
        return path.read_text(encoding="utf-8") if path.is_file() else ""

    def save_skill_file(self, file: str, text: str) -> None:
        path = self._skill_path(file)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    # --------------------------------------------------------------------- mcp
    def mcp_tools(self, name: str) -> list[dict]:
        from ..mcp import describe_server
        try:
            return describe_server(self.cfg, name)
        except ConfigError as exc:
            raise ServiceError(str(exc)) from None

    def serve_mcp(self, name: str, transport: str | None = None, host: str = "127.0.0.1", port: int = 8765) -> None:
        from ..mcp import serve_http, serve_stdio
        spec = self.cfg.mcp_servers.get(name)
        if spec is None:
            raise ServiceError(f"no MCP server '{name}'")
        kind = transport or spec.transport
        creds = self.p.credentials()
        coro = (serve_stdio(self.cfg, self.p.root, creds, name) if kind == "stdio"
                else serve_http(self.cfg, self.p.root, creds, name, host, port))
        try:
            asyncio.run(coro)
        except ConfigError as exc:
            raise ServiceError(str(exc)) from None
        except KeyboardInterrupt:
            pass
