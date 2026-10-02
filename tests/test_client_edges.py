"""HappyFoxClient edges: wire headers, redirects, retry scope and timing, errors and page walks."""

import base64
import json
import sys

import httpx
import pytest
import typer

import hfox.cli.main as main_mod
from hfox.cli.context import AppContext
from hfox.cli.output import OutputFormat
from hfox.core.client import OUTCOME_UNKNOWN_HINT, HappyFoxClient, request_url
from hfox.core.config import Config
from hfox.core.errors import (
    APIError,
    AuthError,
    HfoxError,
    NetworkError,
    NotFoundError,
    RateLimitError,
    RequestTimeoutError,
    ValidationError,
)

BASE = "https://acme.happyfox.com/api/1.1/json"


def make_client(handler, **kwargs):
    """Return (client, requests, sleeps).

    The mock client takes the real one's auth, headers and redirect policy. Its explicit
    transport also keeps a proxy configured on the machine from being used.
    """
    requests: list[httpx.Request] = []
    sleeps: list[float] = []

    def wrapped(request):
        requests.append(request)
        return handler(request)

    client = HappyFoxClient(BASE, "the-key", "the-code", sleep=sleeps.append, **kwargs)
    real = client._client
    client._client = httpx.Client(
        transport=httpx.MockTransport(wrapped),
        auth=real.auth,
        headers=real.headers,
        follow_redirects=real.follow_redirects,
    )
    real.close()
    return client, requests, sleeps


def ok(_request):
    return httpx.Response(200, json={"ok": True})


def always(status, **kwargs):
    return lambda _request: httpx.Response(status, **kwargs)


def raising(exc_type):
    def handler(request):
        raise exc_type("boom", request=request)

    return handler


def payload_text(exc):
    """Return the error JSON as text, for a value that may sit under any key."""
    return json.dumps(exc.value.to_dict())


def pages(count, key="data"):
    def handler(request):
        page = int(request.url.params["page"])
        return httpx.Response(200, json={"page_info": {"page_count": count}, key: [page]})

    return handler


def envelope(**extra):
    """Return a handler serving one-row pages whose envelope also holds `extra`."""

    def handler(request):
        return httpx.Response(200, json={"data": [int(request.url.params["page"])], **extra})

    return handler


def raw_page_count(literal):
    """Return a handler whose page_count is a JSON literal that httpx will not encode."""
    content = b'{"page_info": {"page_count": ' + literal + b'}, "data": [1]}'
    return always(200, content=content, headers={"content-type": "application/json"})


def page_error(field, path="tickets/"):
    return {
        "error": f"Unexpected {field} in the {path} response; "
        "cannot tell how many pages there are.",
        "type": "other",
        "exit_code": 5,
    }


def rows_error(count, path="tickets/"):
    return {
        "error": f"Unexpected page in the {path} response: it reports {count} pages "
        "but holds no list of rows.",
        "type": "other",
        "exit_code": 5,
    }


# -- on the wire ---------------------------------------------------------------
def test_api_key_is_basic_username_and_auth_code_is_password():
    client, requests, _ = make_client(ok)
    client.get("staff/")
    scheme, _, token = requests[0].headers["authorization"].partition(" ")
    assert scheme == "Basic"
    assert base64.b64decode(token).decode() == "the-key:the-code"


def test_requests_ask_for_json():
    client, requests, _ = make_client(ok)
    client.get("staff/")
    assert requests[0].headers["accept"] == "application/json"


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_redirect_is_reported_not_followed(status):
    def handler(request):
        if request.url.host == "acme.happyfox.com":
            return httpx.Response(status, headers={"Location": "https://elsewhere.example/staff/"})
        return httpx.Response(200, json=[])

    client, requests, _ = make_client(handler)
    with pytest.raises(APIError) as exc:
        client.get("staff/")
    assert exc.value.status_code == status
    assert [request.url.host for request in requests] == ["acme.happyfox.com"]


@pytest.mark.parametrize(
    ("base", "path"),
    [(BASE, "/staff/"), (BASE + "/", "staff/")],
    ids=["leading-slash-path", "trailing-slash-base"],
)
def test_slashes_at_the_join_collapse_to_one(base, path):
    assert request_url(base, path) == f"{BASE}/staff/"


