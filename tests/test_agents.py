import asyncio
import json
import sqlite3

import httpx
import pytest

from portwright.agents.llm import LlmProvider, LlmResponse, ToolCall, create_provider
from portwright.agents.llm.base import LlmError
from portwright.agents.loop import run_agent
from portwright.agents.models import check_agent_models, load_models
from portwright.agents.tools import ToolError, ToolRuntime, build_registry, mcp_tools
from portwright.config import ConfigError, ProjectConfig
from portwright.services import Project, Services


class FakeLlm(LlmProvider):
    """Scripted model: returns the queued responses in order (the last one repeats)."""

    def __init__(self, script):
        super().__init__("fake", None, httpx.AsyncClient())
        self.script, self.calls = list(script), []

    async def chat(self, system, messages, tools):
        self.calls.append({"system": system, "messages": list(messages), "tools": [t["name"] for t in tools]})
        return self.script.pop(0) if len(self.script) > 1 else self.script[0]


def call(_tool, **args):
    return LlmResponse("", [ToolCall("c1", _tool, args)], 10, 5)


def final(text):
    return LlmResponse(text, [], 10, 5)


@pytest.fixture
def env(tmp_path):
    s = Services(Project.init(tmp_path, "t"))
    c = sqlite3.connect(tmp_path / "data" / "hr.db")
    c.execute("create table emp(id integer primary key, name varchar(20) not null, dept text)")
    c.execute("create table secret(id integer primary key, v text)")
    c.execute("insert into emp(name, dept) values ('Ann','ENG'), ('Bob','OPS')")
    c.execute("insert into secret(v) values ('x')")
    c.commit()
    c.close()
    s.credentials.add("db", "database", {"driver": "sqlite", "database": str(tmp_path / "data" / "hr.db")})
    s.sources.add("hr", "sql", credential="db", read_write=True)
    cols = s.sources.columns("hr", "emp")
    s.endpoints.crud_from_table("emp", "hr", "emp", "id", cols, ["name", "dept"], ["dept"], ["list", "get"])
    (tmp_path / "skills").mkdir()
    (tmp_path / "skills" / "audit.md").write_text("---\ndescription: audit steps\n---\nStep 1: list employees.")
    data = s.project.config.model_dump(mode="json", by_alias=True)
    data.update({
        "llm_models": {"fake": {"base_url": "https://llm.test/v1", "model": "m"}},
        "tools": {"list_emp": {"type": "endpoint", "endpoint": "emp-list"},
                  "sql": {"type": "source", "source": "hr", "max_rows": 1}},
        "skills": {"audit": {"file": "skills/audit.md"}},
        "agents": {"a": {"model": "fake", "tools": ["list_emp", "sql"], "skills": ["audit"], "scopes": ["emp:read"],
                         "max_steps": 4},
                   "noscope": {"model": "fake", "tools": ["list_emp"], "scopes": []}},
    })
    s.project.config = ProjectConfig.model_validate(data)
    return s


def run(env, agent, script, task="go", **kw):
    llm = FakeLlm(script)
    res = asyncio.run(run_agent(env.project.config, env.project.root, env.project.credentials(), agent, task,
                                provider_factory=lambda n, s: llm, save=False, **kw))
    return res, llm


def test_agent_uses_endpoint_tool_then_answers(env):
    res, llm = run(env, "a", [call("list_emp", dept="OPS"), final("Bob is in OPS")])
    assert res.ok and res.answer == "Bob is in OPS" and len(res.steps) == 2 and res.tokens == 30
    rec = res.steps[0].tool_calls[0]
    assert rec.ok and "Bob" in rec.result and "Ann" not in rec.result
    assert llm.calls[0]["tools"] == ["list_emp", "sql", "use_skill"]
    assert "audit: audit steps" in llm.calls[0]["system"]
    assert llm.calls[1]["messages"][-1]["role"] == "tool"


def test_scope_denied_is_fed_back_and_loop_continues(env):
    res, _ = run(env, "noscope", [call("list_emp"), final("cannot")])
    rec = res.steps[0].tool_calls[0]
    assert not rec.ok and "403" in rec.result and res.ok


def test_max_steps_stops_a_looping_agent(env):
    res, _ = run(env, "a", [call("list_emp")])
    assert res.stopped_by == "max_steps" and len(res.steps) == 4


def test_token_budget_stops(env):
    env.project.config.agents["a"].max_tokens_budget = 20
    res, _ = run(env, "a", [call("list_emp"), call("list_emp"), final("x")])
    assert res.stopped_by == "budget" and len(res.steps) == 2


def test_skill_tool_and_unknown_tool(env):
    res, _ = run(env, "a", [call("use_skill", name="audit"), call("nope"), final("ok")])
    assert res.steps[0].tool_calls[0].result == "Step 1: list employees."
    assert not res.steps[1].tool_calls[0].ok and "unknown tool" in res.steps[1].tool_calls[0].result


