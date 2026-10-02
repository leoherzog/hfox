"""`hfox auth` edge cases: login values, the login target, stored files, --quiet, --config-dir."""

import base64
import json
import os
import stat
import sys

import httpx
import pytest
from typer.testing import CliRunner

import hfox.cli.context as context_mod
import hfox.cli.main as main_mod
import hfox.core.config as config_mod
from hfox.cli.main import cli
from hfox.core.config import save_credentials
from hfox.core.errors import AuthError, HfoxError, NetworkError, ValidationError

runner = CliRunner()

API_KEY = "KEY-SECRET-123"
AUTH_CODE = "CODE-SECRET-456"
SECRETS = ["--api-key", API_KEY, "--auth-code", AUTH_CODE]
LOGIN = ["auth", "login", "--subdomain", "acme", "--region", "us", *SECRETS]
STAFF = [{"id": 7, "email": "a@x.org"}]
WRITE_HINT = "Check the permissions on the config directory, or choose another with --config-dir."

# Mode bits bind neither Windows nor root.
needs_modes = pytest.mark.skipif(
    sys.platform == "win32" or os.geteuid() == 0,
    reason="POSIX permission modes, enforced for a non-root user",
)


@pytest.fixture
def cfg_dir(tmp_path, monkeypatch):
    path = tmp_path / "hfox"
    monkeypatch.setenv("HFOX_CONFIG_DIR", str(path))
    return path


@pytest.fixture
def read_only():
    """Return a function that sets a directory to mode 0500; cleanup sets it back to 0700."""
    changed = []

    def apply(path):
        path.chmod(0o500)
        changed.append(path)

    yield apply
    for path in changed:
        path.chmod(0o700)


def staff_ok(staff=STAFF):
    return lambda request: httpx.Response(200, json=staff)


def no_client(monkeypatch):
    """Fail the test if any client is built."""
    monkeypatch.setattr(
        context_mod, "HappyFoxClient", lambda *a, **k: pytest.fail("client built")
    )


def saved(cfg_dir, **kw):
    save_credentials(
        cfg_dir, subdomain="acme", region="us", api_key=API_KEY, auth_code=AUTH_CODE, **kw
    )


def write_config(cfg_dir, text):
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.toml").write_text(text, encoding="utf-8")


def read_config(cfg_dir):
    return (cfg_dir / "config.toml").read_text(encoding="utf-8")


def login_as(subdomain="acme", *extra):
    return ["auth", "login", "--subdomain", subdomain, *SECRETS, *extra]


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


def assert_nothing_saved(cfg_dir):
    assert not (cfg_dir / "token.json").exists()
    assert not (cfg_dir / "config.toml").exists()


# -- login values ----------------------------------------------------------------
def test_login_prompts_never_echo_the_secrets(mock_api, cfg_dir, tty):
    mock_api(staff_ok())
    result = runner.invoke(cli, ["auth", "login"], input=f"acme\n\n{API_KEY}\n{AUTH_CODE}\n")
    assert result.exit_code == 0, result.stderr
    token = json.loads((cfg_dir / "token.json").read_text(encoding="utf-8"))
    assert (token["api_key"], token["auth_code"]) == (API_KEY, AUTH_CODE)
    for secret in (API_KEY, AUTH_CODE):
        assert secret not in result.stderr
        assert secret not in result.stdout


@pytest.mark.parametrize(
    ("argv", "env", "flag", "variable"),
    [
        (["--subdomain", "  ", *SECRETS], {}, "--subdomain", "HFOX_SUBDOMAIN"),
        (SECRETS, {"HFOX_SUBDOMAIN": "  "}, "--subdomain", "HFOX_SUBDOMAIN"),
        (["--subdomain", "acme", "--api-key", API_KEY, "--auth-code", ""], {},
         "--auth-code", "HFOX_AUTH_CODE"),
        (["--subdomain", "acme", "--api-key", " \t", "--auth-code", AUTH_CODE], {},
         "--api-key", "HFOX_API_KEY"),
        (["--subdomain", "acme", "--auth-code", AUTH_CODE], {"HFOX_API_KEY": "  "},
         "--api-key", "HFOX_API_KEY"),
        (["--subdomain", "acme", "--api-key", API_KEY], {"HFOX_AUTH_CODE": "  "},
         "--auth-code", "HFOX_AUTH_CODE"),
    ],
    ids=[
        "blank --subdomain",
        "blank HFOX_SUBDOMAIN",
        "empty --auth-code",
        "blank --api-key",
        "blank HFOX_API_KEY",
        "blank HFOX_AUTH_CODE",
    ],
)
def test_login_blank_value_without_tty_counts_as_missing(
    monkeypatch, cfg_dir, argv, env, flag, variable
):
    no_client(monkeypatch)
    result = runner.invoke(cli, ["auth", "login", *argv], env=env)
    assert isinstance(result.exception, ValidationError)
    message = str(result.exception)
    assert flag in message
    assert variable in message
    assert_nothing_saved(cfg_dir)


