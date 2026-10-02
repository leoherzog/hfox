"""Edges of core/config.py: host limits, source precedence, unreadable files and failed writes."""

import json
import os
import sys
import tomllib
from pathlib import Path

import pytest

import hfox.cli.main as main_mod
from hfox.core.config import (
    Config,
    ConfigError,
    clear_credentials,
    load_config,
    save_credentials,
    save_settings,
    stored_account,
    validate_host,
)
from hfox.core.errors import AuthError, ValidationError

# Three 63-character labels and one of 61: the 253-character limit exactly.
LONGEST_HOST = ".".join(["a" * 63] * 3 + ["a" * 61])

# Mode bits bind neither Windows nor root.
needs_modes = pytest.mark.skipif(
    sys.platform == "win32" or os.geteuid() == 0,
    reason="POSIX permission modes, enforced for a non-root user",
)

per_file = pytest.mark.parametrize(
    ("filename", "kind", "hint"),
    [
        ("config.toml", "config", "Fix or delete the file."),
        ("token.json", "token", "Delete the file and run `hfox auth login`."),
    ],
    ids=["config.toml", "token.json"],
)

WRITE_HINT = "Check the permissions on the config directory, or choose another with --config-dir."


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


def run_main(monkeypatch, capsys, argv):
    """Run `main.app()` in process; return (exit code, stdout, stderr)."""
    monkeypatch.setattr(sys, "argv", ["hfox", *argv])
    code = 0
    try:
        main_mod.app()
    except SystemExit as exc:
        code = exc.code or 0
    captured = capsys.readouterr()
    return code, captured.out, captured.err


per_saver = pytest.mark.parametrize(
    ("filename", "kind", "save"),
    [
        ("config.toml", "config", lambda cfg_dir: save_settings(cfg_dir, {"subdomain": "acme"})),
        ("token.json", "token", save_token),
    ],
    ids=["config.toml", "token.json"],
)


@pytest.fixture
def restrict():
    """Return restrict(path, mode), which chmods a directory; cleanup sets it back to 0700."""
    changed: list[Path] = []

    def apply(path: Path, mode: int) -> Path:
        path.chmod(mode)
        changed.append(path)
        return path

    yield apply
    for path in changed:
        path.chmod(0o700)


@pytest.fixture
def locked_dir(tmp_path, restrict):
    """A mode-000 directory holding a token.json."""
    path = tmp_path / "locked"
    write_token(path, subdomain="acme", api_key="k" * 12, auth_code="c" * 12)
    return restrict(path, 0)


@pytest.fixture(params=["missing", "below-a-file"])
def absent_dir(request, tmp_path):
    """A config directory that does not exist, or whose parent is a regular file."""
    if request.param == "missing":
        return tmp_path / "absent" / "hfox"
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    return blocker / "hfox"


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


@pytest.mark.parametrize("blank", ["", "  ", "\t"], ids=["empty", "spaces", "tab"])
def test_blank_env_values_fall_back_to_the_files(tmp_path, monkeypatch, blank):
    cfg_dir = tmp_path / "hfox"
    save_credentials(
        cfg_dir,
        subdomain="disk",
        region="eu",
        api_key="disk-key",
        auth_code="disk-code",
        base_url="https://disk.example",
    )
    save_settings(cfg_dir, {"default_format": "yaml", "default_staff_id": 7})
    for name in (
        "HFOX_SUBDOMAIN",
        "HFOX_REGION",
        "HFOX_API_KEY",
        "HFOX_AUTH_CODE",
        "HFOX_BASE_URL",
        "HFOX_FORMAT",
        "HFOX_STAFF_ID",
    ):
        monkeypatch.setenv(name, blank)
    cfg = load_config(cfg_dir)
    assert (cfg.subdomain, cfg.region) == ("disk", "eu")
    assert (cfg.api_key, cfg.auth_code) == ("disk-key", "disk-code")
    assert cfg.base_url_override == "https://disk.example"
    assert cfg.base_url_source == f"base_url in {cfg_dir / 'token.json'}"
    assert cfg.default_format == "yaml"
    assert (cfg.default_staff_id, cfg.staff_id_error) == (7, None)


def test_whitespace_only_token_values_fall_back_to_config_toml(tmp_path):
    write_token(
        tmp_path, subdomain="  ", region=" ", base_url="\t", api_key="  ", auth_code="\n"
    )
    write_toml(
        tmp_path, 'subdomain = "cfg"\nregion = "eu"\nbase_url = "https://cfg.example"\n'
    )
    cfg = load_config(tmp_path)
    assert (cfg.subdomain, cfg.region) == ("cfg", "eu")
    assert cfg.base_url_override == "https://cfg.example"
    assert cfg.base_url_source == f"base_url in {tmp_path / 'config.toml'}"
    assert (cfg.api_key, cfg.auth_code) == (None, None)
    assert cfg.is_authenticated is False


