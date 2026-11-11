import asyncio

import pytest

from portwright.agents.llm import LlmResponse
from portwright.agents.workflow import run_workflow
from portwright.config import ProjectConfig
from portwright.studio import graph_vm as gv

STEPS = [
    {"id": "pull", "type": "tool", "tool": "emps", "input": {"dept": "${input.dept}"}},
    {"id": "check", "type": "condition", "when": "${steps.pull.output.count} != 0", "then": "ask"},
    {"id": "skip", "type": "set", "value": "nothing to do", "next": "end"},
    {"id": "ask", "type": "agent", "agent": "bot", "input": "Review ${steps.pull.output}"},
]


def test_from_steps_links_follow_runtime_semantics():
    g = gv.from_steps(STEPS)
    assert [n.id for n in g.nodes] == ["pull", "check", "skip", "ask"]
    assert g.links[("pull", "next")] == "check"                # default: the following step
    assert g.links[("check", "then")] == "ask"
    assert g.links[("check", "else")] == "skip"                # no 'else': falls through to the next in the list
    assert ("skip", "next") not in g.links                     # next: end
    assert ("ask", "next") not in g.links                      # last step
    assert all(n.x or n.y for n in g.nodes)                    # laid out automatically


def test_roundtrip_is_explicit_and_stable():
    g = gv.from_steps(STEPS)
    steps = gv.to_steps(g)
    by = {s["id"]: s for s in steps}
    assert steps[0]["id"] == "pull" and by["pull"]["next"] == "check"
    assert by["check"]["then"] == "ask" and by["check"]["else"] == "skip"
    assert by["skip"]["next"] == "end" and by["ask"]["next"] == "end"
    assert by["pull"]["input"] == {"dept": "${input.dept}"} and by["ask"]["input"] == "Review ${steps.pull.output}"
    assert gv.to_steps(gv.from_steps(steps)) == steps          # saving twice changes nothing


def test_editing_operations():
    g = gv.from_steps(STEPS)
    n = gv.add_node(g, "set", 500, 40)
    assert n.id == "set_1" and gv.add_node(g, "set").id == "set_2"
    gv.connect(g, "ask", "next", "set_1")
    with pytest.raises(ValueError):
        gv.connect(g, "ask", "then", "set_1")                  # an agent has no 'then' port
    gv.rename_node(g, "ask", "review")
    assert g.links[("check", "then")] == "review" and g.links[("review", "next")] == "set_1"
    with pytest.raises(ValueError, match="already exists"):
        gv.rename_node(g, "review", "pull")
    gv.delete_node(g, "set_1")
    assert ("review", "next") not in g.links
    gv.set_start(g, "check")
    assert g.start == "check"
    gv.disconnect(g, "check", "else")
    assert ("check", "else") not in g.links


def test_problems_and_geometry():
    g = gv.from_steps(STEPS)
    assert gv.problems(g, ["emps"], ["bot"]) == []
    gv.add_node(g, "tool")
    probs = gv.problems(g, ["emps"], ["bot"])
    assert any("choose a tool" in p for p in probs) and any("never reached" in p for p in probs)
    assert gv.problems(gv.Graph(), [], []) == ["Add at least one step."]
    a, b = gv.port_point(g.node("pull"), "next"), gv.port_point(g.node("check"), None)
    c1, c2 = gv.link_curve(a, b)
    assert c1[0] > a[0] and c2[0] < b[0]
    assert gv.port_point(g.node("check"), "then")[1] < gv.port_point(g.node("check"), "else")[1]


def test_saved_graph_validates_and_runs_with_next(tmp_path):
    """x/y/next are accepted by the project config, and a 'next' jump skips the steps in between."""
    cfg = ProjectConfig.model_validate({
        "agents": {}, "tools": {}, "workflows": {"w": {"steps": [
            {"id": "a", "type": "set", "value": "1", "next": "c", "x": 10, "y": 20},
            {"id": "b", "type": "set", "value": "never", "next": "end"},
            {"id": "c", "type": "set", "value": "3", "next": "end"}]}}})
    r = asyncio.run(run_workflow(cfg, tmp_path, None, "w", save=False))
    assert r.ok and [s.id for s in r.steps] == ["a", "c"] and r.output == "3"
    with pytest.raises(Exception, match="unknown target step"):
        ProjectConfig.model_validate({"workflows": {"w": {"steps": [
            {"id": "a", "type": "set", "value": "1", "next": "ghost"}]}}})


# ------------------------------------------------------------------ canvas UI
from types import SimpleNamespace

from portwright.services import ServiceError  # noqa: E402

