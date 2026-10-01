import httpx
import pytest

from hfox import __version__
from hfox.core.client import (
    DEFAULT_TIMEOUT,
    MAX_RETRIES,
    MAX_RETRY_AFTER_DELAY,
    OUTCOME_UNKNOWN_HINT,
    USER_AGENT,
    HappyFoxClient,
)
from hfox.core.errors import (
    APIError,
    AuthError,
    HfoxError,
    NetworkError,
    NotFoundError,
    RateLimitError,
    RequestTimeoutError,
)


def make_client(handler) -> HappyFoxClient:
    """Build a client whose transport is a deterministic mock (no network, no sleep)."""
    client = HappyFoxClient(
        "https://acme.happyfox.com/api/1.1/json", "key", "code", sleep=lambda _s: None
    )
    client._client = httpx.Client(
        transport=httpx.MockTransport(handler),
        auth=httpx.BasicAuth("key", "code"),
    )
    return client


def test_basic_auth_header_is_sent():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization", "")
        return httpx.Response(200, json={"ok": True})

    make_client(handler).get("staff/")
    assert seen["auth"].startswith("Basic ")


def test_pagination_walks_pages():
    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "1"))
        return httpx.Response(
            200,
            json={
                "page_info": {"page_count": 3},
                "data": [{"id": page * 10}, {"id": page * 10 + 1}],
            },
        )

    records = list(make_client(handler).paginate("tickets/", page_limit=10))
    assert [r["id"] for r in records] == [10, 11, 20, 21, 30, 31]


def test_pagination_respects_page_limit():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"page_info": {"page_count": 99}, "data": [{"x": 1}]})

    records = list(make_client(handler).paginate("tickets/", page_limit=2))
    assert len(records) == 2


def test_429_is_retried_then_succeeds():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429, json={"error": "slow down"})
        return httpx.Response(200, json={"done": True})

    assert make_client(handler).get("tickets/") == {"done": True}
    assert calls["n"] == 3


def test_404_maps_to_not_found():
    def handler(request):
        return httpx.Response(404, json={"error": "nope"})

    with pytest.raises(NotFoundError):
        make_client(handler).get("ticket/999/")


def test_401_maps_to_auth_error():
    def handler(request):
        return httpx.Response(401, text="unauthorized")

    with pytest.raises(AuthError):
        make_client(handler).get("staff/")


def test_bool_params_lowercased():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["minify"] = request.url.params.get("minify_response")
        seen["has_skip"] = "skip" in request.url.params
        return httpx.Response(200, json=[])

    make_client(handler).get("tickets/", params={"minify_response": True, "skip": None})
    assert seen["minify"] == "true"
    # A None-valued param is dropped entirely (not sent as "skip=" or "None").
    assert seen["has_skip"] is False


def make_client_recording_sleep(handler, **kwargs):
    """Like make_client, but record every backoff delay the client sleeps for."""
    slept: list[float] = []
    client = HappyFoxClient(
        "https://acme.happyfox.com/api/1.1/json", "key", "code",
        sleep=lambda s: slept.append(s),
        **kwargs,
    )
    # Swap only the transport so the client keeps its own headers and timeout.
    client._client._transport = httpx.MockTransport(handler)
    return client, slept


# -- #37: network errors and 429 exhaustion --------------------------------
def test_transient_network_error_retried_then_succeeds():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200, json={"done": True})

    client, slept = make_client_recording_sleep(handler)
    assert client.get("tickets/") == {"done": True}
    assert calls["n"] == 3
    # Two failures => two backoff sleeps before the success.
    assert len(slept) == 2


def test_persistent_network_error_raised_as_hfox_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    client, _ = make_client_recording_sleep(handler)
    with pytest.raises(HfoxError) as exc:
        client.get("tickets/")
    assert "Network error" in str(exc.value)
    assert type(exc.value) is NetworkError
    assert exc.value.detail == "ConnectError"


def test_retry_after_full_cooldown_passed_through():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            # HappyFox's documented 10-minute cooldown must survive unclamped.
            return httpx.Response(429, headers={"Retry-After": "600"}, json={})
        return httpx.Response(200, json={"ok": True})

    client, slept = make_client_recording_sleep(handler)
    assert client.get("tickets/") == {"ok": True}
    assert slept == [600.0]


def test_numeric_retry_after_header_drives_clamped_sleep():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            # Hostile/large Retry-After must be clamped to MAX_RETRY_AFTER_DELAY.
            return httpx.Response(429, headers={"Retry-After": "99999"}, json={})
        return httpx.Response(200, json={"ok": True})

    client, slept = make_client_recording_sleep(handler)
    assert client.get("tickets/") == {"ok": True}
    assert slept == [MAX_RETRY_AFTER_DELAY]


