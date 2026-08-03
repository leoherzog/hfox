"""Shared test fixtures.

`mock_api` patches the HappyFoxClient that AppContext builds so CLI commands run
against an in-process httpx.MockTransport (no network) with backoff sleep
neutralized. A test supplies a request handler and gets back a list capturing
every request the command issued.
"""

import httpx
import pytest

import hfox.cli.context as context_mod
from hfox.core.client import HappyFoxClient


@pytest.fixture
def mock_api(monkeypatch):
    """Return a factory: install(handler) -> list of captured httpx.Request."""

    def install(handler):
        captured: list[httpx.Request] = []

        def wrapped(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return handler(request)

        def factory(base_url, api_key, auth_code, **kwargs):
            client = HappyFoxClient(base_url, api_key, auth_code, sleep=lambda _s: None)
            client._client = httpx.Client(
                transport=httpx.MockTransport(wrapped),
                auth=httpx.BasicAuth(api_key, auth_code),
            )
            return client

        monkeypatch.setattr(context_mod, "HappyFoxClient", factory)
        return captured

    return install