def test_percent_in_a_path_is_escaped_so_encoded_dots_stay_inert():
    client, requests, _ = make_client(ok)
    client.get("user/%2e%2e/tickets/")
    assert requests[0].url.raw_path == b"/api/1.1/json/user/%252e%252e/tickets/"


def test_zero_and_false_params_are_sent():
    client, requests, _ = make_client(ok)
    client.request("DELETE", "asset/5/", params={"deleted_by": 0, "minify": False, "x": None})
    assert dict(requests[0].url.params) == {"deleted_by": "0", "minify": "false"}


# -- retry scope ---------------------------------------------------------------
@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_server_error_on_a_get_is_not_retried(status):
    client, requests, sleeps = make_client(always(status, json={}))
    with pytest.raises(APIError) as exc:
        client.get("tickets/")
    assert exc.value.status_code == status
    assert len(requests) == 1
    assert sleeps == []


def test_write_answered_429_is_resent_with_the_same_body():
    def handler(request):
        if len(requests) == 1:
            return httpx.Response(429, json={})
        return httpx.Response(200, json={"id": 7})

    client, requests, sleeps = make_client(handler)
    assert client.request("POST", "tickets/", json={"subject": "x"}) == {"id": 7}
    first, second = requests
    assert json.loads(first.content) == {"subject": "x"}
    assert second.content == first.content
    assert len(sleeps) == 1


def test_get_is_retried_when_the_response_cannot_be_decoded():
    def handler(request):
        if len(requests) == 1:
            raise httpx.DecodingError("bad gzip", request=request)
        return httpx.Response(200, json={"ok": True})

    client, requests, sleeps = make_client(handler)
    assert client.get("tickets/") == {"ok": True}
    assert len(requests) == 2
    assert len(sleeps) == 1


def test_lowercase_method_is_classified_like_uppercase():
    client, requests, _ = make_client(raising(httpx.ReadTimeout), max_retries=2)
    with pytest.raises(RequestTimeoutError) as exc:
        client.request("get", "tickets/")
    assert [request.method for request in requests] == ["GET"] * 3
    assert exc.value.outcome_unknown is False
    assert exc.value.hint is None


# -- retry timing ----------------------------------------------------------------
def test_default_429_backoff_doubles_from_one_second_and_stops_near_33s():
    client, requests, sleeps = make_client(always(429, json={}))
    with pytest.raises(RateLimitError):
        client.get("tickets/")
    assert len(requests) == 6
    assert len(sleeps) == 5
    for attempt, delay in enumerate(sleeps):
        assert 2**attempt <= delay < 2**attempt + 1
    assert sum(sleeps) == pytest.approx(33, abs=1.5)


def test_backoff_is_capped_at_a_minute_plus_jitter():
    client, _, sleeps = make_client(always(429, json={}), max_retries=9)
    with pytest.raises(RateLimitError):
        client.get("tickets/")
    assert 32 <= sleeps[5] < 33
    assert all(60 <= delay < 61 for delay in sleeps[6:])
    assert len(sleeps[6:]) == 3


def test_retry_after_just_over_ten_minutes_sleeps_ten_minutes():
    client, _, sleeps = make_client(
        always(429, headers={"Retry-After": "601"}, json={}), max_retries=1
    )
    with pytest.raises(RateLimitError):
        client.get("tickets/")
    assert sleeps == [600.0]


def test_retry_after_zero_is_honored_and_reported():
    seen: list[tuple] = []
    client, requests, sleeps = make_client(
        always(429, headers={"Retry-After": "0"}, json={}),
        max_retries=1,
        on_retry=lambda *a: seen.append(a),
    )
    with pytest.raises(RateLimitError) as exc:
        client.get("tickets/")
    assert len(requests) == 2
    assert sleeps == [0.0]
    assert seen == [("HTTP 429", 0.0, 1, 1)]
    assert exc.value.retry_after == 0.0
    assert exc.value.to_dict()["retry_after"] == 0


# -- error payloads --------------------------------------------------------------
@pytest.mark.parametrize(("status", "error_type"), [(404, NotFoundError), (429, RateLimitError)])
def test_error_payload_carries_the_server_message(status, error_type):
    client, _, _ = make_client(always(status, json={"error": "why it failed"}), max_retries=0)
    with pytest.raises(error_type) as exc:
        client.get("ticket/5/")
    assert "why it failed" in payload_text(exc)


