"""Edges of core/config.py: host limits, source precedence, unreadable files and failed writes."""

import json
import os
import tomllib
from pathlib import Path

import pytest

from hfox.core.config import (
    Config,
    ConfigError,
    clear_credentials,
    load_config,
    save_credentials,
    save_settings,
    validate_host,
)
from hfox.core.errors import AuthError, ValidationError

# Three 63-character labels and one of 61: the 253-character limit exactly.
LONGEST_HOST = ".".join(["a" * 63] * 3 + ["a" * 61])


def write_token(directory: Path, **values) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "token.json").write_text(json.dumps(values), encoding="utf-8")


def write_toml(directory: Path, text: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def save_token(cfg_dir: Path) -> Path:
    return save_credentials(
        cfg_dir, subdomain="acme", region="us", api_key="k", auth_code="c"
    )


# --- validate_host --------------------------------------------------------------------


@pytest.mark.parametrize(
    "host",
    ["a" * 63, f"acme.{'b' * 63}", LONGEST_HOST, "a-b.c-d"],
    ids=["label-63", "later-label-63", "host-253", "hyphens"],
)
def test_validate_host_accepts_names_at_the_dns_limits(host):
    assert validate_host(host) == host


@pytest.mark.parametrize(
    "host",
    [
        "a" * 64,
        f"acme.{'b' * 64}",
        LONGEST_HOST + "a",
        "-acme",
        "acme.-x",
        "acme..com",
        "acme.",
    ],
    ids=[
        "label-64",
        "later-label-64",
        "host-254",
        "leading-hyphen",
        "later-leading-hyphen",
        "empty-label",
        "trailing-dot",
    ],
)
def test_validate_host_rejects_invalid_dns_names(host):
    with pytest.raises(ValidationError):
        validate_host(host)


# --- base URL -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"subdomain": " acme "}, "https://acme.happyfox.com/api/1.1/json"),
        ({"subdomain": " support.acme.com\n"}, "https://support.acme.com/api/1.1/json"),
        ({"subdomain": "acme", "region": " EU "}, "https://acme.happyfox.net/api/1.1/json"),
        ({"base_url_override": "  https://gw.internal/  "}, "https://gw.internal/api/1.1/json"),
    ],
    ids=["subdomain", "custom-host", "region", "override"],
)
def test_base_url_ignores_padding_and_region_case(fields, expected):
    assert Config(**fields).base_url == expected


# --- is_authenticated -----------------------------------------------------------------


@pytest.mark.parametrize(
    "fields",
    [
        {"subdomain": "acme", "api_key": "k"},
        {"subdomain": "acme", "auth_code": "c"},
        {"api_key": "k", "auth_code": "c"},
    ],
    ids=["no-auth-code", "no-api-key", "no-target"],
)
def test_not_authenticated_without_both_secrets_and_a_target(fields):
    cfg = Config(**fields)
    assert cfg.is_authenticated is False
    with pytest.raises(AuthError, match="hfox auth login"):
        cfg.require_auth()


def test_base_url_override_stands_in_for_the_subdomain():
    cfg = Config(api_key="k", auth_code="c", base_url_override="https://gw.internal")
    assert cfg.is_authenticated is True
    cfg.require_auth()


# --- load_config precedence -----------------------------------------------------------


def test_empty_env_values_fall_back_to_the_files(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "hfox"
    save_credentials(
        cfg_dir,
        subdomain="disk",
        region="eu",
        api_key="disk-key",
        auth_code="disk-code",
        base_url="https://disk.example",
    )
    save_settings(cfg_dir, {"default_format": "yaml"})
    for name in (
        "HFOX_SUBDOMAIN",
        "HFOX_REGION",
        "HFOX_API_KEY",
        "HFOX_AUTH_CODE",
        "HFOX_BASE_URL",
        "HFOX_FORMAT",
    ):
        monkeypatch.setenv(name, "")
    cfg = load_config(cfg_dir)
    assert (cfg.subdomain, cfg.region) == ("disk", "eu")
    assert (cfg.api_key, cfg.auth_code) == ("disk-key", "disk-code")
    assert cfg.base_url_override == "https://disk.example"
    assert cfg.default_format == "yaml"


def test_token_json_beats_config_toml(tmp_path):
    write_token(tmp_path, subdomain="tok", region="eu", base_url="https://tok.example")
    write_toml(
        tmp_path, 'subdomain = "cfg"\nregion = "us"\nbase_url = "https://cfg.example"\n'
    )
    cfg = load_config(tmp_path)
    assert cfg.subdomain == "tok"
    assert cfg.region == "eu"
    assert cfg.base_url_override == "https://tok.example"


def test_credentials_in_config_toml_are_ignored(tmp_path):
    write_toml(
        tmp_path,
        'subdomain = "acme"\napi_key = "toml-key-12345"\nauth_code = "toml-code-12345"\n',
    )
    cfg = load_config(tmp_path)
    assert cfg.api_key is None
    assert cfg.auth_code is None
    assert cfg.is_authenticated is False


@pytest.mark.parametrize("stored", ["9", '"x"'], ids=["valid", "malformed"])
def test_env_staff_id_beats_config_toml(tmp_path, monkeypatch, stored):
    write_toml(tmp_path, f"default_staff_id = {stored}\n")
    monkeypatch.setenv("HFOX_STAFF_ID", "12")
    cfg = load_config(tmp_path)
    assert cfg.default_staff_id == 12
    assert cfg.staff_id_error is None


def test_malformed_env_staff_id_does_not_fall_back_to_config_toml(tmp_path, monkeypatch):
    write_toml(tmp_path, "default_staff_id = 9\n")
    monkeypatch.setenv("HFOX_STAFF_ID", "1.5")
    cfg = load_config(tmp_path)
    assert cfg.default_staff_id is None
    assert isinstance(cfg.staff_id_error, ValidationError)
    assert "HFOX_STAFF_ID" in str(cfg.staff_id_error)


@pytest.mark.parametrize(
    ("token", "toml", "filename"),
    [
        # An empty HFOX_BASE_URL is unset, so the token value is the one in use.
        ({"base_url": "gw.internal:8443"}, "", "token.json"),
        # An empty token value is unset, so the config.toml value is the one in use.
        ({"base_url": ""}, 'base_url = "gw.internal:8443"\n', "config.toml"),
    ],
    ids=["token.json", "config.toml"],
)
def test_base_url_error_skips_empty_sources_when_naming_the_file(
    tmp_path, monkeypatch, token, toml, filename
):
    write_token(tmp_path, **token)
    write_toml(tmp_path, toml)
    monkeypatch.setenv("HFOX_BASE_URL", "")
    with pytest.raises(ValidationError) as exc:
        _ = load_config(tmp_path).base_url
    assert f"base_url in {tmp_path / filename}" in str(exc.value)


# --- ConfigError ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("filename", "kind", "hint"),
    [
        ("config.toml", "config", "Fix or delete the file."),
        ("token.json", "token", "Delete the file and run `hfox auth login`."),
    ],
    ids=["config.toml", "token.json"],
)
def test_unreadable_file_raises_config_error(tmp_path, filename, kind, hint):
    # A directory in the file's place exists and cannot be opened for reading.
    (tmp_path / filename).mkdir()
    with pytest.raises(ConfigError) as exc:
        load_config(tmp_path)
    err = exc.value
    assert str(err).startswith(f"Cannot read {kind} file {tmp_path / filename}: ")
    assert err.hint == hint
    assert (err.type, err.exit_code) == ("config", 5)


