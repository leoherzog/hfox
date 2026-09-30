"""HTTP client for the HappyFox REST API: Basic auth, 429 and network retries,
structured errors, and pagination. UI-agnostic: no printing, no Typer.
"""

from __future__ import annotations

import math
import time
import urllib.parse
from collections.abc import Callable, Iterator
from typing import Any

import httpx

from .errors import APIError, AuthError, HfoxError, NotFoundError, ValidationError

DEFAULT_TIMEOUT = 30.0
BASE_RETRY_DELAY = 1.0
MAX_RETRY_DELAY = 60.0
# Matches HappyFox's documented 10-minute 429 cooldown.
MAX_RETRY_AFTER_DELAY = 600.0
MAX_RETRIES = 5
MAX_PAGE_SIZE = 50

# A write that failed with any other transport error may already have been applied.
_WRITE_RETRYABLE = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)

# Deterministic-ish jitter without importing random (keeps backoff testable).
_JITTER_CYCLE = (0.13, 0.41, 0.77, 0.29, 0.59)


class HappyFoxClient:
    """A thin, retrying wrapper over httpx for the HappyFox JSON API."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        auth_code: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = MAX_RETRIES,
        sleep=time.sleep,
    ):
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        self._sleep = sleep
        self._client = httpx.Client(
            auth=httpx.BasicAuth(api_key, auth_code),
            timeout=timeout,
            headers={"Accept": "application/json", "User-Agent": "hfox-cli"},
            # Do not follow redirects: httpx strips Basic auth on cross-host
            # redirects, which would silently send unauthenticated requests.
            follow_redirects=False,
        )

    # -- lifecycle ---------------------------------------------------------
    def __enter__(self) -> HappyFoxClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    # -- URL helpers -------------------------------------------------------
    def _url(self, path: str) -> str:
        return _join(self.base_url, path)

    def _backoff(self, attempt: int) -> float:
        delay = min(BASE_RETRY_DELAY * (2**attempt), MAX_RETRY_DELAY)
        return delay + _JITTER_CYCLE[attempt % len(_JITTER_CYCLE)]

    # -- core request ------------------------------------------------------
    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        data: dict[str, Any] | None = None,
        files: Any = None,
    ) -> Any:
        """Send a request with retries, returning the parsed JSON body.

        GETs retry any transport error; writes retry only failures to connect.
        """
        method = method.upper()
        url = self._url(path)
        params = _clean_params(params)
        retryable = httpx.RequestError if method == "GET" else _WRITE_RETRYABLE

        for attempt in range(self.max_retries + 1):
            try:
                response = self._client.request(
                    method, url, params=params, json=json, data=data, files=files
                )
            except httpx.RequestError as exc:
                if isinstance(exc, retryable) and attempt < self.max_retries:
                    self._sleep(self._backoff(attempt))
                    continue
                raise HfoxError(f"Network error contacting HappyFox: {exc}") from exc

            if response.status_code == 429 and attempt < self.max_retries:
                delay = _retry_after(response.headers.get("Retry-After"))
                self._sleep(delay if delay is not None else self._backoff(attempt))
                continue

            return self._handle_response(response)
        raise AssertionError("unreachable")  # pragma: no cover

    def _handle_response(self, response: httpx.Response) -> Any:
        if response.is_success:
            if not response.content:
                return None
            try:
                return response.json()
            except ValueError:
                return response.text

        status = response.status_code
        detail = _extract_error(response)
        if status in (401, 403):
            raise AuthError(
                "Authentication failed. Check your API key and auth code "
                "(run `hfox auth login`).",
                detail=detail,
            )
        if status == 404:
            raise NotFoundError("Resource not found.", detail=detail)
        if status == 429:
            raise APIError(
                "Rate limit exceeded (HTTP 429). HappyFox enforces a 10-minute "
                "cooldown after the limit is hit.",
                status_code=status,
                detail=detail,
            )
        raise APIError(
            f"HappyFox API returned HTTP {status}.", status_code=status, detail=detail
        )

    # -- verbs -------------------------------------------------------------
    def get(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        return self.request("GET", path, params=params)

    # -- pagination --------------------------------------------------------
    def paginate(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        root_key: str = "data",
        page_limit: int = 10,
        page_delay_ms: int = 100,
        size: int = MAX_PAGE_SIZE,
        on_truncated: Callable[[int, int], None] | None = None,
    ) -> Iterator[Any]:
        """Yield records across pages until exhausted or page_limit is reached."""
        for body in self.paginate_pages(
            path,
            params=params,
            root_key=root_key,
            page_limit=page_limit,
            page_delay_ms=page_delay_ms,
            size=size,
            on_truncated=on_truncated,
        ):
            records, _ = _unwrap_page(body, root_key)
            yield from records

    def paginate_pages(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        root_key: str = "data",
        page_limit: int = 10,
        page_delay_ms: int = 100,
        size: int = MAX_PAGE_SIZE,
        on_truncated: Callable[[int, int], None] | None = None,
    ) -> Iterator[Any]:
        """Yield raw page bodies until exhausted or page_limit is reached.

        Handles both the `page_info.page_count` and the top-level `page_count`
        envelopes. `on_truncated(last_page, page_count)` fires when page_limit
        stops the walk with pages remaining.
        """
        if page_limit < 1:
            raise ValidationError("page_limit must be at least 1.")
        params = first_page_params(params, size)
        page = params["page"]
        pages_fetched = 0

        while True:
            params["page"] = page
            body = self.get(path, params=params)
            yield body
            _, page_count = _unwrap_page(body, root_key)
            pages_fetched += 1
            if page_count is None or page >= page_count:
                return
            if pages_fetched >= page_limit:
                if on_truncated is not None:
                    on_truncated(page, page_count)
                return
            page += 1
            if page_delay_ms > 0:
                self._sleep(page_delay_ms / 1000.0)


def first_page_params(params: dict[str, Any] | None, size: int = MAX_PAGE_SIZE) -> dict[str, Any]:
    """Return the params of a page walk's first request: page defaults to 1, size is clamped."""
    out = dict(params or {})
    out["size"] = min(_int_param(out, "size", size), MAX_PAGE_SIZE)
    out["page"] = _int_param(out, "page", 1)
    return out


