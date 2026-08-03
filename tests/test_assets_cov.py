"""Coverage for hfox assets, assets types, and assets custom-fields.

Read paths (list/get) exercised via the mock_api fixture; write request shape
via --dry-run. Also covers the _parse_new_contacts JSON validation branches and
the delete confirmation-abort path.
"""

import json

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


def run(*args, env_extra=None, **kwargs):
    env = dict(ENV)
    if env_extra:
        env.update(env_extra)
    return runner.invoke(cli, list(args), env=env, **kwargs)


def _collection(rows):
    return {
        "page_info": {"page_count": 1, "count": len(rows)},
        "data": rows,
    }


# --------------------------------------------------------------------------- #
# assets list / get  (lines 61-64, 73-75)
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
    # asset_type omitted -> compact drops it; size defaults to the API default 10, page to 1.
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
# assets create  (line 123: POST + render)
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
# _parse_new_contacts validation branches  (lines 31-43)
# --------------------------------------------------------------------------- #
def test_create_new_contact_json_invalid_json(mock_api):
    # Should never reach the network: _parse_new_contacts raises ValidationError.
    # The bare `cli` object surfaces it as a raised exception (exit 1); the JSON
    # error-object + exact exit code 3 contract is exercised by app() elsewhere.
    captured = mock_api(lambda r: httpx.Response(200, json={}))
    result = run(
        "assets", "create",
        "--name", "X", "--display-id", "D",
        "--new-contact-json", "{not json",
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, ValidationError)
    assert "Invalid --new-contact-json" in str(result.exception)
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
# assets update  (line 167-168: PUT + render)
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
# assets delete  (lines 185-186 abort, 187-189 success + render)
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
    # success() message is a stderr-only side channel; body renders to stdout.
    assert "Deleted asset 10." in result.stderr
    assert "Deleted asset" not in result.stdout
    assert json.loads(result.stdout) == {"deleted": True}


def test_assets_delete_confirm_abort_no_network(mock_api):
    # Decline the confirmation: typer.confirm(abort=True) -> exit 1, no request.
    captured = mock_api(lambda r: httpx.Response(200, json={}))
    result = run("assets", "delete", "10", input="n\n")
    assert result.exit_code == 1
    assert captured == []


def test_assets_delete_confirm_yes_prompt(mock_api):
    captured = mock_api(lambda r: httpx.Response(200, json={"ok": 1}))
    result = run("assets", "delete", "10", input="y\n")
    assert result.exit_code == 0, result.stdout
    assert len(captured) == 1
    assert captured[0].method == "DELETE"


# --------------------------------------------------------------------------- #
# asset types  (lines 201-203 list, 212-214 get)
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
# asset custom fields  (lines 238-241 list, 250-252 get)
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
    assert req.url.path.endswith("/asset_custom_fields/5/")
    assert json.loads(result.stdout) == {"id": 5, "name": "RAM"}


# --------------------------------------------------------------------------- #
# require_staff_id failure path (no staff id configured) -> ValidationError
# --------------------------------------------------------------------------- #
def test_assets_create_requires_staff_id(mock_api):
    captured = mock_api(lambda r: httpx.Response(200, json={}))
    # Drop HFOX_STAFF_ID so resolution fails before any request.
    env = {k: v for k, v in ENV.items() if k != "HFOX_STAFF_ID"}
    result = runner.invoke(
        cli, ["assets", "create", "--name", "X", "--display-id", "D"], env=env
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, ValidationError)
    assert "staff id" in str(result.exception).lower()
    assert captured == []
