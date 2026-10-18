"""Row mapping: source row -> target row (rename, transform, cast, validate).

Each mapped row either succeeds or produces a list of human-readable reasons,
so bad rows can be written to a rejects file instead of failing silently.
"""
from __future__ import annotations

import difflib
import re
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from ..config import ColumnMap, LoadSpec

_TRUE = {"true", "t", "yes", "y", "1"}
_FALSE = {"false", "f", "no", "n", "0"}
_ISO_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d")
_ISO_TS_FORMATS = ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f",
                   "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d")


class RowError(Exception):
    pass


def _blank(v: Any) -> bool:
    return v is None or (isinstance(v, str) and v.strip() == "")


# ----------------------------------------------------------------- transforms
def apply_transforms(value: Any, steps: list[Any], col: str) -> Any:
    for step in steps:
        if value is None:
            break
        if isinstance(step, str):
            name, arg = step, None
        elif isinstance(step, dict) and len(step) == 1:
            name, arg = next(iter(step.items()))
        else:
            raise RowError(f"{col}: invalid transform {step!r}")
        if name == "trim":
            value = str(value).strip()
        elif name == "upper":
            value = str(value).upper()
        elif name == "lower":
            value = str(value).lower()
        elif name == "null_if_empty":
            value = None if _blank(value) else value
        elif name == "replace":
            if not (isinstance(arg, list) and len(arg) == 2):
                raise RowError(f"{col}: replace needs [old, new]")
            value = str(value).replace(str(arg[0]), str(arg[1]))
        elif name == "lookup":
            if not isinstance(arg, dict):
                raise RowError(f"{col}: lookup needs a mapping")
            key = str(value)
            if key not in arg:
                if "_default" in arg:
                    value = arg["_default"]
                else:
                    raise RowError(f"{col}: value '{key}' not in lookup")
            else:
                value = arg[key]
        else:
            raise RowError(f"{col}: unknown transform '{name}'")
    return value


# ---------------------------------------------------------------------- casts
def cast(value: Any, col: ColumnMap) -> Any:
    if value is None:
        return None
    t = col.type
    try:
        if t == "string":
            return str(value)
        if t == "integer":
            if isinstance(value, bool):
                return int(value)
            if isinstance(value, int):
                return value
            s = str(value).strip().replace(",", "")
            d = Decimal(s)
            if d != d.to_integral_value():
                raise ValueError
            return int(d)
        if t == "number":
            if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
                return value
            return Decimal(str(value).strip().replace(",", ""))
        if t == "boolean":
            if isinstance(value, bool):
                return value
            s = str(value).strip().lower()
            if s in _TRUE:
                return True
            if s in _FALSE:
                return False
            raise ValueError
        if t in ("date", "timestamp"):
            if isinstance(value, datetime):
                return value.date() if t == "date" else value
            if isinstance(value, date):
                return value if t == "date" else datetime(value.year, value.month, value.day)
            s = str(value).strip()
            fmts = (col.format,) if col.format else (_ISO_DATE_FORMATS if t == "date" else _ISO_TS_FORMATS)
            for f in fmts:
                try:
                    dt = datetime.strptime(s, f)
                    return dt.date() if t == "date" else dt
                except ValueError:
                    continue
            raise ValueError
    except (ValueError, InvalidOperation, TypeError):
        expect = f"{t}" + (f" ({col.format})" if col.format else "")
        raise RowError(f"{col.target}: '{value}' is not a valid {expect}") from None
    raise RowError(f"{col.target}: unsupported type {t}")


# --------------------------------------------------------------------- mapper
class RowMapper:
    def __init__(self, spec: LoadSpec, run_id: str):
        self.spec = spec
        self.run_id = run_id
        self.now = datetime.now(timezone.utc).replace(tzinfo=None)

    def _constant(self, value: Any) -> Any:
        if isinstance(value, str):
            if value == "${now}":
                return self.now
            if value == "${today}":
                return self.now.date()
            if value == "${run_id}":
                return self.run_id
        return value

    def map_row(self, row: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        errors: list[str] = []
        for col in self.spec.columns:
            try:
                if col.value is not None:
                    v = self._constant(col.value)
                else:
                    v = row.get(col.source)
                    v = apply_transforms(v, col.transform, col.target)
                    if _blank(v):
                        v = self._constant(col.default)
                if _blank(v):
                    if col.required:
                        raise RowError(f"{col.target}: required but '{col.source}' is empty")
                    v = None
                # source data is never expanded as ${...}; only configured constants are
                v = cast(v, col) if col.value is None else v
                if col.max_length and isinstance(v, str) and len(v) > col.max_length:
                    raise RowError(f"{col.target}: length {len(v)} exceeds {col.max_length}")
                out[col.target] = v
            except RowError as exc:
                errors.append(str(exc))
        if errors:
            raise RowError("; ".join(errors))
        return out


# ------------------------------------------------------------------ auto-map
def normalise(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def auto_map(source_columns: list[str], target_columns: dict[str, str]) -> tuple[list[dict], list[str]]:
    """Suggest a mapping. ``target_columns`` = {name: portwright type}.

    Exact matches after normalising (case, spaces, _ - .) are mapped; close matches are
    mapped with ``review: true``; anything else is reported as unmatched.
    """
    norm_targets = {normalise(t): t for t in target_columns}
    used: set[str] = set()
    cols, unmatched = [], []
    for src in source_columns:
        n = normalise(src)
        tgt = norm_targets.get(n)
        review = False
        if tgt is None or tgt in used:
            close = difflib.get_close_matches(n, [k for k, v in norm_targets.items() if v not in used],
                                              n=1, cutoff=0.75)
            tgt = norm_targets[close[0]] if close else None
            review = tgt is not None
        if tgt is None:
            unmatched.append(src)
            continue
        used.add(tgt)
        entry = {"source": src, "target": tgt, "type": target_columns[tgt]}
        if review:
            entry["review"] = True
        cols.append(entry)
    return cols, unmatched