@pytest.mark.parametrize("status", [401, 403])
def test_auth_failure_points_at_auth_login(status):
    client, _, _ = make_client(always(status, json={}))
    with pytest.raises(AuthError) as exc:
        client.get("staff/")
    payload = exc.value.to_dict()
    assert "hfox auth login" in f"{payload['error']} {payload.get('hint')}"
    assert (payload["type"], payload["exit_code"]) == ("auth", 2)


def test_network_error_message_keeps_the_transport_reason():
    def handler(request):
        raise httpx.ConnectError("Name or service not known", request=request)

    client, _, _ = make_client(handler, max_retries=0)
    with pytest.raises(NetworkError) as exc:
        client.get("staff/")
    assert "Name or service not known" in payload_text(exc)


# Each body answers true to `"error" in body` without being an object.
@pytest.mark.parametrize(
    "body", ["error: no such ticket", ["error", "no such ticket"]], ids=["string", "list"]
)
def test_json_error_body_that_is_not_an_object_still_reaches_the_payload(body):
    client, _, _ = make_client(always(400, json=body))
    with pytest.raises(APIError) as exc:
        client.get("ticket/5/")
    assert exc.value.status_code == 400
    assert "no such ticket" in payload_text(exc)


@pytest.mark.parametrize("text", ["", " \r\n\t "], ids=["empty", "whitespace"])
def test_blank_error_body_gives_no_detail(text):
    client, _, _ = make_client(always(502, text=text))
    with pytest.raises(APIError) as exc:
        client.get("ticket/5/")
    assert exc.value.detail is None
    assert "detail" not in exc.value.to_dict()


def test_write_answered_exactly_500_has_unknown_outcome():
    client, requests, _ = make_client(always(500, json={"error": "boom"}))
    with pytest.raises(APIError) as exc:
        client.request("POST", "ticket/5/", json={})
    assert len(requests) == 1
    assert exc.value.outcome_unknown is True
    assert exc.value.hint == OUTCOME_UNKNOWN_HINT


@pytest.mark.parametrize(
    "exc_type", [httpx.LocalProtocolError, httpx.WriteError, httpx.DecodingError]
)
def test_write_cut_off_mid_exchange_has_unknown_outcome(exc_type):
    client, requests, sleeps = make_client(raising(exc_type))
    with pytest.raises(NetworkError) as exc:
        client.request("POST", "tickets/", json={"subject": "x"})
    assert len(requests) == 1
    assert sleeps == []
    payload = exc.value.to_dict()
    assert payload["type"] == "network"
    assert payload.get("outcome_unknown") is True
    assert payload.get("hint") == OUTCOME_UNKNOWN_HINT


@pytest.mark.parametrize("exc_type", [httpx.ConnectTimeout, httpx.PoolTimeout])
def test_timeout_before_connecting_is_a_timeout_error(exc_type):
    client, _, _ = make_client(raising(exc_type), max_retries=0)
    with pytest.raises(RequestTimeoutError) as exc:
        client.request("POST", "tickets/", json={})
    payload = exc.value.to_dict()
    assert (payload["type"], payload["exit_code"]) == ("timeout", 5)
    assert "outcome_unknown" not in payload


# -- page walks ------------------------------------------------------------------
def test_walk_starts_at_the_requested_page_and_reports_the_last_page_read():
    truncated: list[tuple] = []
    client, requests, _ = make_client(pages(9))
    bodies = client.paginate_pages(
        "tickets/", params={"page": 3}, page_limit=2, on_truncated=lambda *a: truncated.append(a)
    )
    assert [body["data"] for body in bodies] == [[3], [4]]
    assert [request.url.params["page"] for request in requests] == ["3", "4"]
    assert truncated == [(4, 9)]


def test_walk_starting_on_the_last_page_reads_one_page():
    truncated: list[tuple] = []
    client, requests, sleeps = make_client(pages(4))
    walk = client.paginate(
        "tickets/", params={"page": 4}, on_truncated=lambda *a: truncated.append(a)
    )
    assert list(walk) == [4]
    assert len(requests) == 1
    assert sleeps == []
    assert truncated == []


