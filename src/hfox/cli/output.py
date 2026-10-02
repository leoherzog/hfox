"""Render command results to stdout as json (default), table, csv or yaml.

Table and CSV flatten nested objects to dot-notation keys. Status and warnings go to stderr.
"""

from __future__ import annotations

import csv
import io
import json
import math
import os
import re
import sys
import unicodedata
from enum import StrEnum
from typing import Any

from rich.console import Console
from rich.table import Table
from rich.text import Text

from ..core.errors import ValidationError


class OutputFormat(StrEnum):
    JSON = "json"
    TABLE = "table"
    CSV = "csv"
    YAML = "yaml"

    @classmethod
    def parse(cls, value: str | None) -> OutputFormat:
        if not value:
            return cls.JSON
        normalized = value.strip().lower()
        if normalized in ("yml",):
            return cls.YAML
        try:
            return cls(normalized)
        except ValueError:
            raise ValidationError(
                f"Unknown output format {value!r}; expected json, table, csv or yaml."
            ) from None


def _color_enabled(stream) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    return bool(getattr(stream, "isatty", lambda: False)())


_err_console = Console(stderr=True, highlight=False)


def warn(message: str) -> None:
    message = sanitize_text(message)
    if _color_enabled(sys.stderr):
        # Only the prefix goes through Rich, which would parse, wrap and tab-expand the message.
        _err_console.print(Text("warning:", style="yellow"), end=" ")
        print(message, file=sys.stderr)
    else:
        print(f"warning: {message}", file=sys.stderr)


def info(message: str) -> None:
    print(sanitize_text(message), file=sys.stderr)


def fox(*streams) -> str:
    """Return "🦊 " when every stream (default stderr) can encode it, else ""."""
    for stream in streams or (sys.stderr,):
        encoding = getattr(stream, "encoding", None) or "utf-8"
        try:
            "🦊".encode(encoding)
        except (LookupError, UnicodeEncodeError):
            return ""
    return "🦊 "


def _flatten(obj: Any, prefix: str = "") -> dict[str, Any]:
    """Flatten nested dicts into dot-notation keys; empty dicts stay as leaves."""
    out: dict[str, Any] = {}
    if isinstance(obj, dict):
        for key, value in obj.items():
            new_key = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(value, dict) and value:
                out.update(_flatten(value, new_key))
            else:
                out[new_key] = value
    else:
        out[prefix or "value"] = obj
    return out


def _as_rows(data: Any) -> list[dict[str, Any]]:
    """Coerce arbitrary JSON into a list of flat row dicts."""
    rows = data if isinstance(data, list) else [data]
    return [_flatten(row) if isinstance(row, dict) else {"value": row} for row in rows]


def _column_union(rows: list[dict[str, Any]]) -> list[str]:
    columns: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            if key not in seen:
                seen.add(key)
                columns.append(key)
    return columns


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        # A nested cell is strict JSON text, so a non-finite number becomes null.
        return json.dumps(_finite(value), ensure_ascii=False, allow_nan=False, default=str)
    return str(value)


_KEPT_CONTROLS = frozenset("\n\t\u200c\u200d")


def sanitize_text(text: str) -> str:
    """Drop control and format characters from text bound for a terminal, table or CSV cell.

    Newline, tab and the zero-width joiner and non-joiner are kept.
    """
    return "".join(
        char
        for char in text
        if char in _KEPT_CONTROLS or unicodedata.category(char) not in ("Cc", "Cf")
    )


sanitize_cell = sanitize_text


# Matched after sanitize_cell, which drops CR, so CR is not listed. The escapes are the
# full-width "=", "+", "-" and "@".
_FORMULA_TRIGGERS = ("=", "+", "-", "@", "\t", "\n", "\uff1d", "\uff0b", "\uff0d", "\uff20")
# ASCII only: a full-width sign or digit is never exempt.
_SIGNED_DECIMAL = re.compile(r"[+-]?[0-9]+(\.[0-9]+)?")


