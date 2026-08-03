"""Coverage for `hfox tickets` read paths, rendering, params, and error branches.

These complement the dry-run write-shape tests in test_cli.py and
test_dry_run_writes.py: here we drive real HTTP through the `mock_api` fixture so
the response-rendering (`obj.render` / `obj.render_list`) and success branches
execute, and we exercise the ValidationError / require_staff_id error paths.
"""

import json
import subprocess
import sys

import httpx
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


_INVOKER = (
    "import sys; sys.argv = ['hfox'] + sys.argv[1:]; "
    "from hfox.cli.main import app; app()"
)


def run_app(argv, base_env=None):
    """Run the real `app()` entry point in a subprocess to assert exact exit codes.

    HfoxError -> JSON-on-stdout + stable exit code mapping lives in `app()`, not
    in the bare `cli` object CliRunner invokes. These cases fail during input
    validation, before any HTTP request, so they stay offline.
    """
    env = dict(base_env if base_env is not None else ENV)
    return subprocess.run(
        [sys.executable, "-c", _INVOKER, *argv],
        env=env,
        capture_output=True,
        text=True,
    )


def ok_json(payload):
    """Build a handler returning a 200 JSON body."""

    def handler(request):
        return httpx.Response(200, json=payload)

    return handler


# -- list: collection read + render_list (line 51-52) -----------------------
def test_list_renders_collection_and_passes_filters(mock_api):
    body = {"page_info": {"page_count": 1, "count": 2}, "data": [{"id": 1}, {"id": 2}]}
    captured = mock_api(ok_json(body))
    result = run(
        "tickets", "list",
        "--status", "Open", "--category", "Sales", "-q", "broken",
        "--sort", "updated", "--minify", "--fields", "id,subject",
        "--page", "2", "--size", "25",
    )
    assert result.exit_code == 0
    req = captured[0]
    assert req.method == "GET"
    assert req.url.path.endswith("/tickets/")
    qp = dict(req.url.params)
    assert qp["status"] == "Open"
    assert qp["category"] == "Sales"
    assert qp["q"] == "broken"
    assert qp["sort"] == "updated"
    assert qp["minify_response"] == "true"
    assert qp["fields"] == "id,subject"
    assert qp["page"] == "2"
    assert qp["size"] == "25"
    # JSON format keeps the full envelope.
    out = json.loads(result.stdout)
    assert out["page_info"]["count"] == 2
    assert [r["id"] for r in out["data"]] == [1, 2]


def test_list_table_format_unwraps_rows(mock_api):
    body = {"page_info": {"page_count": 1, "count": 1}, "data": [{"id": 9, "subject": "Hi"}]}
    mock_api(ok_json(body))
    result = run("-f", "table", "tickets", "list")
    assert result.exit_code == 0
    # Table view shows the row content, not the page_info envelope key.
    assert "subject" in result.stdout or "Hi" in result.stdout


def test_list_minify_omitted_when_false(mock_api):
    captured = mock_api(ok_json({"page_info": {"page_count": 1, "count": 0}, "data": []}))
    result = run("tickets", "list")
    assert result.exit_code == 0
    qp = dict(captured[0].url.params)
    assert "minify_response" not in qp


def test_list_query_defaults_status_to_all(mock_api):
    # Search URLs are `?status=_all&q=...` — --query without --status spans all statuses.
    captured = mock_api(ok_json({"page_info": {"page_count": 1, "count": 0}, "data": []}))
    result = run("tickets", "list", "-q", "foo")
    assert result.exit_code == 0
    qp = dict(captured[0].url.params)
    assert qp["status"] == "_all"
    assert qp["q"] == "foo"


def test_list_query_with_explicit_status_is_kept(mock_api):
    captured = mock_api(ok_json({"page_info": {"page_count": 1, "count": 0}, "data": []}))
    result = run("tickets", "list", "-q", "foo", "--status", "closed")
    assert result.exit_code == 0
    qp = dict(captured[0].url.params)
    assert qp["status"] == "closed"
    assert qp["q"] == "foo"