@pytest.mark.parametrize("walk", ["paginate", "paginate_pages"])
def test_walk_defaults_are_ten_pages_of_fifty_100ms_apart(walk):
    truncated: list[tuple] = []
    client, requests, sleeps = make_client(pages(12))
    list(
        getattr(client, walk)(
            "tickets/", params={"q": "printer"}, on_truncated=lambda *a: truncated.append(a)
        )
    )
    assert [dict(request.url.params) for request in requests] == [
        {"q": "printer", "size": "50", "page": str(page)} for page in range(1, 11)
    ]
    assert sleeps == [0.1] * 9
    assert truncated == [(10, 12)]


def test_paginate_forwards_size_and_page_delay():
    client, requests, sleeps = make_client(pages(3))
    assert list(client.paginate("tickets/", size=20, page_delay_ms=250)) == [1, 2, 3]
    assert [request.url.params["size"] for request in requests] == ["20"] * 3
    assert sleeps == [0.25, 0.25]


def test_custom_root_key_is_walked_and_unwrapped():
    client, requests, _ = make_client(pages(2, key="items"))
    assert list(client.paginate("things/", root_key="items")) == [1, 2]
    assert len(requests) == 2


def test_null_page_info_falls_back_to_top_level_page_count():
    def handler(request):
        page = int(request.url.params["page"])
        return httpx.Response(200, json={"page_info": None, "page_count": 2, "data": [page]})

    client, _, _ = make_client(handler)
    assert list(client.paginate("tickets/")) == [1, 2]


def test_walk_leaves_the_callers_params_untouched():
    params = {"q": "printer"}
    client, _, _ = make_client(pages(2))
    list(client.paginate_pages("tickets/", params=params))
    assert params == {"q": "printer"}


def test_non_numeric_size_is_named_and_nothing_is_sent():
    client, requests, _ = make_client(pages(1))
    with pytest.raises(ValidationError, match="size"):
        list(client.paginate_pages("tickets/", params={"size": "abc"}))
    assert requests == []


# -- page envelopes of the wrong shape -------------------------------------------
WALKS = ["paginate", "paginate_pages"]


@pytest.mark.parametrize("walk", WALKS)
@pytest.mark.parametrize("page_info", [[1], "x", 5, True, [], "", 0, False])
def test_page_info_that_is_not_an_object_is_a_structured_error(walk, page_info):
    client, requests, _ = make_client(envelope(page_info=page_info))
    with pytest.raises(HfoxError) as exc:
        list(getattr(client, walk)("tickets/"))
    assert type(exc.value) is HfoxError
    assert exc.value.to_dict() == page_error("page_info")
    assert len(requests) == 1


@pytest.mark.parametrize("walk", WALKS)
@pytest.mark.parametrize("nested", [True, False])
@pytest.mark.parametrize("page_count", [True, False, "3", "", 2.5, -1, -2.0, [2], {}])
def test_page_count_that_is_not_a_whole_number_is_a_structured_error(walk, nested, page_count):
    extra = {"page_info": {"page_count": page_count}} if nested else {"page_count": page_count}
    client, requests, _ = make_client(envelope(**extra))
    with pytest.raises(HfoxError) as exc:
        list(getattr(client, walk)("assets/"))
    assert type(exc.value) is HfoxError
    assert exc.value.to_dict() == page_error("page_count", "assets/")
    assert len(requests) == 1


@pytest.mark.parametrize("walk", WALKS)
@pytest.mark.parametrize("literal", [b"NaN", b"Infinity", b"-Infinity", b"1e999"])
def test_page_count_that_is_not_finite_is_a_structured_error(walk, literal):
    client, requests, _ = make_client(raw_page_count(literal))
    with pytest.raises(HfoxError) as exc:
        list(getattr(client, walk)("tickets/", page_limit=3))
    assert exc.value.to_dict() == page_error("page_count")
    assert len(requests) == 1


def test_bad_page_count_is_reported_after_its_page_is_yielded():
    client, _, _ = make_client(envelope(page_count="2"))
    walk = client.paginate_pages("tickets/")
    assert next(walk) == {"data": [1], "page_count": "2"}
    with pytest.raises(HfoxError):
        next(walk)


@pytest.mark.parametrize("page_count", [0, 0.0, 1, 1.0])
def test_page_count_of_zero_or_one_reads_one_page(page_count):
    client, requests, sleeps = make_client(envelope(page_info={"page_count": page_count}))
    assert list(client.paginate("tickets/")) == [1]
    assert len(requests) == 1
    assert sleeps == []


