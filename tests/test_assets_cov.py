"""Coverage for hfox assets, asset types and asset custom fields."""

import json
import subprocess
import sys

import httpx
import pytest
from conftest import subprocess_env
from typer.testing import CliRunner

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


def run(*args, env_extra=None, **kwargs):
    env = dict(ENV)
    if env_extra:
        env.update(env_extra)
    return runner.invoke(cli, list(args), env=env, **kwargs)


_INVOKER = (
    "import sys; sys.argv = ['hfox'] + sys.argv[1:]; "
    "from hfox.cli.main import app; app()"
)


def run_app(argv):
    """Run the real `app()` entry point; --dry-run keeps a regression off the network."""
    return subprocess.run(
        [sys.executable, "-c", _INVOKER, "--dry-run", *argv],
        env=subprocess_env(**ENV),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )


def _collection(rows):
    return {
        "page_info": {"page_count": 1, "count": len(rows)},
        "data": rows,
    }


# --------------------------------------------------------------------------- #
# assets list / get
# --------------------------------------------------------------------------- #
def test_assets_list_get_request_and_params(mock_api):
    def handler(request):
        return httpx.Response(200, json=_collection([{"id": 7, "name": "MBP"}]))

    captured = mock_api(handler)
    result = run("assets", "list", "--asset-type", "3", "--page", "2", "--size", "10")
    assert result.exit_code == 0, result.stdout

    assert len(captured) == 1
    req = captured[0]
    assert req.method == "GET"
    assert req.url.path.endswith("/assets/")
    params = dict(req.url.params)
    assert params["asset_type"] == "3"
    assert params["page"] == "2"
    assert params["size"] == "10"

    body = json.loads(result.stdout)
    assert body["data"][0]["id"] == 7


def test_assets_list_default_size_10_no_asset_type(mock_api):
    def handler(request):
        return httpx.Response(200, json=_collection([]))

    captured = mock_api(handler)
    result = run("assets", "list")
    assert result.exit_code == 0, result.stdout
    params = dict(captured[0].url.params)
    assert "asset_type" not in params
    assert params["size"] == "10"
    assert params["page"] == "1"

    body = json.loads(result.stdout)
    assert body["data"] == []


def test_assets_get_single_resource(mock_api):
    def handler(request):
        return httpx.Response(200, json={"id": 42, "name": "Laptop"})

    captured = mock_api(handler)
    result = run("assets", "get", "42")
    assert result.exit_code == 0, result.stdout

    req = captured[0]
    assert req.method == "GET"
    assert req.url.path.endswith("/asset/42/")
    body = json.loads(result.stdout)
    assert body == {"id": 42, "name": "Laptop"}


# --------------------------------------------------------------------------- #
# assets create
# --------------------------------------------------------------------------- #
def test_assets_create_posts_and_renders(mock_api):
    def handler(request):
        assert request.method == "POST"
        return httpx.Response(200, json={"id": 99})

    captured = mock_api(handler)
    result = run(
        "assets", "create",
        "--asset-type", "1", "--name", "MBP", "--display-id", "L-1",
        "--contact-ids", "5,6",
    )
    assert result.exit_code == 0, result.stdout

    req = captured[0]
    assert req.method == "POST"
    assert req.url.path.endswith("/assets/")
    assert dict(req.url.params)["asset_type"] == "1"
    sent = json.loads(req.content)
    assert sent["name"] == "MBP"
    assert sent["display_id"] == "L-1"
    assert sent["created_by"] == 1
    assert sent["contact_ids"] == [5, 6]

    rendered = json.loads(result.stdout)
    assert rendered == {"id": 99}


def test_assets_create_new_contact_json_sent_as_contacts(mock_api):
    def handler(request):
        return httpx.Response(200, json={"id": 1})

    captured = mock_api(handler)
    result = run(
        "assets", "create",
        "--name", "X", "--display-id", "D",
        "--new-contact-json", '[{"name": "Jane", "email": "j@x.org"}]',
    )
    assert result.exit_code == 0, result.stdout
    sent = json.loads(captured[0].content)
    assert sent["contacts"] == [{"name": "Jane", "email": "j@x.org"}]


# --------------------------------------------------------------------------- #
# _parse_new_contacts validation branches
# --------------------------------------------------------------------------- #
def test_create_new_contact_json_invalid_json(mock_api):
    captured = mock_api(lambda r: httpx.Response(200, json={}))
    result = run(
        "assets", "create",
        "--name", "X", "--display-id", "D",
        "--new-contact-json", "{not json",
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, ValidationError)
    assert "Invalid JSON in --new-contact-json" in str(result.exception)
    assert captured == []


