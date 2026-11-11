"""Filesystem and report tools: agents (scripted model), MCP exposure, the report-author template, CLI."""
import asyncio
import base64
import json
import sqlite3

import httpx
import pytest

from portwright.agents.llm import LlmProvider, LlmResponse, ToolCall
from portwright.agents.loop import run_agent
from portwright.agents.tools import ToolError, ToolRegistry, ToolRuntime, build_registry, mcp_tools
from portwright.config import ProjectConfig
from portwright.services import Project, ServiceError, Services

pytest.importorskip("jinja2")

GOOD = ("<html><head><style>td{padding:2px}</style></head><body><h1>{{ report_name }}</h1>"
        "{% if data %}<table>{% for r in data %}<tr><td>{{ r.name }}</td><td>{{ r.dept }}</td></tr>{% endfor %}"
        "</table>{% else %}<p>No records found.</p>{% endif %}</body></html>")
FROM = {"source": "hr", "query": "select name, dept from emp order by id"}


class FakeLlm(LlmProvider):
    def __init__(self, script):
        super().__init__("fake", None, httpx.AsyncClient())
        self.script, self.calls = list(script), []

    async def chat(self, system, messages, tools):
        self.calls.append({"system": system, "messages": [dict(m) for m in messages], "tools": [t["name"] for t in tools]})
        return self.script.pop(0) if len(self.script) > 1 else self.script[0]


def call(tool, **args):
    return LlmResponse("", [ToolCall("c1", tool, args)], 10, 5)


def final(text="done"):
    return LlmResponse(text, [], 10, 5)


def tool_results(llm):
    """Tool results the agent saw, in order: [(name, content)]."""
    msgs = llm.calls[-1]["messages"]
    return [(m["name"], m["content"]) for m in msgs if m["role"] == "tool"]


@pytest.fixture
def svc(tmp_path):
    s = Services(Project.init(tmp_path, "t"))
    db = tmp_path / "data" / "hr.db"
    c = sqlite3.connect(db)
    c.execute("create table emp(id integer primary key, name text, dept text, ssn text)")
    c.executemany("insert into emp(name, dept, ssn) values (?, ?, ?)", [("Ann", "ENG", "1"), ("Bob", "OPS", "2")])
    c.commit()
    c.close()
    s.credentials.add("db", "database", {"driver": "sqlite", "database": str(db)})
    s.sources.add("hr", "sql", credential="db")
    out = tmp_path / "reports" / "output"
    out.mkdir(parents=True)
    (out / "old.pdf").write_bytes(b"%PDF-1.4 fake")
    (out / "notes.txt").write_text("quarterly figures attached")
    share = tmp_path / "share"
    share.mkdir()
    cfg = s.project.config.model_dump(mode="json", by_alias=True)
    cfg["security"]["allowed_folders"] = [str(share)]
    cfg["destinations"] = {"finance": {"path": str(share)}}
    cfg["llm_models"] = {"local": {"base_url": "https://llm.test/v1", "model": "m"}}
    cfg["sources"]["hr"]["ai_exclude_columns"] = ["ssn"]
    cfg["reports"] = [{"name": "staff", "from": FROM, "formats": ["csv"]}]
    s.project.config = ProjectConfig.model_validate(cfg)
    s.project.save(force=True)
    s.share = share
    return s


def with_agent(svc, tools: dict, scopes: list[str], agent_tools: list[str] | None = None) -> ProjectConfig:
    cfg = svc.project.config.model_dump(mode="json", by_alias=True)
    cfg["tools"] = tools
    cfg["agents"] = {"a": {"model": "local", "tools": agent_tools or list(tools), "scopes": scopes, "max_steps": 12}}
    return ProjectConfig.model_validate(cfg)


def run(svc, config, llm, task="go"):
    return asyncio.run(run_agent(config, svc.project.root, svc.project.credentials(), "a", task,
                                 provider_factory=lambda n, s: llm, save=False))


# --------------------------------------------------------------------------- filesystem tools
def test_agent_filesystem_tools(svc):
    config = with_agent(svc, {"fs": {"type": "filesystem", "roots": ["reports", "dest:finance"],
                                     "ops": ["list", "read", "search", "copy"]}},
                        ["fs:reports:read", "fs:dest:finance"])
    llm = FakeLlm([call("fs_list", path="reports:"), call("fs_read", path="reports:notes.txt"),
                   call("fs_search", path="reports:", text="QUARTERLY"),
                   call("fs_read", path="data:hr.db"),                         # not one of the tool's roots
                   call("fs_copy", files=["reports:old.pdf"], to="dest:finance/q3"),
                   call("fs_read", path="dest:finance/q3/old.pdf"),            # destinations are write-only
                   final()])
    res = run(svc, config, llm)
    assert res.ok, res.error
    assert llm.calls[0]["tools"] == ["fs_list", "fs_read", "fs_search", "fs_copy"]
    out = tool_results(llm)
    assert '"name": "old.pdf"' in out[0][1] and "quarterly figures" in out[1][1]
    assert '"notes.txt"' in out[2][1] and '"line": 1' in out[2][1]
    assert out[3][1].startswith("ERROR") and "'data' is not available here" in out[3][1]
    assert '"action": "copied"' in out[4][1] and (svc.share / "q3" / "old.pdf").exists()
    assert out[5][1].startswith("ERROR") and "write-only" in out[5][1]


