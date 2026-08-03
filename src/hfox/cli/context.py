"""AppContext — shared state and request execution for every command.

Stashed on the Typer Context (`ctx.obj`) by the root callback. Command modules
should go through `call`, `paginate`, and `render` rather than touching the
HTTP client directly, so global flags (--dry-run, --page-all, --format) are
honored uniformly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import typer

from ..core.client import HappyFoxClient
from ..core.config import Config
from ..core.errors import ValidationError
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
            self._emit_dry_run(method, path, params=params, json=json, data=data, files=files)
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

        With --page-all in JSON format, stream NDJSON — one compact page envelope
        per line as each page arrives (gws behavior) — then exit, like --dry-run.
        With --page-all in table/csv/yaml, walk every page (bounded by
        --page-limit) and return the full record list. Otherwise return the
        single-page response untouched so the caller can surface page_info.
        """
        if self.dry_run:
            self._emit_dry_run("GET", path, params=params)
            raise typer.Exit(0)
        if self.page_all:
            if self.fmt is OutputFormat.JSON:
                for page in self.client.paginate_pages(
                    path,
                    params=params,
                    root_key=root_key,
                    page_limit=self.page_limit,
                    page_delay_ms=self.page_delay_ms,
                ):
                    output.render_ndjson_line(page)
                raise typer.Exit(0)
            return list(
                self.client.paginate(
                    path,
                    params=params,
                    root_key=root_key,
                    page_limit=self.page_limit,
                    page_delay_ms=self.page_delay_ms,
                )
            )
        return self.client.get(path, params=params)

    # -- output ------------------------------------------------------------
    def render(self, data: Any) -> None:
        output.render(data, self.fmt)

    def render_list(self, body: Any, *, root_key: str = "data") -> None:
        """Render a listing: unwrap the {data: [...]} envelope for non-JSON formats.

        JSON keeps the full envelope (page_info + data) so nothing is lost; table
        and csv render just the rows, which is what a human wants to see.
        """
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

    def _emit_dry_run(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        data: dict[str, Any] | None = None,
        files: Any = None,
    ) -> None:
        preview = {
            "dry_run": True,
            "method": method.upper(),
            "url": f"{self.config.base_url}/{path.lstrip('/')}",
            "params": params or None,
            "body": json if json is not None else (data or None),
            "attachments": _summarize_files(files),
        }
        output.render(preview, OutputFormat.JSON)


def _summarize_files(files: Any) -> dict[str, Any] | None:
    """Describe a multipart `files` payload without dumping file bytes.

    httpx `files` is a list of (field_name, (filename, content[, mime])) tuples.
    Surface the count and the field names/filenames so a dry-run reflects that an
    upload would happen, but never serialize the raw bytes.
    """
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
