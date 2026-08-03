import json

import pytest

from hfox.core.config import Config, load_config, save_credentials, save_settings
from hfox.core.errors import AuthError


def test_base_url_us_eu_custom():
    assert Config(subdomain="acme", region="us").base_url == (
        "https://acme.happyfox.com/api/1.1/json"
    )
    assert Config(subdomain="acme", region="eu").base_url == (
        "https://acme.happyfox.net/api/1.1/json"
    )
    # A dotted subdomain is treated as a full custom host.
    assert Config(subdomain="support.acme.com").base_url == (
        "https://support.acme.com/api/1.1/json"
    )


def test_base_url_requires_account():
    with pytest.raises(AuthError):
        _ = Config().base_url


def test_env_overrides_take_precedence(tmp_path, monkeypatch):
    cfg_dir = tmp_path / ".hfox"
    save_credentials(
        cfg_dir, subdomain="disk", region="us", api_key="diskkey", auth_code="diskcode"
    )
    monkeypatch.setenv("HFOX_CONFIG_DIR", str(cfg_dir))
    monkeypatch.setenv("HFOX_API_KEY", "envkey")
    cfg = load_config()
    assert cfg.api_key == "envkey"  # env wins
    assert cfg.auth_code == "diskcode"  # falls back to disk
    assert cfg.subdomain == "disk"


def test_credentials_roundtrip_and_permissions(tmp_path):
    cfg_dir = tmp_path / ".hfox"
    path = save_credentials(
        cfg_dir, subdomain="acme", region="eu", api_key="k", auth_code="c"
    )
    assert json.loads(path.read_text())["region"] == "eu"
    # token.json must be 0600.
    assert (path.stat().st_mode & 0o777) == 0o600


def test_settings_default_staff_id(tmp_path, monkeypatch):
    cfg_dir = tmp_path / ".hfox"
    save_settings(cfg_dir, {"subdomain": "acme", "default_staff_id": 9})
    monkeypatch.setenv("HFOX_CONFIG_DIR", str(cfg_dir))
    assert load_config().default_staff_id == 9
