"""`tickets update`, `tickets set-cf-choices` and the staff flag pair on ticket verbs."""

import json
import sys

import httpx
import pytest
import typer
from typer.testing import CliRunner

from hfox.cli import main
from hfox.cli.main import cli
from hfox.core.errors import CancelledError, ValidationError

runner = CliRunner()

ENV = {
    "HFOX_SUBDOMAIN": "acme",
    "HFOX_REGION": "us",
    "HFOX_API_KEY": "k",
    "HFOX_AUTH_CODE": "c",
    "HFOX_STAFF_ID": "1",
}
NO_STAFF = {**ENV, "HFOX_STAFF_ID": None}

STAFF = [
    {"id": 1, "name": "Alice Smith", "email": "alice@x.org", "active": True},
    {"id": 2, "name": "Bob Jones", "email": "bob@x.org", "active": True},
]

# Each verb that takes the staff pair, its required arguments and the body key it fills.
STAFF_VERBS = [
    (["reply", "5", "--text", "x"], "staff"),
    (["note", "5", "--text", "x"], "staff"),
    (["update-cf", "5", "--cf", "1=x"], "staff"),
    (["update", "5", "--status", "3"], "staff"),
    (["tags", "5", "--add", "a"], "staff_id"),
    (["forward", "5", "--to", "a@x.org", "--subject", "s", "--message", "m"], "staff_id"),
    (["move", "5", "--to-category", "2"], "staff_id"),
    (["delete", "5", "--yes"], "staff_id"),
    (["subscribe", "5"], "staff_id"),
    (["unsubscribe", "5"], "staff_id"),
]
STAFF_IDS = [verb[0] for verb, _key in STAFF_VERBS]

CHOICES = [
    {"id": 11, "text": "Option 1", "dependant_fields": []},
    {"id": 12, "text": "Option 2 (Edited)", "dependant_fields": []},
    {"id": None, "text": "Option 4", "dependant_fields": []},
]
SUMMARY = (
    "Replace the choices of ticket custom field 7: 2 kept or edited by id, 1 new. "
    "Every other existing choice is deleted."
)


def run(*args, env=ENV, **kw):
    return runner.invoke(cli, list(args), env=env, **kw)


def preview(*args, **kw):
    result = run("--dry-run", *args, **kw)
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def rejected(*args, **kw):
    result = run(*args, **kw)
    assert isinstance(result.exception, ValidationError), result.exception
    assert result.stdout == ""
    return result.exception


def staff_api(mock_api, body=None):
    def handler(request):
        if request.url.path.endswith("/staff/"):
            return httpx.Response(200, json=STAFF)
        return httpx.Response(200, json=body if body is not None else {"ok": True})

    return mock_api(handler)


def run_main(monkeypatch, capsys, *argv, env=ENV):
    """Run main.app() in process; return (exit code, parsed stdout, stderr)."""
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(sys, "argv", ["hfox", *argv])
    with pytest.raises(SystemExit) as exc:
        main.app()
    streams = capsys.readouterr()
    return exc.value.code, json.loads(streams.out), streams.err


