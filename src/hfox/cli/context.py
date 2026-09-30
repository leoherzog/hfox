"""AppContext, stashed on `ctx.obj`: commands go through `call`, `paginate` and `render`
so --dry-run, --page-all and --format apply uniformly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import typer

from ..core.client import HappyFoxClient, first_page_params, request_url
from ..core.config import Config
from ..core.errors import HfoxError, ValidationError
from . import output
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
    _client: HappyFoxClient | None = field(default=None, repr=False)

    # -- client ------------------------------------------------------------
    @property
    def client(self) -> HappyFoxClient:
        if self._client is None:
            self.config.require_auth()
            self._client = HappyFoxClient(
                self.config.base_url,
                self.config.api_key,  # type: ignore[arg-type]
                self.config.auth_code,  # type: ignore[arg-type]
            )
        return self._client

    def close(self) -> None:
        """Close the underlying HTTP client if one was created.

        Safe to call when no client was ever built and safe to call twice.
        """
        if self._client is not None:
            self._client.close()
            self._client = None

    # -- staff id resolution ----------------------------------------------
    def resolve_staff_id(self, explicit: int | None) -> int | None:
        """Pick the staff id: explicit arg > --staff-id global > configured default."""
        if explicit is not None:
            return explicit
        if self.staff_id_override is not None:
            return self.staff_id_override
        if self.config.staff_id_error is not None:
            raise self.config.staff_id_error
        return self.config.default_staff_id

    def require_staff_id(self, explicit: int | None) -> int:
        staff_id = self.resolve_staff_id(explicit)
        if staff_id is None:
            raise ValidationError(
                "This action needs a staff id. Pass --staff-id, set a default "
                "during `hfox auth login`, or export HFOX_STAFF_ID."
            )
        return staff_id

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
    ) -> Any:
        """Fetch a listing.

        --page-all in JSON streams one NDJSON page per line, then exits; in other
        formats it returns the flat record list. Otherwise returns the single page.
        """
        if self.dry_run:
            if self.page_all:
                params = first_page_params(params)
            emit_dry_run(self.config.base_url, "GET", path, params=params)
            raise typer.Exit(0)
        if not self.page_all:
            return self.client.get(path, params=params)
        walk = {
            "params": params,
            "root_key": root_key,
            "page_limit": self.page_limit,
            "page_delay_ms": self.page_delay_ms,
            "on_truncated": _warn_truncated,
        }
        if self.fmt is not OutputFormat.JSON:
            return list(self.client.paginate(path, **walk))
        try:
            for page in self.client.paginate_pages(path, **walk):
                output.render_ndjson_line(page)
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
    """Return exc if it is an HfoxError, else wrap it as one naming the exception type."""
    if isinstance(exc, HfoxError):
        return exc
    return HfoxError(f"Unexpected error ({type(exc).__name__}): {exc}")


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