def test_login_prompts_for_secrets_that_are_blank_in_the_environment(mock_api, cfg_dir, tty):
    requests = mock_api(staff_ok())
    result = runner.invoke(
        cli,
        ["auth", "login", "--subdomain", "acme", "--region", "us"],
        env={"HFOX_API_KEY": "  ", "HFOX_AUTH_CODE": "\t"},
        input=f"{API_KEY}\n{AUTH_CODE}\n",
    )
    assert result.exit_code == 0, result.stderr
    sent = base64.b64decode(requests[0].headers["authorization"].removeprefix("Basic "))
    assert sent.decode() == f"{API_KEY}:{AUTH_CODE}"
    token = json.loads((cfg_dir / "token.json").read_text(encoding="utf-8"))
    assert (token["api_key"], token["auth_code"]) == (API_KEY, AUTH_CODE)


@pytest.mark.parametrize(
    ("argv", "env"),
    [(["--region", "  "], {}), ([], {"HFOX_REGION": "  "})],
    ids=["blank --region", "blank HFOX_REGION"],
)
def test_login_blank_region_without_tty_is_us(mock_api, cfg_dir, argv, env):
    requests = mock_api(staff_ok())
    result = runner.invoke(cli, [*login_as("acme"), *argv], env=env)
    assert result.exit_code == 0, result.stderr
    assert requests[0].url.host == "acme.happyfox.com"
    assert json.loads(result.stdout)["region"] == "us"


def test_login_dry_run_without_tty_still_requires_the_values(monkeypatch, cfg_dir):
    no_client(monkeypatch)
    result = runner.invoke(cli, ["--dry-run", "auth", "login", "--subdomain", "acme"])
    assert isinstance(result.exception, ValidationError)
    for flag in ("--api-key", "--auth-code"):
        assert flag in str(result.exception)
    assert result.stdout == ""


@pytest.mark.parametrize(
    ("argv", "env", "typed", "named"),
    [
        (["--region", "us", *SECRETS], {"HFOX_SUBDOMAIN": "bad name"}, "", "subdomain"),
        (["--region", "us", *SECRETS], {}, "bad name\n", "subdomain"),
        (["--subdomain", "acme", *SECRETS], {}, "mars\n", "region"),
    ],
    ids=["subdomain from env", "prompted subdomain", "prompted region"],
)
def test_login_validates_env_and_prompted_values_under_a_base_url_override(
    monkeypatch, cfg_dir, tty, argv, env, typed, named
):
    # With an override the subdomain and region build no URL, so only login checks them.
    monkeypatch.setenv("HFOX_BASE_URL", "https://gw.internal")
    no_client(monkeypatch)
    result = runner.invoke(cli, ["auth", "login", *argv], env=env, input=typed)
    assert isinstance(result.exception, ValidationError)
    assert named in str(result.exception)
    assert_nothing_saved(cfg_dir)


@pytest.mark.parametrize(
    ("argv", "env", "typed"),
    [
        (["--subdomain", " acme "], {}, ""),
        ([], {"HFOX_SUBDOMAIN": " acme "}, ""),
        ([], {}, " acme \n"),
    ],
    ids=["--subdomain", "HFOX_SUBDOMAIN", "prompt"],
)
def test_login_saves_and_renders_the_subdomain_without_outer_spaces(
    mock_api, cfg_dir, tty, argv, env, typed
):
    requests = mock_api(staff_ok())
    result = runner.invoke(
        cli, ["auth", "login", *argv, "--region", "us", *SECRETS], env=env, input=typed
    )
    assert result.exit_code == 0, result.stderr
    assert requests[0].url.host == "acme.happyfox.com"
    assert json.loads(result.stdout)["subdomain"] == "acme"
    token = json.loads((cfg_dir / "token.json").read_text(encoding="utf-8"))
    assert token["subdomain"] == "acme"
    assert 'subdomain = "acme"' in read_config(cfg_dir)
    assert json.loads(runner.invoke(cli, ["auth", "status"]).stdout)["subdomain"] == "acme"