def test_list_default_size_10_no_status_without_query(mock_api):
    captured = mock_api(ok_json({"page_info": {"page_count": 1, "count": 0}, "data": []}))
    result = run("tickets", "list")
    assert result.exit_code == 0
    qp = dict(captured[0].url.params)
    # API's documented default page size; no query -> no status injected.
    assert qp["size"] == "10"
    assert "status" not in qp
    assert "q" not in qp


def test_list_page_all_walks_pages(mock_api):
    pages = {}

    def handler(request):
        page = dict(request.url.params).get("page", "1")
        pages[page] = pages.get(page, 0) + 1
        if page == "1":
            return httpx.Response(
                200, json={"page_info": {"page_count": 2, "count": 2}, "data": [{"id": 1}]}
            )
        return httpx.Response(
            200, json={"page_info": {"page_count": 2, "count": 2}, "data": [{"id": 2}]}
        )

    mock_api(handler)
    result = run("--page-all", "tickets", "list")
    assert result.exit_code == 0
    # JSON + --page-all streams NDJSON: one compact page envelope per line.
    out = [json.loads(line) for line in result.stdout.strip().splitlines()]
    assert [page["data"][0]["id"] for page in out] == [1, 2]
    assert all("page_info" in page for page in out)
    assert "1" in pages and "2" in pages


# -- get: single-resource GET + show_cf_changes param -----------------------
def test_get_single_ticket(mock_api):
    captured = mock_api(ok_json({"id": 42, "subject": "Server down"}))
    result = run("tickets", "get", "42")
    assert result.exit_code == 0
    req = captured[0]
    assert req.method == "GET"
    assert req.url.path.endswith("/ticket/42/")
    assert "show_cf_changes" not in dict(req.url.params)
    out = json.loads(result.stdout)
    assert out["id"] == 42


def test_get_with_show_cf_changes_param(mock_api):
    captured = mock_api(ok_json({"id": 42}))
    result = run("tickets", "get", "42", "--show-cf-changes")
    assert result.exit_code == 0
    assert dict(captured[0].url.params)["show_cf_changes"] == "true"


# -- create: response render + success message (lines 139-142) --------------
def test_create_renders_result_and_success(mock_api):
    captured = mock_api(ok_json({"id": 7, "display_id": "T-7"}))
    result = run(
        "tickets", "create",
        "--subject", "Down", "--category", "3",
        "--name", "Han", "--email", "h@x.org", "--text", "fire",
    )
    assert result.exit_code == 0
    assert captured[0].method == "POST"
    assert captured[0].url.path.endswith("/tickets/")
    body = json.loads(captured[0].content)
    assert body["category"] == 3
    assert body["name"] == "Han"
    # The rendered object is on stdout; the success() line is a stderr side channel.
    assert json.loads(result.stdout)["display_id"] == "T-7"
    assert "Created ticket T-7." in result.stderr


def test_create_existing_client_no_name_email(mock_api):
    captured = mock_api(ok_json({"id": 8}))
    result = run(
        "tickets", "create",
        "--subject", "S", "--category", "2", "--client", "55", "--html", "<p>hi</p>",
    )
    assert result.exit_code == 0
    body = json.loads(captured[0].content)
    assert body["client"] == 55
    assert body["html"] == "<p>hi</p>"
    # No display_id in response -> no success line, just rendered object.
    out = json.loads(result.stdout)
    assert out["id"] == 8


# -- create: validation branches (lines 110-115) ----------------------------
def test_create_no_body_is_validation_error():
    proc = run_app(
        ["tickets", "create",
         "--subject", "x", "--category", "1", "--name", "n", "--email", "e@x.org"],
    )
    assert proc.returncode == 3
    payload = json.loads(proc.stdout)
    assert payload["exit_code"] == 3
    assert "body" in payload["error"].lower()


def test_create_no_contact_is_validation_error():
    # Has body but neither (name+email) nor client -> line 112-115.
    proc = run_app(["tickets", "create", "--subject", "x", "--category", "1", "--text", "hi"])
    assert proc.returncode == 3
    assert "contact" in json.loads(proc.stdout)["error"].lower()


