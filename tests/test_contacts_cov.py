"""Coverage for `hfox contacts` and its `groups` sub-app.

READ commands (list/get) exercise the live request path via the `mock_api`
fixture; WRITE verbs (create/update/create-bulk, group create/update/
add-contacts/remove-contacts) are verified with --dry-run so we assert the
exact method/url/body HappyFox would receive without hitting the network.

Targets src/hfox/cli/contacts.py lines: 37-42, 51-53, 75-85, 121, 133,
144-149, 160-162, 171-173, 186-195, 208-216, 240, 252-255.
"""

import json
import subprocess
import sys

import httpx
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


def run(*args, env_extra=None):
    env = dict(ENV)
    if env_extra:
        env.update(env_extra)
    return runner.invoke(cli, list(args), env=env)


def preview(*args, env_extra=None):
    result = run(*args, env_extra=env_extra)
    assert result.exit_code == 0, result.stdout
    return json.loads(result.stdout)


_INVOKER = (
    "import sys; sys.argv = ['hfox'] + sys.argv[1:]; "
    "from hfox.cli.main import app; app()"
)


def run_app(argv):
    """Run the real `app()` entry point in a subprocess to assert exact exit codes.

    The HfoxError -> JSON-on-stdout + stable exit code mapping lives in `app()`,
    not in the bare `cli` object CliRunner invokes. These cases fail during input
    validation, before any HTTP request, so they stay offline.
    """
    return subprocess.run(
        [sys.executable, "-c", _INVOKER, *argv],
        env=dict(ENV),
        capture_output=True,
        text=True,
    )


# -- contacts list (lines 37-42) -------------------------------------------
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


# -- contacts get (lines 51-53) --------------------------------------------
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


# -- contacts create (lines 75-85) -----------------------------------------
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
        "--name", "NoPhone",
        "--cf-json", '{"7": ["a", "b"]}',
    )
    assert p["body"]["name"] == "NoPhone"
    # No phone and no email -> compact drops them entirely.
    assert "phones" not in p["body"]
    assert "email" not in p["body"]
    # cf-json passes values through verbatim under the c-cf- prefix.
    assert p["body"]["c-cf-7"] == ["a", "b"]


# -- contacts update (lines 121, 133) --------------------------------------
def test_contacts_update_set_login_true_maps_to_TRUE():
    p = preview(
        "--dry-run", "contacts", "update", "55",
        "--name", "Jane", "--set-login",
    )
    assert p["method"] == "POST"
    assert p["url"].endswith("/user/55/")
    assert p["body"]["name"] == "Jane"
    assert p["body"]["is_login_enabled"] == "TRUE"


def test_contacts_update_no_set_login_omits_field():
    p = preview(
        "--dry-run", "contacts", "update", "55",
        "--email", "new@x.org", "--no-set-login",
    )
    # --no-set-login -> FALSE; verifies the False branch of line 121.
    assert p["body"]["is_login_enabled"] == "FALSE"
    assert p["body"]["email"] == "new@x.org"


def test_contacts_update_phone_and_cf():
    p = preview(
        "--dry-run", "contacts", "update", "12",
        "--phone", "999", "--cf", "3=Gold",
    )
    # No --phone-id -> no id key, so HappyFox ADDS a new phone record.
    assert p["body"]["phones"] == [
        {"type": "o", "number": "999", "is_primary": True}
    ]
    assert p["body"]["c-cf-3"] == "Gold"
    # set_login untouched -> omitted.
    assert "is_login_enabled" not in p["body"]


def test_contacts_update_phone_id_and_type_edit_existing_record():
    p = preview(
        "--dry-run", "contacts", "update", "12",
        "--phone", "999", "--phone-id", "77", "--phone-type", "w",
    )
    # With --phone-id the record id is included so HappyFox EDITS that phone.
    assert p["body"]["phones"] == [
        {"id": 77, "type": "w", "number": "999", "is_primary": True}
    ]


