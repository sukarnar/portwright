"""Custom Jinja2 filters for portwright report templates."""
from __future__ import annotations

import locale
import math
from datetime import date, datetime
from typing import Any


def _parse_dt(value: Any) -> datetime | date | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S",
                    "%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y"):
            try:
                return datetime.strptime(value, fmt)
            except ValueError:
                continue
    return None


def strftime_filter(value: Any, fmt: str = "%Y-%m-%d") -> str:
    """Format a date/datetime/ISO string with strftime."""
    dt = _parse_dt(value)
    if dt is None:
        return str(value) if value is not None else ""
    return dt.strftime(fmt)


def date_filter(value: Any) -> str:
    """Short date: YYYY-MM-DD."""
    return strftime_filter(value, "%Y-%m-%d")


def datetime_filter(value: Any) -> str:
    """Full datetime: YYYY-MM-DD HH:MM."""
    return strftime_filter(value, "%Y-%m-%d %H:%M")


def currency_filter(value: Any, symbol: str = "$", decimals: int = 2) -> str:
    """Format a number as currency, e.g. $1,234.56."""
    try:
        n = float(value)
    except (TypeError, ValueError):
        return str(value) if value is not None else ""
    sign = "-" if n < 0 else ""
    formatted = f"{abs(n):,.{decimals}f}"
    return f"{sign}{symbol}{formatted}"


def number_filter(value: Any, decimals: int = 0, thousands: bool = True) -> str:
    """Format a number with optional thousands separator."""
    try:
        n = float(value)
    except (TypeError, ValueError):
        return str(value) if value is not None else ""
    if thousands:
        return f"{n:,.{decimals}f}"
    return f"{n:.{decimals}f}"


def percent_filter(value: Any, decimals: int = 1) -> str:
    """Format 0.456 → '45.6%'."""
    try:
        n = float(value) * 100
    except (TypeError, ValueError):
        return str(value) if value is not None else ""
    return f"{n:.{decimals}f}%"


def nl2br_filter(value: str) -> str:
    """Replace newlines with <br> tags."""
    if not value:
        return ""
    return str(value).replace("\n", "<br>")


def default_filter(value: Any, default: Any = "") -> Any:
    """Return *default* when value is None or empty string."""
    if value is None or value == "":
        return default
    return value


def yesno_filter(value: Any, yes: str = "Yes", no: str = "No", none: str = "") -> str:
    """Boolean to human label."""
    if value is None:
        return none
    return yes if value else no


def truncate_filter(value: Any, length: int = 50, ellipsis: str = "…") -> str:
    s = str(value) if value is not None else ""
    if len(s) <= length:
        return s
    return s[:length].rstrip() + ellipsis


def sum_filter(rows: list[dict], column: str) -> float:
    """Sum a column across a list of row dicts: {{ rows | sum_col('amount') }}."""
    total = 0.0
    for row in rows:
        try:
            total += float(row.get(column) or 0)
        except (TypeError, ValueError):
            pass
    return total


def avg_filter(rows: list[dict], column: str) -> float | str:
    """Average of a column across rows."""
    vals = []
    for row in rows:
        try:
            vals.append(float(row.get(column) or 0))
        except (TypeError, ValueError):
            pass
    if not vals:
        return ""
    return sum(vals) / len(vals)


def groupby_filter(rows: list[dict], column: str) -> list[tuple]:
    """Group rows by a column value: {% for key, group in rows | groupby_col('dept') %}."""
    groups: dict[Any, list[dict]] = {}
    order: list = []
    for row in rows:
        key = row.get(column)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(row)
    return [(key, groups[key]) for key in order]


ALL_FILTERS = {
    "strftime": strftime_filter,
    "date": date_filter,
    "datetime": datetime_filter,
    "currency": currency_filter,
    "number": number_filter,
    "percent": percent_filter,
    "nl2br": nl2br_filter,
    "default_if_none": default_filter,
    "yesno": yesno_filter,
    "truncate_col": truncate_filter,   # avoid shadowing Jinja2 built-in truncate
    "sum_col": sum_filter,
    "avg_col": avg_filter,
    "groupby_col": groupby_filter,
}
