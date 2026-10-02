"""Table and CSV cells drop control and format characters; JSON, NDJSON and YAML keep the data."""

import csv
import io
import json

import pytest
import yaml

from hfox.cli.output import OutputFormat, render, render_ndjson_line, sanitize_cell

ESC = "\x1b"
C1_CSI = "\x9b"
RLO = "‮"
ZWSP = "​"
BOM = "﻿"
ZWNJ = "‌"
ZWJ = "‍"

# Every character output._JSON_ESCAPED matches.
JSON_ESCAPED = [
    *map(chr, range(0x7F, 0xA0)),
    "؜",
    "‎",
    "‏",
    *map(chr, range(0x202A, 0x202F)),
    *map(chr, range(0x2066, 0x206A)),
    " ",
    " ",
    *map(chr, range(0xE0000, 0xE0080)),
]

HOSTILE = {
    "subject": f"{ESC}[31mred{ESC}[0m {C1_CSI}2J{RLO}txt.exe{ZWSP}{BOM}",
    f"key{RLO}\x85": "=1+1",
    "all": "".join(JSON_ESCAPED),
    "nested": [{"name": f"a{RLO}b\U000e0041"}],
    "count": -5,
}


def _render(data, fmt) -> str:
    buf = io.StringIO()
    render(data, fmt, stream=buf)
    return buf.getvalue()


def _csv_rows(data) -> list[list[str]]:
    return list(csv.reader(io.StringIO(_render(data, OutputFormat.CSV), newline="")))


# --- sanitize_cell ------------------------------------------------------------


@pytest.mark.parametrize(
    "char",
    [
        ESC,
        "\x00",
        "\x07",
        "\x08",
        "\r",
        "\x7f",
        "\x85",
        C1_CSI,
        "؜",
        "‎",
        "‪",
        RLO,
        "⁦",
        "⁩",
        ZWSP,
        "⁠",
        BOM,
        "\xad",
        "\U000e0001",
        "\U000e007f",
    ],
)
def test_sanitize_cell_drops_control_and_format_characters(char):
    assert sanitize_cell(f"a{char}b") == "ab"


@pytest.mark.parametrize("char", ["\n", "\t", ZWNJ, ZWJ])
def test_sanitize_cell_keeps_newline_tab_and_joiners(char):
    assert sanitize_cell(f"a{char}b") == f"a{char}b"


def test_sanitize_cell_keeps_printable_text():
    text = "José 日本語 😀 [bold] a,b\xa0c"
    assert sanitize_cell(text) == text


def test_sanitize_cell_strips_escape_sequence_introducer_only():
    assert sanitize_cell(f"{ESC}[31mred{ESC}[0m") == "[31mred[0m"


# --- table ----------------------------------------------------------------------


@pytest.mark.parametrize("char", [ESC, C1_CSI, RLO, ZWSP, BOM])
def test_table_cells_and_headers_are_sanitized(char):
    text = _render([{f"he{char}ad": f"ce{char}ll"}], OutputFormat.TABLE)
    assert char not in text
    assert "head" in text and "cell" in text


@pytest.mark.parametrize("char", [ESC, C1_CSI, RLO, ZWSP, BOM])
def test_table_single_dict_fields_and_values_are_sanitized(char):
    text = _render({f"he{char}ad": f"ce{char}ll"}, OutputFormat.TABLE)
    assert char not in text
    assert "head" in text and "cell" in text


def test_table_nested_cell_is_sanitized():
    text = _render([{"tags": [f"a{RLO}b"]}], OutputFormat.TABLE)
    assert RLO not in text
    assert '["ab"]' in text


def test_table_keeps_newline_as_a_line_break():
    lines = _render([{"note": "first\nsecond"}], OutputFormat.TABLE).splitlines()
    first = next(i for i, line in enumerate(lines) if "first" in line)
    assert "second" in lines[first + 1]


