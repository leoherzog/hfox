import datetime
import json
import os
import stat
import sys
import tomllib
from pathlib import Path

import pytest

import hfox.core.config as config_mod
from hfox.core.config import (
    Config,
    ConfigError,
    cleartext_host,
    config_dir,
    guarded_dirs,
    guarded_secrets,
    load_config,
    normalize_base_url,
    save_credentials,
    save_settings,
    stored_account,
    validate_host,
)
from hfox.core.errors import AuthError, ExitCode, HfoxError, ValidationError

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission modes")


@pytest.fixture
def home(monkeypatch, tmp_path):
    """Point HOME at a tmp directory and clear the variables that pick the config directory."""
    path = tmp_path / "home"
    path.mkdir()
    monkeypatch.setenv("HOME", str(path))
    monkeypatch.setenv("USERPROFILE", str(path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("HFOX_CONFIG_DIR", raising=False)
    return path


def write_token(directory: Path, **values) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "token.json").write_text(json.dumps(values), encoding="utf-8")


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
    cfg_dir = tmp_path / "hfox"
    save_credentials(
        cfg_dir, subdomain="disk", region="us", api_key="diskkey", auth_code="diskcode"
    )
    monkeypatch.setenv("HFOX_CONFIG_DIR", str(cfg_dir))
    monkeypatch.setenv("HFOX_API_KEY", "envkey")
    cfg = load_config()
    assert cfg.api_key == "envkey"
    assert cfg.auth_code == "diskcode"  # falls back to disk
    assert cfg.subdomain == "disk"


@posix_only
def test_credentials_roundtrip_and_permissions(tmp_path):
    cfg_dir = tmp_path / "hfox"
    path = save_credentials(
        cfg_dir, subdomain="acme", region="eu", api_key="k", auth_code="c"
    )
    assert json.loads(path.read_text())["region"] == "eu"
    assert (path.stat().st_mode & 0o777) == 0o600
    assert (cfg_dir.stat().st_mode & 0o777) == 0o700


def test_settings_default_staff_id(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "hfox"
    save_settings(cfg_dir, {"subdomain": "acme", "default_staff_id": 9})
    monkeypatch.setenv("HFOX_CONFIG_DIR", str(cfg_dir))
    assert load_config().default_staff_id == 9


# --- config_dir -----------------------------------------------------------------------


def test_config_dir_default_is_dot_config(home):
    assert config_dir() == home / ".config" / "hfox"


def test_config_dir_xdg_absolute(home, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert config_dir() == tmp_path / "xdg" / "hfox"


def test_config_dir_xdg_relative_ignored(home, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative/xdg")
    assert config_dir() == home / ".config" / "hfox"


def test_config_dir_xdg_empty_ignored(home, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", "")
    assert config_dir() == home / ".config" / "hfox"


def test_config_dir_env_beats_xdg(home, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("HFOX_CONFIG_DIR", str(tmp_path / "env"))
    assert config_dir() == tmp_path / "env"


def test_config_dir_override_beats_env(home, monkeypatch, tmp_path):
    monkeypatch.setenv("HFOX_CONFIG_DIR", str(tmp_path / "env"))
    assert config_dir(tmp_path / "flag") == tmp_path / "flag"
    assert config_dir("") == tmp_path / "env"


def test_config_dir_never_uses_legacy_home_dir(home):
    (home / ".hfox").mkdir()
    write_token(home / ".hfox", api_key="legacy-key-000", auth_code="legacy-code-000")
    assert config_dir() == home / ".config" / "hfox"
    assert load_config().api_key is None


# --- guarded_dirs and guarded_secrets -------------------------------------------------


def test_guarded_dirs_three_distinct_entries(home, monkeypatch, tmp_path):
    monkeypatch.setenv("HFOX_CONFIG_DIR", str(tmp_path / "env"))
    assert guarded_dirs(tmp_path / "flag") == (
        tmp_path / "flag",
        tmp_path / "env",
        home / ".config" / "hfox",
    )


def test_guarded_dirs_default_follows_xdg(home, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("HFOX_CONFIG_DIR", str(tmp_path / "env"))
    assert guarded_dirs(tmp_path / "flag")[2] == tmp_path / "xdg" / "hfox"


def test_guarded_dirs_deduplicated(home, monkeypatch, tmp_path):
    default = home / ".config" / "hfox"
    assert guarded_dirs(default) == (default,)
    monkeypatch.setenv("HFOX_CONFIG_DIR", str(tmp_path / "env"))
    assert guarded_dirs(tmp_path / "env") == (tmp_path / "env", default)
    assert guarded_dirs(default) == (default, tmp_path / "env")


@pytest.fixture
def no_home(monkeypatch):
    """Make the home directory undeterminable, as with HOME unset and no passwd entry."""

    def fail(cls):
        raise RuntimeError("Could not determine home directory.")

    def fail_expanduser(self):
        if str(self).startswith("~"):
            raise RuntimeError("Could not determine home directory.")
        return self

    monkeypatch.setattr(config_mod.Path, "home", classmethod(fail))
    monkeypatch.setattr(config_mod.Path, "expanduser", fail_expanduser)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("HFOX_CONFIG_DIR", raising=False)


def test_guarded_dirs_without_home_drops_default(no_home, monkeypatch, tmp_path):
    monkeypatch.setenv("HFOX_CONFIG_DIR", str(tmp_path / "env"))
    assert guarded_dirs(tmp_path / "flag") == (tmp_path / "flag", tmp_path / "env")
    assert guarded_dirs(tmp_path / "env") == (tmp_path / "env",)


def test_guarded_dirs_without_home_or_env_keeps_active(no_home, tmp_path):
    assert guarded_dirs(tmp_path / "flag") == (tmp_path / "flag",)


def test_guarded_dirs_without_home_keeps_xdg_default(no_home, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert guarded_dirs(tmp_path / "flag") == (tmp_path / "flag", tmp_path / "xdg" / "hfox")


def test_guarded_secrets_without_home(no_home, monkeypatch, tmp_path):
    monkeypatch.setenv("HFOX_CONFIG_DIR", str(tmp_path / "env"))
    write_token(tmp_path / "env", api_key="env-dir-key-123", auth_code="env-dir-code-456")
    assert guarded_secrets(Config(dir=tmp_path / "env")) == (
        "env-dir-key-123",
        "env-dir-code-456",
    )


def test_config_dir_without_home_raises_config_error(no_home):
    with pytest.raises(ConfigError) as exc:
        config_dir()
    assert exc.value.type == "config"
    assert exc.value.exit_code == 5
    assert str(exc.value) == (
        "Cannot determine the home directory; set HFOX_CONFIG_DIR to an absolute path."
    )
    with pytest.raises(ConfigError):
        load_config()


def test_config_dir_without_home_rejects_tilde_values(no_home, monkeypatch):
    with pytest.raises(ConfigError) as exc:
        config_dir("~/flag")
    assert str(exc.value) == (
        "Cannot expand the config directory '~/flag': its home directory is unknown; "
        "use an absolute path."
    )
    monkeypatch.setenv("HFOX_CONFIG_DIR", "~/env")
    with pytest.raises(ConfigError, match="config directory '~/env'"):
        config_dir()


def test_config_dir_rejects_an_unknown_user_tilde(home):
    with pytest.raises(ConfigError, match="config directory '~hfox-no-such-user/x'"):
        config_dir("~hfox-no-such-user/x")


def test_config_dir_without_home_uses_explicit_values(no_home, monkeypatch, tmp_path):
    assert config_dir(tmp_path / "flag") == tmp_path / "flag"
    monkeypatch.setenv("HFOX_CONFIG_DIR", str(tmp_path / "env"))
    assert config_dir() == tmp_path / "env"
    assert load_config().dir == tmp_path / "env"


def test_guarded_secrets_active_values(home, tmp_path):
    cfg = Config(api_key="active-key-123", auth_code="active-code-456", dir=tmp_path / "flag")
    assert guarded_secrets(cfg) == ("active-key-123", "active-code-456")


def test_guarded_secrets_reads_other_guarded_dirs(home, monkeypatch, tmp_path):
    monkeypatch.setenv("HFOX_CONFIG_DIR", str(tmp_path / "env"))
    write_token(tmp_path / "env", api_key="env-dir-key-123", auth_code="env-dir-code-456")
    write_token(home / ".config" / "hfox", api_key="home-key-12345", auth_code="active-code-456")
    cfg = Config(api_key="active-key-123", auth_code="active-code-456", dir=tmp_path / "flag")
    assert guarded_secrets(cfg) == (
        "active-key-123",
        "active-code-456",
        "env-dir-key-123",
        "env-dir-code-456",
        "home-key-12345",
    )


def test_guarded_secrets_drops_short_and_non_string(home, tmp_path):
    write_token(tmp_path / "flag", api_key="short", auth_code=12345678)
    cfg = Config(api_key="1234567", auth_code="12345678", dir=tmp_path / "flag")
    assert guarded_secrets(cfg) == ("12345678",)


@pytest.mark.parametrize("content", ["{not json", "[1, 2]", '"just-a-string-value"'])
def test_guarded_secrets_skips_malformed_file(home, tmp_path, content):
    (tmp_path / "flag").mkdir()
    (tmp_path / "flag" / "token.json").write_text(content, encoding="utf-8")
    cfg = Config(api_key="active-key-123", auth_code=None, dir=tmp_path / "flag")
    assert guarded_secrets(cfg) == ("active-key-123",)


def test_guarded_secrets_skips_undecodable_file(home, tmp_path):
    (tmp_path / "flag").mkdir()
    (tmp_path / "flag" / "token.json").write_bytes(b"\xff\xfe\x00")
    assert guarded_secrets(Config(dir=tmp_path / "flag")) == ()


# --- ConfigError ----------------------------------------------------------------------


def test_malformed_config_toml_raises_config_error(tmp_path):
    cfg_dir = tmp_path / "hfox"
    cfg_dir.mkdir()
    (cfg_dir / "config.toml").write_text("not = = valid toml", encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        load_config(cfg_dir)
    err = exc.value
    assert isinstance(err, HfoxError)
    assert err.type == "config"
    assert err.exit_code == ExitCode.OTHER == 5
    assert str(err).startswith(f"Cannot read config file {cfg_dir / 'config.toml'}: ")
    assert err.hint == "Fix or delete the file."
    assert err.to_dict()["hint"] == "Fix or delete the file."


@pytest.mark.parametrize("content", ["{not valid json", "[1, 2]"])
def test_malformed_token_json_raises_config_error(tmp_path, content):
    cfg_dir = tmp_path / "hfox"
    cfg_dir.mkdir()
    (cfg_dir / "token.json").write_text(content, encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        load_config(cfg_dir)
    err = exc.value
    assert err.type == "config"
    assert err.exit_code == 5
    assert str(err).startswith(f"Cannot read token file {cfg_dir / 'token.json'}: ")
    assert err.hint == "Delete the file and run `hfox auth login`."
    assert err.to_dict()["hint"] == "Delete the file and run `hfox auth login`."


def test_undecodable_config_files_raise_config_error(tmp_path):
    cfg_dir = tmp_path / "hfox"
    cfg_dir.mkdir()
    (cfg_dir / "token.json").write_bytes(b"\xff\xfe")
    with pytest.raises(ConfigError, match="Cannot read token file") as exc:
        load_config(cfg_dir)
    assert exc.value.hint == "Delete the file and run `hfox auth login`."
    (cfg_dir / "token.json").unlink()
    (cfg_dir / "config.toml").write_bytes(b"a = \"\xff\"")
    with pytest.raises(ConfigError, match="Cannot read config file") as exc:
        load_config(cfg_dir)
    assert exc.value.hint == "Fix or delete the file."


@pytest.mark.parametrize(
    ("key", "literal"),
    [("subdomain", "5"), ("region", "true"), ("base_url", '["https://gw.example.com"]')],
)
def test_non_string_config_toml_value_raises_config_error(tmp_path, key, literal):
    cfg_dir = tmp_path / "hfox"
    cfg_dir.mkdir()
    (cfg_dir / "config.toml").write_text(f"{key} = {literal}\n", encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        load_config(cfg_dir)
    err = exc.value
    assert err.type == "config"
    assert err.exit_code == 5
    assert str(err) == f"Cannot read config file {cfg_dir / 'config.toml'}: {key} must be a string"
    assert err.hint == "Fix or delete the file."


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("subdomain", 5),
        ("region", False),
        ("base_url", ["https://gw.example.com"]),
        ("api_key", 1234567890123),
        ("auth_code", {"value": "nested-secret-123"}),
    ],
)
def test_non_string_token_json_value_raises_config_error(tmp_path, key, value):
    cfg_dir = tmp_path / "hfox"
    write_token(cfg_dir, **{key: value})
    with pytest.raises(ConfigError) as exc:
        load_config(cfg_dir)
    err = exc.value
    assert err.type == "config"
    assert err.exit_code == 5
    assert str(err) == f"Cannot read token file {cfg_dir / 'token.json'}: {key} must be a string"
    assert err.hint == "Delete the file and run `hfox auth login`."


def test_non_string_file_value_fails_even_when_env_overrides_it(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "hfox"
    write_token(cfg_dir, subdomain=5)
    monkeypatch.setenv("HFOX_SUBDOMAIN", "acme")
    with pytest.raises(ConfigError, match="subdomain must be a string"):
        load_config(cfg_dir)


def test_null_and_empty_file_values_count_as_unset(tmp_path):
    cfg_dir = tmp_path / "hfox"
    write_token(cfg_dir, subdomain=None, base_url="", api_key=None, auth_code="code-123")
    cfg = load_config(cfg_dir)
    assert cfg.subdomain is None
    assert cfg.base_url_override is None
    assert cfg.auth_code == "code-123"


# --- base URL -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://alice:hunter2pw@gw.example.com",
        "https://alice@gw.example.com",
        "http://alice:hunter2pw@gw.example.com:bad",
        "ftp://alice:hunter2pw@gw.example.com",
        "https://alice:hunter2pw@[bad",
        "alice:hunter2pw@gw.example.com",
        "gw.example.com/alice:hunter2pw@x",
        "https:/alice:hunter2pw@gw.example.com",
        "https//alice:hunter2pw@gw.example.com",
        "https://gw.example.com/?k=alice:hunter2pw@x",
    ],
)
def test_normalize_base_url_rejects_credentials_without_echo(url):
    with pytest.raises(ValidationError) as exc:
        normalize_base_url(url, "HFOX_BASE_URL")
    message = str(exc.value)
    assert message == (
        "Invalid HFOX_BASE_URL: credentials in the URL are not supported; expected http(s)://host."
    )
    assert "alice" not in message
    assert "hunter2pw" not in message


@pytest.mark.parametrize("value", ["alice:hunter2pw@gw.example.com", "hunter2pw@acme"])
def test_validate_host_rejects_credentials_without_echo(value):
    with pytest.raises(ValidationError) as exc:
        validate_host(value)
    message = str(exc.value)
    assert message.startswith("Invalid subdomain/host: expected a bare hostname")
    assert "hunter2pw" not in message


def test_validate_host_echoes_a_value_without_credentials():
    with pytest.raises(ValidationError, match=r"Invalid subdomain/host 'ac me': expected"):
        validate_host("ac me")
    assert validate_host(" support.acme.com ") == "support.acme.com"


def test_config_base_url_rejects_credentials():
    cfg = Config(base_url_override="https://alice:hunter2pw@gw.example.com")
    with pytest.raises(ValidationError) as exc:
        _ = cfg.base_url
    assert "hunter2pw" not in str(exc.value)


@pytest.mark.parametrize(
    "url",
    [
        "https://gw.example.com/?k=hunter2pw",
        "https://gw.example.com/#hunter2pw",
        "gw.example.com?k=hunter2pw",
        "https://gw.example.com:bad/?k=hunter2pw",
        "https://[bad?k=hunter2pw",
    ],
)
def test_normalize_base_url_rejects_query_and_fragment_without_echo(url):
    with pytest.raises(ValidationError) as exc:
        normalize_base_url(url, "HFOX_BASE_URL")
    assert str(exc.value) == (
        "Invalid HFOX_BASE_URL: a query or fragment is not supported; expected http(s)://host."
    )


def test_normalize_base_url_echoes_a_value_without_secret_parts():
    with pytest.raises(ValidationError) as exc:
        normalize_base_url("ftp://gw.example.com", "HFOX_BASE_URL")
    assert str(exc.value) == (
        "Invalid HFOX_BASE_URL 'ftp://gw.example.com': expected http(s)://host."
    )


def test_normalize_base_url_accepts_plain_roots():
    assert normalize_base_url("https://gw.example.com/api/1.1/json/") == "https://gw.example.com"
    assert normalize_base_url("http://localhost:8080") == "http://localhost:8080"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://gw.example.com", "gw.example.com"),
        ("http://GW.Example.COM:8080/api/1.1/json", "gw.example.com"),
        ("HTTP://10.0.0.5", "10.0.0.5"),
        ("http://[2001:db8::1]:8080", "2001:db8::1"),
        ("https://gw.example.com", None),
        ("http://localhost", None),
        ("http://LOCALHOST:8080", None),
        ("http://127.0.0.1", None),
        ("http://127.8.9.10:9000", None),
        ("http://[::1]:8080", None),
        ("http://x.localhost", None),
        ("http://notlocalhost", "notlocalhost"),
        ("not a url", None),
        ("http://", None),
        ("http://[bad", None),
        ("", None),
    ],
)
def test_cleartext_host(url, expected):
    assert cleartext_host(url) == expected


# --- atomic writes --------------------------------------------------------------------


@pytest.fixture
def temp_modes(monkeypatch):
    """Record the mode each temp sibling has when os.chmod is first called on it."""
    seen: dict[str, int] = {}
    real_chmod = os.chmod

    def recording_chmod(target, mode, **kwargs):
        name = Path(target).name
        if name.startswith("."):
            seen[name.split(".")[1]] = stat.S_IMODE(os.stat(target).st_mode)
        return real_chmod(target, mode, **kwargs)

    monkeypatch.setattr(os, "chmod", recording_chmod)
    old_umask = os.umask(0)
    yield seen
    os.umask(old_umask)


@posix_only
def test_temp_file_is_0600_from_creation(tmp_path, temp_modes):
    cfg_dir = tmp_path / "hfox"
    token = save_credentials(
        cfg_dir, subdomain="acme", region="us", api_key="k", auth_code="c"
    )
    settings = save_settings(cfg_dir, {"subdomain": "acme"})
    assert temp_modes == {"token": 0o600, "config": 0o600}
    assert stat.S_IMODE(token.stat().st_mode) == 0o600
    assert stat.S_IMODE(settings.stat().st_mode) == 0o644
    assert stat.S_IMODE(cfg_dir.stat().st_mode) == 0o700


def test_no_temp_file_left_after_success(tmp_path):
    cfg_dir = tmp_path / "hfox"
    save_credentials(cfg_dir, subdomain="acme", region="us", api_key="k", auth_code="c")
    save_settings(cfg_dir, {"subdomain": "acme"})
    assert sorted(p.name for p in cfg_dir.iterdir()) == ["config.toml", "token.json"]


def test_no_temp_file_left_after_failed_replace(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "hfox"

    def failing_replace(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", failing_replace)
    with pytest.raises(ConfigError, match="disk full"):
        save_credentials(cfg_dir, subdomain="acme", region="us", api_key="k", auth_code="c")
    assert list(cfg_dir.iterdir()) == []


def test_no_temp_file_left_after_unencodable_text(tmp_path):
    cfg_dir = tmp_path / "hfox"
    with pytest.raises(UnicodeEncodeError):
        save_settings(cfg_dir, {"subdomain": "\ud800"})
    assert list(cfg_dir.iterdir()) == []


def test_failed_write_keeps_existing_file(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "hfox"
    path = save_settings(cfg_dir, {"subdomain": "acme"})
    before = path.read_text(encoding="utf-8")
    monkeypatch.setattr(os, "replace", lambda src, dst: (_ for _ in ()).throw(OSError("no")))
    with pytest.raises(ConfigError):
        save_settings(cfg_dir, {"subdomain": "other"})
    assert path.read_text(encoding="utf-8") == before
    assert [p.name for p in cfg_dir.iterdir()] == ["config.toml"]


def test_dir_chmod_failure_goes_to_warn_callback(tmp_path, monkeypatch, capsys):
    cfg_dir = tmp_path / "hfox"
    real_chmod = Path.chmod

    def fake_chmod(self, mode, **kwargs):
        if self == cfg_dir:
            raise OSError("denied")
        return real_chmod(self, mode, **kwargs)

    monkeypatch.setattr(config_mod.Path, "chmod", fake_chmod)
    messages: list[str] = []
    save_settings(cfg_dir, {"subdomain": "acme"}, warn=messages.append)
    save_credentials(
        cfg_dir, subdomain="acme", region="us", api_key="k", auth_code="c", warn=messages.append
    )
    expected = (
        f"could not restrict permissions on {cfg_dir} to 0700 (denied); "
        "credentials may be readable by other users."
    )
    assert messages == [expected, expected]
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_dir_chmod_failure_without_callback_is_silent(tmp_path, monkeypatch, capsys):
    cfg_dir = tmp_path / "hfox"
    real_chmod = Path.chmod

    def fake_chmod(self, mode, **kwargs):
        if self == cfg_dir:
            raise OSError("denied")
        return real_chmod(self, mode, **kwargs)

    monkeypatch.setattr(config_mod.Path, "chmod", fake_chmod)
    path = save_settings(cfg_dir, {"subdomain": "acme"})
    assert path.exists()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


# --- config.toml writer ---------------------------------------------------------------

EXISTING_TOML = """\
subdomain = "acme"
note = "a\\tb"
released = 2026-01-02
tags = ["x", "y"]

[profiles.work]
subdomain = "work"
default_staff_id = 4

[profiles.work.limits]
ratio = 0.5

[display]
columns = ["id", "subject"]
"""


def test_save_settings_round_trips_existing_structures(tmp_path):
    cfg_dir = tmp_path / "hfox"
    cfg_dir.mkdir()
    (cfg_dir / "config.toml").write_text(EXISTING_TOML, encoding="utf-8")
    expected = tomllib.loads(EXISTING_TOML)
    assert expected["note"] == "a\tb"
    assert expected["released"] == datetime.date(2026, 1, 2)

    path = save_settings(cfg_dir, {"region": "eu", "default_staff_id": 9})

    expected.update(region="eu", default_staff_id=9)
    text = path.read_text(encoding="utf-8")
    assert tomllib.loads(text) == expected
    assert "[profiles.work]" in text
    assert "[profiles.work.limits]" in text
    assert 'note = "a\\tb"' in text


def test_save_settings_removes_named_keys(tmp_path):
    cfg_dir = tmp_path / "hfox"
    save_settings(cfg_dir, {"subdomain": "acme", "default_staff_id": 9})
    path = save_settings(cfg_dir, {"region": "eu"}, remove=("default_staff_id", "absent"))
    assert tomllib.loads(path.read_text(encoding="utf-8")) == {"subdomain": "acme", "region": "eu"}


def test_save_settings_remove_wins_over_an_incoming_value(tmp_path):
    path = save_settings(tmp_path, {"default_staff_id": 9}, remove=("default_staff_id",))
    assert path.read_text(encoding="utf-8") == "\n"


def test_stored_account_ignores_the_environment(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "hfox"
    write_token(cfg_dir, subdomain="acme", base_url="https://gw.internal")
    save_settings(cfg_dir, {"subdomain": "old", "region": "eu", "default_staff_id": 9})
    for name in ("HFOX_SUBDOMAIN", "HFOX_REGION", "HFOX_BASE_URL", "HFOX_STAFF_ID"):
        monkeypatch.setenv(name, "5")
    assert stored_account(cfg_dir) == {
        "subdomain": "acme",
        "region": "eu",
        "base_url": "https://gw.internal",
        "default_staff_id": 9,
    }


def test_stored_account_without_files(tmp_path):
    assert stored_account(tmp_path) == {
        "subdomain": None,
        "region": None,
        "base_url": None,
        "default_staff_id": None,
    }


def test_stored_account_skips_blank_and_non_string_values(tmp_path):
    (tmp_path / "token.json").write_text('{"subdomain": " ", "region": 5}', encoding="utf-8")
    (tmp_path / "config.toml").write_text(
        'subdomain = "acme"\ndefault_staff_id = "x"\n', encoding="utf-8"
    )
    account = stored_account(tmp_path)
    assert account["subdomain"] == "acme"
    assert account["region"] is None
    assert account["default_staff_id"] == "x"


def test_save_settings_is_stable_across_rewrites(tmp_path):
    cfg_dir = tmp_path / "hfox"
    cfg_dir.mkdir()
    (cfg_dir / "config.toml").write_text(EXISTING_TOML, encoding="utf-8")
    first = save_settings(cfg_dir, {"region": "eu"}).read_text(encoding="utf-8")
    second = save_settings(cfg_dir, {"region": "eu"}).read_text(encoding="utf-8")
    assert first == second


def test_toml_dumps_value_types_round_trip():
    data = {
        "text": 'quote " backslash \\ bell \x07 del \x7f nl \n cr \r ff \f bs \b',
        "yes": True,
        "no": False,
        "count": -3,
        "ratio": 1.5,
        "big": 1e30,
        "pos_inf": float("inf"),
        "neg_inf": float("-inf"),
        "when": datetime.datetime(2026, 1, 2, 3, 4, 5, tzinfo=datetime.UTC),
        "local": datetime.datetime(2026, 1, 2, 3, 4, 5, 250000),
        "day": datetime.date(2026, 1, 2),
        "clock": datetime.time(7, 32),
        "empty": [],
        "nested": [[1, 2], ["a"]],
        "rows": [{"name": "a", "inner": {"k": 1}}, {"name": "b"}],
        "key with space": 1,
        "dotted.key": 2,
        "": 3,
        "table": {"x": 1, "odd key": {"y": "z"}, "blank": {}},
    }
    text = config_mod._toml_dumps(data)
    assert tomllib.loads(text) == data
    assert '"key with space" = 1' in text
    assert '[table."odd key"]' in text
    assert "\\u0007" in text
    assert "\\u007F" in text


def test_toml_dumps_nan_and_scalars_before_tables():
    text = config_mod._toml_dumps({"table": {"x": 1}, "value": float("nan"), "last": "z"})
    assert text == 'value = nan\nlast = "z"\n\n[table]\nx = 1\n'
    loaded = tomllib.loads(text)
    assert loaded["value"] != loaded["value"]


def test_toml_dumps_rejects_unsupported_value():
    with pytest.raises(ValidationError, match="Cannot write a NoneType value"):
        config_mod._toml_dumps({"a": [None]})


@pytest.mark.parametrize("value", ["a\nb", "a\tb", "a\x00b", "a\x7fb"])
def test_save_settings_rejects_incoming_control_characters(tmp_path, value):
    cfg_dir = tmp_path / "hfox"
    with pytest.raises(ValidationError, match="control characters"):
        save_settings(cfg_dir, {"subdomain": value})
    assert not (cfg_dir / "config.toml").exists()


def test_save_settings_incoming_scalars(tmp_path):
    cfg_dir = tmp_path / "hfox"
    path = save_settings(
        cfg_dir, {"subdomain": 'a"b\\c', "flag": True, "skipped": None, "default_staff_id": "7"}
    )
    assert tomllib.loads(path.read_text(encoding="utf-8")) == {
        "subdomain": 'a"b\\c',
        "flag": True,
        "default_staff_id": 7,
    }
