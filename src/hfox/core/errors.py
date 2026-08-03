"""Structured errors and stable, documented exit codes.

Mirrors the gws convention: every failure maps to a documented exit code so
scripts and AI agents can branch on it, and errors serialize to JSON so even
failures stay machine-parseable.
"""

from __future__ import annotations

from enum import IntEnum
from typing import Any


class ExitCode(IntEnum):
    """Stable exit codes. Keep these values frozen — they are a public contract."""

    SUCCESS = 0
    API = 1          # HappyFox returned an error response
    AUTH = 2         # missing / invalid credentials
    VALIDATION = 3   # bad arguments or input
    NOT_FOUND = 4    # resource not found
    OTHER = 5        # unexpected / internal


class HfoxError(Exception):
    """Base class for all hfox errors. Carries an exit code and optional detail."""

    exit_code: ExitCode = ExitCode.OTHER

    def __init__(self, message: str, *, detail: Any = None, exit_code: ExitCode | None = None):
        super().__init__(message)
        self.message = message
        self.detail = detail
        if exit_code is not None:
            self.exit_code = exit_code

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"error": self.message, "exit_code": int(self.exit_code)}
        if self.detail is not None:
            payload["detail"] = self.detail
        return payload


class AuthError(HfoxError):
    exit_code = ExitCode.AUTH


class ValidationError(HfoxError):
    exit_code = ExitCode.VALIDATION


class NotFoundError(HfoxError):
    exit_code = ExitCode.NOT_FOUND


class APIError(HfoxError):
    """An error response from the HappyFox API."""

    exit_code = ExitCode.API

    def __init__(self, message: str, *, status_code: int, detail: Any = None):
        super().__init__(message, detail=detail)
        self.status_code = status_code

    def to_dict(self) -> dict[str, Any]:
        payload = super().to_dict()
        payload["status_code"] = self.status_code
        return payload
