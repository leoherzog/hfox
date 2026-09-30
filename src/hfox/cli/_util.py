"""Shared command helpers: prompts, validation, CSV splitting, JSON, attachments, bulk results."""

from __future__ import annotations

import contextlib
import json
import math
import re
import sys
from pathlib import Path
from typing import Any

import typer

from ..core.errors import ValidationError
from . import output

#: Documented cap on the combined size of one request's attachments.
MAX_ATTACHMENT_BYTES = 25_000_000

#: delete_contacts reports this for a contact outside the group; it is not a failure.
NOT_IN_GROUP = "Contact not part of the contact group"

_DIGITS = re.compile(r"[0-9]+")
_CONTACT_ID = re.compile(r"[1-9][0-9]*")
_EMAIL = re.compile(r"[^@/\s]+@[^@/\s]+")


def confirm(message: str) -> None:
    """Ask for confirmation on stderr; abort unless the user agrees."""
    # Click writes the prompt's trailing space to stdout, which would corrupt the data stream.
    with contextlib.redirect_stdout(sys.stderr):
        typer.confirm(message, abort=True, err=True)


def compact(data: dict[str, Any]) -> dict[str, Any]:
    """Drop keys whose value is None."""
    return {k: v for k, v in data.items() if v is not None}


def comma_join(value: Any) -> Any:
    """Join a list into a comma-separated string; pass other values through."""
    if isinstance(value, (list, tuple)):
        return ",".join(str(v) for v in value)
    return value


def split_csv(value: str | None) -> list[str] | None:
    """Split a comma-separated string into trimmed, non-blank parts; None when there are none."""
    if value is None:
        return None
    return [part.strip() for part in value.split(",") if part.strip()] or None


def split_csv_ints(value: str | None) -> list[int] | None:
    """Split a comma-separated string of ASCII digits into ints; [] when blank, None for None."""
    parts = split_csv(value)
    if parts is None:
        return None if value is None else []
    if not all(_DIGITS.fullmatch(p) for p in parts):
        raise ValidationError(f"Expected comma-separated integers, got '{value}'.")
    return [int(p) for p in parts]


def require_nonblank(value: str | None, flag: str) -> str:
    """Return `value` unchanged; raise ValidationError if it is missing or whitespace-only."""
    if value is None or value.strip() == "":
        raise ValidationError(f"{flag} must not be blank.")
    return value


def nonblank_or_none(value: str | None) -> str | None:
    """Return `value`, or None when it is missing or whitespace-only."""
    return value if value and value.strip() else None


def validate_ticket_id(value: str) -> str:
    """Return a ticket number; raise ValidationError unless it is ASCII digits."""
    if not _DIGITS.fullmatch(value):
        raise ValidationError(f"Invalid ticket id '{value}'; expected the numeric ticket number.")
    return value


def validate_contact_ref(value: str) -> str:
    """Return a contact reference; raise ValidationError unless it is a positive id or an email."""
    if not (_CONTACT_ID.fullmatch(value) or _EMAIL.fullmatch(value)):
        raise ValidationError(
            f"Invalid contact '{value}'; expected a positive id or an email address."
        )
    return value


def attach(
    body: dict[str, Any], attachments: list[str] | None, *, field: str = "attachments"
) -> dict[str, Any]:
    """Build AppContext.call kwargs: {"json": body}, or {"data", "files"} when files are given.

    The JSON body is sent as given. Multipart drops None, JSON-encodes lists, dicts and
    bools, and stringifies the rest. Raises ValidationError for a missing or unreadable file
    or a total over MAX_ATTACHMENT_BYTES.
    """
    if not attachments:
        return {"json": body}
    paths = [Path(path).expanduser() for path in attachments]
    for p in paths:
        if not p.is_file():
            raise ValidationError(f"Attachment not found: {p}")
    total = sum(p.stat().st_size for p in paths)
    if total > MAX_ATTACHMENT_BYTES:
        raise ValidationError(
            f"Attachments total {total:,} bytes; the limit is {MAX_ATTACHMENT_BYTES:,}."
        )
    # httpx sets each part's content type from the filename extension.
    files = []
    for p in paths:
        try:
            files.append((field, (p.name, p.read_bytes())))
        except OSError as exc:
            raise ValidationError(f"Cannot read {p}: {exc.strerror}") from exc
    data = {
        k: (json.dumps(v) if isinstance(v, (list, dict, bool)) else str(v))
        for k, v in body.items()
        if v is not None
    }
    return {"data": data, "files": files}


def _reject_constant(name: str) -> Any:
    raise ValueError(f"{name} is not valid JSON")


def _finite_float(text: str) -> float:
    number = float(text)
    if not math.isfinite(number):
        raise ValueError(f"{text} is out of range")
    return number


def parse_json(text: str, source: str) -> Any:
    """Parse standard JSON; raise ValidationError naming `source` on bad or non-finite input."""
    try:
        return json.loads(text, parse_constant=_reject_constant, parse_float=_finite_float)
    except ValueError as exc:  # includes JSONDecodeError
        raise ValidationError(f"Invalid JSON in {source}: {exc}") from exc


def load_json_file(path: str) -> Any:
    """Read and parse a UTF-8 JSON file; raise ValidationError if it is missing or invalid."""
    p = Path(path).expanduser()
    if not p.is_file():
        raise ValidationError(f"File not found: {p}")
    try:
        text = p.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValidationError(f"{p} is not UTF-8 (byte {exc.start}).") from exc
    except OSError as exc:
        raise ValidationError(f"Cannot read {p}: {exc.strerror}") from exc
    return parse_json(text, str(p))


def count_failures(result: Any, *, benign: str | None = None) -> int:
    """Count entries of a bulk or group-membership response that report success false.

    Entries whose data.message equals `benign` are not counted.
    """
    if not isinstance(result, list):
        return 0
    failed = 0
    for entry in result:
        if not isinstance(entry, dict) or entry.get("success") is not False:
            continue
        data = entry.get("data")
        if benign is not None and isinstance(data, dict) and data.get("message") == benign:
            continue
        failed += 1
    return failed


def exit_on_failures(result: Any, *, benign: str | None = None) -> None:
    """Call after rendering: warn on stderr and exit 1 when any entry failed."""
    failed = count_failures(result, benign=benign)
    if failed:
        output.warn(f"{failed} of {len(result)} entries failed.")
        raise typer.Exit(1)
