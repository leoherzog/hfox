"""Unit coverage for hfox.cli.output: format parsing, stderr helpers and each renderer."""

import csv
import datetime
import io
import json
import math

import pytest
import yaml

from hfox.cli import output
from hfox.cli.output import (
    OutputFormat,
    _as_rows,
    _column_union,
    _flatten,
    _needs_quoting,
    _stringify,
    _to_yaml,
    _yaml_scalar,
    info,
    render,
    warn,
)
from hfox.core.errors import ValidationError


def _render(data, fmt) -> str:
    buf = io.StringIO()
    render(data, fmt, stream=buf)
    return buf.getvalue()


# --- OutputFormat.parse ------------------------------------------------------


def test_parse_none_defaults_json():
    assert OutputFormat.parse(None) is OutputFormat.JSON
    assert OutputFormat.parse("") is OutputFormat.JSON


def test_parse_yml_alias_maps_to_yaml():
    assert OutputFormat.parse("yml") is OutputFormat.YAML
    assert OutputFormat.parse("  YML ") is OutputFormat.YAML


def test_parse_known_values_normalized():
    assert OutputFormat.parse("TABLE") is OutputFormat.TABLE
    assert OutputFormat.parse(" csv ") is OutputFormat.CSV
    assert OutputFormat.parse("yaml") is OutputFormat.YAML


def test_parse_unknown_raises_without_warning(capsys):
    with pytest.raises(ValidationError) as exc:
        OutputFormat.parse("toml")
    assert exc.value.message == "Unknown output format 'toml'; expected json, table, csv or yaml."
    assert exc.value.exit_code == 3
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""


# --- warn/info stderr helpers ------------------------------------------------