def _csv_cell(value: Any) -> str:
    """Sanitize a CSV cell and quote-prefix a string a spreadsheet would run as a formula."""
    text = sanitize_cell(_stringify(value))
    if (
        isinstance(value, str)
        and text.startswith(_FORMULA_TRIGGERS)
        and not _SIGNED_DECIMAL.fullmatch(text)
    ):
        return "'" + text
    return text


def render(data: Any, fmt: OutputFormat, *, stream=None) -> None:
    """Render data in the requested format.

    `stream` defaults to sys.stdout resolved at call time, so CliRunner redirects are honored.
    """
    if stream is None:
        stream = sys.stdout
    if fmt is OutputFormat.JSON:
        _render_json(data, stream)
    elif fmt is OutputFormat.TABLE:
        _render_table(data, stream)
    elif fmt is OutputFormat.CSV:
        _render_csv(data, stream)
    elif fmt is OutputFormat.YAML:
        _render_yaml(data, stream)


def _ascii_only(stream) -> bool:
    # A non-UTF stream backslash-replaces raw non-ASCII, which is not valid JSON.
    encoding = (getattr(stream, "encoding", None) or "utf-8").lower().replace("-", "")
    return not encoding.startswith("utf")


# C1 and DEL, bidi controls, line and paragraph separators, tag characters.
_JSON_ESCAPED = re.compile(
    "[\x7f-\x9f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069\u2028\u2029\U000e0000-\U000e007f]"
)


def _json_escape(match: re.Match[str]) -> str:
    code = ord(match.group())
    if code <= 0xFFFF:
        return f"\\u{code:04x}"
    code -= 0x10000
    return f"\\u{0xD800 + (code >> 10):04x}\\u{0xDC00 + (code & 0x3FF):04x}"


def _finite(data: Any) -> Any:
    """Copy JSON data with None in place of every NaN and infinity, as JSON.stringify does."""
    if isinstance(data, float):
        return data if math.isfinite(data) else None
    if isinstance(data, dict):
        return {key: _finite(value) for key, value in data.items()}
    if isinstance(data, (list, tuple)):
        return [_finite(item) for item in data]
    return data


def _dump_json(data: Any, stream, **kwargs: Any) -> str:
    """Serialize to strict JSON text that carries terminal-affecting characters as escapes.

    Every JSON document hfox prints comes from here. A non-finite number becomes null.
    """
    text = json.dumps(
        _finite(data),
        ensure_ascii=_ascii_only(stream),
        allow_nan=False,
        default=str,
        **kwargs,
    )
    return _JSON_ESCAPED.sub(_json_escape, text)


def _render_json(data: Any, stream) -> None:
    stream.write(_dump_json(data, stream, indent=2) + "\n")


def render_ndjson_line(data: Any, *, stream=None) -> None:
    """Write one compact JSON document as a single flushed line (NDJSON)."""
    if stream is None:
        stream = sys.stdout
    stream.write(_dump_json(data, stream, separators=(",", ":")) + "\n")
    stream.flush()


def _render_table(data: Any, stream) -> None:
    """Render a list as one row per item and a single dict as field/value rows."""
    # Cells are Text so data containing Rich markup or :emoji: codes prints verbatim.
    if isinstance(data, dict):
        columns = ["field", "value"]
        cells = [[key, _stringify(value)] for key, value in _flatten(data).items()]
    else:
        rows = _as_rows(data)
        columns = _column_union(rows)
        cells = [[_stringify(row.get(col)) for col in columns] for row in rows]
    if not cells:
        info("(no results)")
        return
    table = Table(show_header=True, header_style="bold")
    for col in columns:
        table.add_column(Text(sanitize_cell(col)), overflow="fold", no_wrap=False)
    for row in cells:
        table.add_row(*[Text(sanitize_cell(cell)) for cell in row])
    Console(file=stream, highlight=False, soft_wrap=False).print(table)


