"""AppContext, stashed on `ctx.obj`: commands go through `call`, `paginate` and `render`
so --dry-run, --page-all and --format apply uniformly. It also owns staff resolution,
prompts and the guarded file and stdin readers.
"""

from __future__ import annotations

import contextlib
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import typer

from ..core.client import (
    DEFAULT_TIMEOUT,
    MAX_RETRIES,
    HappyFoxClient,
    first_page_params,
    request_url,
)
from ..core.config import Config, cleartext_host, guarded_dirs, guarded_secrets
from ..core.errors import CancelledError, HfoxError, InternalError, ValidationError
from . import _util, output
from ._util import PAGE_ALL_PLACEMENT, STDIN, filter_needles, filter_rows, fold
from .output import OutputFormat


@dataclass
class AppContext:
    config: Config
    fmt: OutputFormat = OutputFormat.JSON
    dry_run: bool = False
    page_all: bool = False
    page_limit: int = 10
    page_delay_ms: int = 100
    quiet: bool = False
    staff_id_override: int | None = None
    staff_override: str | None = None
    timeout: float | None = None
    max_retries: int | None = None
    _client: HappyFoxClient | None = field(default=None, repr=False)
    _staff_rows: list | None = field(default=None, repr=False)
    _stdin_flag: str | None = field(default=None, repr=False)
    _cleartext_warned: bool = field(default=False, repr=False)
    _guard_dirs: tuple[Path, ...] | None = field(default=None, repr=False)
    _guard_secrets: tuple[str, ...] | None = field(default=None, repr=False)

    # -- client ------------------------------------------------------------
    def make_client(self, base_url: str, api_key: str, auth_code: str) -> HappyFoxClient:
        """Build a client carrying --timeout, --max-retries and the retry notice.

        Warns once per invocation, even under --quiet, when `base_url` is http to a
        non-loopback host.
        """
        host = cleartext_host(base_url)
        if host is not None and not self._cleartext_warned:
            self._cleartext_warned = True
            output.warn(f"Sending credentials in cleartext to http://{host}; use https.")
        return HappyFoxClient(
            base_url,
            api_key,
            auth_code,
            timeout=self.timeout or DEFAULT_TIMEOUT,
            max_retries=MAX_RETRIES if self.max_retries is None else self.max_retries,
            on_retry=self._on_retry,
        )

    @property
    def client(self) -> HappyFoxClient:
        if self._client is None:
            self.config.require_auth()
            self._client = self.make_client(
                self.config.base_url,
                self.config.api_key,  # type: ignore[arg-type]
                self.config.auth_code,  # type: ignore[arg-type]
            )
        return self._client

    def close(self) -> None:
        """Close the HTTP client if one was created; safe to call twice."""
        if self._client is not None:
            self._client.close()
            self._client = None

    def _on_retry(self, reason: str, delay: float, attempt: int, max_retries: int) -> None:
        if not self.quiet:
            output.info(
                f"Retrying in {delay:.1f}s ({reason}; retry {attempt} of {max_retries})."
            )

    # -- staff resolution --------------------------------------------------
    def resolve_staff_id(self, staff_id: int | None = None, staff: str | None = None) -> int | None:
        """Pick the staff id: per-command flag > global flag > configured default.

        `staff` is an email or name resolved through `lookup_staff`; passing both raises
        ValidationError.
        """
        if staff_id is not None and staff is not None:
            raise ValidationError("--staff and --staff-id are mutually exclusive.")
        if staff_id is not None:
            return staff_id
        if staff is not None:
            return self.lookup_staff(staff)
        if self.staff_id_override is not None:
            return self.staff_id_override
        if self.staff_override is not None:
            return self.lookup_staff(self.staff_override)
        if self.config.staff_id_error is not None:
            raise self.config.staff_id_error
        return self.config.default_staff_id

    def require_staff_id(self, staff_id: int | None = None, staff: str | None = None) -> int:
        """Resolve like `resolve_staff_id`; raise ValidationError when no identity is set."""
        resolved = self.resolve_staff_id(staff_id, staff)
        if resolved is None:
            raise ValidationError(
                "This action needs a staff identity. Pass --staff or --staff-id, set a "
                "default during `hfox auth login`, or export HFOX_STAFF_ID."
            )
        return resolved

    def lookup_staff(self, text: str) -> int:
        """Return the id of the one staff member whose email, else name, equals `text` under `fold`.

        Reads GET staff/ once per invocation, also under --dry-run, so it needs credentials.
        Blank or digits-only text and zero or several matches raise ValidationError; `active`
        is not consulted.
        """
        if text.strip() == "":
            raise ValidationError("--staff must not be blank.")
        digits = text.strip()
        if digits.isascii() and digits.isdigit():
            raise ValidationError(
                f"--staff takes an email or name; use --staff-id {digits} for a numeric id."
            )
        if self._staff_rows is None:
            rows = self.client.get("staff/")
            if not isinstance(rows, list):
                raise HfoxError("Unexpected response from staff/; cannot resolve --staff.")
            self._staff_rows = rows
        needle = fold(text)
        rows = [row for row in self._staff_rows if isinstance(row, dict)]
        matches = [row for row in rows if fold(row.get("email")) == needle]
        if not matches:
            matches = [row for row in rows if fold(row.get("name")) == needle]
        if not matches:
            raise ValidationError(
                f"No staff member matches --staff {text!r}.",
                hint="List agents with `hfox system staff`.",
            )
        if len(matches) > 1:
            raise ValidationError(
                f"--staff {text!r} matches {len(matches)} staff members.",
                detail={"staff_ids": [row.get("id") for row in matches]},
                hint="Pass --staff-id instead.",
            )
        staff_id = matches[0].get("id")
        if not isinstance(staff_id, int) or isinstance(staff_id, bool) or staff_id < 0:
            raise HfoxError("Unexpected staff id in the staff/ response; cannot resolve --staff.")
        return staff_id

    # -- prompts -----------------------------------------------------------
    def confirm(self, message: str, *, yes: bool) -> None:
        """Ask before a destructive action; return when confirmed, `yes` or --dry-run.

        Raises ValidationError when stdin is not a terminal and CancelledError on a
        decline, Ctrl-C or EOF.
        """
        if yes or self.dry_run:
            return
        if not _util.stdin_is_tty():
            raise ValidationError(
                f"{message} Confirmation is required: pass --yes, since stdin is not a terminal.",
                hint="Re-run with --yes.",
            )
        try:
            # Click writes the prompt's trailing space to stdout, which would corrupt the data.
            with contextlib.redirect_stdout(sys.stderr):
                agreed = typer.confirm(output.sanitize_text(message), default=False, err=True)
        except typer.Abort:
            raise CancelledError("Cancelled at the confirmation prompt.") from None
        if not agreed:
            raise CancelledError("Cancelled: confirmation declined.")

    def prompt(self, text: str, *, default: str | None = None, hide_input: bool = False) -> str:
        """Prompt on stderr and return the answer. The caller checks for a terminal first."""
        text = output.sanitize_text(text)
        try:
            with contextlib.redirect_stdout(sys.stderr):
                return typer.prompt(text, default=default, hide_input=hide_input, err=True)
        except typer.Abort:
            raise CancelledError("Cancelled at the prompt.") from None

    # -- guarded input -----------------------------------------------------
    @property
    def _forbidden(self) -> tuple[Path, ...]:
        if self._guard_dirs is None:
            self._guard_dirs = guarded_dirs(self.config.dir)
        return self._guard_dirs

    @property
    def _secrets(self) -> tuple[str, ...]:
        if self._guard_secrets is None:
            self._guard_secrets = guarded_secrets(self.config)
        return self._guard_secrets

    def read_text(self, source: str, flag: str) -> str:
        """Return the content of a file, or of stdin when `source` is '-', less a leading BOM.

        Only one input per invocation may read stdin. Raises ValidationError for a file in
        a guarded config directory, content holding credentials or bytes that are not UTF-8.
        """
        if source != STDIN:
            return _util.read_text_file(source, forbidden=self._forbidden, secrets=self._secrets)
        if self._stdin_flag is not None:
            raise ValidationError(
                f"{flag} and {self._stdin_flag} both read stdin; only one input may use '-'."
            )
        self._stdin_flag = flag
        data = _read_stdin()
        _util.check_no_secrets(data, "stdin", self._secrets)
        try:
            return data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValidationError(f"{flag}: stdin is not UTF-8 (byte {exc.start}).") from exc

    def read_json(self, source: str, flag: str) -> Any:
        """Parse strict JSON from a file or stdin; see `read_text`."""
        text = self.read_text(source, flag)
        return _util.parse_json(text, "stdin" if source == STDIN else source)

    def text_input(self, inline: str | None, file: str | None, flag: str) -> str | None:
        """Return the body given inline or through `<flag>-file`; the two exclude each other.

        A file or stdin body that is empty or whitespace only raises ValidationError.
        """
        if file is None:
            return inline
        if inline is not None:
            raise ValidationError(f"{flag} and {flag}-file are mutually exclusive.")
        content = self.read_text(file, f"{flag}-file")
        if content.strip() == "":
            raise ValidationError(f"{flag}-file is empty; nothing to send.")
        return content

    def attach(
        self, body: dict[str, Any], attachments: list[str] | None, *, field: str = "attachments"
    ) -> dict[str, Any]:
        """Build `call` kwargs through `_util.attach` with the file guard and credential check.

        An attachment named '-' is an ordinary file; attachments never read stdin.
        """
        return _util.attach(
            body, attachments, field=field, forbidden=self._forbidden, secrets=self._secrets
        )

    # -- request execution -------------------------------------------------
    def call(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        data: dict[str, Any] | None = None,
        files: Any = None,
    ) -> Any:
        """Execute a request, honoring --dry-run."""
        if self.dry_run:
            emit_dry_run(
                self.config.base_url, method, path,
                params=params, json=json, data=data, files=files,
            )
            raise typer.Exit(0)
        return self.client.request(
            method, path, params=params, json=json, data=data, files=files
        )

    def paginate(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        root_key: str = "data",
        filters: dict[str, str | None] | None = None,
    ) -> Any:
        """Fetch a listing.

        --page-all in JSON streams one NDJSON page per line, then exits; in other
        formats it returns the flat record list, or the body of an only page that holds no
        row list, which `render_list` shows as it does without --page-all. Otherwise returns
        the single page, or raises HfoxError for an envelope the walk would refuse.
        `filters` keep matching rows on every page (see filter_rows) and require --page-all;
        a filtered walk always returns the list of matching rows.
        """
        filters = filters or {}
        needles = filter_needles(filters)
        if needles and not self.page_all:
            flags = ", ".join(f"--{field.replace('_', '-')}" for field in needles)
            raise ValidationError(
                f"{flags} filters locally and needs the global {PAGE_ALL_PLACEMENT} "
                "to search every page."
            )
        if self.dry_run:
            if self.page_all:
                params = first_page_params(params)
            emit_dry_run(self.config.base_url, "GET", path, params=params)
            raise typer.Exit(0)
        if not self.page_all:
            return self.client.get_page(path, params=params, root_key=root_key)
        walk = {
            "params": params,
            "root_key": root_key,
            "page_limit": self.page_limit,
            "page_delay_ms": self.page_delay_ms,
            "on_truncated": _warn_truncated,
        }
        if self.fmt is not OutputFormat.JSON:
            if not needles:
                return self.client.get_all(path, **walk)
            # A filter returns matching rows only, never a body without a row list as it is.
            return filter_rows(list(self.client.paginate(path, **walk)), filters)
        try:
            for page in self.client.paginate_pages(path, **walk):
                output.render_ndjson_line(
                    filter_rows(page, filters, root_key=root_key, paged=True)
                )
        except Exception as exc:
            # A multi-line error object would break line-oriented NDJSON readers.
            err = as_hfox_error(exc)
            output.render_ndjson_line(err.to_dict())
            raise typer.Exit(int(err.exit_code)) from None
        raise typer.Exit(0)

    # -- output ------------------------------------------------------------
    def render(self, data: Any) -> None:
        output.render(data, self.fmt)

    def render_list(self, body: Any, *, root_key: str = "data") -> None:
        """Render a listing: JSON keeps the {page_info, data} envelope; others render the rows."""
        if isinstance(body, list):
            self.render(body)
            return
        if self.fmt is OutputFormat.JSON:
            self.render(body)
            return
        records = body.get(root_key) if isinstance(body, dict) else None
        if records is None and isinstance(body, dict) and "rows" in body:
            records = body["rows"]
        self.render(records if records is not None else body)

    def success(self, message: str) -> None:
        if not self.quiet:
            output.info(message)


