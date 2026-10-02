"""AppContext edges: dry-run previews, writes, the client lifecycle, staff and prompts,
page walks, rendering and stdin.
"""

import base64
import email
import getpass
import io
import json
import re
import sys

import httpx
import pytest
import typer
import yaml
from typer.testing import CliRunner

import hfox.cli.context as context_mod
from hfox.cli.context import AppContext
from hfox.cli.main import cli
from hfox.cli.output import OutputFormat
from hfox.core.client import HappyFoxClient
from hfox.core.config import Config
from hfox.core.errors import ValidationError

runner = CliRunner()

ENV = {
    "HFOX_SUBDOMAIN": "acme",
    "HFOX_REGION": "us",
    "HFOX_API_KEY": "k",
    "HFOX_AUTH_CODE": "c",
    "HFOX_STAFF_ID": "1",
}

BASE = "https://acme.happyfox.com/api/1.1/json"

STAFF = [
    {"id": 1, "name": "Alice Smith", "email": "alice@x.org"},
    {"id": 2, "name": "Bob Jones", "email": "bob@x.org"},
]


def make_ctx(**kw):
    return AppContext(config=Config(subdomain="acme", api_key="k", auth_code="c"), **kw)


def run(*args):
    return runner.invoke(cli, list(args), env=ENV)


def dry_run_preview(capsys, **call_kwargs):
    with pytest.raises(typer.Exit) as exc:
        make_ctx(dry_run=True).call("POST", "tickets/", **call_kwargs)
    assert exc.value.exit_code == 0
    return json.loads(capsys.readouterr().out)


def install_client(monkeypatch, handler, *, sleeps=None, built=None):
    """Patch the client factory; record its sleeps and the httpx clients it builds."""

    def factory(base_url, api_key, auth_code, **kwargs):
        sleep = (lambda _s: None) if sleeps is None else sleeps.append
        client = HappyFoxClient(base_url, api_key, auth_code, sleep=sleep)
        client._client = httpx.Client(transport=httpx.MockTransport(handler))
        if built is not None:
            built.append(client._client)
        return client

    monkeypatch.setattr(context_mod, "HappyFoxClient", factory)


def paged(key, pages):
    """Return a handler serving `pages` as envelopes whose rows sit under `key`."""

    def handler(request):
        page = int(request.url.params.get("page", "1"))
        return httpx.Response(
            200, json={"page_info": {"page_count": len(pages)}, key: pages[page - 1]}
        )

    return handler


def ndjson(text):
    return [json.loads(line) for line in text.splitlines()]


# -- dry-run preview ----------------------------------------------------------
def test_dry_run_of_a_bare_get_previews_null_params_body_and_attachments():
    result = run("--dry-run", "tickets", "get", "5")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "dry_run": True,
        "method": "GET",
        "url": f"{BASE}/ticket/5/",
        "params": None,
        "body": None,
        "attachments": None,
    }


@pytest.mark.parametrize("flags", [(), ("--page-all",)], ids=["one-page", "page-all"])
def test_dry_run_of_a_listing_needs_no_credentials(flags):
    result = runner.invoke(
        cli, ["--dry-run", *flags, "tickets", "list"], env={"HFOX_SUBDOMAIN": "acme"}
    )
    assert result.exit_code == 0, result.output
    preview = json.loads(result.stdout)
    assert (preview["method"], preview["url"].partition("?")[0]) == ("GET", f"{BASE}/tickets/")


@pytest.mark.parametrize("body", [{}, []], ids=["object", "array"])
def test_dry_run_previews_an_empty_json_body_as_sent(capsys, body):
    assert dry_run_preview(capsys, json=body)["body"] == body


