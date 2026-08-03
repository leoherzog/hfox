"""HTTP client for the HappyFox REST API (api/1.1/json).

Authentication is HTTP Basic with the API key as the username and the auth code
as the password (the only scheme HappyFox supports). The client adds:

  * exponential backoff with jitter on HTTP 429 and transient network errors
    (HappyFox enforces a global 500 GET / 300 POST per-minute limit and then
    returns 429 for a 10-minute cooldown); a server-supplied `Retry-After`
    header is honored up to that full 10-minute cooldown,
  * structured error raising (APIError / NotFoundError / AuthError),
  * a `paginate` helper that walks `page_info.page_count` and yields records.

It is deliberately UI-agnostic: no printing, no Typer. The CLI layer owns output.
"""

from __future__ import annotations

import time
import urllib.parse
from collections.abc import Iterator
from typing import Any

import httpx

from .errors import APIError, AuthError, HfoxError, NotFoundError, ValidationError

DEFAULT_TIMEOUT = 30.0
BASE_RETRY_DELAY = 1.0
MAX_RETRY_DELAY = 60.0
# HappyFox's documented 429 cooldown is 10 minutes; a server-supplied
# Retry-After may legitimately be that long, so clamp it separately.
MAX_RETRY_AFTER_DELAY = 600.0
MAX_RETRIES = 5

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
        # Percent-encode free-form identifiers in the path while preserving the
        # path separators and trailing slash, so segments containing #, ?, or
        # spaces are transmitted safely.
        encoded = urllib.parse.quote(path.lstrip("/"), safe="/")
        return f"{self.base_url}/{encoded}"

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
        """Send a request with retries, returning the parsed JSON body."""
        url = self._url(path)
        params = _clean_params(params)
        last_exc: Exception | None = None

        for attempt in range(self.max_retries + 1):
            try:
                response = self._client.request(
                    method.upper(),
                    url,
                    params=params,
                    json=json,
                    data=data,
                    files=files,
                )
            except httpx.RequestError as exc:
                last_exc = exc
                if attempt < self.max_retries:
                    self._sleep(self._backoff(attempt))
                    continue
                raise HfoxError(f"Network error contacting HappyFox: {exc}") from exc

            if response.status_code == 429 and attempt < self.max_retries:
                retry_after = response.headers.get("Retry-After")
                delay: float | None = None
                if retry_after:
                    try:
                        # Retry-After may be a (possibly fractional) seconds
                        # value; clamp to the documented 10-minute cooldown so
                        # a hostile/large header can't stall indefinitely.
                        delay = min(float(retry_after), MAX_RETRY_AFTER_DELAY)
                    except ValueError:
                        # Non-numeric (e.g. an HTTP-date): fall back to backoff.
                        delay = None
                self._sleep(delay if delay is not None else self._backoff(attempt))
                continue

            return self._handle_response(response)

        # Exhausted retries on network errors.
        raise HfoxError(f"Network error contacting HappyFox: {last_exc}")

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
                "Authentication failed — check your API key and auth code "
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
        size: int = 50,
    ) -> Iterator[Any]:
        """Yield records across pages until exhausted or page_limit is reached."""
        for body in self.paginate_pages(
            path,
            params=params,
            root_key=root_key,
            page_limit=page_limit,
            page_delay_ms=page_delay_ms,
            size=size,
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
        size: int = 50,
    ) -> Iterator[Any]:
        """Yield raw page bodies until exhausted or page_limit is reached.

        Works with HappyFox's `page_info.page_count` envelope as well as the
        reports module's top-level `page_count`/`rows` envelope.
        """
        params = dict(params or {})
        # HappyFox caps the page size at 50; clamp so we never request more.
        params.setdefault("size", min(size, 50))
        try:
            page = int(params.get("page", 1))
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"Invalid page number: {params.get('page')!r}") from exc
        pages_fetched = 0

        while pages_fetched < page_limit:
            params["page"] = page
            body = self.get(path, params=params)
            yield body
            _, page_count = _unwrap_page(body, root_key)
            pages_fetched += 1
            if page_count is None or page >= page_count:
                break
            page += 1
            if page_delay_ms:
                self._sleep(page_delay_ms / 1000.0)


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
