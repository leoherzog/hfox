"""Helpers for HappyFox custom-field encoding.

HappyFox addresses custom fields by internal id, not label:
  * ticket custom fields use the key  t-cf-<id>
  * contact custom fields use         c-cf-<id>  (on create) / ccf-<id> (on updates)
  * asset custom fields are sent as a JSON object keyed by the bare field id

Value encoding by field type:
  text/textarea -> string
  number        -> int/float
  dropdown      -> single option id (int)
  multiple opts -> list of option ids ([1,4,5])
  date          -> "YYYY-MM-DD" string

`--cf` options arrive as "<id>=<value>" or "<key>=<value>" strings. A value with
commas becomes a list (multi-option); otherwise we coerce int/float when possible
and fall back to the raw string. For full control over value types (and to bypass
the comma/number coercion entirely), use `--cf-json` with a JSON object mapping
field id -> value; see `parse_cf_json`.
"""

from __future__ import annotations

import json
from typing import Any

from ..core.errors import ValidationError

#: Recognized custom-field key prefixes. A --cf key that already starts with one
#: of these is used verbatim; a bare id gets the caller's prefix applied.
KNOWN_CF_PREFIXES: tuple[str, ...] = ("t-cf-", "c-cf-", "ccf-")

#: Lowercased value strings that Python's float() accepts but JSON cannot encode.
#: We treat them as plain strings so we never emit float('inf')/float('nan').
_NON_FINITE_FLOATS = frozenset(
    {"inf", "-inf", "+inf", "nan", "-nan", "+nan", "infinity", "-infinity", "+infinity"}
)


def _coerce_scalar(text: str) -> Any:
    text = text.strip()
    if text == "":
        return text
    try:
        return int(text)
    except ValueError:
        pass
    if text.lower() not in _NON_FINITE_FLOATS:
        try:
            return float(text)
        except ValueError:
            pass
    return text


def coerce_value(raw: str) -> Any:
    """Coerce a raw --cf value string into the right JSON type."""
    if "," in raw:
        return [_coerce_scalar(part) for part in raw.split(",") if part.strip() != ""]
    return _coerce_scalar(raw)


def parse_cf_options(items: list[str] | None, *, prefix: str = "t-cf-") -> dict[str, Any]:
    """Parse repeated --cf "<id>=<value>" options into a {prefix+id: value} dict.

    A key that already starts with a known prefix (t-cf-/c-cf-/ccf-) is used
    verbatim; a bare numeric id gets the given prefix applied. An all-blank value
    raises ValidationError (use --cf-json to send an explicit empty value).
    """
    out: dict[str, Any] = {}
    for item in items or []:
        if "=" not in item:
            raise ValidationError(
                f"Invalid --cf value '{item}'. Expected '<id>=<value>' (e.g. '3=Urgent')."
            )
        key, _, value = item.partition("=")
        key = key.strip()
        if value.strip() == "":
            raise ValidationError(
                f"Invalid --cf value '{item}'. Empty value for '{key}'; "
                "use --cf-json to send an explicit empty value."
            )
        if key.startswith(KNOWN_CF_PREFIXES):
            field_key = key
        else:
            field_key = f"{prefix}{key}"
        out[field_key] = coerce_value(value)
    return out


def parse_asset_cf(items: list[str] | None) -> dict[str, Any]:
    """Parse --cf options for assets into a {"<id>": value} JSON object."""
    out: dict[str, Any] = {}
    for item in items or []:
        if "=" not in item:
            raise ValidationError(
                f"Invalid --cf value '{item}'. Expected '<id>=<value>' (e.g. '5=4')."
            )
        key, _, value = item.partition("=")
        key = key.strip()
        if value.strip() == "":
            raise ValidationError(
                f"Invalid --cf value '{item}'. Empty value for '{key}'; "
                "use --cf-json to send an explicit empty value."
            )
        out[key] = coerce_value(value)
    return out


def parse_cf_json(raw: str | None, prefix: str = "t-cf-") -> dict[str, Any]:
    """Parse a --cf-json escape-hatch string into a {prefix+id: value} dict.

    `raw` must be a JSON object mapping field id -> value. Values are passed
    through with NO coercion (their types come straight from the JSON). For
    assets the caller passes prefix="" so keys are bare ids.

    Returns {} for None/empty. Raises ValidationError on invalid JSON or a
    non-object payload.
    """
    if raw is None or raw.strip() == "":
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValidationError(f"Invalid --cf-json: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValidationError(
            "Invalid --cf-json: expected a JSON object mapping field id -> value."
        )
    return {f"{prefix}{key}": value for key, value in parsed.items()}
