"""Shared fixtures: config isolation for every test and an offline `mock_api` transport."""

import os

import httpx
import pytest

import hfox.cli.context as context_mod
from hfox.core.client import HappyFoxClient


@pytest.fixture(autouse=True)
def _isolated_config(monkeypatch, tmp_path):
    """Keep tests off the developer's ~/.hfox; subprocess helpers must forward HFOX_CONFIG_DIR."""
    for key in [k for k in os.environ if k.startswith("HFOX_")]:
        monkeypatch.delenv(key)
    monkeypatch.setenv("HFOX_CONFIG_DIR", str(tmp_path / "cfg"))


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
