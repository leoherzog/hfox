"""Direct unit coverage for hfox.cli.output renderers and helpers.

Targets json/table/csv/yaml rendering of dicts and lists, the stderr helpers
(warn/info), empty-list handling, nested/compact values, and the YAML scalar
quoting rules. Renderers are called directly; stdout/stderr captured via capsys.
"""

import io
import json

from hfox.cli import output
from hfox.cli.output import (
    OutputFormat,
    _as_rows,
    _column_union,
    _flatten,
    _needs_quoting,
    _stringify,
    _to_yaml,
    _truncate,
    _yaml_scalar,
    info,
    render,
    warn,
)

# --- OutputFormat.parse (lines 34, 37, 40-43) -------------------------------


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


def test_parse_unknown_falls_back_to_json_with_warning(capsys):
    result = OutputFormat.parse("toml")
    assert result is OutputFormat.JSON
    err = capsys.readouterr().err
    assert "Unknown format 'toml'" in err
    assert "falling back to json" in err


# --- warn/info stderr helpers (lines 56-59, 62-63) --------------------------


def test_warn_plain_when_no_color(capsys, monkeypatch):
    # stderr under capsys is not a tty -> plain branch (line 59).
    monkeypatch.delenv("NO_COLOR", raising=False)
    warn("something off")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "warning: something off\n"


def test_warn_color_branch(monkeypatch):
    # Force the color-enabled branch (lines 56-57) via a tty-like stderr.
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