def test_whole_float_page_count_is_walked_and_reported_as_an_int():
    truncated: list[tuple] = []
    client, _, _ = make_client(envelope(page_info={"page_count": 3.0}))
    walk = client.paginate("tickets/", page_limit=2, on_truncated=lambda *a: truncated.append(a))
    assert list(walk) == [1, 2]
    assert truncated == [(2, 3)]
    assert type(truncated[0][1]) is int


@pytest.mark.parametrize("extra", [{}, {"page_info": {}}, {"page_info": {"count": 4}}])
def test_envelope_without_a_page_count_reads_one_page(extra):
    client, requests, _ = make_client(envelope(**extra))
    assert list(client.paginate("tickets/")) == [1]
    assert len(requests) == 1


def test_page_info_without_a_count_falls_back_to_top_level_page_count():
    client, _, _ = make_client(envelope(page_info={"page_count": None}, page_count=2))
    assert list(client.paginate("tickets/")) == [1, 2]


def test_bad_top_level_page_count_is_not_read_past_a_page_info_count():
    client, _, _ = make_client(envelope(page_info={"page_count": 2}, page_count="x"))
    assert list(client.paginate("tickets/")) == [1, 2]


ROWLESS = [{}, {"data": None}, {"data": {"id": 1}}, {"data": "x"}, {"rows": None}]


@pytest.mark.parametrize("walk", WALKS)
@pytest.mark.parametrize("rows", ROWLESS)
@pytest.mark.parametrize("count", [{"page_info": {"page_count": 3}}, {"page_count": 2.0}])
def test_page_without_a_row_list_that_reports_more_pages_is_a_structured_error(walk, rows, count):
    client, requests, _ = make_client(always(200, json={**rows, **count}))
    with pytest.raises(HfoxError) as exc:
        list(getattr(client, walk)("tickets/", page_limit=1))
    assert type(exc.value) is HfoxError
    assert exc.value.to_dict() == rows_error(3 if "page_info" in count else 2)
    assert len(requests) == 1


@pytest.mark.parametrize("walk", WALKS)
@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"data": {"id": 1}, "page_info": "x"}, "page_info"),
        ({"page_count": "x"}, "page_count"),
        ({"data": None, "page_info": {"page_count": "3"}}, "page_count"),
    ],
)
def test_page_without_a_row_list_still_has_its_envelope_checked(walk, body, field):
    client, requests, _ = make_client(always(200, json=body))
    with pytest.raises(HfoxError) as exc:
        list(getattr(client, walk)("tickets/"))
    assert exc.value.to_dict() == page_error(field)
    assert len(requests) == 1


def test_page_without_a_row_list_is_reported_after_it_is_yielded():
    body = {"page_info": {"page_count": 3}, "data": None}
    client, _, _ = make_client(always(200, json=body))
    walk = client.paginate_pages("tickets/")
    assert next(walk) == body
    with pytest.raises(HfoxError):
        next(walk)


@pytest.mark.parametrize(
    ("body", "records"),
    [
        ({"id": 1}, []),
        ({"data": {"id": 1}}, [{"id": 1}]),
        ({"data": None, "page_info": {"page_count": 1, "count": 0}}, []),
        ({"page_info": {"page_count": 1, "count": 0}}, []),
        ({"data": {"id": 1}, "page_count": 0}, [{"id": 1}]),
        ({"rows": None, "page_count": 1.0}, []),
        ([1, 2], [1, 2]),
        ("text", []),
    ],
)
def test_body_without_a_row_list_that_reports_no_more_pages_is_one_page(body, records):
    client, requests, _ = make_client(always(200, json=body))
    assert list(client.paginate_pages("tickets/")) == [body]
    assert list(client.paginate("tickets/")) == records
    assert len(requests) == 2


def test_empty_body_is_one_page_without_rows():
    client, requests, _ = make_client(always(200))
    assert list(client.paginate_pages("tickets/")) == [None]
    assert list(client.paginate("tickets/")) == []
    assert len(requests) == 2


# -- one page, checked like a walk -----------------------------------------------
def test_get_page_returns_the_body_and_sends_the_params_as_given():
    body = {"page_info": {"page_count": 3}, "data": [1]}
    client, requests, _ = make_client(always(200, json=body))
    assert client.get_page("tickets/", params={"size": 5, "page": 2}) == body
    assert dict(requests[0].url.params) == {"size": "5", "page": "2"}
    assert len(requests) == 1