def no_prompt(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("prompted")

    monkeypatch.setattr(typer, "confirm", fail)


# -- tickets update -----------------------------------------------------------
def test_update_dry_run_method_url_and_body():
    p = preview(
        "tickets", "update", "42",
        "--status", "3", "--priority", "5", "--assignee", "4",
        "--due-date", "2026-12-01", "--tags", "vip, urgent", "--time-spent", "15",
        "--cf", "7=Urgent", "--cf-json", '{"8": [1, 2], "t-cf-9": null}',
    )
    assert p["method"] == "POST"
    assert p["url"].endswith("/api/1.1/json/ticket/42/staff_update/")
    assert p["params"] is None
    assert p["attachments"] is None
    assert p["body"] == {
        "staff": 1,
        "status": 3,
        "priority": 5,
        "assignee": 4,
        "time_spent": 15,
        "due_date": "2026-12-01",
        "tags": "vip,urgent",
        "t-cf-7": "Urgent",
        "t-cf-8": [1, 2],
        "t-cf-9": None,
    }


def test_update_documented_payload():
    p = preview(
        "tickets", "update", "42", "--priority", "5", "--assignee", "4", "--status", "3"
    )
    assert p["body"] == {"staff": 1, "status": 3, "priority": 5, "assignee": 4}


def test_update_posts_and_renders(mock_api):
    captured = mock_api(lambda req: httpx.Response(200, json={"id": 42, "status": 3}))
    result = run("tickets", "update", "42", "--status", "3", "--staff-id", "9")
    assert result.exit_code == 0, result.output
    assert captured[0].method == "POST"
    assert captured[0].url.path.endswith("/ticket/42/staff_update/")
    assert json.loads(captured[0].content) == {"staff": 9, "status": 3}
    assert json.loads(result.stdout) == {"id": 42, "status": 3}


@pytest.mark.parametrize("extra", [[], ["--tags", " , "], ["--cf-json", "{}"]])
def test_update_without_a_property_is_rejected_before_staff(extra):
    exc = rejected("--dry-run", "tickets", "update", "42", *extra, env=NO_STAFF)
    assert str(exc) == "Provide at least one property to change."


def test_update_needs_a_staff_identity():
    exc = rejected("--dry-run", "tickets", "update", "42", "--status", "3", env=NO_STAFF)
    assert "staff identity" in str(exc)


def test_update_unassign_sends_null():
    p = preview("tickets", "update", "42", "--unassign")
    assert p["body"] == {"staff": 1, "assignee": None}


def test_update_unassign_rejects_assignee():
    exc = rejected("--dry-run", "tickets", "update", "42", "--unassign", "--assignee", "4")
    assert "--assignee" in str(exc)


@pytest.mark.parametrize(
    "flags",
    [
        ["--cf", "ccf-3=x"],
        ["--cf", "c-cf-3=x"],
        ["--cf-json", '{"ccf-3": 1}'],
        ["--cf-json", '{"c-cf-3": 1}'],
    ],
)
def test_update_cf_accepts_only_the_ticket_prefix(flags):
    rejected("--dry-run", "tickets", "update", "42", *flags)


def test_update_cf_keeps_an_explicit_ticket_prefix():
    p = preview("tickets", "update", "42", "--cf", "t-cf-7=x")
    assert p["body"] == {"staff": 1, "t-cf-7": "x"}


def test_update_takes_no_message_flags():
    result = run("--dry-run", "tickets", "update", "42", "--text", "hi")
    assert result.exit_code == 2
    assert result.stdout == ""


# -- staff flags --------------------------------------------------------------
def test_update_staff_by_email(mock_api):
    captured = staff_api(mock_api)
    result = run("tickets", "update", "42", "--status", "3", "--staff", "BOB@x.org", env=NO_STAFF)
    assert result.exit_code == 0, result.output
    assert [(r.method, r.url.path.rsplit("/json/", 1)[1]) for r in captured] == [
        ("GET", "staff/"),
        ("POST", "ticket/42/staff_update/"),
    ]
    assert json.loads(captured[1].content) == {"staff": 2, "status": 3}


@pytest.mark.parametrize(("verb", "key"), STAFF_VERBS, ids=STAFF_IDS)
def test_staff_by_name_fills_the_body_under_dry_run(mock_api, verb, key):
    captured = staff_api(mock_api)
    p = preview("tickets", *verb, "--staff", "bob jones", env=NO_STAFF)
    assert p["body"][key] == 2
    assert [r.url.path.endswith("/staff/") for r in captured] == [True]


@pytest.mark.parametrize(("verb", "key"), STAFF_VERBS, ids=STAFF_IDS)
def test_staff_id_flag_fills_the_body(verb, key):
    p = preview("tickets", *verb, "--staff-id", "0")
    assert p["body"][key] == 0


@pytest.mark.parametrize(("verb", "key"), STAFF_VERBS, ids=STAFF_IDS)
def test_staff_and_staff_id_together_are_rejected(mock_api, verb, key):
    captured = staff_api(mock_api)
    exc = rejected("--dry-run", "tickets", *verb, "--staff", "bob@x.org", "--staff-id", "2")
    assert str(exc) == "--staff and --staff-id are mutually exclusive."
    assert captured == []


@pytest.mark.parametrize(("verb", "key"), STAFF_VERBS, ids=STAFF_IDS)
def test_numeric_staff_is_rejected(mock_api, verb, key):
    captured = staff_api(mock_api)
    exc = rejected("--dry-run", "tickets", *verb, "--staff", "12")
    assert "--staff-id 12" in str(exc)
    assert captured == []


@pytest.mark.parametrize(("verb", "key"), STAFF_VERBS, ids=STAFF_IDS)
def test_negative_staff_id_exits_3(monkeypatch, capsys, verb, key):
    code, payload, _err = run_main(
        monkeypatch, capsys, "--dry-run", "tickets", *verb, "--staff-id", "-1"
    )
    assert code == 3
    assert (payload["exit_code"], payload["type"]) == (3, "validation")
    assert "dry_run" not in payload


def test_unknown_staff_is_rejected_without_a_write(mock_api):
    captured = staff_api(mock_api)
    exc = rejected("tickets", "update", "42", "--status", "3", "--staff", "nobody@x.org")
    assert "No staff member matches" in str(exc)
    assert [r.method for r in captured] == ["GET"]


def test_subscribe_staff_names_the_agent_added(mock_api):
    captured = staff_api(mock_api)
    result = run("tickets", "subscribe", "42", "--staff", "alice@x.org", "--agents", "3,4")
    assert result.exit_code == 0, result.output
    assert json.loads(captured[1].content) == {"staff_id": 1, "data": [3, 4]}


def test_delete_resolves_staff_before_the_prompt(mock_api, tty):
    captured = staff_api(mock_api)
    result = run("tickets", "delete", "42", "--staff", "nobody@x.org", input="y\n")
    assert isinstance(result.exception, ValidationError), result.exception
    assert "Delete ticket" not in result.stderr
    assert [r.method for r in captured] == ["GET"]


# -- tickets set-cf-choices ---------------------------------------------------
def test_set_cf_choices_dry_run_from_inline_array():
    p = preview("tickets", "set-cf-choices", "7", "--choices-json", json.dumps(CHOICES))
    assert p["method"] == "PUT"
    assert p["url"].endswith("/api/1.1/json/ticket_custom_field/7/")
    assert p["params"] is None
    assert p["body"] == {"choices": CHOICES}


def test_set_cf_choices_dry_run_from_inline_object():
    raw = json.dumps({"choices": CHOICES})
    p = preview("tickets", "set-cf-choices", "7", "--choices-json", raw)
    assert p["method"] == "PUT"
    assert p["body"] == {"choices": CHOICES}


@pytest.mark.parametrize("wrap", [False, True], ids=["array", "object"])
def test_set_cf_choices_dry_run_from_file(tmp_path, wrap):
    f = tmp_path / "choices.json"
    f.write_text(json.dumps({"choices": CHOICES} if wrap else CHOICES))
    p = preview("tickets", "set-cf-choices", "7", "--file", str(f))
    assert p["method"] == "PUT"
    assert p["url"].endswith("/ticket_custom_field/7/")
    assert p["body"] == {"choices": CHOICES}


@pytest.mark.parametrize("wrap", [False, True], ids=["array", "object"])
def test_set_cf_choices_dry_run_from_stdin(wrap):
    raw = json.dumps({"choices": CHOICES} if wrap else CHOICES)
    p = preview("tickets", "set-cf-choices", "7", "--file", "-", input=raw)
    assert p["method"] == "PUT"
    assert p["url"].endswith("/ticket_custom_field/7/")
    assert p["body"] == {"choices": CHOICES}


def test_set_cf_choices_allows_an_empty_list():
    p = preview("tickets", "set-cf-choices", "7", "--choices-json", "[]")
    assert p["body"] == {"choices": []}


def test_set_cf_choices_needs_no_staff_identity():
    p = preview("tickets", "set-cf-choices", "7", "--choices-json", "[]", env=NO_STAFF)
    assert p["body"] == {"choices": []}


@pytest.mark.parametrize(
    ("choices", "word"),
    [
        ([{"text": "A"}], "no 'id'"),
        ([{"id": 0, "text": "A"}], "invalid 'id'"),
        ([{"id": -3, "text": "A"}], "invalid 'id'"),
        ([{"id": True, "text": "A"}], "invalid 'id'"),
        ([{"id": "11", "text": "A"}], "invalid 'id'"),
        ([{"id": 1.5, "text": "A"}], "invalid 'id'"),
        ([{"id": 11, "text": "A"}, {"id": 11, "text": "B"}], "more than once"),
        ([{"id": 11, "text": " "}], "'text'"),
        ([{"id": 11, "text": ""}], "'text'"),
        ([{"id": 11}], "'text'"),
        ([{"id": 11, "text": 5}], "'text'"),
        (["Option 1"], "JSON object"),
        ({"choices": "x"}, "JSON array"),
        ({"options": []}, "JSON array"),
        ("x", "JSON array"),
    ],
)
def test_set_cf_choices_rejects_bad_payloads(mock_api, choices, word):
    captured = mock_api(lambda req: httpx.Response(200, json={}))
    exc = rejected("tickets", "set-cf-choices", "7", "--yes", "--choices-json", json.dumps(choices))
    assert word in str(exc)
    assert captured == []


@pytest.mark.parametrize("choice", [{"text": "A"}, {"id": 0, "text": "A"}])
def test_set_cf_choices_id_errors_carry_the_hint(choice):
    exc = rejected(
        "--dry-run", "tickets", "set-cf-choices", "7", "--choices-json", json.dumps([choice])
    )
    assert exc.hint == (
        "Use the id from `hfox system ticket-custom-fields`, or null for a new choice."
    )


def test_set_cf_choices_two_new_choices_are_not_duplicates():
    choices = [{"id": None, "text": "A"}, {"id": None, "text": "B"}]
    p = preview("tickets", "set-cf-choices", "7", "--choices-json", json.dumps(choices))
    assert p["body"] == {"choices": choices}


def test_set_cf_choices_rejects_both_sources(tmp_path):
    f = tmp_path / "choices.json"
    f.write_text("[]")
    exc = rejected(
        "--dry-run", "tickets", "set-cf-choices", "7", "--file", str(f), "--choices-json", "[]"
    )
    assert "exactly one of --file or --choices-json" in str(exc)


def test_set_cf_choices_rejects_no_source():
    exc = rejected("--dry-run", "tickets", "set-cf-choices", "7")
    assert "exactly one of --file or --choices-json" in str(exc)


def test_set_cf_choices_rejects_bad_json():
    exc = rejected("--dry-run", "tickets", "set-cf-choices", "7", "--choices-json", "[")
    assert "Invalid JSON in --choices-json" in str(exc)


def test_set_cf_choices_file_in_config_dir_is_rejected(tmp_path):
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "payload.json").write_text("[]")
    exc = rejected(
        "--dry-run", "tickets", "set-cf-choices", "7", "--file", str(cfg / "payload.json")
    )
    assert "Refusing to read" in str(exc)


