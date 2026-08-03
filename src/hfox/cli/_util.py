"""Small shared helpers for command modules."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..core.errors import ValidationError


def compact(data: dict[str, Any]) -> dict[str, Any]:
    """Drop keys whose value is None."""
    return {k: v for k, v in data.items() if v is not None}


def comma_join(value: Any) -> Any:
    """Join a list into a comma-separated string; pass other values through."""
    if isinstance(value, (list, tuple)):
        return ",".join(str(v) for v in value)
    return value


def split_csv(value: str | None) -> list[str] | None:
    """Split a comma-separated string into a trimmed list (None -> None)."""
    if value is None:
        return None
    return [part.strip() for part in value.split(",") if part.strip() != ""]


def split_csv_ints(value: str | None) -> list[int] | None:
    """Split a comma-separated string into ints."""
    parts = split_csv(value)
    if parts is None:
        return None
    try:
        return [int(p) for p in parts]
    except ValueError as exc:
        raise ValidationError(f"Expected comma-separated integers, got '{value}'.") from exc


def attach(body: dict[str, Any], attachments: list[str] | None) -> dict[str, Any]:
    """Build kwargs for AppContext.call: a JSON body, or multipart when files exist.

    Returns either {"json": {...}} or {"data": {...}, "files": [...]}. When
    attachments are present every body value is stringified (multipart form
    fields are strings; lists/dicts are JSON-encoded).
    """
    body = compact(body)
    if not attachments:
        return {"json": body}
    files = []
    for path in attachments:
        p = Path(path).expanduser()
        if not p.is_file():
            raise ValidationError(f"Attachment not found: {p}")
        # NOTE: reads the whole file into memory and sends no explicit MIME type
        # (httpx defaults to application/octet-stream). Fine for the 25 MB cap.
        files.append(("attachments", (p.name, p.read_bytes())))
    data = {
        k: (json.dumps(v) if isinstance(v, (list, dict, bool)) else str(v))
        for k, v in body.items()
    }
    return {"data": data, "files": files}


def load_json_file(path: str) -> Any:
    """Read and parse a JSON file (used for bulk operations)."""
    p = Path(path).expanduser()
    if not p.is_file():
        raise ValidationError(f"File not found: {p}")
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValidationError(f"Invalid JSON in {p}: {exc}") from exc