def test_create_new_contact_json_not_a_list(mock_api):
    captured = mock_api(lambda r: httpx.Response(200, json={}))
    result = run(
        "assets", "create",
        "--name", "X", "--display-id", "D",
        "--new-contact-json", '{"name": "Jane"}',
    )
    assert isinstance(result.exception, ValidationError)
    assert "expected a JSON array" in str(result.exception)
    assert captured == []


def test_create_new_contact_json_element_not_object(mock_api):
    captured = mock_api(lambda r: httpx.Response(200, json={}))
    result = run(
        "assets", "create",
        "--name", "X", "--display-id", "D",
        "--new-contact-json", '[1, 2, 3]',
    )
    assert isinstance(result.exception, ValidationError)
    assert "each array element must be a JSON object" in str(result.exception)
    assert captured == []


# --------------------------------------------------------------------------- #
# assets update
# --------------------------------------------------------------------------- #
def test_assets_update_puts_and_renders(mock_api):
    def handler(request):
        assert request.method == "PUT"
        return httpx.Response(200, json={"id": 10, "name": "New"})

    captured = mock_api(handler)
    result = run(
        "assets", "update", "10",
        "--name", "New", "--display-id", "L-9", "--cf", "5=4",
    )
    assert result.exit_code == 0, result.stdout

    req = captured[0]
    assert req.method == "PUT"
    assert req.url.path.endswith("/asset/10/")
    sent = json.loads(req.content)
    assert sent["name"] == "New"
    assert sent["updated_by"] == 1
    assert sent["custom_fields"] == {"5": 4}

    assert json.loads(result.stdout) == {"id": 10, "name": "New"}


# --------------------------------------------------------------------------- #
# assets delete
# --------------------------------------------------------------------------- #
def test_assets_delete_with_yes_renders_and_succeeds(mock_api):
    def handler(request):
        assert request.method == "DELETE"
        return httpx.Response(200, json={"deleted": True})

    captured = mock_api(handler)
    result = run("assets", "delete", "10", "--yes")
    assert result.exit_code == 0, result.stdout

    req = captured[0]
    assert req.method == "DELETE"
    assert req.url.path.endswith("/asset/10/")
    assert dict(req.url.params)["deleted_by"] == "1"
    assert "Deleted asset 10." in result.stderr
    assert "Deleted asset" not in result.stdout
    assert json.loads(result.stdout) == {"deleted": True}


@pytest.mark.parametrize("answer", ["n\n", "\n", ""])
def test_assets_delete_confirm_abort_no_network(mock_api, tty, answer):
    # A decline, the default answer and EOF all cancel before any request.
    captured = mock_api(lambda r: httpx.Response(200, json={}))
    result = run("assets", "delete", "10", input=answer)
    assert isinstance(result.exception, CancelledError), result.output
    assert int(result.exception.exit_code) == 5
    assert captured == []
    assert result.stdout == ""


def test_assets_delete_without_yes_needs_a_terminal(mock_api):
    captured = mock_api(lambda r: httpx.Response(200, json={}))
    result = run("assets", "delete", "10", input="y\n")
    assert isinstance(result.exception, ValidationError), result.output
    assert "Delete asset 10?" in str(result.exception)
    assert "--yes" in str(result.exception)
    assert "Delete asset 10?" not in result.stderr
    assert captured == []


def test_assets_delete_dry_run_never_prompts():
    result = run("--dry-run", "assets", "delete", "10")
    assert result.exit_code == 0, result.output
    p = json.loads(result.stdout)
    assert p["method"] == "DELETE"
    assert p["url"].endswith("/asset/10/?deleted_by=1")
    assert "Delete asset 10?" not in result.stderr


def test_assets_delete_resolves_staff_before_the_prompt(mock_api, tty):
    captured = mock_api(lambda r: httpx.Response(200, json={}))
    env = {k: v for k, v in ENV.items() if k != "HFOX_STAFF_ID"}
    result = runner.invoke(cli, ["assets", "delete", "10"], env=env, input="y\n")
    assert isinstance(result.exception, ValidationError), result.output
    assert "staff identity" in str(result.exception)
    assert "Delete asset 10?" not in result.stderr
    assert captured == []