def test_retry_after_fractional_seconds_passed_through():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "2.5"}, json={})
        return httpx.Response(200, json={"ok": True})

    client, slept = make_client_recording_sleep(handler)
    assert client.get("tickets/") == {"ok": True}
    assert slept == [2.5]


def test_always_429_exhausts_retries_and_raises_api_error():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429, json={"error": "slow down"})

    client, slept = make_client_recording_sleep(handler)
    with pytest.raises(APIError) as exc:
        client.get("tickets/")
    assert isinstance(exc.value, RateLimitError)
    assert exc.value.status_code == 429
    assert exc.value.retry_after is None
    assert "retry_after" not in exc.value.to_dict()
    # max_retries (5) backoff sleeps, then a final attempt that raises.
    assert len(slept) == client.max_retries
    assert calls["n"] == client.max_retries + 1


# -- #39: paginate handles all three response envelopes ---------------------
def test_paginate_reports_envelope_page_count_and_rows():
    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "1"))
        return httpx.Response(
            200,
            # reports module shape: top-level page_count + rows
            json={"page_count": 2, "rows": [{"r": page}]},
        )

    records = list(make_client(handler).paginate("reports/1/", page_limit=10))
    assert [r["r"] for r in records] == [1, 2]


def test_paginate_bare_list_single_page():
    def handler(request: httpx.Request) -> httpx.Response:
        # A bare list (no envelope) has no page_count -> single page only.
        return httpx.Response(200, json=[{"id": 1}, {"id": 2}])

    records = list(make_client(handler).paginate("staff/", page_limit=10))
    assert [r["id"] for r in records] == [1, 2]


def test_paginate_single_page_when_page_count_one():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            200, json={"page_info": {"page_count": 1}, "data": [{"id": 1}]}
        )

    records = list(make_client(handler).paginate("tickets/", page_limit=10))
    assert [r["id"] for r in records] == [1]
    assert calls["n"] == 1  # did not fetch a phantom second page


def test_paginate_clamps_size_to_50():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["size"] = request.url.params.get("size")
        return httpx.Response(200, json={"page_info": {"page_count": 1}, "data": []})

    list(make_client(handler).paginate("tickets/", size=500, page_limit=1))
    assert seen["size"] == "50"


def test_paginate_non_numeric_page_raises_validation():
    from hfox.core.errors import ValidationError

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"page_info": {"page_count": 1}, "data": []})

    with pytest.raises(ValidationError):
        list(make_client(handler).paginate("tickets/", params={"page": "abc"}))


# -- User-Agent, timeout and retry reporting --------------------------------
def test_user_agent_carries_version():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["ua"] = request.headers["user-agent"]
        return httpx.Response(200, json=[])

    client, _ = make_client_recording_sleep(handler)
    client.get("staff/")
    assert USER_AGENT == f"hfox/{__version__}"
    assert seen["ua"] == USER_AGENT


def test_timeout_reaches_httpx_client():
    client = HappyFoxClient("https://acme.happyfox.com/api/1.1/json", "key", "code", timeout=7.5)
    assert client._client.timeout == httpx.Timeout(7.5)
    client.close()


def test_default_timeout_and_retries():
    client = HappyFoxClient("https://acme.happyfox.com/api/1.1/json", "key", "code")
    assert client._client.timeout == httpx.Timeout(DEFAULT_TIMEOUT)
    assert client.max_retries == MAX_RETRIES
    client.close()


def test_on_retry_called_once_per_429_retry():
    calls = {"n": 0}
    seen: list[tuple] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "2.5"}, json={})
        if calls["n"] == 2:
            return httpx.Response(429, json={})
        return httpx.Response(200, json={"ok": True})

    client, slept = make_client_recording_sleep(handler, on_retry=lambda *a: seen.append(a))
    assert client.get("tickets/") == {"ok": True}
    assert seen == [
        ("HTTP 429", 2.5, 1, MAX_RETRIES),
        ("HTTP 429", client._backoff(1), 2, MAX_RETRIES),
    ]
    assert slept == [2.5, client._backoff(1)]


def test_on_retry_called_before_the_sleep():
    order: list[str] = []
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429 if calls["n"] == 1 else 200, json={})

    client = HappyFoxClient(
        "https://acme.happyfox.com/api/1.1/json", "key", "code",
        sleep=lambda _s: order.append("sleep"),
        on_retry=lambda *_a: order.append("notify"),
    )
    client._client._transport = httpx.MockTransport(handler)
    client.get("tickets/")
    assert order == ["notify", "sleep"]