def test_create_name_without_email_is_validation_error():
    proc = run_app(
        ["tickets", "create",
         "--subject", "x", "--category", "1", "--text", "hi", "--name", "Han"],
    )
    assert proc.returncode == 3
    assert "contact" in json.loads(proc.stdout)["error"].lower()


# -- create-bulk: list payload + validation (lines 151-158) -----------------
def test_create_bulk_posts_array(mock_api, tmp_path):
    f = tmp_path / "bulk.json"
    f.write_text(json.dumps([{"subject": "a"}, {"subject": "b"}]))
    captured = mock_api(ok_json([{"id": 1}, {"id": 2}]))
    result = run("tickets", "create-bulk", "--file", str(f))
    assert result.exit_code == 0
    assert captured[0].url.path.endswith("/tickets/")
    sent = json.loads(captured[0].content)
    assert isinstance(sent, list) and len(sent) == 2


def test_create_bulk_non_array_is_validation_error(tmp_path):
    f = tmp_path / "bad.json"
    f.write_text(json.dumps({"subject": "not a list"}))
    proc = run_app(["tickets", "create-bulk", "--file", str(f)])
    assert proc.returncode == 3
    assert "array" in json.loads(proc.stdout)["error"].lower()


def test_create_bulk_empty_array_is_validation_error(tmp_path):
    f = tmp_path / "empty.json"
    f.write_text("[]")
    proc = run_app(["tickets", "create-bulk", "--file", str(f)])
    assert proc.returncode == 3
    assert "between 1 and 100" in json.loads(proc.stdout)["error"]


# -- reply / note: body-required validation (lines 209-210, 280-281) --------
def test_reply_without_body_is_validation_error():
    proc = run_app(["tickets", "reply", "42"])
    assert proc.returncode == 3
    assert "reply body" in json.loads(proc.stdout)["error"].lower()


def test_note_without_body_is_validation_error():
    proc = run_app(["tickets", "note", "42"])
    assert proc.returncode == 3
    assert "note body" in json.loads(proc.stdout)["error"].lower()


# -- reply / note: real POST renders the result (lines 235-238, 301-304) ----
def test_reply_posts_and_renders(mock_api):
    captured = mock_api(ok_json({"id": 100, "status": "open"}))
    result = run("tickets", "reply", "42", "--text", "ok", "--status", "5")
    assert result.exit_code == 0
    assert captured[0].url.path.endswith("/ticket/42/staff_update/")
    body = json.loads(captured[0].content)
    assert body["plaintext"] == "ok"
    assert body["staff"] == 1
    assert json.loads(result.stdout)["id"] == 100


def test_reply_parent_update_in_body(mock_api):
    captured = mock_api(ok_json({"id": 102}))
    result = run("tickets", "reply", "42", "--text", "ok", "--parent-update", "5")
    assert result.exit_code == 0
    body = json.loads(captured[0].content)
    assert body["parent_update"] == 5


def test_reply_omits_parent_update_by_default(mock_api):
    captured = mock_api(ok_json({"id": 103}))
    result = run("tickets", "reply", "42", "--text", "ok")
    assert result.exit_code == 0
    body = json.loads(captured[0].content)
    assert "parent_update" not in body


def test_note_posts_with_alert_and_renders(mock_api):
    captured = mock_api(ok_json({"id": 101}))
    result = run("tickets", "note", "42", "--text", "internal", "--alert", "s")
    assert result.exit_code == 0
    assert captured[0].url.path.endswith("/ticket/42/staff_pvtnote/")
    body = json.loads(captured[0].content)
    assert body["alert"] == "s"
    assert json.loads(result.stdout)["id"] == 101


