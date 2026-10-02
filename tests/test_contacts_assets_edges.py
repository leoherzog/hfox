"""Edge cases for `hfox contacts` and `hfox assets`: limits, ranges, phones and custom fields."""

import json
import re
import sys

import httpx
import pytest
from typer.testing import CliRunner

import hfox.cli.main as main_mod
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

STAFF = [{"id": 7, "name": "Ada Lovelace", "email": "ada@x.org", "active": True}]


def run(*args, env_extra=None):
    return runner.invoke(cli, list(args), env={**ENV, **(env_extra or {})})


def dry(*args):
    result = run("--dry-run", *args)
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def refused(mock_api, *args, match):
    """Assert a ValidationError containing `match` is raised before any request; return its text."""
    captured = mock_api(lambda req: httpx.Response(200, json={}))
    result = run(*args)
    assert isinstance(result.exception, ValidationError), result.output
    message = str(result.exception)
    assert match in message
    assert captured == []
    assert result.stdout == ""
    return message


@pytest.fixture
def app_error(monkeypatch, capsys):
    """Run the real app() in process; return the JSON error it printed on stdout."""

    def invoke(*argv):
        for key, value in ENV.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setattr(sys, "argv", ["hfox", *argv])
        with pytest.raises(SystemExit) as exc:
            main_mod.app()
        payload = json.loads(capsys.readouterr().out)
        assert payload["exit_code"] == exc.value.code
        return payload

    return invoke


# -- bulk limits -------------------------------------------------------------
def test_contacts_create_bulk_accepts_100_contacts(tmp_path):
    rows = [{"name": f"C{i}", "email": f"c{i}@x.org"} for i in range(100)]
    f = tmp_path / "contacts.json"
    f.write_text(json.dumps(rows), encoding="utf-8")
    p = dry("contacts", "create-bulk", "--file", str(f))
    assert p["method"] == "POST"
    assert p["url"].endswith("/users/")
    assert p["body"] == rows


def test_groups_add_contacts_accepts_100_ids():
    ids = list(range(1, 101))
    p = dry(
        "contacts", "groups", "add-contacts", "3", "--contacts", ",".join(str(i) for i in ids)
    )
    assert p["url"].endswith("/contact_group/3/update_contacts/")
    assert p["body"] == [{"contact": i} for i in ids]


@pytest.mark.parametrize("verb", ["add-contacts", "remove-contacts"])
@pytest.mark.parametrize("blank", ["", " , "])
def test_groups_membership_refuses_a_blank_contact_list(mock_api, verb, blank):
    refused(mock_api, "contacts", "groups", verb, "3", "--contacts", blank, match="contact id")


@pytest.mark.parametrize("verb", ["add-contacts", "remove-contacts"])
@pytest.mark.parametrize("ids", ["0", "4,0", "00"])
def test_groups_membership_refuses_a_zero_contact_id(mock_api, verb, ids):
    refused(mock_api, "contacts", "groups", verb, "3", "--contacts", ids, match="at least 1")


# -- contact phones ----------------------------------------------------------
@pytest.mark.parametrize("phone_type", ["m", "h", "o"])
def test_contacts_create_accepts_the_documented_phone_type(phone_type):
    p = dry("contacts", "create", "--name", "J", "--phone", "555", "--phone-type", phone_type)
    assert p["body"]["phones"] == [{"type": phone_type, "number": "555", "is_primary": True}]


def test_contacts_bad_phone_type_error_lists_the_valid_types(mock_api):
    message = refused(
        mock_api, "contacts", "create", "--name", "J", "--phone", "555", "--phone-type", "mobile",
        match="mobile",
    )
    assert {"mo", "w", "m", "h", "o"} <= set(re.findall(r"\w+", message))


@pytest.mark.parametrize(
    "argv", [("create", "--name", "J", "--phone", "555"), ("update", "12", "--phone", "555")]
)
def test_contacts_empty_phone_type_is_refused(mock_api, argv):
    refused(mock_api, "contacts", *argv, "--phone-type", "", match="Invalid phone type")


def test_contacts_create_blank_phone_with_phone_type_is_refused(mock_api):
    refused(
        mock_api, "contacts", "create", "--name", "J", "--email", "j@x.org",
        "--phone", " ", "--phone-type", "w",
        match="--phone-type requires --phone",
    )