def test_assets_delete_confirm_yes_prompt(mock_api, tty):
    captured = mock_api(lambda r: httpx.Response(200, json={"ok": 1}))
    result = run("assets", "delete", "10", input="y\n")
    assert result.exit_code == 0, result.stdout
    assert len(captured) == 1
    assert captured[0].method == "DELETE"
    assert "Delete asset 10?" in result.stderr
    assert json.loads(result.stdout) == {"ok": 1}


# --------------------------------------------------------------------------- #
# asset types
# --------------------------------------------------------------------------- #
def test_asset_types_list(mock_api):
    def handler(request):
        return httpx.Response(200, json=_collection([{"id": 1, "name": "Laptops"}]))

    captured = mock_api(handler)
    result = run("assets", "types", "list")
    assert result.exit_code == 0, result.stdout

    req = captured[0]
    assert req.method == "GET"
    assert req.url.path.endswith("/asset_types/")
    body = json.loads(result.stdout)
    assert body["data"][0]["name"] == "Laptops"


def test_asset_types_list_sends_default_size_and_page(mock_api):
    captured = mock_api(lambda request: httpx.Response(200, json=_collection([])))
    result = run("assets", "types", "list")
    assert result.exit_code == 0, result.stdout
    assert dict(captured[0].url.params) == {"size": "10", "page": "1"}


def test_dry_run_types_list_sends_size_and_page():
    p = dry("assets", "types", "list", "--page", "2", "--size", "50")
    assert p["method"] == "GET"
    assert p["url"].endswith("/asset_types/?size=50&page=2")
    assert p["body"] is None


def test_types_list_size_over_50_exits_3():
    proc = run_app(["assets", "types", "list", "--size", "51"])
    assert proc.returncode == 3, proc.stdout
    payload = json.loads(proc.stdout)
    assert payload["type"] == "validation"
    assert "--size" in payload["error"]


def test_dry_run_page_all_previews_the_first_wire_request(mock_api):
    result = run("--dry-run", "--page-all", "assets", "types", "list")
    assert result.exit_code == 0, result.stdout
    preview = json.loads(result.stdout)
    assert preview["url"].endswith("/asset_types/?size=10&page=1")

    captured = mock_api(lambda request: httpx.Response(200, json=_collection([])))
    result = run("--page-all", "assets", "types", "list")
    assert result.exit_code == 0, result.stdout
    assert str(captured[0].url) == preview["url"]


def test_asset_types_get(mock_api):
    def handler(request):
        return httpx.Response(200, json={"id": 4, "name": "Phones"})

    captured = mock_api(handler)
    result = run("assets", "types", "get", "4")
    assert result.exit_code == 0, result.stdout

    req = captured[0]
    assert req.method == "GET"
    assert req.url.path.endswith("/asset_type/4/")
    assert json.loads(result.stdout) == {"id": 4, "name": "Phones"}


# --------------------------------------------------------------------------- #
# asset custom fields
# --------------------------------------------------------------------------- #
def test_asset_custom_fields_list(mock_api):
    def handler(request):
        return httpx.Response(200, json=_collection([{"id": 5, "name": "RAM"}]))

    captured = mock_api(handler)
    result = run(
        "assets", "custom-fields", "list", "--asset-type", "2", "--size", "20", "--page", "3"
    )
    assert result.exit_code == 0, result.stdout

    req = captured[0]
    assert req.method == "GET"
    assert req.url.path.endswith("/asset_custom_fields/")
    params = dict(req.url.params)
    assert params["asset_type"] == "2"
    assert params["size"] == "20"
    assert params["page"] == "3"
    body = json.loads(result.stdout)
    assert body["data"][0]["name"] == "RAM"


def test_asset_custom_fields_get(mock_api):
    def handler(request):
        return httpx.Response(200, json={"id": 5, "name": "RAM"})

    captured = mock_api(handler)
    result = run("assets", "custom-fields", "get", "5")
    assert result.exit_code == 0, result.stdout

    req = captured[0]
    assert req.method == "GET"
    assert req.url.path.endswith("/asset_custom_field/5/")
    assert json.loads(result.stdout) == {"id": 5, "name": "RAM"}


# --------------------------------------------------------------------------- #
# no staff id configured
# --------------------------------------------------------------------------- #
def test_assets_create_requires_staff_id(mock_api):
    captured = mock_api(lambda r: httpx.Response(200, json={}))
    env = {k: v for k, v in ENV.items() if k != "HFOX_STAFF_ID"}
    result = runner.invoke(
        cli, ["assets", "create", "--name", "X", "--display-id", "D"], env=env
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, ValidationError)
    assert "staff identity" in str(result.exception)
    assert captured == []


