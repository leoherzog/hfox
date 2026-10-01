"""Dry-run request shapes driven by the cf.py and _util.py helpers."""

import json
import os

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

CREATE = (
    "--dry-run", "tickets", "create", "--subject", "Down", "--category", "3",
    "--name", "Han", "--email", "h@x.org", "--text", "fire",
)


def run(*args):
    return runner.invoke(cli, list(args), env=ENV)


def rejected(*args):
    """Run a command expected to fail validation before any request is built."""
    result = run(*args)
    assert isinstance(result.exception, ValidationError), result.exception
    assert result.stdout == ""
    return result.exception


def preview(*args):
    result = run(*args)
    assert result.exit_code == 0, result.stdout
    return json.loads(result.stdout)


def test_create_cf_keeps_text_and_builds_bracket_list():
    p = preview(*CREATE, "--cf", "1=Acme, Inc.", "--cf", "2=[4]", "--cf", "3=007")
    assert p["method"] == "POST"
    assert p["url"].endswith("/tickets/")
    assert p["body"]["t-cf-1"] == "Acme, Inc."
    assert p["body"]["t-cf-2"] == [4]
    assert p["body"]["t-cf-3"] == "007"


def test_create_cf_json_null_is_sent():
    p = preview(*CREATE, "--cf-json", '{"10": null}')
    assert p["method"] == "POST"
    assert p["url"].endswith("/tickets/")
    assert "t-cf-10" in p["body"]
    assert p["body"]["t-cf-10"] is None


def test_create_passes_documented_contact_prefix_through():
    p = preview(*CREATE, "--cf", "c-cf-3=Gold", "--cf-json", '{"c-cf-4": "Acme, Inc."}')
    assert p["method"] == "POST"
    assert p["url"].endswith("/tickets/")
    assert p["body"]["c-cf-3"] == "Gold"
    assert p["body"]["c-cf-4"] == "Acme, Inc."
    assert not any(k.startswith("t-cf-c-cf-") for k in p["body"])


def test_reply_passes_ccf_prefix_through():
    p = preview(
        "--dry-run", "tickets", "reply", "42", "--text", "ok",
        "--cf-json", '{"ccf-4": 1, "5": null}',
    )
    assert p["method"] == "POST"
    assert p["url"].endswith("/ticket/42/staff_update/")
    assert p["body"]["ccf-4"] == 1
    assert p["body"]["t-cf-5"] is None


def test_update_cf_rejects_contact_prefix():
    rejected("--dry-run", "tickets", "update-cf", "42", "--cf", "ccf-4=y")


def test_asset_cf_rejects_non_numeric_key():
    rejected(
        "--dry-run", "assets", "create", "--asset-type", "1",
        "--name", "MBP", "--display-id", "L-1", "--cf", "Serial=x",
    )


def test_create_rejects_attachments_over_25mb(tmp_path):
    a, b = tmp_path / "a.bin", tmp_path / "b.bin"
    a.write_bytes(b"")
    b.write_bytes(b"")
    os.truncate(a, 20_000_000)
    os.truncate(b, 5_000_001)
    exc = rejected(*CREATE, "--attachment", str(a), "--attachment", str(b))
    assert "25,000,000" in str(exc)


def test_create_bulk_rejects_nan(tmp_path):
    f = tmp_path / "bulk.json"
    f.write_text('[{"subject": "s", "t-cf-1": NaN}]', encoding="utf-8")
    rejected("--dry-run", "tickets", "create-bulk", "--file", str(f))


@pytest.mark.parametrize(
    "argv, url_suffix",
    [
        (("system", "categories", "--name", "x"), "/categories/"),
        (("system", "priorities", "--name", "x"), "/priorities/"),
        (("system", "staff", "--name", "x", "--email", "y"), "/staff/"),
        (("system", "statuses", "--name", "x"), "/statuses/"),
        (("system", "ticket-custom-fields", "--name", "x"), "/ticket_custom_fields/"),
        (("system", "contact-custom-fields", "--name", "x"), "/user_custom_fields/"),
        (("contacts", "groups", "list", "--name", "x"), "/contact_groups/"),
        (("--page-all", "assets", "list", "--name", "x"), "/assets/?size=10&page=1"),
        (("--page-all", "assets", "types", "list", "--name", "x"), "/asset_types/?size=10&page=1"),
        (
            ("--page-all", "assets", "custom-fields", "list", "--name", "x"),
            "/asset_custom_fields/?size=10&page=1",
        ),
    ],
)
def test_local_filters_never_reach_the_request(argv, url_suffix):
    p = preview("--dry-run", *argv)
    assert p["method"] == "GET"
    assert p["url"].endswith(url_suffix)
    query = p["url"].partition("?")[2]
    for key in ("name", "email"):
        assert key not in (p["params"] or {})
        assert key not in query