def test_login_under_a_stored_base_url_saves_the_stripped_subdomain(mock_api, cfg_dir):
    saved(cfg_dir, base_url="https://gw.internal")
    write_config(cfg_dir, 'subdomain = "acme"\nregion = "us"\ndefault_staff_id = 7\n')
    mock_api(staff_ok())
    result = runner.invoke(cli, login_as(" acme "))
    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert (payload["subdomain"], payload["default_staff_id"]) == ("acme", 7)
    assert "The stored base URL https://gw.internal overrides --subdomain" in result.stderr
    token = json.loads((cfg_dir / "token.json").read_text(encoding="utf-8"))
    assert (token["subdomain"], token["base_url"]) == ("acme", "https://gw.internal")


def sent_credentials(request):
    header = request.headers["authorization"].removeprefix("Basic ")
    return base64.b64decode(header).decode()


@pytest.mark.parametrize(
    ("argv", "env", "typed"),
    [
        (["--api-key", f" {API_KEY} ", "--auth-code", f"\t{AUTH_CODE}"], {}, ""),
        ([], {"HFOX_API_KEY": f" {API_KEY} ", "HFOX_AUTH_CODE": f"{AUTH_CODE}  "}, ""),
        ([], {}, f"  {API_KEY} \n {AUTH_CODE}\t\n"),
    ],
    ids=["flags", "environment", "prompts"],
)
def test_login_strips_the_secrets_before_the_probe_and_the_save(
    mock_api, cfg_dir, tty, argv, env, typed
):
    requests = mock_api(staff_ok())
    result = runner.invoke(
        cli, ["auth", "login", "--subdomain", "acme", "--region", "us", *argv],
        env=env, input=typed,
    )
    assert result.exit_code == 0, result.stderr
    assert sent_credentials(requests[0]) == f"{API_KEY}:{AUTH_CODE}"
    token = json.loads((cfg_dir / "token.json").read_text(encoding="utf-8"))
    assert (token["api_key"], token["auth_code"]) == (API_KEY, AUTH_CODE)


@pytest.mark.parametrize(
    ("argv", "typed", "message"),
    [
        (["--auth-code", AUTH_CODE], "   \n", "API key must not be blank."),
        (["--api-key", API_KEY], "\t \n", "Auth code must not be blank."),
    ],
    ids=["api key", "auth code"],
)
def test_login_rejects_a_whitespace_only_secret_typed_at_the_prompt(
    monkeypatch, cfg_dir, tty, argv, typed, message
):
    no_client(monkeypatch)
    result = runner.invoke(
        cli, ["auth", "login", "--subdomain", "acme", "--region", "us", *argv],
        input=f"{typed}{API_KEY}\n",
    )
    assert isinstance(result.exception, ValidationError)
    assert str(result.exception) == message
    assert result.stdout == ""
    assert_nothing_saved(cfg_dir)


def test_login_blank_secret_at_the_prompt_is_refused_under_dry_run(monkeypatch, cfg_dir, tty):
    no_client(monkeypatch)
    result = runner.invoke(
        cli,
        ["--dry-run", "auth", "login", "--subdomain", "acme", "--region", "us",
         "--auth-code", AUTH_CODE],
        input="  \n",
    )
    assert isinstance(result.exception, ValidationError)
    assert result.stdout == ""


