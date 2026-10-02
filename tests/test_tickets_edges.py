"""Ticket request bodies, the bulk cap, custom-field prefixes and the order of local checks."""

import json

import httpx
import pytest
from typer.testing import CliRunner

from hfox.cli.main import cli
from hfox.core.errors import ValidationError

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

CREATE = ("tickets", "create", "--subject", "S", "--category", "3", "--client", "5", "--text", "b")
REPLY = ("tickets", "reply", "5", "--text", "b")
NOTE = ("tickets", "note", "5", "--text", "b")
UPDATE_CF = ("tickets", "update-cf", "5", "--cf", "1=x")

CHOICES = [
    {"id": 11, "text": "Option 1", "dependant_fields": []},
    {"id": 12, "text": "Option 2 (Edited)", "dependant_fields": []},
    {"id": None, "text": "Option 4", "dependant_fields": []},
]
COUNTS = "2 kept or edited by id, 1 new"


def run(*args, env=ENV, **kw):
    return runner.invoke(cli, list(args), env=env, **kw)


def preview(*args, **kw):
    result = run("--dry-run", *args, **kw)
    assert result.exit_code == 0, result.exception or result.output
    return json.loads(result.stdout)


def rejected(*args, **kw):
    result = run(*args, **kw)
    assert isinstance(result.exception, ValidationError), result.exception
    assert result.stdout == ""
    return str(result.exception)


def staff_api(mock_api):
    def handler(request):
        if request.url.path.endswith("/staff/"):
            return httpx.Response(200, json=STAFF)
        return httpx.Response(200, json={"ok": True})

    return mock_api(handler)


def bulk(count):
    return [{"subject": f"T{i}", "category": 3, "client": 5, "text": "b"} for i in range(count)]


# -- create-bulk: the 100-entry cap -------------------------------------------
def test_create_bulk_sends_100_tickets():
    p = preview("tickets", "create-bulk", "--file", "-", input=json.dumps(bulk(100)))
    assert p["method"] == "POST"
    assert p["url"].endswith("/api/1.1/json/tickets/")
    assert p["body"] == bulk(100)


def test_create_bulk_rejects_101_tickets_before_any_request(mock_api):
    captured = mock_api(lambda req: httpx.Response(200, json=[]))
    payload = json.dumps(bulk(101))
    assert "1 and 100" in rejected("tickets", "create-bulk", "--file", "-", input=payload)
    assert captured == []


# -- create -------------------------------------------------------------------
def test_create_sends_every_optional_field():
    p = preview(
        "tickets", "create", "--subject", "S", "--category", "3",
        "--name", "Han", "--email", "h@x.org", "--phone", "555-0100",
        "--text", "plain", "--html", "<p>rich</p>",
        "--priority", "2", "--assignee", "4",
        "--tags", " vip , urgent ,", "--cc", " a@x.org , b@x.org", "--bcc", "c@x.org, ",
        "--created-at", "2026-01-02T03:04:05", "--due-date", "2026-02-03",
        "--visible-only-staff", "--contact-cf", "4=VIP",
    )
    assert p["method"] == "POST"
    assert p["url"].endswith("/api/1.1/json/tickets/")
    assert p["body"] == {
        "name": "Han",
        "email": "h@x.org",
        "phone": "555-0100",
        "subject": "S",
        "text": "plain",
        "html": "<p>rich</p>",
        "category": 3,
        "priority": 2,
        "assignee": 4,
        "tags": "vip,urgent",
        "cc": "a@x.org,b@x.org",
        "bcc": "c@x.org",
        "created_at": "2026-01-02T03:04:05",
        "due_date": "2026-02-03",
        "visible_only_staff": True,
        "c-cf-4": "VIP",
    }


@pytest.mark.parametrize(
    ("body", "rendered"), [({}, None), ({"json": [{"id": 7}]}, [{"id": 7}])], ids=["empty", "list"]
)
def test_create_renders_a_non_object_response_without_a_status_line(mock_api, body, rendered):
    captured = mock_api(lambda req: httpx.Response(200, **body))
    result = run(*CREATE)
    assert result.exit_code == 0, result.exception
    assert len(captured) == 1
    assert json.loads(result.stdout) == rendered
    assert result.stderr == ""