from test_studio_agents import FakeCtx, svc  # noqa: E402,F401  (fixtures)
from portwright.studio import agent_vm as vm  # noqa: E402
from portwright.studio.views.agents import AgentsView  # noqa: E402
from portwright.studio.views.workflow_canvas import WorkflowCanvas, open_editor  # noqa: E402


def test_canvas_build_connect_drag_save(svc):
    vm.save_item(svc, "tool", {"name": "emps", "type": "endpoint", "endpoint": "emp-list"}, False)
    ctx = FakeCtx(svc)
    ed = WorkflowCanvas(ctx, None)
    assert ed.save() is False and ed.f_name.error                       # needs a name
    ed.add("tool")
    ed.add("condition")
    ed.add("set")
    a, b, c = (n.id for n in ed.graph.nodes)
    assert (a, b, c) == ("tool_1", "condition_2", "set_3") or [n.type for n in ed.graph.nodes] == ["tool", "condition", "set"]
    ed.graph.node(a).tool = "emps"
    ed.graph.node(b).when = "${steps.%s.output.count} != 0" % a
    ed.graph.node(c).value = "done"
    ed.start_link(a, "next")
    assert ed.linking == (a, "next")
    ed.click(b)                                                          # finishes the link
    ed.start_link(b, "then")
    ed.click(c)
    assert ed.graph.links[(a, "next")] == b and ed.graph.links[(b, "then")] == c and ed.linking is None
    x0 = ed.graph.node(c).x
    ed.drag(c, SimpleNamespace(local_delta=SimpleNamespace(x=25, y=-1000)))
    n = ed.graph.node(c)
    assert n.x == x0 + 25 and n.y == 0 and ed.cards[c].left == n.x         # clamped at the top edge
    ed.start_link(b, "else")
    ed.start_link(b, "else")                                             # second click cancels
    assert ed.linking is None
    ed.f_name.value = "visual"
    assert ed.save() is True
    saved = svc.project.config.workflows["visual"]
    assert [s.id for s in saved.steps] == [a, b, c]
    assert saved.steps[0].next == b and saved.steps[1].then == c and saved.steps[1].else_ == "end"
    assert saved.steps[2].x == round(n.x) and saved.steps[2].y == 0
    # reopen from the saved workflow: same graph, positions kept
    again = WorkflowCanvas(ctx, "visual")
    assert [(m.id, m.x, m.y) for m in again.graph.nodes] == [(m.id, m.x, m.y) for m in ed.graph.nodes]
    again.unlink(b, "then")
    assert (b, "then") not in again.graph.links
    again.delete(c)
    assert again.graph.node(c) is None and again.selected == again.graph.start
    assert ctx.errors == []


def test_canvas_inspector_edits_and_validation(svc):
    vm.save_item(svc, "tool", {"name": "emps", "type": "endpoint", "endpoint": "emp-list"}, False)
    ctx = FakeCtx(svc)
    ed = WorkflowCanvas(ctx, None)
    ed.add("tool")
    ed.f_name.value = "v2"
    inspector = ed.inspector.controls                                    # id field, tool dropdown, arguments, ...
    tool_dd = next(c for c in inspector if getattr(c, "label", None) == "Tool")
    tool_dd.value = "emps"
    tool_dd.on_select(None)
    assert ed.graph.nodes[0].tool == "emps" and "emps" in ed.cards["tool_1"].content.content.controls[1].value
    args = next(c for c in inspector if getattr(c, "label", None) == "Arguments (YAML)")
    args.value = "dept: ENG"
    args.on_change(None)
    id_field = inspector[1]
    id_field.value = "pull"
    id_field.on_blur(None)
    assert ed.graph.nodes[0].id == "pull" and ed.selected == "pull"
    ed.add("agent")                                                      # no agent chosen: a problem is shown, save fails
    assert any("choose an agent" in str(t.value) for r in ed.problems.controls for t in r.controls if hasattr(t, "value"))
    assert ed.save() is False and ctx.errors
    ed.delete("agent_1")
    assert ed.save() is True
    assert svc.project.config.workflows["v2"].steps[0].input == {"dept": "ENG"}


def test_agents_screen_opens_visual_editor(svc):
    vm.save_item(svc, "workflow", {"name": "wf", "description": "", "scopes": "", "output": "",
                                   "steps": "steps:\n  - {id: a, type: set, value: hi}\n"}, False)
    ctx = FakeCtx(svc)
    view = AgentsView(ctx)
    view.section = "workflow"
    view.refresh(update=False)
    view.visual_editor("wf")
    title, body, actions = ctx.dialogs[-1]
    assert title.startswith("Workflow editor") and ctx.errors == []
    open_editor(ctx, None)
    assert len(ctx.dialogs) == 2


