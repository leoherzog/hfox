"""Shared command helpers: validation, CSV splitting, JSON, guarded file reads, attachments,
bulk results and local row filters.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import re
import stat
import sys
import unicodedata
from collections.abc import Iterable
from pathlib import Path
from typing import Any, BinaryIO

import typer

from ..core.config import TOKEN_FILENAME
from ..core.errors import HfoxError, ValidationError
from . import output

#: A file argument with this value reads stdin.
STDIN = "-"

STAFF_HELP = "Acting staff email or name, looked up through staff/; excludes --staff-id."
STAFF_ID_HELP = "Acting staff id; excludes --staff."
YES_HELP = "Skip the confirmation prompt; required when stdin is not a terminal."

#: Documented cap on the combined size of one request's attachments.
MAX_ATTACHMENT_BYTES = 25_000_000

#: delete_contacts reports this for a contact outside the group; it is not a failure.
NOT_IN_GROUP = "Contact not part of the contact group"

_DIGITS = re.compile(r"[0-9]+")
_CONTACT_ID = re.compile(r"[1-9][0-9]*")
_EMAIL = re.compile(r"[^@/\s]+@[^@/\s]+")


def _has_console(stream: Any) -> bool:
    """True when `stream` is backed by a Windows console; false on any failure."""
    try:
        import ctypes
        import msvcrt
        from ctypes import wintypes

        handle = wintypes.HANDLE(msvcrt.get_osfhandle(stream.fileno()))
        mode = wintypes.DWORD()
        return bool(ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode)))
    except Exception:
        return False


def stdin_is_tty() -> bool:
    """True when stdin is an interactive terminal. Call as `_util.stdin_is_tty()`."""
    try:
        if not (sys.stdin and sys.stdin.isatty()):
            return False
    except Exception:
        return False
    # Windows isatty() is true for any character device, NUL included.
    return _has_console(sys.stdin) if sys.platform == "win32" else True


def text_file_help(what: str) -> str:
    """Help text for a flag that reads `what` from a file or stdin."""
    return f"File holding the {what}; '-' reads stdin."


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


def _refusal(p: Path) -> ValidationError:
    return ValidationError(f"Refusing to read {p}: it is inside the hfox config directory.")


def _expand(path: str | Path) -> Path:
    """Return `path` with `~` expanded, or unexpanded when it names no known home."""
    try:
        return Path(path).expanduser()
    except RuntimeError:
        return Path(path)


def _resolve(p: Path) -> Path | None:
    try:
        return p.resolve()
    except (OSError, RuntimeError, ValueError):
        return None


def _same_file(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except (OSError, ValueError):
        return False


def _is_within(path: Path, directory: Path) -> bool:
    """Component-wise containment on the normcase form; never a string prefix test."""
    return Path(os.path.normcase(path)).is_relative_to(Path(os.path.normcase(directory)))


def check_readable_path(path: str, forbidden: Iterable[Path] = ()) -> Path:
    """Return `path` with `~` expanded; raise ValidationError when it resolves to a
    `forbidden` directory or below one. Runs before any existence check.
    """
    p = _expand(path)
    resolved = _resolve(p)
    if resolved is None:
        raise ValidationError(f"Cannot read {p}: the path cannot be resolved.")
    chain = [resolved, *resolved.parents]
    for directory in forbidden:
        guard = _resolve(_expand(directory))
        if guard is None:
            continue
        if _is_within(resolved, guard) or any(_same_file(link, guard) for link in chain):
            raise _refusal(p)
    return p


def _token_identities(forbidden: Iterable[Path]) -> set[tuple[int, int]]:
    """Return the (st_dev, st_ino) of each existing token.json in a guarded directory."""
    found: set[tuple[int, int]] = set()
    for directory in forbidden:
        try:
            info = os.stat(_expand(directory) / TOKEN_FILENAME)
        except (OSError, ValueError):
            continue
        if info.st_ino:
            found.add((info.st_dev, info.st_ino))
    return found


def open_guarded(
    path: str, *, forbidden: Iterable[Path] = (), what: str = "File"
) -> tuple[Path, BinaryIO]:
    """Open a regular file for binary reading; return (path, handle), which the caller closes.

    Raises ValidationError for a guarded path, a hard link to a guarded token.json, or a
    missing, non-regular or unreadable file. Read from the handle, so the checked file is read.
    """
    forbidden = tuple(forbidden)
    p = check_readable_path(path, forbidden)
    # The type is checked before the open, which blocks on a named pipe.
    try:
        mode = os.stat(p).st_mode
    except (FileNotFoundError, NotADirectoryError) as exc:
        raise ValidationError(f"{what} not found: {p}") from exc
    except OSError as exc:
        raise ValidationError(f"Cannot read {p}: {exc.strerror}") from exc
    if not stat.S_ISREG(mode):
        raise ValidationError(f"{what} is not a regular file: {p}")
    try:
        handle = p.open("rb")
    except OSError as exc:
        raise ValidationError(f"Cannot read {p}: {exc.strerror}") from exc
    try:
        info = os.fstat(handle.fileno())
        if info.st_ino and (info.st_dev, info.st_ino) in _token_identities(forbidden):
            raise _refusal(p)
    except OSError as exc:
        handle.close()
        raise ValidationError(f"Cannot read {p}: {exc.strerror}") from exc
    except BaseException:
        handle.close()
        raise
    return p, handle


def check_no_secrets(data: bytes, source: str, secrets: Iterable[str] = ()) -> None:
    """Raise ValidationError when `data` holds the UTF-8 bytes of any of `secrets`."""
    for secret in secrets:
        if secret and secret.encode("utf-8") in data:
            raise ValidationError(
                f"Refusing to send {source}: it contains HappyFox credentials."
            )


def _read_all(handle: BinaryIO) -> bytes:
    return handle.read()


def read_text_file(
    path: str, *, forbidden: Iterable[Path] = (), secrets: Iterable[str] = ()
) -> str:
    """Read a guarded file as strict UTF-8, dropping a BOM; content is otherwise unchanged.

    Raises ValidationError for a guarded, missing, unreadable or non-UTF-8 file, or one
    holding credentials.
    """
    p, handle = open_guarded(path, forbidden=forbidden)
    with handle:
        try:
            data = _read_all(handle)
        except OSError as exc:
            raise ValidationError(f"Cannot read {p}: {exc.strerror}") from exc
    check_no_secrets(data, str(p), secrets)
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValidationError(f"{p} is not UTF-8 (byte {exc.start}).") from exc


def _too_large(total: int) -> ValidationError:
    return ValidationError(
        f"Attachments total {total:,} bytes; the limit is {MAX_ATTACHMENT_BYTES:,}."
    )


def attach(
    body: dict[str, Any],
    attachments: list[str] | None,
    *,
    field: str = "attachments",
    forbidden: Iterable[Path] = (),
    secrets: Iterable[str] = (),
) -> dict[str, Any]:
    """Build AppContext.call kwargs: {"json": body}, or {"data", "files"} when files are given.

    The JSON body is sent as given; multipart drops None and JSON-encodes lists, dicts and
    bools. Raises ValidationError for an unreadable file, credentials or a total over the cap.
    """
    if not attachments:
        return {"json": body}
    forbidden, secrets = tuple(forbidden), tuple(secrets)
    # httpx sets each part's content type from the filename extension.
    files = []
    with contextlib.ExitStack() as stack:
        opened = []
        for path in attachments:
            p, handle = open_guarded(path, forbidden=forbidden, what="Attachment")
            opened.append((p, stack.enter_context(handle)))
        try:
            total = sum(os.fstat(handle.fileno()).st_size for _, handle in opened)
        except OSError as exc:
            raise ValidationError(f"Cannot read attachments: {exc.strerror}") from exc
        if total > MAX_ATTACHMENT_BYTES:
            raise _too_large(total)
        total = 0
        for p, handle in opened:
            try:
                content = _read_all(handle)
            except OSError as exc:
                raise ValidationError(f"Cannot read {p}: {exc.strerror}") from exc
            # A file can grow between the size check and the read.
            total += len(content)
            if total > MAX_ATTACHMENT_BYTES:
                raise _too_large(total)
            check_no_secrets(content, str(p), secrets)
            files.append((field, (p.name, content)))
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


def load_json_file(
    path: str, *, forbidden: Iterable[Path] = (), secrets: Iterable[str] = ()
) -> Any:
    """Read and parse a UTF-8 JSON file through read_text_file; '-' is an ordinary name."""
    p = _expand(path)
    return parse_json(read_text_file(path, forbidden=forbidden, secrets=secrets), str(p))


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


def fold(value: Any) -> str | None:
    """Return a string stripped and NFKC-casefolded for caseless matching; else None.

    NFKC runs again after casefold, since casefold can emit non-NFKC sequences (e.g. U+0390).
    """
    if not isinstance(value, str):
        return None
    return unicodedata.normalize("NFKC", unicodedata.normalize("NFKC", value.strip()).casefold())


def filter_needles(filters: dict[str, str | None]) -> dict[str, str]:
    """Map each field to its folded filter text, dropping missing or whitespace-only texts."""
    return {field: fold(text) for field, text in filters.items() if nonblank_or_none(text)}


def filter_rows(
    body: Any, filters: dict[str, str | None], *, root_key: str = "data", paged: bool = False
) -> Any:
    """Keep the rows whose string field contains every active filter text, ignoring case.

    Takes a bare list or an envelope, whose other keys are kept; never mutates `body`. With no
    active filter returns `body`. A body without a row list raises HfoxError unless `paged`,
    which reads rows as client._unwrap_page does, so every --page-all format keeps the same rows.
    """
    needles = filter_needles(filters)
    if not needles:
        return body
    key, rows = root_key, body
    if isinstance(body, dict):
        # Same key rule as client._unwrap_page and render_list: root_key, then reports "rows".
        if body.get(root_key) is None and "rows" in body:
            key = "rows"
        rows = body.get(key)
        if paged and not isinstance(rows, list):
            rows = [] if rows is None else [rows]
    elif paged and not isinstance(body, list):
        rows = []
    if not isinstance(rows, list):
        raise HfoxError(
            f"Cannot filter by {', '.join(needles)}: the response holds no list of rows."
        )
    kept = [row for row in rows if _row_matches(row, needles)]
    return {**body, key: kept} if isinstance(body, dict) else kept


def _row_matches(row: Any, needles: dict[str, str]) -> bool:
    """True when `row` is a dict and each needled field is a string containing its needle."""
    if not isinstance(row, dict):
        return False
    for field, needle in needles.items():
        text = fold(row.get(field))
        if text is None or needle not in text:
            return False
    return True


# --page-all is a root flag, so appending it after the subcommand is a usage error.
PAGE_ALL_PLACEMENT = "--page-all before the resource (hfox --page-all <resource> ...)"


def filter_help(field: str, *, paged: bool = False) -> str:
    """Help text for a local filter flag on `field`; `paged` adds that it needs --page-all."""
    text = f"Only rows whose {field} contains this (case-insensitive, filtered locally)."
    if not paged:
        return text
    return f"{text} Requires the global {PAGE_ALL_PLACEMENT}."