def test_whitespace_only_values_in_both_files_resolve_to_the_defaults(tmp_path):
    write_token(tmp_path, subdomain=" ", region=" ", base_url=" ")
    write_toml(
        tmp_path,
        'subdomain = " "\nregion = " "\nbase_url = " "\ndefault_format = " "\n'
        'default_staff_id = " "\n',
    )
    cfg = load_config(tmp_path)
    assert (cfg.subdomain, cfg.region, cfg.base_url_override) == (None, "us", None)
    assert cfg.default_format == "json"
    assert (cfg.default_staff_id, cfg.staff_id_error) == (None, None)


PADDED = {
    "subdomain": " acme ",
    "region": "\teu ",
    "base_url": "  https://gw.internal/Hf\n",
    "api_key": " key-12345 ",
    "auth_code": "\tcode-12345\n",
}


def assert_stripped(cfg):
    assert (cfg.subdomain, cfg.region) == ("acme", "eu")
    assert cfg.base_url_override == "https://gw.internal/Hf"
    assert (cfg.api_key, cfg.auth_code) == ("key-12345", "code-12345")


def test_a_padded_env_value_is_set_and_stripped(tmp_path, monkeypatch):
    write_token(tmp_path, subdomain="tok", api_key="tok-key-12345")
    for key, value in PADDED.items():
        monkeypatch.setenv(f"HFOX_{key.upper()}", value)
    monkeypatch.setenv("HFOX_FORMAT", " yaml ")
    cfg = load_config(tmp_path)
    assert_stripped(cfg)
    assert cfg.default_format == "yaml"


def test_a_padded_token_value_is_set_and_stripped(tmp_path):
    write_token(tmp_path, **PADDED)
    write_toml(tmp_path, 'subdomain = "cfg"\nregion = "us"\n')
    assert_stripped(load_config(tmp_path))


def test_a_padded_config_toml_value_is_set_and_stripped(tmp_path):
    write_token(tmp_path, api_key=PADDED["api_key"], auth_code=PADDED["auth_code"])
    write_toml(
        tmp_path,
        'subdomain = " acme "\nregion = "\\teu "\nbase_url = "  https://gw.internal/Hf\\n"\n'
        'default_format = " yaml "\n',
    )
    cfg = load_config(tmp_path)
    assert_stripped(cfg)
    assert cfg.default_format == "yaml"


@pytest.mark.parametrize("filename", ["token.json", "config.toml"])
def test_stored_account_strips_as_load_config_does(tmp_path, filename):
    values = {key: PADDED[key] for key in ("subdomain", "region", "base_url")}
    if filename == "token.json":
        write_token(tmp_path, **values)
    else:
        write_toml(tmp_path, "".join(f"{k} = {json.dumps(v)}\n" for k, v in values.items()))
    cfg = load_config(tmp_path)
    assert stored_account(tmp_path) == {
        "subdomain": cfg.subdomain,
        "region": cfg.region,
        "base_url": cfg.base_url_override,
        "default_staff_id": None,
    }
    assert stored_account(tmp_path)["subdomain"] == "acme"


# --- host case ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("written", "host", "url"),
    [
        ("ACME", "acme", "https://acme.happyfox.com/api/1.1/json"),
        (" Support.Acme.COM ", "support.acme.com", "https://support.acme.com/api/1.1/json"),
    ],
    ids=["subdomain", "custom-host"],
)
@pytest.mark.parametrize("source", ["env", "token.json", "config.toml"])
def test_load_config_lowercases_the_host(tmp_path, monkeypatch, source, written, host, url):
    if source == "env":
        monkeypatch.setenv("HFOX_SUBDOMAIN", written)
    elif source == "token.json":
        write_token(tmp_path, subdomain=written)
    else:
        write_toml(tmp_path, f'subdomain = "{written}"\n')
    cfg = load_config(tmp_path)
    assert cfg.subdomain == host
    assert cfg.base_url == url
    if source != "env":
        assert stored_account(tmp_path)["subdomain"] == host