def test_color_enabled_no_color_env(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    buf = io.StringIO()
    buf.isatty = lambda: True  # type: ignore[attr-defined]
    assert output._color_enabled(buf) is False


def test_color_enabled_non_tty(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    assert output._color_enabled(io.StringIO()) is False


# --- _flatten (lines 73, 79) ------------------------------------------------


def test_flatten_nested_dict_dot_notation():
    assert _flatten({"a": {"b": 1}}) == {"a.b": 1}


def test_flatten_empty_dict_kept_as_leaf():
    # value is an empty dict -> NOT recursed, kept as leaf (line 77).
    assert _flatten({"a": {}}) == {"a": {}}


def test_flatten_non_dict_uses_prefix_or_value():
    # scalar with no prefix -> "value" key (line 79).
    assert _flatten(7) == {"value": 7}
    assert _flatten("x", prefix="k") == {"k": "x"}


# --- _as_rows (lines 88, 94) ------------------------------------------------


def test_as_rows_scalar_wrapped():
    # non-list, non-dict -> wrapped in single row (line 88 + 94).
    assert _as_rows("scalar") == [{"value": "scalar"}]


def test_as_rows_list_of_scalars():
    assert _as_rows([1, "x"]) == [{"value": 1}, {"value": "x"}]


def test_as_rows_list_of_dicts_flattened():
    assert _as_rows([{"a": {"b": 2}}]) == [{"a.b": 2}]


# --- _column_union ----------------------------------------------------------


def test_column_union_preserves_order_and_dedupes():
    rows = [{"a": 1, "b": 2}, {"b": 3, "c": 4}]
    assert _column_union(rows) == ["a", "b", "c"]


# --- _stringify (lines 111, 113) --------------------------------------------


def test_stringify_none_empty():
    assert _stringify(None) == ""


def test_stringify_dict_and_list_json():
    assert _stringify({"a": 1}) == '{"a": 1}'
    assert _stringify([1, 2]) == "[1, 2]"


def test_stringify_scalar_str():
    assert _stringify(5) == "5"


# --- _truncate (line 120) ---------------------------------------------------


def test_truncate_short_unchanged():
    assert _truncate("abc", width=10) == "abc"


def test_truncate_long_adds_ellipsis():
    out = _truncate("abcdefghij", width=5)
    assert out == "abcd…"
    assert len(out) == 5


# --- render dispatch + JSON -------------------------------------------------


def test_render_json_to_stream():
    buf = io.StringIO()
    render({"id": 1}, OutputFormat.JSON, stream=buf)
    assert json.loads(buf.getvalue()) == {"id": 1}


def test_render_default_stream_is_stdout(capsys):
    render([1, 2], OutputFormat.JSON)
    out = capsys.readouterr().out
    assert json.loads(out) == [1, 2]


# --- table rendering (lines 149-150, 166 adjacency) -------------------------


def test_render_table_empty_list_says_no_results(capsys):
    buf = io.StringIO()
    render([], OutputFormat.TABLE, stream=buf)
    # info("(no results)") goes to stderr; table stream stays empty (149-150).
    assert buf.getvalue() == ""
    assert "(no results)" in capsys.readouterr().err


def test_render_table_with_rows_has_columns_and_values():
    buf = io.StringIO()
    render([{"id": 1, "sub": "down"}], OutputFormat.TABLE, stream=buf)
    text = buf.getvalue()
    assert "id" in text
    assert "sub" in text
    assert "down" in text


def test_render_table_single_dict():
    buf = io.StringIO()
    render({"id": 9}, OutputFormat.TABLE, stream=buf)
    text = buf.getvalue()
    assert "id" in text
    assert "9" in text


# --- csv rendering (line 166) -----------------------------------------------


def test_render_csv_empty_list_produces_nothing():
    buf = io.StringIO()
    render([], OutputFormat.CSV, stream=buf)
    assert buf.getvalue() == ""


def test_render_csv_writes_header_and_rows():
    buf = io.StringIO()
    render([{"id": 1, "n": "a"}, {"id": 2, "n": "b"}], OutputFormat.CSV, stream=buf)
    lines = buf.getvalue().splitlines()
    assert lines[0] == "id,n"
    assert lines[1] == "1,a"
    assert lines[2] == "2,b"


def test_render_csv_missing_column_empty_cell():
    buf = io.StringIO()
    render([{"a": 1}, {"b": 2}], OutputFormat.CSV, stream=buf)
    lines = buf.getvalue().splitlines()
    assert lines[0] == "a,b"
    assert lines[1] == "1,"
    assert lines[2] == ",2"


# --- yaml rendering (lines 188, 192-193, 197-208) ---------------------------


def test_render_yaml_dict_scalar():
    buf = io.StringIO()
    render({"id": 1, "name": "Bob"}, OutputFormat.YAML, stream=buf)
    assert buf.getvalue() == "id: 1\nname: Bob\n"


def test_to_yaml_empty_dict():
    assert _to_yaml({}, 0) == "{}"


def test_to_yaml_empty_list():
    assert _to_yaml([], 0) == "[]"


def test_to_yaml_nested_dict():
    # line 192-193: nested non-empty dict recursion.
    out = _to_yaml({"a": {"b": 1}}, 0)
    assert out == "a:\n  b: 1"


def test_to_yaml_list_of_scalars():
    # lines 197-199, 205-206.
    out = _to_yaml([1, 2], 0)
    assert out == "- 1\n- 2"


def test_to_yaml_list_of_dicts():
    # lines 202-204: nested dict items get "-" then recursion.
    out = _to_yaml([{"a": 1}], 0)
    assert out == "-\n  a: 1"


def test_to_yaml_scalar_top_level():
    # line 208.
    assert _to_yaml("hi", 0) == "hi"


def test_render_yaml_list_top_level():
    buf = io.StringIO()
    render([{"id": 1}], OutputFormat.YAML, stream=buf)
    assert buf.getvalue() == "-\n  id: 1\n"


def test_render_yaml_empty_dict_trailing_newline():
    buf = io.StringIO()
    render({}, OutputFormat.YAML, stream=buf)
    assert buf.getvalue() == "{}\n"


# --- _needs_quoting (lines 231, 234, 237, 241, 246, 250, 255) ---------------


def test_needs_quoting_empty_string():
    assert _needs_quoting("") is True  # line 231


def test_needs_quoting_edge_whitespace_and_special():
    assert _needs_quoting(" pad") is True  # line 234 strip mismatch
    assert _needs_quoting("a:b") is True  # contains ':'
    assert _needs_quoting("a#b") is True  # contains '#'
    assert _needs_quoting('a"b') is True


def test_needs_quoting_reserved_words():
    # line 237: bool/null literals coerced.
    for word in ("true", "FALSE", "Yes", "no", "ON", "off", "null", "~"):
        assert _needs_quoting(word) is True


def test_needs_quoting_leading_indicator():
    # line 241: leading reserved indicator.
    assert _needs_quoting("@handle") is True
    assert _needs_quoting("[bracket") is True


def test_needs_quoting_int_like():
    # line 250: integer-looking strings.
    assert _needs_quoting("0123") is True
    assert _needs_quoting("42") is True


def test_needs_quoting_signed_and_float():
    # line 246 (sign stripping) + 255 (float).
    assert _needs_quoting("+1_000") is True
    assert _needs_quoting("3.14") is True
    assert _needs_quoting("-2.5") is True


def test_needs_quoting_plain_word_ok():
    assert _needs_quoting("hello") is False
    assert _needs_quoting("Printer down") is False


def test_needs_quoting_underscore_only_not_numeric():
    # candidate becomes empty after stripping underscores -> falls through.
    assert _needs_quoting("_") is False


# --- _yaml_scalar (lines 263, 265, 267, 270) --------------------------------


def test_yaml_scalar_none():
    assert _yaml_scalar(None) == "null"  # line 263


def test_yaml_scalar_bool():
    assert _yaml_scalar(True) == "true"  # line 265
    assert _yaml_scalar(False) == "false"


def test_yaml_scalar_numbers():
    assert _yaml_scalar(7) == "7"  # int -> not bool branch
    assert _yaml_scalar(1.5) == "1.5"


def test_yaml_scalar_plain_string():
    assert _yaml_scalar("plain") == "plain"


def test_yaml_scalar_quoted_string():
    # line 270: needs quoting -> json.dumps representation.
    assert _yaml_scalar("true") == '"true"'
    assert _yaml_scalar("a:b") == '"a:b"'


def test_render_yaml_quotes_reserved_value():
    buf = io.StringIO()
    render({"flag": "yes"}, OutputFormat.YAML, stream=buf)
    assert buf.getvalue() == 'flag: "yes"\n'