def emit_dry_run(
    base_url: str,
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    json: Any = None,
    data: dict[str, Any] | None = None,
    files: Any = None,
) -> None:
    """Print the request that would be sent, as JSON on stdout."""
    preview = {
        "dry_run": True,
        "method": method.upper(),
        "url": request_url(base_url, path, params),
        "params": params or None,
        "body": json if json is not None else (data or None),
        "attachments": _summarize_files(files),
    }
    output.render(preview, OutputFormat.JSON)


def as_hfox_error(exc: Exception) -> HfoxError:
    """Return exc if it is an HfoxError, else wrap it as an InternalError naming its type."""
    if isinstance(exc, HfoxError):
        return exc
    return InternalError(f"Unexpected error ({type(exc).__name__}): {exc}")


def _read_stdin() -> bytes:
    """Return all of stdin as bytes; empty when there is no stdin."""
    stream = sys.stdin
    if stream is None:
        return b""
    buffer = getattr(stream, "buffer", None)
    data = buffer.read() if buffer is not None else stream.read()
    # Surrogate escapes turn back into the original bytes, so the strict decode still fails.
    return data.encode("utf-8", "surrogateescape") if isinstance(data, str) else data


def _warn_truncated(last_page: int, page_count: int) -> None:
    output.warn(
        f"Stopped at page {last_page} of {page_count} (--page-limit); output is incomplete."
    )


def _summarize_files(files: Any) -> dict[str, Any] | None:
    """Describe a multipart `files` payload by field and filename, never the bytes."""
    if not files:
        return None
    items = files.items() if isinstance(files, dict) else files
    fields: list[dict[str, Any]] = []
    for entry in items:
        if isinstance(entry, (list, tuple)) and len(entry) == 2:
            field_name, value = entry
        else:
            field_name, value = entry, None
        filename = value[0] if isinstance(value, (list, tuple)) and value else None
        fields.append({"field": field_name, "filename": filename})
    return {"count": len(fields), "fields": fields}


def get_ctx(ctx: typer.Context) -> AppContext:
    """Fetch the AppContext from a Typer context."""
    obj = ctx.obj
    if not isinstance(obj, AppContext):  # pragma: no cover - defensive
        raise RuntimeError("AppContext not initialized.")
    return obj
