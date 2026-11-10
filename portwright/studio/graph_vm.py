"""Graph model behind the visual workflow editor (no UI code; unit-tested).

A workflow is a list of steps; the graph view shows each step as a node and each ``next`` / ``then`` / ``else`` /
switch case as a link. Saving writes every link explicitly, so the picture is exactly what runs.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import yaml

NODE_W, NODE_H = 200, 78
GAP_X, GAP_Y = 80, 50
TYPES = ("input", "tool", "agent", "llm", "load", "foreach", "condition", "switch", "set", "output")
OUT_PORTS = {"input": ("next",), "tool": ("next",), "agent": ("next",), "llm": ("next",), "load": ("next",),
             "foreach": ("next",), "set": ("next",), "condition": ("then", "else"), "output": ()}
MAX_CASES = 5
DO_KINDS = ("tool", "agent", "llm", "load")        # what a foreach node runs for each item


def case_values(text: str) -> list[str]:
    """The case values of a switch node: one per line (or comma separated), blanks and repeats dropped."""
    out: list[str] = []
    for part in re.split(r"[\n,]", text or ""):
        v = part.strip()
        if v and v not in out:
            out.append(v)
    return out[:MAX_CASES]


@dataclass
class Node:
    id: str
    type: str
    x: float = 0
    y: float = 0
    tool: str = ""
    agent: str = ""
    input: str = ""            # tool: YAML mapping of arguments; agent/llm: the prompt; foreach: per item (${item})
    when: str = ""             # condition: the test; switch: the value switched on
    value: str = ""            # set: the value; output: the workflow result
    inputs: str = ""           # input: YAML {name: default | {default, required, description}}
    model: str = ""            # llm (and foreach do=llm)
    system: str = ""           # llm: system prompt
    format: str = "text"       # llm: text | json
    load: str = ""             # load (and foreach do=load)
    items: str = ""            # foreach: the list, e.g. ${steps.q.output.rows}
    do: str = "tool"           # foreach: tool | agent | llm | load
    max_items: str = "50"      # foreach
    cases: str = ""            # switch: one value per line
    on_error: str = "fail"

    @property
    def ports(self) -> tuple[str, ...]:
        if self.type == "switch":
            return tuple(f"case:{v}" for v in case_values(self.cases)) + ("default",)
        return OUT_PORTS[self.type]

    @property
    def target_kind(self) -> str | None:
        """The kind of thing this node needs picked: tool | agent | model | load (None if nothing)."""
        kind = self.do if self.type == "foreach" else self.type
        return {"tool": "tool", "agent": "agent", "llm": "model", "load": "load"}.get(kind)

    def target(self) -> str:
        return {"tool": self.tool, "agent": self.agent, "model": self.model, "load": self.load}.get(self.target_kind or "", "")

    def set_target(self, name: str) -> None:
        if self.target_kind:
            setattr(self, self.target_kind, name)


@dataclass
class Graph:
    nodes: list[Node] = field(default_factory=list)
    links: dict[tuple[str, str], str] = field(default_factory=dict)      # (node id, port) -> target node id

    def node(self, node_id: str) -> Node | None:
        return next((n for n in self.nodes if n.id == node_id), None)

    @property
    def start(self) -> str | None:
        return self.nodes[0].id if self.nodes else None


def node_h(n: Node) -> int:
    """Switch nodes grow so each case port has room."""
    return max(NODE_H, 22 * len(n.ports) + 24) if n.type == "switch" else NODE_H


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return yaml.safe_dump(value, sort_keys=False).strip()


def _parse(text: str) -> Any:
    t = (text or "").strip()
    if not t:
        return None
    try:
        v = yaml.safe_load(t)
    except yaml.YAMLError:
        return text
    return v if isinstance(v, (dict, list)) else text


# ---------------------------------------------------------------- conversion
def from_steps(steps: list[dict]) -> Graph:
    g = Graph()
    laid_out = True
    for st in steps:
        n = Node(st["id"], st["type"], st.get("x") or 0, st.get("y") or 0, tool=st.get("tool") or "",
                 agent=st.get("agent") or "", input=_text(st.get("input")), when=st.get("when") or "",
                 value=_text(st.get("value")), inputs=_text(st.get("inputs")), model=st.get("model") or "",
                 system=st.get("system") or "", format=st.get("format") or "text", load=st.get("load") or "",
                 items=_text(st.get("items")), do=st.get("do") or "tool", max_items=str(st.get("max_items") or 50),
                 cases="\n".join(str(k) for k in (st.get("cases") or {})), on_error=st.get("on_error", "fail"))
        laid_out = laid_out and st.get("x") is not None and st.get("y") is not None
        g.nodes.append(n)
    ids = [n.id for n in g.nodes]
    for i, (st, n) in enumerate(zip(steps, g.nodes)):
        follow = ids[i + 1] if i + 1 < len(ids) else None
        if n.type == "condition":
            targets = {"then": st.get("then"), "else": st.get("else") or st.get("else_") or follow}
        elif n.type == "switch":
            by_value = {str(k): v for k, v in (st.get("cases") or {}).items()}
            targets = {f"case:{v}": by_value.get(v) for v in case_values(n.cases)}
            targets["default"] = st.get("default") or follow
        else:
            targets = {"next": st.get("next") or follow} if n.ports else {}
        for port, tgt in targets.items():
            if tgt and tgt != "end" and tgt in ids:
                g.links[(n.id, port)] = tgt
    if g.nodes and not laid_out:
        auto_layout(g)
    return g


def to_steps(g: Graph) -> list[dict]:
    """Steps in run order: the start node first, then the rest as reached; every link written explicitly."""
    order = reachable(g)
    order += [n.id for n in g.nodes if n.id not in order]
    out = []
    for nid in order:
        n = g.node(nid)
        s: dict[str, Any] = {"id": n.id, "type": n.type}
        if n.type == "tool":
            s["tool"] = n.tool
            if _parse(n.input) is not None:
                s["input"] = _parse(n.input)
        elif n.type == "agent":
            s["agent"] = n.agent
            if (n.input or "").strip():
                s["input"] = _parse(n.input)
        elif n.type == "llm":
            s["model"] = n.model
            if (n.input or "").strip():
                s["input"] = _parse(n.input)
            if (n.system or "").strip():
                s["system"] = n.system
            if n.format != "text":
                s["format"] = n.format
        elif n.type == "load":
            s["load"] = n.load
        elif n.type == "foreach":
            s["items"] = _parse(n.items) if _parse(n.items) is not None else ""
            s["do"] = n.do
            s[n.target_kind] = n.target()
            if (n.input or "").strip():
                s["input"] = _parse(n.input)
            if n.do == "llm":
                if (n.system or "").strip():
                    s["system"] = n.system
                if n.format != "text":
                    s["format"] = n.format
            if (n.max_items or "").strip().isdigit() and int(n.max_items) != 50:
                s["max_items"] = int(n.max_items)
        elif n.type == "input":
            if isinstance(_parse(n.inputs), dict):
                s["inputs"] = _parse(n.inputs)
        elif n.type == "output":
            if _parse(n.value) is not None:
                s["value"] = _parse(n.value)
        elif n.type == "set":
            s["value"] = _parse(n.value) if _parse(n.value) is not None else ""
        else:                                            # condition, switch
            s["when"] = n.when
        if n.type == "switch":
            s["cases"] = {v: g.links.get((n.id, f"case:{v}"), "end") for v in case_values(n.cases)}
            s["default"] = g.links.get((n.id, "default"), "end")
        else:
            for port in n.ports:
                s["then" if port == "then" else "else" if port == "else" else "next"] = g.links.get((n.id, port), "end")
        if n.on_error != "fail":
            s["on_error"] = n.on_error
        s["x"], s["y"] = round(n.x), round(n.y)
        out.append(s)
    return out


def reachable(g: Graph) -> list[str]:
    """Node ids in the order a run could reach them from the start (breadth first)."""
    if not g.nodes:
        return []
    seen, queue = [g.start], [g.start]
    while queue:
        cur = queue.pop(0)
        for port in g.node(cur).ports:
            tgt = g.links.get((cur, port))
            if tgt and tgt not in seen:
                seen.append(tgt)
                queue.append(tgt)
    return seen


# ------------------------------------------------------------------ editing
def new_id(g: Graph, kind: str) -> str:
    n = 1
    while g.node(f"{kind}_{n}"):
        n += 1
    return f"{kind}_{n}"


def add_node(g: Graph, kind: str, x: float = 40, y: float = 40) -> Node:
    n = Node(new_id(g, kind), kind, x, y, on_error="fail")
    if kind == "switch":
        n.cases = "yes\nno"
    g.nodes.append(n)
    return n


def delete_node(g: Graph, node_id: str) -> None:
    g.nodes = [n for n in g.nodes if n.id != node_id]
    g.links = {k: v for k, v in g.links.items() if k[0] != node_id and v != node_id}


def prune_links(g: Graph, n: Node) -> None:
    """After a switch node's cases change: drop links from ports that no longer exist."""
    g.links = {k: v for k, v in g.links.items() if k[0] != n.id or k[1] in n.ports}


