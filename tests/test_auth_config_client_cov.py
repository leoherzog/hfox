"""Coverage fill for auth.py, config.py, client.py.

Offline + fast. CLI paths use CliRunner with a patched HappyFoxClient backed by
httpx.MockTransport; core units (config.py, client.py) are exercised directly.
All state lives under an isolated HFOX_CONFIG_DIR / tmp_path.
"""

import json
import stat

import httpx
import pytest
from typer.testing import CliRunner

import hfox.cli.auth as auth_mod
import hfox.cli.output as output_mod
from hfox.cli.auth import _mask, _resolve_staff_id
from hfox.cli.main import cli
from hfox.core.client import HappyFoxClient
from hfox.core.config import (
    Config,
    config_dir,
    load_config,
    save_credentials,
    save_settings,
)
from hfox.core.errors import (
    APIError,
    AuthError,
    HfoxError,
    NotFoundError,
    ValidationError,
)

runner = CliRunner()


# =========================================================================
# helpers
# =========================================================================
def _patch_staff_client(monkeypatch, *, response_factory):
    """Patch auth.login's HappyFoxClient to a mock transport using a handler."""

    def factory(base_url, api_key, auth_code, **kwargs):
        client = HappyFoxClient(base_url, api_key, auth_code, sleep=lambda _s: None)
        client._client = httpx.Client(
            transport=httpx.MockTransport(response_factory),
            auth=httpx.BasicAuth(api_key, auth_code),
        )
        return client

    monkeypatch.setattr(auth_mod, "HappyFoxClient", factory)


def _capture_render(monkeypatch):
    captured: list = []
    real = output_mod.render

    def spy(data, fmt, **kwargs):
        captured.append(data)
        return real(data, fmt, **kwargs)

    monkeypatch.setattr(output_mod, "render", spy)
    return captured


def make_client(handler) -> HappyFoxClient:
    client = HappyFoxClient(
        "https://acme.happyfox.com/api/1.1/json", "key", "code", sleep=lambda _s: None
    )
    client._client = httpx.Client(
        transport=httpx.MockTransport(handler),
        auth=httpx.BasicAuth("key", "code"),
    )
    return client


# =========================================================================
# auth.py: _mask
# =========================================================================
def test_mask_none_returns_none():
    assert _mask(None) is None
    assert _mask("") is None


def test_mask_short_secret_is_fixed_length_dots():
    # len < 8 -> fully masked, never reveals length/short-key chars.
    assert _mask("abc") == "••••••••"
    assert _mask("1234567") == "••••••••"


def test_mask_long_secret_shows_two_edges():
    out = _mask("SECRETKEY123")
    assert out == "SE••••••23"
    assert "SECRETKEY123" not in out


# =========================================================================
# auth.py: _resolve_staff_id
# =========================================================================
def test_resolve_staff_id_no_email_returns_none():
    assert _resolve_staff_id([{"id": 1, "email": "a@x.org"}], None) is None


def test_resolve_staff_id_no_match_returns_none():
    # No member matches -> None. Non-dict members are skipped safely.
    staff = ["junk", {"id": 5, "email": "other@x.org"}]
    assert _resolve_staff_id(staff, "nobody@x.org") is None


def test_resolve_staff_id_match_is_case_insensitive():
    staff = [{"id": 9, "email": "Agent@X.ORG"}]
    assert _resolve_staff_id(staff, " agent@x.org ") == 9


def test_resolve_staff_id_null_email_never_matches():
    assert _resolve_staff_id([{"id": 5, "email": None}], "None") is None
    assert _resolve_staff_id([{"id": 6}], "None") is None


def test_resolve_staff_id_is_exact_not_substring():
    staff = [{"id": 1, "email": "jimbob@x.org"}, {"id": 2, "email": "bob@x.org"}]
    assert _resolve_staff_id(staff, "bob@x.org") == 2


def test_resolve_staff_id_blank_email_returns_none():
    assert _resolve_staff_id([{"id": 1, "email": ""}], "  ") is None


def test_resolve_staff_id_casefold():
    assert _resolve_staff_id([{"id": 3, "email": "straße@x.org"}], "STRASSE@x.org") == 3