def test_visual_editor_save_closes_before_toasting(svc):
    vm.save_item(svc, "workflow", {"name": "wf", "description": "", "scopes": "", "output": "",
                                   "steps": "steps:\n  - {id: a, type: set, value: hi}\n"}, False)
    order, saved = [], []
    ctx = FakeCtx(svc)
    ctx.close = lambda: order.append("close")
    ctx.toast = lambda msg, error=False: order.append("toast")
    open_editor(ctx, "wf", on_saved=lambda: saved.append(1))
    ctx.dialogs[-1][2][-1].on_click(None)                               # the Save button
    assert order == ["close", "toast"] and saved == [1]                 # closing a dialog under a toast pops the toast


def test_tool_node_can_pick_a_source_or_endpoint_directly(svc):
    """No tools defined yet: the node offers the SQL source and endpoint, and creates the tool behind them."""
    ctx = FakeCtx(svc)
    ed = WorkflowCanvas(ctx, None)
    ed.add("tool")
    assert ed.tools == []
    labels = [getattr(c, "label", None) for c in ed.inspector.controls]
    assert "Query a SQL source" in labels and "Call an endpoint" in labels
    assert any("No tools yet" in str(getattr(c, "value", "")) for c in ed.inspector.controls)
    node = ed.graph.nodes[0]
    next(c for c in ed.inspector.controls if getattr(c, "label", None) == "Query a SQL source").on_select(
        SimpleNamespace(control=SimpleNamespace(value="hr")))
    assert node.tool == "hr_sql" and node.input.startswith("query:")
    tool = svc.project.config.tools["hr_sql"]
    assert tool.type == "source" and tool.source == "hr" and tool.read_only
    next(c for c in ed.inspector.controls if getattr(c, "label", None) == "Call an endpoint").on_select(
        SimpleNamespace(control=SimpleNamespace(value="emp-list")))
    assert node.tool == "emp-list" and svc.project.config.tools["emp-list"].endpoint == "emp-list"
    assert ed.tools == ["hr_sql", "emp-list"]
    ed.f_name.value = "from_source"
    assert ed.save() is True and ctx.errors == []
    ed.graph.nodes[0].tool = "hr_sql"
    ed.tool_from_source(node, "hr")                                     # picking again reuses the tool
    assert list(svc.project.config.tools) == ["hr_sql", "emp-list"]


# ------------------------------------------------------- input / output nodes and templates
def test_input_and_output_nodes(tmp_path):
    cfg = ProjectConfig.model_validate({"workflows": {"w": {"steps": [
        {"id": "start", "type": "input", "inputs": {"name": {"description": "who"}, "greeting": "Hello", "n": 3}},
        {"id": "msg", "type": "set", "value": "${input.greeting} ${input.name} x${input.n}"},
        {"id": "ignored", "type": "set", "value": "not the result"},
        {"id": "result", "type": "output", "value": {"text": "${steps.msg.output}"}},
        {"id": "after", "type": "set", "value": "never runs"}]}}})
    ok = asyncio.run(run_workflow(cfg, tmp_path, None, "w", {"name": "Ada"}, save=False))
    assert ok.ok and ok.output == {"text": "Hello Ada x3"}
    assert [s.id for s in ok.steps] == ["start", "msg", "ignored", "result"]       # output ends the workflow
    assert ok.steps[0].output == {"name": "Ada", "greeting": "Hello", "n": 3}
    over = asyncio.run(run_workflow(cfg, tmp_path, None, "w", {"name": "Bo", "greeting": "Hi"}, save=False))
    assert over.output == {"text": "Hi Bo x3"}
    bad = asyncio.run(run_workflow(cfg, tmp_path, None, "w", {}, save=False))
    assert not bad.ok and "missing required input: name" in bad.error
    plain = ProjectConfig.model_validate({"workflows": {"p": {"steps": [{"id": "o", "type": "output"}]}}})
    assert asyncio.run(run_workflow(plain, tmp_path, None, "p", save=False)).output is None


def test_graph_roundtrip_with_input_and_output_nodes():
    steps = [{"id": "start", "type": "input", "inputs": {"q": None, "limit": 5}, "next": "out"},
             {"id": "out", "type": "output", "value": "${input.q}"}]
    g = gv.from_steps(steps)
    assert g.node("out").ports == () and g.links == {("start", "next"): "out"}
    back = gv.to_steps(g)
    assert back[0]["inputs"] == {"q": None, "limit": 5} and back[0]["next"] == "out"
    assert "next" not in back[1] and back[1]["value"] == "${input.q}"
    assert gv.to_steps(gv.from_steps(back)) == back
    with pytest.raises(ValueError):
        gv.connect(g, "out", "next", "start")                  # an output has nothing after it
    g.node("start").inputs = "just text"
    assert any("inputs must be a mapping" in p for p in gv.problems(g, [], []))