@pytest.mark.parametrize(
    "extra",
    [
        ("--no-primary",),
        ("--phone", " ", "--primary"),
        ("--phone", "", "--phone-id", "31", "--phone-type", "w"),
    ],
)
def test_contacts_update_phone_options_need_a_number(mock_api, extra):
    refused(
        mock_api, "contacts", "update", "12", "--name", "x", *extra, match="require --phone"
    )


# -- optional one-line values ------------------------------------------------
_EDIT = ("contacts", "update", "12", "--cf", "5=Gold")
_GROUP = ("contacts", "groups", "create", "--name", "VIPs")
BLANK_VALUES = [
    (_EDIT, "--name", ""),
    (_EDIT, "--name", " \t"),
    (_EDIT, "--email", ""),
    (_EDIT, "--email", "  "),
    (_GROUP, "--description", ""),
    (_GROUP, "--description", "  "),
    (_GROUP, "--domains", ""),
    (_GROUP, "--domains", "  "),
    (_GROUP, "--domains", " , "),
]


@pytest.mark.parametrize("dry_run", [(), ("--dry-run",)], ids=["live", "dry-run"])
@pytest.mark.parametrize(
    "argv, flag, blank",
    BLANK_VALUES,
    ids=[f"{' '.join(argv[1:-2])} {flag} {blank!r}" for argv, flag, blank in BLANK_VALUES],
)
def test_contacts_blank_one_line_value_is_refused(mock_api, argv, flag, blank, dry_run):
    message = refused(mock_api, *dry_run, *argv, flag, blank, match=flag)
    assert message == f"{flag} must not be blank."


def test_contacts_update_sends_name_and_email_without_outer_whitespace():
    p = dry("contacts", "update", "12", "--name", "  Jane Doe ", "--email", " j@x.org\t")
    assert p["body"] == {"name": "Jane Doe", "email": "j@x.org"}


def test_groups_create_sends_description_without_outer_whitespace():
    p = dry(*_GROUP, "--description", "  key accounts ", "--domains", " a.com , b.com ")
    assert p["body"] == {
        "name": "VIPs",
        "description": "key accounts",
        "tagged_domains": "a.com,b.com",
    }


@pytest.mark.parametrize(
    "description, sent", [("", ""), ("  ", ""), ("  key accounts ", "key accounts")]
)
def test_groups_update_blank_description_clears(description, sent):
    p = dry("contacts", "groups", "update", "3", "--description", description)
    assert p["method"] == "POST"
    assert p["url"].endswith("/contact_group/3/")
    assert p["body"] == {"description": sent}


def test_groups_update_help_says_a_blank_description_clears():
    result = run("contacts", "groups", "update", "--help", env_extra={"COLUMNS": "200"})
    assert result.exit_code == 0, result.output
    assert "New description; '' clears it." in result.stdout


# -- contact custom fields ---------------------------------------------------
@pytest.mark.parametrize(
    "argv, flag, typed, key",
    [
        (("create", "--name", "J", "--email", "j@x.org"), "--cf", "007", "c-cf-7"),
        (("create", "--name", "J", "--email", "j@x.org"), "--cf", "c-cf-007", "c-cf-7"),
        (("update", "12"), "--cf", "007", "c-cf-7"),
        (("update", "12"), "--cf", "c-cf-007", "c-cf-7"),
    ],
)
def test_contacts_custom_field_id_is_sent_without_leading_zeros(argv, flag, typed, key):
    for option in ((flag, f"{typed}=x"), (f"{flag}-json", json.dumps({typed: "x"}))):
        body = dry("contacts", *argv, *option)["body"]
        assert {name: value for name, value in body.items() if "cf-" in name} == {key: "x"}



@pytest.mark.parametrize(
    "flag, value, body",
    [
        ("--cf", "5=Gold", {"c-cf-5": "Gold"}),
        ("--cf-json", '{"3": null, "c-cf-4": "02"}', {"c-cf-3": None, "c-cf-4": "02"}),
    ],
)
def test_contacts_update_custom_fields_alone_are_an_edit(flag, value, body):
    p = dry("contacts", "update", "12", flag, value)
    assert p["method"] == "POST"
    assert p["url"].endswith("/user/12/")
    assert p["body"] == body


_CREATE = ("create", "--name", "J", "--email", "j@x.org")
_UPDATE = ("update", "12")


