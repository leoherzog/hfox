"""Custom-field option parsing and help text for ticket, contact and asset writes."""

from __future__ import annotations

import json
import re
from typing import Any

from ..core.errors import ValidationError
from ._util import _DIGITS, parse_json

# Rich markup would swallow an unescaped [a,b].
CF_HELP = "Custom field '<id>=<value>' (repeatable); '<id>=\\[a,b]' sends a list."
CF_JSON_HELP = "Custom fields as JSON {id: value}, sent uncoerced."

_NUMBER = re.compile(r"-?(0|[1-9][0-9]*)(\.[0-9]+)?")


def _coerce_scalar(text: str) -> Any:
    if not _NUMBER.fullmatch(text):
        return text
    try:
        number = float(text) if "." in text else int(text)
    except ValueError:  # int() refuses more than 4300 digits
        return text
    # Keep a number only when it re-serializes to the same text, so 1.10 stays a string.
    return number if json.dumps(number) == text else text


def coerce_value(raw: str) -> Any:
    """Coerce a --cf value: canonical ASCII decimals become numbers, "[a,b]" a list.

    Anything else is returned as the exact string, commas included.
    """
    if not (raw.startswith("[") and raw.endswith("]")):
        return _coerce_scalar(raw)
    inner = raw[1:-1]
    if inner.strip() == "":
        return []
    items = [part.strip() for part in inner.split(",")]
    if "" in items:
        raise ValidationError(f"Empty item in custom-field list '{raw}'.")
    return [_coerce_scalar(item) for item in items]


def _field_key(key: str, prefix: str, allowed: tuple[str, ...] | None) -> str:
    """Map a numeric id to prefix+id; pass through a key carrying an allowed prefix."""
    if _DIGITS.fullmatch(key):
        return f"{prefix}{key}"
    prefixes = [p for p in ((prefix,) if allowed is None else allowed) if p]
    for p in prefixes:
        if key.startswith(p) and _DIGITS.fullmatch(key[len(p):]):
            return key
    forms = ["<id>", *(f"{p}<id>" for p in prefixes)]
    raise ValidationError(
        f"Invalid custom-field key '{key}'; expected {' or '.join(forms)} with a numeric id."
    )


def parse_cf_options(
    items: list[str] | None,
    *,
    prefix: str = "t-cf-",
    allowed: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Parse repeated "<id>=<value>" options into {prefix+id: coerced value}.

    Keys already carrying a prefix in `allowed` (default: `prefix` alone) pass through.
    Raises ValidationError on a malformed item, an invalid key or an all-blank value.
    """
    out: dict[str, Any] = {}
    for item in items or []:
        key, sep, value = item.partition("=")
        if not sep:
            raise ValidationError(f"Invalid custom field '{item}'; expected '<id>=<value>'.")
        if value.strip() == "":
            raise ValidationError(
                f"Empty value in custom field '{item}'; use --cf-json to send an empty value."
            )
        out[_field_key(key.strip(), prefix, allowed)] = coerce_value(value)
    return out


def parse_asset_cf(items: list[str] | None) -> dict[str, Any]:
    """Parse asset --cf options into a {"<id>": value} object."""
    return parse_cf_options(items, prefix="", allowed=())


def parse_cf_json(
    raw: str | None,
    prefix: str = "t-cf-",
    allowed: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Parse a JSON object of field id -> value into {prefix+id: value}, values uncoerced.

    Keys follow the parse_cf_options rules. Returns {} for a blank input.
    """
    if raw is None or raw.strip() == "":
        return {}
    parsed = parse_json(raw, "custom-field option")
    if not isinstance(parsed, dict):
        raise ValidationError("Custom-field JSON must be an object mapping field id -> value.")
    return {_field_key(key, prefix, allowed): value for key, value in parsed.items()}