def test_every_template_is_a_valid_workflow():
    from portwright.agents.spec import WorkflowSpec
    from portwright.agents.templates import get_template, list_templates, template_yaml
    listing = list_templates()
    assert len(listing) >= 10 and listing[0]["id"] == "hello"
    for t in listing:
        tpl = get_template(t["id"])
        g = gv.from_steps(tpl["steps"])
        assert any(n.type == "input" for n in g.nodes) and any(n.type == "output" for n in g.nodes), t["id"]
        for n in g.nodes:                                       # pick tools, agents, models, loads like a user would
            n.set_target({"tool": "t", "agent": "a", "model": "m", "load": "l", "report": "r"}.get(n.target_kind, ""))
        assert gv.problems(g, ["t"], ["a"], ["m"], ["l"], ["r"]) == [], t["id"]
        WorkflowSpec.model_validate({"steps": gv.to_steps(g)})
        assert "CHOOSE_" in template_yaml(t["id"]) or not gv.blank_choices(gv.from_steps(tpl["steps"])), t["id"]
    with pytest.raises(KeyError):
        get_template("nope")


def test_hello_template_runs_without_tools(tmp_path):
    from portwright.agents.templates import get_template
    t = get_template("hello")
    cfg = ProjectConfig.model_validate({"workflows": {"hello": {"steps": gv.to_steps(gv.from_steps(t["steps"]))}}})
    r = asyncio.run(run_workflow(cfg, tmp_path, None, "hello", t["sample_input"], save=False))
    assert r.ok and r.output == {"greeting": "Hello Ada!"}
    assert asyncio.run(run_workflow(cfg, tmp_path, None, "hello", None, save=False)).output == {"greeting": "Hello world!"}


def test_canvas_starts_from_a_template_and_shows_input_output_nodes(svc):
    vm.save_item(svc, "tool", {"name": "emps", "type": "endpoint", "endpoint": "emp-list"}, False)
    ctx = FakeCtx(svc)
    ed = WorkflowCanvas(ctx, None)
    assert ed.f_template is not None and "template" in ed.hint.value
    tips = [getattr(b, "tooltip", "") or "" for b in ed.body()[0].controls]
    assert {"Add step: Input", "Add step: Output", "Add step: LLM prompt", "Add step: Run load",
            "Add step: For each", "Add step: Switch"} <= set(tips)
    ed.load_template("query-and-summarise")
    assert [n.type for n in ed.graph.nodes] == ["input", "tool", "agent", "output"]
    assert ed.graph.node("query").tool == "emps"              # the only tool is filled in
    assert ed.graph.node("summarise").agent == ""             # no agents yet: left to pick
    assert ed.f_name.value == "query_and_summarise" and "Pick the agent" in ed.hint.value
    ed.selected = "start"
    ed.rebuild()
    assert any(getattr(c, "label", None) == "Inputs (YAML)" for c in ed.inspector.controls)
    ed.selected = "result"
    ed.rebuild()
    assert any(getattr(c, "label", None) == "Result" for c in ed.inspector.controls)
    assert not any(getattr(c, "label", None) == "If this step fails" for c in ed.inspector.controls)
    assert ed.save() is False and ctx.errors                  # the agent is still to be chosen
    ed.graph.node("summarise").agent = "bot"
    ctx.errors.clear()
    ed.load_template("hello")
    ed.f_name.value = "greeter"
    assert ed.save() is True
    saved = svc.project.config.workflows["greeter"]
    assert [s.type for s in saved.steps] == ["input", "set", "output"] and saved.steps[0].inputs == {"name": "world"}
    assert svc.agents.workflow_inputs("greeter")["name"]["default"] == "world"
    again = WorkflowCanvas(ctx, "greeter")
    assert again.f_template is None                           # existing workflows are not replaced by a template
    assert "tool: CHOOSE_TOOL" in svc.agents.template_yaml("query-and-summarise")
    with pytest.raises(ServiceError, match="no template"):
        svc.agents.template("nope")


