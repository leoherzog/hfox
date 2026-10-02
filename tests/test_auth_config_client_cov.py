"""Tests for auth.py, config.py and client.py.

CLI paths use CliRunner with `hfox.cli.context.HappyFoxClient` patched to an
httpx.MockTransport; core units are exercised directly.
"""

import json
import stat
import sys

import httpx
import pytest
from typer.testing import CliRunner

import hfox.cli.context as context_mod
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
    NetworkError,
    NotFoundError,
    RateLimitError,
    ValidationError,
)

runner = CliRunner()


# =========================================================================
# helpers
# =========================================================================
def _patch_staff_client(monkeypatch, *, response_factory):
    """Back every client the CLI builds with a mock transport using a handler."""

    def factory(base_url, api_key, auth_code, **kwargs):
        client = HappyFoxClient(base_url, api_key, auth_code, sleep=lambda _s: None)
        client._client = httpx.Client(
            transport=httpx.MockTransport(response_factory),
            auth=httpx.BasicAuth(api_key, auth_code),
        )
        return client

    monkeypatch.setattr(context_mod, "HappyFoxClient", factory)


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
    # Under 8 characters the mask shows none of the secret.
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
    assert _resolve_staff_id([{"id": 1, "email": "a@x.org"}], None) == (None, None)


def test_resolve_staff_id_no_match_gives_a_reason():
    # Non-dict members are skipped.
    staff = ["junk", {"id": 5, "email": "other@x.org"}]
    assert _resolve_staff_id(staff, "nobody@x.org") == (
        None, "No agent matched nobody@x.org; default staff id not set."
    )


def test_resolve_staff_id_match_is_case_insensitive():
    staff = [{"id": 9, "email": "Agent@X.ORG"}]
    assert _resolve_staff_id(staff, " agent@x.org ") == (9, None)


def test_resolve_staff_id_null_email_never_matches():
    assert _resolve_staff_id([{"id": 5, "email": None}], "None")[0] is None
    assert _resolve_staff_id([{"id": 6}], "None")[0] is None


def test_resolve_staff_id_is_exact_not_substring():
    staff = [{"id": 1, "email": "jimbob@x.org"}, {"id": 2, "email": "bob@x.org"}]
    assert _resolve_staff_id(staff, "bob@x.org") == (2, None)


def test_resolve_staff_id_blank_email_returns_none():
    assert _resolve_staff_id([{"id": 1, "email": ""}], "  ") == (None, None)


def test_resolve_staff_id_casefold():
    assert _resolve_staff_id([{"id": 3, "email": "straße@x.org"}], "STRASSE@x.org") == (3, None)


def test_resolve_staff_id_several_matches_give_a_reason():
    staff = [{"id": 1, "email": "a@x.org"}, {"id": 2, "email": "A@x.org"}]
    assert _resolve_staff_id(staff, "a@x.org") == (
        None, "2 agents matched a@x.org; default staff id not set."
    )


@pytest.mark.parametrize("bad_id", [True, False, 1.9, 2.0, -1, "abc", "", None, {"id": 1}])
def test_resolve_staff_id_rejects_an_unusable_id(bad_id):
    staff_id, reason = _resolve_staff_id([{"id": bad_id, "email": "a@x.org"}], "a@x.org")
    assert staff_id is None
    assert reason == f"Ignoring unexpected staff id {bad_id!r}; default staff id not set."


@pytest.mark.parametrize(("raw", "expected"), [(0, 0), (7, 7), ("7", 7), (" 12 ", 12)])
def test_resolve_staff_id_accepts_whole_numbers(raw, expected):
    assert _resolve_staff_id([{"id": raw, "email": "a@x.org"}], "a@x.org") == (expected, None)


# =========================================================================
# auth.py: login branches
# =========================================================================
def test_login_invalid_region_rejected(monkeypatch, tmp_path):
    _patch_staff_client(
        monkeypatch, response_factory=lambda r: httpx.Response(200, json=[])
    )
    result = runner.invoke(
        cli,
        ["auth", "login", "--subdomain", "acme", "--region", "mars",
         "--api-key", "K", "--auth-code", "C"],
        env={"HFOX_CONFIG_DIR": str(tmp_path / "hfox")},
    )
    assert isinstance(result.exception, ValidationError)
    assert "region must be one of" in str(result.exception)