# -- host case -------------------------------------------------------------------
@pytest.mark.parametrize(
    ("written", "host", "url"),
    [
        ("ACME", "acme", "https://acme.happyfox.com/api/1.1/json"),
        ("Support.Acme.COM", "support.acme.com", "https://support.acme.com/api/1.1/json"),
    ],
    ids=["subdomain", "custom-host"],
)
@pytest.mark.parametrize("source", ["flag", "environment", "prompt"])
def test_login_saves_and_renders_the_host_in_lowercase(
    mock_api, cfg_dir, tty, source, written, host, url
):
    argv = ["--subdomain", written] if source == "flag" else []
    env = {"HFOX_SUBDOMAIN": written} if source == "environment" else {}
    typed = f"{written}\n" if source == "prompt" else ""
    requests = mock_api(staff_ok())
    result = runner.invoke(
        cli, ["auth", "login", *argv, "--region", "us", *SECRETS], env=env, input=typed
    )
    assert result.exit_code == 0, result.stderr
    assert str(requests[0].url) == f"{url}/staff/"
    payload = json.loads(result.stdout)
    assert (payload["subdomain"], payload["base_url"]) == (host, url)
    assert f"Authenticated to {url} " in result.stderr
    token = json.loads((cfg_dir / "token.json").read_text(encoding="utf-8"))
    assert token["subdomain"] == host
    assert f'subdomain = "{host}"' in read_config(cfg_dir)


def test_login_lowercases_the_base_url_host_and_keeps_the_path_case(
    mock_api, cfg_dir, monkeypatch
):
    monkeypatch.setenv("HFOX_BASE_URL", "HTTPS://Proxy.Example/Tenant/HF/")
    requests = mock_api(staff_ok())
    result = runner.invoke(cli, LOGIN)
    assert result.exit_code == 0, result.stderr
    assert str(requests[0].url) == "https://proxy.example/Tenant/HF/api/1.1/json/staff/"
    assert json.loads(result.stdout)["base_url"] == "https://proxy.example/Tenant/HF/api/1.1/json"
    token = json.loads((cfg_dir / "token.json").read_text(encoding="utf-8"))
    assert token["base_url"] == "https://proxy.example/Tenant/HF"


@pytest.mark.parametrize(
    ("filename", "text"),
    [
        ("token.json", '{"subdomain": " ACME ", "api_key": " KEY-SECRET-123 "}'),
        ("config.toml", 'subdomain = " ACME "\n'),
    ],
    ids=["token.json", "config.toml"],
)
def test_status_renders_a_stored_host_stripped_and_lowercased(cfg_dir, filename, text):
    cfg_dir.mkdir(parents=True)
    (cfg_dir / filename).write_text(text, encoding="utf-8")
    payload = json.loads(runner.invoke(cli, ["auth", "status"]).stdout)
    assert payload["subdomain"] == "acme"
    assert payload["base_url"] == "https://acme.happyfox.com/api/1.1/json"
    if filename == "token.json":
        assert payload["api_key"] == "KE••••••23"


def test_status_renders_an_env_host_stripped_and_lowercased(cfg_dir):
    env = {"HFOX_SUBDOMAIN": " Support.Acme.COM ", "HFOX_API_KEY": f" {API_KEY} ",
           "HFOX_AUTH_CODE": f" {AUTH_CODE} "}
    result = runner.invoke(cli, ["auth", "status"], env=env)
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["subdomain"] == "support.acme.com"
    assert payload["base_url"] == "https://support.acme.com/api/1.1/json"
    assert payload["api_key"] == "KE••••••23"


def test_status_check_sends_the_stripped_credentials(mock_api, cfg_dir):
    cfg_dir.mkdir(parents=True)
    token = {"subdomain": "acme", "api_key": f" {API_KEY}\n", "auth_code": f" {AUTH_CODE} "}
    (cfg_dir / "token.json").write_text(json.dumps(token), encoding="utf-8")
    requests = mock_api(staff_ok())
    result = runner.invoke(cli, ["auth", "status", "--check"])
    assert result.exit_code == 0, result.stderr
    assert sent_credentials(requests[0]) == f"{API_KEY}:{AUTH_CODE}"


def test_login_short_flags_name_the_subdomain_and_email(mock_api, cfg_dir):
    mock_api(staff_ok())
    result = runner.invoke(cli, ["auth", "login", "-s", "acme", "-e", "a@x.org", *SECRETS])
    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert (payload["subdomain"], payload["default_staff_id"]) == ("acme", 7)


# -- login probe -----------------------------------------------------------------
def test_login_reports_the_host_and_agent_count_on_stderr(mock_api, cfg_dir):
    mock_api(staff_ok([{"id": 7, "email": "a@x.org"}, {"id": 8, "email": "b@x.org"}]))
    result = runner.invoke(cli, LOGIN)
    assert result.exit_code == 0, result.stderr
    assert "https://acme.happyfox.com/api/1.1/json" in result.stderr
    assert "2 agents" in result.stderr


