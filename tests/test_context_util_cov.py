"""Unit tests for AppContext helpers, _util and the error hierarchy.

`_install_client` patches the client factory where AppContext.paginate or call must send
a request.
"""

import json
import os

import httpx
import pytest
import typer

import hfox.cli._util as util_mod
import hfox.cli.context as context_mod
from hfox.cli._util import (
    MAX_ATTACHMENT_BYTES,
    NOT_IN_GROUP,
    attach,
    comma_join,
    compact,
    count_failures,
    exit_on_failures,
    filter_help,
    filter_needles,
    filter_rows,
    fold,
    require_nonblank,
    split_csv,
    split_csv_ints,
    validate_contact_ref,
    validate_ticket_id,
)
from hfox.cli.context import AppContext
from hfox.cli.output import OutputFormat
from hfox.core.client import _unwrap_page
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
    assert ctx.resolve_staff_id(42, None) == 42


def test_resolve_staff_id_override_beats_config():
    ctx = make_ctx(staff_id_override=99, default_staff_id=7)
    assert ctx.resolve_staff_id(None, None) == 99


def test_resolve_staff_id_falls_back_to_config_default():
    ctx = make_ctx(default_staff_id=7)
    assert ctx.resolve_staff_id(None, None) == 7


def test_resolve_staff_id_none_everywhere():
    ctx = make_ctx()
    assert ctx.resolve_staff_id(None, None) is None
    assert ctx.resolve_staff_id() is None


def test_resolve_staff_id_zero_is_an_id():
    ctx = make_ctx(staff_id_override=0, default_staff_id=7)
    assert ctx.resolve_staff_id(None, None) == 0
    assert ctx.resolve_staff_id(0, None) == 0


def test_require_staff_id_returns_resolved():
    ctx = make_ctx(default_staff_id=5)
    assert ctx.require_staff_id(None, None) == 5
    assert ctx.require_staff_id(11, None) == 11


def test_require_staff_id_raises_validation_when_missing():
    ctx = make_ctx()
    with pytest.raises(ValidationError) as exc:
        ctx.require_staff_id(None, None)
    assert exc.value.exit_code is ExitCode.VALIDATION
    assert str(exc.value) == (
        "This action needs a staff identity. Pass --staff or --staff-id, set a default "
        "during `hfox auth login`, or export HFOX_STAFF_ID."
    )


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
    captured = _install_client(monkeypatch, _page_handler)
    ctx = make_ctx(page_all=True, page_limit=10, page_delay_ms=0)
    with pytest.raises(typer.Exit) as ei:
        ctx.paginate("tickets/", params={"size": 2})
    assert ei.value.exit_code == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    assert [[r["id"] for r in page["data"]] for page in lines] == [[10, 11], [20, 21]]
    assert len(captured) == 2


def test_paginate_page_all_non_json_returns_flat_list(monkeypatch):
    captured = _install_client(monkeypatch, _page_handler)
    ctx = make_ctx(page_all=True, page_limit=10, page_delay_ms=0, fmt=OutputFormat.TABLE)
    result = ctx.paginate("tickets/", params={"size": 2})
    assert result == [{"id": 10}, {"id": 11}, {"id": 20}, {"id": 21}]
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


@pytest.mark.parametrize("dry_run", [True, False])
def test_paginate_filters_require_page_all(capsys, monkeypatch, dry_run):
    captured = _install_client(monkeypatch, _page_handler)
    ctx = make_ctx(page_all=False, dry_run=dry_run)
    with pytest.raises(ValidationError, match="global --page-all before the resource"):
        ctx.paginate("assets/", filters={"name": "x"})
    assert captured == []
    assert capsys.readouterr().out == ""


def test_paginate_blank_filters_behave_as_none(monkeypatch):
    body = {"page_info": {"page_count": 3, "count": 9}, "data": [{"id": 1, "name": "x"}]}
    _install_client(monkeypatch, lambda r: httpx.Response(200, json=body))
    ctx = make_ctx(page_all=False)
    assert ctx.paginate("assets/", filters={"name": "  "}) == body


_NAMED_PAGE_INFO = {"page_count": 2, "count": 2}


def _named_page_handler(request):
    page = int(httpx.QueryParams(request.url.query).get("page", "1"))
    rows = (
        [{"id": 10, "name": "Laptop"}, {"id": 11, "name": "Phone"}]
        if page == 1
        else [{"id": 20, "name": "Phone"}]
    )
    return httpx.Response(200, json={"page_info": _NAMED_PAGE_INFO, "data": rows})