def test_run_dialog_prefills_declared_inputs(svc):
    vm.save_item(svc, "workflow", {"name": "g", "description": "", "scopes": "", "output": "", "steps":
                                   "steps:\n  - {id: s, type: input, inputs: {name: world, q: {description: ask}}}\n"}, False)
    ctx = FakeCtx(svc)
    AgentsView(ctx).run_workflow_dialog("g")
    field = ctx.dialogs[-1][1][0]
    assert field.value == "name=world\nq="


# ------------------------------------------- LLM prompt, Run load, For each and Switch nodes
import httpx  # noqa: E402

from portwright.agents import workflow as wfmod  # noqa: E402
from portwright.agents.llm import LlmError, LlmProvider  # noqa: E402
from portwright.agents.models import check_agent_models  # noqa: E402


class _Echo(LlmProvider):
    """A scripted model: reply(prompt) -> text; a prompt containing fail_on raises."""

    def __init__(self, reply=lambda t: "re:" + t, fail_on=None):
        super().__init__("m", None, httpx.AsyncClient())
        self.reply, self.fail_on, self.prompts = reply, fail_on, []

    async def chat(self, system, messages, tools):
        text = messages[-1]["content"]
        self.prompts.append((system, text))
        if self.fail_on and self.fail_on in text:
            raise LlmError("boom")
        return LlmResponse(self.reply(text), [], 3, 2)

    async def aclose(self):
        pass


def _wf(steps, **extra):
    return ProjectConfig.model_validate({"llm_models": {"m": {"base_url": "https://llm.test/v1", "model": "x"}},
                                         "workflows": {"w": {"steps": steps, **extra}}})


def _go(cfg, tmp_path, inp=None, echo=None):
    echo = echo or _Echo()
    return asyncio.run(run_workflow(cfg, tmp_path, None, "w", inp, provider_factory=lambda n, s: echo, save=False))


def test_llm_step_text_json_and_errors(tmp_path):
    echo = _Echo(lambda t: '```json\n{"label": "bug"}\n```' if "classify" in t else "plain answer")
    cfg = _wf([{"id": "a", "type": "llm", "model": "m", "system": "Be brief", "input": "say ${input.word}"},
               {"id": "b", "type": "llm", "model": "m", "format": "json", "input": "classify it"}])
    r = _go(cfg, tmp_path, {"word": "hi"}, echo)
    assert r.ok and r.steps[0].output == "plain answer" and r.output == {"label": "bug"}
    assert echo.prompts[0] == ("Be brief", "say hi") and "valid JSON" in echo.prompts[1][0]
    assert r.steps[0].tokens == 5
    bad = _go(_wf([{"id": "a", "type": "llm", "model": "m", "format": "json", "input": "x"}]), tmp_path,
              echo=_Echo(lambda t: "not json"))
    assert not bad.ok and "did not return valid JSON" in bad.error
    down = _go(_wf([{"id": "a", "type": "llm", "model": "m", "input": "x"}]), tmp_path, echo=_Echo(fail_on="x"))
    assert not down.ok and "boom" in down.error
    ghost = _go(_wf([{"id": "a", "type": "llm", "model": "ghost", "input": "x"}]), tmp_path)
    assert not ghost.ok and "unknown model 'ghost'" in ghost.error
    with pytest.raises(Exception, match="needs 'model'"):
        _wf([{"id": "a", "type": "llm"}])


def test_load_step_runs_the_named_load(tmp_path):
    from types import SimpleNamespace as NS
    calls = []

    class FakeTool:
        def __init__(self, load):
            self.load = load

        async def run(self, args):
            calls.append(self.load)
            return {"rows_loaded": 12}

    env = wfmod._Env(None, tmp_path, None, rt=NS(load_tool=lambda name, load: FakeTool(load)))
    out, run_id, tokens = asyncio.run(wfmod._do("load", NS(load="nightly"), None, {}, env))
    assert out == {"rows_loaded": 12} and calls == ["nightly"] and tokens == 0
    with pytest.raises(Exception, match="unknown load 'ghost'"):
        ProjectConfig.model_validate({"workflows": {"w": {"steps": [{"id": "l", "type": "load", "load": "ghost"}]}}})
    with pytest.raises(Exception, match="needs 'load'"):
        _wf([{"id": "l", "type": "load"}])