def connect(g: Graph, src: str, port: str, dst: str) -> None:
    node = g.node(src)
    if node is None or g.node(dst) is None or port not in node.ports:
        raise ValueError("cannot connect")
    g.links[(src, port)] = dst


def disconnect(g: Graph, src: str, port: str) -> None:
    g.links.pop((src, port), None)


def set_start(g: Graph, node_id: str) -> None:
    n = g.node(node_id)
    if n is not None:
        g.nodes.remove(n)
        g.nodes.insert(0, n)


def rename_node(g: Graph, old: str, new: str) -> None:
    new = (new or "").strip()
    if not new or new == old:
        return
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9_\-]{0,63}$", new):
        raise ValueError("id: letters, digits, - and _; start with a letter")
    if g.node(new):
        raise ValueError(f"a step '{new}' already exists")
    g.node(old).id = new
    g.links = {((new if k[0] == old else k[0]), k[1]): (new if v == old else v) for k, v in g.links.items()}


def blank_choices(g: Graph) -> list[str]:
    """Which kinds of thing still need picking (tool / agent / model / load), e.g. after loading a template."""
    out: list[str] = []
    for n in g.nodes:
        k = n.target_kind
        if k and not n.target() and k not in out:
            out.append(k)
    return out


def prefill_choices(g: Graph, tools: list[str], agents: list[str], models: list[str] = (), loads: list[str] = ()) -> None:
    """Template nodes arrive with nothing picked; fill them in when the project has only one to choose."""
    pool = {"tool": tools, "agent": agents, "model": models, "load": loads}
    for n in g.nodes:
        k = n.target_kind
        if k and not n.target() and len(pool[k]) == 1:
            n.set_target(pool[k][0])