# --------------------------------------------------------------------------- #
# --staff and --staff-id
# --------------------------------------------------------------------------- #
STAFF = [
    {"id": 7, "name": "Ada Lovelace", "email": "ada@x.org", "active": True},
    {"id": 8, "name": "Bob Jones", "email": "bob@x.org", "active": True},
]

STAFF_VERBS = [
    ("create", ("create", "--name", "X", "--display-id", "D")),
    ("update", ("update", "10", "--name", "X")),
    ("delete", ("delete", "10", "--yes")),
]
_STAFF_IDS = [verb for verb, _ in STAFF_VERBS]


def _staff_api(mock_api):
    def handler(request):
        if request.url.path.endswith("/staff/"):
            return httpx.Response(200, json=STAFF)
        return httpx.Response(200, json={"ok": True})

    return mock_api(handler)


def _sent_staff(request):
    """Return the acting staff id a write carried, from its body or its query."""
    if request.method == "DELETE":
        return int(request.url.params["deleted_by"])
    body = json.loads(request.content)
    return body["created_by" if request.method == "POST" else "updated_by"]


@pytest.mark.parametrize("verb, argv", STAFF_VERBS, ids=_STAFF_IDS)
@pytest.mark.parametrize("text, staff_id", [("ADA@x.org", 7), ("bob jones", 8)])
def test_assets_staff_by_email_or_name(mock_api, verb, argv, text, staff_id):
    captured = _staff_api(mock_api)
    result = run("assets", *argv, "--staff", text)
    assert result.exit_code == 0, result.output
    assert [req.method for req in captured][0] == "GET"
    assert captured[0].url.path.endswith("/staff/")
    assert len(captured) == 2
    assert _sent_staff(captured[1]) == staff_id


@pytest.mark.parametrize("verb, argv", STAFF_VERBS, ids=_STAFF_IDS)
def test_assets_staff_id_flag_beats_the_default(mock_api, verb, argv):
    captured = _staff_api(mock_api)
    result = run("assets", *argv, "--staff-id", "42")
    assert result.exit_code == 0, result.output
    assert len(captured) == 1
    assert _sent_staff(captured[0]) == 42


def test_assets_staff_lookup_runs_under_dry_run(mock_api):
    captured = _staff_api(mock_api)
    result = run("--dry-run", "assets", "delete", "10", "--staff", "ada@x.org")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["url"].endswith("/asset/10/?deleted_by=7")
    assert [req.url.path.rsplit("/", 2)[-2] for req in captured] == ["staff"]


@pytest.mark.parametrize("verb, argv", STAFF_VERBS, ids=_STAFF_IDS)
def test_assets_staff_and_staff_id_are_exclusive(mock_api, verb, argv):
    captured = _staff_api(mock_api)
    result = run("assets", *argv, "--staff", "ada@x.org", "--staff-id", "7")
    assert isinstance(result.exception, ValidationError), result.output
    assert str(result.exception) == "--staff and --staff-id are mutually exclusive."
    assert captured == []


@pytest.mark.parametrize("verb, argv", STAFF_VERBS, ids=_STAFF_IDS)
def test_assets_unknown_staff_sends_no_write(mock_api, verb, argv):
    captured = _staff_api(mock_api)
    result = run("assets", *argv, "--staff", "nobody@x.org")
    assert isinstance(result.exception, ValidationError), result.output
    assert "No staff member matches" in str(result.exception)
    assert [req.method for req in captured] == ["GET"]


@pytest.mark.parametrize("verb, argv", STAFF_VERBS, ids=_STAFF_IDS)
def test_assets_numeric_staff_is_rejected(mock_api, verb, argv):
    captured = _staff_api(mock_api)
    result = run("assets", *argv, "--staff", "7")
    assert isinstance(result.exception, ValidationError), result.output
    assert "--staff-id 7" in str(result.exception)
    assert captured == []


@pytest.mark.parametrize("verb, argv", STAFF_VERBS, ids=_STAFF_IDS)
def test_assets_negative_staff_id_exits_3(verb, argv):
    proc = run_app(["assets", *argv, "--staff-id", "-1"])
    assert proc.returncode == 3, proc.stdout
    payload = json.loads(proc.stdout)
    assert payload["type"] == "validation"
    assert "--staff-id" in payload["error"]