def test_paginate_page_all_json_filters_each_ndjson_page(capsys, monkeypatch):
    _install_client(monkeypatch, _named_page_handler)
    ctx = make_ctx(page_all=True, page_delay_ms=0)
    with pytest.raises(typer.Exit) as ei:
        ctx.paginate("assets/", filters={"name": "LAP"})
    assert ei.value.exit_code == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    assert [[r["id"] for r in page["data"]] for page in lines] == [[10], []]
    assert [page["page_info"] for page in lines] == [_NAMED_PAGE_INFO, _NAMED_PAGE_INFO]


def test_paginate_page_all_non_json_filters_flat_list(monkeypatch):
    _install_client(monkeypatch, _named_page_handler)
    ctx = make_ctx(page_all=True, page_delay_ms=0, fmt=OutputFormat.TABLE)
    assert ctx.paginate("assets/", filters={"name": "LAP"}) == [{"id": 10, "name": "Laptop"}]


def test_paginate_dry_run_page_all_never_sends_filters(capsys, monkeypatch):
    captured = _install_client(monkeypatch, _named_page_handler)
    ctx = make_ctx(dry_run=True, page_all=True)
    with pytest.raises(typer.Exit) as ei:
        ctx.paginate("assets/", filters={"name": "LAP"})
    assert ei.value.exit_code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["params"] == {"size": 50, "page": 1}
    assert "name" not in out["url"]
    assert captured == []


def test_paginate_filtered_walk_warns_when_truncated(capsys, monkeypatch):
    _install_client(monkeypatch, _named_page_handler)
    ctx = make_ctx(page_all=True, page_limit=1, page_delay_ms=0)
    with pytest.raises(typer.Exit) as ei:
        ctx.paginate("assets/", filters={"name": "LAP"})
    assert ei.value.exit_code == 0
    out = capsys.readouterr()
    assert len(out.out.strip().splitlines()) == 1
    assert "incomplete" in out.err


_EMPTY_INFO = {"page_count": 1, "count": 0}


@pytest.mark.parametrize(
    ("page", "rows"),
    [
        ({"page_info": _EMPTY_INFO, "data": None}, []),
        ({"page_info": _EMPTY_INFO}, []),
        ({"data": {"id": 1, "name": "x"}}, [{"id": 1, "name": "x"}]),
        ({"data": {"id": 2, "name": "y"}}, []),
        ([{"id": 1, "name": "x"}, {"id": 2}, "x"], [{"id": 1, "name": "x"}]),
    ],
)
def test_paginate_filter_odd_pages_agree_across_formats(capsys, monkeypatch, page, rows):
    _install_client(monkeypatch, lambda r: httpx.Response(200, json=page))
    table = make_ctx(page_all=True, page_delay_ms=0, fmt=OutputFormat.TABLE)
    assert table.paginate("assets/", filters={"name": "x"}) == rows

    ctx = make_ctx(page_all=True, page_delay_ms=0)
    with pytest.raises(typer.Exit) as ei:
        ctx.paginate("assets/", filters={"name": "x"})
    assert ei.value.exit_code == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 1
    line = json.loads(lines[0])
    if isinstance(page, list):
        assert line == rows
    else:
        assert line["data"] == rows
        assert line.get("page_info") == page.get("page_info")


def test_paginate_filtered_walk_api_error_becomes_ndjson_line(capsys, monkeypatch):
    _install_client(monkeypatch, lambda r: httpx.Response(500, json={"error": "boom"}))
    ctx = make_ctx(page_all=True, page_delay_ms=0)
    with pytest.raises(typer.Exit) as ei:
        ctx.paginate("assets/", filters={"name": "x"})
    assert ei.value.exit_code == 1
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 1
    line = json.loads(lines[0])
    assert set(line) >= {"error", "type", "exit_code"}
    assert (line["type"], line["exit_code"]) == ("api", 1)


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


@pytest.mark.parametrize(("value", "number"), [("5", "5"), ("0042", "42")])
def test_validate_ticket_id_accepts_digits(value, number):
    assert validate_ticket_id(value) == number


@pytest.mark.parametrize(
    "value", ["", "0", "abc", "#DC00000003", "5/../tickets", "..", "１２", " 5", "-1"]
)
def test_validate_ticket_id_rejects_non_digits(value):
    with pytest.raises(ValidationError):
        validate_ticket_id(value)


@pytest.mark.parametrize("value", ["1", "42", "jane@example.com", "a.b+c@x.co"])
def test_validate_contact_ref_accepts_id_or_email(value):
    assert validate_contact_ref(value) == value


