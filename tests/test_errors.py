"""Error contract: type slugs, exit codes and the JSON shape of `to_dict`."""

import pytest

from hfox.core.errors import (
    APIError,
    AuthError,
    CancelledError,
    ExitCode,
    HfoxError,
    InternalError,
    NetworkError,
    NotFoundError,
    RateLimitError,
    RequestTimeoutError,
    UsageError,
    ValidationError,
)

HINT = "Check the resource."


def build(cls):
    if cls is APIError:
        return cls("boom", status_code=500)
    return cls("boom")


@pytest.mark.parametrize(
    ("cls", "slug", "code"),
    [
        (HfoxError, "other", ExitCode.OTHER),
        (InternalError, "internal", ExitCode.OTHER),
        (AuthError, "auth", ExitCode.AUTH),
        (ValidationError, "validation", ExitCode.VALIDATION),
        (UsageError, "usage", ExitCode.VALIDATION),
        (NotFoundError, "not_found", ExitCode.NOT_FOUND),
        (CancelledError, "cancelled", ExitCode.OTHER),
        (NetworkError, "network", ExitCode.OTHER),
        (RequestTimeoutError, "timeout", ExitCode.OTHER),
        (APIError, "api", ExitCode.API),
        (RateLimitError, "rate_limited", ExitCode.API),
    ],
)
def test_slug_and_exit_code_per_class(cls, slug, code):
    err = build(cls)
    assert cls.type == slug
    assert err.exit_code is code
    payload = err.to_dict()
    assert payload["type"] == slug
    assert payload["exit_code"] == int(code)
    assert payload["error"] == "boom"


def test_exit_code_values_are_frozen():
    assert [(c.name, c.value) for c in ExitCode] == [
        ("SUCCESS", 0), ("API", 1), ("AUTH", 2), ("VALIDATION", 3), ("NOT_FOUND", 4), ("OTHER", 5),
    ]


def test_class_hierarchy():
    assert issubclass(UsageError, ValidationError)
    assert issubclass(RequestTimeoutError, NetworkError)
    assert issubclass(RateLimitError, APIError)
    assert not issubclass(NetworkError, APIError)


def test_minimal_payload_has_three_keys():
    assert HfoxError("oops").to_dict() == {"error": "oops", "type": "other", "exit_code": 5}


def test_exit_code_override_keeps_the_slug():
    err = HfoxError("custom", exit_code=ExitCode.NOT_FOUND)
    assert err.to_dict() == {"error": "custom", "type": "other", "exit_code": 4}
    assert HfoxError.exit_code is ExitCode.OTHER


def test_to_dict_key_order():
    err = RateLimitError("slow", retry_after=30, detail={"raw": "x"})
    err.hint = HINT
    err.outcome_unknown = True
    assert list(err.to_dict()) == [
        "error", "type", "exit_code", "status_code", "detail", "hint", "retry_after",
        "outcome_unknown",
    ]


def test_api_error_key_order():
    err = APIError("fail", status_code=503, detail="x", hint=HINT, outcome_unknown=True)
    assert err.to_dict() == {
        "error": "fail",
        "type": "api",
        "exit_code": 1,
        "status_code": 503,
        "detail": "x",
        "hint": HINT,
        "outcome_unknown": True,
    }
    assert list(err.to_dict()) == [
        "error", "type", "exit_code", "status_code", "detail", "hint", "outcome_unknown",
    ]


def test_error_stays_a_string():
    assert isinstance(APIError("fail", status_code=500, detail={"a": 1}).to_dict()["error"], str)


def test_hint_only_when_set():
    assert "hint" not in ValidationError("bad").to_dict()
    err = ValidationError("bad", hint="Re-run with --yes.")
    assert err.hint == "Re-run with --yes."
    assert err.to_dict() == {
        "error": "bad", "type": "validation", "exit_code": 3, "hint": "Re-run with --yes.",
    }


def test_falsy_detail_is_kept():
    assert HfoxError("x", detail=[]).to_dict()["detail"] == []
    assert "detail" not in HfoxError("x").to_dict()


@pytest.mark.parametrize(
    ("value", "rendered"),
    [(600.0, 600), (600, 600), (0.0, 0), (2.5, 2.5)],
)
def test_retry_after_is_int_when_integral(value, rendered):
    payload = RateLimitError("slow", retry_after=value).to_dict()
    assert payload["retry_after"] == rendered
    assert type(payload["retry_after"]) is type(rendered)


def test_rate_limit_error_is_always_429():
    err = RateLimitError("slow")
    assert err.status_code == 429
    assert err.retry_after is None
    assert err.to_dict() == {
        "error": "slow", "type": "rate_limited", "exit_code": 1, "status_code": 429,
    }


def test_outcome_unknown_absent_when_false():
    assert HfoxError("x").outcome_unknown is False
    assert "outcome_unknown" not in HfoxError("x").to_dict()
    assert "outcome_unknown" not in APIError("x", status_code=500).to_dict()
    assert "outcome_unknown" not in NetworkError("x", outcome_unknown=False).to_dict()


def test_outcome_unknown_present_on_base_error():
    err = HfoxError("x", outcome_unknown=True)
    assert err.to_dict() == {
        "error": "x", "type": "other", "exit_code": 5, "outcome_unknown": True,
    }


def test_outcome_unknown_present_on_api_error():
    err = APIError("x", status_code=502, outcome_unknown=True)
    assert err.outcome_unknown is True
    assert err.to_dict()["outcome_unknown"] is True


def test_str_is_the_message():
    assert str(NetworkError("down", detail="ConnectError")) == "down"
