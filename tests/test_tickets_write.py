"""Request shapes and validation for ticket write verbs."""

import json
import os
import sys

import httpx
import pytest
from typer.testing import CliRunner

from hfox.cli import main
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

CREATE = (
    "--dry-run", "tickets", "create", "--subject", "S", "--category", "3",
    "--client", "5", "--text", "body",
)

# Documented create-bulk response: one success and one validation failure.
BULK_PARTIAL = [
    {"display_id": "#DC00000011", "id": 11, "success": True},
    {
        "success": False,
        "error": [{"field": "category", "errors": ["This field is required."]}],
    },
]


def run(*args):
    return runner.invoke(cli, list(args), env=ENV)


def preview(*args):
    result = run(*args)
    assert result.exit_code == 0, result.stdout
    return json.loads(result.stdout)


def rejected(*args):
    result = run(*args)
    assert isinstance(result.exception, ValidationError), result.exception
    assert result.stdout == ""
    return str(result.exception)


# -- reply / note: property-only updates and --unassign ---------------------
def test_reply_property_only_update():
    p = preview(
        "--dry-run", "tickets", "reply", "5",
        "--status", "3", "--priority", "5", "--assignee", "4",
    )
    assert p["method"] == "POST"
    assert p["url"].endswith("/ticket/5/staff_update/")
    assert p["body"] == {"staff": 1, "status": 3, "priority": 5, "assignee": 4}


@pytest.mark.parametrize(("verb", "path"), [("reply", "staff_update"), ("note", "staff_pvtnote")])
def test_unassign_sends_null_assignee(verb, path):
    p = preview("--dry-run", "tickets", verb, "5", "--unassign")
    assert p["method"] == "POST"
    assert p["url"].endswith(f"/ticket/5/{path}/")
    assert p["body"] == {"staff": 1, "assignee": None}


@pytest.mark.parametrize("verb", ["reply", "note"])
def test_unassign_rejects_assignee(verb):
    assert "--assignee" in rejected(
        "--dry-run", "tickets", verb, "5", "--unassign", "--assignee", "4"
    )


