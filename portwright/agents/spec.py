"""Declarative specs for LLM models, tools, skills, agents, workflows and MCP servers."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

NAME = r"^[a-zA-Z][a-zA-Z0-9_\-]{0,63}$"

# How much report data an AI model may see, from least to most (see portwright/reports/policy.py)
DataAccess = Literal["none", "schema", "profile", "sample", "full"]
DATA_ACCESS_ORDER = ("none", "schema", "profile", "sample", "full")


class LlmTls(BaseModel):
    """TLS for corporate gateways: private CA and/or mutual TLS. Paths are relative to the project root."""
    ca_bundle: str | None = None
    client_cert: str | None = None
    client_key: str | None = None
    verify: bool = True                  # False disables certificate checks (never for production)

    @model_validator(mode="after")
    def _pair(self) -> "LlmTls":
        if bool(self.client_key) and not self.client_cert:
            raise ValueError("tls: 'client_key' needs 'client_cert'")
        return self


class LlmModelSpec(BaseModel):
    provider: Literal["openai_compatible", "anthropic"] = "openai_compatible"
    base_url: str
    model: str
    credential: str | None = None        # bearer / api_key credential holding the API key
    headers: dict[str, str] = Field(default_factory=dict)   # may use ${CRED:...} / ${ENV:...}
    tls: LlmTls = Field(default_factory=LlmTls)
    timeout_seconds: float = Field(120, gt=0, le=3600)
    max_tokens: int = Field(4096, ge=1)
    temperature: float | None = Field(None, ge=0, le=2)
    data_access: DataAccess = "full"     # ceiling on report data this model may see (AI report authoring)


ToolType = Literal["endpoint", "source", "mcp", "filesystem", "report"]
FsToolOp = Literal["list", "stat", "read", "search", "write", "mkdir", "copy", "move", "delete", "zip"]
ReportToolOp = Literal["sources", "sample", "preview", "save", "run", "design"]
FS_READ_OPS = ("list", "stat", "read", "search")


class ToolSpec(BaseModel):
    type: ToolType
    description: str = ""
    endpoint: str | None = None          # type endpoint: any endpoint (callable sources included)
    source: str | None = None            # type source: a SQL source, exposed as a SELECT tool
    server: str | None = None            # type mcp: a defined MCP server (all of its tools)
    read_only: bool = True               # source tools: SELECT only unless false (needs a writable source)
    max_rows: int = Field(200, ge=1, le=10000)
    # type filesystem: one tool per op, named <tool>_<op> (name the tool 'fs' to get fs_list, fs_read, ...)
    roots: list[str] = Field(default_factory=list)   # root names and dest:<name>; the only places it may touch
    ops: list[str] = Field(default_factory=list)     # filesystem default: list, stat, read · report: sources, run
    # type report: report_<op> tools (name the tool 'report'); reports: the reports it may use (empty: all)
    reports: list[str] = Field(default_factory=list)
    model: str | None = None             # report design: the model that writes templates (default: the only one)

    @model_validator(mode="after")
    def _fields(self) -> "ToolSpec":
        if self.type == "filesystem":
            if not self.roots:
                raise ValueError("tool of type 'filesystem' needs 'roots' (root names and/or dest:<name>)")
            self.ops = self.ops or list(FS_READ_OPS[:3])
            bad = sorted(set(self.ops) - set(FsToolOp.__args__))
            if bad:
                raise ValueError(f"unknown filesystem op(s) {bad}; choose from {', '.join(FsToolOp.__args__)}")
            return self
        if self.type == "report":
            self.ops = self.ops or ["sources", "run"]
            bad = sorted(set(self.ops) - set(ReportToolOp.__args__))
            if bad:
                raise ValueError(f"unknown report op(s) {bad}; choose from {', '.join(ReportToolOp.__args__)}")
            return self
        need = {"endpoint": self.endpoint, "source": self.source, "mcp": self.server}[self.type]
        key = {"endpoint": "endpoint", "source": "source", "mcp": "server"}[self.type]
        if not need:
            raise ValueError(f"tool of type '{self.type}' needs '{key}'")
        return self


class SkillSpec(BaseModel):
    file: str                            # markdown playbook, relative to the project root
    description: str = ""


class AgentSpec(BaseModel):
    description: str = ""
    model: str
    instructions: str = ""
    tools: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    max_steps: int = Field(8, ge=1, le=100)              # hard cap on model calls
    max_tokens_budget: int | None = Field(None, ge=1)    # stop when total tokens exceed this
    timeout_seconds: float = Field(300, gt=0, le=7200)
    scopes: list[str] = Field(default_factory=list)      # scopes of the principal used for tool calls


StepType = Literal["input", "tool", "agent", "llm", "load", "foreach", "condition", "switch", "set", "output",
                   "report", "report_design", "copy", "move", "file"]
FileOp = Literal["write", "read", "list", "mkdir", "delete", "zip"]
Overwrite = Literal["never", "always", "rename", "if_newer"]


class WorkflowStep(BaseModel):
    id: str = Field(pattern=NAME)
    type: StepType
    tool: str | None = None
    agent: str | None = None
    input: Any = None                    # ${steps.<id>.output...} / ${input...} templating
    when: str | None = None              # condition: truthy -> 'then', else -> 'else'
    then: str | None = None              # step id, or 'end'
    else_: str | None = Field(None, alias="else")
    next: str | None = None              # step id to run next, or 'end' (default: the following step)
    x: float | None = None               # position in the visual editor (ignored when running)
    y: float | None = None
    value: Any = None                    # set: a literal/templated value stored as output; output: the result
    inputs: dict[str, Any] | None = None  # input: declared workflow inputs {name: default | {default, required, description}}
    model: str | None = None             # llm: the model to call (llm_models)
    system: str | None = None            # llm: system prompt (may use ${...})
    format: Literal["text", "json"] = "text"   # llm: 'json' asks for a JSON reply and parses it for later steps
    load: str | None = None              # load: the data load to run (needs scope load:run)
    items: Any = None                    # foreach: the list to loop over, e.g. ${steps.q.output.rows}
    do: Literal["tool", "agent", "llm", "load", "report", "copy", "move"] | None = None   # foreach: per item
    max_items: int = Field(50, ge=1, le=1000)   # foreach: refuse longer lists instead of running them all
    cases: dict[str, str] | None = None  # switch: value -> step id (or 'end'); 'when' is the value switched on
    default: str | None = None           # switch: step id when no case matches (default: the following step)
    on_error: Literal["fail", "continue"] = "fail"
    # report: a registered report (input = its params) or an inline one (template / formats / data)
    report: str | None = None
    template: str | None = None          # report: inline template (pdf/html)
    data: Any = None                     # report: rows to use instead of the report's own source
    formats: list[str] | None = None     # report: override the formats
    vars: dict[str, Any] | None = None   # report: extra template variables
    output_path: str | None = None       # report: output path stem
    # copy / move / file
    files: Any = None                    # copy / move / zip / delete: paths, addresses or report outputs
    to: str | None = None                # copy / move: target folder; file write / mkdir / zip: target
    path: str | None = None              # file read / list: what to read
    op: FileOp | None = None             # file: the operation
    content: Any = None                  # file write: text or a value written as JSON
    overwrite: Overwrite | None = None   # default: the destination's policy, else 'never'
    create_dirs: bool = True
    verify: Literal["none", "size", "sha256"] = "sha256"
    manifest: bool = False               # copy / move: write manifest.json beside the files
    glob: str | None = None              # file list
    recursive: bool = False              # file list
    # report_design: an AI model writes the report's template (input = what the report should show)
    from_: dict[str, Any] | None = Field(None, alias="from")   # the report's data, when it does not exist yet
    base: str | None = None              # a gallery template (or project file) to start from
    sample_rows: int | None = Field(None, ge=0, le=500)
    data_access: DataAccess | None = None   # lower the data access for this run (never raises it)

    model_config = {"populate_by_name": True}

    @model_validator(mode="after")
    def _fields(self) -> "WorkflowStep":
        if self.type == "tool" and not self.tool:
            raise ValueError(f"step '{self.id}': type tool needs 'tool'")
        if self.type == "agent" and not self.agent:
            raise ValueError(f"step '{self.id}': type agent needs 'agent'")
        if self.type == "condition" and not (self.when and self.then):
            raise ValueError(f"step '{self.id}': type condition needs 'when' and 'then'")
        if self.type == "llm" and not self.model:
            raise ValueError(f"step '{self.id}': type llm needs 'model'")
        if self.type == "load" and not self.load:
            raise ValueError(f"step '{self.id}': type load needs 'load'")
        if self.type == "switch" and not self.when:
            raise ValueError(f"step '{self.id}': type switch needs 'when' (the value to switch on)")
        if self.type == "report_design":
            if not (self.model and self.report):
                raise ValueError(f"step '{self.id}': type report_design needs 'model' and 'report' (the name)")
            if self.input in (None, ""):
                raise ValueError(f"step '{self.id}': type report_design needs 'input' (what the report shows)")
            if self.overwrite not in (None, "never", "always"):
                raise ValueError(f"step '{self.id}': report_design 'overwrite' is never (keep an existing AI "
                                 "template) or always (design again)")
        kind = self.do if self.type == "foreach" else self.type
        if kind == "report" and not (self.report or self.formats):
            raise ValueError(f"step '{self.id}': a report step needs 'report' (a report name), or 'formats' "
                             "and 'data' for an inline report")
        if kind == "report" and not self.report and self.data is None:
            raise ValueError(f"step '{self.id}': an inline report step needs 'data'")
        if kind in ("copy", "move") and (self.files in (None, "", []) or not self.to):
            raise ValueError(f"step '{self.id}': type {kind} needs 'files' and 'to' (a folder)")
        if self.type == "file":
            need = {"write": ("to",), "read": ("path",), "list": ("path",), "mkdir": ("to",),
                    "delete": ("files",), "zip": ("files", "to")}
            if not self.op:
                raise ValueError(f"step '{self.id}': type file needs 'op' ({', '.join(need)})")
            missing = [f for f in need[self.op] if getattr(self, f) in (None, "", [])]
            if missing:
                raise ValueError(f"step '{self.id}': file op '{self.op}' needs {' and '.join(missing)}")
        if self.type == "foreach":
            if self.items is None or self.items == "":
                raise ValueError(f"step '{self.id}': type foreach needs 'items'")
            target = {"tool": self.tool, "agent": self.agent, "llm": self.model, "load": self.load,
                      "report": self.report or self.formats, "copy": self.to, "move": self.to}.get(self.do or "")
            if not target:
                raise ValueError(f"step '{self.id}': foreach needs 'do' (tool, agent, llm, load, report, copy or "
                                 "move) and what it runs")
        return self


class WorkflowSpec(BaseModel):
    description: str = ""
    steps: list[WorkflowStep] = Field(min_length=1)
    output: Any = None                   # templated result; default: the last executed step's output
    max_steps: int = Field(50, ge=1, le=1000)   # guards against loops through condition jumps
    scopes: list[str] = Field(default_factory=list)   # principal for the workflow's own tool steps


class McpSourceExpose(BaseModel):
    name: str
    tables: list[str] = Field(default_factory=list)      # empty = no table tools; list the allowed tables
    read_only: bool = True


class McpFsExpose(BaseModel):
    root: str                                            # a root name, or dest:<name> (write-only)
    ops: list[FsToolOp] = Field(default_factory=lambda: ["list", "stat", "read"])


class McpExpose(BaseModel):
    endpoints: list[str] = Field(default_factory=list)
    sources: list[McpSourceExpose] = Field(default_factory=list)
    loads: list[str] = Field(default_factory=list)       # run_load_<name> tools (needs scope load:run)
    reports: list[str] = Field(default_factory=list)     # run_report_<name> tools (needs scope reports:run)
    filesystem: list[McpFsExpose] = Field(default_factory=list)   # fs_<op> tools on these roots


class McpServerSpec(BaseModel):
    description: str = ""
    transport: Literal["stdio", "http"] = "stdio"
    expose: McpExpose = Field(default_factory=McpExpose)
    scopes: list[str] = Field(default_factory=list)      # principal used for tool calls
    max_blob_mb: float = Field(10, gt=0, le=100)         # fs_read returns files up to this size as MCP resources