def test_set_cf_choices_field_id_zero_exits_3(monkeypatch, capsys):
    code, payload, _err = run_main(
        monkeypatch, capsys, "--dry-run", "tickets", "set-cf-choices", "0", "--choices-json", "[]"
    )
    assert code == 3
    assert payload["type"] == "validation"


def test_set_cf_choices_yes_warns_the_counts_and_puts(mock_api):
    captured = mock_api(lambda req: httpx.Response(200, json={"id": 7, "choices": CHOICES}))
    result = run("tickets", "set-cf-choices", "7", "-y", "--choices-json", json.dumps(CHOICES))
    assert result.exit_code == 0, result.output
    assert SUMMARY in result.stderr.replace("\n", " ")
    assert len(captured) == 1
    assert captured[0].method == "PUT"
    assert captured[0].url.path.endswith("/ticket_custom_field/7/")
    assert json.loads(captured[0].content) == {"choices": CHOICES}
    assert json.loads(result.stdout) == {"id": 7, "choices": CHOICES}


def test_set_cf_choices_prompt_shows_the_counts(mock_api, tty):
    captured = mock_api(lambda req: httpx.Response(200, json={"id": 7}))
    result = run(
        "tickets", "set-cf-choices", "7", "--choices-json", json.dumps(CHOICES), input="y\n"
    )
    assert result.exit_code == 0, result.output
    assert SUMMARY in result.stderr.replace("\n", " ")
    assert "2 kept or edited by id, 1 new" not in result.stdout
    assert json.loads(result.stdout) == {"id": 7}
    assert [r.method for r in captured] == ["PUT"]