# -- reply / note -------------------------------------------------------------
PROPERTIES = [
    (["--priority", "2"], {"priority": 2}),
    (["--time-spent", "15"], {"time_spent": 15}),
    (["--due-date", "2026-12-01"], {"due_date": "2026-12-01"}),
    (["--tags", "vip, urgent"], {"tags": "vip,urgent"}),
    (["--cf", "ccf-4=VIP"], {"ccf-4": "VIP"}),
    (["--cf-json", '{"ccf-4": "VIP"}'], {"ccf-4": "VIP"}),
    (["--contact-cf", "4=VIP"], {"ccf-4": "VIP"}),
    (["--contact-cf-json", '{"4": "VIP", "ccf-5": null}'], {"ccf-4": "VIP", "ccf-5": None}),
]


@pytest.mark.parametrize(
    ("verb", "path"), [("reply", "staff_update"), ("note", "staff_pvtnote")], ids=["reply", "note"]
)
@pytest.mark.parametrize(("flags", "fields"), PROPERTIES, ids=[flags[0] for flags, _ in PROPERTIES])
def test_one_property_is_a_complete_update(verb, path, flags, fields):
    p = preview("tickets", verb, "5", *flags)
    assert p["method"] == "POST"
    assert p["url"].endswith(f"/ticket/5/{path}/")
    assert p["body"] == {"staff": 1, **fields}


def test_reply_sends_message_options():
    p = preview(
        *REPLY, "--subject", "Re: printer", "--cc", " a@x.org , b@x.org ", "--bcc", "c@x.org, ",
        "--update-customer", "--send-survey", "--last-staff-message", "77",
    )
    assert p["method"] == "POST"
    assert p["url"].endswith("/ticket/5/staff_update/")
    assert p["body"] == {
        "staff": 1,
        "plaintext": "b",
        "cc": "a@x.org,b@x.org",
        "bcc": "c@x.org",
        "subject": "Re: printer",
        "update_customer": True,
        "send_survey": True,
        "last_staff_message": 77,
    }


# -- custom-field keys: a flag takes only its own endpoint's prefixes ---------
STAFF_UPDATE_FOREIGN = [
    ("--cf", "c-cf-3"),
    ("--cf-json", "c-cf-3"),
    ("--contact-cf", "c-cf-3"),
    ("--contact-cf", "t-cf-3"),
    ("--contact-cf-json", "c-cf-3"),
    ("--contact-cf-json", "t-cf-3"),
]
FOREIGN_KEYS = [
    (CREATE, "--cf", "ccf-3"),
    (CREATE, "--cf-json", "ccf-3"),
    (CREATE, "--contact-cf", "ccf-3"),
    (CREATE, "--contact-cf", "t-cf-3"),
    (CREATE, "--contact-cf-json", "t-cf-3"),
    (UPDATE_CF, "--cf", "c-cf-3"),
    (UPDATE_CF, "--cf-json", "c-cf-3"),
    (UPDATE_CF, "--cf-json", "ccf-3"),
    *[(argv, flag, key) for argv in (REPLY, NOTE) for flag, key in STAFF_UPDATE_FOREIGN],
]


@pytest.mark.parametrize(
    ("argv", "flag", "key"),
    FOREIGN_KEYS,
    ids=[f"{argv[1]} {flag} {key}" for argv, flag, key in FOREIGN_KEYS],
)
def test_custom_field_key_with_a_foreign_prefix_is_rejected(argv, flag, key):
    value = json.dumps({key: 1}) if flag.endswith("-json") else f"{key}=x"
    assert f"'{key}'" in rejected("--dry-run", *argv, flag, value)


# -- local checks run before the staff lookup ---------------------------------
# (argv with a local fault, the flag or value the error names)
INVALID = [
    (("reply", "5"), "--text"),
    (("note", "5"), "--text"),
    (("update-cf", "5"), "--cf"),
    (("tags", "5"), "--add"),
    (("forward", "5", "--to", "a@x.org", "--subject", "s"), "--message"),
    (("unsubscribe", "DC5"), "DC5"),
    (("delete", "DC5", "--yes"), "DC5"),
]


@pytest.mark.parametrize(("argv", "named"), INVALID, ids=[argv[0] for argv, _ in INVALID])
def test_invalid_command_is_rejected_before_the_staff_lookup(mock_api, argv, named):
    captured = staff_api(mock_api)
    assert named in rejected("tickets", *argv, "--staff", "bob@x.org", env=NO_STAFF)
    assert captured == []


# -- tags / subscribe / user-reply / forward ----------------------------------
@pytest.mark.parametrize(
    ("flags", "fields"),
    [
        (["--add", " vip , urgent "], {"add": "vip,urgent"}),
        (["--remove", "old, stale"], {"remove": "old,stale"}),
    ],
    ids=["add", "remove"],
)
def test_tags_sends_only_the_list_given(flags, fields):
    p = preview("tickets", "tags", "5", *flags)
    assert p["method"] == "POST"
    assert p["url"].endswith("/ticket/5/update_tags/")
    assert p["body"] == {**fields, "staff_id": 1}


