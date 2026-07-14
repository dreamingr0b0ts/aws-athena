"""Render result sets as a text table, CSV, or JSON."""

from __future__ import annotations

import csv
import io
import json
from typing import Sequence

Row = Sequence[object]


def _stringify(value: object) -> str:
    return "" if value is None else str(value)


def format_table(columns: Sequence[str], rows: Sequence[Row], max_col_width: int = 60) -> str:
    """Render an aligned, monospaced table with a header separator."""
    cols = list(columns)
    if not cols:
        return "(no columns)"

    def clip(s: str) -> str:
        return s if len(s) <= max_col_width else s[: max_col_width - 1] + "\u2026"

    str_rows = [[clip(_stringify(c)) for c in row] for row in rows]
    widths = [len(c) for c in cols]
    for row in str_rows:
        for i, cell in enumerate(row):
            if i < len(widths):
                widths[i] = max(widths[i], len(cell))

    def fmt_row(cells: Sequence[str]) -> str:
        return " | ".join(c.ljust(widths[i]) for i, c in enumerate(cells))

    sep = "-+-".join("-" * w for w in widths)
    lines = [fmt_row([clip(c) for c in cols]), sep]
    lines.extend(fmt_row(row) for row in str_rows)
    footer = f"\n({len(str_rows)} row{'s' if len(str_rows) != 1 else ''})"
    return "\n".join(lines) + footer


def format_csv(columns: Sequence[str], rows: Sequence[Row]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    if columns:
        writer.writerow(columns)
    for row in rows:
        writer.writerow([_stringify(c) for c in row])
    return buf.getvalue().rstrip("\r\n")


def format_json(columns: Sequence[str], rows: Sequence[Row], indent: int | None = 2) -> str:
    records = [dict(zip(columns, [None if c is None else c for c in row])) for row in rows]
    return json.dumps(records, indent=indent, default=str)


def render(columns: Sequence[str], rows: Sequence[Row], fmt: str = "table") -> str:
    fmt = fmt.lower()
    if fmt == "table":
        return format_table(columns, rows)
    if fmt == "csv":
        return format_csv(columns, rows)
    if fmt == "json":
        return format_json(columns, rows)
    raise ValueError(f"Unknown output format: {fmt!r} (use table|csv|json)")