@pytest.mark.parametrize(
    "verb, flag, value",
    [
        (_CREATE, "--cf-json", '{"ccf-4": "x"}'),
        (_CREATE, "--cf-json", '{"t-cf-4": "x"}'),
        (_UPDATE, "--cf", "ccf-4=x"),
        (_UPDATE, "--cf-json", '{"ccf-4": "x"}'),
        (_UPDATE, "--cf-json", '{"t-cf-4": "x"}'),
    ],
)
def test_contacts_cf_keys_of_another_endpoint_are_refused(mock_api, verb, flag, value):
    refused(mock_api, "contacts", *verb, flag, value, match="Invalid custom-field key")


# -- ranges and required options ---------------------------------------------
@pytest.mark.parametrize(
    "argv, url",
    [
        (("contacts", "list"), "/users/?page=1&size=50"),
        (
            ("assets", "custom-fields", "list", "--asset-type", "2"),
            "/asset_custom_fields/?asset_type=2&size=50&page=1",
        ),
    ],
)
def test_listing_accepts_the_maximum_size(argv, url):
    assert dry(*argv, "--size", "50")["url"].endswith(url)


_PHONE_EDIT = ("contacts", "update", "12", "--phone", "5", "--phone-type", "w")
_ASSET = ("assets", "create", "--name", "X", "--display-id", "D")


@pytest.mark.parametrize(
    "argv, name",
    [
        ((*_PHONE_EDIT, "--phone-id", "0"), "--phone-id"),
        (("contacts", "groups", "get", "0"), "group_id"),
        (("contacts", "groups", "update", "0", "--description", "d"), "group_id"),
        (("contacts", "groups", "add-contacts", "0", "--contacts", "1"), "group_id"),
        ((*_ASSET, "--asset-type", "0"), "--asset-type"),
        (("assets", "delete", "0", "--yes"), "asset_id"),
        (("assets", "custom-fields", "list", "--asset-type", "0"), "--asset-type"),
        (("assets", "custom-fields", "list", "--size", "51"), "--size"),
        (("assets", "custom-fields", "list", "--size", "0"), "--size"),
        (("assets", "custom-fields", "list", "--page", "0"), "--page"),
    ],
)
def test_out_of_range_value_exits_3_as_validation(app_error, mock_api, argv, name):
    captured = mock_api(lambda req: httpx.Response(200, json={}))
    payload = app_error(*argv)
    assert (payload["type"], payload["exit_code"]) == ("validation", 3)
    assert name in payload["error"].lower()
    assert captured == []


@pytest.mark.parametrize(
    "argv, flag",
    [
        (("contacts", "create", "--email", "j@x.org"), "--name"),
        (("contacts", "create-bulk"), "--file"),
        (("contacts", "groups", "create"), "--name"),
        (("contacts", "groups", "add-contacts", "3"), "--contacts"),
        (("contacts", "groups", "remove-contacts", "3"), "--contacts"),
        (("assets", "create", "--display-id", "D"), "--name"),
        (("assets", "create", "--name", "X"), "--display-id"),
    ],
)
def test_missing_required_option_is_a_usage_error(app_error, mock_api, argv, flag):
    captured = mock_api(lambda req: httpx.Response(200, json={}))
    payload = app_error(*argv)
    assert (payload["type"], payload["exit_code"]) == ("usage", 3)
    assert flag in payload["error"]
    assert captured == []


# -- listings ----------------------------------------------------------------
ASSET_LISTINGS = [
    ("assets", "list"),
    ("assets", "types", "list"),
    ("assets", "custom-fields", "list"),
]


@pytest.mark.parametrize("argv", [("contacts", "list"), *ASSET_LISTINGS])
def test_single_page_listing_renders_its_rows_as_csv(mock_api, argv):
    body = {
        "page_info": {"page_count": 1, "count": 2},
        "data": [{"id": 1, "name": "A"}, {"id": 2, "name": "B"}],
    }
    mock_api(lambda req: httpx.Response(200, json=body))
    result = run("-f", "csv", *argv)
    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == ["id,name", "1,A", "2,B"]


@pytest.mark.parametrize(
    "argv, needs_page_all",
    [*((argv, True) for argv in ASSET_LISTINGS), (("contacts", "groups", "list"), False)],
)
def test_name_filter_help_says_whether_it_needs_page_all(argv, needs_page_all):
    result = run(*argv, "--help", env_extra={"COLUMNS": "200"})
    assert result.exit_code == 0, result.output
    assert "--name" in result.stdout
    assert ("--page-all" in result.stdout) is needs_page_all


# -- asset bodies ------------------------------------------------------------
def test_assets_create_without_asset_type_sends_no_query():
    p = dry(*_ASSET)
    assert p["url"].endswith("/assets/")
    assert p["params"] is None
    assert p["body"] == {"name": "X", "display_id": "D", "created_by": 1}