# ----------------------------------------------------------------- layout
def auto_layout(g: Graph) -> None:
    """Columns by distance from the start, rows within a column; unreachable nodes go in a last column."""
    depth: dict[str, int] = {}
    if g.nodes:
        depth[g.start] = 0
        queue = [g.start]
        while queue:
            cur = queue.pop(0)
            for port in g.node(cur).ports:
                tgt = g.links.get((cur, port))
                if tgt and tgt not in depth:
                    depth[tgt] = depth[cur] + 1
                    queue.append(tgt)
    last = (max(depth.values()) + 1) if depth else 0
    used: dict[int, float] = {}
    for n in g.nodes:
        d = depth.get(n.id, last)
        n.x = 40 + d * (NODE_W + GAP_X)
        n.y = used.get(d, 40)
        used[d] = n.y + node_h(n) + GAP_Y


def port_point(n: Node, port: str | None) -> tuple[float, float]:
    """Where a link attaches: port=None is the input on the left edge."""
    h = node_h(n)
    if port is None:
        return n.x, n.y + h / 2
    if n.type == "condition":
        return n.x + NODE_W, n.y + (h * 0.33 if port == "then" else h * 0.72)
    if n.type == "switch":
        i = n.ports.index(port) if port in n.ports else 0
        return n.x + NODE_W, n.y + h * (i + 1) / (len(n.ports) + 1)
    return n.x + NODE_W, n.y + h / 2


def link_curve(a: tuple[float, float], b: tuple[float, float]) -> tuple[tuple[float, float], tuple[float, float]]:
    """Cubic bezier control points between an output point and an input point."""
    dx = max(40.0, abs(b[0] - a[0]) / 2)
    return (a[0] + dx, a[1]), (b[0] - dx, b[1])


def canvas_size(g: Graph) -> tuple[int, int]:
    w = max([n.x + NODE_W for n in g.nodes] + [900]) + 120
    h = max([n.y + node_h(n) for n in g.nodes] + [420]) + 120
    return int(w), int(h)


# ---------------------------------------------------------------- checking
def problems(g: Graph, tools: list[str], agents: list[str], models: list[str] | None = None,
             loads: list[str] | None = None) -> list[str]:
    """Things to fix before saving. models / loads are checked only when given."""
    out = []
    if not g.nodes:
        return ["Add at least one step."]
    ok = set(reachable(g))
    pools = {"tool": tools, "agent": agents, "model": models, "load": loads}
    for n in g.nodes:
        k = n.target_kind
        if k and pools[k] is not None and n.target() not in pools[k]:
            out.append(f"{n.id}: choose {'an' if k in ('agent',) else 'a'} {k}")
        if n.type == "tool" and _parse(n.input) is not None and not isinstance(_parse(n.input), dict):
            out.append(f"{n.id}: the tool input must be a mapping of argument: value")
        if n.type == "condition" and not (n.when or "").strip():
            out.append(f"{n.id}: the condition needs an expression")
        if n.type == "condition" and (n.id, "then") not in g.links:
            out.append(f"{n.id}: 'then' goes nowhere, so the workflow ends there")
        if n.type == "switch" and not (n.when or "").strip():
            out.append(f"{n.id}: enter the value to switch on")
        if n.type == "switch" and not case_values(n.cases):
            out.append(f"{n.id}: add at least one case, or every run takes 'default'")
        if n.type == "foreach":
            if not (n.items or "").strip():
                out.append(f"{n.id}: enter the list to loop over")
            if (n.max_items or "").strip() and not (n.max_items.strip().isdigit() and int(n.max_items) >= 1):
                out.append(f"{n.id}: max items must be a whole number")
        if n.type == "input" and (n.inputs or "").strip() and not isinstance(_parse(n.inputs), dict):
            out.append(f"{n.id}: inputs must be a mapping of name: default")
        if n.id not in ok:
            out.append(f"{n.id}: never reached from the start ({g.start})")
    return out