# =========================================================================
# auth.py: login branches
# =========================================================================
def test_login_invalid_region_rejected(monkeypatch, tmp_path):
    # region not in REGION_HOSTS -> BadParameter (a usage error; app() exits 3).
    _patch_staff_client(
        monkeypatch, response_factory=lambda r: httpx.Response(200, json=[])
    )
    result = runner.invoke(
        cli,
        ["auth", "login", "--subdomain", "acme", "--region", "mars",
         "--api-key", "K", "--auth-code", "C"],
        env={"HFOX_CONFIG_DIR": str(tmp_path / ".hfox")},
    )
    assert result.exit_code != 0
    assert "region must be one of" in result.output


def test_login_region_prompt_default_us(monkeypatch, tmp_path):
    # No --region: prompt defaults to "us". Feed empty line to accept default.
    _patch_staff_client(
        monkeypatch,
        response_factory=lambda r: httpx.Response(200, json=[{"id": 1, "email": "a@x.org"}]),
    )
    cfg_dir = tmp_path / ".hfox"
    result = runner.invoke(
        cli,
        ["auth", "login", "--subdomain", "acme", "--api-key", "K", "--auth-code", "C"],
        input="\n",  # accept default region us
        env={"HFOX_CONFIG_DIR": str(cfg_dir)},
    )
    assert result.exit_code == 0, result.stderr
    saved = json.loads((cfg_dir / "token.json").read_text())
    assert saved["region"] == "us"


def test_login_validation_failure_maps_to_auth_error(monkeypatch, tmp_path):
    # /staff/ returns 401 -> client raises AuthError(HfoxError) -> wrapped.
    _patch_staff_client(
        monkeypatch,
        response_factory=lambda r: httpx.Response(401, text="nope"),
    )
    result = runner.invoke(
        cli,
        ["auth", "login", "--subdomain", "acme", "--region", "us",
         "--api-key", "K", "--auth-code", "C"],
        env={"HFOX_CONFIG_DIR": str(tmp_path / ".hfox")},
    )
    # CliRunner invokes `cli` directly (no app()-level mapping), so the HfoxError
    # propagates uncaught: exit_code 1 with the exception preserved.
    assert isinstance(result.exception, AuthError)
    assert "Could not validate credentials" in str(result.exception)


def test_login_non_list_staff_response_rejected(monkeypatch, tmp_path):
    _patch_staff_client(
        monkeypatch,
        response_factory=lambda r: httpx.Response(200, json={"unexpected": True}),
    )
    cfg_dir = tmp_path / ".hfox"
    result = runner.invoke(
        cli,
        ["auth", "login", "--subdomain", "acme", "--region", "us",
         "--api-key", "K", "--auth-code", "C"],
        env={"HFOX_CONFIG_DIR": str(cfg_dir)},
    )
    assert isinstance(result.exception, AuthError)
    assert "Unexpected response" in str(result.exception)
    # Nothing persisted on failure.
    assert not (cfg_dir / "token.json").exists()


def test_login_non_numeric_staff_id_warns_and_skips_default(monkeypatch, tmp_path):
    _patch_staff_client(
        monkeypatch,
        response_factory=lambda r: httpx.Response(
            200, json=[{"id": "not-a-number", "email": "a@x.org"}]
        ),
    )
    cfg_dir = tmp_path / ".hfox"
    result = runner.invoke(
        cli,
        ["auth", "login", "--subdomain", "acme", "--region", "us",
         "--api-key", "K", "--auth-code", "C", "--email", "a@x.org"],
        env={"HFOX_CONFIG_DIR": str(cfg_dir)},
    )
    assert result.exit_code == 0, result.stderr
    assert "Ignoring non-numeric staff id" in result.stderr
    settings = (cfg_dir / "config.toml").read_text()
    assert "default_staff_id" not in settings


def test_login_email_no_match_warns(monkeypatch, tmp_path):
    _patch_staff_client(
        monkeypatch,
        response_factory=lambda r: httpx.Response(
            200, json=[{"id": 1, "email": "someone@x.org"}]
        ),
    )
    cfg_dir = tmp_path / ".hfox"
    result = runner.invoke(
        cli,
        ["auth", "login", "--subdomain", "acme", "--region", "us",
         "--api-key", "K", "--auth-code", "C", "--email", "ghost@x.org"],
        env={"HFOX_CONFIG_DIR": str(cfg_dir)},
    )
    assert result.exit_code == 0, result.stderr
    assert "No agent matched ghost@x.org" in result.stderr
    assert "default_staff_id" not in (cfg_dir / "config.toml").read_text()