def test_sql_tool_is_read_only_and_capped(env):
    res, _ = run(env, "a", [call("sql", query="select name from emp order by id"),
                            call("sql", query="delete from emp"), call("sql", query="select 1; select 2"),
                            final("done")], )
    ok, delete, multi = res.steps[0].tool_calls[0], res.steps[1].tool_calls[0], res.steps[2].tool_calls[0]
    body = json.loads(ok.result)
    assert body["rows"] == [{"name": "Ann"}] and body["truncated"] is True      # max_rows: 1
    assert not delete.ok and "only SELECT" in delete.result
    assert not multi.ok and "one statement" in multi.result
    assert sqlite3.connect(env.project.root / "data" / "hr.db").execute("select count(*) from emp").fetchone()[0] == 2


def test_mcp_table_restriction_and_describe(env):
    cfg = env.project.config.model_dump(mode="json", by_alias=True)
    cfg["mcp_servers"] = {"hr_server": {"scopes": ["emp:read"], "expose": {
        "endpoints": ["emp-get"], "sources": [{"name": "hr", "tables": ["emp"]}]}}}
    cfg["tools"]["hr_mcp"] = {"type": "mcp", "server": "hr_server"}
    cfg = ProjectConfig.model_validate(cfg)

    async def go():
        async with ToolRuntime(cfg, env.project.root, env.project.credentials(), ["emp:read"], "t") as rt:
            reg = build_registry(rt, ["hr_mcp"], cfg.tools)
            assert sorted(reg.tools) == ["hr_mcp__describe_hr", "hr_mcp__emp-get", "hr_mcp__query_hr"]
            assert "name" in json.dumps(await reg.call("hr_mcp__describe_hr", {}))
            assert (await reg.call("hr_mcp__query_hr", {"query": "select name from emp"}))["count"] == 2
            for q in ("select v from secret", "select * from emp, secret", "select * from emp join secret on 1=1"):
                with pytest.raises(ToolError):
                    await reg.call("hr_mcp__query_hr", {"query": q})
    asyncio.run(go())


def test_config_rejects_bad_references(env):
    base = env.project.config.model_dump(mode="json", by_alias=True)
    for patch, msg in [({"agents": {"x": {"model": "fake", "tools": ["ghost"]}}}, "unknown tool"),
                       ({"tools": {"t": {"type": "endpoint", "endpoint": "ghost"}}}, "unknown endpoint"),
                       ({"workflows": {"w": {"steps": [{"id": "s", "type": "agent", "agent": "ghost"}]}}},
                        "unknown agent"),
                       ({"mcp_servers": {"m": {"expose": {"sources": [{"name": "nope"}]}}}}, "SQL source")]:
        with pytest.raises(Exception, match=msg):
            ProjectConfig.model_validate({**base, **patch})
    ProjectConfig.model_validate({k: v for k, v in base.items() if k not in ("agents", "tools")})  # old YAML ok


def test_models_load_from_files(env):
    (env.project.root / "models").mkdir()
    (env.project.root / "models" / "corp.yaml").write_text(
        "corp:\n  base_url: https://llm.corp/v1\n  model: gpt-x\n  tls: {ca_bundle: certs/ca.pem}\n")
    cfg = env.project.config.model_dump(mode="json", by_alias=True)
    cfg["llm_models_include"] = ["models/*.yaml"]
    cfg["agents"]["b"] = {"model": "corp"}
    cfg["agents"]["bad"] = {"model": "ghost"}
    cfg = ProjectConfig.model_validate(cfg)
    assert set(load_models(cfg, env.project.root)) == {"fake", "corp"}
    assert check_agent_models(cfg, env.project.root) == ["agent 'bad' uses unknown model 'ghost'"]
    cfg.llm_models_include = ["nothing/*.yaml"]
    with pytest.raises(ConfigError, match="matches no files"):
        load_models(cfg, env.project.root)


def test_openai_client_request_and_response(env):
    seen = {}

    def handler(request: httpx.Request):
        seen["url"], seen["auth"] = str(request.url), request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "t1", "type": "function", "function": {"name": "list_emp", "arguments": "{\"dept\": \"ENG\"}"}}]},
            "finish_reason": "tool_calls"}], "usage": {"prompt_tokens": 7, "completion_tokens": 3}})

    env.credentials.add("llm_key", "bearer", {"token": "sk-secret"})
    manager = env.project.credentials()
    cfg = env.project.config
    cfg.security.allowed_llm_hosts = ["llm.test"]
    spec = cfg.llm_models["fake"].model_copy(update={"credential": "llm_key"})
    p = create_provider("fake", spec, cfg.security, env.project.root, manager,
                        transport=httpx.MockTransport(handler))
    r = asyncio.run(p.chat("sys", [{"role": "user", "content": "hi"}],
                           [{"name": "list_emp", "description": "d", "parameters": {"type": "object"}}]))
    assert seen["url"] == "https://llm.test/v1/chat/completions" and seen["auth"] == "Bearer sk-secret"
    assert seen["body"]["messages"][0] == {"role": "system", "content": "sys"}
    assert seen["body"]["tools"][0]["function"]["name"] == "list_emp"
    assert r.tool_calls[0].arguments == {"dept": "ENG"} and r.total_tokens == 10


