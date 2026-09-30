"""Coverage for AppContext helpers, _util edge cases, and the error hierarchy.

These are mostly direct unit calls against context.py / _util.py / errors.py.
mock_api is used only where AppContext.paginate/call must reach a real client.
"""

import json
import os

import httpx
import pytest
import typer

import hfox.cli.context as context_mod
from hfox.cli._util import (
    MAX_ATTACHMENT_BYTES,
    NOT_IN_GROUP,
    attach,
    comma_join,
    compact,
    count_failures,
    exit_on_failures,
    load_json_file,
    require_nonblank,
    split_csv,
    split_csv_ints,
    validate_contact_ref,
    validate_ticket_id,
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
    ctx = make_ctx()
    with pytest.raises(ValidationError) as exc:
        ctx.require_staff_id(None)
    assert exc.value.exit_code is ExitCode.VALIDATION
    assert "staff id" in str(exc.value).lower()


# ----------------------------------------------------------------------------
# context.py: paginate wiring
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
    body = {"page_info": {"page_count": 3, "count": 9}, "data": [{"id": 1}]}
    captured = _install_client(monkeypatch, lambda r: httpx.Response(200, json=body))
    ctx = make_ctx(page_all=False)
    result = ctx.paginate("tickets/")
    assert result == body
    assert len(captured) == 1


def test_paginate_dry_run_emits_and_exits(capsys, monkeypatch):
    import typer

    ctx = make_ctx(dry_run=True)
    with pytest.raises(typer.Exit) as exc:
        ctx.paginate("tickets/", params={"size": 5})
    assert exc.value.exit_code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["dry_run"] is True
    assert out["method"] == "GET"
    assert out["url"] == "https://acme.happyfox.com/api/1.1/json/tickets/?size=5"
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
# context.py: render / render_list branches
# ----------------------------------------------------------------------------
def test_render_list_bare_list(capsys):
    ctx = make_ctx(fmt=OutputFormat.CSV)
    ctx.render_list([{"id": 1}, {"id": 2}])
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "id"
    assert "1" in out and "2" in out


def test_render_list_json_keeps_envelope(capsys):
    ctx = make_ctx(fmt=OutputFormat.JSON)
    body = {"page_info": {"count": 1}, "data": [{"id": 1}]}
    ctx.render_list(body)
    assert json.loads(capsys.readouterr().out) == body


def test_render_list_unwraps_data_for_csv(capsys):
    ctx = make_ctx(fmt=OutputFormat.CSV)
    ctx.render_list({"page_info": {"count": 1}, "data": [{"id": 5, "name": "a"}]})
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "id,name"
    assert "5,a" in out


def test_render_list_unwraps_rows_envelope(capsys):
    ctx = make_ctx(fmt=OutputFormat.CSV)
    ctx.render_list({"page_count": 1, "rows": [{"a": 1}]})
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "a"
    assert "1" in out


def test_render_list_falls_back_to_body_when_no_records(capsys):
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
    assert comma_join([1, 2, 3]) == "1,2,3"
    assert comma_join(("a", "b")) == "a,b"


def test_comma_join_passthrough():
    assert comma_join("already") == "already"
    assert comma_join(5) == 5
    assert comma_join(None) is None


def test_split_csv_basic_and_trimming():
    assert split_csv("a, b ,c") == ["a", "b", "c"]
    assert split_csv("a,,b, ") == ["a", "b"]


def test_split_csv_none_when_no_parts():
    assert split_csv(None) is None
    assert split_csv("") is None
    assert split_csv("  , ") is None


def test_split_csv_ints_ok():
    assert split_csv_ints("1, 2 ,3") == [1, 2, 3]


def test_split_csv_ints_none_or_empty_when_no_parts():
    assert split_csv_ints(None) is None
    assert split_csv_ints(",") == []


@pytest.mark.parametrize("value", ["1,two,3", "1_000", "+5", "-1", "１２"])
def test_split_csv_ints_invalid_raises(value):
    with pytest.raises(ValidationError) as exc:
        split_csv_ints(value)
    assert exc.value.exit_code is ExitCode.VALIDATION
    assert value in str(exc.value)


def test_require_nonblank_returns_value_unchanged():
    assert require_nonblank(" hi ", "--subject") == " hi "


@pytest.mark.parametrize("value", [None, "", "   ", "\t\n"])
def test_require_nonblank_rejects_blank(value):
    with pytest.raises(ValidationError) as exc:
        require_nonblank(value, "--subject")
    assert "--subject" in str(exc.value)


@pytest.mark.parametrize("value", ["0", "5", "0042"])
def test_validate_ticket_id_accepts_digits(value):
    assert validate_ticket_id(value) == value


@pytest.mark.parametrize(
    "value", ["", "abc", "#DC00000003", "5/../tickets", "..", "１２", " 5", "-1"]
)
def test_validate_ticket_id_rejects_non_digits(value):
    with pytest.raises(ValidationError):
        validate_ticket_id(value)


@pytest.mark.parametrize("value", ["1", "42", "jane@example.com", "a.b+c@x.co"])
def test_validate_contact_ref_accepts_id_or_email(value):
    assert validate_contact_ref(value) == value


@pytest.mark.parametrize(
    "value",
    ["", "0", "007", "-3", "abc", "5/../../tickets", "a/b@x.com", "a@b@c", "@x", "a @x", ".."],
)
def test_validate_contact_ref_rejects_other_values(value):
    with pytest.raises(ValidationError):
        validate_contact_ref(value)


def test_attach_no_files_sends_body_as_given():
    body = {"subject": "s", "assignee": None}
    assert attach(body, None) == {"json": {"subject": "s", "assignee": None}}


def test_attach_with_file_builds_multipart(tmp_path):
    f = tmp_path / "note.txt"
    f.write_bytes(b"hello")
    body = {"subject": "s", "tags": ["x", "y"], "flag": True, "n": 3, "t-cf-1": None}
    out = attach(body, [str(f)])
    assert out["files"] == [("attachments", ("note.txt", b"hello"))]
    assert out["data"] == {
        "subject": "s",
        "tags": json.dumps(["x", "y"]),
        "flag": "true",
        "n": "3",
    }


def test_attach_custom_field_name(tmp_path):
    f = tmp_path / "img.png"
    f.write_bytes(b"\x89PNG")
    out = attach({}, [str(f)], field="file")
    assert out["files"][0][0] == "file"


def test_attach_missing_file_raises(tmp_path):
    missing = tmp_path / "nope.bin"
    with pytest.raises(ValidationError) as exc:
        attach({"a": 1}, [str(missing)])
    assert "Attachment not found" in str(exc.value)
    assert exc.value.exit_code is ExitCode.VALIDATION


def test_attach_unreadable_file_raises(tmp_path, monkeypatch):
    f = tmp_path / "locked.bin"
    f.write_bytes(b"x")

    def denied(self):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(type(f), "read_bytes", denied)
    with pytest.raises(ValidationError, match="Cannot read .*Permission denied"):
        attach({}, [str(f)])


def test_attach_total_at_limit_is_allowed(tmp_path):
    a, b = tmp_path / "a.bin", tmp_path / "b.bin"
    a.write_bytes(b"")
    b.write_bytes(b"")
    os.truncate(a, MAX_ATTACHMENT_BYTES - 10)
    os.truncate(b, 10)
    out = attach({}, [str(a), str(b)])
    assert len(out["files"]) == 2


def test_attach_total_over_limit_raises_before_reading(tmp_path, monkeypatch):
    a, b = tmp_path / "a.bin", tmp_path / "b.bin"
    a.write_bytes(b"")
    b.write_bytes(b"")
    os.truncate(a, MAX_ATTACHMENT_BYTES)
    os.truncate(b, 1)

    def no_read(self):
        raise AssertionError("read before size check")

    monkeypatch.setattr(type(a), "read_bytes", no_read)
    with pytest.raises(ValidationError) as exc:
        attach({}, [str(a), str(b)])
    assert "25,000,000" in str(exc.value)
    assert MAX_ATTACHMENT_BYTES == 25_000_000


def test_load_json_file_ok(tmp_path):
    f = tmp_path / "data.json"
    f.write_text(json.dumps([{"id": 1}, {"id": 2}]), encoding="utf-8")
    assert load_json_file(str(f)) == [{"id": 1}, {"id": 2}]


def test_load_json_file_accepts_utf8_bom(tmp_path):
    f = tmp_path / "bom.json"
    f.write_bytes(b"\xef\xbb\xbf" + b'{"a": "caf\xc3\xa9"}')
    assert load_json_file(str(f)) == {"a": "café"}


def test_load_json_file_missing(tmp_path):
    with pytest.raises(ValidationError) as exc:
        load_json_file(str(tmp_path / "absent.json"))
    assert "File not found" in str(exc.value)


def test_load_json_file_invalid_json(tmp_path):
    f = tmp_path / "bad.json"
    f.write_text("{not valid", encoding="utf-8")
    with pytest.raises(ValidationError) as exc:
        load_json_file(str(f))
    assert "Invalid JSON" in str(exc.value)
    assert exc.value.exit_code is ExitCode.VALIDATION


@pytest.mark.parametrize("encoding", ["latin-1", "utf-16"])
def test_load_json_file_rejects_non_utf8(tmp_path, encoding):
    f = tmp_path / "enc.json"
    f.write_text('{"name": "Zoë"}', encoding=encoding)
    with pytest.raises(ValidationError) as exc:
        load_json_file(str(f))
    assert exc.value.exit_code is ExitCode.VALIDATION


@pytest.mark.parametrize(
    "text", ['[{"n": NaN}]', '{"n": Infinity}', '{"n": -Infinity}', '{"n": 1e400}']
)
def test_load_json_file_rejects_non_finite_numbers(tmp_path, text):
    f = tmp_path / "nan.json"
    f.write_text(text, encoding="utf-8")
    with pytest.raises(ValidationError) as exc:
        load_json_file(str(f))
    assert "Invalid JSON" in str(exc.value)


# Response shapes from Docs/1039-tickets-endpoint.md section 5 and
# Docs/1092-contacts-and-contact-groups-api-endpoints.md sections 5, 6 and 12.
TICKETS_BULK = [
    {"display_id": "#DC00000011", "id": 11, "success": True},
    {"success": False, "error": [{"field": "category", "errors": ["This field is required."]}]},
]
CONTACTS_BULK = [
    {"email": "johnsmith@example.com", "success": True, "id": 14},
    {"email": "nathen@example.com", "success": True, "id": 15},
]
GROUP_REMOVE = [
    {"data": {"message": "Successfully removed contact from group", "contact": 1}, "success": True},
    {"data": {"message": "Contact not part of the contact group", "contact": 3}, "success": False},
    {"data": {"message": "Contact does not exist", "contact": 100}, "success": False},
]
GROUP_ADD = [
    {"data": {"access_tickets": True, "contact": 1}, "success": True},
    {"errors": [{"field": "contact", "errors": ["Select a valid choice."]}], "success": False},
]


def test_count_failures_documented_shapes():
    assert count_failures(TICKETS_BULK) == 1
    assert count_failures(CONTACTS_BULK) == 0
    assert count_failures(GROUP_ADD) == 1
    assert count_failures(GROUP_REMOVE) == 2


def test_count_failures_skips_benign_not_in_group():
    assert count_failures(GROUP_REMOVE, benign=NOT_IN_GROUP) == 1


def test_count_failures_ignores_non_list_bodies():
    assert count_failures({"success": False}) == 0
    assert count_failures(None) == 0


def test_exit_on_failures_warns_and_exits_1(capsys):
    with pytest.raises(typer.Exit) as exc:
        exit_on_failures(TICKETS_BULK)
    assert exc.value.exit_code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "1 of 2 entries failed" in captured.err


def test_exit_on_failures_silent_when_all_succeed(capsys):
    exit_on_failures(CONTACTS_BULK)
    exit_on_failures([GROUP_REMOVE[1]], benign=NOT_IN_GROUP)
    assert capsys.readouterr().err == ""


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
    assert HfoxError("oops").to_dict() == {"error": "oops", "exit_code": 5}


def test_hfox_error_to_dict_with_detail():
    err = HfoxError("oops", detail={"field": "x"})
    assert err.to_dict() == {
        "error": "oops",
        "exit_code": 5,
        "detail": {"field": "x"},
    }


def test_hfox_error_exit_code_override():
    err = HfoxError("custom", exit_code=ExitCode.NOT_FOUND)
    assert err.exit_code is ExitCode.NOT_FOUND
    assert err.to_dict()["exit_code"] == 4


def test_subclass_exit_codes():
    assert AuthError("a").exit_code is ExitCode.AUTH
    assert ValidationError("v").exit_code is ExitCode.VALIDATION
    assert NotFoundError("n").exit_code is ExitCode.NOT_FOUND
    assert APIError("e", status_code=500).exit_code is ExitCode.API


def test_api_error_to_dict_includes_status_code():
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
