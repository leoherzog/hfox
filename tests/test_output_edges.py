"""Edge cases of hfox.cli.output: stream encodings, escape boundaries, nested cells, stderr."""

import csv
import io
import json
import re
import sys
import types

import pytest
import yaml

from hfox.cli import output
from hfox.cli.output import OutputFormat, render, render_ndjson_line, sanitize_cell

RLO = "\u202e"
NON_ASCII = {"q": "café 😀"}
_SGR = re.compile(r"\x1b\[[0-9;]*m")


class _Terminal(io.StringIO):
    def isatty(self) -> bool:
        return True


def _render(data, fmt) -> str:
    buf = io.StringIO()
    render(data, fmt, stream=buf)
    return buf.getvalue()


def _csv_rows(data) -> list[list[str]]:
    return list(csv.reader(io.StringIO(_render(data, OutputFormat.CSV), newline="")))


def _json_document(data, stream) -> None:
    render(data, OutputFormat.JSON, stream=stream)


def _ndjson_line(data, stream) -> None:
    render_ndjson_line(data, stream=stream)


JSON_WRITERS = pytest.mark.parametrize("write", [_json_document, _ndjson_line])
NON_FINITE = pytest.mark.parametrize(
    "number", [float("nan"), float("inf"), float("-inf")], ids=["nan", "inf", "-inf"]
)


def _strict_loads(text: str):
    """Parse JSON, failing on the bare NaN, Infinity and -Infinity that json.loads accepts."""

    def refuse(name: str):
        raise AssertionError(f"{name} is not JSON")

    return json.loads(text, parse_constant=refuse)


def _encoded(write, data, encoding) -> bytes:
    raw = io.BytesIO()
    # main.app() sets this handler on stdout, so a raw unencodable character becomes a
    # backslash escape that JSON does not have.
    stream = io.TextIOWrapper(raw, encoding=encoding, errors="backslashreplace")
    write(data, stream)
    stream.flush()
    return raw.getvalue()


# --- stderr helpers -------------------------------------------------------------


def test_warn_on_a_color_terminal_writes_to_stderr_only(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    stdout, stderr = io.StringIO(), _Terminal()
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)
    output.warn("check the page limit")
    assert stdout.getvalue() == ""
    assert "warning:" in stderr.getvalue()
    assert "check the page limit" in stderr.getvalue()


# (message, text printed): ESC, CR, BEL, backspace and a bidi override are dropped.
STDERR_TEXT = pytest.mark.parametrize(
    ("message", "printed"),
    [
        ("remove base_url from /tmp/[work]/config.toml to clear it.", None),
        ("closing [/b] tag", None),
        ("rate limited :warning: retry later", None),
        (" ".join(["word"] * 400), None),
        ("a\tb\nc‌d‍e", None),
        (f"a\x1b[2Kb\rc\x07d\x08e{RLO}f", "a[2Kbcdef"),
    ],
    ids=["bracketed-path", "closing-tag", "emoji-code", "long-line", "kept", "dropped"],
)


@STDERR_TEXT
@pytest.mark.parametrize("terminal", [_Terminal, io.StringIO], ids=["color", "plain"])
def test_warn_prints_text_verbatim_apart_from_control_characters(
    monkeypatch, terminal, message, printed
):
    monkeypatch.delenv("NO_COLOR", raising=False)
    stdout, stderr = io.StringIO(), terminal()
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)
    output.warn(message)
    assert stdout.getvalue() == ""
    assert _SGR.sub("", stderr.getvalue()) == f"warning: {printed or message}\n"


@STDERR_TEXT
def test_info_prints_text_verbatim_apart_from_control_characters(monkeypatch, message, printed):
    stdout, stderr = io.StringIO(), _Terminal()
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)
    output.info(message)
    assert stdout.getvalue() == ""
    assert stderr.getvalue() == f"{printed or message}\n"


def test_empty_no_color_counts_as_unset(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "")
    assert output._color_enabled(_Terminal()) is True


def test_fox_is_empty_for_a_codec_python_does_not_know():
    assert output.fox(types.SimpleNamespace(encoding="no-such-codec")) == ""


# --- json and ndjson --------------------------------------------------------------


def test_json_is_a_two_space_indented_document():
    text = _render({"id": 1, "tags": ["a"]}, OutputFormat.JSON)
    assert text == '{\n  "id": 1,\n  "tags": [\n    "a"\n  ]\n}\n'


@JSON_WRITERS
@pytest.mark.parametrize("encoding", ["UTF-8", "utf-16"])
def test_json_keeps_non_ascii_raw_for_any_utf_stream(write, encoding):
    assert NON_ASCII["q"] in _encoded(write, NON_ASCII, encoding).decode(encoding)


@JSON_WRITERS
@pytest.mark.parametrize("encoding", ["cp1252", "latin-1"])
def test_json_is_pure_ascii_for_a_non_utf_stream(write, encoding):
    out = _encoded(write, NON_ASCII, encoding)
    assert out.isascii()
    assert json.loads(out) == NON_ASCII


@JSON_WRITERS
@pytest.mark.parametrize("char", ["~", "\xa0", "\u061b", "\u2027", "\u202f", "\U000e0080"])
def test_json_keeps_neighbors_of_the_escaped_ranges_raw(write, char):
    buf = io.StringIO()
    write({"q": f"a{char}b"}, buf)
    assert f"a{char}b" in buf.getvalue()