def test_on_retry_called_for_transport_error():
    calls = {"n": 0}
    seen: list[tuple] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200, json={"ok": True})

    client, slept = make_client_recording_sleep(
        handler, max_retries=3, on_retry=lambda *a: seen.append(a)
    )
    assert client.request("POST", "tickets/", json={}) == {"ok": True}
    assert seen == [("network error: ConnectError", client._backoff(0), 1, 3)]
    assert slept == [client._backoff(0)]


def test_on_retry_not_called_for_page_delay():
    seen: list[tuple] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"page_info": {"page_count": 3}, "data": [1]})

    client, slept = make_client_recording_sleep(handler, on_retry=lambda *a: seen.append(a))
    assert len(list(client.paginate("tickets/", page_delay_ms=100))) == 3
    assert slept == [0.1, 0.1]
    assert seen == []


@pytest.mark.parametrize("status", [429, 503])
def test_max_retries_zero_sends_one_request(status):
    calls = {"n": 0}
    seen: list[tuple] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(status, json={})

    client, slept = make_client_recording_sleep(
        handler, max_retries=0, on_retry=lambda *a: seen.append(a)
    )
    with pytest.raises(APIError):
        client.get("tickets/")
    assert calls["n"] == 1
    assert slept == []
    assert seen == []


def test_max_retries_zero_does_not_retry_transport_error():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("boom", request=request)

    client, slept = make_client_recording_sleep(handler, max_retries=0)
    with pytest.raises(NetworkError):
        client.get("tickets/")
    assert calls["n"] == 1
    assert slept == []


@pytest.mark.parametrize(
    ("header", "expected", "rendered"),
    [("120", 120.0, 120), ("2.5", 2.5, 2.5), ("99999", 99999.0, 99999)],
)
def test_exhausted_429_carries_unclamped_retry_after(header, expected, rendered):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": header}, json={"error": "slow"})

    client, slept = make_client_recording_sleep(handler, max_retries=1)
    with pytest.raises(RateLimitError) as exc:
        client.get("tickets/")
    assert exc.value.retry_after == expected
    assert slept == [min(expected, MAX_RETRY_AFTER_DELAY)]
    payload = exc.value.to_dict()
    assert payload["type"] == "rate_limited"
    assert payload["status_code"] == 429
    assert payload["retry_after"] == rendered
    assert type(payload["retry_after"]) is type(rendered)
    assert "HTTP 429" in payload["error"]


@pytest.mark.parametrize("header", [None, "", "soon", "-5", "nan", "inf"])
def test_exhausted_429_without_usable_retry_after(header):
    headers = {} if header is None else {"Retry-After": header}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers=headers, json={})

    client, _ = make_client_recording_sleep(handler, max_retries=0)
    with pytest.raises(RateLimitError) as exc:
        client.get("tickets/")
    assert exc.value.retry_after is None
    assert "retry_after" not in exc.value.to_dict()


def test_read_timeout_raises_request_timeout_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    client, _ = make_client_recording_sleep(handler, max_retries=1)
    with pytest.raises(RequestTimeoutError) as exc:
        client.get("tickets/")
    assert exc.value.type == "timeout"
    assert exc.value.message == "Request to HappyFox timed out (ReadTimeout)."
    assert exc.value.detail == "ReadTimeout"
    assert exc.value.outcome_unknown is False
    assert int(exc.value.exit_code) == 5


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE"])
def test_write_answered_504_has_unknown_outcome(method):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(504, text="gateway timeout")

    client, _ = make_client_recording_sleep(handler)
    with pytest.raises(APIError) as exc:
        client.request(method, "ticket/5/", json={})
    assert exc.value.outcome_unknown is True
    assert exc.value.hint == OUTCOME_UNKNOWN_HINT
    payload = exc.value.to_dict()
    assert payload["status_code"] == 504
    assert payload["outcome_unknown"] is True
    assert payload["hint"] == "The write may have been applied. Check the resource before retrying."


def test_get_answered_504_has_known_outcome():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(504, text="gateway timeout")

    client, _ = make_client_recording_sleep(handler)
    with pytest.raises(APIError) as exc:
        client.get("ticket/5/")
    assert exc.value.outcome_unknown is False
    assert exc.value.hint is None
    assert "outcome_unknown" not in exc.value.to_dict()
    assert "hint" not in exc.value.to_dict()


@pytest.mark.parametrize("status", [400, 409, 429])
def test_write_answered_below_500_has_known_outcome(status):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": "no"})

    client, _ = make_client_recording_sleep(handler, max_retries=0)
    with pytest.raises(APIError) as exc:
        client.request("POST", "tickets/", json={})
    assert exc.value.outcome_unknown is False
    assert exc.value.hint is None