@pytest.mark.parametrize("source", ["env", "token.json", "config.toml"])
def test_base_url_override_lowercases_the_host_and_keeps_the_path_case(
    tmp_path, monkeypatch, source
):
    written = "HTTPS://GW.Internal:8443/Tenant/HF/"
    if source == "env":
        monkeypatch.setenv("HFOX_BASE_URL", written)
    elif source == "token.json":
        write_token(tmp_path, base_url=written)
    else:
        write_toml(tmp_path, f'base_url = "{written}"\n')
    assert load_config(tmp_path).base_url == "https://gw.internal:8443/Tenant/HF/api/1.1/json"


@pytest.mark.parametrize("literal", ["false", "0", "[]"])
def test_non_string_default_format_stays_set(tmp_path, literal):
    write_toml(tmp_path, f"default_format = {literal}\n")
    assert load_config(tmp_path).default_format == tomllib.loads(f"v = {literal}")["v"]


@pytest.mark.parametrize(
    ("key", "value"),
    [("subdomain", "cfg"), ("region", "eu"), ("base_url", "https://cfg.example")],
    ids=["subdomain", "region", "base_url"],
)
@pytest.mark.parametrize("blank", [None, "", "  "], ids=["null", "empty", "spaces"])
def test_load_config_and_stored_account_agree_on_an_unset_token_value(
    tmp_path, key, value, blank
):
    write_token(tmp_path, **{key: blank})
    write_toml(tmp_path, f'{key} = "{value}"\n')
    cfg = load_config(tmp_path)
    loaded = {"subdomain": cfg.subdomain, "region": cfg.region, "base_url": cfg.base_url_override}
    assert loaded[key] == stored_account(tmp_path)[key] == value


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


def test_whitespace_only_env_staff_id_leaves_a_malformed_config_toml_value_named(
    tmp_path, monkeypatch
):
    path = write_toml(tmp_path, 'default_staff_id = "x"\n')
    monkeypatch.setenv("HFOX_STAFF_ID", "  ")
    cfg = load_config(tmp_path)
    assert cfg.default_staff_id is None
    assert f"default_staff_id in {path}" in str(cfg.staff_id_error)
    assert "HFOX_STAFF_ID" not in str(cfg.staff_id_error)


@pytest.mark.parametrize("blank", ["", "  "], ids=["empty", "spaces"])
@pytest.mark.parametrize(
    ("token", "toml", "filename"),
    [
        # A blank HFOX_BASE_URL is unset, so the token value is the one in use.
        ({"base_url": "gw.internal:8443"}, "", "token.json"),
        # A blank token value is unset, so the config.toml value is the one in use.
        ({"base_url": "{blank}"}, 'base_url = "gw.internal:8443"\n', "config.toml"),
    ],
    ids=["token.json", "config.toml"],
)
def test_base_url_error_skips_blank_sources_when_naming_the_file(
    tmp_path, monkeypatch, token, toml, filename, blank
):
    write_token(tmp_path, **{key: value.format(blank=blank) for key, value in token.items()})
    write_toml(tmp_path, toml)
    monkeypatch.setenv("HFOX_BASE_URL", blank)
    with pytest.raises(ValidationError) as exc:
        _ = load_config(tmp_path).base_url
    assert f"base_url in {tmp_path / filename}" in str(exc.value)


# --- blank HFOX_FORMAT through main.app() ---------------------------------------------


def test_whitespace_only_env_format_falls_back_to_json(monkeypatch, capsys):
    monkeypatch.setenv("HFOX_FORMAT", "  ")
    code, out, _ = run_main(monkeypatch, capsys, ["--dry-run", "auth", "logout"])
    assert code == 0
    assert json.loads(out)["dry_run"] is True


def test_whitespace_only_env_format_leaves_the_config_file_as_the_named_source(
    tmp_path, monkeypatch, capsys
):
    path = write_toml(tmp_path, 'default_format = "xml"\n')
    monkeypatch.setenv("HFOX_FORMAT", "  ")
    code, out, _ = run_main(
        monkeypatch, capsys, ["--config-dir", str(tmp_path), "--dry-run", "auth", "logout"]
    )
    payload = json.loads(out)
    assert (code, payload["type"]) == (3, "validation")
    assert payload["error"] == (
        f"Unknown output format 'xml' from default_format in {path}; "
        "expected json, table, csv or yaml."
    )


def test_padded_env_format_is_named_without_its_padding(monkeypatch, capsys):
    monkeypatch.setenv("HFOX_FORMAT", " xml ")
    code, out, _ = run_main(monkeypatch, capsys, ["--dry-run", "auth", "logout"])
    payload = json.loads(out)
    assert (code, payload["type"]) == (3, "validation")
    assert payload["error"] == (
        "Unknown output format 'xml' from HFOX_FORMAT; expected json, table, csv or yaml."
    )