def test_warn_plain_when_no_color(capsys, monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    warn("something off")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "warning: something off\n"


def test_warn_color_branch(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    buf = io.StringIO()
    buf.isatty = lambda: True  # type: ignore[attr-defined]
    monkeypatch.setattr(output.sys, "stderr", buf)
    captured_console = []

    class FakeConsole:
        def print(self, msg):
            captured_console.append(msg)

    monkeypatch.setattr(output, "_err_console", FakeConsole())
    warn("colored msg")
    assert captured_console == ["[yellow]warning:[/yellow] colored msg"]


def test_info_goes_to_stderr(capsys):
    info("hello")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "hello\n"


@pytest.mark.parametrize(
    ("encoding", "expected"),
    [("utf-8", "🦊 "), ("utf-16", "🦊 "), ("cp1252", ""), ("ascii", "")],
)
def test_fox_only_when_stream_can_encode_it(encoding, expected):
    stream = io.TextIOWrapper(io.BytesIO(), encoding=encoding)
    assert output.fox(stream) == expected


def test_fox_assumes_utf8_without_encoding():
    assert output.fox(io.StringIO()) == "🦊 "


def test_fox_defaults_to_stderr(monkeypatch):
    monkeypatch.setattr("sys.stderr", io.TextIOWrapper(io.BytesIO(), encoding="cp1252"))
    assert output.fox() == ""


def test_fox_needs_every_stream():
    utf8 = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
    cp1252 = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
    assert output.fox(utf8, utf8) == "🦊 "
    assert output.fox(utf8, cp1252) == ""


def test_color_enabled_no_color_env(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    buf = io.StringIO()
    buf.isatty = lambda: True  # type: ignore[attr-defined]
    assert output._color_enabled(buf) is False


def test_color_enabled_non_tty(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    assert output._color_enabled(io.StringIO()) is False


# --- flattening helpers -----------------------------------------------------


def test_flatten_nested_dict_dot_notation():
    assert _flatten({"a": {"b": 1}}) == {"a.b": 1}


def test_flatten_empty_dict_kept_as_leaf():
    assert _flatten({"a": {}}) == {"a": {}}


def test_flatten_non_dict_uses_prefix_or_value():
    assert _flatten(7) == {"value": 7}
    assert _flatten("x", prefix="k") == {"k": "x"}


def test_as_rows_scalar_wrapped():
    assert _as_rows("scalar") == [{"value": "scalar"}]


def test_as_rows_list_of_scalars():
    assert _as_rows([1, "x"]) == [{"value": 1}, {"value": "x"}]


def test_as_rows_list_of_dicts_flattened():
    assert _as_rows([{"a": {"b": 2}}]) == [{"a.b": 2}]


def test_column_union_preserves_order_and_dedupes():
    rows = [{"a": 1, "b": 2}, {"b": 3, "c": 4}]
    assert _column_union(rows) == ["a", "b", "c"]


# --- _stringify -------------------------------------------------------------


def test_stringify_none_empty():
    assert _stringify(None) == ""


def test_stringify_dict_and_list_json():
    assert _stringify({"a": 1}) == '{"a": 1}'
    assert _stringify([1, 2]) == "[1, 2]"


def test_stringify_scalar_str():
    assert _stringify(5) == "5"


def test_stringify_bool_lowercase():
    assert _stringify(True) == "true"
    assert _stringify(False) == "false"


# --- render dispatch + JSON -------------------------------------------------


def test_render_json_to_stream():
    assert json.loads(_render({"id": 1}, OutputFormat.JSON)) == {"id": 1}


def test_render_json_escapes_non_ascii_for_non_utf_stream():
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="ascii", errors="backslashreplace")
    render({"q": "café 😀"}, OutputFormat.JSON, stream=stream)
    output.render_ndjson_line({"q": "café 😀"}, stream=stream)
    stream.flush()
    lines = raw.getvalue().decode("ascii")
    assert json.loads(lines[: lines.rindex("{")]) == {"q": "café 😀"}
    assert json.loads(lines.splitlines()[-1]) == {"q": "café 😀"}


def test_render_json_keeps_raw_utf8():
    assert "café" in _render({"q": "café"}, OutputFormat.JSON)


def test_render_default_stream_is_stdout(capsys):
    render([1, 2], OutputFormat.JSON)
    assert json.loads(capsys.readouterr().out) == [1, 2]


# --- table ------------------------------------------------------------------


def test_render_table_empty_list_says_no_results(capsys):
    assert _render([], OutputFormat.TABLE) == ""
    assert "(no results)" in capsys.readouterr().err


def test_render_table_empty_dict_says_no_results(capsys):
    assert _render({}, OutputFormat.TABLE) == ""
    assert "(no results)" in capsys.readouterr().err


def test_render_table_with_rows_has_columns_and_values():
    text = _render([{"id": 1, "sub": "down"}], OutputFormat.TABLE)
    assert "id" in text
    assert "sub" in text
    assert "down" in text


def test_render_table_single_dict_is_vertical():
    text = _render({"id": 9, "status": {"name": "New"}, "unread": True}, OutputFormat.TABLE)
    lines = text.splitlines()
    assert "field" in lines[1] and "value" in lines[1]
    assert any("id" in line and "9" in line for line in lines)
    assert any("status.name" in line and "New" in line for line in lines)
    assert any("unread" in line and "true" in line for line in lines)


def test_render_table_single_dict_keeps_long_values():
    choices = [{"id": n, "value": f"Choice {n}"} for n in range(1, 8)]
    text = _render({"choices": choices}, OutputFormat.TABLE)
    assert "…" not in text
    assert '"id": 7' in "".join(line.strip("│ ") for line in text.splitlines())


def test_render_table_list_cells_fold_not_truncate():
    text = _render([{"note": "x" * 99 + "END"}], OutputFormat.TABLE)
    assert "…" not in text
    assert "x" * 99 + "END" in "".join(line.strip("│ ") for line in text.splitlines())


def test_render_table_bools_lowercase():
    text = _render([{"success": True, "default": False}], OutputFormat.TABLE)
    assert "true" in text and "false" in text
    assert "True" not in text and "False" not in text


def test_render_table_prints_markup_and_emoji_codes_verbatim():
    text = _render([{"s": "fix [/b] tag [bold]x :smile:"}], OutputFormat.TABLE)
    assert "fix [/b] tag [bold]x :smile:" in text


# --- csv --------------------------------------------------------------------


def test_render_csv_empty_list_produces_nothing():
    assert _render([], OutputFormat.CSV) == ""


def test_render_csv_writes_header_and_rows():
    lines = _render([{"id": 1, "n": "a"}, {"id": 2, "n": "b"}], OutputFormat.CSV).splitlines()
    assert lines == ["id,n", "1,a", "2,b"]


def test_render_csv_missing_column_empty_cell():
    lines = _render([{"a": 1}, {"b": 2}], OutputFormat.CSV).splitlines()
    assert lines == ["a,b", "1,", ",2"]


def test_render_csv_bools_lowercase():
    text = _render([{"success": True, "status": {"default": False}}], OutputFormat.CSV)
    assert list(csv.DictReader(io.StringIO(text))) == [
        {"success": "true", "status.default": "false"}
    ]


# --- yaml layout ------------------------------------------------------------


def test_render_yaml_dict_scalar():
    assert _render({"id": 1, "name": "Bob"}, OutputFormat.YAML) == "id: 1\nname: Bob\n"


def test_to_yaml_empty_containers_top_level():
    assert _to_yaml({}, 0) == "{}"
    assert _to_yaml([], 0) == "[]"


def test_to_yaml_nested_empty_containers_are_literal():
    out = _to_yaml({"merged_tickets": [], "phones": [], "dependant_fields": {}}, 0)
    assert out == "merged_tickets: []\nphones: []\ndependant_fields: {}"
    assert _to_yaml([[], {}], 0) == "- []\n- {}"


def test_to_yaml_nested_dict():
    assert _to_yaml({"a": {"b": 1}}, 0) == "a:\n  b: 1"


def test_to_yaml_list_of_scalars():
    assert _to_yaml([1, 2], 0) == "- 1\n- 2"


def test_to_yaml_list_of_dicts():
    assert _to_yaml([{"a": 1}], 0) == "-\n  a: 1"


def test_to_yaml_scalar_top_level():
    assert _to_yaml("hi", 0) == "hi"


def test_to_yaml_quotes_keys():
    assert _to_yaml({"12": "x", "<<": "z"}, 0) == '"12": x\n"<<": z'


def test_render_yaml_list_top_level():
    assert _render([{"id": 1}], OutputFormat.YAML) == "-\n  id: 1\n"


def test_render_yaml_empty_dict_trailing_newline():
    assert _render({}, OutputFormat.YAML) == "{}\n"


def test_render_yaml_quotes_reserved_value():
    assert _render({"flag": "yes"}, OutputFormat.YAML) == 'flag: "yes"\n'


# --- yaml scalars -----------------------------------------------------------


def test_needs_quoting_empty_string():
    assert _needs_quoting("") is True


def test_needs_quoting_edge_whitespace_and_special():
    assert _needs_quoting(" pad") is True
    assert _needs_quoting("pad ") is True
    assert _needs_quoting("a:b") is True
    assert _needs_quoting("a#b") is True
    assert _needs_quoting('a"b') is True


def test_needs_quoting_reserved_words():
    for word in ("true", "FALSE", "Yes", "no", "ON", "off", "null", "~", "y", "N"):
        assert _needs_quoting(word) is True


def test_needs_quoting_leading_indicator():
    assert _needs_quoting("@handle") is True
    assert _needs_quoting("[bracket") is True


def test_needs_quoting_number_like():
    for text in ("0123", "42", "+1_000", "3.14", "-2.5", "0x1F", "0b101", ".inf", ".nan"):
        assert _needs_quoting(text) is True


def test_needs_quoting_non_printable():
    for text in ("a\tb", "a\rb", "a\x7fb", "a\x85b", "a\u2028b", "a\ufeffb"):
        assert _needs_quoting(text) is True


def test_needs_quoting_plain_word_ok():
    assert _needs_quoting("hello") is False
    assert _needs_quoting("Printer down") is False
    assert _needs_quoting("_") is False
    assert _needs_quoting("José") is False


def test_yaml_scalar_none_and_bool():
    assert _yaml_scalar(None) == "null"
    assert _yaml_scalar(True) == "true"
    assert _yaml_scalar(False) == "false"


def test_yaml_scalar_numbers():
    assert _yaml_scalar(7) == "7"
    assert _yaml_scalar(1.5) == "1.5"
    assert _yaml_scalar(1e16) == "1.0e+16"
    assert _yaml_scalar(float("inf")) == ".inf"
    assert _yaml_scalar(float("-inf")) == "-.inf"
    assert _yaml_scalar(float("nan")) == ".nan"


def test_yaml_scalar_plain_string():
    assert _yaml_scalar("plain") == "plain"


def test_yaml_scalar_quoted_string():
    assert _yaml_scalar("true") == '"true"'
    assert _yaml_scalar("a:b") == '"a:b"'
    assert _yaml_scalar('say "hi"\n') == '"say \\"hi\\"\\n"'
    assert _yaml_scalar("a\x7fb\u2028c") == '"a\\x7fb\\u2028c"'


# --- yaml round-trip through a real parser ------------------------------------

ROUND_TRIP_STRINGS = [
    "",
    " ",
    "plain",
    "Printer down",
    "#DC00000003",
    "2026-06-01",
    "2026-06-01 10:00:00",
    "2026-06-01T10:00:00Z",
    "12:30",
    "1:20",
    "0x1F",
    "0o17",
    "0b101",
    "017",
    "+12",
    "-3",
    "1_000",
    "3.14",
    "1e3",
    ".5",
    ".inf",
    "-.inf",
    ".nan",
    "NaN",
    "Infinity",
    "~",
    "null",
    "Null",
    "true",
    "False",
    "yes",
    "No",
    "on",
    "OFF",
    "y",
    "n",
    "=",
    "<<",
    "-",
    "- item",
    "? key",
    "---",
    "...",
    "key: value",
    "trailing:",
    "a # not a comment",
    "!tag",
    "&anchor",
    "*alias",
    "|",
    ">",
    "%directive",
    "@at",
    "`tick",
    "[1, 2]",
    "{a: 1}",
    "O'Brien",
    'say "hi"',
    "back\\slash",
    "line\nbreak",
    "tab\there",
    "lone\rcr",
    "del\x7f",
    "c0\x01\x1f",
    "c1\x80\x9f",
    "nel\x85",
    "nbsp\xa0",
    "ls\u2028ps\u2029",
    "bom\ufeff",
    "private\ue000",
    "José Müller",
    "日本語",
    "emoji 😀",
    "tag\U000e0001",
    "trailing space ",
    " leading space",
]


@pytest.mark.parametrize("text", ROUND_TRIP_STRINGS)
def test_yaml_round_trip_strings(text):
    doc = {"value": text, "list": [text], text or "empty": 1}
    assert yaml.safe_load(_render(doc, OutputFormat.YAML)) == doc


def test_yaml_round_trip_documented_ticket_shape():
    ticket = {
        "id": 3,
        "display_id": "#DC00000003",
        "subject": "Printer: jammed # again",
        "due_date": "2026-06-01",
        "created_at": "2026-05-30 09:12:01",
        "merged_tickets": [],
        "unread": False,
        "time_spent": 1.5,
        "assigned_to": None,
        "status": {"id": 1, "name": "New", "default": True, "color": "0x1F"},
        "custom_fields": [
            {"id": 12, "value": "017", "value_id": None, "dependant_fields": {}},
            {"id": 13, "value": ["a", "b"], "choices": [{"id": 4, "value": "On"}]},
        ],
        "user": {"name": "José", "phones": [], "email": "jose@example.com"},
        "updates": [{"message": {"text": "Hi\r\n\tthere\x85"}, "ratio": 1e16}],
        "nested": [[1, [2]], [], {}],
    }
    loaded = yaml.safe_load(_render(ticket, OutputFormat.YAML))
    assert loaded == ticket
    assert isinstance(loaded["due_date"], str)


def test_yaml_round_trip_special_floats():
    loaded = yaml.safe_load(_render([float("inf"), float("-inf"), 1e-7], OutputFormat.YAML))
    assert loaded[:2] == [float("inf"), float("-inf")]
    assert loaded[2] == 1e-7
    assert math.isnan(yaml.safe_load(_render(float("nan"), OutputFormat.YAML)))


def test_yaml_round_trip_top_level_scalars():
    for value in ("2026-06-01", "yes", "", None, True, 0, "plain"):
        assert yaml.safe_load(_render(value, OutputFormat.YAML)) == value


def test_yaml_dates_stay_strings():
    loaded = yaml.safe_load(_render({"due": "2026-06-01"}, OutputFormat.YAML))
    assert not isinstance(loaded["due"], datetime.date)
