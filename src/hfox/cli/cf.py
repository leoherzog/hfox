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
    """Map a numeric id to prefix+id and keep an allowed prefix a key carries.

    The id loses its leading zeros, so every spelling of a field yields one key. Raises
    ValidationError for any other key and for an id of zero.
    """
    prefixes = [p for p in ((prefix,) if allowed is None else allowed) if p]
    for p in ("", *prefixes):
        digits = key[len(p):]
        if key.startswith(p) and _DIGITS.fullmatch(digits):
            number = digits.lstrip("0")
            if not number:
                raise ValidationError(
                    f"Invalid custom-field key '{key}'; a field id is at least 1."
                )
            return f"{p or prefix}{number}"
    forms = ["<id>", *(f"{p}<id>" for p in prefixes)]
    raise ValidationError(
        f"Invalid custom-field key '{key}'; expected {' or '.join(forms)} with a numeric id."
    )


def parse_cf_options(
    items: list[str] | None,
    *,
    prefix: str = "t-cf-",
    allowed: tuple[str, ...] | None = None,
    json_flag: str = "--cf-json",
) -> dict[str, Any]:
    """Parse repeated "<id>=<value>" options into {prefix+id: coerced value}.

    A key already carrying a prefix in `allowed` (default: `prefix` alone) keeps it. Ids lose
    their leading zeros, and a field given twice keeps its last value.
    Raises ValidationError on a malformed item, an invalid key or an all-blank value;
    the last names `json_flag`, the caller's flag that can send an empty value.
    """
    out: dict[str, Any] = {}
    for item in items or []:
        key, sep, value = item.partition("=")
        if not sep:
            raise ValidationError(f"Invalid custom field '{item}'; expected '<id>=<value>'.")
        if value.strip() == "":
            raise ValidationError(
                f"Empty value in custom field '{item}'; use {json_flag} to send an empty value."
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
    flag: str = "--cf-json",
) -> dict[str, Any]:
    """Parse a JSON object of field id -> value into {prefix+id: value}, values uncoerced.

    Keys follow the parse_cf_options rules. Returns {} for a blank input. Raises
    ValidationError naming `flag`, the caller's flag, for input that is not a JSON object.
    """
    if raw is None or raw.strip() == "":
        return {}
    parsed = parse_json(raw, flag)
    if not isinstance(parsed, dict):
        raise ValidationError(f"{flag} must be an object mapping field id -> value.")
    return {_field_key(key, prefix, allowed): value for key, value in parsed.items()}