def test_plain_api_key_credential_is_sent_as_bearer_and_new_openai_models_work(env):
    seen = []

    def handler(request: httpx.Request):
        body = json.loads(request.content)
        seen.append((request.headers.get("authorization"), request.headers.get("x-api-key"), body))
        if "max_tokens" in body:
            return httpx.Response(400, json={"error": {"message": "Unsupported parameter: 'max_tokens' is not "
                                                       "supported with this model. Use 'max_completion_tokens'."}})
        if "temperature" in body:
            return httpx.Response(400, json={"error": {"message": "Unsupported value: 'temperature' does not support 0.2"}})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ready"}, "finish_reason": "stop"}],
                                         "usage": {"prompt_tokens": 1, "completion_tokens": 1}})

    env.credentials.add("openai_key", "api_key", {"key": "sk-plain"})       # no header set: must not become X-API-Key
    cfg = env.project.config
    cfg.security.allowed_llm_hosts = ["llm.test"]
    spec = cfg.llm_models["fake"].model_copy(update={"credential": "openai_key", "temperature": 0.2})
    p = create_provider("fake", spec, cfg.security, env.project.root, env.project.credentials(),
                        transport=httpx.MockTransport(handler))
    assert asyncio.run(p.chat("", [{"role": "user", "content": "hi"}], [])).text == "ready"
    assert all(a == "Bearer sk-plain" and x is None for a, x, _ in seen)
    assert "max_completion_tokens" in seen[-1][2] and "max_tokens" not in seen[-1][2] and "temperature" not in seen[-1][2]


def test_rejected_key_gets_a_helpful_message(env):
    cfg = env.project.config
    cfg.security.allowed_llm_hosts = ["llm.test"]
    p = create_provider("fake", cfg.llm_models["fake"], cfg.security, env.project.root,
                        transport=httpx.MockTransport(lambda r: httpx.Response(401, text="bad key")))
    with pytest.raises(LlmError, match="API key was rejected"):
        asyncio.run(p.chat("", [{"role": "user", "content": "x"}], []))


def test_llm_host_must_be_allowed_and_errors_hide_keys(env):
    cfg = env.project.config
    with pytest.raises(ConfigError, match="allowed_llm_hosts"):
        create_provider("fake", cfg.llm_models["fake"], cfg.security, env.project.root)
    cfg.security.allowed_llm_hosts = ["llm.test"]
    p = create_provider("fake", cfg.llm_models["fake"], cfg.security, env.project.root,
                        transport=httpx.MockTransport(lambda r: httpx.Response(500, text="boom")))
    with pytest.raises(LlmError, match="HTTP 500"):
        asyncio.run(p.chat("", [{"role": "user", "content": "x"}], []))


def test_mcp_server_lists_and_calls_tools(env):
    pytest.importorskip("mcp")
    from mcp.shared.memory import create_connected_server_and_client_session

    from portwright.agents.tools import ToolRegistry
    from portwright.mcp import build_server, describe_server
    cfg = env.project.config.model_dump(mode="json", by_alias=True)
    cfg["mcp_servers"] = {"s": {"scopes": ["emp:read"], "expose": {
        "endpoints": ["emp-list"], "sources": [{"name": "hr", "tables": ["emp"]}]}}}
    cfg = ProjectConfig.model_validate(cfg)
    assert [t["tool"] for t in describe_server(cfg, "s")] == ["emp-list", "describe_hr", "query_hr"]

    async def go():
        async with ToolRuntime(cfg, env.project.root, env.project.credentials(), ["emp:read"], "mcp:s") as rt:
            server = build_server("s", ToolRegistry(mcp_tools(rt, cfg.mcp_servers["s"])))
            async with create_connected_server_and_client_session(server) as client:
                names = [t.name for t in (await client.list_tools()).tools]
                assert names == ["emp-list", "describe_hr", "query_hr"]
                ok = await client.call_tool("query_hr", {"query": "select name from emp order by id"})
                assert not ok.isError and "Ann" in ok.content[0].text
                bad = await client.call_tool("query_hr", {"query": "select v from secret"})
                assert bad.isError
    asyncio.run(go())


