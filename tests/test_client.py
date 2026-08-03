import httpx
import pytest

from hfox.core.client import MAX_RETRY_AFTER_DELAY, HappyFoxClient
from hfox.core.errors import APIError, AuthError, HfoxError, NotFoundError


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


def make_client_recording_sleep(handler):
    """Like make_client, but record every backoff delay the client sleeps for."""
    slept: list[float] = []
    client = HappyFoxClient(
        "https://acme.happyfox.com/api/1.1/json", "key", "code",
        sleep=lambda s: slept.append(s),
    )
    client._client = httpx.Client(
        transport=httpx.MockTransport(handler),
        auth=httpx.BasicAuth("key", "code"),
    )
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
    assert exc.value.status_code == 429
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