@pytest.mark.parametrize(
    "response",
    [
        lambda r: httpx.Response(200, html="<html><title>Sign in</title></html>"),
        lambda r: httpx.Response(204),
    ],
    ids=["html", "no content"],
)
def test_login_rejects_a_2xx_body_that_is_not_json(mock_api, cfg_dir, response):
    mock_api(response)
    result = runner.invoke(cli, LOGIN)
    assert type(result.exception) is HfoxError
    assert_nothing_saved(cfg_dir)


@pytest.mark.parametrize(
    "error", [AuthError("denied"), NetworkError("down")], ids=["auth", "network"]
)
def test_login_closes_the_probe_client_when_the_probe_fails(monkeypatch, cfg_dir, error):
    closed = []

    class Probe:
        def __init__(self, *args, **kwargs):
            pass

        def get(self, path):
            raise error

        def close(self):
            closed.append(True)

    monkeypatch.setattr(context_mod, "HappyFoxClient", Probe)
    result = runner.invoke(cli, LOGIN)
    assert isinstance(result.exception, type(error))
    assert closed


# -- stored base URL -------------------------------------------------------------
@pytest.mark.parametrize(
    ("filename", "text"),
    [
        ("token.json", '{"base_url": "gw.internal:8443"}'),
        ("config.toml", 'base_url = "gw.internal:8443"\n'),
    ],
    ids=["token.json", "config.toml"],
)
def test_login_names_the_file_holding_an_unusable_base_url(monkeypatch, cfg_dir, filename, text):
    cfg_dir.mkdir(parents=True)
    (cfg_dir / filename).write_text(text, encoding="utf-8")
    no_client(monkeypatch)
    result = runner.invoke(cli, login_as("acme"))
    assert isinstance(result.exception, ValidationError)
    assert filename in str(result.exception)
    assert "HFOX_BASE_URL" not in str(result.exception)


def test_login_through_env_base_url_replaces_an_unusable_stored_one(
    mock_api, cfg_dir, monkeypatch
):
    write_config(
        cfg_dir, 'subdomain = "acme"\nbase_url = "gw.internal:8443"\ndefault_staff_id = 7\n'
    )
    monkeypatch.setenv("HFOX_BASE_URL", "https://gw.internal")
    requests = mock_api(staff_ok())
    result = runner.invoke(cli, login_as("acme"))
    assert result.exit_code == 0, result.stderr
    assert str(requests[0].url) == "https://gw.internal/api/1.1/json/staff/"
    token = json.loads((cfg_dir / "token.json").read_text(encoding="utf-8"))
    assert token.get("base_url") == "https://gw.internal"
    assert json.loads(result.stdout)["default_staff_id"] is None
    assert "default_staff_id" not in read_config(cfg_dir)


# -- the stored default ----------------------------------------------------------
@pytest.mark.parametrize(
    ("token", "config", "argv", "env"),
    [
        (None, 'subdomain = "acme"\n', login_as("acme"), {}),
        (None, 'subdomain = "acme"\nregion = "us"\nbase_url = "https://gw.internal/api/1.1/json/"\n',
         login_as("acme"), {}),
        ({"base_url": "https://gw.internal"}, 'subdomain = "acme"\nregion = "us"\n',
         login_as("acme"), {"HFOX_BASE_URL": "https://gw.internal/"}),
        ({}, 'subdomain = "acme"\nregion = "us"\n', login_as(" acme "), {}),
        ({"base_url": "https://gw.internal/Hf"}, 'subdomain = "acme"\nregion = "us"\n',
         login_as("acme"), {"HFOX_BASE_URL": "HTTPS://GW.Internal/Hf"}),
    ],
    ids=["no stored region", "unnormalized stored base URL", "same base URL from env",
         "spaces around the subdomain", "base URL host in another case"],
)
def test_login_to_an_equivalent_target_keeps_the_default(
    mock_api, cfg_dir, token, config, argv, env
):
    if token is not None:
        saved(cfg_dir, **token)
    write_config(cfg_dir, f"{config}default_staff_id = 7\n")
    mock_api(staff_ok())
    result = runner.invoke(cli, argv, env=env)
    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout)["default_staff_id"] == 7
    assert "default_staff_id = 7" in read_config(cfg_dir)


