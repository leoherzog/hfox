"""Shared fixtures: config isolation for every test, an offline `mock_api` transport, a
terminal stand-in and the environment for subprocess tests.
"""

import os

# Typer reads these at import and then styles help with ANSI codes, even when piped.
for _key in ("GITHUB_ACTIONS", "FORCE_COLOR", "PY_COLORS"):
    os.environ.pop(_key, None)

import httpx  # noqa: E402
import pytest  # noqa: E402

import hfox.cli._util as util_mod  # noqa: E402
import hfox.cli.context as context_mod  # noqa: E402
from hfox.core.client import HappyFoxClient  # noqa: E402


def subprocess_env(**extra: str) -> dict[str, str]:
    """Return os.environ without HFOX_* and XDG_CONFIG_HOME, plus the isolated
    HFOX_CONFIG_DIR, HOME and USERPROFILE, plus `extra`.

    PATH and SYSTEMROOT stay, which Windows needs to start Python.
    """
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("HFOX_") and key != "XDG_CONFIG_HOME"
    }
    for key in ("HFOX_CONFIG_DIR", "HOME", "USERPROFILE"):
        if key in os.environ:
            env[key] = os.environ[key]
    env.update(extra)
    return env


@pytest.fixture(autouse=True)
def _isolated_config(monkeypatch, tmp_path):
    """Keep tests off the real home and config directory.

    Subprocess helpers must forward HFOX_CONFIG_DIR, HOME and USERPROFILE.
    """
    for key in [k for k in os.environ if k.startswith("HFOX_")]:
        monkeypatch.delenv(key)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setenv("HFOX_CONFIG_DIR", str(tmp_path / "cfg"))


@pytest.fixture
def mock_api(monkeypatch):
    """Return a factory: install(handler) -> list of captured httpx.Request.

    The client keeps the caller's `max_retries` and `on_retry`; `timeout` is ignored and
    sleeps are instant.
    """

    def install(handler):
        captured: list[httpx.Request] = []

        def wrapped(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return handler(request)

        def factory(base_url, api_key, auth_code, **kwargs):
            passed = {k: kwargs[k] for k in ("max_retries", "on_retry") if k in kwargs}
            client = HappyFoxClient(
                base_url, api_key, auth_code, sleep=lambda _s: None, **passed
            )
            client._client = httpx.Client(
                transport=httpx.MockTransport(wrapped),
                auth=httpx.BasicAuth(api_key, auth_code),
            )
            return client

        monkeypatch.setattr(context_mod, "HappyFoxClient", factory)
        return captured

    return install


@pytest.fixture
def tty(monkeypatch):
    """Make stdin look like a terminal, so prompts run and read CliRunner input."""
    monkeypatch.setattr(util_mod, "stdin_is_tty", lambda: True)
