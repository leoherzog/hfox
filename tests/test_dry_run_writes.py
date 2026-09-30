"""Dry-run method, URL and body for write verbs."""

import json

from typer.testing import CliRunner

from hfox.cli.main import cli

runner = CliRunner()

ENV = {
    "HFOX_SUBDOMAIN": "acme",
    "HFOX_REGION": "us",
    "HFOX_API_KEY": "k",
    "HFOX_AUTH_CODE": "c",
    "HFOX_STAFF_ID": "1",
}


def run(*args, env_extra=None):
    env = dict(ENV)
    if env_extra:
        env.update(env_extra)
    return runner.invoke(cli, list(args), env=env)


def preview(*args, env_extra=None):
    result = run(*args, env_extra=env_extra)
    assert result.exit_code == 0, result.stdout
    return json.loads(result.stdout)


# -- tickets move ------------------------------------------------------
def test_dry_run_ticket_move_body():
    p = preview(
        "--dry-run", "tickets", "move", "42",
        "--to-category", "9", "--note", "rerouted", "--assign-to", "7",
    )
    assert p["method"] == "POST"
    assert p["url"].endswith("/ticket/42/move/")
    assert p["body"]["target_category_id"] == 9
    assert p["body"]["move_note"] == "rerouted"
    assert p["body"]["assign_to"] == 7
    assert p["body"]["staff_id"] == 1


# -- tickets note (ccf- prefix + staff_pvtnote path) -------------
def test_dry_run_ticket_note_uses_ccf_prefix_and_pvtnote_path():
    p = preview(
        "--dry-run", "tickets", "note", "42",
        "--text", "internal", "--contact-cf", "4=VIP",
    )
    assert p["method"] == "POST"
    assert p["url"].endswith("/ticket/42/staff_pvtnote/")
    assert p["body"]["plaintext"] == "internal"
    assert p["body"]["staff"] == 1
    assert p["body"]["ccf-4"] == "VIP"
    assert "c-cf-4" not in p["body"]


# -- tickets reply --contact-cf uses ccf- ------------------------------
def test_dry_run_ticket_reply_contact_cf_uses_ccf_prefix():
    p = preview(
        "--dry-run", "tickets", "reply", "42",
        "--text", "ok", "--contact-cf", "4=VIP",
    )
    assert p["url"].endswith("/ticket/42/staff_update/")
    assert p["body"]["ccf-4"] == "VIP"
    assert "c-cf-4" not in p["body"]


# -- tickets subscribe (int-array data body) ---------------------------
def test_dry_run_ticket_subscribe_int_array_data():
    p = preview(
        "--dry-run", "tickets", "subscribe", "42", "--agents", "3,4,5",
    )
    assert p["method"] == "POST"
    assert p["url"].endswith("/ticket/42/subscribe/")
    assert p["body"]["data"] == [3, 4, 5]
    assert all(isinstance(x, int) for x in p["body"]["data"])
    assert p["body"]["staff_id"] == 1


# -- assets update (PUT) -----------------------------------------------
def test_dry_run_asset_update_is_put():
    p = preview(
        "--dry-run", "assets", "update", "10",
        "--name", "New", "--display-id", "L-9",
    )
    assert p["method"] == "PUT"
    assert p["url"].endswith("/asset/10/")
    assert p["body"]["name"] == "New"
    assert p["body"]["display_id"] == "L-9"
    assert p["body"]["updated_by"] == 1


# -- assets delete (DELETE + deleted_by) -------------------------------
def test_dry_run_asset_delete_is_delete_with_deleted_by():
    p = preview(
        "--dry-run", "assets", "delete", "10", "--yes",
    )
    assert p["method"] == "DELETE"
    assert p["url"].endswith("/asset/10/?deleted_by=1")
    assert p["params"]["deleted_by"] == 1


# -- contacts update ---------------------------------------------------
def test_dry_run_contact_update_body():
    p = preview(
        "--dry-run", "contacts", "update", "55",
        "--name", "Jane", "--email", "j@x.org",
    )
    assert p["method"] == "POST"
    assert p["url"].endswith("/user/55/")
    assert p["body"]["name"] == "Jane"
    assert p["body"]["email"] == "j@x.org"


# -- contacts groups add-contacts --------------------------------------
def test_dry_run_group_add_contacts_array_body():
    p = preview(
        "--dry-run", "contacts", "groups", "add-contacts", "3",
        "--contacts", "11,12", "--access-tickets",
    )
    assert p["method"] == "POST"
    assert p["url"].endswith("/contact_group/3/update_contacts/")
    # HappyFox wants a JSON array, one entry per contact.
    assert p["body"] == [
        {"contact": 11, "access_tickets": True},
        {"contact": 12, "access_tickets": True},
    ]


# -- --cf-json on tickets create keeps a comma value as a string -------
def test_dry_run_ticket_create_cf_json_keeps_string_unsplit():
    p = preview(
        "--dry-run", "tickets", "create",
        "--subject", "S", "--category", "3",
        "--name", "Han", "--email", "h@x.org", "--text", "body",
        "--cf-json", '{"1":"Acme, Inc."}',
    )
    assert p["body"]["t-cf-1"] == "Acme, Inc."
    assert not isinstance(p["body"]["t-cf-1"], list)


# -- multipart attachment dry-run encodes a bool as 'true'/'false' -----
def test_dry_run_multipart_attachment_bool_serialized(tmp_path):
    f = tmp_path / "log.txt"
    f.write_text("hello")
    p = preview(
        "--dry-run", "tickets", "reply", "42",
        "--text", "see attached", "--update-customer",
        "--attachment", str(f),
    )
    assert p["body"]["update_customer"] == "true"
    assert p["attachments"]["count"] == 1
    assert p["attachments"]["fields"][0]["filename"] == "log.txt"
    assert p["attachments"]["fields"][0]["field"] == "attachments"