def test_login_persists_base_url_override(monkeypatch, tmp_path):
    # HFOX_BASE_URL override is honored for validation and stored to token.json.
    captured: dict = {}

    def factory(base_url, api_key, auth_code, **kwargs):
        captured["base_url"] = base_url
        client = HappyFoxClient(base_url, api_key, auth_code, sleep=lambda _s: None)
        client._client = httpx.Client(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(200, json=[{"id": 3, "email": "a@x.org"}])
            ),
            auth=httpx.BasicAuth(api_key, auth_code),
        )
        return client

    monkeypatch.setattr(auth_mod, "HappyFoxClient", factory)
    cfg_dir = tmp_path / ".hfox"
    # HFOX_BASE_URL is the root; Config.base_url appends the /api/1.1/json suffix.
    result = runner.invoke(
        cli,
        ["auth", "login", "--subdomain", "acme", "--region", "us",
         "--api-key", "K", "--auth-code", "C", "--email", "a@x.org"],
        env={
            "HFOX_CONFIG_DIR": str(cfg_dir),
            "HFOX_BASE_URL": "https://gw.internal",
        },
    )
    assert result.exit_code == 0, result.stderr
    # The override drove the client's base_url (not the subdomain-derived host).
    assert captured["base_url"] == "https://gw.internal/api/1.1/json"
    saved = json.loads((cfg_dir / "token.json").read_text())
    assert saved["base_url"] == "https://gw.internal"
    assert "Default staff id set to 3" in result.stderr


# =========================================================================
# auth.py: status with no creds (status masks None api_key)
# =========================================================================
def test_status_unauthenticated_masks_none(monkeypatch, tmp_path):
    cfg_dir = tmp_path / ".hfox"
    cfg_dir.mkdir(parents=True)
    captured = _capture_render(monkeypatch)
    result = runner.invoke(
        cli, ["auth", "status"], env={"HFOX_CONFIG_DIR": str(cfg_dir)}
    )
    assert result.exit_code == 0
    payload = captured[-1]
    assert payload["authenticated"] is False
    assert payload["api_key"] is None
    assert payload["base_url"] is None  # no subdomain or override


# =========================================================================
# config.py: config_dir resolution (override > env > default)
# =========================================================================
def test_config_dir_override_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("HFOX_CONFIG_DIR", str(tmp_path / "env"))
    assert config_dir(tmp_path / "explicit") == (tmp_path / "explicit")


def test_config_dir_empty_override_falls_to_env(monkeypatch, tmp_path):
    monkeypatch.setenv("HFOX_CONFIG_DIR", str(tmp_path / "env"))
    assert config_dir("") == (tmp_path / "env")


def test_config_dir_empty_env_falls_to_home(monkeypatch):
    monkeypatch.setenv("HFOX_CONFIG_DIR", "")
    from pathlib import Path

    assert config_dir() == Path.home() / ".hfox"


# =========================================================================
# config.py: base_url override and require_auth
# =========================================================================
def test_base_url_override_strips_trailing_slash_and_appends_suffix():
    # The override is the root; base_url strips trailing slash and appends suffix.
    cfg = Config(base_url_override="https://gw.internal/")
    assert cfg.base_url == "https://gw.internal/api/1.1/json"


def test_invalid_host_rejected():
    with pytest.raises(ValidationError):
        _ = Config(subdomain="http://acme:8080/x").base_url


def test_require_auth_raises_when_unauthenticated():
    with pytest.raises(AuthError):
        Config().require_auth()


def test_require_auth_passes_when_authenticated():
    cfg = Config(subdomain="acme", api_key="k", auth_code="c")
    assert cfg.is_authenticated is True
    cfg.require_auth()  # does not raise


# =========================================================================
# config.py: env > file > default precedence and base_url resolution
# =========================================================================
def test_load_config_env_base_url_overrides_disk(tmp_path, monkeypatch):
    cfg_dir = tmp_path / ".hfox"
    save_credentials(
        cfg_dir, subdomain="disk", region="eu", api_key="dk", auth_code="dc",
        base_url="https://disk.example",
    )
    monkeypatch.setenv("HFOX_CONFIG_DIR", str(cfg_dir))
    monkeypatch.setenv("HFOX_BASE_URL", "https://env.example")
    cfg = load_config()
    assert cfg.base_url_override == "https://env.example"
    assert cfg.region == "eu"  # from disk
    # base_url resolves from the override root + suffix.
    assert cfg.base_url == "https://env.example/api/1.1/json"