def test_foreach_runs_per_item_and_collects(tmp_path):
    steps = [{"id": "rows", "type": "set", "value": {"rows": [{"n": "a"}, {"n": "b"}, {"n": "boom"}]}},
             {"id": "each", "type": "foreach", "items": "${steps.rows.output.rows}", "do": "llm", "model": "m",
              "input": "row ${index}: ${item.n}", "on_error": "continue"}]
    r = _go(_wf(steps), tmp_path, echo=_Echo(fail_on="boom"))
    assert r.ok and r.output == ["re:row 0: a", "re:row 1: b", {"error": "boom"}] and r.steps[1].tokens == 10
    strict = _go(_wf([steps[0], {**steps[1], "on_error": "fail"}]), tmp_path, echo=_Echo(fail_on="boom"))
    assert not strict.ok and "item 2: boom" in strict.error
    whole = _go(_wf([steps[0], {**steps[1], "items": "${steps.rows.output}"}]), tmp_path, echo=_Echo())
    assert len(whole.output) == 3                                       # a SQL-style {"rows": [...]} works as the list
    capped = _go(_wf([steps[0], {**steps[1], "max_items": 2, "on_error": "fail"}]), tmp_path)
    assert not capped.ok and "more than max_items (2)" in capped.error
    notlist = _go(_wf([{"id": "each", "type": "foreach", "items": "${input.x}", "do": "llm", "model": "m",
                        "input": "${item}"}]), tmp_path, {"x": "text"})
    assert not notlist.ok and "must be a list" in notlist.error
    with pytest.raises(Exception, match="foreach needs 'do'"):
        _wf([{"id": "e", "type": "foreach", "items": "${input.x}"}])
    with pytest.raises(Exception, match="unknown tool"):
        ProjectConfig.model_validate({"workflows": {"w": {"steps": [
            {"id": "e", "type": "foreach", "items": "${input.x}", "do": "tool", "tool": "ghost"}]}}})


def test_switch_picks_the_case_or_default(tmp_path):
    def steps(**sw):
        return [{"id": "s", "type": "switch", "when": "${input.kind}", **sw},
                {"id": "b", "type": "set", "value": "bug", "next": "end"},
                {"id": "n", "type": "set", "value": "new", "next": "end"},
                {"id": "d", "type": "set", "value": "other", "next": "end"}]
    cfg = _wf(steps(cases={"Bug": "b", "new": "n"}, default="d"))
    assert [_go(cfg, tmp_path, {"kind": k}).output for k in ("BUG", " new ", "zzz")] == ["bug", "new", "other"]
    fall = _wf([{"id": "s", "type": "switch", "when": "${input.kind}", "cases": {"x": "end"}},
                {"id": "f", "type": "set", "value": "fell through"}])
    assert _go(fall, tmp_path, {"kind": "q"}).output == "fell through"       # no match, no default: the next step
    stopped = _go(fall, tmp_path, {"kind": "x"})
    assert [s.id for s in stopped.steps] == ["s"] and stopped.output == "x"
    with pytest.raises(Exception, match="unknown target step"):
        _wf(steps(cases={"a": "ghost"}))
    with pytest.raises(Exception, match="needs 'when'"):
        _wf([{"id": "s", "type": "switch", "cases": {"a": "end"}}])


def test_workflow_model_references_are_checked(tmp_path):
    cfg = _wf([{"id": "a", "type": "llm", "model": "ghost", "input": "x"},
               {"id": "e", "type": "foreach", "items": "${input.x}", "do": "llm", "model": "nope", "input": "y"}])
    assert check_agent_models(cfg, tmp_path) == ["workflow 'w' step 'a' uses unknown model 'ghost'",
                                                 "workflow 'w' step 'e' uses unknown model 'nope'"]
    assert check_agent_models(_wf([{"id": "a", "type": "llm", "model": "m", "input": "x"}]), tmp_path) == []


SWITCH_STEPS = [
    {"id": "s", "type": "switch", "when": "${input.kind}", "cases": {"bug": "b", "new": "n"}, "default": "d"},
    {"id": "b", "type": "set", "value": "bug", "next": "end"}, {"id": "n", "type": "set", "value": "new", "next": "end"},
    {"id": "d", "type": "set", "value": "other", "next": "end"},
    {"id": "each", "type": "foreach", "items": "${input.rows}", "do": "llm", "model": "m", "input": "${item}",
     "format": "json", "max_items": 7, "on_error": "continue", "next": "end"},
    {"id": "ask", "type": "llm", "model": "m", "system": "Be brief", "input": "hi", "next": "end"},
    {"id": "run", "type": "load", "load": "nightly", "next": "end"}]