def test_login_region_prompt_default_us(monkeypatch, tmp_path, tty):
    _patch_staff_client(
        monkeypatch,
        response_factory=lambda r: httpx.Response(200, json=[{"id": 1, "email": "a@x.org"}]),
    )
    cfg_dir = tmp_path / "hfox"
    result = runner.invoke(
        cli,
        ["auth", "login", "--subdomain", "acme", "--api-key", "K", "--auth-code", "C"],
        input="\n",  # accept default region us
        env={"HFOX_CONFIG_DIR": str(cfg_dir)},
    )
    assert result.exit_code == 0, result.stderr
    saved = json.loads((cfg_dir / "token.json").read_text())
    assert saved["region"] == "us"
    assert "Data center [us/eu]" in result.stderr
    assert json.loads(result.stdout)["region"] == "us"


def test_login_no_region_without_tty_defaults_to_us(monkeypatch, tmp_path):
    _patch_staff_client(
        monkeypatch, response_factory=lambda r: httpx.Response(200, json=[])
    )
    cfg_dir = tmp_path / "hfox"
    result = runner.invoke(
        cli,
        ["auth", "login", "--subdomain", "acme", "--api-key", "K", "--auth-code", "C"],
        env={"HFOX_CONFIG_DIR": str(cfg_dir)},
    )
    assert result.exit_code == 0, result.stderr
    assert json.loads((cfg_dir / "token.json").read_text())["region"] == "us"
    assert "Data center" not in result.stderr


def test_login_missing_secret_without_tty_builds_no_client(monkeypatch, tmp_path):
    monkeypatch.setattr(
        context_mod, "HappyFoxClient", lambda *a, **k: pytest.fail("client built")
    )
    result = runner.invoke(
        cli,
        ["auth", "login", "--subdomain", "acme", "--api-key", "K"],
        env={"HFOX_CONFIG_DIR": str(tmp_path / "hfox")},
    )
    assert isinstance(result.exception, ValidationError)
    assert str(result.exception).startswith("Missing --auth-code; stdin is not a terminal")


def test_login_validation_failure_maps_to_auth_error(monkeypatch, tmp_path):
    _patch_staff_client(
        monkeypatch,
        response_factory=lambda r: httpx.Response(401, text="nope"),
    )
    result = runner.invoke(
        cli,
        ["auth", "login", "--subdomain", "acme", "--region", "us",
         "--api-key", "K", "--auth-code", "C"],
        env={"HFOX_CONFIG_DIR": str(tmp_path / "hfox")},
    )
    assert type(result.exception) is AuthError
    assert "Could not validate credentials" in str(result.exception)
    assert result.exception.detail == "nope"
    assert result.exception.to_dict()["type"] == "auth"


@pytest.mark.parametrize(
    ("response", "error_type", "slug", "exit_code"),
    [
        (httpx.Response(404, text="missing"), NotFoundError, "not_found", 4),
        (httpx.Response(500, json={"error": "boom"}), APIError, "api", 1),
        (httpx.Response(429, json={}), RateLimitError, "rate_limited", 1),
    ],
)
def test_login_other_probe_errors_are_not_wrapped(
    monkeypatch, tmp_path, response, error_type, slug, exit_code
):
    _patch_staff_client(monkeypatch, response_factory=lambda r: response)
    cfg_dir = tmp_path / "hfox"
    result = runner.invoke(
        cli,
        ["auth", "login", "--subdomain", "acme", "--region", "us",
         "--api-key", "K", "--auth-code", "C"],
        env={"HFOX_CONFIG_DIR": str(cfg_dir)},
    )
    assert type(result.exception) is error_type
    assert "Could not validate credentials" not in str(result.exception)
    payload = result.exception.to_dict()
    assert (payload["type"], payload["exit_code"]) == (slug, exit_code)
    assert not (cfg_dir / "token.json").exists()


def test_login_transport_error_is_network_error(monkeypatch, tmp_path):
    def handler(request):
        raise httpx.ConnectError("boom", request=request)

    _patch_staff_client(monkeypatch, response_factory=handler)
    result = runner.invoke(
        cli,
        ["auth", "login", "--subdomain", "acme", "--region", "us",
         "--api-key", "K", "--auth-code", "C"],
        env={"HFOX_CONFIG_DIR": str(tmp_path / "hfox")},
    )
    assert type(result.exception) is NetworkError
    payload = result.exception.to_dict()
    assert (payload["type"], payload["exit_code"]) == ("network", 5)