def test_load_config_defaults_when_nothing_set(tmp_path, monkeypatch):
    cfg_dir = tmp_path / ".hfox"  # does not exist
    monkeypatch.setenv("HFOX_CONFIG_DIR", str(cfg_dir))
    for k in ("HFOX_SUBDOMAIN", "HFOX_REGION", "HFOX_API_KEY",
              "HFOX_AUTH_CODE", "HFOX_FORMAT", "HFOX_STAFF_ID", "HFOX_BASE_URL"):
        monkeypatch.delenv(k, raising=False)
    cfg = load_config()
    assert cfg.region == "us"  # default
    assert cfg.default_format == "json"  # default
    assert cfg.subdomain is None
    assert cfg.api_key is None
    assert cfg.default_staff_id is None
    assert cfg.is_authenticated is False


def test_load_config_settings_fallback_when_token_missing(tmp_path, monkeypatch):
    # Only config.toml present (partial config): subdomain/region come from settings.
    cfg_dir = tmp_path / ".hfox"
    save_settings(cfg_dir, {"subdomain": "fromsettings", "region": "eu"})
    monkeypatch.setenv("HFOX_CONFIG_DIR", str(cfg_dir))
    for k in ("HFOX_SUBDOMAIN", "HFOX_REGION", "HFOX_API_KEY", "HFOX_AUTH_CODE"):
        monkeypatch.delenv(k, raising=False)
    cfg = load_config()
    assert cfg.subdomain == "fromsettings"
    assert cfg.region == "eu"
    assert cfg.api_key is None  # token.json absent


# =========================================================================
# config.py: corrupt files raise HfoxError
# =========================================================================
def test_corrupt_toml_raises_hfox_error(tmp_path):
    cfg_dir = tmp_path / ".hfox"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.toml").write_text("not = = valid toml")
    with pytest.raises(HfoxError) as exc:
        load_config(cfg_dir)
    assert "Failed to read config file" in str(exc.value)


def test_corrupt_json_raises_hfox_error(tmp_path):
    cfg_dir = tmp_path / ".hfox"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "token.json").write_text("{not valid json")
    with pytest.raises(HfoxError) as exc:
        load_config(cfg_dir)
    assert "Failed to read token file" in str(exc.value)


# =========================================================================
# config.py: atomic write modes and dir permissions (0600/0700)
# =========================================================================
def test_save_credentials_modes_0600_and_dir_0700(tmp_path):
    cfg_dir = tmp_path / ".hfox"
    path = save_credentials(cfg_dir, subdomain="a", region="us", api_key="k", auth_code="c")
    assert (path.stat().st_mode & 0o777) == 0o600
    assert (cfg_dir.stat().st_mode & 0o777) == stat.S_IRWXU  # 0700


def test_save_credentials_includes_base_url(tmp_path):
    cfg_dir = tmp_path / ".hfox"
    path = save_credentials(
        cfg_dir, subdomain="a", region="us", api_key="k", auth_code="c",
        base_url="https://gw.internal",
    )
    assert json.loads(path.read_text())["base_url"] == "https://gw.internal"


def test_save_settings_mode_0644(tmp_path):
    cfg_dir = tmp_path / ".hfox"
    path = save_settings(cfg_dir, {"subdomain": "a"})
    assert (path.stat().st_mode & 0o777) == 0o644


def test_save_settings_chmod_failure_warns(tmp_path, monkeypatch, capsys):
    cfg_dir = tmp_path / ".hfox"
    import hfox.core.config as config_mod

    orig_chmod = config_mod.Path.chmod

    def fake_chmod(self, mode):
        # Only fail the 0700 dir chmod; let the temp-file chmod proceed.
        if mode == stat.S_IRWXU:
            raise OSError("denied")
        return orig_chmod(self, mode)

    monkeypatch.setattr(config_mod.Path, "chmod", fake_chmod)
    save_settings(cfg_dir, {"subdomain": "a"})
    err = capsys.readouterr().err
    assert "could not restrict permissions" in err


# =========================================================================
# config.py: save_settings coercions and toml str
# =========================================================================
def test_save_settings_non_int_staff_id_raises(tmp_path):
    cfg_dir = tmp_path / ".hfox"
    with pytest.raises(ValidationError) as exc:
        save_settings(cfg_dir, {"default_staff_id": "abc"})
    assert "must be a non-negative integer" in str(exc.value)


def test_save_settings_bool_value_serialized_lowercase(tmp_path):
    cfg_dir = tmp_path / ".hfox"
    path = save_settings(cfg_dir, {"some_flag": True, "other_flag": False})
    text = path.read_text()
    assert "some_flag = true" in text
    assert "other_flag = false" in text