def test_graph_roundtrip_for_the_new_nodes():
    g = gv.from_steps(SWITCH_STEPS)
    sw = g.node("s")
    assert sw.ports == ("case:bug", "case:new", "default") and gv.node_h(sw) > gv.NODE_H
    assert g.links[("s", "case:bug")] == "b" and g.links[("s", "default")] == "d"
    ys = [gv.port_point(sw, p)[1] for p in sw.ports]
    assert ys == sorted(ys) and len(set(ys)) == 3                        # each port has its own spot
    each = g.node("each")
    assert (each.do, each.model, each.format, each.max_items) == ("llm", "m", "json", "7") and each.target() == "m"
    steps = gv.to_steps(g)
    by = {s["id"]: s for s in steps}
    assert by["s"]["cases"] == {"bug": "b", "new": "n"} and by["s"]["default"] == "d" and "next" not in by["s"]
    assert by["each"]["do"] == "llm" and by["each"]["model"] == "m" and by["each"]["max_items"] == 7
    assert by["each"]["format"] == "json" and "tool" not in by["each"]
    assert by["ask"]["system"] == "Be brief" and by["run"]["load"] == "nightly"
    assert gv.to_steps(gv.from_steps(steps)) == steps
    from portwright.agents.spec import WorkflowSpec
    WorkflowSpec.model_validate({"steps": steps})


def test_switch_cases_edit_ports_and_prune_links():
    g = gv.from_steps(SWITCH_STEPS)
    sw = g.node("s")
    sw.cases = "bug\nspam, bug\n\n"                                    # repeats and blanks are ignored
    assert gv.case_values(sw.cases) == ["bug", "spam"]
    gv.prune_links(g, sw)
    assert ("s", "case:new") not in g.links and g.links[("s", "case:bug")] == "b" and ("s", "default") in g.links
    gv.connect(g, "s", "case:spam", "n")
    with pytest.raises(ValueError):
        gv.connect(g, "s", "case:ghost", "n")
    assert len(gv.case_values("\n".join(str(i) for i in range(12)))) == gv.MAX_CASES
    fresh = gv.add_node(gv.Graph(), "switch")
    assert gv.case_values(fresh.cases) == ["yes", "no"]


def test_new_node_problems_and_prefill():
    g = gv.from_steps(SWITCH_STEPS)
    unreached = lambda probs: [p for p in probs if "never reached" not in p]          # noqa: E731
    assert unreached(gv.problems(g, [], [], ["m"], ["nightly"])) == []
    assert any("choose a model" in p for p in gv.problems(g, [], [], [], ["nightly"]))
    assert any("choose a load" in p for p in gv.problems(g, [], [], ["m"], []))
    h = gv.Graph()
    for kind in ("llm", "load", "foreach", "switch"):
        gv.add_node(h, kind)
    h.node("foreach_1").items = ""
    probs = gv.problems(h, [], [], ["m"], ["l"])
    assert any("llm_1: choose a model" in p for p in probs) and any("load_1: choose a load" in p for p in probs)
    assert any("foreach_1: enter the list" in p for p in probs) and any("foreach_1: choose a tool" in p for p in probs)
    assert any("switch_1: enter the value to switch on" in p for p in probs)
    assert gv.blank_choices(h) == ["model", "load", "tool"]
    gv.prefill_choices(h, ["t"], ["a"], ["m"], ["l"])
    assert (h.node("llm_1").model, h.node("load_1").load, h.node("foreach_1").tool) == ("m", "l", "t")
    h.node("foreach_1").do = "agent"
    assert h.node("foreach_1").target_kind == "agent"