@pytest.mark.parametrize(
    "argv, flag",
    [
        (STAFF_VERBS[0][1], "--created-by"),
        (STAFF_VERBS[1][1], "--updated-by"),
        (STAFF_VERBS[2][1], "--deleted-by"),
    ],
    ids=_STAFF_IDS,
)
def test_assets_removed_staff_flags_are_usage_errors(argv, flag):
    proc = run_app(["assets", *argv, flag, "1"])
    assert proc.returncode == 3, proc.stdout
    payload = json.loads(proc.stdout)
    assert payload["type"] == "usage"
    assert flag in payload["error"]


# --------------------------------------------------------------------------- #
# Dry-run request shapes and client-side validation
# --------------------------------------------------------------------------- #
def dry(*args):
    result = run("--dry-run", *args)
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def test_dry_run_create_sends_contact_group_ids():
    p = dry(
        "assets", "create", "--asset-type", "2", "--name", "MBP", "--display-id", "L-1",
        "--contact-ids", "5", "--contact-group-ids", "1, 3",
    )
    assert p["method"] == "POST"
    assert p["url"].endswith("/assets/?asset_type=2")
    assert p["body"] == {
        "name": "MBP",
        "display_id": "L-1",
        "created_by": 1,
        "contact_ids": [5],
        "contact_group_ids": [1, 3],
    }


def test_dry_run_update_sends_contact_group_ids():
    p = dry("assets", "update", "10", "--contact-group-ids", "4")
    assert p["method"] == "PUT"
    assert p["url"].endswith("/asset/10/")
    assert p["body"] == {"updated_by": 1, "contact_group_ids": [4]}


def test_dry_run_custom_field_get_uses_singular_path():
    p = dry("assets", "custom-fields", "get", "5")
    assert p["method"] == "GET"
    assert p["url"].endswith("/asset_custom_field/5/")
    assert p["body"] is None


def test_dry_run_cf_json_keeps_null_and_bare_ids():
    p = dry(
        "assets", "update", "10", "--cf", "6=[3,4]", "--cf-json", '{"7": null, "8": "02"}',
    )
    assert p["body"]["custom_fields"] == {"6": [3, 4], "7": None, "8": "02"}


def test_dry_run_blank_new_contact_json_is_omitted():
    p = dry("assets", "update", "10", "--name", "X", "--new-contact-json", "[]")
    assert p["body"] == {"updated_by": 1, "name": "X"}


def test_dry_run_list_sends_size_and_page():
    p = dry("assets", "list", "--size", "50", "--page", "2")
    assert p["method"] == "GET"
    assert p["url"].endswith("/assets/?size=50&page=2")


@pytest.mark.parametrize(
    "args",
    [
        ("assets", "get", "0"),
        ("assets", "update", "0", "--name", "x"),
        ("assets", "delete", "-y", "--", "-1"),
        ("assets", "types", "get", "0"),
        ("assets", "custom-fields", "get", "0"),
        ("assets", "list", "--asset-type", "0"),
        ("assets", "list", "--size", "51"),
        ("assets", "list", "--size", "0"),
        ("assets", "list", "--page", "0"),
        ("assets", "custom-fields", "list", "--size", "100"),
        ("assets", "custom-fields", "list", "--page", "-1"),
        ("assets", "types", "list", "--size", "51"),
        ("assets", "types", "list", "--size", "0"),
        ("assets", "types", "list", "--page", "0"),
    ],
)
def test_out_of_range_ids_and_paging_are_usage_errors(mock_api, args):
    captured = mock_api(lambda r: httpx.Response(200, json={}))
    result = run(*args)
    assert result.exit_code == 2  # click usage error; main.app() maps it to 3
    assert "range" in result.output
    assert captured == []


@pytest.mark.parametrize(
    "args, message",
    [
        (("create", "--name", "  ", "--display-id", "D"), "--name must not be blank"),
        (("create", "--name", "X", "--display-id", ""), "--display-id must not be blank"),
        (("create", "--name", "x" * 201, "--display-id", "D"), "200 characters"),
        (("update", "10", "--name", ""), "--name must not be blank"),
        (("update", "10", "--display-id", " "), "--display-id must not be blank"),
        (("create", "--name", "X", "--display-id", "D", "--cf-json", '{"t-cf-5": 1}'), "t-cf-5"),
        (("create", "--name", "X", "--display-id", "D", "--contact-group-ids", "a"), "integers"),
        (("create", "--name", "X", "--display-id", "D", "--contact-ids", " , "), "--contact-ids"),
        (("update", "10", "--new-contact-json", "[NaN]"), "--new-contact-json"),
        (("update", "10"), "Nothing to update"),
        (("update", "10", "--new-contact-json", "[]"), "Nothing to update"),
        (("update", "10", "--cf-json", "{}"), "Nothing to update"),
    ],
)
def test_invalid_input_raises_before_request(mock_api, args, message):
    captured = mock_api(lambda r: httpx.Response(200, json={}))
    result = run("assets", *args)
    assert isinstance(result.exception, ValidationError), result.output
    assert message in str(result.exception)
    assert captured == []


