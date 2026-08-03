"""Output rendering: json (default), table, csv, yaml.

Design follows gws: JSON is the default so output stays machine-parseable for
agents/scripts; table is the human-friendly view (Rich), auto-discovering the
union of columns across rows and flattening nested objects to dot-notation.
Status/warning text always goes to stderr so stdout stays clean structured data.
"""

from __future__ import annotations

import csv
import io
import json
import os
import sys
from enum import StrEnum
from typing import Any

from rich.console import Console
from rich.table import Table

MAX_CELL_WIDTH = 60


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
            # Unknown format falls back to JSON with a warning (gws behavior).
            warn(f"Unknown format '{value}', falling back to json.")
            return cls.JSON


def _color_enabled(stream) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    return bool(getattr(stream, "isatty", lambda: False)())


_err_console = Console(stderr=True, highlight=False)


def warn(message: str) -> None:
    if _color_enabled(sys.stderr):
        _err_console.print(f"[yellow]warning:[/yellow] {message}")
    else:
        print(f"warning: {message}", file=sys.stderr)


def info(message: str) -> None:
    print(message, file=sys.stderr)


def _flatten(obj: Any, prefix: str = "") -> dict[str, Any]:
    """Flatten nested dicts into dot-notation keys for tabular display."""
    out: dict[str, Any] = {}
    if isinstance(obj, dict):
        for key, value in obj.items():
            new_key = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(value, dict) and value:
                out.update(_flatten(value, new_key))
            else:
                # Keep empty dicts as a leaf so the key isn't silently dropped,
                # symmetric with how empty lists are preserved.
                out[new_key] = value
    else:
        out[prefix or "value"] = obj
    return out


def _as_rows(data: Any) -> list[dict[str, Any]]:
    """Coerce arbitrary JSON into a list of flat row dicts."""
    if isinstance(data, list):
        rows = data
    else:
        rows = [data]
    flat_rows: list[dict[str, Any]] = []
    for row in rows:
        if isinstance(row, dict):
            flat_rows.append(_flatten(row))
        else:
            flat_rows.append({"value": row})
    return flat_rows


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
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value)


def _truncate(text: str, width: int = MAX_CELL_WIDTH) -> str:
    if len(text) <= width:
        return text
    return text[: width - 1] + "…"


def render(data: Any, fmt: OutputFormat, *, stream=None) -> None:
    """Render data to the given stream in the requested format.

    ``stream`` defaults to the *current* ``sys.stdout`` resolved at call time
    (not import time), so redirects — e.g. ``typer.testing.CliRunner`` — are
    honored.
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


def _render_json(data: Any, stream) -> None:
    stream.write(json.dumps(data, indent=2, ensure_ascii=False, default=str) + "\n")


def render_ndjson_line(data: Any, *, stream=None) -> None:
    """Write one compact JSON document on a single line (NDJSON), flushed.

    Used by --page-all in JSON format: one page envelope per line, emitted as
    each page arrives — mirrors gws's streaming pagination output.
    """
    if stream is None:
        stream = sys.stdout
    stream.write(json.dumps(data, ensure_ascii=False, default=str, separators=(",", ":")) + "\n")
    stream.flush()


def _render_table(data: Any, stream) -> None:
    rows = _as_rows(data)
    if not rows:
        info("(no results)")
        return
    columns = _column_union(rows)
    table = Table(show_header=True, header_style="bold")
    for col in columns:
        table.add_column(col, overflow="fold", no_wrap=False)
    for row in rows:
        table.add_row(*[_truncate(_stringify(row.get(col))) for col in columns])
    console = Console(file=stream, highlight=False, soft_wrap=False)
    console.print(table)


def _render_csv(data: Any, stream) -> None:
    rows = _as_rows(data)
    if not rows:
        # Empty result -> empty file (no header). Table prints "(no results)";
        # CSV stays header-less so the output remains parseable by csv readers.
        return
    columns = _column_union(rows)
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({col: _stringify(row.get(col)) for col in columns})
    stream.write(buffer.getvalue())


def _render_yaml(data: Any, stream) -> None:
    """Minimal YAML emitter (avoids a PyYAML dependency for a read-mostly need)."""
    text = _to_yaml(data, 0)
    if not text.endswith("\n"):
        text += "\n"
    stream.write(text)


def _to_yaml(obj: Any, indent: int) -> str:
    pad = "  " * indent
    if isinstance(obj, dict):
        if not obj:
            return "{}"
        lines = []
        for key, value in obj.items():
            if isinstance(value, (dict, list)) and value:
                lines.append(f"{pad}{key}:")
                lines.append(_to_yaml(value, indent + 1))
            else:
                lines.append(f"{pad}{key}: {_yaml_scalar(value)}")
        return "\n".join(lines)
    if isinstance(obj, list):
        if not obj:
            return "[]"
        lines = []
        for item in obj:
            if isinstance(item, (dict, list)) and item:
                lines.append(f"{pad}-")
                lines.append(_to_yaml(item, indent + 1))
            else:
                lines.append(f"{pad}- {_yaml_scalar(item)}")
        return "\n".join(lines)
    return f"{pad}{_yaml_scalar(obj)}"


# YAML 1.1 bool/null literals (every case variant a 1.1 parser would coerce).
_YAML_RESERVED_WORDS = frozenset(
    {
        "true",
        "false",
        "yes",
        "no",
        "on",
        "off",
        "null",
        "~",
    }
)
# Indicators that have special meaning at the start of a plain scalar.
_YAML_LEADING_INDICATORS = frozenset("*&!|>%@`,[]{}#:-?")


def _needs_quoting(text: str) -> bool:
    """Whether a string scalar would be misread by a YAML 1.1 parser unquoted."""
    if text == "":
        return True
    # Whitespace at the edges, or characters that break plain flow scalars.
    if text.strip() != text or any(c in text for c in ":#\n\"'"):
        return True
    # YAML 1.1 bool/null literals (case-insensitive) would be coerced.
    if text.lower() in _YAML_RESERVED_WORDS:
        return True
    # Leading reserved indicator (incl. things like '-1' handled by the
    # numeric check below, but '- ' / '-x' need quoting).
    if text[0] in _YAML_LEADING_INDICATORS:
        return True
    # Numeric-looking strings (incl. leading-zero like '0123', floats, signs)
    # would be parsed as numbers, losing the string type.
    candidate = text.replace("_", "")
    if candidate and candidate[0] in "+-":
        candidate = candidate[1:]
    if candidate:
        try:
            int(candidate)
            return True
        except ValueError:
            pass
        try:
            float(candidate)
            return True
        except ValueError:
            pass
    return False


def _yaml_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if _needs_quoting(text):
        return json.dumps(text, ensure_ascii=False)
    return text
