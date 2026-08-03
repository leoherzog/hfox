"""Coverage for AppContext helpers, _util edge cases, and the error hierarchy.

These are mostly direct unit calls against context.py / _util.py / errors.py.
mock_api is used only where AppContext.paginate/call must reach a real client.
"""

import json

import httpx
import pytest
import typer

import hfox.cli.context as context_mod
from hfox.cli._util import (
    attach,
    comma_join,
    compact,
    load_json_file,
    split_csv,
    split_csv_ints,
)
from hfox.cli.context import AppContext
from hfox.cli.output import OutputFormat
from hfox.core.config import Config
from hfox.core.errors import (
    APIError,
    AuthError,
    ExitCode,
    HfoxError,
    NotFoundError,
    ValidationError,
)


def make_config(**kw):
    return Config(
        subdomain="acme",
        region="us",
        api_key="k",
        auth_code="c",
        **kw,
    )


def make_ctx(**kw):
    cfg = kw.pop("config", None) or make_config(
        default_staff_id=kw.pop("default_staff_id", None)
    )
    return AppContext(config=cfg, **kw)


# ----------------------------------------------------------------------------
# context.py: staff-id resolution / precedence
# ----------------------------------------------------------------------------
def test_resolve_staff_id_explicit_wins():
    # line 60: explicit arg beats both override and config default.
    ctx = make_ctx(staff_id_override=99, default_staff_id=7)
    assert ctx.resolve_staff_id(42) == 42


def test_resolve_staff_id_override_beats_config():
    ctx = make_ctx(staff_id_override=99, default_staff_id=7)
    assert ctx.resolve_staff_id(None) == 99


def test_resolve_staff_id_falls_back_to_config_default():
    ctx = make_ctx(default_staff_id=7)
    assert ctx.resolve_staff_id(None) == 7


def test_resolve_staff_id_none_everywhere():
    ctx = make_ctx()
    assert ctx.resolve_staff_id(None) is None


def test_require_staff_id_returns_resolved():
    ctx = make_ctx(default_staff_id=5)
    assert ctx.require_staff_id(None) == 5
    assert ctx.require_staff_id(11) == 11


def test_require_staff_id_raises_validation_when_missing():
    # line 68: no staff id anywhere -> ValidationError (exit code 3).
    ctx = make_ctx()
    with pytest.raises(ValidationError) as exc:
        ctx.require_staff_id(None)
    assert exc.value.exit_code is ExitCode.VALIDATION
    assert "staff id" in str(exc.value).lower()


# ----------------------------------------------------------------------------
# context.py: paginate wiring (lines 109-119, 182)
# ----------------------------------------------------------------------------
def _install_client(monkeypatch, handler):
    captured: list[httpx.Request] = []

    def wrapped(request):
        captured.append(request)
        return handler(request)

    def factory(base_url, api_key, auth_code, **kwargs):
        from hfox.core.client import HappyFoxClient

        client = HappyFoxClient(base_url, api_key, auth_code, sleep=lambda _s: None)
        client._client = httpx.Client(
            transport=httpx.MockTransport(wrapped),
            auth=httpx.BasicAuth(api_key, auth_code),
        )
        return client

    monkeypatch.setattr(context_mod, "HappyFoxClient", factory)
    return captured


def _page_handler(request):
    page = int(httpx.QueryParams(request.url.query).get("page", "1"))
    return httpx.Response(
        200,
        json={
            "page_info": {"page_count": 2, "count": 4},
            "data": [{"id": page * 10}, {"id": page * 10 + 1}],
        },
    )


def test_paginate_page_all_json_streams_ndjson_and_exits(capsys, monkeypatch):
    # page_all=True + JSON -> one compact page envelope per line, then Exit(0).
    captured = _install_client(monkeypatch, _page_handler)
    ctx = make_ctx(page_all=True, page_limit=10, page_delay_ms=0)
    with pytest.raises(typer.Exit) as ei:
        ctx.paginate("tickets/", params={"size": 2})
    assert ei.value.exit_code == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    assert [[r["id"] for r in page["data"]] for page in lines] == [[10, 11], [20, 21]]
    assert len(captured) == 2


def test_paginate_page_all_non_json_returns_flat_list(monkeypatch):
    # page_all=True + table/csv/yaml -> client.paginate, flattened to a list.
    captured = _install_client(monkeypatch, _page_handler)
    ctx = make_ctx(page_all=True, page_limit=10, page_delay_ms=0, fmt=OutputFormat.TABLE)
    result = ctx.paginate("tickets/", params={"size": 2})
    assert result == [{"id": 10}, {"id": 11}, {"id": 20}, {"id": 21}]
    # Two pages were fetched.
    assert len(captured) == 2


