"""Ready-made workflow templates: start from a working shape, then pick your own tools and agents.

A template is a list of steps like any workflow. Tool and agent steps arrive with the tool / agent left blank
(``""``): the editor shows them as "choose a tool" and fills one in when the project has only one to pick from.
"""
from __future__ import annotations

import copy
from typing import Any

import yaml


def _t(id: str, title: str, description: str, steps: list[dict], sample: dict | None = None) -> dict:
    return {"id": id, "title": title, "description": description, "steps": steps, "sample_input": sample or {}}


TEMPLATES: list[dict] = [
    _t("hello", "Hello, world (no tools needed)",
       "Smallest working workflow: reads an input, builds a greeting, returns it. Run it to see inputs and outputs work.",
       [{"id": "start", "type": "input", "inputs": {"name": "world"}, "next": "greet"},
        {"id": "greet", "type": "set", "value": "Hello ${input.name}!", "next": "result"},
        {"id": "result", "type": "output", "value": {"greeting": "${steps.greet.output}"}}],
       {"name": "Ada"}),
    _t("ask-an-agent", "Ask an agent",
       "Passes a question to one agent and returns its answer.",
       [{"id": "start", "type": "input", "inputs": {"question": {"description": "What should the agent answer?"}},
         "next": "ask"},
        {"id": "ask", "type": "agent", "agent": "", "input": "${input.question}", "next": "answer"},
        {"id": "answer", "type": "output", "value": "${steps.ask.output}"}],
       {"question": "Summarise what this project does."}),
    _t("query-and-summarise", "Query data, then summarise",
       "Runs a read-only query (SQL source or endpoint tool), then has an agent answer a question from the rows.",
       [{"id": "start", "type": "input",
         "inputs": {"question": {"description": "What do you want to know?"}, "limit": 20}, "next": "query"},
        {"id": "query", "type": "tool", "tool": "", "input": {"query": "select * from your_table limit ${input.limit}"},
         "next": "summarise"},
        {"id": "summarise", "type": "agent", "agent": "",
         "input": "Answer this question: ${input.question}\n\nUse only this data:\n${steps.query.output}",
         "next": "result"},
        {"id": "result", "type": "output", "value": {"answer": "${steps.summarise.output}", "rows": "${steps.query.output}"}}]),
    _t("check-and-alert", "Check, then alert only if needed",
       "Fetches data and branches: if there is something to report an agent drafts the alert, otherwise it stays quiet.",
       [{"id": "start", "type": "input", "inputs": {"scope": "all"}, "next": "fetch"},
        {"id": "fetch", "type": "tool", "tool": "", "input": {"dept": "${input.scope}"}, "next": "found"},
        {"id": "found", "type": "condition", "when": "${steps.fetch.output.count} != 0", "then": "draft", "else": "quiet"},
        {"id": "draft", "type": "agent", "agent": "",
         "input": "Write a short alert for the team about:\n${steps.fetch.output}", "next": "alert"},
        {"id": "quiet", "type": "set", "value": "Nothing to report.", "next": "all_clear"},
        {"id": "alert", "type": "output", "value": {"alert": "${steps.draft.output}"}},
        {"id": "all_clear", "type": "output", "value": {"alert": None, "message": "${steps.quiet.output}"}}]),
    _t("draft-and-review", "Draft, then review",
       "One agent writes a draft, a second agent reviews and improves it. Returns both.",
       [{"id": "start", "type": "input", "inputs": {"topic": {"description": "What should be written?"}},
         "next": "draft"},
        {"id": "draft", "type": "agent", "agent": "", "input": "Write a first draft about: ${input.topic}",
         "next": "review"},
        {"id": "review", "type": "agent", "agent": "",
         "input": "Review this draft, fix mistakes and tighten it:\n${steps.draft.output}", "next": "result"},
        {"id": "result", "type": "output", "value": {"draft": "${steps.draft.output}", "final": "${steps.review.output}"}}],
       {"topic": "Why we validate data before loading it"}),
    _t("with-fallback", "Try one tool, fall back to another",
       "Calls a primary tool; if it fails the workflow carries on to a backup tool instead of stopping.",
       [{"id": "start", "type": "input", "inputs": {"id": {"description": "The record to look up"}}, "next": "primary"},
        {"id": "primary", "type": "tool", "tool": "", "input": {"id": "${input.id}"}, "on_error": "continue",
         "next": "worked"},
        {"id": "worked", "type": "condition", "when": "${steps.primary.ok}", "then": "from_primary", "else": "backup"},
        {"id": "backup", "type": "tool", "tool": "", "input": {"id": "${input.id}"}, "next": "from_backup"},
        {"id": "from_primary", "type": "output", "value": {"source": "primary", "result": "${steps.primary.output}"}},
        {"id": "from_backup", "type": "output", "value": {"source": "backup", "result": "${steps.backup.output}"}}]),
    _t("compare-two-sources", "Combine two sources",
       "Reads from two tools and has an agent compare them.",
       [{"id": "start", "type": "input", "inputs": {"focus": {"description": "What to compare, e.g. headcount"}},
         "next": "first"},
        {"id": "first", "type": "tool", "tool": "", "input": {"query": "select * from first_table"}, "next": "second"},
        {"id": "second", "type": "tool", "tool": "", "input": {"query": "select * from second_table"}, "next": "compare"},
        {"id": "compare", "type": "agent", "agent": "",
         "input": "Compare these two data sets with focus on ${input.focus}.\n\nA:\n${steps.first.output}\n\nB:\n${steps.second.output}",
         "next": "result"},
        {"id": "result", "type": "output", "value": "${steps.compare.output}"}]),
    _t("classify-and-route", "Classify, then route",
       "An LLM labels the message, a switch sends it down the matching branch, each branch answers differently.",
       [{"id": "start", "type": "input", "inputs": {"message": {"description": "The text to handle"}}, "next": "classify"},
        {"id": "classify", "type": "llm", "model": "",
         "system": "Classify the message as exactly one word, lowercase, no punctuation: billing, bug or other.",
         "input": "${input.message}", "next": "route"},
        {"id": "route", "type": "switch", "when": "${steps.classify.output}",
         "cases": {"billing": "billing_reply", "bug": "bug_reply"}, "default": "human"},
        {"id": "billing_reply", "type": "llm", "model": "",
         "input": "Write a short, polite reply to this billing question:\n${input.message}", "next": "out_billing"},
        {"id": "bug_reply", "type": "llm", "model": "",
         "input": "Write a short reply acknowledging this bug report and asking for steps to reproduce:\n${input.message}",
         "next": "out_bug"},
        {"id": "human", "type": "set", "value": "Needs a person to look at it.", "next": "out_human"},
        {"id": "out_billing", "type": "output", "value": {"category": "billing", "reply": "${steps.billing_reply.output}"}},
        {"id": "out_bug", "type": "output", "value": {"category": "bug", "reply": "${steps.bug_reply.output}"}},
        {"id": "out_human", "type": "output", "value": {"category": "other", "reply": "${steps.human.output}"}}],
       {"message": "I was charged twice this month."}),
    _t("summarise-each-row", "Summarise every row",
       "Queries rows, then runs an LLM prompt for each row and returns the list of summaries.",
       [{"id": "start", "type": "input", "inputs": {"limit": 5}, "next": "query"},
        {"id": "query", "type": "tool", "tool": "", "input": {"query": "select * from your_table limit ${input.limit}"},
         "next": "each"},
        {"id": "each", "type": "foreach", "items": "${steps.query.output.rows}", "do": "llm", "model": "",
         "input": "Summarise this record in one sentence:\n${item}", "on_error": "continue", "next": "result"},
        {"id": "result", "type": "output", "value": {"summaries": "${steps.each.output}"}}]),
    _t("load-then-report", "Run a load, then report",
       "Runs one of your data loads, then has an LLM write a short status report from its result. "
       "Needs the scope load:run on the workflow.",
       [{"id": "start", "type": "input", "inputs": {"audience": "the data team"}, "next": "run_it"},
        {"id": "run_it", "type": "load", "load": "", "next": "report"},
        {"id": "report", "type": "llm", "model": "",
         "input": "Write a short status report for ${input.audience} from this load result:\n${steps.run_it.output}",
         "next": "result"},
        {"id": "result", "type": "output", "value": "${steps.report.output}"}]),
]


def list_templates() -> list[dict]:
    out = []
    for t in TEMPLATES:
        steps = t["steps"]
        out.append({"id": t["id"], "title": t["title"], "description": t["description"],
                    "tools": sum(s["type"] == "tool" for s in steps), "agents": sum(s["type"] == "agent" for s in steps),
                    "steps": len(steps)})
    return out


def get_template(template_id: str) -> dict[str, Any]:
    for t in TEMPLATES:
        if t["id"] == template_id:
            return copy.deepcopy(t)
    raise KeyError(template_id)


def template_yaml(template_id: str) -> str:
    """The template as workflow YAML, with CHOOSE_TOOL / CHOOSE_AGENT where you pick your own."""
    t = get_template(template_id)
    for s in t["steps"]:
        kind = s.get("do") if s["type"] == "foreach" else s["type"]
        field = {"tool": "tool", "agent": "agent", "llm": "model", "load": "load"}.get(kind)
        if field and not s.get(field):
            s[field] = "CHOOSE_" + field.upper()
    return yaml.safe_dump({"description": t["description"], "steps": t["steps"]}, sort_keys=False, allow_unicode=True)