# --- blank config directory through main.app() ----------------------------------------


@pytest.mark.parametrize("blank", ["  ", "\t"], ids=["spaces", "tab"])
def test_whitespace_only_env_config_dir_falls_back_to_the_default(
    tmp_path, monkeypatch, capsys, blank
):
    monkeypatch.setenv("HFOX_CONFIG_DIR", blank)
    code, out, _ = run_main(monkeypatch, capsys, ["--dry-run", "auth", "logout"])
    assert code == 0
    assert json.loads(out)["config_dir"] == str(tmp_path / "home" / ".config" / "hfox")


@pytest.mark.parametrize("form", ["separate", "equals"])
def test_whitespace_only_config_dir_flag_falls_back_to_the_env_value(
    tmp_path, monkeypatch, capsys, form
):
    flag = ["--config-dir", "  "] if form == "separate" else ["--config-dir=  "]
    code, out, _ = run_main(monkeypatch, capsys, [*flag, "--dry-run", "auth", "logout"])
    assert code == 0
    assert json.loads(out)["config_dir"] == str(tmp_path / "cfg")


# --- control character in the base URL through main.app() -----------------------------


@pytest.mark.parametrize("argv", [["system", "staff"], ["auth", "status"]], ids=["read", "status"])
def test_base_url_with_a_control_character_exits_3_and_sends_nothing(
    mock_api, monkeypatch, capsys, argv
):
    captured = mock_api(lambda request: pytest.fail("no request expected"))
    for key, value in (("SUBDOMAIN", "acme"), ("API_KEY", "k"), ("AUTH_CODE", "c")):
        monkeypatch.setenv(f"HFOX_{key}", value)
    monkeypatch.setenv("HFOX_BASE_URL", "https://gw.exa\tmple.com")
    code, out, _ = run_main(monkeypatch, capsys, argv)
    payload = json.loads(out)
    assert (code, payload["type"]) == (3, "validation")
    assert payload["error"] == (
        "Invalid HFOX_BASE_URL 'https://gw.exa\\tmple.com': expected http(s)://host."
    )
    assert captured == []


# --- an unusable base URL host through main.app() -------------------------------------


@pytest.mark.parametrize(
    "argv",
    [["system", "staff"], ["--dry-run", "system", "staff"], ["auth", "status"], ["auth", "login"]],
    ids=["read", "dry-run", "status", "login"],
)
@pytest.mark.parametrize("source", ["env", "token.json", "config.toml"])
@pytest.mark.parametrize(
    "url",
    ["https://gw exa mple.com", "https://gw<x>.example.com", "https://xn--a.example.com"],
    ids=["space", "brackets", "punycode"],
)
def test_base_url_with_an_unusable_host_exits_3_and_sends_nothing(
    mock_api, tmp_path, monkeypatch, capsys, argv, source, url
):
    captured = mock_api(lambda request: pytest.fail("no request expected"))
    for key, value in (("SUBDOMAIN", "acme"), ("API_KEY", "k"), ("AUTH_CODE", "c")):
        monkeypatch.setenv(f"HFOX_{key}", value)
    cfg_dir = tmp_path / "cfg"
    if source == "env":
        monkeypatch.setenv("HFOX_BASE_URL", url)
        name = "HFOX_BASE_URL"
    elif source == "token.json":
        write_token(cfg_dir, base_url=url)
        name = f"base_url in {cfg_dir / 'token.json'}"
    else:
        write_toml(cfg_dir, f'base_url = "{url}"\n')
        name = f"base_url in {cfg_dir / 'config.toml'}"
    code, out, _ = run_main(monkeypatch, capsys, argv)
    payload = json.loads(out)
    assert (code, payload["type"]) == (3, "validation")
    assert payload["error"] == f"Invalid {name} {url!r}: expected http(s)://host."
    assert captured == []
    # A rejected login writes nothing.
    assert not (cfg_dir / "token.json").exists() or source == "token.json"


# --- ConfigError ----------------------------------------------------------------------


@per_file
def test_unreadable_file_raises_config_error(tmp_path, filename, kind, hint):
    # A directory in the file's place exists and cannot be opened for reading.
    (tmp_path / filename).mkdir()
    with pytest.raises(ConfigError) as exc:
        load_config(tmp_path)
    err = exc.value
    assert str(err).startswith(f"Cannot read {kind} file {tmp_path / filename}: ")
    assert err.hint == hint
    assert (err.type, err.exit_code) == ("config", 5)