# -- user-reply: required user+text, real POST (lines 320-332) --------------
def test_user_reply_posts_and_renders(mock_api):
    captured = mock_api(ok_json({"id": 200}))
    result = run(
        "tickets", "user-reply", "42",
        "--user", "9", "--text", "thanks", "--cc", "a@x.org,b@x.org",
    )
    assert result.exit_code == 0
    req = captured[0]
    assert req.method == "POST"
    assert req.url.path.endswith("/ticket/42/user_reply/")
    body = json.loads(req.content)
    assert body["user"] == 9
    assert body["text"] == "thanks"
    assert body["cc"] == "a@x.org,b@x.org"
    assert json.loads(result.stdout)["id"] == 200


def test_user_reply_missing_required_user_errors():
    result = run("tickets", "user-reply", "42", "--text", "hi")
    # Missing required option -> usage error (exit 2).
    assert result.exit_code == 2


# -- update-cf: fields required + POST render (lines 351-359) ---------------
def test_update_cf_posts_and_renders(mock_api):
    captured = mock_api(ok_json({"id": 1, "updated": True}))
    result = run("tickets", "update-cf", "42", "--cf", "7=Urgent", "--cf", "6=3,4")
    assert result.exit_code == 0
    req = captured[0]
    assert req.url.path.endswith("/ticket/42/update_custom_fields/")
    body = json.loads(req.content)
    assert body["t-cf-7"] == "Urgent"
    assert body["t-cf-6"] == [3, 4]
    assert body["staff"] == 1
    assert json.loads(result.stdout)["updated"] is True


def test_update_cf_no_fields_is_validation_error():
    proc = run_app(["tickets", "update-cf", "42"])
    assert proc.returncode == 3
    assert "at least one" in json.loads(proc.stdout)["error"].lower()


def test_update_cf_json_payload(mock_api):
    captured = mock_api(ok_json({"ok": 1}))
    result = run("tickets", "update-cf", "42", "--cf-json", '{"1":"Acme, Inc."}')
    assert result.exit_code == 0
    body = json.loads(captured[0].content)
    assert body["t-cf-1"] == "Acme, Inc."


# -- tags: add/remove required + POST render (lines 371-382) ----------------
def test_tags_posts_and_renders(mock_api):
    captured = mock_api(ok_json({"tags": ["vip"]}))
    result = run("tickets", "tags", "42", "--add", "vip,urgent", "--remove", "old")
    assert result.exit_code == 0
    req = captured[0]
    assert req.url.path.endswith("/ticket/42/update_tags/")
    body = json.loads(req.content)
    assert body["add"] == "vip,urgent"
    assert body["remove"] == "old"
    assert body["staff_id"] == 1
    assert "vip" in json.loads(result.stdout)["tags"]


def test_tags_without_add_or_remove_is_validation_error():
    proc = run_app(["tickets", "tags", "42"])
    assert proc.returncode == 3
    assert "add" in json.loads(proc.stdout)["error"].lower()


# -- subscribe / unsubscribe: real POST render (lines 402-403, 413-416) -----
def test_subscribe_posts_and_renders(mock_api):
    captured = mock_api(ok_json({"subscribed": [3, 4]}))
    result = run("tickets", "subscribe", "42", "--agents", "3,4")
    assert result.exit_code == 0
    req = captured[0]
    assert req.url.path.endswith("/ticket/42/subscribe/")
    body = json.loads(req.content)
    assert body["data"] == [3, 4]
    assert body["staff_id"] == 1
    assert json.loads(result.stdout)["subscribed"] == [3, 4]


def test_unsubscribe_posts_and_renders(mock_api):
    captured = mock_api(ok_json({"ok": True}))
    result = run("tickets", "unsubscribe", "42", "--staff-id", "5")
    assert result.exit_code == 0
    req = captured[0]
    assert req.url.path.endswith("/ticket/42/unsubscribe/")
    body = json.loads(req.content)
    assert body == {"staff_id": 5}
    assert json.loads(result.stdout)["ok"] is True


def test_unsubscribe_missing_staff_id_is_validation_error():
    # No --staff-id, no HFOX_STAFF_ID env -> require_staff_id raises (line 414).
    env = {k: v for k, v in ENV.items() if k != "HFOX_STAFF_ID"}
    proc = run_app(["tickets", "unsubscribe", "42"], base_env=env)
    assert proc.returncode == 3
    assert "staff id" in json.loads(proc.stdout)["error"].lower()


