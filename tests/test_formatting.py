import json

import pytest

from athena_toolkit.formatting import format_csv, format_json, format_table, render


COLUMNS = ["id", "name"]
ROWS = [["1", "alice"], ["2", None]]


def test_table_contains_headers_and_rows():
    out = format_table(COLUMNS, ROWS)
    assert "id" in out and "name" in out
    assert "alice" in out
    assert "(2 rows)" in out


def test_table_none_renders_empty():
    out = format_table(COLUMNS, [["2", None]])
    assert "(1 row)" in out


def test_csv_roundtrip():
    out = format_csv(COLUMNS, ROWS)
    lines = out.splitlines()
    assert lines[0] == "id,name"
    assert lines[1] == "1,alice"
    assert lines[2] == "2,"


def test_json_structure():
    out = format_json(COLUMNS, ROWS)
    data = json.loads(out)
    assert data == [
        {"id": "1", "name": "alice"},
        {"id": "2", "name": None},
    ]


def test_render_dispatch():
    assert "id" in render(COLUMNS, ROWS, "table")
    assert "id,name" in render(COLUMNS, ROWS, "csv")
    assert json.loads(render(COLUMNS, ROWS, "json"))


def test_render_unknown_format_raises():
    with pytest.raises(ValueError):
        render(COLUMNS, ROWS, "xml")


def test_table_clips_wide_values():
    out = format_table(["c"], [["x" * 100]], )
    # default max width 60, so a clipped cell + ellipsis should appear
    assert "\u2026" in out