def test_paginate_single_page_returns_envelope(monkeypatch):
    # line 119: page_all False -> single client.get, envelope returned untouched.
    body = {"page_info": {"page_count": 3, "count": 9}, "data": [{"id": 1}]}
    captured = _install_client(monkeypatch, lambda r: httpx.Response(200, json=body))
    ctx = make_ctx(page_all=False)
    result = ctx.paginate("tickets/")
    assert result == body
    assert len(captured) == 1


def test_paginate_dry_run_emits_and_exits(capsys, monkeypatch):
    # lines 106-107 / 182 region: dry-run serializes a GET preview and exits 0.
    import typer

    ctx = make_ctx(dry_run=True)
    with pytest.raises(typer.Exit) as exc:
        ctx.paginate("tickets/", params={"size": 5})
    assert exc.value.exit_code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["dry_run"] is True
    assert out["method"] == "GET"
    assert out["url"].endswith("/tickets/")
    assert out["params"] == {"size": 5}


def test_call_dry_run_emits_post(capsys):
    import typer

    ctx = make_ctx(dry_run=True)
    with pytest.raises(typer.Exit) as exc:
        ctx.call("POST", "tickets/", json={"subject": "x"})
    assert exc.value.exit_code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["method"] == "POST"
    assert out["body"] == {"subject": "x"}


def test_call_executes_request(monkeypatch):
    captured = _install_client(
        monkeypatch, lambda r: httpx.Response(200, json={"id": 7})
    )
    ctx = make_ctx()
    assert ctx.call("GET", "ticket/7/") == {"id": 7}
    assert captured[0].method == "GET"


# ----------------------------------------------------------------------------
# context.py: render / render_list branches (lines 131-140)
# ----------------------------------------------------------------------------
def test_render_list_bare_list(capsys):
    # line 131-133: body is already a list -> render it directly.
    ctx = make_ctx(fmt=OutputFormat.CSV)
    ctx.render_list([{"id": 1}, {"id": 2}])
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "id"
    assert "1" in out and "2" in out


def test_render_list_json_keeps_envelope(capsys):
    # lines 134-136: JSON format keeps the full {page_info, data} envelope.
    ctx = make_ctx(fmt=OutputFormat.JSON)
    body = {"page_info": {"count": 1}, "data": [{"id": 1}]}
    ctx.render_list(body)
    assert json.loads(capsys.readouterr().out) == body


def test_render_list_unwraps_data_for_csv(capsys):
    # line 137: non-JSON unwraps the root_key records.
    ctx = make_ctx(fmt=OutputFormat.CSV)
    ctx.render_list({"page_info": {"count": 1}, "data": [{"id": 5, "name": "a"}]})
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "id,name"
    assert "5,a" in out


def test_render_list_unwraps_rows_envelope(capsys):
    # lines 138-139: reports-style {page_count, rows} envelope -> use rows.
    ctx = make_ctx(fmt=OutputFormat.CSV)
    ctx.render_list({"page_count": 1, "rows": [{"a": 1}]})
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "a"
    assert "1" in out


def test_render_list_falls_back_to_body_when_no_records(capsys):
    # line 140: no root_key and no "rows" -> render the dict itself.
    ctx = make_ctx(fmt=OutputFormat.CSV)
    ctx.render_list({"foo": "bar"})
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "foo"
    assert "bar" in out


def test_render_list_custom_root_key(capsys):
    ctx = make_ctx(fmt=OutputFormat.CSV)
    ctx.render_list({"items": [{"x": 9}]}, root_key="items")
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "x"
    assert "9" in out


def test_render_plain(capsys):
    ctx = make_ctx(fmt=OutputFormat.JSON)
    ctx.render({"ok": True})
    assert json.loads(capsys.readouterr().out) == {"ok": True}


# ----------------------------------------------------------------------------
# _util.py
# ----------------------------------------------------------------------------
def test_compact_drops_none_keeps_falsey():
    assert compact({"a": 1, "b": None, "c": 0, "d": "", "e": False}) == {
        "a": 1,
        "c": 0,
        "d": "",
        "e": False,
    }


def test_comma_join_list():
    # line 20: list -> comma string of stringified members.
    assert comma_join([1, 2, 3]) == "1,2,3"
    assert comma_join(("a", "b")) == "a,b"


def test_comma_join_passthrough():
    assert comma_join("already") == "already"
    assert comma_join(5) == 5
    assert comma_join(None) is None


def test_split_csv_basic_and_trimming():
    assert split_csv("a, b ,c") == ["a", "b", "c"]
    # Empty fragments are dropped.
    assert split_csv("a,,b, ") == ["a", "b"]


def test_split_csv_none_and_empty():
    assert split_csv(None) is None
    assert split_csv("") == []
    assert split_csv("  , ") == []


def test_split_csv_ints_ok():
    assert split_csv_ints("1, 2 ,3") == [1, 2, 3]


def test_split_csv_ints_none():
    assert split_csv_ints(None) is None