@pytest.mark.parametrize(
    "value",
    ["", "0", "000", "-3", "abc", "5/../../tickets", "a/b@x.com", "a@b@c", "@x", "a @x", ".."],
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


def test_attach_unopenable_file_raises(tmp_path, monkeypatch):
    f = tmp_path / "locked.bin"
    f.write_bytes(b"x")

    def denied(self, *args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(type(f), "open", denied)
    with pytest.raises(ValidationError, match="Cannot read .*Permission denied"):
        attach({}, [str(f)])


def test_attach_unreadable_file_raises(tmp_path, monkeypatch):
    f = tmp_path / "locked.bin"
    f.write_bytes(b"x")

    def denied(handle):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(util_mod, "_read_all", denied)
    with pytest.raises(ValidationError, match="Cannot read .*Input/output error"):
        attach({}, [str(f)])


def test_attach_rechecks_total_on_the_bytes_read(tmp_path, monkeypatch):
    # A file that grows after the size check must not slip past the limit.
    f = tmp_path / "grows.bin"
    f.write_bytes(b"x")
    monkeypatch.setattr(util_mod, "MAX_ATTACHMENT_BYTES", 4)
    monkeypatch.setattr(util_mod, "_read_all", lambda handle: b"12345")
    with pytest.raises(ValidationError, match="Attachments total 5 bytes; the limit is 4"):
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

    def no_read(handle):
        raise AssertionError("read before size check")

    monkeypatch.setattr(util_mod, "_read_all", no_read)
    with pytest.raises(ValidationError) as exc:
        attach({}, [str(a), str(b)])
    assert "25,000,000" in str(exc.value)
    assert MAX_ATTACHMENT_BYTES == 25_000_000


def test_read_json_ok(tmp_path):
    f = tmp_path / "data.json"
    f.write_text(json.dumps([{"id": 1}, {"id": 2}]), encoding="utf-8")
    assert make_ctx().read_json(str(f), "--file") == [{"id": 1}, {"id": 2}]


def test_read_json_accepts_utf8_bom(tmp_path):
    f = tmp_path / "bom.json"
    f.write_bytes(b"\xef\xbb\xbf" + b'{"a": "caf\xc3\xa9"}')
    assert make_ctx().read_json(str(f), "--file") == {"a": "café"}


def test_read_json_missing(tmp_path):
    with pytest.raises(ValidationError) as exc:
        make_ctx().read_json(str(tmp_path / "absent.json"), "--file")
    assert "File not found" in str(exc.value)


def test_read_json_invalid_json(tmp_path):
    f = tmp_path / "bad.json"
    f.write_text("{not valid", encoding="utf-8")
    with pytest.raises(ValidationError) as exc:
        make_ctx().read_json(str(f), "--file")
    assert "Invalid JSON" in str(exc.value)
    assert exc.value.exit_code is ExitCode.VALIDATION


@pytest.mark.parametrize("encoding", ["latin-1", "utf-16"])
def test_read_json_rejects_non_utf8(tmp_path, encoding):
    f = tmp_path / "enc.json"
    f.write_text('{"name": "Zoë"}', encoding=encoding)
    with pytest.raises(ValidationError) as exc:
        make_ctx().read_json(str(f), "--file")
    assert exc.value.exit_code is ExitCode.VALIDATION


@pytest.mark.parametrize(
    "text", ['[{"n": NaN}]', '{"n": Infinity}', '{"n": -Infinity}', '{"n": 1e400}']
)
def test_read_json_rejects_non_finite_numbers(tmp_path, text):
    f = tmp_path / "nan.json"
    f.write_text(text, encoding="utf-8")
    with pytest.raises(ValidationError) as exc:
        make_ctx().read_json(str(f), "--file")
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


@pytest.mark.parametrize(
    ("value", "expected"),
    [(" Straße ", "strasse"), ("José", "josé"), ("ＡB", "ab")],
)
def test_fold(value, expected):
    assert fold(value) == expected


def test_fold_is_closed_under_case():
    # casefold turns U+0390 into a non-NFKC sequence; the uppercase form must still match.
    assert fold("ΐ") == fold("Ϊ́")
    assert fold("Πρωτεΐνη") == fold("ΠΡΩΤΕΪ́ΝΗ")


def test_fold_non_string_is_none():
    assert fold(None) is None
    assert fold(5) is None
    assert fold(True) is None
    assert fold({"a": "b"}) is None


def test_filter_needles_drops_blank_and_folds():
    assert filter_needles({"name": " Ali ", "email": None, "role": "  ", "x": ""}) == {
        "name": "ali"
    }


# Staff rows: mixed-case email, a substring shared across names and emails, and a null email.
STAFF = [
    {"id": 1, "name": "Alice Smith", "email": "alice@x.org"},
    {"id": 2, "name": "Bob Jones", "email": "BOB@x.org"},
    {"id": 3, "name": "Alicia Bob", "email": None},
]


def _ids(rows):
    return [row["id"] for row in rows]


def test_filter_rows_casefolded_substring_all_must_match():
    assert _ids(filter_rows(STAFF, {"name": "ALI"})) == [1, 3]
    assert _ids(filter_rows(STAFF, {"name": "ali", "email": "x.org"})) == [1]
    assert _ids(filter_rows(STAFF, {"email": "BOB@"})) == [2]


@pytest.mark.parametrize(
    ("value", "needle"),
    [
        (None, "none"),
        (0, "0"),
        (False, "false"),
        (True, "true"),
        (12, "12"),
        ({"x": "a"}, "a"),
        (["a"], "a"),
    ],
)
def test_filter_rows_non_text_values_never_match(value, needle):
    assert filter_rows([{"f": value}], {"f": needle}) == []
    assert filter_rows([{"g": needle}], {"f": needle}) == []


def test_filter_rows_drops_non_dict_rows():
    assert filter_rows(["Alice", {"name": "Alice"}, None], {"name": "ali"}) == [{"name": "Alice"}]


@pytest.mark.parametrize("body", [[{"id": 1}], {"data": 1}, "text", None])
@pytest.mark.parametrize("filters", [{}, {"name": None}, {"name": "  "}])
def test_filter_rows_without_active_filter_returns_body(body, filters):
    assert filter_rows(body, filters) is body


@pytest.mark.parametrize(
    ("body", "root_key", "key"),
    [
        ({"page_info": {"page_count": 1, "count": 2}, "data": list(STAFF)}, "data", "data"),
        ({"page_info": {"page_count": 1, "count": 2}, "items": list(STAFF)}, "items", "items"),
        ({"page_count": 1, "rows": list(STAFF)}, "data", "rows"),
    ],
)
def test_filter_rows_envelope_keeps_other_keys(body, root_key, key):
    result = filter_rows(body, {"name": "bob"}, root_key=root_key)
    assert _ids(result[key]) == [2, 3]
    assert result.get("page_info") == body.get("page_info")
    assert result.get("page_count") == body.get("page_count")
    assert set(result) == set(body)
    assert body[key] == STAFF


@pytest.mark.parametrize("body", ["text", None, {"error": "x"}, {"data": {"id": 1}}, 5])
def test_filter_rows_rejects_body_without_rows(body):
    with pytest.raises(HfoxError) as exc:
        filter_rows(body, {"name": "x"})
    assert exc.value.exit_code is ExitCode.OTHER
    assert "name" in str(exc.value)


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"page_info": {"count": 0}, "data": None}, {"page_info": {"count": 0}, "data": []}),
        ({"page_info": {"count": 0}}, {"page_info": {"count": 0}, "data": []}),
        ({"data": None, "rows": None}, {"data": None, "rows": []}),
        ({"data": {"id": 1, "name": "x"}}, {"data": [{"id": 1, "name": "x"}]}),
        ({"data": "x"}, {"data": []}),
        ({"data": [{"id": 1, "name": "x"}, {"id": 2}]}, {"data": [{"id": 1, "name": "x"}]}),
        ([{"id": 1, "name": "x"}, {"id": 2}, "x"], [{"id": 1, "name": "x"}]),
        ("text", []),
        (None, []),
    ],
)
def test_filter_rows_paged_reads_rows_like_the_walk(body, expected):
    result = filter_rows(body, {"name": "x"}, paged=True)
    assert result == expected
    walk_rows = _unwrap_page(body, "data")[0]
    kept = result if isinstance(result, list) else result["rows" if "rows" in result else "data"]
    assert kept == filter_rows(walk_rows, {"name": "x"})


def test_filter_help():
    text = filter_help("name")
    assert "name" in text
    assert "case-insensitive" in text
    assert "--page-all" not in text
    paged = filter_help("name", paged=True)
    assert paged.startswith(text)
    assert "global --page-all before the resource (hfox --page-all <resource> ...)" in paged


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
    assert HfoxError("oops").to_dict() == {"error": "oops", "type": "other", "exit_code": 5}


def test_hfox_error_to_dict_with_detail():
    err = HfoxError("oops", detail={"field": "x"})
    assert err.to_dict() == {
        "error": "oops",
        "type": "other",
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
        "type": "api",
        "exit_code": 1,
        "detail": {"raw": "x"},
        "status_code": 503,
    }


def test_api_error_to_dict_without_detail():
    payload = APIError("nope", status_code=404).to_dict()
    assert payload == {"error": "nope", "type": "api", "exit_code": 1, "status_code": 404}
    assert "detail" not in payload or payload.get("detail") is None