def _render_csv(data: Any, stream) -> None:
    """Write RFC 4180 rows ending in CRLF; an LF inside a cell stays a bare LF."""
    rows = _as_rows(data)
    if not rows:
        # No header either, so an empty result stays a valid empty CSV.
        return
    columns = _column_union(rows)
    buffer = io.StringIO()
    # Positional rows, since two headers can sanitize to the same text.
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow([_csv_cell(col) for col in columns])
    for row in rows:
        writer.writerow([_csv_cell(row.get(col)) for col in columns])
    text = buffer.getvalue()
    raw = getattr(stream, "buffer", None)
    if raw is None:
        stream.write(text)
        return
    # A Windows text layer would turn each \n into \r\n, so the bytes go under it.
    stream.flush()
    raw.write(
        text.encode(
            getattr(stream, "encoding", None) or "utf-8",
            getattr(stream, "errors", None) or "strict",
        )
    )


def _render_yaml(data: Any, stream) -> None:
    """Emit block-style YAML that loads back to the same data under YAML 1.1 and 1.2."""
    stream.write(_to_yaml(data, 0) + "\n")


def _to_yaml(obj: Any, indent: int) -> str:
    pad = "  " * indent
    if isinstance(obj, dict) and obj:
        items = [(f"{_yaml_scalar(str(key))}:", value) for key, value in obj.items()]
    elif isinstance(obj, list) and obj:
        items = [("-", value) for value in obj]
    else:
        return f"{pad}{_yaml_scalar(obj)}"
    lines = []
    for head, value in items:
        if isinstance(value, (dict, list)) and value:
            lines.append(f"{pad}{head}\n{_to_yaml(value, indent + 1)}")
        else:
            lines.append(f"{pad}{head} {_yaml_scalar(value)}")
    return "\n".join(lines)


# YAML 1.1 bool and null words; the check is case-insensitive.
_YAML_RESERVED_WORDS = frozenset({"y", "n", "yes", "no", "true", "false", "on", "off", "null"})
_YAML_ESCAPES = {'"': '\\"', "\\": "\\\\", "\n": "\\n", "\t": "\\t", "\r": "\\r"}


def _needs_quoting(text: str) -> bool:
    """Whether a string would not load back as itself if emitted as a plain scalar.

    Plain output is limited to printable text starting with a letter or '_', which no
    YAML 1.1 or 1.2 number, timestamp, merge or value resolver matches.
    """
    return not (
        (text[:1].isalpha() or text[:1] == "_")
        and text.isprintable()
        and text == text.rstrip()
        and not any(c in text for c in ":#\"'")
        and text.lower() not in _YAML_RESERVED_WORDS
    )


def _yaml_quote(text: str) -> str:
    """Double-quote text, escaping every character YAML cannot carry raw."""
    out = []
    for char in text:
        code = ord(char)
        if char in _YAML_ESCAPES:
            out.append(_YAML_ESCAPES[char])
        elif char.isprintable():
            out.append(char)
        elif code <= 0xFF:
            out.append(f"\\x{code:02x}")
        elif code <= 0xFFFF:
            out.append(f"\\u{code:04x}")
        else:
            out.append(f"\\U{code:08x}")
    return '"' + "".join(out) + '"'


def _yaml_float(value: float) -> str:
    if math.isnan(value):
        return ".nan"
    if math.isinf(value):
        return ".inf" if value > 0 else "-.inf"
    text = repr(value)
    # YAML 1.1 floats need a '.' in the mantissa, so 1e+16 becomes 1.0e+16.
    if "e" in text and "." not in text:
        mantissa, exponent = text.split("e")
        text = f"{mantissa}.0e{exponent}"
    return text


def _yaml_scalar(value: Any) -> str:
    """Render a scalar or an empty container as a YAML flow value."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return _yaml_float(value)
    if isinstance(value, dict):
        return "{}"
    if isinstance(value, list):
        return "[]"
    text = str(value)
    return _yaml_quote(text) if _needs_quoting(text) else text