def test_split_csv_ints_invalid_raises():
    # lines 38-39: non-integer fragment -> ValidationError naming the value.
    with pytest.raises(ValidationError) as exc:
        split_csv_ints("1,two,3")
    assert exc.value.exit_code is ExitCode.VALIDATION
    assert "1,two,3" in str(exc.value)


def test_attach_no_files_returns_json_body():
    out = attach({"subject": "s", "drop": None}, None)
    assert out == {"json": {"subject": "s"}}


def test_attach_with_file_builds_multipart(tmp_path):
    f = tmp_path / "note.txt"
    f.write_bytes(b"hello")
    out = attach({"subject": "s", "tags": ["x", "y"], "flag": True, "n": 3}, [str(f)])
    assert "files" in out
    assert out["files"][0][0] == "attachments"
    assert out["files"][0][1][0] == "note.txt"
    assert out["files"][0][1][1] == b"hello"
    data = out["data"]
    # lists/dicts/bools are JSON-encoded; scalars stringified.
    assert data["tags"] == json.dumps(["x", "y"])
    assert data["flag"] == "true"
    assert data["n"] == "3"
    assert data["subject"] == "s"


def test_attach_missing_file_raises(tmp_path):
    # line 56: a non-existent attachment path -> ValidationError.
    missing = tmp_path / "nope.bin"
    with pytest.raises(ValidationError) as exc:
        attach({"a": 1}, [str(missing)])
    assert "Attachment not found" in str(exc.value)
    assert exc.value.exit_code is ExitCode.VALIDATION


def test_load_json_file_ok(tmp_path):
    # lines 69, 72-73: parse valid JSON content.
    f = tmp_path / "data.json"
    f.write_text(json.dumps([{"id": 1}, {"id": 2}]), encoding="utf-8")
    assert load_json_file(str(f)) == [{"id": 1}, {"id": 2}]


def test_load_json_file_missing(tmp_path):
    # lines 70-71: missing file -> ValidationError.
    with pytest.raises(ValidationError) as exc:
        load_json_file(str(tmp_path / "absent.json"))
    assert "File not found" in str(exc.value)


def test_load_json_file_invalid_json(tmp_path):
    # lines 74-75: malformed JSON -> ValidationError mentioning the path.
    f = tmp_path / "bad.json"
    f.write_text("{not valid", encoding="utf-8")
    with pytest.raises(ValidationError) as exc:
        load_json_file(str(f))
    assert "Invalid JSON" in str(exc.value)
    assert exc.value.exit_code is ExitCode.VALIDATION


# ----------------------------------------------------------------------------
# errors.py
# ----------------------------------------------------------------------------
def test_exit_code_values_frozen():
    assert int(ExitCode.SUCCESS) == 0
    assert int(ExitCode.API) == 1
    assert int(ExitCode.AUTH) == 2
    assert int(ExitCode.VALIDATION) == 3
    assert int(ExitCode.NOT_FOUND) == 4
    assert int(ExitCode.OTHER) == 5


def test_hfox_error_defaults():
    err = HfoxError("boom")
    assert err.exit_code is ExitCode.OTHER
    assert err.message == "boom"
    assert err.detail is None
    assert str(err) == "boom"


def test_hfox_error_to_dict_without_detail():
    # lines 37-38, 41: detail omitted from payload when None.
    assert HfoxError("oops").to_dict() == {"error": "oops", "exit_code": 5}


def test_hfox_error_to_dict_with_detail():
    # lines 39-40: detail included when present.
    err = HfoxError("oops", detail={"field": "x"})
    assert err.to_dict() == {
        "error": "oops",
        "exit_code": 5,
        "detail": {"field": "x"},
    }


def test_hfox_error_exit_code_override():
    # line 34-35: explicit exit_code arg overrides the class default.
    err = HfoxError("custom", exit_code=ExitCode.NOT_FOUND)
    assert err.exit_code is ExitCode.NOT_FOUND
    assert err.to_dict()["exit_code"] == 4


def test_subclass_exit_codes():
    assert AuthError("a").exit_code is ExitCode.AUTH
    assert ValidationError("v").exit_code is ExitCode.VALIDATION
    assert NotFoundError("n").exit_code is ExitCode.NOT_FOUND
    assert APIError("e", status_code=500).exit_code is ExitCode.API


def test_api_error_to_dict_includes_status_code():
    # lines 61-68: APIError carries status_code in both attr and serialization.
    err = APIError("server fail", status_code=503, detail={"raw": "x"})
    assert err.status_code == 503
    payload = err.to_dict()
    assert payload == {
        "error": "server fail",
        "exit_code": 1,
        "detail": {"raw": "x"},
        "status_code": 503,
    }


def test_api_error_to_dict_without_detail():
    payload = APIError("nope", status_code=404).to_dict()
    assert payload == {"error": "nope", "exit_code": 1, "status_code": 404}
    assert "detail" not in payload or payload.get("detail") is None
