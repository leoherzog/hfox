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
UPDATE = ("tickets", "update", "5", "--status", "3")
UPDATE_CF = ("tickets", "update-cf", "5", "--cf", "1=x")
SUBSCRIBE = ("tickets", "subscribe", "5")
FORWARD = ("tickets", "forward", "5", "--to", "a@x.org", "--subject", "s", "--message", "m")
MOVE = ("tickets", "move", "5", "--to-category", "2")

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


# -- update-cf: JSON only, so a null is sent ----------------------------------
def test_update_cf_sends_a_null():
    p = preview("tickets", "update-cf", "5", "--cf-json", '{"4": null}')
    assert p["method"] == "POST"
    assert p["url"].endswith("/ticket/5/update_custom_fields/")
    assert p["body"] == {"staff": 1, "t-cf-4": None}
    assert p["attachments"] is None


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


# -- an empty value: the error names the JSON flag that sends it --------------
# (argv, the flag given an empty value, its JSON sibling, the body key that sibling writes)
EMPTY_VALUES = [
    (CREATE, "--cf", "--cf-json", "t-cf-4"),
    (CREATE, "--contact-cf", "--contact-cf-json", "c-cf-4"),
    (REPLY, "--cf", "--cf-json", "t-cf-4"),
    (REPLY, "--contact-cf", "--contact-cf-json", "ccf-4"),
    (NOTE, "--cf", "--cf-json", "t-cf-4"),
    (NOTE, "--contact-cf", "--contact-cf-json", "ccf-4"),
]


@pytest.mark.parametrize(
    ("argv", "flag", "json_flag", "key"),
    EMPTY_VALUES,
    ids=[f"{argv[1]} {flag}" for argv, flag, _, _ in EMPTY_VALUES],
)
def test_empty_custom_field_value_names_the_json_flag_that_sends_it(argv, flag, json_flag, key):
    result = run("--dry-run", *argv, flag, "4=")
    error = result.exception
    assert isinstance(error, ValidationError), error
    assert (error.type, error.exit_code) == ("validation", 3)
    assert result.stdout == ""
    assert f"use {json_flag} to send an empty value" in str(error)
    body = preview(*argv, json_flag, '{"4": ""}')["body"]
    assert {name: value for name, value in body.items() if "cf-" in name} == {key: ""}


# -- a broken JSON value: the error names the flag that carries it ------------
JSON_FLAGS = ("--cf-json", "--contact-cf-json")
BROKEN_JSON = {"malformed": "{bad", "not an object": "[1]"}


@pytest.mark.parametrize("argv", [CREATE, REPLY, NOTE], ids=["create", "reply", "note"])
@pytest.mark.parametrize("broken", JSON_FLAGS)
@pytest.mark.parametrize("value", BROKEN_JSON.values(), ids=BROKEN_JSON.keys())
def test_broken_custom_field_json_names_its_own_flag(argv, broken, value):
    (valid,) = set(JSON_FLAGS) - {broken}
    result = run("--dry-run", *argv, broken, value, valid, '{"4": "ok"}')
    error = result.exception
    assert isinstance(error, ValidationError), error
    assert (error.type, error.exit_code) == ("validation", 3)
    assert result.stdout == ""
    assert broken in str(error)
    assert valid not in str(error)


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
    (("subscribe", "5", "--agents", "x"), "integers"),
    ((*FORWARD[1:], "--ticket-attachments", "x"), "integers"),
]


@pytest.mark.parametrize(("argv", "named"), INVALID, ids=[argv[0] for argv, _ in INVALID])
def test_invalid_command_is_rejected_before_the_staff_lookup(mock_api, argv, named):
    captured = staff_api(mock_api)
    assert named in rejected("tickets", *argv, "--staff", "bob@x.org", env=NO_STAFF)
    assert captured == []


# -- optional one-line values: blank is rejected, outer whitespace is dropped --
# (argv that is complete without the flag, the flag, its body key, a value)
LINE_FLAGS = [
    (CREATE, "--phone", "phone", "555-0100"),
    (CREATE, "--created-at", "created_at", "2026-01-02T03:04:05"),
    (CREATE, "--due-date", "due_date", "2026-02-03"),
    (REPLY, "--due-date", "due_date", "2026-02-03"),
    (REPLY, "--subject", "subject", "Re: printer"),
    (NOTE, "--alert", "alert", "s"),
    (NOTE, "--due-date", "due_date", "2026-02-03"),
    (UPDATE, "--due-date", "due_date", "2026-02-03"),
]
LINE_IDS = [f"{argv[1]} {flag}" for argv, flag, _, _ in LINE_FLAGS]
ID_LISTS = [
    (SUBSCRIBE, "--agents", "data"),
    (FORWARD, "--ticket-attachments", "ticket_attachments"),
]
ID_LIST_IDS = [f"{argv[1]} {flag}" for argv, flag, _ in ID_LISTS]