def test_login_non_list_staff_response_rejected(monkeypatch, tmp_path):
    _patch_staff_client(
        monkeypatch,
        response_factory=lambda r: httpx.Response(200, json={"unexpected": True}),
    )
    cfg_dir = tmp_path / "hfox"
    result = runner.invoke(
        cli,
        ["auth", "login", "--subdomain", "acme", "--region", "us",
         "--api-key", "K", "--auth-code", "C"],
        env={"HFOX_CONFIG_DIR": str(cfg_dir)},
    )
    assert type(result.exception) is HfoxError
    assert str(result.exception) == (
        "Unexpected response from staff/; credentials were not saved."
    )
    assert result.exception.to_dict()["exit_code"] == 5
    assert not (cfg_dir / "token.json").exists()


def test_login_non_numeric_staff_id_warns_and_skips_default(monkeypatch, tmp_path):
    _patch_staff_client(
        monkeypatch,
        response_factory=lambda r: httpx.Response(
            200, json=[{"id": "not-a-number", "email": "a@x.org"}]
        ),
    )
    cfg_dir = tmp_path / "hfox"
    result = runner.invoke(
        cli,
        ["auth", "login", "--subdomain", "acme", "--region", "us",
         "--api-key", "K", "--auth-code", "C", "--email", "a@x.org"],
        env={"HFOX_CONFIG_DIR": str(cfg_dir)},
    )
    assert result.exit_code == 0, result.stderr
    assert "Ignoring unexpected staff id 'not-a-number'" in result.stderr
    settings = (cfg_dir / "config.toml").read_text()
    assert "default_staff_id" not in settings


def test_login_email_no_match_warns(monkeypatch, tmp_path):
    _patch_staff_client(
        monkeypatch,
        response_factory=lambda r: httpx.Response(
            200, json=[{"id": 1, "email": "someone@x.org"}]
        ),
    )
    cfg_dir = tmp_path / "hfox"
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

    monkeypatch.setattr(context_mod, "HappyFoxClient", factory)
    cfg_dir = tmp_path / "hfox"
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
    assert captured["base_url"] == "https://gw.internal/api/1.1/json"
    saved = json.loads((cfg_dir / "token.json").read_text())
    assert saved["base_url"] == "https://gw.internal"
    assert "Default staff id set to 3" in result.stderr
    assert json.loads(result.stdout)["base_url"] == "https://gw.internal/api/1.1/json"


# =========================================================================
# auth.py: status without credentials
# =========================================================================
def test_status_unauthenticated_masks_none(monkeypatch, tmp_path):
    cfg_dir = tmp_path / "hfox"
    cfg_dir.mkdir(parents=True)
    captured = _capture_render(monkeypatch)
    result = runner.invoke(
        cli, ["auth", "status"], env={"HFOX_CONFIG_DIR": str(cfg_dir)}
    )
    assert result.exit_code == 2
    assert len(captured) == 1
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


def test_config_dir_empty_env_falls_to_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HFOX_CONFIG_DIR", "")
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    from pathlib import Path

    assert config_dir() == Path.home() / ".config" / "hfox"


# =========================================================================
# config.py: base_url override and require_auth
# =========================================================================
def test_base_url_override_strips_trailing_slash_and_appends_suffix():
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
    cfg.require_auth()