def test_canvas_edits_the_new_nodes(svc):
    vm.save_item(svc, "model", {"name": "corp", "provider": "openai_compatible", "base_url": "https://llm.corp.local/v1",
                                "model": "m", "credential": "", "ca_bundle": "", "client_cert": "", "client_key": "",
                                "tls_verify": True, "headers": "", "timeout": "60", "max_tokens": "100",
                                "temperature": ""}, False)
    vm.save_item(svc, "tool", {"name": "emps", "type": "endpoint", "endpoint": "emp-list"}, False)
    ctx = FakeCtx(svc)
    ed = WorkflowCanvas(ctx, None)
    assert ed.models == ["corp"] and ed.loads == []
    labels = lambda: [getattr(c, "label", None) for c in ed.inspector.controls]          # noqa: E731
    for kind in ("llm", "load", "foreach", "switch"):
        ed.add(kind)
    llm, load, each, sw = (n.id for n in ed.graph.nodes)
    ed.selected = llm
    ed.rebuild()
    assert {"Model", "Prompt", "System prompt (optional)", "Reply as"} <= set(labels())
    next(c for c in ed.inspector.controls if getattr(c, "label", None) == "Model").value = "corp"
    dd = next(c for c in ed.inspector.controls if getattr(c, "label", None) == "Model")
    dd.on_select(None)
    assert ed.graph.node(llm).model == "corp" and "corp" in ed.cards[llm].content.content.controls[1].value
    ed.selected = load
    ed.rebuild()
    assert any("No loads yet" in str(getattr(c, "value", "")) for c in ed.inspector.controls)
    ed.selected = each
    ed.rebuild()
    assert {"List to loop over", "Run for each item", "Tool", "Input for each item", "Most items allowed"} <= set(labels())
    kind = next(c for c in ed.inspector.controls if getattr(c, "label", None) == "Run for each item")
    kind.value = "llm"
    kind.on_select(SimpleNamespace(control=kind))
    assert ed.graph.node(each).do == "llm" and {"Model", "Reply as"} <= set(labels())
    ed.selected = sw
    ed.rebuild()
    assert {"Value to switch on", "Cases (one per line)"} <= set(labels())
    assert "If this step fails" not in labels()
    ports = lambda: sum(1 for c in ed.stack.controls if getattr(c, "tooltip", "") and "click, then click" in c.tooltip)   # noqa: E731
    before = ports()
    cases = next(c for c in ed.inspector.controls if getattr(c, "label", None) == "Cases (one per line)")
    ed.graph.links[(sw, "case:yes")] = llm
    cases.value = "yes\nmaybe\nno"
    cases.on_change(None)
    cases.on_blur(None)
    assert ports() == before + 1 and ed.graph.node(sw).ports == ("case:yes", "case:maybe", "case:no", "default")
    cases = next(c for c in ed.inspector.controls if getattr(c, "label", None) == "Cases (one per line)")
    cases.value = "maybe"
    cases.on_blur(None)
    assert (sw, "case:yes") not in ed.graph.links and ed.graph.node(sw).ports == ("case:maybe", "default")
    assert ctx.errors == []


def test_canvas_saves_a_workflow_with_the_new_nodes(svc):
    vm.save_item(svc, "model", {"name": "corp", "provider": "openai_compatible", "base_url": "https://llm.corp.local/v1",
                                "model": "m", "credential": "", "ca_bundle": "", "client_cert": "", "client_key": "",
                                "tls_verify": True, "headers": "", "timeout": "60", "max_tokens": "100",
                                "temperature": ""}, False)
    ctx = FakeCtx(svc)
    ed = WorkflowCanvas(ctx, None)
    ed.load_template("classify-and-route")                 # the only model is filled in on every LLM node
    assert [n.model for n in ed.graph.nodes if n.type == "llm"] == ["corp"] * 3
    assert ed.graph.node("route").ports == ("case:billing", "case:bug", "default")
    ed.f_name.value = "router"
    assert ed.save() is True and ctx.errors == []
    saved = svc.project.config.workflows["router"]
    route = next(s for s in saved.steps if s.id == "route")
    assert route.cases == {"billing": "billing_reply", "bug": "bug_reply"} and route.default == "human"
    ed.load_template("summarise-each-row")
    assert "Pick the tool" in ed.hint.value and "model" not in ed.hint.value


def test_editor_follows_the_window_size_and_can_fill_it(svc):
    from types import SimpleNamespace as NS
    ctx = FakeCtx(svc)
    ctx.page = NS(width=1000, height=700, update=lambda: None, run_task=lambda *a: None)
    ed = WorkflowCanvas(ctx, None)
    width, height = ed.fit_sizes()
    assert width == 904 and height == 370                      # window minus margins, not the fixed 1220 x 420
    assert ed.area.height == 370 and ed.inspector.height == 370 and ed.col.height == 368
    ed.dialog = NS(content=NS(width=width), update=lambda: None)
    ed.toggle_size()
    assert ed.big and ed.dialog.content.width == 960 and ed.area.height == 450 and ed.inspector.height == 450
    assert ed.f_big.tooltip == "Back to normal size"
    ed.toggle_size()
    assert not ed.big and ed.dialog.content.width == 904 and ed.f_big.tooltip == "Fill the window"
    small = FakeCtx(svc)
    small.page = NS(width=500, height=300)
    assert WorkflowCanvas(small, None).fit_sizes() == (640, 320)    # never smaller than a usable editor


def test_dialogs_never_exceed_the_window_and_scroll(svc):
    import flet as ft
    from types import SimpleNamespace as NS
    from portwright.studio.common import Ctx
    shown = []
    page = NS(width=700, height=500, show_dialog=lambda d: shown.append(d))
    Ctx(page, svc, lambda k: None).dialog("t", [ft.Text("x")], [], width=1220)
    dlg = shown[0]
    assert dlg.content.width == 604 and dlg.content.content.scroll == ft.ScrollMode.ALWAYS
