"""Staff identity resolution: --staff lookup through staff/, precedence and caching."""

import json

import httpx
import pytest
from typer.testing import CliRunner

from hfox.cli.context import AppContext
from hfox.cli.main import cli
from hfox.core.config import Config
from hfox.core.errors import AuthError, ExitCode, HfoxError, ValidationError

runner = CliRunner()

ENV = {
    "HFOX_SUBDOMAIN": "acme",
    "HFOX_REGION": "us",
    "HFOX_API_KEY": "k",
    "HFOX_AUTH_CODE": "c",
}

STAFF = [
    {"id": 1, "name": "Alice Smith", "email": "alice@x.org", "active": True},
    {"id": 2, "name": "Bob Jones", "email": "BOB@x.org", "active": True},
    # A name that equals another agent's email: the email match must win.
    {"id": 3, "name": "alice@x.org", "email": "third@x.org", "active": True},
    {"id": 4, "name": "Sam Twin", "email": "sam1@x.org", "active": True},
    {"id": 5, "name": "Sam Twin", "email": "sam2@x.org", "active": False},
    {"id": 0, "name": "Zero", "email": "zero@x.org", "active": True},
    {"id": 6, "name": "Straße Müller", "email": None, "active": True},
]


def staff_api(mock_api, rows=STAFF):
    def handler(request):
        if request.url.path.endswith("/staff/"):
            return httpx.Response(200, json=rows)
        return httpx.Response(200, json={"ok": True})

    return mock_api(handler)


def make_ctx(**kw):
    cfg = Config(
        subdomain="acme", api_key="k", auth_code="c",
        default_staff_id=kw.pop("default_staff_id", None),
    )
    return AppContext(config=cfg, **kw)


def run(*args, env=None):
    return runner.invoke(cli, list(args), env=env or ENV)


# -- lookup -------------------------------------------------------------------
def test_email_match_beats_a_name_match(mock_api):
    staff_api(mock_api)
    assert make_ctx().lookup_staff("alice@x.org") == 1


def test_name_match(mock_api):
    captured = staff_api(mock_api)
    assert make_ctx().lookup_staff("Bob Jones") == 2
    assert [(r.method, r.url.path) for r in captured] == [("GET", "/api/1.1/json/staff/")]
    assert captured[0].url.query == b""


@pytest.mark.parametrize(
    ("text", "staff_id"),
    [
        ("bob@X.ORG", 2),
        ("  ALICE SMITH  ", 1),
        ("STRASSE MÜLLER", 6),
        ("ｚｅｒｏ@x.org", 0),
    ],
)
def test_match_is_fold_insensitive(mock_api, text, staff_id):
    staff_api(mock_api)
    assert make_ctx().lookup_staff(text) == staff_id


def test_match_is_exact_not_substring(mock_api):
    staff_api(mock_api)
    with pytest.raises(ValidationError):
        make_ctx().lookup_staff("Alice")


def test_zero_matches(mock_api):
    staff_api(mock_api)
    with pytest.raises(ValidationError) as exc:
        make_ctx().lookup_staff("nobody@x.org")
    assert exc.value.to_dict() == {
        "error": "No staff member matches --staff 'nobody@x.org'.",
        "type": "validation",
        "exit_code": 3,
        "hint": "List agents with `hfox system staff`.",
    }


def test_two_matches_raise_even_when_one_is_inactive(mock_api):
    staff_api(mock_api)
    with pytest.raises(ValidationError) as exc:
        make_ctx().lookup_staff("sam twin")
    assert exc.value.to_dict() == {
        "error": "--staff 'sam twin' matches 2 staff members.",
        "type": "validation",
        "exit_code": 3,
        "detail": {"staff_ids": [4, 5]},
        "hint": "Pass --staff-id instead.",
    }


def test_two_email_matches_raise(mock_api):
    rows = [{"id": 1, "email": "dup@x.org"}, {"id": 2, "email": "DUP@x.org"}]
    staff_api(mock_api, rows)
    with pytest.raises(ValidationError) as exc:
        make_ctx().lookup_staff("dup@x.org")
    assert exc.value.detail == {"staff_ids": [1, 2]}


@pytest.mark.parametrize("text", ["7", "007", " 12 "])
def test_digits_are_rejected_without_a_request(mock_api, text):
    captured = staff_api(mock_api)
    with pytest.raises(ValidationError) as exc:
        make_ctx().lookup_staff(text)
    assert str(exc.value) == (
        f"--staff takes an email or name; use --staff-id {text.strip()} for a numeric id."
    )
    assert captured == []


@pytest.mark.parametrize("text", ["", "   "])
def test_blank_is_rejected_without_a_request(mock_api, text):
    captured = staff_api(mock_api)
    with pytest.raises(ValidationError, match="--staff must not be blank."):
        make_ctx().lookup_staff(text)
    assert captured == []


@pytest.mark.parametrize("body", [{"data": STAFF}, None, "text"])
def test_non_list_body_is_an_unexpected_response(mock_api, body):
    staff_api(mock_api, body)
    with pytest.raises(HfoxError) as exc:
        make_ctx().lookup_staff("alice@x.org")
    assert str(exc.value) == "Unexpected response from staff/; cannot resolve --staff."
    assert exc.value.exit_code is ExitCode.OTHER


@pytest.mark.parametrize("staff_id", [None, "7", True, -1, 1.5])
def test_matched_row_needs_a_usable_id(mock_api, staff_id):
    staff_api(mock_api, [{"id": staff_id, "name": "Odd", "email": "odd@x.org"}, "junk", None])
    with pytest.raises(HfoxError) as exc:
        make_ctx().lookup_staff("odd@x.org")
    assert exc.value.exit_code is ExitCode.OTHER
    assert not isinstance(exc.value, ValidationError)