@pytest.mark.parametrize("verb", ["reply", "note"])
def test_unassign_rejects_attachment(verb, tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("x")
    assert "--attachment" in rejected(
        "--dry-run", "tickets", verb, "5", "--text", "x", "--unassign", "--attachment", str(f)
    )


@pytest.mark.parametrize(("verb", "path"), [("reply", "staff_update"), ("note", "staff_pvtnote")])
def test_attachment_alone_is_an_update(verb, path, tmp_path):
    f = tmp_path / "x.png"
    f.write_bytes(b"\x89PNG")
    p = preview("--dry-run", "tickets", verb, "5", "--attachment", str(f))
    assert p["method"] == "POST"
    assert p["url"].endswith(f"/ticket/5/{path}/")
    assert p["body"] == {"staff": "1"}
    assert p["attachments"]["fields"] == [{"field": "attachments", "filename": "x.png"}]


@pytest.mark.parametrize("verb", ["reply", "note"])
def test_null_value_rejects_attachment(verb, tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("x")
    assert "--attachment" in rejected(
        "--dry-run", "tickets", verb, "5", "--cf-json", '{"3": null}', "--attachment", str(f)
    )


@pytest.mark.parametrize("verb", ["reply", "note"])
@pytest.mark.parametrize("extra", [[], ["--tags", " , "], ["--cf-json", "{}"]])
def test_update_without_body_or_property_is_rejected(verb, extra):
    assert "property" in rejected("--dry-run", "tickets", verb, "5", *extra)


@pytest.mark.parametrize(("verb", "path"), [("reply", "staff_update"), ("note", "staff_pvtnote")])
@pytest.mark.parametrize("flag", ["--html", "--text"])
def test_blank_body_is_dropped_from_property_update(verb, path, flag):
    p = preview("--dry-run", "tickets", verb, "5", flag, " ", "--status", "3")
    assert p["method"] == "POST"
    assert p["url"].endswith(f"/ticket/5/{path}/")
    assert p["body"] == {"staff": 1, "status": 3}


def test_reply_cc_alone_is_rejected():
    rejected("--dry-run", "tickets", "reply", "5", "--cc", "a@x.org")


def test_note_custom_field_alone_is_a_property():
    p = preview("--dry-run", "tickets", "note", "5", "--cf-json", '{"10": null}')
    assert p["method"] == "POST"
    assert p["url"].endswith("/ticket/5/staff_pvtnote/")
    assert p["body"] == {"staff": 1, "t-cf-10": None}


@pytest.mark.parametrize("verb", ["reply", "note"])
def test_blank_tags_are_omitted(verb):
    p = preview("--dry-run", "tickets", verb, "5", "--text", "hi", "--tags", " , ")
    assert p["method"] == "POST"
    assert p["body"] == {"staff": 1, "plaintext": "hi"}


# -- create -----------------------------------------------------------------
def test_create_contact_cf_json_uses_c_cf_prefix():
    p = preview(*CREATE, "--contact-cf-json", '{"3": "Acme, Inc.", "c-cf-4": [1], "5": null}')
    assert p["method"] == "POST"
    assert p["url"].endswith("/tickets/")
    assert p["body"]["c-cf-3"] == "Acme, Inc."
    assert p["body"]["c-cf-4"] == [1]
    assert p["body"]["c-cf-5"] is None
    assert not any(k.startswith("c-cf-c-cf-") for k in p["body"])


def test_create_contact_cf_json_rejects_other_prefix():
    rejected(*CREATE, "--contact-cf-json", '{"ccf-3": 1}')


@pytest.mark.parametrize(
    ("flags", "word"),
    [
        (["--client", "5", "--text", "   "], "body"),
        (["--client", "5", "--html", " "], "body"),
        (["--name", " ", "--email", " ", "--text", "t"], "contact"),
    ],
)
def test_create_blank_body_or_contact_is_rejected(flags, word):
    args = ("--dry-run", "tickets", "create", "--subject", "S", "--category", "3", *flags)
    assert word in rejected(*args)


def test_create_blank_subject_is_rejected():
    assert "--subject" in rejected(
        "--dry-run", "tickets", "create", "--subject", "  ", "--category", "3",
        "--client", "5", "--text", "body",
    )


# -- create-bulk: partial failure -------------------------------------------
def test_create_bulk_partial_failure_exits_1(mock_api, tmp_path):
    f = tmp_path / "bulk.json"
    f.write_text(json.dumps([{"subject": "a"}, {"subject": "b"}]))
    mock_api(lambda req: httpx.Response(200, json=BULK_PARTIAL))
    result = run("tickets", "create-bulk", "--file", str(f))
    assert result.exit_code == 1
    assert json.loads(result.stdout) == BULK_PARTIAL
    assert "1 of 2 entries failed" in result.stderr


def test_create_bulk_partial_failure_exits_1_through_app(mock_api, tmp_path, monkeypatch, capsys):
    f = tmp_path / "bulk.json"
    f.write_text(json.dumps([{"subject": "a"}, {"subject": "b"}]))
    mock_api(lambda req: httpx.Response(200, json=BULK_PARTIAL))
    monkeypatch.setattr(sys, "argv", ["hfox", "tickets", "create-bulk", "--file", str(f)])
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(SystemExit) as exc:
        main.app()
    assert exc.value.code == 1
    assert json.loads(capsys.readouterr().out) == BULK_PARTIAL


def test_create_bulk_all_success_exits_0(mock_api, tmp_path):
    f = tmp_path / "bulk.json"
    f.write_text(json.dumps([{"subject": "a"}]))
    mock_api(lambda req: httpx.Response(200, json=BULK_PARTIAL[:1]))
    result = run("tickets", "create-bulk", "--file", str(f))
    assert result.exit_code == 0
    assert result.stderr == ""


# -- tags / delete ----------------------------------------------------------
@pytest.mark.parametrize("flags", [["--add", ","], ["--add", " , "], ["--remove", ","]])
def test_tags_blank_list_is_rejected(flags):
    rejected("--dry-run", "tickets", "tags", "5", *flags)


def test_delete_checks_staff_id_before_prompt():
    result = runner.invoke(
        cli,
        ["--dry-run", "tickets", "delete", "42"],
        input="y\n",
        env={**ENV, "HFOX_STAFF_ID": None},
    )
    assert isinstance(result.exception, ValidationError), result.exception
    assert "Delete ticket" not in result.output


# -- forward / user-reply: blank required values ----------------------------
@pytest.mark.parametrize("extra", [[], ["--to-include-contact"]])
def test_forward_needs_to_addresses(extra):
    assert "--to" in rejected(
        "--dry-run", "tickets", "forward", "5", "--to", " , ", *extra,
        "--subject", "s", "--message", "m",
    )


def test_forward_requires_to_option():
    result = run(
        "--dry-run", "tickets", "forward", "5",
        "--to-include-contact", "--subject", "s", "--message", "m",
    )
    assert result.exit_code != 0
    assert "--to" in result.output


@pytest.mark.parametrize(
    ("flags", "flag"),
    [
        (["--subject", " ", "--message", "m"], "--subject"),
        (["--subject", "s", "--message", ""], "--message"),
    ],
)
def test_forward_blank_subject_or_message_is_rejected(flags, flag):
    assert flag in rejected("--dry-run", "tickets", "forward", "5", "--to", "a@x.org", *flags)


def test_user_reply_blank_text_is_rejected():
    assert "--text" in rejected(
        "--dry-run", "tickets", "user-reply", "5", "--user", "9", "--text", " \t"
    )


# -- inline-attachment --------------------------------------------------------
def test_inline_attachment_posts_single_file_field(tmp_path):
    f = tmp_path / "shot.png"
    f.write_bytes(b"\x89PNG\r\n")
    p = preview("--dry-run", "tickets", "inline-attachment", str(f))
    assert p["method"] == "POST"
    assert p["url"].endswith("/api/1.1/json/ticket-inline-attachment")
    assert p["body"] is None
    assert p["attachments"] == {"count": 1, "fields": [{"field": "file", "filename": "shot.png"}]}


def test_inline_attachment_returns_url(mock_api, tmp_path):
    f = tmp_path / "shot.png"
    f.write_bytes(b"\x89PNG\r\n")
    url = "https://acme.happyfox.com/get_hdp_temporarily_attachment/2091/"
    captured = mock_api(lambda req: httpx.Response(200, json={"url": url}))
    result = run("tickets", "inline-attachment", str(f))
    assert result.exit_code == 0
    req = captured[0]
    assert req.method == "POST"
    assert req.url.path == "/api/1.1/json/ticket-inline-attachment"
    assert req.headers["content-type"].startswith("multipart/form-data")
    assert b'name="file"; filename="shot.png"' in req.content
    assert json.loads(result.stdout) == {"url": url}


def test_inline_attachment_rejects_non_image(tmp_path):
    f = tmp_path / "notes.txt"
    f.write_text("x")
    assert "image" in rejected("--dry-run", "tickets", "inline-attachment", str(f))


def test_inline_attachment_rejects_over_25mb(tmp_path):
    f = tmp_path / "big.png"
    f.write_bytes(b"")
    os.truncate(f, 25_000_001)
    assert "25,000,000" in rejected("--dry-run", "tickets", "inline-attachment", str(f))


def test_inline_attachment_rejects_missing_file(tmp_path):
    rejected("--dry-run", "tickets", "inline-attachment", str(tmp_path / "gone.png"))


@pytest.mark.parametrize("verb", ["create", "reply", "note", "update-cf"])
def test_cf_help_shows_list_syntax(verb):
    result = run("tickets", verb, "--help")
    assert "'<id>=[a,b]'" in result.stdout


# -- create --unassign --------------------------------------------------------
def test_create_unassign_sends_null_assignee():
    p = preview(*CREATE, "--unassign")
    assert p["method"] == "POST"
    assert p["url"].endswith("/tickets/")
    assert p["body"] == {
        "client": 5, "subject": "S", "text": "body", "category": 3, "assignee": None,
    }


def test_create_omits_assignee_by_default():
    assert "assignee" not in preview(*CREATE)["body"]


def test_create_unassign_rejects_assignee():
    assert "--assignee" in rejected(*CREATE, "--unassign", "--assignee", "4")


def test_create_unassign_rejects_attachment(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("x")
    assert "--attachment" in rejected(*CREATE, "--unassign", "--attachment", str(f))


# -- bodies from a file or stdin ----------------------------------------------
CREATE_NO_BODY = (
    "tickets", "create", "--subject", "S", "--category", "3", "--client", "5",
)
FORWARD_NO_BODY = ("tickets", "forward", "5", "--to", "a@x.org", "--subject", "s")

# (argv without a body, file flag, inline flag, body key)
BODY_FLAGS = [
    (CREATE_NO_BODY, "--text-file", "--text", "text"),
    (CREATE_NO_BODY, "--html-file", "--html", "html"),
    (("tickets", "reply", "5"), "--text-file", "--text", "plaintext"),
    (("tickets", "reply", "5"), "--html-file", "--html", "html"),
    (("tickets", "note", "5"), "--text-file", "--text", "plaintext"),
    (("tickets", "note", "5"), "--html-file", "--html", "html"),
    (("tickets", "user-reply", "5", "--user", "9"), "--text-file", "--text", "text"),
    (FORWARD_NO_BODY, "--message-file", "--message", "message"),
    (("tickets", "move", "5", "--to-category", "2"), "--note-file", "--note", "move_note"),
]
BODY_IDS = [f"{argv[1]} {flag}" for argv, flag, _inline, _key in BODY_FLAGS]

# One file flag per command that must not send an empty body.
EMPTY_BODY = [
    (("tickets", "reply", "5"), "--text-file"),
    (("tickets", "note", "5"), "--html-file"),
    (CREATE_NO_BODY, "--text-file"),
    (("tickets", "user-reply", "5", "--user", "9"), "--text-file"),
    (FORWARD_NO_BODY, "--message-file"),
]
EMPTY_IDS = [argv[1] for argv, _flag in EMPTY_BODY]


@pytest.mark.parametrize(("argv", "flag", "inline", "key"), BODY_FLAGS, ids=BODY_IDS)
def test_body_from_file_is_sent_unmodified(argv, flag, inline, key, tmp_path):
    f = tmp_path / "body.txt"
    f.write_bytes("línea uno\nline two\n".encode())
    p = preview("--dry-run", *argv, flag, str(f))
    assert p["method"] == "POST"
    assert p["body"][key] == "línea uno\nline two\n"


@pytest.mark.parametrize(("argv", "flag", "inline", "key"), BODY_FLAGS, ids=BODY_IDS)
def test_body_from_stdin(argv, flag, inline, key):
    result = runner.invoke(cli, ["--dry-run", *argv, flag, "-"], input="from stdin\n", env=ENV)
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["body"][key] == "from stdin\n"


@pytest.mark.parametrize(("argv", "flag", "inline", "key"), BODY_FLAGS, ids=BODY_IDS)
def test_inline_and_file_body_are_mutually_exclusive(argv, flag, inline, key, tmp_path):
    f = tmp_path / "body.txt"
    f.write_text("from file")
    message = rejected("--dry-run", *argv, inline, "inline", flag, str(f))
    assert message == f"{inline} and {flag} are mutually exclusive."


@pytest.mark.parametrize(("argv", "flag"), EMPTY_BODY, ids=EMPTY_IDS)
@pytest.mark.parametrize("content", ["", " \n\t\n"])
def test_empty_body_file_sends_nothing(mock_api, argv, flag, content, tmp_path):
    captured = mock_api(lambda req: httpx.Response(200, json={}))
    f = tmp_path / "body.txt"
    f.write_text(content)
    assert rejected(*argv, flag, str(f)) == f"{flag} is empty; nothing to send."
    assert captured == []


@pytest.mark.parametrize(("argv", "flag"), EMPTY_BODY, ids=EMPTY_IDS)
def test_empty_stdin_body_sends_nothing(mock_api, argv, flag):
    captured = mock_api(lambda req: httpx.Response(200, json={}))
    result = runner.invoke(cli, [*argv, flag, "-"], input="", env=ENV)
    assert isinstance(result.exception, ValidationError), result.exception
    assert str(result.exception) == f"{flag} is empty; nothing to send."
    assert result.stdout == ""
    assert captured == []


def test_empty_file_is_rejected_even_with_a_property(tmp_path):
    f = tmp_path / "body.txt"
    f.write_text("")
    assert "--text-file is empty" in rejected(
        "--dry-run", "tickets", "reply", "5", "--status", "3", "--text-file", str(f)
    )


@pytest.mark.parametrize("verb", ["reply", "note"])
def test_two_stdin_inputs_are_rejected(mock_api, verb):
    captured = mock_api(lambda req: httpx.Response(200, json={}))
    result = runner.invoke(
        cli,
        ["tickets", verb, "5", "--html-file", "-", "--text-file", "-"],
        input="body\n",
        env=ENV,
    )
    assert isinstance(result.exception, ValidationError), result.exception
    assert "both read stdin" in str(result.exception)
    assert "--text-file" in str(result.exception) and "--html-file" in str(result.exception)
    assert captured == []


def test_body_file_is_sent_on_the_wire(mock_api, tmp_path):
    f = tmp_path / "body.html"
    f.write_bytes(b"<p>hi</p>\n")
    captured = mock_api(lambda req: httpx.Response(200, json={"id": 1}))
    result = run("tickets", "reply", "5", "--html-file", str(f))
    assert result.exit_code == 0, result.output
    assert json.loads(captured[0].content) == {"staff": 1, "html": "<p>hi</p>\n"}


def test_user_reply_needs_a_body():
    assert "--text-file" in rejected("--dry-run", "tickets", "user-reply", "5", "--user", "9")


def test_forward_needs_a_message():
    assert "--message-file" in rejected(
        "--dry-run", "tickets", "forward", "5", "--to", "a@x.org", "--subject", "s"
    )


def test_move_without_note_omits_it():
    p = preview("--dry-run", "tickets", "move", "5", "--to-category", "2")
    assert p["body"] == {"staff_id": 1, "target_category_id": 2}


def test_body_file_in_config_dir_is_rejected(tmp_path):
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "token.json").write_text("{}")
    message = rejected(
        "--dry-run", "tickets", "reply", "5", "--text-file", str(cfg / "token.json")
    )
    assert "Refusing to read" in message


# -- create-bulk from stdin ---------------------------------------------------
def test_create_bulk_reads_stdin():
    tickets = [{"subject": "a"}, {"subject": "b"}]
    result = runner.invoke(
        cli,
        ["--dry-run", "tickets", "create-bulk", "--file", "-"],
        input=json.dumps(tickets),
        env=ENV,
    )
    assert result.exit_code == 0, result.output
    p = json.loads(result.stdout)
    assert p["method"] == "POST"
    assert p["url"].endswith("/tickets/")
    assert p["body"] == tickets


def test_create_bulk_bad_stdin_json_names_stdin():
    result = runner.invoke(
        cli, ["--dry-run", "tickets", "create-bulk", "--file", "-"], input="[", env=ENV
    )
    assert isinstance(result.exception, ValidationError), result.exception
    assert "Invalid JSON in stdin" in str(result.exception)


def test_create_bulk_file_in_config_dir_is_rejected(tmp_path):
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "payload.json").write_text("[]")
    assert "Refusing to read" in rejected(
        "--dry-run", "tickets", "create-bulk", "--file", str(cfg / "payload.json")
    )


# -- attachments and the config directory -------------------------------------
@pytest.mark.parametrize(
    "argv",
    [
        (*CREATE_NO_BODY, "--text", "b"),
        ("tickets", "reply", "5", "--text", "b"),
        ("tickets", "note", "5", "--text", "b"),
        ("tickets", "user-reply", "5", "--user", "9", "--text", "b"),
        (*FORWARD_NO_BODY, "--message", "m"),
    ],
    ids=["create", "reply", "note", "user-reply", "forward"],
)
def test_attachment_in_config_dir_is_rejected(mock_api, argv, tmp_path):
    captured = mock_api(lambda req: httpx.Response(200, json={}))
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "token.json").write_text('{"api_key": "k"}')
    message = rejected(*argv, "--attachment", str(cfg / "token.json"))
    assert "Refusing to read" in message
    assert "hfox config directory" in message
    assert captured == []


def test_inline_attachment_in_config_dir_is_rejected(tmp_path):
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "shot.png").write_bytes(b"\x89PNG")
    assert "Refusing to read" in rejected(
        "--dry-run", "tickets", "inline-attachment", str(cfg / "shot.png")
    )


def test_attachment_holding_credentials_is_rejected(tmp_path):
    f = tmp_path / "notes.txt"
    f.write_text("key=secret-api-key-123")
    result = runner.invoke(
        cli,
        ["--dry-run", "tickets", "reply", "5", "--text", "b", "--attachment", str(f)],
        env={**ENV, "HFOX_API_KEY": "secret-api-key-123"},
    )
    assert isinstance(result.exception, ValidationError), result.exception
    assert "contains HappyFox credentials" in str(result.exception)
    assert "secret-api-key-123" not in str(result.exception)