@pytest.mark.parametrize(
    ("stored", "expected"),
    [('"7"', 7), ("1.5", None), ("-3", None)],
    ids=["digit string", "float", "negative"],
)
def test_login_renders_a_hand_written_default_as_status_does(mock_api, cfg_dir, stored, expected):
    write_config(cfg_dir, f'subdomain = "acme"\ndefault_staff_id = {stored}\n')
    mock_api(staff_ok())
    result = runner.invoke(cli, login_as("acme"))
    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout)["default_staff_id"] == expected
    status = json.loads(runner.invoke(cli, ["auth", "status"]).stdout)
    assert status["default_staff_id"] == expected
    assert ("kept" in result.stderr) is (expected is not None)


# -- config directory ------------------------------------------------------------
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
@pytest.mark.parametrize("failing_write", [1, 2], ids=["token.json", "config.toml"])
def test_login_warns_when_a_write_cannot_restrict_the_directory(
    mock_api, cfg_dir, monkeypatch, failing_write
):
    restricted = []
    real_chmod = config_mod.Path.chmod

    def chmod(self, mode, **kwargs):
        if mode == stat.S_IRWXU:
            restricted.append(self)
            if len(restricted) == failing_write:
                raise OSError("denied")
        return real_chmod(self, mode, **kwargs)

    monkeypatch.setattr(config_mod.Path, "chmod", chmod)
    mock_api(staff_ok())
    result = runner.invoke(cli, LOGIN)
    assert result.exit_code == 0, result.stderr
    warned = [line for line in result.stderr.splitlines() if line.startswith("warning: ")]
    assert len(warned) == 1
    assert str(cfg_dir) in warned[0]
    assert json.loads(result.stdout)["authenticated"] is True


@needs_modes
def test_logout_in_a_read_only_directory_is_a_config_error(
    cfg_dir, read_only, monkeypatch, capsys
):
    saved(cfg_dir)
    read_only(cfg_dir)
    code, out, _ = run_main(monkeypatch, capsys, ["auth", "logout"])
    body = json.loads(out)
    assert code == 5
    assert (body["type"], body["exit_code"]) == ("config", 5)
    assert body["error"].startswith(f"Cannot remove token file {cfg_dir / 'token.json'}: ")
    assert body["hint"] == WRITE_HINT
    assert (cfg_dir / "token.json").exists()


@needs_modes
def test_login_below_a_read_only_directory_is_a_config_error(
    mock_api, tmp_path, read_only, monkeypatch, capsys
):
    parent = tmp_path / "parent"
    parent.mkdir()
    read_only(parent)
    monkeypatch.setenv("HFOX_CONFIG_DIR", str(parent / "hfox"))
    mock_api(staff_ok())
    code, out, err = run_main(monkeypatch, capsys, LOGIN)
    body = json.loads(out)
    assert code == 5
    assert (body["type"], body["exit_code"]) == ("config", 5)
    assert body["error"].startswith(
        f"Cannot write token file {parent / 'hfox' / 'token.json'}: "
    )
    assert body["hint"] == WRITE_HINT
    assert list(parent.iterdir()) == []
    for secret in (API_KEY, AUTH_CODE):
        assert secret not in out + err


def test_config_dir_flag_beats_the_environment_for_every_auth_command(
    mock_api, cfg_dir, tmp_path
):
    other = tmp_path / "elsewhere"
    flag = ["--config-dir", str(other)]
    mock_api(staff_ok())

    login = runner.invoke(cli, [*flag, *LOGIN])
    assert login.exit_code == 0, login.stderr
    assert json.loads(login.stdout)["config_dir"] == str(other)
    assert (other / "token.json").exists()
    assert not cfg_dir.exists()

    status = runner.invoke(cli, [*flag, "auth", "status"])
    assert status.exit_code == 0
    payload = json.loads(status.stdout)
    assert (payload["config_dir"], payload["authenticated"]) == (str(other), True)

    logout = runner.invoke(cli, [*flag, "auth", "logout"])
    payload = json.loads(logout.stdout)
    assert (payload["removed"], payload["config_dir"]) == (True, str(other))
    assert not (other / "token.json").exists()