# -- forward: full body + render (lines 454-472) ----------------------------
def test_forward_posts_full_body_and_renders(mock_api):
    captured = mock_api(ok_json({"forwarded": True}))
    result = run(
        "tickets", "forward", "42",
        "--to", "a@x.org,b@x.org", "--subject", "FWD", "--message", "see this",
        "--cc", "c@x.org", "--to-include-contact", "--include-private-notes",
        "--no-convert-replies", "--ticket-attachments", "11,12",
    )
    assert result.exit_code == 0
    req = captured[0]
    assert req.url.path.endswith("/ticket/42/forward/")
    body = json.loads(req.content)
    assert body["to"] == "a@x.org,b@x.org"
    assert body["subject"] == "FWD"
    assert body["message"] == "see this"
    assert body["cc"] == "c@x.org"
    assert body["staff_id"] == 1
    assert body["to_include_ticket_contact"] is True
    assert body["include_pvt_notes"] is True
    # default-True flag flipped off.
    assert body["convert_replies_as_new_ticket"] is False
    # send_all_messages defaults to True and is always sent.
    assert body["send_all_messages"] is True
    assert body["ticket_attachments"] == [11, 12]
    assert json.loads(result.stdout)["forwarded"] is True


def test_forward_defaults_send_all_and_convert(mock_api):
    captured = mock_api(ok_json({"ok": 1}))
    result = run(
        "tickets", "forward", "42",
        "--to", "a@x.org", "--subject", "S", "--message", "m",
    )
    assert result.exit_code == 0
    body = json.loads(captured[0].content)
    assert body["send_all_messages"] is True
    assert body["convert_replies_as_new_ticket"] is True
    # optional flags that default to False/None are omitted by compact().
    assert "to_include_ticket_contact" not in body
    assert "ticket_attachments" not in body


# -- move: real POST render (line 495) --------------------------------------
def test_move_posts_and_renders(mock_api):
    captured = mock_api(ok_json({"moved": True}))
    result = run("tickets", "move", "42", "--to-category", "9", "--note", "rerouted")
    assert result.exit_code == 0
    req = captured[0]
    assert req.url.path.endswith("/ticket/42/move/")
    body = json.loads(req.content)
    assert body["target_category_id"] == 9
    assert body["move_note"] == "rerouted"
    assert json.loads(result.stdout)["moved"] is True


# -- delete: confirm/abort + --yes path + success (lines 506-512) -----------
def test_delete_with_yes_posts_and_renders(mock_api):
    captured = mock_api(ok_json({"deleted": True}))
    result = run("tickets", "delete", "42", "--yes")
    assert result.exit_code == 0
    req = captured[0]
    assert req.url.path.endswith("/ticket/42/delete/")
    body = json.loads(req.content)
    assert body == {"staff_id": 1}
    # The success line is a stderr side channel; the result object is on stdout.
    assert "Deleted ticket 42" in result.stderr
    assert json.loads(result.stdout)["deleted"] is True


def test_delete_confirm_yes_via_prompt(mock_api):
    captured = mock_api(ok_json({"deleted": True}))
    # Answer the typer.confirm prompt with 'y'.
    env = dict(ENV)
    result = runner.invoke(cli, ["tickets", "delete", "42"], input="y\n", env=env)
    assert result.exit_code == 0
    assert captured and captured[0].url.path.endswith("/ticket/42/delete/")


def test_delete_abort_when_declined():
    # Decline the confirmation -> typer abort, no request issued (exit != 0).
    result = runner.invoke(cli, ["tickets", "delete", "42"], input="n\n", env=dict(ENV))
    assert result.exit_code != 0


# -- API error surfaces as exit code 1 --------------------------------------
def test_get_api_error_exit_code(mock_api):
    def handler(request):
        return httpx.Response(404, json={"error": "Ticket not found"})

    mock_api(handler)
    result = run("tickets", "get", "999999")
    assert result.exit_code in (1, 4)