def test_filesystem_tool_scopes_and_excel_rows(svc, tmp_path):
    from openpyxl import Workbook
    wb = Workbook()
    wb.active.append(["a", "b"])
    wb.active.append([1, "x"])
    wb.save(tmp_path / "reports/output/t.xlsx")
    config = with_agent(svc, {"fs": {"type": "filesystem", "roots": ["reports"], "ops": ["read", "write"]}},
                        ["fs:reports:read"])
    llm = FakeLlm([call("fs_read", path="reports:t.xlsx"), call("fs_write", path="reports:x.txt", content="hi"),
                   final()])
    run(svc, config, llm)
    out = tool_results(llm)
    assert json.loads(out[0][1])["rows"] == [{"a": 1, "b": "x"}]
    assert out[1][1].startswith("ERROR") and "missing scope fs:reports:write" in out[1][1]
    assert not (tmp_path / "reports/output/x.txt").exists()


# --------------------------------------------------------------------------- report tools
def test_report_author_flow(svc, tmp_path):
    made = svc.agents.add_agent_from_template("report-author", "author", "local")
    assert made == ["tool report", "tool fs", "agent author"]
    llm = FakeLlm([
        call("report_sources"),
        call("report_sample", **{"from": FROM}),
        call("report_preview", template="<script>x</script>{{ data | length }}", **{"from": FROM}),
        call("report_preview", template=GOOD, **{"from": FROM}),
        call("report_save", report="dept-staff", template=GOOD, formats=["html", "csv"], **{"from": FROM}),
        call("report_run", report="dept-staff"),
        final("Report written"),
    ])
    res = asyncio.run(run_agent(svc.project.config, svc.project.root, svc.project.credentials(), "author",
                                "Staff list by dept", provider_factory=lambda n, s: llm, save=False))
    assert res.ok and res.answer == "Report written", (res.stopped_by, res.error, tool_results(llm))
    out = dict((i, c) for i, (_, c) in enumerate(tool_results(llm)))
    srcs = json.loads(out[0])
    assert [r["name"] for r in srcs["reports"]] == ["staff"] and srcs["sql_sources"] == ["hr"]
    sample = json.loads(out[1])
    assert sample["rows"] == [{"name": "Ann", "dept": "ENG"}, {"name": "Bob", "dept": "OPS"}]      # ssn excluded
    assert "no <script>" in out[2] and '"ok": false' in out[2]
    prev = json.loads(out[3])
    assert prev["ok"] and "Ann" in prev["text"]
    saved = json.loads(out[4])
    assert saved["template"] == ".portwright/templates/dept-staff.html"
    ran = json.loads(out[5])
    assert [o["format"] for o in ran["outputs"]] == ["html", "csv"] and ran["row_count"] == 2
    spec = Project(svc.project.path).config.report("dept-staff")
    assert spec.provenance["generated_by"] == "agent:author" and spec.provenance["draft"] is True


def test_report_tool_policy_and_limits(svc):
    cfg = svc.project.config.model_dump(mode="json", by_alias=True)
    cfg["sources"]["hr"]["ai_data_access"] = "profile"
    svc.project.config = ProjectConfig.model_validate(cfg)
    config = with_agent(svc, {"report": {"type": "report", "reports": ["staff"],
                                         "ops": ["sample", "preview", "run", "save"]}}, ["reports:run"])
    llm = FakeLlm([call("report_sample", report="staff"),
                   call("report_preview", report="staff", template=GOOD),
                   call("report_run", report="other"),
                   call("report_save", report="staff", template=GOOD),
                   final()])
    run(svc, config, llm)
    out = [c for _, c in tool_results(llm)]
    sample = json.loads(out[0])
    assert sample["data_access"] == "profile" and "rows" not in sample and "Ann" not in out[0]
    prev = json.loads(out[1])
    assert prev["ok"] and "text" not in prev                     # the snapshot would show data values
    assert out[2].startswith("ERROR")                           # 'other' is not in the tool's enum / list
    assert out[3].startswith("ERROR") and "reports:write" in out[3]