def test_assets_update_sends_new_contacts_as_contacts():
    new = [{"name": "Jane", "email": "j@x.org"}]
    p = dry("assets", "update", "10", "--new-contact-json", json.dumps(new))
    assert p["body"] == {"updated_by": 1, "contacts": new}


@pytest.mark.parametrize("blank", ["", "  "])
def test_assets_blank_new_contact_json_is_omitted(blank):
    p = dry(*_ASSET, "--new-contact-json", blank)
    assert p["body"] == {"name": "X", "display_id": "D", "created_by": 1}


@pytest.mark.parametrize(
    "argv, flag",
    [
        ((*_ASSET, "--new-contact-json", '[{"name": "Jane"}, 5]'), "--new-contact-json"),
        (("assets", "update", "10", "--name", "x" * 201), "--name"),
        (("assets", "update", "10", "--display-id", ""), "--display-id"),
        ((*_ASSET, "--contact-group-ids", ""), "--contact-group-ids"),
        ((*_ASSET, "--contact-group-ids", " , "), "--contact-group-ids"),
    ],
    ids=[
        "new contacts mixed with a non-object",
        "update name over 200 characters",
        "update empty display id",
        "create empty contact group ids",
        "create blank contact group ids",
    ],
)
def test_assets_invalid_value_is_refused_naming_its_flag(mock_api, argv, flag):
    refused(mock_api, *argv, match=flag)


@pytest.mark.parametrize("argv", [_ASSET, ("assets", "update", "10")], ids=["create", "update"])
@pytest.mark.parametrize(
    "option",
    [
        ("--contact-ids", "0"),
        ("--contact-ids", "5,0"),
        ("--contact-group-ids", "00"),
        ("--cf", "0=x"),
        ("--cf-json", '{"0": "x"}'),
    ],
    ids=lambda option: " ".join(option),
)
def test_assets_refuse_a_zero_id(mock_api, argv, option):
    refused(mock_api, *argv, *option, match="at least 1")


@pytest.mark.parametrize("argv", [_ASSET, ("assets", "update", "10")], ids=["create", "update"])
@pytest.mark.parametrize(
    "options, fields",
    [
        (("--cf", "007=x"), {"7": "x"}),
        (("--cf-json", '{"0100": "x"}'), {"100": "x"}),
        (("--cf", "7=a", "--cf", "007=b"), {"7": "b"}),
        (("--cf", "007=a", "--cf-json", '{"7": "b"}'), {"7": "b"}),
    ],
    ids=["--cf", "--cf-json", "given twice", "across flags"],
)
def test_assets_custom_field_id_is_sent_without_leading_zeros(argv, options, fields):
    assert dry(*argv, *options)["body"]["custom_fields"] == fields


# -- asset staff identity ----------------------------------------------------
@pytest.mark.parametrize(
    "argv",
    [
        ("create", "--name", " ", "--display-id", "D"),
        ("create", "--name", "X", "--display-id", "D", "--contact-ids", ""),
        ("update", "10"),
    ],
)
def test_assets_invalid_input_is_refused_before_the_staff_lookup(mock_api, argv):
    captured = mock_api(lambda req: httpx.Response(200, json=STAFF))
    result = run("assets", *argv, "--staff", "ada@x.org")
    assert isinstance(result.exception, ValidationError), result.output
    assert captured == []


@pytest.mark.parametrize(
    "argv, key",
    [
        (("create", "--name", "X", "--display-id", "D"), "created_by"),
        (("update", "10", "--name", "X"), "updated_by"),
    ],
)
def test_assets_staff_id_zero_is_sent_in_the_body(argv, key):
    p = dry("assets", *argv, "--staff-id", "0")
    assert p["body"][key] == 0


def test_assets_delete_staff_id_zero_is_sent_in_the_query_only():
    p = dry("assets", "delete", "10", "--staff-id", "0")
    assert p["url"].endswith("/asset/10/?deleted_by=0")
    assert p["params"] == {"deleted_by": 0}
    assert p["body"] is None


def test_assets_delete_quiet_suppresses_the_status_line(mock_api):
    captured = mock_api(lambda req: httpx.Response(200, json={"deleted": True}))
    result = run("--quiet", "assets", "delete", "10", "--yes")
    assert result.exit_code == 0, result.output
    assert [req.method for req in captured] == ["DELETE"]
    assert result.stderr == ""
    assert json.loads(result.stdout) == {"deleted": True}