def test_workflow_steps_templating_and_conditions(env):
    from portwright.agents.workflow import run_workflow, truthy
    assert truthy("a == a") and not truthy("a == b") and truthy("x != y") and not truthy("false") and truthy("[1]")
    cfg = env.project.config.model_dump(mode="json", by_alias=True)
    cfg["agents"]["a"]["max_steps"] = 3
    cfg["workflows"] = {"w": {"scopes": ["emp:read"], "steps": [
        {"id": "pull", "type": "tool", "tool": "list_emp", "input": {"dept": "${input.dept}"}},
        {"id": "pick", "type": "set", "value": "${steps.pull.output.items[0].name}"},
        {"id": "ask", "type": "agent", "agent": "a", "input": "Who is ${steps.pick.output}?"},
        {"id": "chk", "type": "condition", "when": "${steps.ask.output.flagged}", "then": "yes", "else": "end"},
        {"id": "yes", "type": "set", "value": "flagged!"}],
        "output": {"who": "${steps.pick.output}", "agent": "${steps.ask.output}"}},
        "bad": {"steps": [{"id": "t", "type": "tool", "tool": "list_emp", "on_error": "continue"},
                          {"id": "z", "type": "set", "value": "after"}]}}
    cfg = ProjectConfig.model_validate(cfg)
    llm = FakeLlm([final('{"flagged": false}')])
    r = asyncio.run(run_workflow(cfg, env.project.root, env.project.credentials(), "w", {"dept": "OPS"},
                                 provider_factory=lambda n, s: llm, save=False))
    assert r.ok and r.output == {"who": "Bob", "agent": {"flagged": False}}
    assert [s.id for s in r.steps] == ["pull", "pick", "ask", "chk"]        # 'yes' skipped via else: end
    assert "Who is Bob?" in llm.calls[0]["messages"][0]["content"]
    # a failing tool step (no scopes -> 403) with on_error: continue lets the workflow go on
    r2 = asyncio.run(run_workflow(cfg, env.project.root, env.project.credentials(), "bad", save=False))
    assert r2.ok and not r2.steps[0].ok and r2.output == "after"


def test_cli_define_and_inspect(env):
    from typer.testing import CliRunner

    from portwright.cli import app
    r = CliRunner()
    cfg = str(env.project.path)

    def cli(*a):
        return r.invoke(app, [*a, "-c", cfg])
    out = cli("model", "add", "corp", "--base-url", "https://llm.corp/v1", "--model", "gpt-x", "--ca-bundle", "ca.pem")
    assert out.exit_code == 0, out.output
    assert "llm.corp" in Services.open(cfg).project.config.security.allowed_llm_hosts
    assert cli("tool", "add", "emps", "-t", "endpoint", "--endpoint", "emp-list").exit_code == 0
    assert cli("agent", "add", "bot", "--model", "corp", "--tool", "emps", "--max-steps", "3").exit_code == 0
    bad = cli("agent", "add", "bad", "--model", "corp", "--tool", "ghost")
    assert bad.exit_code == 1 and "unknown tool" in bad.output
    assert "bot" in cli("agent", "list").output
    assert cli("tool", "remove", "emps").exit_code == 1                      # still used by agent bot
    assert cli("mcp", "add", "m", "--endpoint", "emp-list", "--source", "hr", "--table", "emp",
               "--scope", "emp:read").exit_code == 0
    tools = cli("mcp", "tools", "m").output
    assert "emp-list" in tools and "query_hr" in tools
    problems = Services.open(cfg).agents.validate()
    assert any("TLS file 'ca.pem' not found" in p for p in problems)


def test_agent_http_route(env, monkeypatch):
    from fastapi.testclient import TestClient

    from portwright.server import create_app
    from portwright.security.hashing import generate_api_key, hash_api_key
    from portwright.config import ApiKeyRecord
    llm = FakeLlm([call("list_emp", dept="ENG"), final("Ann")])
    monkeypatch.setattr("portwright.agents.loop.create_provider", lambda *a, **k: llm)
    cfg = env.project.config
    kid, key = generate_api_key()
    cfg.security.api_keys.append(ApiKeyRecord(id=kid, name="t", hash=hash_api_key(key), scopes=["agent:run"],
                                              created="2026-01-01T00:00:00+00:00"))
    app = create_app(cfg, project_root=env.project.root, credentials=env.project.credentials())
    with TestClient(app) as c:
        h = {"X-API-Key": key}
        assert c.post("/api/_agents/a/run", json={"task": "x"}).status_code == 401
        r = c.post("/api/_agents/a/run", json={"task": "who is in ENG?"}, headers=h)
        assert r.status_code == 200 and r.json()["answer"] == "Ann"
        assert r.json()["steps"][0]["tools"] == ["list_emp"] and "Ann" not in json.dumps(r.json()["steps"])
        assert c.post("/api/_agents/nope/run", json={"task": "x"}, headers=h).status_code == 404
