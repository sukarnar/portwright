"""Join a report's datasets in memory: hang each dataset's rows on its parent's rows (master-detail, lookups).

Rows are copied first, so the caller's lists are never changed. Children are shared dicts, which is what
makes several levels work in any order: attaching serials to lines also shows them under order.lines.
Keys are compared as text after number normalisation (7 == 7.0 == Decimal('7') == "7"); a null key never
matches. A key column is found by exact name, else ignoring case (Oracle and SQL Server differ).
"""
from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
from typing import Any


class DatasetError(ValueError):
    pass


def norm_key(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return str(v).lower()
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() else repr(v)
    if isinstance(v, Decimal):
        return str(int(v)) if v == v.to_integral_value() else format(v.normalize(), "f")
    if isinstance(v, (datetime, date, time)):
        return v.isoformat()
    if isinstance(v, bytes):
        return v.hex()
    s = str(v).strip()
    return s if s else None


def _column(rows: list[dict], name: str, who: str) -> str:
    """The real column name for *name* in *rows* (exact, else case-insensitive)."""
    cols: dict[str, None] = {}
    for r in rows[:200]:
        for k in r:
            cols.setdefault(k, None)
    if name in cols:
        return name
    lower = {str(c).lower(): c for c in cols}
    if name.lower() in lower:
        return lower[name.lower()]
    raise DatasetError(f"{who} has no column '{name}' (columns: {', '.join(map(str, cols)) or 'none'})")


def _key(row: dict, cols: list[str]) -> tuple | None:
    vals = tuple(norm_key(row.get(c)) for c in cols)
    return None if any(v is None for v in vals) else vals


def join(spec, master: list[dict], datasets: dict[str, list[dict]]) -> tuple[list[dict], dict[str, list[dict]],
                                                                           list[str]]:
    """Copies of *master* and *datasets* with every ``attach`` applied, plus warnings."""
    master = [dict(r) for r in master]
    data = {n: [dict(r) for r in rows] for n, rows in datasets.items()}
    warnings: list[str] = []
    for name, ds in spec.datasets.items():
        a = ds.attach
        if a is None or name not in data:
            continue
        parents = master if a.to == "master" else data.get(a.to, [])
        children = data[name]
        field = a.as_ or name
        if not parents:
            continue
        key, pkey = a.keys()
        pcols = [_column(parents, c, "the master rows" if a.to == "master" else f"dataset '{a.to}'") for c in pkey]
        if children:
            ccols = [_column(children, c, f"dataset '{name}'") for c in key]
        index: dict[tuple, list[dict]] = {}
        for c in children:
            k = _key(c, ccols)
            if k is not None:
                index.setdefault(k, []).append(c)
        if any(field in p for p in parents[:200]):
            warnings.append(f"dataset '{name}' replaces the column '{field}' of "
                            f"{'the master rows' if a.to == 'master' else repr(a.to)} (set attach.as to keep it)")
        several = 0
        for p in parents:
            k = _key(p, pcols)
            found = index.get(k, []) if k is not None else []
            if a.one:
                several += len(found) > 1
                p[field] = found[0] if found else None
            else:
                p[field] = list(found)
        if several:
            warnings.append(f"dataset '{name}' is attached with one: true, but {several} parent row(s) matched "
                            "more than one row; the first was used")
    return master, data, warnings