@pytest.mark.parametrize("dry_run", [(), ("--dry-run",)], ids=["live", "dry-run"])
@pytest.mark.parametrize("blank", ["", " \t"], ids=["empty", "whitespace"])
@pytest.mark.parametrize(("argv", "flag", "key", "value"), LINE_FLAGS, ids=LINE_IDS)
def test_blank_one_line_value_is_rejected_before_the_staff_lookup(
    mock_api, argv, flag, key, value, blank, dry_run
):
    captured = staff_api(mock_api)
    staff = () if argv is CREATE else ("--staff", "bob@x.org")
    message = rejected(*dry_run, *argv, flag, blank, *staff, env=NO_STAFF)
    assert message == f"{flag} must not be blank."
    assert captured == []


@pytest.mark.parametrize(("argv", "flag", "key", "value"), LINE_FLAGS, ids=LINE_IDS)
def test_one_line_value_is_sent_without_outer_whitespace(argv, flag, key, value):
    assert preview(*argv, flag, f"  {value}\t")["body"][key] == value


def test_note_alert_id_is_checked_and_sent_without_outer_whitespace():
    assert preview(*NOTE, "--alert", " 7 ")["body"]["alert"] == "7"
    assert "at least 1" in rejected("--dry-run", *NOTE, "--alert", " 0 ")


@pytest.mark.parametrize("dry_run", [(), ("--dry-run",)], ids=["live", "dry-run"])
@pytest.mark.parametrize("blank", ["", "  ", " , "], ids=["empty", "whitespace", "commas"])
@pytest.mark.parametrize(("argv", "flag", "key"), ID_LISTS, ids=ID_LIST_IDS)
def test_blank_id_list_is_rejected_before_the_staff_lookup(
    mock_api, argv, flag, key, blank, dry_run
):
    captured = staff_api(mock_api)
    message = rejected(*dry_run, *argv, flag, blank, "--staff", "bob@x.org", env=NO_STAFF)
    assert message == f"{flag} needs at least one id."
    assert captured == []


@pytest.mark.parametrize(("argv", "flag", "key"), ID_LISTS, ids=ID_LIST_IDS)
def test_id_list_is_sent_as_numbers(argv, flag, key):
    assert preview(*argv, flag, " 11 , 012 ")["body"][key] == [11, 12]


@pytest.mark.parametrize("dry_run", [(), ("--dry-run",)], ids=["live", "dry-run"])
@pytest.mark.parametrize("blank", ["", " \t\n"], ids=["empty", "whitespace"])
def test_blank_move_note_is_rejected_before_the_staff_lookup(mock_api, blank, dry_run):
    captured = staff_api(mock_api)
    message = rejected(*dry_run, *MOVE, "--note", blank, "--staff", "bob@x.org", env=NO_STAFF)
    assert message == "--note must not be blank."
    assert captured == []


def test_move_note_is_sent_as_typed():
    assert preview(*MOVE)["body"] == {"staff_id": 1, "target_category_id": 2}
    assert preview(*MOVE, "--note", " moved\n")["body"]["move_note"] == " moved\n"


# -- custom-field ids: one field, however its id is spelled --------------------
# (argv that is complete without a custom field, flag, id as typed, body key)
PADDED_KEYS = [
    (CREATE, "--cf", "007", "t-cf-7"),
    (CREATE, "--cf", "t-cf-007", "t-cf-7"),
    (CREATE, "--cf", "c-cf-007", "c-cf-7"),
    (CREATE, "--contact-cf", "007", "c-cf-7"),
    (REPLY, "--cf", "ccf-007", "ccf-7"),
    (REPLY, "--contact-cf", "007", "ccf-7"),
    (NOTE, "--contact-cf", "ccf-007", "ccf-7"),
    (UPDATE, "--cf", "007", "t-cf-7"),
    (("tickets", "update-cf", "5"), "--cf", "t-cf-007", "t-cf-7"),
]


@pytest.mark.parametrize(
    ("argv", "flag", "typed", "key"),
    PADDED_KEYS,
    ids=[f"{argv[1]} {flag} {typed}" for argv, flag, typed, _ in PADDED_KEYS],
)
def test_custom_field_id_is_sent_without_leading_zeros(argv, flag, typed, key):
    for option in ((flag, f"{typed}=x"), (f"{flag}-json", json.dumps({typed: "x"}))):
        body = preview(*argv, *option)["body"]
        assert {name: value for name, value in body.items() if "cf-" in name} == {key: "x"}


@pytest.mark.parametrize(
    "options",
    [
        ("--cf", "7=a", "--cf", "7=b"),
        ("--cf", "7=a", "--cf", "007=b"),
        ("--cf", "007=a", "--cf", "t-cf-7=b"),
        ("--cf", "007=a", "--cf-json", '{"7": "b"}'),
        ("--cf", "7=z", "--cf-json", '{"7": "a", "t-cf-007": "b"}'),
    ],
    ids=["identical", "padded", "prefixed", "across flags", "within the JSON"],
)
def test_one_custom_field_given_twice_sends_the_last_value(options):
    p = preview("tickets", "update-cf", "5", *options)
    assert p["body"] == {"staff": 1, "t-cf-7": "b"}


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