def test_set_cf_choices_decline_sends_nothing(mock_api, tty):
    captured = mock_api(lambda req: httpx.Response(200, json={}))
    result = run(
        "tickets", "set-cf-choices", "7", "--choices-json", json.dumps(CHOICES), input="n\n"
    )
    assert isinstance(result.exception, CancelledError), result.exception
    assert result.stdout == ""
    assert captured == []


def test_set_cf_choices_without_terminal_exits_3(mock_api, monkeypatch, capsys):
    captured = mock_api(lambda req: httpx.Response(200, json={}))
    code, payload, _err = run_main(
        monkeypatch, capsys, "tickets", "set-cf-choices", "7", "--choices-json", json.dumps(CHOICES)
    )
    assert code == 3
    assert payload["type"] == "validation"
    assert "--yes" in payload["error"]
    assert "2 kept or edited by id, 1 new" in payload["error"]
    assert captured == []


@pytest.mark.parametrize("extra", [[], ["--yes"]])
def test_set_cf_choices_dry_run_never_prompts_or_warns(monkeypatch, extra):
    no_prompt(monkeypatch)
    result = run(
        "--dry-run", "tickets", "set-cf-choices", "7", *extra,
        "--choices-json", json.dumps(CHOICES),
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["method"] == "PUT"
    assert result.stderr == ""


def test_set_cf_choices_from_stdin_with_yes_puts(mock_api):
    captured = mock_api(lambda req: httpx.Response(200, json={"id": 7}))
    result = run(
        "tickets", "set-cf-choices", "7", "--yes", "--file", "-", input=json.dumps(CHOICES)
    )
    assert result.exit_code == 0, result.output
    assert json.loads(captured[0].content) == {"choices": CHOICES}