def test_get_page_reads_rows_under_a_custom_root_key():
    body = {"page_info": {"page_count": 3}, "items": [1]}
    client, _, _ = make_client(always(200, json=body))
    assert client.get_page("things/", root_key="items") == body
    with pytest.raises(HfoxError) as exc:
        client.get_page("things/")
    assert exc.value.to_dict() == rows_error(3, "things/")


BAD_PAGES = [
    ({"data": [1], "page_info": [1]}, "page_info"),
    ({"data": [1], "page_info": {"page_count": "2"}}, "page_count"),
    ({"data": [1], "page_count": -1}, "page_count"),
    ({"page_info": {"page_count": 3}}, "rows"),
    ({"page_info": {"page_count": 3}, "data": None}, "rows"),
]


def bad_page_error(field, path="tickets/"):
    return rows_error(3, path) if field == "rows" else page_error(field, path)


@pytest.mark.parametrize(("body", "field"), BAD_PAGES)
def test_get_page_applies_the_walks_envelope_check(body, field):
    client, requests, _ = make_client(always(200, json=body))
    with pytest.raises(HfoxError) as exc:
        client.get_page("tickets/")
    assert type(exc.value) is HfoxError
    assert exc.value.to_dict() == bad_page_error(field)
    assert len(requests) == 1


@pytest.mark.parametrize("body", [{"id": 1}, {"data": {"id": 1}}, [1, 2], "text"])
def test_get_page_returns_a_body_that_is_not_an_envelope(body):
    client, _, _ = make_client(always(200, json=body))
    assert client.get_page("tickets/") == body


# -- the wrong shape through the CLI ---------------------------------------------
def page_all_ctx(**kwargs):
    config = Config(subdomain="acme", api_key="k", auth_code="c")
    return AppContext(config=config, page_all=True, page_delay_ms=0, **kwargs)


def one_page_ctx(**kwargs):
    return AppContext(config=Config(subdomain="acme", api_key="k", auth_code="c"), **kwargs)


@pytest.mark.parametrize(
    ("extra", "field"),
    [({"page_info": [1]}, "page_info"), ({"page_info": {"page_count": "2"}}, "page_count")],
)
def test_bad_envelope_is_an_ndjson_error_line_after_its_page(mock_api, capsys, extra, field):
    captured = mock_api(envelope(**extra))
    with pytest.raises(typer.Exit) as exc:
        page_all_ctx().paginate("tickets/")
    assert exc.value.exit_code == 5
    page, error = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert page == {"data": [1], **extra}
    assert error == page_error(field)
    assert len(captured) == 1


def test_bad_envelope_raises_before_any_row_in_other_formats(mock_api, capsys):
    mock_api(envelope(page_info={"page_count": "2"}))
    with pytest.raises(HfoxError) as exc:
        page_all_ctx(fmt=OutputFormat.CSV).paginate("tickets/")
    assert exc.value.to_dict() == page_error("page_count")
    assert capsys.readouterr().out == ""


def test_bad_envelope_through_app_is_one_json_error_and_exit_5(mock_api, monkeypatch, capsys):
    mock_api(envelope(page_info="x"))
    for key, value in (("SUBDOMAIN", "acme"), ("API_KEY", "k"), ("AUTH_CODE", "c")):
        monkeypatch.setenv(f"HFOX_{key}", value)
    monkeypatch.setattr(sys, "argv", ["hfox", "--page-all", "-f", "csv", "assets", "list"])
    with pytest.raises(SystemExit) as exc:
        main_mod.app()
    assert exc.value.code == 5
    assert json.loads(capsys.readouterr().out) == page_error("page_info", "assets/")


@pytest.mark.parametrize("filters", [None, {"name": "x"}], ids=["unfiltered", "filtered"])
def test_rowless_page_reporting_more_pages_is_an_ndjson_error_line_after_it(
    mock_api, capsys, filters
):
    body = {"page_info": {"page_count": 3}, "data": None}
    captured = mock_api(always(200, json=body))
    with pytest.raises(typer.Exit) as exc:
        page_all_ctx(page_limit=1).paginate("tickets/", filters=filters)
    assert exc.value.exit_code == 5
    out = capsys.readouterr()
    page, error = [json.loads(line) for line in out.out.splitlines()]
    assert page == ({**body, "data": []} if filters else body)
    assert error == rows_error(3)
    assert "--page-limit" not in out.err
    assert len(captured) == 1


