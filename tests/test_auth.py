"""`hfox auth` login / status / logout (finding #40).

Fully offline: `auth login` validates against /staff/ through a patched
HappyFoxClient backed by httpx.MockTransport; status/logout never touch the
network. All state lives under an isolated HFOX_CONFIG_DIR (tmp_path).

`output.render` writes to a stdout bound at import time, so CliRunner cannot
capture rendered JSON; the status assertions therefore intercept `output.render`
to inspect the payload directly.
"""

import json

import httpx
import pytest
from typer.testing import CliRunner

import hfox.cli.auth as auth_mod
from hfox.cli.main import cli
from hfox.core.client import HappyFoxClient
from hfox.core.config import save_credentials

runner = CliRunner()


@pytest.fixture
def cfg_dir(tmp_path):
    return tmp_path / ".hfox"


def _patch_staff_client(monkeypatch, staff):
    """Make auth.login's HappyFoxClient hit a mock /staff/ endpoint."""

    def factory(base_url, api_key, auth_code, **kwargs):
        client = HappyFoxClient(base_url, api_key, auth_code, sleep=lambda _s: None)
        client._client = httpx.Client(
            transport=httpx.MockTransport(lambda req: httpx.Response(200, json=staff)),
            auth=httpx.BasicAuth(api_key, auth_code),
        )
        return client

    monkeypatch.setattr(auth_mod, "HappyFoxClient", factory)


def _capture_render(monkeypatch):
    """Intercept output.render and return a list that captures rendered payloads."""
    captured: list = []
    import hfox.cli.output as output_mod

    real = output_mod.render

    def spy(data, fmt, **kwargs):
        captured.append(data)
        return real(data, fmt, **kwargs)

    monkeypatch.setattr(output_mod, "render", spy)
    return captured


# -- login persists token.json at mode 0600 --------------------------------
def test_login_persists_token_at_0600(monkeypatch, cfg_dir):
    _patch_staff_client(monkeypatch, staff=[{"id": 7, "email": "a@x.org"}])
    result = runner.invoke(
        cli,
        ["auth", "login", "--subdomain", "acme", "--region", "us",
         "--api-key", "K", "--auth-code", "C", "--email", "a@x.org"],
        env={"HFOX_CONFIG_DIR": str(cfg_dir)},
    )
    assert result.exit_code == 0, result.stderr
    token = cfg_dir / "token.json"
    assert token.exists()
    assert (token.stat().st_mode & 0o777) == 0o600
    saved = json.loads(token.read_text())
    assert saved["api_key"] == "K"
    assert saved["auth_code"] == "C"
    assert saved["subdomain"] == "acme"
    # The matched agent's id is persisted as the default staff id.
    settings = (cfg_dir / "config.toml").read_text()
    assert "default_staff_id = 7" in settings


# -- status masks api_key and omits auth_code ------------------------------
def test_status_masks_api_key_and_omits_auth_code(monkeypatch, cfg_dir):
    save_credentials(
        cfg_dir, subdomain="acme", region="us",
        api_key="SECRETKEY123", auth_code="AUTHCODE",
    )
    captured = _capture_render(monkeypatch)
    result = runner.invoke(
        cli, ["auth", "status"], env={"HFOX_CONFIG_DIR": str(cfg_dir)}
    )
    assert result.exit_code == 0
    payload = captured[-1]
    assert payload["authenticated"] is True
    assert "auth_code" not in payload
    # api_key is masked: the secret never appears verbatim.
    assert payload["api_key"] != "SECRETKEY123"
    assert "SECRETKEY123" not in str(payload)
    assert payload["api_key"].startswith("SE")


# -- logout removes token.json ---------------------------------------------
def test_logout_removes_token(monkeypatch, cfg_dir):
    save_credentials(cfg_dir, subdomain="acme", region="us", api_key="K", auth_code="C")
    token = cfg_dir / "token.json"
    assert token.exists()
    result = runner.invoke(
        cli, ["auth", "logout"], env={"HFOX_CONFIG_DIR": str(cfg_dir)}
    )
    assert result.exit_code == 0
    assert not token.exists()
    assert "Credentials removed." in result.stderr


def test_logout_when_nothing_stored(cfg_dir):
    cfg_dir.mkdir(parents=True)
    result = runner.invoke(
        cli, ["auth", "logout"], env={"HFOX_CONFIG_DIR": str(cfg_dir)}
    )
    assert result.exit_code == 0
    assert "No stored credentials found." in result.stderr
