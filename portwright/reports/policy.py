"""What report data an AI model may see.

Levels, from least to most: ``none`` (refuse), ``schema`` (column names and types), ``profile`` (+ null rate,
distinct count, numeric/date min/max), ``sample`` (+ real rows, long values cut to 200 characters), ``full``
(+ real rows as they are). The effective level is the most restrictive of the project (``report_ai``), the
model, the source, the report, and any per-run override; an override can only lower it.
``ai_exclude_columns`` on the source are dropped before anything else, at every level.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from ..agents.spec import DATA_ACCESS_ORDER

SAMPLE_TRUNCATE = 200
MAX_DISTINCT = 1000


@dataclass
class Policy:
    level: str
    sample_rows: int
    exclude: list[str] = field(default_factory=list)
    reasons: dict[str, str] = field(default_factory=dict)      # setting -> its level, for messages/provenance

    @property
    def allowed(self) -> bool:
        return self.level != "none"

    def explain(self) -> str:
        limits = ", ".join(f"{k}={v}" for k, v in self.reasons.items())
        return f"data access '{self.level}' ({limits})"


def strictest(*levels: str | None) -> str:
    present = [lv for lv in levels if lv]
    return min(present, key=DATA_ACCESS_ORDER.index) if present else "full"


def resolve(config, *, model_spec=None, model_name: str = "", source: str | None = None,
            report=None, override: str | None = None, sample_rows: int | None = None) -> Policy:
    """The effective policy for one authoring run."""
    reasons: dict[str, str] = {"project": config.report_ai.data_access}
    if model_spec is not None:
        reasons[f"model:{model_name}"] = model_spec.data_access
    exclude: list[str] = []
    if source and source in config.sources:
        src = config.sources[source]
        reasons[f"source:{source}"] = src.ai_data_access
        exclude = list(src.ai_exclude_columns)
    if report is not None and report.ai_data_access:
        reasons[f"report:{report.name}"] = report.ai_data_access
    if override:
        if override not in DATA_ACCESS_ORDER:
            raise ValueError(f"unknown data access level '{override}'; choose from {', '.join(DATA_ACCESS_ORDER)}")
        reasons["requested"] = override
    level = strictest(*reasons.values())
    cap = config.report_ai.sample_rows
    rows = cap if sample_rows is None else max(0, min(int(sample_rows), 500))
    return Policy(level, rows, exclude, reasons)


def source_of(config, frm) -> str | None:
    """The source a report's data comes from (an endpoint's source counts)."""
    if frm is None:
        return None
    if frm.source:
        return frm.source
    ep = config.endpoint(frm.endpoint) if frm.endpoint else None
    return ep.source if ep else None


# --------------------------------------------------------------------------- shaping
def drop_columns(rows: list[dict], exclude: list[str]) -> list[dict]:
    if not exclude:
        return rows
    ex = {c.lower() for c in exclude}
    return [{k: v for k, v in r.items() if str(k).lower() not in ex} for r in rows]


def _kind(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, int):
        return "integer"
    if isinstance(v, (float, Decimal)):
        return "number"
    if isinstance(v, datetime):
        return "datetime"
    if isinstance(v, date):
        return "date"
    if isinstance(v, (dict, list)):
        return "json"
    if isinstance(v, str):
        for fmt, kind in (("%Y-%m-%d", "date"), ("%Y-%m-%dT%H:%M:%S", "datetime"), ("%Y-%m-%d %H:%M:%S", "datetime")):
            try:
                datetime.strptime(v[:19], fmt)
                return kind
            except ValueError:
                pass
        try:
            float(v)
            return "number-like text"
        except ValueError:
            return "text"
    return type(v).__name__


def _plain(v: Any, cut: int | None) -> Any:
    if isinstance(v, Decimal):
        v = float(v)
    if isinstance(v, (datetime, date)):
        v = v.isoformat()
    if isinstance(v, (dict, list)):
        v = json.dumps(v, default=str, ensure_ascii=False)
    if isinstance(v, bytes):
        v = f"<{len(v)} bytes>"
    if cut and isinstance(v, str) and len(v) > cut:
        v = v[:cut] + "…"
    return v


def columns(rows: list[dict]) -> list[str]:
    seen: dict[str, None] = {}
    for r in rows:
        for k in r:
            seen.setdefault(k, None)
    return list(seen)


def payload(rows: list[dict], policy: Policy) -> dict:
    """The data section of the prompt, shaped by the policy. Never includes excluded columns."""
    rows = drop_columns(rows, policy.exclude)
    cols = columns(rows)
    out: dict[str, Any] = {"data_access": policy.level, "row_count_profiled": len(rows), "columns": []}
    for c in cols:
        vals = [r.get(c) for r in rows]
        present = [v for v in vals if v is not None]
        kinds = sorted({_kind(v) for v in present}) or ["null"]
        col: dict[str, Any] = {"name": c, "type": kinds[0] if len(kinds) == 1 else " | ".join(kinds)}
        if policy.level in ("profile", "sample", "full"):
            col["null_rate"] = round(1 - len(present) / len(vals), 3) if vals else 0
            try:
                col["distinct"] = min(len({json.dumps(_plain(v, None), default=str) for v in present}), MAX_DISTINCT)
            except TypeError:
                pass
            numeric = [v for v in present if isinstance(v, (int, float, Decimal)) and not isinstance(v, bool)]
            if numeric and len(numeric) == len(present):
                col["min"], col["max"] = _plain(min(numeric), None), _plain(max(numeric), None)
            elif present and all(_kind(v) in ("date", "datetime") for v in present):
                col["min"], col["max"] = _plain(min(map(str, present)), None), _plain(max(map(str, present)), None)
        out["columns"].append(col)
    if policy.level in ("sample", "full"):
        cut = SAMPLE_TRUNCATE if policy.level == "sample" else None
        out["rows"] = [{k: _plain(v, cut) for k, v in r.items()} for r in rows[:policy.sample_rows]]
    return out


def fetch_limit(policy: Policy) -> int:
    """Rows to read for one design run: enough to profile, at least what may be sent."""
    return max(policy.sample_rows, 20)