def request_url(base_url: str, path: str, params: dict[str, Any] | None = None) -> str:
    """Return the exact URL the client sends for this path and params."""
    return str(httpx.Request("GET", _join(base_url, path), params=_clean_params(params)).url)


def _join(base_url: str, path: str) -> str:
    """Join base_url and a relative path, percent-encoding each segment."""
    path = path.lstrip("/")
    if any(seg in (".", "..") for seg in path.split("/")):
        raise ValidationError(f"Invalid path {path!r}: '.' and '..' segments are not allowed.")
    # '@' stays literal: the docs show contact emails unescaped in paths.
    return f"{base_url.rstrip('/')}/{urllib.parse.quote(path, safe='/@')}"


def _retry_after(value: str | None) -> float | None:
    """Parse Retry-After seconds; None when absent, non-numeric, negative or non-finite."""
    try:
        delay = float(value or "")
    except ValueError:
        return None
    if not math.isfinite(delay) or delay < 0:
        return None
    return min(delay, MAX_RETRY_AFTER_DELAY)


def _int_param(params: dict[str, Any], key: str, default: int) -> int:
    value = params.get(key)
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"Invalid {key}: {value!r}") from exc


def _clean_params(params: dict[str, Any] | None) -> dict[str, Any] | None:
    """Drop None values and coerce bools to lowercase strings HappyFox expects."""
    if not params:
        return None
    cleaned: dict[str, Any] = {}
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, bool):
            cleaned[key] = "true" if value else "false"
        else:
            cleaned[key] = value
    return cleaned or None


def _unwrap_page(body: Any, root_key: str) -> tuple[list[Any], int | None]:
    """Return (records, page_count) from a paginated response body."""
    if isinstance(body, list):
        return body, None
    if isinstance(body, dict):
        records = body.get(root_key)
        if records is None and "rows" in body:  # reports envelope
            records = body.get("rows")
        if not isinstance(records, list):
            return ([] if records is None else [records]), None
        page_info = body.get("page_info") or {}
        page_count = page_info.get("page_count")
        if page_count is None:
            page_count = body.get("page_count")
        return records, page_count
    return [], None


def _extract_error(response: httpx.Response) -> Any:
    try:
        body = response.json()
    except ValueError:
        text = response.text.strip()
        return text[:500] if text else None
    if isinstance(body, dict) and "error" in body:
        return body["error"]
    return body