@pytest.mark.parametrize(
    "flag, value, key",
    [("--contact-ids", "", "contact_ids"), ("--contact-group-ids", " , ", "contact_group_ids")],
)
def test_update_blank_id_list_sends_empty_list(flag, value, key):
    p = dry("assets", "update", "2", flag, value)
    assert p["method"] == "PUT"
    assert p["url"].endswith("/asset/2/")
    assert p["body"] == {"updated_by": 1, key: []}


def test_create_accepts_200_character_name():
    p = dry("assets", "create", "--name", "x" * 200, "--display-id", "D")
    assert len(p["body"]["name"]) == 200


@pytest.mark.parametrize("verb", ["create", "update"])
def test_assets_cf_help_shows_list_syntax(verb):
    result = run("assets", verb, "--help")
    assert "'<id>=[a,b]'" in result.stdout


# --------------------------------------------------------------------------- #
# local --name filter
# --------------------------------------------------------------------------- #
LISTINGS = [
    (("assets", "list"), "/assets/"),
    (("assets", "types", "list"), "/asset_types/"),
    (("assets", "custom-fields", "list"), "/asset_custom_fields/"),
]
_NAMED_PAGE_INFO = {"page_count": 2, "count": 3}


def _named_pages(request):
    page = request.url.params.get("page", "1")
    rows = (
        [{"id": 1, "name": "Dell Latitude"}, {"id": 2, "name": "MacBook"}]
        if page == "1"
        else [{"id": 3, "name": "dell optiplex"}]
    )
    return httpx.Response(200, json={"page_info": _NAMED_PAGE_INFO, "data": rows})


@pytest.mark.parametrize("dry_run", [True, False])
@pytest.mark.parametrize("argv, path", LISTINGS)
def test_asset_listing_name_filter_requires_page_all(mock_api, argv, path, dry_run):
    captured = mock_api(_named_pages)
    flags = ["--dry-run"] if dry_run else []
    result = run(*flags, *argv, "--name", "dell")
    assert isinstance(result.exception, ValidationError), result.output
    assert "global --page-all before the resource" in str(result.exception)
    assert captured == []
    assert result.stdout == ""


@pytest.mark.parametrize("argv, path", LISTINGS)
def test_asset_listing_name_filter_page_all_ndjson(mock_api, argv, path):
    captured = mock_api(_named_pages)
    result = run("--page-all", *argv, "--name", "DELL")
    assert result.exit_code == 0, result.stdout
    lines = [json.loads(line) for line in result.stdout.splitlines()]
    assert [[r["id"] for r in line["data"]] for line in lines] == [[1], [3]]
    assert all(line["page_info"] == _NAMED_PAGE_INFO for line in lines)
    assert all(req.url.path.endswith(path) for req in captured)
    assert all("name" not in dict(req.url.params) for req in captured)


@pytest.mark.parametrize("argv, path", LISTINGS)
def test_asset_listing_name_filter_zero_matches(mock_api, argv, path):
    mock_api(_named_pages)
    result = run("--page-all", *argv, "--name", "zzz")
    assert result.exit_code == 0, result.stdout
    lines = [json.loads(line) for line in result.stdout.splitlines()]
    assert len(lines) == 2
    assert all(line == {"page_info": _NAMED_PAGE_INFO, "data": []} for line in lines)

    result = run("-f", "csv", "--page-all", *argv, "--name", "zzz")
    assert result.exit_code == 0, result.stdout
    assert result.stdout == ""


def test_asset_listing_name_filter_page_all_csv(mock_api):
    mock_api(_named_pages)
    result = run("-f", "csv", "--page-all", "assets", "list", "--name", "dell")
    assert result.exit_code == 0, result.stdout
    assert result.stdout.splitlines() == ["id,name", "1,Dell Latitude", "3,dell optiplex"]