def test_contacts_update_bad_phone_type_exits_3():
    proc = run_app(
        ["contacts", "update", "12", "--phone", "999", "--phone-type", "bad"]
    )
    assert proc.returncode == 3
    assert "phone type" in json.loads(proc.stdout)["error"].lower()


# -- contacts create-bulk (lines 144-149) ----------------------------------
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
    result = run("contacts", "create-bulk", "--file", str(f))
    # A non-array bulk file raises ValidationError before any HTTP call.
    assert result.exit_code != 0
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


# -- groups list (lines 160-162) -------------------------------------------
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


# -- groups get (lines 171-173) --------------------------------------------
def test_groups_get(mock_api):
    group = {"id": 3, "name": "VIPs", "tagged_domains": ["acme.com"]}
    captured = mock_api(lambda req: httpx.Response(200, json=group))

    result = run("contacts", "groups", "get", "3")

    assert result.exit_code == 0, result.stdout
    assert captured[0].method == "GET"
    assert captured[0].url.path.endswith("/contact_group/3/")
    assert json.loads(result.stdout) == group


# -- groups create (lines 186-195) -----------------------------------------
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
    # split_csv trims whitespace per entry.
    assert p["body"]["tagged_domains"] == ["acme.com", "foo.com"]


def test_groups_create_minimal_omits_optional():
    p = preview("--dry-run", "contacts", "groups", "create", "--name", "Solo")
    assert p["body"]["name"] == "Solo"
    # compact drops None description and None tagged_domains.
    assert "description" not in p["body"]
    assert "tagged_domains" not in p["body"]


# -- groups update (lines 208-216) -----------------------------------------
def test_groups_update_body():
    p = preview(
        "--dry-run", "contacts", "groups", "update", "3",
        "--description", "new desc", "--domains", "x.com",
    )
    assert p["method"] == "POST"
    assert p["url"].endswith("/contact_group/3/")
    assert p["body"]["description"] == "new desc"
    assert p["body"]["tagged_domains"] == ["x.com"]


def test_groups_update_empty_body():
    p = preview("--dry-run", "contacts", "groups", "update", "3")
    # Nothing supplied -> compact yields an empty body.
    assert p["body"] is None or p["body"] == {}


# -- groups add-contacts (line 240) ----------------------------------------
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


def test_groups_add_contacts_default_no_access():
    p = preview(
        "--dry-run", "contacts", "groups", "add-contacts", "5",
        "--contacts", "1",
    )
    assert p["body"] == [{"contact": 1, "access_tickets": False}]


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
        "contacts", "groups", "add-contacts", "3", "--contacts", "abc",
    )
    # split_csv_ints rejects non-integers -> ValidationError.
    assert result.exit_code != 0
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
    created = [{"id": 1, "name": "A"}]
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
    resp = {"updated": 1}
    captured = mock_api(lambda req: httpx.Response(200, json=resp))
    result = run("contacts", "groups", "add-contacts", "3", "--contacts", "11")
    assert result.exit_code == 0, result.stdout
    assert captured[0].url.path.endswith("/contact_group/3/update_contacts/")
    assert json.loads(result.stdout) == resp


def test_groups_remove_contacts_live_renders_result(mock_api):
    resp = {"removed": 2}
    captured = mock_api(lambda req: httpx.Response(200, json=resp))
    result = run("contacts", "groups", "remove-contacts", "3", "--contacts", "11,12")
    assert result.exit_code == 0, result.stdout
    assert captured[0].url.path.endswith("/contact_group/3/delete_contacts/")
    assert json.loads(result.stdout) == resp


# -- groups remove-contacts (lines 252-255) --------------------------------
def test_groups_remove_contacts_body():
    p = preview(
        "--dry-run", "contacts", "groups", "remove-contacts", "3",
        "--contacts", "11, 12, 13",
    )
    assert p["method"] == "POST"
    assert p["url"].endswith("/contact_group/3/delete_contacts/")
    assert p["body"] == {"contacts": [11, 12, 13]}