@pytest.mark.parametrize("blank", ["  ", "\t"], ids=["spaces", "tab"])
def test_status_reads_past_a_whitespace_only_config_dir(cfg_dir, tmp_path, blank):
    saved(cfg_dir)
    default = tmp_path / "home" / ".config" / "hfox"

    from_flag = runner.invoke(cli, ["--config-dir", blank, "auth", "status"])
    payload = json.loads(from_flag.stdout)
    assert (payload["config_dir"], payload["authenticated"]) == (str(cfg_dir), True)

    from_env = runner.invoke(cli, ["auth", "status"], env={"HFOX_CONFIG_DIR": blank})
    assert from_env.exit_code == 2
    assert json.loads(from_env.stdout)["config_dir"] == str(default)


# -- --quiet ---------------------------------------------------------------------
def test_quiet_login_still_warns_about_the_default(mock_api, cfg_dir):
    mock_api(staff_ok())
    assert runner.invoke(cli, login_as("acme", "--email", "a@x.org")).exit_code == 0
    result = runner.invoke(cli, ["--quiet", *login_as("acme", "--email", "ghost@x.org")])
    assert result.exit_code == 0, result.stderr
    lines = result.stderr.splitlines()
    # One warning for the unmatched email, one for the removed default.
    assert len(lines) == 2
    assert all(line.startswith("warning: ") for line in lines)
    assert "ghost@x.org" in result.stderr


def test_quiet_status_still_warns_when_not_authenticated(cfg_dir):
    result = runner.invoke(cli, ["--quiet", "auth", "status"])
    assert result.exit_code == 2
    assert result.stderr.startswith("warning: ")
    assert "hfox auth login" in result.stderr


def test_quiet_status_check_still_warns_on_failure(mock_api, cfg_dir):
    saved(cfg_dir)
    mock_api(lambda r: httpx.Response(500, json={"error": "boom"}))
    result = runner.invoke(cli, ["--quiet", "auth", "status", "--check"])
    assert result.exit_code == 1
    error = json.loads(result.stdout)["check_error"]["error"]
    assert f"warning: {error}" in result.stderr


@pytest.mark.parametrize(
    ("stored", "argv"),
    [
        (True, ["auth", "logout"]),
        (False, ["auth", "logout"]),
        (True, ["--dry-run", "auth", "logout"]),
    ],
    ids=["removed", "nothing stored", "dry run"],
)
def test_quiet_logout_prints_only_the_document(cfg_dir, stored, argv):
    if stored:
        saved(cfg_dir)
    result = runner.invoke(cli, ["--quiet", *argv])
    assert result.exit_code == 0
    assert result.stderr == ""
    assert json.loads(result.stdout)["config_dir"] == str(cfg_dir)


# -- status and the group --------------------------------------------------------
def test_status_without_a_subdomain_shows_the_base_url_override(cfg_dir):
    env = {"HFOX_BASE_URL": "https://gw.internal", "HFOX_API_KEY": API_KEY,
           "HFOX_AUTH_CODE": AUTH_CODE}
    result = runner.invoke(cli, ["auth", "status"], env=env)
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["subdomain"] is None
    assert payload["base_url"] == "https://gw.internal/api/1.1/json"
    assert payload["authenticated"] is True


def test_status_after_logout_still_names_the_account(mock_api, cfg_dir):
    mock_api(staff_ok())
    assert runner.invoke(cli, login_as("acme", "--region", "eu")).exit_code == 0
    assert runner.invoke(cli, ["auth", "logout"]).exit_code == 0
    result = runner.invoke(cli, ["auth", "status"])
    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert (payload["subdomain"], payload["region"]) == ("acme", "eu")
    assert payload["base_url"] == "https://acme.happyfox.net/api/1.1/json"
    assert (payload["authenticated"], payload["api_key"]) == (False, None)


def test_status_shows_only_the_edges_of_an_eight_character_key(cfg_dir):
    env = {"HFOX_SUBDOMAIN": "acme", "HFOX_API_KEY": "abcdefgh", "HFOX_AUTH_CODE": AUTH_CODE}
    result = runner.invoke(cli, ["auth", "status"], env=env)
    assert result.exit_code == 0
    assert json.loads(result.stdout)["api_key"] == "ab••••••gh"


def test_bare_auth_shows_help_and_exits_0(monkeypatch, capsys):
    code, out, _ = run_main(monkeypatch, capsys, ["auth"])
    assert code == 0
    assert "Usage" in out
    for verb in ("login", "status", "logout"):
        assert verb in out