# --- writes ---------------------------------------------------------------------------


def test_save_creates_missing_parent_directories(tmp_path):
    cfg_dir = tmp_path / "a" / "b" / "hfox"
    save_token(cfg_dir)
    save_settings(cfg_dir, {"subdomain": "acme"})
    assert sorted(p.name for p in cfg_dir.iterdir()) == ["config.toml", "token.json"]


def test_temp_file_is_created_beside_its_target(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "hfox"
    moves: list[tuple[Path, Path]] = []
    real_replace = os.replace

    def recording_replace(src, dst):
        moves.append((Path(src).parent, Path(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", recording_replace)
    save_token(cfg_dir)
    save_settings(cfg_dir, {"subdomain": "acme"})
    assert {parent for parent, _ in moves} == {cfg_dir}
    assert {target for _, target in moves} == {cfg_dir / "token.json", cfg_dir / "config.toml"}


def test_failed_write_raises_the_original_error_when_the_temp_file_is_gone(
    tmp_path, monkeypatch
):
    cfg_dir = tmp_path / "hfox"

    def vanishing_replace(src, dst):
        os.unlink(src)
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", vanishing_replace)
    with pytest.raises(OSError, match="disk full"):
        save_token(cfg_dir)
    assert list(cfg_dir.iterdir()) == []


def test_no_temp_file_left_after_an_interrupt(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "hfox"

    def interrupted_replace(src, dst):
        raise KeyboardInterrupt

    monkeypatch.setattr(os, "replace", interrupted_replace)
    with pytest.raises(KeyboardInterrupt):
        save_token(cfg_dir)
    assert list(cfg_dir.iterdir()) == []


def test_save_settings_rejects_0x1f_and_accepts_a_space(tmp_path):
    with pytest.raises(ValidationError, match="control characters"):
        save_settings(tmp_path, {"note": "a\x1fb"})
    assert not (tmp_path / "config.toml").exists()
    path = save_settings(tmp_path, {"note": "a b"})
    assert tomllib.loads(path.read_text(encoding="utf-8")) == {"note": "a b"}


def test_save_settings_bad_staff_id_names_the_key_and_writes_nothing(tmp_path):
    path = save_settings(tmp_path, {"subdomain": "acme", "default_staff_id": 9})
    before = path.read_text(encoding="utf-8")
    with pytest.raises(ValidationError, match="default_staff_id"):
        save_settings(tmp_path, {"region": "eu", "default_staff_id": "abc"})
    assert path.read_text(encoding="utf-8") == before


def test_save_settings_table_only_file_starts_with_its_header(tmp_path):
    path = write_toml(tmp_path, '[display]\ncolumns = ["id", "subject"]\n')
    save_settings(tmp_path, {})
    text = path.read_text(encoding="utf-8")
    assert text.startswith("[display]\n")
    assert tomllib.loads(text) == {"display": {"columns": ["id", "subject"]}}


def test_clear_credentials_keeps_config_toml(tmp_path):
    cfg_dir = tmp_path / "hfox"
    save_token(cfg_dir)
    settings = save_settings(cfg_dir, {"subdomain": "acme", "default_staff_id": 9})
    before = settings.read_text(encoding="utf-8")
    assert clear_credentials(cfg_dir) is True
    assert [p.name for p in cfg_dir.iterdir()] == ["config.toml"]
    assert settings.read_text(encoding="utf-8") == before