def test_dry_run_lists_every_attachment_in_order(tmp_path):
    first = tmp_path / "b.txt"
    second = tmp_path / "a.png"
    first.write_text("one", encoding="utf-8")
    second.write_bytes(b"\x89PNG")
    result = run(
        "--dry-run", "tickets", "reply", "5", "--text", "see attached",
        "--attachment", str(first), "--attachment", str(second),
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["attachments"] == {
        "count": 2,
        "fields": [
            {"field": "attachments", "filename": "b.txt"},
            {"field": "attachments", "filename": "a.png"},
        ],
    }


@pytest.mark.parametrize(
    ("files", "fields"),
    [
        ({"file": ("shot.png", b"\x89PNG")}, [{"field": "file", "filename": "shot.png"}]),
        ([("file", b"\x89PNG")], [{"field": "file", "filename": None}]),
    ],
    ids=["mapping", "part-without-filename"],
)
def test_dry_run_summarizes_the_other_files_shapes_httpx_takes(capsys, files, fields):
    preview = dry_run_preview(capsys, files=files)
    assert preview["attachments"] == {"count": 1, "fields": fields}


# -- writes -------------------------------------------------------------------
def test_reply_with_attachment_sends_the_form_fields_beside_the_file(mock_api, tmp_path):
    f = tmp_path / "log.txt"
    f.write_bytes(b"hello")
    captured = mock_api(lambda req: httpx.Response(200, json={"id": 5}))
    result = run("tickets", "reply", "5", "--text", "see attached", "--attachment", str(f))
    assert result.exit_code == 0, result.output
    (request,) = captured
    assert (request.method, str(request.url)) == ("POST", f"{BASE}/ticket/5/staff_update/")
    content_type = request.headers["content-type"]
    assert content_type.startswith("multipart/form-data")
    message = email.message_from_bytes(
        b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + request.content
    )
    parts = {
        part.get_param("name", header="content-disposition"): (
            part.get_filename(),
            part.get_payload(decode=True),
        )
        for part in message.get_payload()
    }
    assert parts == {
        "staff": (None, b"1"),
        "plaintext": (None, b"see attached"),
        "attachments": ("log.txt", b"hello"),
    }


# -- client lifecycle ---------------------------------------------------------
def test_command_leaves_no_http_client_open(monkeypatch):
    def handler(request):
        if request.url.path.endswith("/staff/"):
            return httpx.Response(200, json=STAFF)
        return httpx.Response(200, json={"id": 5})

    built = []
    install_client(monkeypatch, handler, built=built)
    result = run("--staff", "bob@x.org", "tickets", "reply", "5", "--text", "ok")
    assert result.exit_code == 0, result.output
    assert built and all(client.is_closed for client in built)


def test_requests_authenticate_with_the_key_as_user_and_the_code_as_password(mock_api):
    captured = mock_api(lambda req: httpx.Response(200, json=[]))
    env = {**ENV, "HFOX_API_KEY": "the-key", "HFOX_AUTH_CODE": "the-code"}
    result = runner.invoke(cli, ["system", "statuses"], env=env)
    assert result.exit_code == 0, result.output
    expected = "Basic " + base64.b64encode(b"the-key:the-code").decode()
    assert [req.headers["authorization"] for req in captured] == [expected]


# -- staff resolution and prompts ---------------------------------------------
def test_global_staff_id_wins_over_global_staff_without_a_lookup(mock_api):
    captured = mock_api(lambda req: httpx.Response(200, json=STAFF))
    ctx = make_ctx(staff_id_override=9, staff_override="Bob Jones")
    assert ctx.resolve_staff_id() == 9
    assert ctx.require_staff_id() == 9
    assert captured == []


def test_prompt_hides_the_answer_only_when_asked(monkeypatch, capsys):
    # A hidden answer comes from getpass, which does not echo; a visible one from stdin.
    monkeypatch.setattr(getpass, "getpass", lambda prompt="", stream=None: "s3cret")
    monkeypatch.setattr(sys, "stdin", io.StringIO("typed\n"))
    ctx = make_ctx()
    assert ctx.prompt("API key", hide_input=True) == "s3cret"
    assert ctx.prompt("HappyFox subdomain") == "typed"
    assert capsys.readouterr().out == ""


# -- page walks ---------------------------------------------------------------
def test_filter_error_names_each_active_filter_as_its_flag(mock_api):
    captured = mock_api(lambda req: httpx.Response(200, json=[]))
    filters = {"display_name": "x", "name": "  ", "email": "y", "serial": None}
    with pytest.raises(ValidationError) as exc:
        make_ctx().paginate("assets/", filters=filters)
    named = set(re.findall(r"--[\w-]+", str(exc.value)))
    assert named >= {"--display-name", "--email", "--page-all"}
    assert not named & {"--name", "--serial"}
    assert captured == []


ITEM_PAGES = [
    [{"id": 1, "name": "Laptop"}, {"id": 2, "name": "Phone"}],
    [{"id": 3, "name": "Laptop dock"}],
]
THREE_PAGES = [[{"id": 1}], [{"id": 2}], [{"id": 3}]]


def test_page_walk_reads_rows_under_a_custom_root_key(mock_api):
    captured = mock_api(paged("items", ITEM_PAGES))
    ctx = make_ctx(page_all=True, page_delay_ms=0, fmt=OutputFormat.CSV)
    assert ctx.paginate("things/", root_key="items") == ITEM_PAGES[0] + ITEM_PAGES[1]
    assert [req.url.params["page"] for req in captured] == ["1", "2"]


def test_ndjson_filter_reads_rows_under_a_custom_root_key(mock_api, capsys):
    mock_api(paged("items", ITEM_PAGES))
    ctx = make_ctx(page_all=True, page_delay_ms=0)
    with pytest.raises(typer.Exit) as exc:
        ctx.paginate("things/", root_key="items", filters={"name": "laptop"})
    assert exc.value.exit_code == 0
    assert ndjson(capsys.readouterr().out) == [
        {"page_info": {"page_count": 2}, "items": [{"id": 1, "name": "Laptop"}]},
        {"page_info": {"page_count": 2}, "items": [{"id": 3, "name": "Laptop dock"}]},
    ]


@pytest.mark.parametrize(("delay_ms", "slept"), [(250, [0.25, 0.25]), (0, [])])
def test_page_delay_is_slept_between_pages(monkeypatch, delay_ms, slept):
    sleeps = []
    install_client(monkeypatch, paged("data", THREE_PAGES), sleeps=sleeps)
    ctx = make_ctx(page_all=True, page_delay_ms=delay_ms, fmt=OutputFormat.CSV)
    assert ctx.paginate("tickets/") == [{"id": 1}, {"id": 2}, {"id": 3}]
    assert sleeps == slept


@pytest.mark.parametrize(
    ("status", "slug", "exit_code"), [(401, "auth", 2), (404, "not_found", 4)]
)
def test_ndjson_error_line_exits_with_the_code_of_its_error(
    mock_api, capsys, status, slug, exit_code
):
    def handler(request):
        if request.url.params["page"] == "2":
            return httpx.Response(status, json={"error": "nope"})
        return httpx.Response(200, json={"page_info": {"page_count": 2}, "data": [{"id": 1}]})

    mock_api(handler)
    with pytest.raises(typer.Exit) as exc:
        make_ctx(page_all=True, page_delay_ms=0).paginate("tickets/")
    assert exc.value.exit_code == exit_code
    page, error = ndjson(capsys.readouterr().out)
    assert page["data"] == [{"id": 1}]
    assert (error["type"], error["exit_code"]) == (slug, exit_code)


def test_unexpected_exception_mid_walk_is_an_internal_ndjson_line(mock_api, capsys):
    def handler(request):
        if request.url.params["page"] == "2":
            raise RuntimeError("boom")
        return httpx.Response(200, json={"page_info": {"page_count": 2}, "data": [{"id": 1}]})

    mock_api(handler)
    with pytest.raises(typer.Exit) as exc:
        make_ctx(page_all=True, page_delay_ms=0).paginate("tickets/")
    assert exc.value.exit_code == 5
    page, error = ndjson(capsys.readouterr().out)
    assert page["data"] == [{"id": 1}]
    assert (error["type"], error["exit_code"]) == ("internal", 5)
    assert "RuntimeError" in error["error"]


def test_truncation_warning_names_the_flag_and_ignores_quiet(mock_api):
    captured = mock_api(paged("data", THREE_PAGES))
    result = run("--quiet", "--page-all", "--page-limit", "1", "tickets", "list")
    assert result.exit_code == 0, result.output
    assert ndjson(result.stdout) == [{"page_info": {"page_count": 3}, "data": [{"id": 1}]}]
    assert "--page-limit" in result.stderr
    assert len(captured) == 1


# -- rendering ----------------------------------------------------------------
def test_render_list_keeps_an_empty_page_empty(capsys):
    ctx = make_ctx(fmt=OutputFormat.CSV)
    ctx.render_list({"page_info": {"page_count": 1, "count": 0}, "data": []})
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("body", [None, "text"])
def test_render_list_renders_a_body_without_rows_as_it_is(capsys, body):
    # The client returns None for a 2xx response without a body.
    make_ctx(fmt=OutputFormat.YAML).render_list(body)
    out = capsys.readouterr().out
    assert out.strip()
    assert yaml.safe_load(out) == body


# -- stdin --------------------------------------------------------------------
def test_text_only_stdin_holding_undecodable_bytes_is_rejected(monkeypatch):
    # A text stream decoded with surrogateescape carries a raw byte as a lone surrogate.
    monkeypatch.setattr(sys, "stdin", io.StringIO("ok \udcff"))
    with pytest.raises(ValidationError) as exc:
        make_ctx().read_text("-", "--text-file")
    assert "--text-file" in str(exc.value)