# =========================================================================
# config.py: env > file > default precedence and base_url resolution
# =========================================================================
def test_load_config_env_base_url_overrides_disk(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "hfox"
    save_credentials(
        cfg_dir, subdomain="disk", region="eu", api_key="dk", auth_code="dc",
        base_url="https://disk.example",
    )
    monkeypatch.setenv("HFOX_CONFIG_DIR", str(cfg_dir))
    monkeypatch.setenv("HFOX_BASE_URL", "https://env.example")
    cfg = load_config()
    assert cfg.base_url_override == "https://env.example"
    assert cfg.region == "eu"  # from disk
    assert cfg.base_url == "https://env.example/api/1.1/json"


def test_load_config_defaults_when_nothing_set(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "hfox"  # does not exist
    monkeypatch.setenv("HFOX_CONFIG_DIR", str(cfg_dir))
    for k in ("HFOX_SUBDOMAIN", "HFOX_REGION", "HFOX_API_KEY",
              "HFOX_AUTH_CODE", "HFOX_FORMAT", "HFOX_STAFF_ID", "HFOX_BASE_URL"):
        monkeypatch.delenv(k, raising=False)
    cfg = load_config()
    assert cfg.region == "us"
    assert cfg.default_format == "json"
    assert cfg.subdomain is None
    assert cfg.api_key is None
    assert cfg.default_staff_id is None
    assert cfg.is_authenticated is False


def test_load_config_settings_fallback_when_token_missing(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "hfox"
    save_settings(cfg_dir, {"subdomain": "fromsettings", "region": "eu"})
    monkeypatch.setenv("HFOX_CONFIG_DIR", str(cfg_dir))
    for k in ("HFOX_SUBDOMAIN", "HFOX_REGION", "HFOX_API_KEY", "HFOX_AUTH_CODE"):
        monkeypatch.delenv(k, raising=False)
    cfg = load_config()
    assert cfg.subdomain == "fromsettings"
    assert cfg.region == "eu"
    assert cfg.api_key is None


# =========================================================================
# config.py: corrupt files raise HfoxError
# =========================================================================
def test_corrupt_toml_raises_hfox_error(tmp_path):
    cfg_dir = tmp_path / "hfox"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.toml").write_text("not = = valid toml")
    with pytest.raises(HfoxError) as exc:
        load_config(cfg_dir)
    assert "Cannot read config file" in str(exc.value)


def test_corrupt_json_raises_hfox_error(tmp_path):
    cfg_dir = tmp_path / "hfox"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "token.json").write_text("{not valid json")
    with pytest.raises(HfoxError) as exc:
        load_config(cfg_dir)
    assert "Cannot read token file" in str(exc.value)


# =========================================================================
# config.py: atomic write modes and dir permissions (0600/0700)
# =========================================================================
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_save_credentials_modes_0600_and_dir_0700(tmp_path):
    cfg_dir = tmp_path / "hfox"
    path = save_credentials(cfg_dir, subdomain="a", region="us", api_key="k", auth_code="c")
    assert (path.stat().st_mode & 0o777) == 0o600
    assert (cfg_dir.stat().st_mode & 0o777) == stat.S_IRWXU  # 0700


def test_save_credentials_includes_base_url(tmp_path):
    cfg_dir = tmp_path / "hfox"
    path = save_credentials(
        cfg_dir, subdomain="a", region="us", api_key="k", auth_code="c",
        base_url="https://gw.internal",
    )
    assert json.loads(path.read_text())["base_url"] == "https://gw.internal"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_save_settings_mode_0644(tmp_path):
    cfg_dir = tmp_path / "hfox"
    path = save_settings(cfg_dir, {"subdomain": "a"})
    assert (path.stat().st_mode & 0o777) == 0o644


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_save_settings_chmod_failure_warns(tmp_path, monkeypatch, capsys):
    warnings: list[str] = []
    cfg_dir = tmp_path / "hfox"
    import hfox.core.config as config_mod

    orig_chmod = config_mod.Path.chmod

    def fake_chmod(self, mode):
        # Fail only the 0700 directory chmod.
        if mode == stat.S_IRWXU:
            raise OSError("denied")
        return orig_chmod(self, mode)

    monkeypatch.setattr(config_mod.Path, "chmod", fake_chmod)
    save_settings(cfg_dir, {"subdomain": "a"}, warn=warnings.append)
    assert len(warnings) == 1
    assert "could not restrict permissions" in warnings[0]
    assert capsys.readouterr().err == ""


# =========================================================================
# config.py: save_settings coercions and toml str
# =========================================================================
def test_save_settings_non_int_staff_id_raises(tmp_path):
    cfg_dir = tmp_path / "hfox"
    with pytest.raises(ValidationError) as exc:
        save_settings(cfg_dir, {"default_staff_id": "abc"})
    assert "must be a non-negative integer" in str(exc.value)


def test_save_settings_bool_value_serialized_lowercase(tmp_path):
    cfg_dir = tmp_path / "hfox"
    path = save_settings(cfg_dir, {"some_flag": True, "other_flag": False})
    text = path.read_text()
    assert "some_flag = true" in text
    assert "other_flag = false" in text


def test_save_settings_string_with_quotes_escaped(tmp_path):
    cfg_dir = tmp_path / "hfox"
    path = save_settings(cfg_dir, {"subdomain": 'a"b\\c'})
    cfg = load_config(cfg_dir)
    assert cfg.subdomain == 'a"b\\c'
    assert '\\"' in path.read_text()


def test_save_settings_control_char_rejected(tmp_path):
    cfg_dir = tmp_path / "hfox"
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
            # An HTTP-date Retry-After is not numeric.
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
    assert exc.value.detail == "x" * 500
