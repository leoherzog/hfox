"""Structured errors: each carries a stable exit code and `type` slug and serializes to JSON
for scripts and agents to branch on.
"""

from __future__ import annotations

from enum import IntEnum
from typing import Any


class ExitCode(IntEnum):
    """Exit codes. The values are a public contract; do not renumber."""

    SUCCESS = 0
    API = 1          # HappyFox returned an error response
    AUTH = 2         # missing / invalid credentials
    VALIDATION = 3   # bad arguments or input
    NOT_FOUND = 4    # HTTP 404
    OTHER = 5        # anything else: network, timeout, config, cancelled, internal


class HfoxError(Exception):
    """Base class for all hfox errors. Carries an exit code, a type slug and optional detail."""

    exit_code: ExitCode = ExitCode.OTHER
    type: str = "other"

    def __init__(
        self,
        message: str,
        *,
        detail: Any = None,
        exit_code: ExitCode | None = None,
        hint: str | None = None,
        outcome_unknown: bool = False,
    ):
        super().__init__(message)
        self.message = message
        self.detail = detail
        self.hint = hint
        self.outcome_unknown = outcome_unknown
        if exit_code is not None:
            self.exit_code = exit_code

    def to_dict(self) -> dict[str, Any]:
        """Return the error JSON. Optional keys appear only when set, in a fixed order."""
        payload: dict[str, Any] = {
            "error": self.message,
            "type": self.type,
            "exit_code": int(self.exit_code),
        }
        status_code = getattr(self, "status_code", None)
        if status_code is not None:
            payload["status_code"] = status_code
        if self.detail is not None:
            payload["detail"] = self.detail
        if self.hint is not None:
            payload["hint"] = self.hint
        retry_after = getattr(self, "retry_after", None)
        if retry_after is not None:
            whole = float(retry_after).is_integer()
            payload["retry_after"] = int(retry_after) if whole else float(retry_after)
        if self.outcome_unknown:
            payload["outcome_unknown"] = True
        return payload


class AuthError(HfoxError):
    exit_code = ExitCode.AUTH
    type = "auth"


class ValidationError(HfoxError):
    """A value failed a range, type or content check."""

    exit_code = ExitCode.VALIDATION
    type = "validation"


class UsageError(ValidationError):
    """A structural command-line error: unknown command, unknown or misplaced flag, missing
    argument, flag-shaped value for a root option."""

    type = "usage"


class NotFoundError(HfoxError):
    exit_code = ExitCode.NOT_FOUND
    type = "not_found"


class CancelledError(HfoxError):
    """The user declined or interrupted a prompt."""

    exit_code = ExitCode.OTHER
    type = "cancelled"


class InternalError(HfoxError):
    """An unexpected exception from hfox itself."""

    exit_code = ExitCode.OTHER
    type = "internal"


class NetworkError(HfoxError):
    """A transport failure other than a timeout."""

    exit_code = ExitCode.OTHER
    type = "network"


class RequestTimeoutError(NetworkError):
    type = "timeout"


class APIError(HfoxError):
    """An error response from the HappyFox API."""

    exit_code = ExitCode.API
    type = "api"

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        detail: Any = None,
        hint: str | None = None,
        outcome_unknown: bool = False,
    ):
        super().__init__(message, detail=detail, hint=hint, outcome_unknown=outcome_unknown)
        self.status_code = status_code


class RateLimitError(APIError):
    """HTTP 429 after retries ran out. `retry_after` is the server's value in seconds, if any."""

    type = "rate_limited"

    def __init__(self, message: str, *, retry_after: float | None = None, detail: Any = None):
        super().__init__(message, status_code=429, detail=detail)
        self.retry_after = retry_after
