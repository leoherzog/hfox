"""HappyFoxClient edges: wire headers, redirects, retry scope and timing, errors and page walks."""

import base64
import json

import httpx
import pytest

from hfox.core.client import OUTCOME_UNKNOWN_HINT, HappyFoxClient, request_url
from hfox.core.errors import (
    APIError,
    AuthError,
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