def test_table_keeps_tab_as_whitespace():
    text = _render([{"note": "left\tright"}], OutputFormat.TABLE)
    assert "leftright" not in text
    assert "left" in text and "right" in text


@pytest.mark.parametrize("char", [ZWNJ, ZWJ])
def test_table_keeps_joiners(char):
    assert f"a{char}b" in _render([{"name": f"a{char}b"}], OutputFormat.TABLE)


# --- csv ------------------------------------------------------------------------


@pytest.mark.parametrize("char", [ESC, C1_CSI, RLO, ZWSP, BOM])
def test_csv_cells_and_headers_are_sanitized(char):
    text = _render([{f"he{char}ad": f"ce{char}ll"}], OutputFormat.CSV)
    assert char not in text
    assert _csv_rows([{f"he{char}ad": f"ce{char}ll"}]) == [["head"], ["cell"]]


def test_csv_keeps_newline_and_tab():
    assert _csv_rows([{"note": "a\nb\tc"}]) == [["note"], ["a\nb\tc"]]


def _through_text_layer(data, fmt, **layer) -> bytes:
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, **layer)
    render(data, fmt, stream=stream)
    stream.flush()
    return raw.getvalue()


# newline="\r\n" translates each \n on write, as Windows text mode does.
WINDOWS = {"encoding": "utf-8", "newline": "\r\n"}


def test_csv_rows_end_in_one_crlf_under_a_translating_text_layer():
    data = [{"id": 1, "note": "a\nb"}, {"id": 2, "note": "c\r\nd"}]
    expected = b'id,note\r\n1,"a\nb"\r\n2,"c\nd"\r\n'
    assert _through_text_layer(data, OutputFormat.CSV, **WINDOWS) == expected
    assert _through_text_layer(data, OutputFormat.CSV, encoding="utf-8", newline="") == expected
    assert _render(data, OutputFormat.CSV).encode() == expected


def test_csv_follows_text_already_written_to_the_stream():
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, **WINDOWS)
    stream.write("before\n")
    render([{"a": 1}], OutputFormat.CSV, stream=stream)
    stream.write("after\n")
    stream.flush()
    assert raw.getvalue() == b"before\r\na\r\n1\r\nafter\r\n"


def test_csv_bytes_use_the_stream_encoding_and_error_handler():
    layer = {"encoding": "cp1252", "errors": "backslashreplace", "newline": "\r\n"}
    out = _through_text_layer([{"n": "café 🦊"}], OutputFormat.CSV, **layer)
    assert out == b"n\r\ncaf\xe9 \\U0001f98a\r\n"


@pytest.mark.parametrize("fmt", [OutputFormat.JSON, OutputFormat.YAML, OutputFormat.TABLE])
def test_other_formats_keep_the_text_layer_line_endings(fmt):
    out = _through_text_layer([{"id": 1}], fmt, **WINDOWS)
    assert b"\r\n" in out
    assert b"\r\r\n" not in out
    assert b"\n" not in out.replace(b"\r\n", b"")


def test_ndjson_keeps_the_text_layer_line_ending():
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, **WINDOWS)
    render_ndjson_line({"id": 1}, stream=stream)
    assert raw.getvalue() == b'{"id":1}\r\n'


@pytest.mark.parametrize("char", [ZWNJ, ZWJ])
def test_csv_keeps_joiners(char):
    assert _csv_rows([{f"h{char}h": f"a{char}b"}]) == [[f"h{char}h"], [f"a{char}b"]]


def test_csv_headers_that_sanitize_alike_keep_their_own_cells():
    assert _csv_rows([{f"a{ZWSP}": 1, "a": 2}]) == [["a", "a"], ["1", "2"]]


@pytest.mark.parametrize(
    "value",
    ["=1+1", "+cmd", "-cmd", "@SUM(A1)", "\tx", "=HYPERLINK(\"http://x\")", "-5+cmd", "+", "-"],
)
def test_csv_prefixes_formula_cells(value):
    assert _csv_rows([{"v": value}]) == [["v"], ["'" + value]]