def test_tool_config_validation(svc):
    base = svc.project.config.model_dump(mode="json", by_alias=True)
    for tools, msg in [({"f": {"type": "filesystem"}}, "needs 'roots'"),
                       ({"f": {"type": "filesystem", "roots": ["nowhere"]}}, "unknown filesystem root"),
                       ({"f": {"type": "filesystem", "roots": ["dest:x"]}}, "unknown destination"),
                       ({"f": {"type": "filesystem", "roots": ["reports"], "ops": ["format_disk"]}}, "unknown filesystem op"),
                       ({"r": {"type": "report", "reports": ["nope"]}}, "unknown report"),
                       ({"r": {"type": "report", "ops": ["hack"]}}, "unknown report op")]:
        with pytest.raises(ValueError, match=msg):
            ProjectConfig.model_validate({**base, "tools": tools})
    with pytest.raises(ValueError, match="is for agents"):
        ProjectConfig.model_validate({**base, "tools": {"fs": {"type": "filesystem", "roots": ["reports"]}},
                                      "workflows": {"w": {"steps": [{"id": "s", "type": "tool", "tool": "fs"}]}}})
    with pytest.raises(ValueError, match="write-only"):
        ProjectConfig.model_validate({**base, "mcp_servers": {"m": {"expose": {
            "filesystem": [{"root": "dest:finance", "ops": ["read"]}]}}}})


def test_agent_template_conflicts(svc):
    svc.agents.set("tool", "report", {"type": "report", "ops": ["run"]})
    with pytest.raises(ServiceError, match="different tool 'report'"):
        svc.agents.add_agent_from_template("report-author", "author", "local")
    assert "author" not in svc.project.config.agents
    with pytest.raises(ServiceError, match="no agent template"):
        svc.agents.add_agent_from_template("nope", "x", "local")


# --------------------------------------------------------------------------- MCP
def _mcp_cfg(svc):
    cfg = svc.project.config.model_dump(mode="json", by_alias=True)
    cfg["mcp_servers"] = {"m": {"scopes": ["reports:run", "fs:reports:read"], "max_blob_mb": 1, "expose": {
        "reports": ["staff"], "filesystem": [{"root": "reports", "ops": ["list", "read"]}]}}}
    return ProjectConfig.model_validate(cfg)


def test_mcp_describe_and_tools(svc):
    from portwright.mcp import describe_server, to_content
    import mcp.types as types
    config = _mcp_cfg(svc)
    desc = {d["tool"]: d for d in describe_server(config, "m")}
    assert set(desc) == {"run_report_staff", "fs_list", "fs_read"} and desc["fs_read"]["description"] == "roots: reports"

    async def go():
        async with ToolRuntime(config, svc.project.root, svc.project.credentials(), ["reports:run", "fs:reports:read"],
                               "mcp:m") as rt:
            reg = ToolRegistry(mcp_tools(rt, config.mcp_servers["m"]))
            assert reg.tools["run_report_staff"].parameters["properties"].keys() == {"formats"}
            ran = await reg.call("run_report_staff", {"formats": ["json"]})
            blob = await reg.call("fs_read", {"path": "reports:old.pdf"})
            text = await reg.call("fs_read", {"path": "reports:notes.txt"})
            return ran, blob, text
    ran, blob, text = asyncio.run(go())
    assert ran["outputs"][0]["format"] == "json" and ran["row_count"] == 2
    assert base64.b64decode(blob["_blob"]["data"]) == b"%PDF-1.4 fake" and blob["_blob"]["mime"] == "application/pdf"
    content = to_content(types, blob)
    assert isinstance(content[1], types.EmbeddedResource) and content[1].resource.mimeType == "application/pdf"
    assert '"_blob"' not in content[0].text
    assert text["content"] == "quarterly figures attached"


def test_mcp_resources(svc):
    import mcp.types as types
    from portwright.fs import Sandbox
    from portwright.mcp import build_server
    config = _mcp_cfg(svc)
    sb = Sandbox(config, svc.project.root, scopes=["fs:reports:read"], allowed=["reports"])
    server = build_server("m", ToolRegistry([]), sb, ["reports"], 1024 * 1024)

    async def go():
        listed = await server.request_handlers[types.ListResourcesRequest](
            types.ListResourcesRequest(method="resources/list"))
        uris = [str(r.uri) for r in listed.root.resources]
        read = await server.request_handlers[types.ReadResourceRequest](
            types.ReadResourceRequest(method="resources/read", params={"uri": "portwright://fs/reports:notes.txt"}))
        return uris, read.root.contents
    uris, contents = asyncio.run(go())
    assert "portwright://fs/reports:old.pdf" in uris and "portwright://fs/reports:notes.txt" in uris
    assert contents[0].text == "quarterly figures attached"


def test_cli_tool_and_mcp_add(svc, tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from portwright.cli import app
    monkeypatch.chdir(tmp_path)
    r = CliRunner().invoke
    assert r(app, ["tool", "add", "fs", "-t", "filesystem", "--root", "reports", "--op", "list,read"]).exit_code == 0
    assert Project(tmp_path / "portwright.yaml").config.tools["fs"].ops == ["list", "read"]
    res = r(app, ["mcp", "add", "m", "--report", "staff", "--fs", "reports:list,read", "--scope", "reports:run"])
    assert res.exit_code == 0, res.output
    res = r(app, ["mcp", "tools", "m"])
    assert "run_report_staff" in res.output and "fs_read" in res.output
    res = r(app, ["agent", "templates"])
    assert "report-author" in res.output
    res = r(app, ["agent", "from-template", "report-author", "author", "--model", "local"])
    assert res.exit_code == 1 and "different tool 'fs'" in res.output      # the fs tool above differs