@needs_modes
def test_unsearchable_directory_raises_config_error(locked_dir):
    with pytest.raises(ConfigError) as exc:
        load_config(locked_dir)
    err = exc.value
    # config.toml is the first file read.
    assert str(err).startswith(f"Cannot read config file {locked_dir / 'config.toml'}: ")
    assert err.hint == "Fix or delete the file."
    assert (err.type, err.exit_code) == ("config", 5)


@needs_modes
@per_file
def test_file_linked_into_an_unsearchable_directory_raises_config_error(
    tmp_path, locked_dir, filename, kind, hint
):
    cfg_dir = tmp_path / "hfox"
    cfg_dir.mkdir()
    (cfg_dir / filename).symlink_to(locked_dir / filename)
    with pytest.raises(ConfigError) as exc:
        load_config(cfg_dir)
    err = exc.value
    assert str(err).startswith(f"Cannot read {kind} file {cfg_dir / filename}: ")
    assert err.hint == hint
    assert (err.type, err.exit_code) == ("config", 5)


@needs_modes
def test_auth_status_reports_an_unsearchable_directory(locked_dir, monkeypatch, capsys):
    monkeypatch.setenv("HFOX_CONFIG_DIR", str(locked_dir))
    code, out, _ = run_main(monkeypatch, capsys, ["auth", "status"])
    body = json.loads(out)
    assert code == 5
    assert (body["type"], body["exit_code"]) == ("config", 5)
    assert str(locked_dir / "config.toml") in body["error"]
    assert body["hint"] == "Fix or delete the file."


def test_absent_directory_loads_an_empty_config(absent_dir):
    cfg = load_config(absent_dir)
    assert (cfg.subdomain, cfg.api_key, cfg.auth_code) == (None, None, None)
    assert cfg.is_authenticated is False
    assert cfg.dir == absent_dir


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
    with pytest.raises(ConfigError, match="disk full") as exc:
        save_token(cfg_dir)
    assert str(exc.value.__cause__) == "disk full"
    assert list(cfg_dir.iterdir()) == []


@needs_modes
@per_saver
def test_save_below_a_read_only_directory_raises_config_error(
    tmp_path, restrict, filename, kind, save
):
    parent = tmp_path / "parent"
    parent.mkdir()
    restrict(parent, 0o500)
    cfg_dir = parent / "hfox"
    with pytest.raises(ConfigError) as exc:
        save(cfg_dir)
    err = exc.value
    assert str(err).startswith(f"Cannot write {kind} file {cfg_dir / filename}: ")
    assert isinstance(err.__cause__, PermissionError)
    assert err.hint == WRITE_HINT
    assert (err.type, err.exit_code) == ("config", 5)
    assert list(parent.iterdir()) == []


@per_saver
def test_save_into_a_regular_file_raises_config_error(tmp_path, filename, kind, save):
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        save(blocker)
    assert str(exc.value).startswith(f"Cannot write {kind} file {blocker / filename}: ")
    assert exc.value.hint == WRITE_HINT
    assert blocker.read_text(encoding="utf-8") == "x"


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


@needs_modes
@pytest.mark.parametrize("mode", [0o000, 0o500], ids=["unsearchable", "read-only"])
def test_clear_credentials_without_write_access_raises_config_error(tmp_path, restrict, mode):
    cfg_dir = tmp_path / "hfox"
    write_token(cfg_dir, subdomain="acme")
    restrict(cfg_dir, mode)
    with pytest.raises(ConfigError) as exc:
        clear_credentials(cfg_dir)
    err = exc.value
    assert str(err).startswith(f"Cannot remove token file {cfg_dir / 'token.json'}: ")
    assert isinstance(err.__cause__, PermissionError)
    assert err.hint == WRITE_HINT
    assert (err.type, err.exit_code) == ("config", 5)
    cfg_dir.chmod(0o700)
    assert (cfg_dir / "token.json").exists()


def test_clear_credentials_in_an_absent_directory_removes_nothing(absent_dir):
    assert clear_credentials(absent_dir) is False


def test_clear_credentials_keeps_config_toml(tmp_path):
    cfg_dir = tmp_path / "hfox"
    save_token(cfg_dir)
    settings = save_settings(cfg_dir, {"subdomain": "acme", "default_staff_id": 9})
    before = settings.read_text(encoding="utf-8")
    assert clear_credentials(cfg_dir) is True
    assert [p.name for p in cfg_dir.iterdir()] == ["config.toml"]
    assert settings.read_text(encoding="utf-8") == before