def test_subscribe_without_agents_omits_data():
    p = preview("tickets", "subscribe", "5")
    assert p["method"] == "POST"
    assert p["url"].endswith("/ticket/5/subscribe/")
    assert p["body"] == {"staff_id": 1}


def test_user_reply_joins_cc_and_bcc():
    p = preview(
        "tickets", "user-reply", "5", "--user", "9", "--text", "thanks",
        "--cc", " a@x.org , b@x.org", "--bcc", "c@x.org, ",
    )
    assert p["method"] == "POST"
    assert p["url"].endswith("/ticket/5/user_reply/")
    assert p["body"] == {"user": 9, "text": "thanks", "cc": "a@x.org,b@x.org", "bcc": "c@x.org"}


def test_forward_joins_address_lists_and_sends_switched_flags():
    p = preview(
        "tickets", "forward", "5", "--subject", "s", "--message", "m",
        "--to", " a@x.org , b@x.org ", "--cc", "c@x.org , d@x.org", "--bcc", "e@x.org, ",
        "--cc-include-contact", "--no-send-all-messages",
    )
    assert p["method"] == "POST"
    assert p["url"].endswith("/ticket/5/forward/")
    assert p["body"] == {
        "to": "a@x.org,b@x.org",
        "subject": "s",
        "message": "m",
        "cc": "c@x.org,d@x.org",
        "bcc": "e@x.org",
        "staff_id": 1,
        "cc_include_ticket_contact": True,
        "send_all_messages": False,
        "convert_replies_as_new_ticket": True,
    }


# -- inline-attachment / delete -----------------------------------------------
def test_inline_attachment_rejects_a_file_without_a_known_type(tmp_path):
    f = tmp_path / "shot"
    f.write_bytes(b"\x89PNG\r\n")
    assert "image" in rejected("--dry-run", "tickets", "inline-attachment", str(f))


def test_delete_takes_the_short_yes_flag(mock_api):
    captured = mock_api(lambda req: httpx.Response(200, json={"deleted_ticket": "#DC00000005"}))
    result = run("tickets", "delete", "5", "-y")
    assert result.exit_code == 0, result.output
    assert [r.method for r in captured] == ["POST"]
    assert captured[0].url.path.endswith("/ticket/5/delete/")
    assert json.loads(captured[0].content) == {"staff_id": 1}


# -- set-cf-choices -----------------------------------------------------------
@pytest.mark.parametrize(
    ("second", "named"),
    [
        ("Option 2", "Choice 2"),
        ({"id": 12, "text": " "}, "Choice 2"),
        ({"text": "B"}, "Choice 2"),
        ({"id": 0, "text": "B"}, "Choice 2"),
        ({"id": 11, "text": "B"}, "id 11"),
    ],
    ids=["not-object", "blank-text", "no-id", "invalid-id", "duplicate"],
)
def test_set_cf_choices_error_names_the_offending_choice(second, named):
    choices = [{"id": 11, "text": "A"}, second]
    assert named in rejected(
        "--dry-run", "tickets", "set-cf-choices", "7", "--choices-json", json.dumps(choices)
    )


def test_set_cf_choices_sends_only_the_choices_of_a_field_object():
    field = {"id": 7, "name": "Drop Down Field", "type": "choice", "choices": CHOICES}
    p = preview("tickets", "set-cf-choices", "7", "--choices-json", json.dumps(field))
    assert p["method"] == "PUT"
    assert p["url"].endswith("/ticket_custom_field/7/")
    assert p["body"] == {"choices": CHOICES}


def test_set_cf_choices_yes_summary_ignores_quiet(mock_api):
    captured = mock_api(lambda req: httpx.Response(200, json={"id": 7}))
    result = run(
        "--quiet", "tickets", "set-cf-choices", "7", "--yes", "--choices-json", json.dumps(CHOICES)
    )
    assert result.exit_code == 0, result.output
    assert COUNTS in result.stderr.replace("\n", " ")
    assert [r.method for r in captured] == ["PUT"]


def test_set_cf_choices_prompt_states_the_summary_once(mock_api, tty):
    captured = mock_api(lambda req: httpx.Response(200, json={"id": 7}))
    result = run(
        "tickets", "set-cf-choices", "7", "--choices-json", json.dumps(CHOICES), input="y\n"
    )
    assert result.exit_code == 0, result.output
    assert result.stderr.replace("\n", " ").count(COUNTS) == 1
    assert [r.method for r in captured] == ["PUT"]