def test_rowless_page_reporting_more_pages_raises_in_other_formats(mock_api, capsys):
    mock_api(always(200, json={"page_info": {"page_count": 3}}))
    with pytest.raises(HfoxError) as exc:
        page_all_ctx(fmt=OutputFormat.CSV).paginate("tickets/", filters={"name": "x"})
    assert exc.value.to_dict() == rows_error(3)
    assert capsys.readouterr().out == ""


# -- the wrong shape on a single page --------------------------------------------
@pytest.mark.parametrize("fmt", list(OutputFormat))
@pytest.mark.parametrize(("body", "field"), BAD_PAGES)
def test_single_page_gets_the_walks_envelope_check(mock_api, capsys, fmt, body, field):
    captured = mock_api(always(200, json=body))
    with pytest.raises(HfoxError) as exc:
        one_page_ctx(fmt=fmt).paginate("tickets/")
    assert type(exc.value) is HfoxError
    assert exc.value.to_dict() == bad_page_error(field)
    assert capsys.readouterr().out == ""
    assert len(captured) == 1


@pytest.mark.parametrize("literal", [b"NaN", b"Infinity", b"1e999"])
def test_single_page_with_a_page_count_that_is_not_finite_is_refused(mock_api, capsys, literal):
    mock_api(raw_page_count(literal))
    with pytest.raises(HfoxError) as exc:
        one_page_ctx().paginate("tickets/")
    assert exc.value.to_dict() == page_error("page_count")
    assert capsys.readouterr().out == ""


def test_single_page_is_checked_under_its_own_root_key(mock_api):
    body = {"page_info": {"page_count": 3}, "items": [{"id": 1}]}
    mock_api(always(200, json=body))
    assert one_page_ctx().paginate("things/", root_key="items") == body
    with pytest.raises(HfoxError) as exc:
        one_page_ctx().paginate("things/")
    assert exc.value.to_dict() == rows_error(3, "things/")


@pytest.mark.parametrize(
    "body",
    [
        {"page_info": {"page_count": 3, "count": 9}, "data": [{"id": 1}]},
        {"page_info": None, "page_count": 2.0, "data": []},
        {"page_info": {"page_count": 1, "count": 0}},
        {"data": {"id": 1}},
        [{"id": 1}],
    ],
)
def test_single_page_of_an_accepted_shape_is_returned_as_sent(mock_api, body):
    mock_api(always(200, json=body))
    assert one_page_ctx().paginate("tickets/") == body


def test_single_page_dry_run_sends_nothing(mock_api, capsys):
    captured = mock_api(always(200, json={"page_info": "x"}))
    with pytest.raises(typer.Exit) as exc:
        one_page_ctx(dry_run=True).paginate("tickets/")
    assert exc.value.exit_code == 0
    assert json.loads(capsys.readouterr().out)["dry_run"] is True
    assert captured == []


LISTINGS = [
    (["tickets", "list"], "tickets/"),
    (["contacts", "list"], "users/"),
    (["assets", "list"], "assets/"),
    (["assets", "types", "list"], "asset_types/"),
    (["assets", "custom-fields", "list"], "asset_custom_fields/"),
]


@pytest.mark.parametrize(("command", "path"), LISTINGS)
@pytest.mark.parametrize("fmt", ["json", "table"])
def test_bad_single_page_through_app_is_one_json_error_and_exit_5(
    mock_api, monkeypatch, capsys, command, path, fmt
):
    mock_api(envelope(page_info={"page_count": "2"}))
    for key, value in (("SUBDOMAIN", "acme"), ("API_KEY", "k"), ("AUTH_CODE", "c")):
        monkeypatch.setenv(f"HFOX_{key}", value)
    monkeypatch.setattr(sys, "argv", ["hfox", "-f", fmt, *command, "--page", "1"])
    with pytest.raises(SystemExit) as exc:
        main_mod.app()
    assert exc.value.code == 5
    assert json.loads(capsys.readouterr().out) == page_error("page_count", path)