@JSON_WRITERS
@NON_FINITE
def test_json_writes_null_for_a_non_finite_number_at_any_depth(write, number):
    data = {"n": number, "rows": [number, {"deep": [[number]], "pair": (number, 1.5)}], "ok": -2.5}
    buf = io.StringIO()
    write(data, buf)
    assert _strict_loads(buf.getvalue()) == {
        "n": None,
        "rows": [None, {"deep": [[None]], "pair": [None, 1.5]}],
        "ok": -2.5,
    }


@JSON_WRITERS
@NON_FINITE
def test_json_writes_null_for_a_bare_non_finite_number(write, number):
    buf = io.StringIO()
    write(number, buf)
    assert buf.getvalue() == "null\n"


@JSON_WRITERS
def test_json_leaves_the_data_it_was_given_unchanged(write):
    row = {"n": float("inf")}
    data = {"n": float("-inf"), "rows": [row]}
    write(data, io.StringIO())
    assert data == {"n": float("-inf"), "rows": [{"n": float("inf")}]}
    assert data["rows"][0] is row


@JSON_WRITERS
def test_json_keeps_non_finite_names_inside_text(write):
    data = {"NaN": "Infinity", "v": ["-Infinity", "NaN"], "big": 1e308}
    buf = io.StringIO()
    write(data, buf)
    assert _strict_loads(buf.getvalue()) == data


# --- table and csv ----------------------------------------------------------------


def test_sanitize_cell_keeps_private_use_characters():
    assert sanitize_cell("a\ue000b\U000f0000c") == "a\ue000b\U000f0000c"


def test_table_leaves_a_cell_blank_when_a_row_lacks_the_column():
    sparse = _render([{"a": 1}, {"b": 2}], OutputFormat.TABLE)
    assert sparse == _render([{"a": 1, "b": ""}, {"a": "", "b": 2}], OutputFormat.TABLE)


def test_table_prints_markup_and_emoji_codes_in_headers_verbatim():
    header = "Type [internal] [bold]x :smile:"
    assert header in _render([{header: 1}], OutputFormat.TABLE)


def test_deeply_nested_keys_keep_every_level_in_the_column_name():
    row = {"user": {"name": "A", "address": {"city": "X", "geo": {"lat": 1}}}}
    assert _csv_rows([row]) == [
        ["user.name", "user.address.city", "user.address.geo.lat"],
        ["A", "X", "1"],
    ]
    assert "user.address.geo.lat" in _render(row, OutputFormat.TABLE)


def test_csv_nested_cell_is_sanitized():
    assert _csv_rows([{"tags": [f"a{RLO}b"], "who": [{"name": f"c{RLO}d"}]}]) == [
        ["tags", "who"],
        ['["ab"]', '[{"name": "cd"}]'],
    ]


@NON_FINITE
def test_csv_nested_cell_writes_null_for_a_non_finite_number(number):
    row = {"low": [number, {"x": number, "pair": (number, 1.5)}], "ok": [-2.5]}
    header, cells = _csv_rows([row])
    assert header == ["low", "ok"]
    assert cells == ['[null, {"x": null, "pair": [null, 1.5]}]', "[-2.5]"]
    assert [_strict_loads(cell) for cell in cells] == [
        [None, {"x": None, "pair": [None, 1.5]}],
        [-2.5],
    ]


@NON_FINITE
def test_table_nested_cell_writes_null_for_a_non_finite_number(number):
    assert '[null, {"x": null}]' in _render([{"low": [number, {"x": number}]}], OutputFormat.TABLE)
    assert '[{"x": null}]' in _render({"low": [{"x": number}]}, OutputFormat.TABLE)


def test_nested_cell_leaves_the_data_it_was_given_unchanged():
    inner = {"x": float("inf")}
    row = {"low": [float("-inf"), inner]}
    _render([row], OutputFormat.CSV)
    assert row == {"low": [float("-inf"), {"x": float("inf")}]}
    assert row["low"][1] is inner


def test_nested_cell_keeps_key_order_separators_non_ascii_and_number_text():
    cell = ["\u00e9 \ud83d\ude00", {"b": 1, "a": "NaN"}, 1.0, 1e20, -0.0, {}, [], None, True]
    text = '["\u00e9 \ud83d\ude00", {"b": 1, "a": "NaN"}, 1.0, 1e+20, -0.0, {}, [], null, true]'
    assert _csv_rows([{"cell": cell}]) == [["cell"], [text]]


@pytest.mark.parametrize("value", ["-\u0663", "+\uff15", "-1.\u0665"])
def test_csv_prefixes_signed_numbers_in_non_ascii_digits(value):
    assert _csv_rows([{"v": value}]) == [["v"], ["'" + value]]


# --- yaml -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    ["C:\\temp\\new", "\\", '\\"quoted\\"', "back\\slash # x", "trailing: \\", "\\x41 \\u0041"],
)
def test_yaml_round_trips_backslashes_in_quoted_strings(text):
    doc = {"value": text, "list": [text], text: 1}
    assert yaml.safe_load(_render(doc, OutputFormat.YAML)) == doc


@pytest.mark.parametrize(
    ("value", "text"),
    [(1.5e20, "1.5e+20"), (2.5e-07, "2.5e-07"), (-1.25e-10, "-1.25e-10")],
)
def test_yaml_float_with_fraction_and_exponent_is_written_as_is(value, text):
    rendered = _render({"ratio": value}, OutputFormat.YAML)
    assert rendered == f"ratio: {text}\n"
    assert yaml.safe_load(rendered) == {"ratio": value}