def test_csv_prefixes_cell_led_by_carriage_return_trigger():
    # CR is dropped by sanitizing, so the trigger behind it leads the cell.
    assert _csv_rows([{"v": "\r=1+1"}]) == [["v"], ["'=1+1"]]


def test_csv_prefixes_trigger_exposed_by_sanitizing():
    assert _csv_rows([{"v": f"{ZWSP}{ESC}=1+1"}]) == [["v"], ["'=1+1"]]


def test_csv_prefixes_formula_headers():
    assert _csv_rows([{"=cmd": "x", "@h": {"-n": "y"}}]) == [["'=cmd", "'@h.-n"], ["x", "y"]]


@pytest.mark.parametrize("value", ["-5", "+15551234567", "3.14", "-0.5", "+3.0", "42"])
def test_csv_leaves_signed_decimal_strings(value):
    assert _csv_rows([{"v": value}]) == [["v"], [value]]


@pytest.mark.parametrize("value", ["-5.", "-.5", "+1e3", "-5\n", "- 5", "-5,6"])
def test_csv_prefixes_near_numbers(value):
    assert _csv_rows([{"v": value}]) == [["v"], ["'" + value]]


@pytest.mark.parametrize(
    ("value", "cell"),
    [(-5, "-5"), (-2.5, "-2.5"), (float("-inf"), "-inf"), (True, "true"), (None, "")],
)
def test_csv_never_prefixes_non_strings(value, cell):
    assert _csv_rows([{"k": "x", "v": value}]) == [["k", "v"], ["x", cell]]


def test_csv_never_prefixes_nested_values():
    assert _csv_rows([{"v": ["=1+1"], "e": {}}]) == [["v", "e"], ['["=1+1"]', "{}"]]
    assert _csv_rows([["=1+1"]]) == [["value"], ['["=1+1"]']]


def test_csv_prefixes_bare_string_rows():
    assert _csv_rows(["=1+1", "ok"]) == [["value"], ["'=1+1"], ["ok"]]


def test_csv_leaves_inner_triggers():
    assert _csv_rows([{"v": "a=b", "w": "x@example.com"}]) == [["v", "w"], ["a=b", "x@example.com"]]


# --- json, ndjson, yaml keep the data -------------------------------------------


def test_json_round_trips_and_escapes_listed_characters():
    text = _render(HOSTILE, OutputFormat.JSON)
    assert json.loads(text) == HOSTILE
    assert not [char for char in JSON_ESCAPED if char in text]
    assert ESC not in text
    assert "\\u202e" in text
    assert "\\udb40\\udc41" in text


def test_ndjson_round_trips_and_escapes_listed_characters():
    buf = io.StringIO()
    render_ndjson_line(HOSTILE, stream=buf)
    text = buf.getvalue()
    assert text.endswith("\n") and text.count("\n") == 1
    assert json.loads(text) == HOSTILE
    assert not [char for char in JSON_ESCAPED if char in text]
    assert ESC not in text


def test_json_escapes_for_a_non_utf_stream_too():
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="ascii")
    render(HOSTILE, OutputFormat.JSON, stream=stream)
    stream.flush()
    assert json.loads(raw.getvalue().decode("ascii")) == HOSTILE


def test_json_keeps_other_text_raw():
    data = {"q": f"café 😀 a{ZWJ}b{ZWNJ}c{ZWSP}"}
    text = _render(data, OutputFormat.JSON)
    assert data["q"] in text


def test_json_escaped_backslash_before_listed_character_round_trips():
    data = {"q": f"\\{RLO}\\u202e"}
    assert json.loads(_render(data, OutputFormat.JSON)) == data


def test_yaml_round_trips_hostile_data():
    assert yaml.safe_load(_render(HOSTILE, OutputFormat.YAML)) == HOSTILE