def test_save_settings_string_with_quotes_escaped(tmp_path):
    cfg_dir = tmp_path / ".hfox"
    path = save_settings(cfg_dir, {"subdomain": 'a"b\\c'})
    # Round-trips through tomllib via load_config.
    cfg = load_config(cfg_dir)
    assert cfg.subdomain == 'a"b\\c'
    assert '\\"' in path.read_text()


def test_save_settings_control_char_rejected(tmp_path):
    cfg_dir = tmp_path / ".hfox"
    with pytest.raises(ValidationError) as exc:
        save_settings(cfg_dir, {"subdomain": "a\nb"})
    assert "control characters" in str(exc.value)


# =========================================================================
# client.py: context manager
# =========================================================================
def test_client_context_manager_closes():
    client = make_client(lambda r: httpx.Response(200, json={"ok": True}))
    closed = {"n": 0}
    orig = client._client.close
    client._client.close = lambda: (closed.__setitem__("n", closed["n"] + 1), orig())[1]
    with client as c:
        assert c is client
        assert c.get("staff/") == {"ok": True}
    assert closed["n"] == 1


# =========================================================================
# client.py: non-numeric Retry-After falls back to backoff
# =========================================================================
def test_non_numeric_retry_after_falls_back_to_backoff():
    calls = {"n": 0}
    slept: list[float] = []
    client = HappyFoxClient(
        "https://acme.happyfox.com/api/1.1/json", "key", "code",
        sleep=lambda s: slept.append(s),
    )

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            # HTTP-date style header is non-numeric -> ValueError -> backoff.
            return httpx.Response(
                429, headers={"Retry-After": "Wed, 21 Oct 2025 07:28:00 GMT"}, json={}
            )
        return httpx.Response(200, json={"ok": True})

    client._client = httpx.Client(
        transport=httpx.MockTransport(handler), auth=httpx.BasicAuth("key", "code")
    )
    assert client.get("tickets/") == {"ok": True}
    # Backoff for attempt 0: min(1*2^0, 60) + jitter[0] = 1 + 0.13.
    assert slept == [pytest.approx(1.0 + 0.13)]


# =========================================================================
# client.py: success body shapes
# =========================================================================
def test_empty_success_body_returns_none():
    client = make_client(lambda r: httpx.Response(204))
    assert client.get("tickets/1/") is None


def test_non_json_success_body_returns_text():
    client = make_client(
        lambda r: httpx.Response(200, text="plain text", headers={"content-type": "text/plain"})
    )
    assert client.get("export/") == "plain text"


# =========================================================================
# client.py: error mapping for other status codes
# =========================================================================
def test_500_maps_to_api_error():
    client = make_client(lambda r: httpx.Response(500, json={"error": "boom"}))
    with pytest.raises(APIError) as exc:
        client.get("tickets/")
    assert exc.value.status_code == 500
    assert exc.value.detail == "boom"


def test_403_maps_to_auth_error():
    client = make_client(lambda r: httpx.Response(403, json={"error": "forbidden"}))
    with pytest.raises(AuthError):
        client.get("tickets/")


def test_404_maps_to_not_found():
    client = make_client(lambda r: httpx.Response(404, text="missing"))
    with pytest.raises(NotFoundError):
        client.get("ticket/1/")


# =========================================================================
# client.py: _unwrap_page edge cases and _extract_error
# =========================================================================
def test_paginate_non_list_records_wrapped_as_single():
    client = make_client(
        lambda r: httpx.Response(200, json={"data": {"id": 1}, "page_info": {}})
    )
    records = list(client.paginate("tickets/", page_limit=1))
    assert records == [{"id": 1}]


def test_paginate_scalar_body_yields_nothing():
    client = make_client(lambda r: httpx.Response(200, json=42))
    records = list(client.paginate("weird/", page_limit=1))
    assert records == []


def test_extract_error_dict_without_error_key_returns_whole_body():
    client = make_client(lambda r: httpx.Response(500, json={"message": "oops", "code": 9}))
    with pytest.raises(APIError) as exc:
        client.get("tickets/")
    assert exc.value.detail == {"message": "oops", "code": 9}


def test_extract_error_non_json_returns_truncated_text():
    long = "x" * 1000
    client = make_client(
        lambda r: httpx.Response(500, text=long, headers={"content-type": "text/plain"})
    )
    with pytest.raises(APIError) as exc:
        client.get("tickets/")
    assert exc.value.detail == "x" * 500  # truncated to 500 chars
