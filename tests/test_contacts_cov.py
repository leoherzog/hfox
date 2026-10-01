"""Coverage for `hfox contacts` and its `groups` sub-app.

Reads run against `mock_api`; writes are checked with --dry-run for method, URL and body.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from conftest import subprocess_env
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


def run(*args, env_extra=None, **kwargs):
    env = dict(ENV)
    if env_extra:
        env.update(env_extra)
    return runner.invoke(cli, list(args), env=env, **kwargs)


def preview(*args, env_extra=None):
    result = run(*args, env_extra=env_extra)
    assert result.exit_code == 0, result.stdout
    return json.loads(result.stdout)


_INVOKER = (
    "import sys; sys.argv = ['hfox'] + sys.argv[1:]; "
    "from hfox.cli.main import app; app()"
)


def run_app(argv, stdin=None):
    """Run the real `app()` entry point; --dry-run keeps a regression off the network.

    `stdin` is the text piped in; without it stdin is empty.
    """
    return subprocess.run(
        [sys.executable, "-c", _INVOKER, "--dry-run", *argv],
        env=subprocess_env(**ENV),
        capture_output=True,
        text=True,
        **({"stdin": subprocess.DEVNULL} if stdin is None else {"input": stdin}),
    )


# -- contacts list ---------------------------------------------------------
def test_contacts_list_get_params_and_renders(mock_api):
    body = {"page_info": {"page_count": 1, "count": 1}, "data": [{"id": 7, "name": "Ada"}]}
    captured = mock_api(lambda req: httpx.Response(200, json=body))

    result = run("contacts", "list", "--query", "name:ada", "--page", "2", "--size", "25")

    assert result.exit_code == 0, result.stdout
    assert len(captured) == 1
    req = captured[0]
    assert req.method == "GET"
    assert req.url.path.endswith("/users/")
    params = dict(req.url.params)
    assert params["q"] == "name:ada"
    assert params["page"] == "2"
    assert params["size"] == "25"
    # JSON render keeps the full envelope.
    assert json.loads(result.stdout) == body


def test_contacts_list_default_size_10_no_query(mock_api):
    body = {"page_info": {"page_count": 1, "count": 0}, "data": []}
    captured = mock_api(lambda req: httpx.Response(200, json=body))

    result = run("contacts", "list")

    assert result.exit_code == 0, result.stdout
    params = dict(captured[0].url.params)
    # compact() drops the None query; size defaults to the API's 10, page to 1.
    assert "q" not in params
    assert params["size"] == "10"
    assert params["page"] == "1"
    assert json.loads(result.stdout)["data"] == []


def test_contacts_list_page_all_walks_pages(mock_api):
    def handler(req):
        page = dict(req.url.params).get("page", "1")
        return httpx.Response(
            200,
            json={"page_info": {"page_count": 2, "count": 2}, "data": [{"id": int(page)}]},
        )

    captured = mock_api(handler)

    result = run("--page-all", "contacts", "list")

    assert result.exit_code == 0, result.stdout
    # Two pages fetched; JSON + --page-all streams one page envelope per line.
    assert len(captured) == 2
    out = [json.loads(line) for line in result.stdout.strip().splitlines()]
    assert [page["data"][0]["id"] for page in out] == [1, 2]


# -- contacts get ----------------------------------------------------------
def test_contacts_get_by_id(mock_api):
    contact = {"id": 55, "name": "Jane", "email": "j@x.org"}
    captured = mock_api(lambda req: httpx.Response(200, json=contact))

    result = run("contacts", "get", "55")

    assert result.exit_code == 0, result.stdout
    assert captured[0].method == "GET"
    assert captured[0].url.path.endswith("/user/55/")
    assert json.loads(result.stdout) == contact


def test_contacts_get_by_email(mock_api):
    contact = {"id": 9, "email": "a@x.com"}
    captured = mock_api(lambda req: httpx.Response(200, json=contact))

    result = run("contacts", "get", "a@x.com")

    assert result.exit_code == 0, result.stdout
    assert captured[0].url.path.endswith("/user/a@x.com/")


# -- contacts create -------------------------------------------------------
def test_contacts_create_full_body_with_phone_and_cf():
    p = preview(
        "--dry-run", "contacts", "create",
        "--name", "Jane", "--email", "j@x.org", "--phone", "5551234",
        "--cf", "4=VIP",
    )
    assert p["method"] == "POST"
    assert p["url"].endswith("/users/")
    assert p["body"]["name"] == "Jane"
    assert p["body"]["email"] == "j@x.org"
    # Phone becomes a phones array marked primary.
    assert p["body"]["phones"] == [
        {"type": "o", "number": "5551234", "is_primary": True}
    ]
    # Contact create custom fields use the c-cf- prefix.
    assert p["body"]["c-cf-4"] == "VIP"


def test_contacts_create_phone_type_flag():
    p = preview(
        "--dry-run", "contacts", "create",
        "--name", "Jane", "--email", "j@x.org",
        "--phone", "5551234", "--phone-type", "mo",
    )
    assert p["body"]["phones"] == [
        {"type": "mo", "number": "5551234", "is_primary": True}
    ]


def test_contacts_create_bad_phone_type_exits_3():
    proc = run_app(
        ["contacts", "create", "--name", "J", "--phone", "5551234", "--phone-type", "x"]
    )
    assert proc.returncode == 3
    assert "phone type" in json.loads(proc.stdout)["error"].lower()


def test_contacts_create_bad_phone_type_no_network(mock_api):
    captured = mock_api(lambda req: httpx.Response(200, json={}))
    result = run(
        "contacts", "create", "--name", "J", "--phone", "5551234", "--phone-type", "zz",
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, ValidationError)
    assert int(result.exception.exit_code) == 3
    # Validation happens before any HTTP request.
    assert captured == []


def test_contacts_create_phone_only_sends_null_email():
    p = preview(
        "--dry-run", "contacts", "create",
        "--name", "PhoneOnly", "--phone", "5551234",
    )
    # email is required but nullable: phone-only creates send an explicit null.
    assert "email" in p["body"]
    assert p["body"]["email"] is None
    assert p["body"]["phones"] == [
        {"type": "o", "number": "5551234", "is_primary": True}
    ]


def test_contacts_create_no_phone_omits_phones_uses_cf_json():
    p = preview(
        "--dry-run", "contacts", "create",
        "--name", "NoPhone", "--email", "n@x.org",
        "--cf-json", '{"7": ["a", "b"], "c-cf-8": null}',
    )
    assert p["body"] == {
        "name": "NoPhone",
        "email": "n@x.org",
        "c-cf-7": ["a", "b"],
        "c-cf-8": None,
    }


# -- contacts update -------------------------------------------------------
def test_contacts_update_set_login_true_maps_to_TRUE():
    p = preview(
        "--dry-run", "contacts", "update", "55",
        "--name", "Jane", "--set-login",
    )
    assert p["method"] == "POST"
    assert p["url"].endswith("/user/55/")
    assert p["body"]["name"] == "Jane"
    assert p["body"]["is_login_enabled"] == "TRUE"


def test_contacts_update_no_set_login_sends_false():
    p = preview(
        "--dry-run", "contacts", "update", "55",
        "--email", "new@x.org", "--no-set-login",
    )
    assert p["body"]["is_login_enabled"] == "FALSE"
    assert p["body"]["email"] == "new@x.org"


def test_contacts_update_phone_and_cf():
    p = preview(
        "--dry-run", "contacts", "update", "12",
        "--phone", "999", "--cf", "3=Gold",
    )
    # No --phone-id adds a phone; type and is_primary are sent only when given.
    assert p["body"]["phones"] == [{"number": "999"}]
    assert p["body"]["c-cf-3"] == "Gold"
    # set_login untouched -> omitted.
    assert "is_login_enabled" not in p["body"]


def test_contacts_update_phone_id_and_type_edit_existing_record():
    p = preview(
        "--dry-run", "contacts", "update", "12",
        "--phone", "999", "--phone-id", "77", "--phone-type", "w",
    )
    assert p["url"].endswith("/user/12/")
    assert p["body"] == {"phones": [{"id": 77, "type": "w", "number": "999"}]}


def test_contacts_update_bad_phone_type_exits_3():
    proc = run_app(
        ["contacts", "update", "12", "--phone", "999", "--phone-type", "bad"]
    )
    assert proc.returncode == 3
    assert "phone type" in json.loads(proc.stdout)["error"].lower()


# -- contacts create-bulk --------------------------------------------------
def test_contacts_create_bulk_posts_array(tmp_path):
    f = tmp_path / "contacts.json"
    f.write_text(json.dumps([{"name": "A"}, {"name": "B"}]))
    p = preview("--dry-run", "contacts", "create-bulk", "--file", str(f))
    assert p["method"] == "POST"
    assert p["url"].endswith("/users/")
    assert p["body"] == [{"name": "A"}, {"name": "B"}]


def test_contacts_create_bulk_rejects_non_array(tmp_path):
    f = tmp_path / "contacts.json"
    f.write_text(json.dumps({"name": "A"}))
    result = run("--dry-run", "contacts", "create-bulk", "--file", str(f))
    assert isinstance(result.exception, ValidationError)
    assert "JSON array of contacts" in str(result.exception)


def test_contacts_create_bulk_over_100_is_validation_error(mock_api, tmp_path):
    captured = mock_api(lambda req: httpx.Response(200, json=[]))
    f = tmp_path / "contacts.json"
    f.write_text(json.dumps([{"name": f"C{i}"} for i in range(101)]))
    result = run("contacts", "create-bulk", "--file", str(f))
    assert result.exit_code != 0
    assert isinstance(result.exception, ValidationError)
    assert int(result.exception.exit_code) == 3
    assert "between 1 and 100" in str(result.exception)
    # The cap is enforced before any HTTP request.
    assert captured == []


def test_contacts_create_bulk_over_100_exits_3(tmp_path):
    f = tmp_path / "contacts.json"
    f.write_text(json.dumps([{"name": f"C{i}"} for i in range(101)]))
    proc = run_app(["contacts", "create-bulk", "--file", str(f)])
    assert proc.returncode == 3
    assert "between 1 and 100" in json.loads(proc.stdout)["error"]


def test_contacts_create_bulk_empty_array_is_validation_error(tmp_path):
    f = tmp_path / "contacts.json"
    f.write_text("[]")
    proc = run_app(["contacts", "create-bulk", "--file", str(f)])
    assert proc.returncode == 3
    assert "between 1 and 100" in json.loads(proc.stdout)["error"]


def test_contacts_create_bulk_missing_file():
    result = run("contacts", "create-bulk", "--file", "/no/such/file.json")
    assert result.exit_code != 0
    assert isinstance(result.exception, ValidationError)


def test_contacts_create_bulk_reads_stdin():
    rows = [{"name": "A", "email": "a@x.org"}, {"name": "B", "email": "b@x.org"}]
    result = run(
        "--dry-run", "contacts", "create-bulk", "--file", "-", input=json.dumps(rows) + "\n"
    )
    assert result.exit_code == 0, result.output
    p = json.loads(result.stdout)
    assert p["method"] == "POST"
    assert p["url"].endswith("/users/")
    assert p["body"] == rows


def test_contacts_create_bulk_stdin_through_app():
    proc = run_app(["contacts", "create-bulk", "--file", "-"], stdin='[{"name": "A"}]')
    assert proc.returncode == 0, proc.stdout
    assert json.loads(proc.stdout)["body"] == [{"name": "A"}]


def test_contacts_create_bulk_stdin_posts_array(mock_api):
    created = [{"email": "a@x.org", "success": True, "id": 1}]
    captured = mock_api(lambda req: httpx.Response(200, json=created))
    result = run("contacts", "create-bulk", "--file", "-", input='[{"email": "a@x.org"}]')
    assert result.exit_code == 0, result.output
    assert captured[0].method == "POST"
    assert captured[0].url.path.endswith("/users/")
    assert json.loads(captured[0].content) == [{"email": "a@x.org"}]
    assert json.loads(result.stdout) == created


def test_contacts_create_bulk_empty_stdin_exits_3():
    proc = run_app(["contacts", "create-bulk", "--file", "-"])
    assert proc.returncode == 3, proc.stdout
    payload = json.loads(proc.stdout)
    assert payload["type"] == "validation"
    assert "stdin" in payload["error"]


def test_contacts_create_bulk_invalid_stdin_names_stdin(mock_api):
    captured = mock_api(lambda req: httpx.Response(200, json=[]))
    result = run("contacts", "create-bulk", "--file", "-", input="{not json")
    assert isinstance(result.exception, ValidationError), result.output
    assert "Invalid JSON in stdin" in str(result.exception)
    assert captured == []


def test_contacts_create_bulk_stdin_must_be_utf8(mock_api):
    captured = mock_api(lambda req: httpx.Response(200, json=[]))
    result = run("contacts", "create-bulk", "--file", "-", input=b'["caf\xe9"]')
    assert isinstance(result.exception, ValidationError), result.output
    assert "--file: stdin is not UTF-8" in str(result.exception)
    assert captured == []


@pytest.mark.parametrize("dry_run", [True, False])
def test_contacts_create_bulk_file_inside_config_dir_is_refused(mock_api, dry_run):
    captured = mock_api(lambda req: httpx.Response(200, json=[]))
    cfg = Path(os.environ["HFOX_CONFIG_DIR"])
    cfg.mkdir(parents=True)
    for name, content in (("token.json", "{}"), ("contacts.json", '[{"name": "A"}]')):
        (cfg / name).write_text(content, encoding="utf-8")
        flags = ["--dry-run"] if dry_run else []
        result = run(*flags, "contacts", "create-bulk", "--file", str(cfg / name))
        assert isinstance(result.exception, ValidationError), result.output
        assert "inside the hfox config directory" in str(result.exception)
        assert result.stdout == ""
    assert captured == []


def test_contacts_create_bulk_file_inside_config_dir_exits_3():
    cfg = Path(os.environ["HFOX_CONFIG_DIR"])
    cfg.mkdir(parents=True)
    (cfg / "token.json").write_text("{}", encoding="utf-8")
    proc = run_app(["contacts", "create-bulk", "--file", str(cfg / "token.json")])
    assert proc.returncode == 3, proc.stdout
    payload = json.loads(proc.stdout)
    assert payload["type"] == "validation"
    assert "Refusing to read" in payload["error"]


def test_contacts_create_bulk_refuses_a_file_holding_credentials(mock_api, tmp_path):
    captured = mock_api(lambda req: httpx.Response(200, json=[]))
    secret = "s3cret-api-key-value"
    f = tmp_path / "contacts.json"
    f.write_text(json.dumps([{"name": secret}]), encoding="utf-8")
    result = run("contacts", "create-bulk", "--file", str(f), env_extra={"HFOX_API_KEY": secret})
    assert isinstance(result.exception, ValidationError), result.output
    assert "contains HappyFox credentials" in str(result.exception)
    assert secret not in str(result.exception)
    assert captured == []


def test_contacts_create_bulk_help_mentions_stdin():
    result = run("contacts", "create-bulk", "--help", env_extra={"COLUMNS": "200"})
    assert "'-' reads stdin." in result.stdout


# -- groups list -----------------------------------------------------------
def test_groups_list_renders(mock_api):
    body = {"data": [{"id": 1, "name": "VIPs"}, {"id": 2, "name": "Beta"}]}
    captured = mock_api(lambda req: httpx.Response(200, json=body))

    result = run("contacts", "groups", "list")

    assert result.exit_code == 0, result.stdout
    assert captured[0].method == "GET"
    assert captured[0].url.path.endswith("/contact_groups/")
    assert json.loads(result.stdout) == body


def test_groups_list_bare_list_body(mock_api):
    # A bare list body exercises render_list's list branch.
    captured = mock_api(lambda req: httpx.Response(200, json=[{"id": 1}]))
    result = run("contacts", "groups", "list")
    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout) == [{"id": 1}]
    assert captured[0].url.path.endswith("/contact_groups/")


# Docs/1092 section 8 example response.
_DOC_GROUPS = [
    {
        "tagged_domains": "example.com",
        "id": 1,
        "name": "test group",
        "description": "example description",
    },
    {"tagged_domains": "", "id": 2, "name": "test1 group", "description": ""},
]


def test_groups_list_name_filter_is_local(mock_api):
    captured = mock_api(lambda req: httpx.Response(200, json=_DOC_GROUPS))

    result = run("contacts", "groups", "list", "--name", "TEST1")
    assert result.exit_code == 0, result.stdout
    assert [g["id"] for g in json.loads(result.stdout)] == [2]

    result = run("contacts", "groups", "list", "--name", "group")
    assert [g["id"] for g in json.loads(result.stdout)] == [1, 2]
    assert all(req.url.query == b"" for req in captured)


def test_groups_list_name_filter_inside_envelope(mock_api):
    body = {"data": [{"id": 1, "name": "VIPs"}, {"id": 2, "name": "Beta"}]}
    mock_api(lambda req: httpx.Response(200, json=body))
    result = run("contacts", "groups", "list", "--name", "vip")
    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout) == {"data": [{"id": 1, "name": "VIPs"}]}


def test_groups_list_name_filter_zero_matches(mock_api):
    mock_api(lambda req: httpx.Response(200, json=_DOC_GROUPS))
    result = run("contacts", "groups", "list", "--name", "zzz")
    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout) == []

    mock_api(lambda req: httpx.Response(200, json={"data": _DOC_GROUPS}))
    result = run("contacts", "groups", "list", "--name", "zzz")
    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout) == {"data": []}


# -- groups get ------------------------------------------------------------
def test_groups_get(mock_api):
    group = {"id": 3, "name": "VIPs", "tagged_domains": "acme.com,foo.com"}
    captured = mock_api(lambda req: httpx.Response(200, json=group))

    result = run("contacts", "groups", "get", "3")

    assert result.exit_code == 0, result.stdout
    assert captured[0].method == "GET"
    assert captured[0].url.path.endswith("/contact_group/3/")
    assert json.loads(result.stdout) == group


# -- groups create ---------------------------------------------------------
def test_groups_create_full_body():
    p = preview(
        "--dry-run", "contacts", "groups", "create",
        "--name", "VIPs", "--description", "important",
        "--domains", "acme.com, foo.com",
    )
    assert p["method"] == "POST"
    assert p["url"].endswith("/contact_groups/")
    assert p["body"]["name"] == "VIPs"
    assert p["body"]["description"] == "important"
    # Sent as the documented comma string, entries trimmed.
    assert p["body"]["tagged_domains"] == "acme.com,foo.com"


def test_groups_create_minimal_omits_optional():
    p = preview("--dry-run", "contacts", "groups", "create", "--name", "Solo")
    assert p["body"]["name"] == "Solo"
    # compact drops None description and None tagged_domains.
    assert "description" not in p["body"]
    assert "tagged_domains" not in p["body"]


# -- groups update ---------------------------------------------------------
def test_groups_update_body():
    p = preview(
        "--dry-run", "contacts", "groups", "update", "3",
        "--description", "new desc", "--domains", "x.com",
    )
    assert p["method"] == "POST"
    assert p["url"].endswith("/contact_group/3/")
    assert p["body"]["description"] == "new desc"
    assert p["body"]["tagged_domains"] == "x.com"


def test_groups_update_empty_body_rejected(mock_api):
    _fails_offline(mock_api, "contacts", "groups", "update", "3", match="Nothing to update")


# -- groups add-contacts ---------------------------------------------------
def test_groups_add_contacts_array_body():
    p = preview(
        "--dry-run", "contacts", "groups", "add-contacts", "3",
        "--contacts", "11,12", "--access-tickets",
    )
    assert p["method"] == "POST"
    assert p["url"].endswith("/contact_group/3/update_contacts/")
    assert p["body"] == [
        {"contact": 11, "access_tickets": True},
        {"contact": 12, "access_tickets": True},
    ]


def test_groups_add_contacts_omits_access_tickets_by_default():
    p = preview(
        "--dry-run", "contacts", "groups", "add-contacts", "5",
        "--contacts", "1",
    )
    assert p["body"] == [{"contact": 1}]


def test_groups_add_contacts_no_access_tickets_applies_to_every_contact():
    p = preview(
        "--dry-run", "contacts", "groups", "add-contacts", "5",
        "--contacts", "1,2", "--no-access-tickets",
    )
    assert p["body"] == [
        {"contact": 1, "access_tickets": False},
        {"contact": 2, "access_tickets": False},
    ]


def test_groups_add_contacts_over_100_is_validation_error(mock_api):
    captured = mock_api(lambda req: httpx.Response(200, json={}))
    ids = ",".join(str(i) for i in range(1, 102))
    result = run("contacts", "groups", "add-contacts", "3", "--contacts", ids)
    assert result.exit_code != 0
    assert isinstance(result.exception, ValidationError)
    assert int(result.exception.exit_code) == 3
    assert "between 1 and 100" in str(result.exception)
    # The cap is enforced before any HTTP request.
    assert captured == []


def test_groups_add_contacts_over_100_exits_3():
    ids = ",".join(str(i) for i in range(1, 102))
    proc = run_app(["contacts", "groups", "add-contacts", "3", "--contacts", ids])
    assert proc.returncode == 3
    assert "between 1 and 100" in json.loads(proc.stdout)["error"]


def test_groups_add_contacts_rejects_non_int():
    result = run(
        "--dry-run", "contacts", "groups", "add-contacts", "3", "--contacts", "abc",
    )
    assert isinstance(result.exception, ValidationError)


# -- live write paths: cover the obj.render(result) lines after a POST ------
# --dry-run short-circuits before render(); these exercise the real (mocked)
# request + render so the trailing render lines are covered too.
def test_contacts_create_live_renders_result(mock_api):
    created = {"id": 101, "name": "Jane"}
    captured = mock_api(lambda req: httpx.Response(200, json=created))
    result = run("contacts", "create", "--name", "Jane", "--email", "j@x.org")
    assert result.exit_code == 0, result.stdout
    assert captured[0].method == "POST"
    assert captured[0].url.path.endswith("/users/")
    assert json.loads(result.stdout) == created


def test_contacts_update_live_renders_result(mock_api):
    updated = {"id": 55, "name": "Jane2"}
    captured = mock_api(lambda req: httpx.Response(200, json=updated))
    result = run("contacts", "update", "55", "--name", "Jane2")
    assert result.exit_code == 0, result.stdout
    assert captured[0].url.path.endswith("/user/55/")
    assert json.loads(result.stdout) == updated


def test_contacts_create_bulk_live_renders_result(mock_api, tmp_path):
    f = tmp_path / "c.json"
    f.write_text(json.dumps([{"name": "A"}]))
    created = [{"email": "a@x.org", "success": True, "id": 1}]
    captured = mock_api(lambda req: httpx.Response(200, json=created))
    result = run("contacts", "create-bulk", "--file", str(f))
    assert result.exit_code == 0, result.stdout
    assert captured[0].method == "POST"
    assert json.loads(result.stdout) == created


def test_groups_create_live_renders_result(mock_api):
    created = {"id": 9, "name": "VIPs"}
    captured = mock_api(lambda req: httpx.Response(200, json=created))
    result = run("contacts", "groups", "create", "--name", "VIPs")
    assert result.exit_code == 0, result.stdout
    assert captured[0].url.path.endswith("/contact_groups/")
    assert json.loads(result.stdout) == created


def test_groups_update_live_renders_result(mock_api):
    updated = {"id": 9, "description": "d"}
    captured = mock_api(lambda req: httpx.Response(200, json=updated))
    result = run("contacts", "groups", "update", "9", "--description", "d")
    assert result.exit_code == 0, result.stdout
    assert captured[0].url.path.endswith("/contact_group/9/")
    assert json.loads(result.stdout) == updated


def test_groups_add_contacts_live_renders_result(mock_api):
    resp = [{"data": {"access_tickets": False, "contact": 11}, "success": True}]
    captured = mock_api(lambda req: httpx.Response(200, json=resp))
    result = run("contacts", "groups", "add-contacts", "3", "--contacts", "11")
    assert result.exit_code == 0, result.stdout
    assert captured[0].url.path.endswith("/contact_group/3/update_contacts/")
    assert json.loads(result.stdout) == resp


def test_groups_remove_contacts_live_renders_result(mock_api):
    resp = [
        {"data": {"message": "Successfully removed contact from group", "contact": 11},
         "success": True},
        {"data": {"message": "Contact not part of the contact group", "contact": 12},
         "success": False},
    ]
    captured = mock_api(lambda req: httpx.Response(200, json=resp))
    result = run("contacts", "groups", "remove-contacts", "3", "--contacts", "11,12")
    assert result.exit_code == 0, result.stdout
    assert captured[0].url.path.endswith("/contact_group/3/delete_contacts/")
    assert json.loads(result.stdout) == resp


# -- groups remove-contacts ------------------------------------------------
def test_groups_remove_contacts_body():
    p = preview(
        "--dry-run", "contacts", "groups", "remove-contacts", "3",
        "--contacts", "11, 12, 13",
    )
    assert p["method"] == "POST"
    assert p["url"].endswith("/contact_group/3/delete_contacts/")
    assert p["body"] == {"contacts": [11, 12, 13]}


# -- validation and request shapes ------------------------------------------
def _fails_offline(mock_api, *args, match):
    """Assert a ValidationError (exit 3) is raised before any request."""
    captured = mock_api(lambda req: httpx.Response(200, json={}))
    result = run(*args)
    assert isinstance(result.exception, ValidationError), result.stdout
    assert int(result.exception.exit_code) == 3
    assert match in str(result.exception)
    assert captured == []


@pytest.mark.parametrize("query", ["", "   "])
def test_contacts_list_blank_query_is_dropped(query):
    p = preview("--dry-run", "contacts", "list", "-q", query)
    assert p["method"] == "GET"
    assert p["url"].endswith("/users/?page=1&size=10")
    assert "q" not in p["params"]


def test_contacts_list_query_sent_unchanged():
    p = preview("--dry-run", "contacts", "list", "-q", "name:adam email:adam@x.com")
    assert p["params"]["q"] == "name:adam email:adam@x.com"


def test_contacts_list_size_and_page_bounds_exit_3():
    for flag, value in (("--size", "51"), ("--size", "0"), ("--page", "0")):
        proc = run_app(["contacts", "list", flag, value])
        assert proc.returncode == 3, (flag, value, proc.stdout)
        assert flag in json.loads(proc.stdout)["error"]


def test_contacts_get_rejects_path_traversal(mock_api):
    _fails_offline(mock_api, "contacts", "get", "5/../../tickets", match="Invalid contact")


def test_contacts_update_rejects_path_traversal(mock_api):
    _fails_offline(
        mock_api, "contacts", "update", "5/../../tickets", "--name", "x",
        match="Invalid contact",
    )


def test_contacts_update_by_email_set_login():
    p = preview("--dry-run", "contacts", "update", "a@x.com", "--no-set-login")
    assert p["method"] == "POST"
    assert p["url"].endswith("/user/a@x.com/")
    assert p["body"] == {"is_login_enabled": "FALSE"}


def test_contacts_create_requires_email_or_phone(mock_api):
    _fails_offline(mock_api, "contacts", "create", "--name", "Jane", match="--email or --phone")


def test_contacts_create_blank_email_and_phone_fail(mock_api):
    _fails_offline(
        mock_api, "contacts", "create", "--name", "Jane", "--email", " ", "--phone", "",
        match="--email or --phone",
    )


def test_contacts_create_blank_email_with_phone_sends_null():
    p = preview(
        "--dry-run", "contacts", "create", "--name", "Jane", "--email", "", "--phone", "555",
    )
    assert p["body"] == {
        "name": "Jane",
        "email": None,
        "phones": [{"type": "o", "number": "555", "is_primary": True}],
    }


def test_contacts_create_blank_name_fails(mock_api):
    _fails_offline(
        mock_api, "contacts", "create", "--name", "  ", "--email", "j@x.org",
        match="--name must not be blank",
    )


def test_contacts_create_repeated_phone_fails(mock_api):
    _fails_offline(
        mock_api, "contacts", "create", "--name", "J", "--phone", "1", "--phone", "2",
        match="one number per call",
    )


def test_contacts_create_phone_type_without_phone_fails(mock_api):
    _fails_offline(
        mock_api, "contacts", "create", "--name", "J", "--email", "j@x.org",
        "--phone-type", "w",
        match="--phone-type requires --phone",
    )


def test_contacts_create_set_login_flags():
    base = ("--dry-run", "contacts", "create", "--name", "J", "--email", "j@x.org")
    assert "is_login_enabled" not in preview(*base)["body"]
    assert preview(*base, "--set-login")["body"]["is_login_enabled"] == "TRUE"
    assert preview(*base, "--no-set-login")["body"]["is_login_enabled"] == "FALSE"


def test_contacts_create_cf_keys_allow_only_contact_prefix(mock_api):
    p = preview(
        "--dry-run", "contacts", "create", "--name", "J", "--email", "j@x.org",
        "--cf", "c-cf-4=VIP", "--cf", "5=[1,2]",
    )
    assert p["body"]["c-cf-4"] == "VIP"
    assert p["body"]["c-cf-5"] == [1, 2]
    _fails_offline(
        mock_api, "contacts", "create", "--name", "J", "--email", "j@x.org",
        "--cf", "ccf-4=x",
        match="Invalid custom-field key",
    )


def test_contacts_create_cf_help_shows_list_syntax():
    result = run("contacts", "create", "--help")
    assert "'<id>=[a,b]'" in result.stdout


def test_contacts_update_primary_is_tri_state():
    base = ("--dry-run", "contacts", "update", "12", "--phone", "999")
    assert preview(*base, "--primary")["body"]["phones"] == [
        {"number": "999", "is_primary": True}
    ]
    assert preview(*base, "--no-primary")["body"]["phones"] == [
        {"number": "999", "is_primary": False}
    ]


def test_contacts_update_phone_id_requires_phone_type(mock_api):
    _fails_offline(
        mock_api, "contacts", "update", "12", "--phone", "999", "--phone-id", "31",
        match="--phone-id requires --phone-type",
    )


def test_contacts_update_phone_options_require_phone(mock_api):
    for extra in (("--phone-id", "31", "--phone-type", "w"), ("--primary",), ("--phone-type", "w")):
        _fails_offline(
            mock_api, "contacts", "update", "12", "--name", "x", *extra,
            match="require --phone",
        )


def test_contacts_update_repeated_phone_fails(mock_api):
    _fails_offline(
        mock_api, "contacts", "update", "12", "--phone", "1", "--phone", "2",
        match="one number per call",
    )


def test_contacts_update_empty_body_fails(mock_api):
    _fails_offline(mock_api, "contacts", "update", "12", match="Nothing to update")


def test_groups_ids_must_be_positive_ints():
    for gid in ("abc", "0", "2/../7"):
        proc = run_app(["contacts", "groups", "remove-contacts", gid, "--contacts", "1"])
        assert proc.returncode == 3, (gid, proc.stdout)
    proc = run_app(["contacts", "groups", "get", "abc"])
    assert proc.returncode == 3


def test_groups_update_blank_domains_clears():
    p = preview("--dry-run", "contacts", "groups", "update", "3", "--domains", "")
    assert p["method"] == "POST"
    assert p["url"].endswith("/contact_group/3/")
    assert p["body"] == {"tagged_domains": ""}


def test_groups_create_blank_name_fails(mock_api):
    _fails_offline(
        mock_api, "contacts", "groups", "create", "--name", " ", match="--name must not be blank"
    )


# -- partial bulk failures exit 1 -------------------------------------------
def test_contacts_create_bulk_failure_exits_1(mock_api, tmp_path):
    f = tmp_path / "c.json"
    f.write_text(json.dumps([{"name": "A", "email": "a@x.org"}, {"email": "b@x.org"}]))
    resp = [
        {"email": "a@x.org", "success": True, "id": 14},
        {"email": "b@x.org", "success": False},
    ]
    mock_api(lambda req: httpx.Response(200, json=resp))
    result = run("contacts", "create-bulk", "--file", str(f))
    assert result.exit_code == 1
    assert json.loads(result.stdout) == resp
    assert "1 of 2 entries failed" in result.stderr


def test_groups_add_contacts_failure_exits_1(mock_api):
    resp = [
        {"data": {"access_tickets": True, "contact": 1}, "success": True},
        {"errors": [{"field": "contact", "errors": ["Select a valid choice."]}], "success": False},
    ]
    mock_api(lambda req: httpx.Response(200, json=resp))
    result = run("contacts", "groups", "add-contacts", "3", "--contacts", "1,200")
    assert result.exit_code == 1
    assert json.loads(result.stdout) == resp
    assert "1 of 2 entries failed" in result.stderr


def test_groups_remove_contacts_missing_contact_exits_1(mock_api):
    resp = [
        {"data": {"message": "Contact not part of the contact group", "contact": 3},
         "success": False},
        {"data": {"message": "Contact does not exist", "contact": 100}, "success": False},
    ]
    mock_api(lambda req: httpx.Response(200, json=resp))
    result = run("contacts", "groups", "remove-contacts", "3", "--contacts", "3,100")
    assert result.exit_code == 1
    assert json.loads(result.stdout) == resp
    assert "1 of 2 entries failed" in result.stderr


def test_groups_remove_contacts_not_in_group_is_benign(mock_api):
    resp = [
        {"data": {"message": "Contact not part of the contact group", "contact": 3},
         "success": False},
    ]
    mock_api(lambda req: httpx.Response(200, json=resp))
    result = run("contacts", "groups", "remove-contacts", "3", "--contacts", "3")
    assert result.exit_code == 0, result.stderr
    assert result.stderr == ""