def test_lookup_is_cached_to_one_get(mock_api):
    captured = staff_api(mock_api)
    ctx = make_ctx(staff_override="Bob Jones")
    assert ctx.require_staff_id(None, None) == 2
    assert ctx.require_staff_id(None, "alice@x.org") == 1
    assert ctx.resolve_staff_id(None, None) == 2
    assert len(captured) == 1


def test_lookup_needs_credentials_even_under_dry_run():
    ctx = AppContext(config=Config(subdomain="acme"), dry_run=True)
    with pytest.raises(AuthError):
        ctx.lookup_staff("alice@x.org")


# -- precedence ---------------------------------------------------------------
def test_both_per_command_flags_are_exclusive(mock_api):
    captured = staff_api(mock_api)
    for call in (make_ctx().resolve_staff_id, make_ctx().require_staff_id):
        with pytest.raises(ValidationError) as exc:
            call(1, "alice@x.org")
        assert str(exc.value) == "--staff and --staff-id are mutually exclusive."
    assert captured == []


def test_per_command_id_beats_both_global_flags(mock_api):
    captured = staff_api(mock_api)
    assert make_ctx(staff_id_override=9).resolve_staff_id(5, None) == 5
    assert make_ctx(staff_override="Bob Jones").resolve_staff_id(5, None) == 5
    assert captured == []


def test_per_command_staff_beats_both_global_flags(mock_api):
    staff_api(mock_api)
    assert make_ctx(staff_id_override=9).resolve_staff_id(None, "alice@x.org") == 1
    assert make_ctx(staff_override="Bob Jones").resolve_staff_id(None, "alice@x.org") == 1


def test_global_flags_beat_the_configured_default(mock_api):
    staff_api(mock_api)
    assert make_ctx(staff_id_override=9, default_staff_id=7).resolve_staff_id() == 9
    assert make_ctx(staff_override="Bob Jones", default_staff_id=7).resolve_staff_id() == 2
    assert make_ctx(default_staff_id=7).resolve_staff_id() == 7


def test_global_staff_spares_a_malformed_configured_id(mock_api):
    staff_api(mock_api)
    cfg = Config(
        subdomain="acme", api_key="k", auth_code="c",
        staff_id_error=ValidationError("HFOX_STAFF_ID must be a non-negative integer."),
    )
    assert AppContext(config=cfg, staff_override="Bob Jones").require_staff_id() == 2
    with pytest.raises(ValidationError, match="HFOX_STAFF_ID"):
        AppContext(config=cfg).require_staff_id()


# -- through the CLI ----------------------------------------------------------
def test_global_staff_is_resolved_lazily_into_the_body(mock_api):
    captured = staff_api(mock_api)
    result = run("--staff", "Alice Smith", "tickets", "reply", "5", "--text", "ok")
    assert result.exit_code == 0, result.output
    assert [(r.method, r.url.path.rsplit("/json", 1)[1]) for r in captured] == [
        ("GET", "/staff/"),
        ("POST", "/ticket/5/staff_update/"),
    ]
    assert json.loads(captured[1].content)["staff"] == 1


def test_lookup_is_performed_under_dry_run(mock_api):
    captured = staff_api(mock_api)
    result = run("--dry-run", "--staff", "bob@x.org", "tickets", "reply", "5", "--text", "ok")
    assert result.exit_code == 0, result.output
    assert [r.url.path.endswith("/staff/") for r in captured] == [True]
    preview = json.loads(result.stdout)
    assert preview["dry_run"] is True
    assert preview["body"]["staff"] == 2


def test_dry_run_lookup_without_credentials_is_an_auth_error(mock_api):
    captured = staff_api(mock_api)
    result = run(
        "--dry-run", "--staff", "bob@x.org", "tickets", "reply", "5", "--text", "ok",
        env={"HFOX_SUBDOMAIN": "acme"},
    )
    assert isinstance(result.exception, AuthError)
    assert result.stdout == ""
    assert captured == []


def test_failed_lookup_sends_no_write(mock_api):
    captured = staff_api(mock_api)
    result = run("--staff", "nobody", "tickets", "reply", "5", "--text", "ok")
    assert isinstance(result.exception, ValidationError)
    assert [r.method for r in captured] == ["GET"]


@pytest.mark.parametrize(
    "argv",
    [
        ("system", "statuses"),
        ("tickets", "get", "5"),
        ("--dry-run", "tickets", "list"),
        ("contacts", "get", "7"),
    ],
)
def test_commands_that_need_no_staff_never_call_staff(mock_api, argv):
    captured = staff_api(mock_api)
    result = run("--staff", "nobody at all", *argv)
    assert result.exit_code == 0, result.output
    assert not any(r.url.path.endswith("/staff/") for r in captured)


def test_both_global_flags_are_exclusive(mock_api):
    captured = staff_api(mock_api)
    result = run("--staff", "Bob Jones", "--staff-id", "2", "system", "statuses")
    assert isinstance(result.exception, ValidationError)
    assert str(result.exception) == "--staff and --staff-id are mutually exclusive."
    assert captured == []


def test_global_staff_id_must_not_be_negative():
    result = run("--staff-id", "-1", "system", "statuses")
    assert result.exit_code == 2
    assert "--staff-id" in result.output


def test_global_staff_id_zero_is_sent(mock_api):
    result = run("--dry-run", "--staff-id", "0", "tickets", "reply", "5", "--text", "ok")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["body"]["staff"] == 0
