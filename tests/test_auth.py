"""`hfox auth` login, status and logout.

The login probe and the status check go through the `mock_api` transport. Exit codes that
`main.app()` assigns are tested through `run_main`.
"""

import io
import json
import sys

import httpx
import pytest
from typer.testing import CliRunner

import hfox.cli.context as context_mod
import hfox.cli.main as main_mod
from hfox.cli.main import cli
from hfox.core.config import save_credentials
from hfox.core.errors import ValidationError

runner = CliRunner()

API_KEY = "KEY-SECRET-123"
AUTH_CODE = "CODE-SECRET-456"
SECRETS = {"HFOX_API_KEY": API_KEY, "HFOX_AUTH_CODE": AUTH_CODE}
LOGIN = ["auth", "login", "--subdomain", "acme", "--region", "us"]
LOGIN_KEYS = ["authenticated", "subdomain", "region", "base_url", "default_staff_id",
              "config_dir"]
STAFF = [{"id": 7, "email": "a@x.org"}]


@pytest.fixture
def cfg_dir(tmp_path, monkeypatch):
    path = tmp_path / "hfox"
    monkeypatch.setenv("HFOX_CONFIG_DIR", str(path))
    return path


@pytest.fixture
def secrets(monkeypatch):
    """Supply the API key and auth code through the environment, as login reads them."""
    for key, value in SECRETS.items():
        monkeypatch.setenv(key, value)


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


def assert_no_secrets(text):
    assert API_KEY not in text
    assert AUTH_CODE not in text


# -- login -------------------------------------------------------------------
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_login_persists_token_at_0600(mock_api, cfg_dir, secrets):
    mock_api(staff_ok())
    result = runner.invoke(cli, [*LOGIN, "--email", "a@x.org"])
    assert result.exit_code == 0, result.stderr
    token = cfg_dir / "token.json"
    assert (token.stat().st_mode & 0o777) == 0o600


def test_login_saves_credentials_and_default_staff_id(mock_api, cfg_dir, secrets):
    requests = mock_api(staff_ok())
    result = runner.invoke(cli, [*LOGIN, "--email", "a@x.org"])
    assert result.exit_code == 0, result.stderr
    assert [(r.method, str(r.url)) for r in requests] == [
        ("GET", "https://acme.happyfox.com/api/1.1/json/staff/")
    ]
    token = json.loads((cfg_dir / "token.json").read_text())
    assert token == {
        "subdomain": "acme", "region": "us", "api_key": API_KEY, "auth_code": AUTH_CODE,
    }
    assert "default_staff_id = 7" in (cfg_dir / "config.toml").read_text()


def test_login_renders_one_document_without_secrets(mock_api, cfg_dir, secrets):
    mock_api(staff_ok())
    result = runner.invoke(cli, [*LOGIN, "--email", "a@x.org"])
    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert list(payload) == LOGIN_KEYS
    assert payload == {
        "authenticated": True,
        "subdomain": "acme",
        "region": "us",
        "base_url": "https://acme.happyfox.com/api/1.1/json",
        "default_staff_id": 7,
        "config_dir": str(cfg_dir),
    }
    assert_no_secrets(result.stdout)
    assert_no_secrets(result.stderr)
    assert "Credentials saved to" in result.stderr


def test_login_without_email_renders_null_staff_id(mock_api, cfg_dir, secrets):
    mock_api(staff_ok())
    result = runner.invoke(cli, LOGIN)
    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout)["default_staff_id"] is None


def test_login_staff_id_zero_stays_zero(mock_api, cfg_dir, secrets):
    mock_api(staff_ok([{"id": 0, "email": "a@x.org"}]))
    result = runner.invoke(cli, [*LOGIN, "--email", "a@x.org"])
    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout)["default_staff_id"] == 0
    assert "default_staff_id = 0" in (cfg_dir / "config.toml").read_text()


def login_as(subdomain="acme", *extra):
    return ["auth", "login", "--subdomain", subdomain, *extra]


def status_staff_id():
    return json.loads(runner.invoke(cli, ["auth", "status"]).stdout)["default_staff_id"]


def test_relogin_to_the_same_target_keeps_the_default(mock_api, cfg_dir, secrets):
    mock_api(staff_ok())
    assert runner.invoke(cli, login_as("acme", "--email", "a@x.org")).exit_code == 0
    result = runner.invoke(cli, login_as("ACME"))
    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout)["default_staff_id"] == 7
    assert "default_staff_id = 7" in (cfg_dir / "config.toml").read_text()
    assert "Default staff id 7 kept." in result.stderr
    assert status_staff_id() == 7


@pytest.mark.parametrize(
    ("argv", "env"),
    [
        (login_as("other"), {}),
        (login_as("acme", "--region", "eu"), {}),
        (login_as("acme"), {"HFOX_BASE_URL": "https://gw.internal"}),
        (["auth", "login"], {"HFOX_SUBDOMAIN": "other"}),
    ],
)
def test_relogin_to_another_target_removes_the_default(mock_api, cfg_dir, secrets, argv, env):
    mock_api(staff_ok())
    assert runner.invoke(cli, login_as("acme", "--email", "a@x.org")).exit_code == 0
    result = runner.invoke(cli, argv, env=env)
    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout)["default_staff_id"] is None
    assert "default_staff_id" not in (cfg_dir / "config.toml").read_text()
    assert "Removed the stored default staff id: the login target changed." in result.stderr
    assert status_staff_id() is None


def test_relogin_with_unmatched_email_removes_the_default(mock_api, cfg_dir, secrets):
    mock_api(staff_ok())
    assert runner.invoke(cli, login_as("acme", "--email", "a@x.org")).exit_code == 0
    result = runner.invoke(cli, login_as("acme", "--email", "ghost@x.org"))
    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout)["default_staff_id"] is None
    assert "default_staff_id" not in (cfg_dir / "config.toml").read_text()
    assert "No agent matched ghost@x.org; default staff id not set." in result.stderr
    assert "Removed the stored default staff id." in result.stderr


def test_relogin_keeps_other_settings_when_the_default_is_removed(mock_api, cfg_dir, secrets):
    mock_api(staff_ok())
    assert runner.invoke(cli, login_as("acme", "--email", "a@x.org")).exit_code == 0
    with (cfg_dir / "config.toml").open("a") as fh:
        fh.write('default_format = "yaml"\n')
    assert runner.invoke(cli, login_as("other")).exit_code == 0
    text = (cfg_dir / "config.toml").read_text()
    assert 'default_format = "yaml"' in text
    assert 'subdomain = "other"' in text


def test_first_login_without_email_says_nothing_about_a_default(mock_api, cfg_dir, secrets):
    mock_api(staff_ok())
    result = runner.invoke(cli, LOGIN)
    assert result.exit_code == 0, result.stderr
    assert "default staff id" not in result.stderr.lower()


@pytest.mark.parametrize("bad_id", [True, 1.9, -5, "x7", None, [7]])
def test_login_unusable_staff_id_warns_and_saves_both_files(mock_api, cfg_dir, secrets, bad_id):
    mock_api(staff_ok([{"id": bad_id, "email": "a@x.org"}]))
    result = runner.invoke(cli, [*LOGIN, "--email", "a@x.org"])
    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout)["default_staff_id"] is None
    assert f"Ignoring unexpected staff id {bad_id!r}; default staff id not set." in result.stderr
    assert (cfg_dir / "token.json").exists()
    config = (cfg_dir / "config.toml").read_text()
    assert 'subdomain = "acme"' in config
    assert "default_staff_id" not in config


def test_login_unusable_staff_id_removes_a_stored_default(mock_api, cfg_dir, secrets):
    mock_api(staff_ok())
    assert runner.invoke(cli, [*LOGIN, "--email", "a@x.org"]).exit_code == 0
    mock_api(staff_ok([{"id": True, "email": "a@x.org"}]))
    result = runner.invoke(cli, [*LOGIN, "--email", "a@x.org"])
    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout)["default_staff_id"] is None
    assert "default_staff_id" not in (cfg_dir / "config.toml").read_text()


def test_login_digit_string_staff_id_is_saved_as_an_int(mock_api, cfg_dir, secrets):
    mock_api(staff_ok([{"id": " 7 ", "email": "a@x.org"}]))
    result = runner.invoke(cli, [*LOGIN, "--email", "a@x.org"])
    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout)["default_staff_id"] == 7
    assert "default_staff_id = 7" in (cfg_dir / "config.toml").read_text()


def test_login_several_email_matches_leave_the_default_unset(mock_api, cfg_dir, secrets):
    mock_api(staff_ok([{"id": 7, "email": "a@x.org"}, {"id": 8, "email": "A@X.org"}]))
    result = runner.invoke(cli, [*LOGIN, "--email", "a@x.org"])
    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout)["default_staff_id"] is None
    assert "2 agents matched a@x.org; default staff id not set." in result.stderr
    assert "default_staff_id" not in (cfg_dir / "config.toml").read_text()


def test_login_quiet_keeps_the_document(mock_api, cfg_dir, secrets):
    mock_api(staff_ok())
    result = runner.invoke(cli, ["--quiet", *LOGIN])
    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout)["authenticated"] is True
    assert result.stderr == ""


def test_login_proxied_base_url_is_rendered(mock_api, cfg_dir, secrets, monkeypatch):
    monkeypatch.setenv("HFOX_BASE_URL", "https://gw.internal")
    requests = mock_api(staff_ok())
    result = runner.invoke(cli, LOGIN)
    assert result.exit_code == 0, result.stderr
    assert str(requests[0].url) == "https://gw.internal/api/1.1/json/staff/"
    assert json.loads(result.stdout)["base_url"] == "https://gw.internal/api/1.1/json"


@pytest.mark.parametrize(
    "url",
    ["https://proxy.example/hf", "https://proxy.example/hf/", "https://proxy.example/hf/api/1.1/json"],
    ids=["bare", "trailing-slash", "api-prefix"],
)
def test_login_keeps_a_base_url_path_prefix(mock_api, cfg_dir, secrets, monkeypatch, url):
    monkeypatch.setenv("HFOX_BASE_URL", url)
    requests = mock_api(staff_ok())
    result = runner.invoke(cli, LOGIN)
    assert result.exit_code == 0, result.stderr
    assert str(requests[0].url) == "https://proxy.example/hf/api/1.1/json/staff/"
    assert json.loads(result.stdout)["base_url"] == "https://proxy.example/hf/api/1.1/json"
    token = json.loads((cfg_dir / "token.json").read_text())
    assert token["base_url"] == "https://proxy.example/hf"


def test_login_warns_when_a_stored_base_url_overrides_subdomain(mock_api, cfg_dir, secrets):
    saved(cfg_dir, base_url="https://gw.internal")
    requests = mock_api(staff_ok())
    result = runner.invoke(cli, login_as("other"))
    assert result.exit_code == 0, result.stderr
    assert str(requests[0].url) == "https://gw.internal/api/1.1/json/staff/"
    assert (
        "The stored base URL https://gw.internal overrides --subdomain; "
        "`hfox auth logout` clears it."
    ) in result.stderr


def test_login_base_url_warning_points_at_config_toml(mock_api, cfg_dir, secrets):
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.toml").write_text('base_url = "https://gw.internal"\n', encoding="utf-8")
    mock_api(staff_ok())
    result = runner.invoke(cli, login_as("other"))
    assert result.exit_code == 0, result.stderr
    assert (
        "The stored base URL https://gw.internal overrides --subdomain; "
        f"remove base_url from {cfg_dir / 'config.toml'} to clear it."
    ) in result.stderr
    assert "auth logout" not in result.stderr


@pytest.mark.parametrize("subdomain", ["bad\x07name", "alice:hunter2pw@acme"])
def test_login_rejects_a_bad_subdomain_under_a_base_url_override(
    monkeypatch, cfg_dir, secrets, subdomain
):
    monkeypatch.setenv("HFOX_BASE_URL", "https://gw.internal")
    no_client(monkeypatch)
    result = runner.invoke(cli, login_as(subdomain))
    assert isinstance(result.exception, ValidationError)
    assert "hunter2pw" not in str(result.exception)
    assert not (cfg_dir / "token.json").exists()
    assert not (cfg_dir / "config.toml").exists()


def test_login_dry_run_also_warns_about_a_stored_base_url(monkeypatch, cfg_dir, secrets):
    saved(cfg_dir, base_url="https://gw.internal")
    no_client(monkeypatch)
    result = runner.invoke(cli, ["--dry-run", *login_as("other")])
    assert result.exit_code == 0, result.stderr
    assert "overrides --subdomain" in result.stderr


@pytest.mark.parametrize(
    ("argv", "env"),
    [
        (login_as("other"), {"HFOX_BASE_URL": "https://gw.internal"}),
        (["auth", "login"], {"HFOX_SUBDOMAIN": "other"}),
    ],
)
def test_login_base_url_warning_needs_a_stored_url_and_the_flag(
    mock_api, cfg_dir, secrets, argv, env
):
    saved(cfg_dir, base_url="https://gw.internal")
    mock_api(staff_ok())
    result = runner.invoke(cli, argv, env=env)
    assert result.exit_code == 0, result.stderr
    assert "overrides --subdomain" not in result.stderr


def test_login_dry_run_previews_probe_and_saves_nothing(monkeypatch, cfg_dir, secrets):
    no_client(monkeypatch)
    result = runner.invoke(cli, ["--dry-run", *LOGIN])
    assert result.exit_code == 0, result.output
    preview = json.loads(result.stdout)
    assert preview["dry_run"] is True
    assert preview["method"] == "GET"
    assert preview["url"] == "https://acme.happyfox.com/api/1.1/json/staff/"
    assert preview["body"] is None
    assert_no_secrets(result.stdout)
    assert not (cfg_dir / "token.json").exists()
    assert not (cfg_dir / "config.toml").exists()


def test_login_unknown_region_is_validation_error(monkeypatch, cfg_dir, secrets):
    no_client(monkeypatch)
    result = runner.invoke(cli, ["auth", "login", "--subdomain", "acme", "--region", "mars"])
    assert isinstance(result.exception, ValidationError)
    assert str(result.exception) == "region must be one of ['us', 'eu']"


# -- login without a terminal --------------------------------------------------
@pytest.mark.parametrize(
    ("argv", "env", "message"),
    [
        ([], {}, "Missing the subdomain, API key and auth code; stdin is not a terminal, so set "
                 "HFOX_SUBDOMAIN, HFOX_API_KEY and HFOX_AUTH_CODE. "
                 "The subdomain can also be passed with --subdomain."),
        (["--subdomain", "acme"], {},
         "Missing the API key and auth code; stdin is not a terminal, so set HFOX_API_KEY and "
         "HFOX_AUTH_CODE."),
        (["--subdomain", "acme"], {"HFOX_API_KEY": API_KEY},
         "Missing the auth code; stdin is not a terminal, so set HFOX_AUTH_CODE."),
        ([], SECRETS,
         "Missing the subdomain; stdin is not a terminal, so set HFOX_SUBDOMAIN. "
         "The subdomain can also be passed with --subdomain."),
        (["--subdomain", "acme"], {"HFOX_API_KEY": "", "HFOX_AUTH_CODE": AUTH_CODE},
         "Missing the API key; stdin is not a terminal, so set HFOX_API_KEY."),
    ],
)
def test_login_missing_value_without_tty_names_only_what_is_missing(
    monkeypatch, cfg_dir, argv, env, message
):
    no_client(monkeypatch)
    result = runner.invoke(
        cli, ["auth", "login", *argv], env=env, input="typed\ntyped\ntyped\n"
    )
    assert isinstance(result.exception, ValidationError)
    assert str(result.exception) == message
    assert result.stdout == ""
    assert result.stderr == ""
    assert not (cfg_dir / "token.json").exists()


def test_login_missing_value_without_tty_exits_3(monkeypatch, capsys, cfg_dir):
    no_client(monkeypatch)
    code, out, _ = run_main(monkeypatch, capsys, ["auth", "login", "--subdomain", "acme"])
    assert code == 3
    payload = json.loads(out)
    assert (payload["type"], payload["exit_code"]) == ("validation", 3)
    assert "API key and auth code" in payload["error"]


def test_login_without_tty_defaults_region_to_us(mock_api, cfg_dir, secrets):
    mock_api(staff_ok())
    result = runner.invoke(cli, ["auth", "login", "--subdomain", "acme"])
    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout)["region"] == "us"
    assert json.loads((cfg_dir / "token.json").read_text())["region"] == "us"
    assert "Data center" not in result.stderr


def test_login_reads_both_secrets_from_the_environment(mock_api, cfg_dir, secrets):
    requests = mock_api(staff_ok())
    result = runner.invoke(cli, ["auth", "login", "--subdomain", "acme"])
    assert result.exit_code == 0, result.stderr
    assert requests[0].headers["authorization"] == httpx.BasicAuth(
        API_KEY, AUTH_CODE
    )._auth_header
    token = json.loads((cfg_dir / "token.json").read_text())
    assert (token["api_key"], token["auth_code"]) == (API_KEY, AUTH_CODE)
    assert list(json.loads(result.stdout)) == LOGIN_KEYS
    assert_no_secrets(result.stdout)
    assert_no_secrets(result.stderr)


def test_login_takes_subdomain_and_region_from_env(mock_api, cfg_dir, monkeypatch):
    for key, value in {
        "HFOX_SUBDOMAIN": "acme", "HFOX_REGION": " EU ",
        "HFOX_API_KEY": API_KEY, "HFOX_AUTH_CODE": AUTH_CODE,
    }.items():
        monkeypatch.setenv(key, value)
    requests = mock_api(staff_ok())
    result = runner.invoke(cli, ["auth", "login"])
    assert result.exit_code == 0, result.stderr
    assert str(requests[0].url) == "https://acme.happyfox.net/api/1.1/json/staff/"
    assert json.loads(result.stdout)["region"] == "eu"


def test_login_flag_beats_env(mock_api, cfg_dir, secrets, monkeypatch):
    monkeypatch.setenv("HFOX_SUBDOMAIN", "fromenv")
    requests = mock_api(staff_ok())
    result = runner.invoke(cli, LOGIN)
    assert result.exit_code == 0, result.stderr
    assert requests[0].url.host == "acme.happyfox.com"


def test_login_empty_env_counts_as_unset(monkeypatch, cfg_dir):
    monkeypatch.setenv("HFOX_API_KEY", "")
    monkeypatch.setenv("HFOX_AUTH_CODE", "")
    no_client(monkeypatch)
    result = runner.invoke(cli, ["auth", "login", "--subdomain", "acme"])
    assert isinstance(result.exception, ValidationError)
    assert "Missing the API key and auth code;" in str(result.exception)


# -- login prompts -------------------------------------------------------------
PROMPTS = ["HappyFox subdomain (e.g. acme)", "Data center [us/eu]", "API key", "Auth code"]
ANSWERS = f"acme\n\n{API_KEY}\n{AUTH_CODE}\n"


def test_login_prompts_on_stderr_and_renders_one_document(mock_api, cfg_dir, tty):
    requests = mock_api(staff_ok())
    result = runner.invoke(cli, ["auth", "login"], input=ANSWERS)
    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert list(payload) == LOGIN_KEYS
    assert (payload["subdomain"], payload["region"]) == ("acme", "us")
    for label in PROMPTS:
        assert label in result.stderr
        assert label not in result.stdout
    assert_no_secrets(result.stdout)
    assert len(requests) == 1
    token = json.loads((cfg_dir / "token.json").read_text())
    assert (token["api_key"], token["auth_code"]) == (API_KEY, AUTH_CODE)


def test_login_dry_run_prompts_on_stderr_and_previews(monkeypatch, cfg_dir, tty):
    no_client(monkeypatch)
    result = runner.invoke(cli, ["--dry-run", "auth", "login"], input=ANSWERS)
    assert result.exit_code == 0, result.stderr
    preview = json.loads(result.stdout)
    assert preview["url"] == "https://acme.happyfox.com/api/1.1/json/staff/"
    for label in PROMPTS:
        assert label in result.stderr
        assert label not in result.stdout
    assert_no_secrets(result.stdout)
    assert not (cfg_dir / "token.json").exists()


def test_login_prompts_only_for_the_missing_region(mock_api, cfg_dir, tty, secrets):
    mock_api(staff_ok())
    result = runner.invoke(cli, ["auth", "login", "--subdomain", "acme"], input="eu\n")
    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout)["region"] == "eu"
    assert "Data center" in result.stderr
    assert "API key" not in result.stderr


def test_login_prompt_eof_is_cancelled(monkeypatch, capsys, cfg_dir, tty):
    no_client(monkeypatch)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    code, out, _ = run_main(monkeypatch, capsys, ["auth", "login"])
    assert code == 5
    payload = json.loads(out)
    assert payload["type"] == "cancelled"
    assert payload["error"] == "Cancelled at the prompt."


# -- login errors ----------------------------------------------------------------
def test_login_401_exits_2_as_auth(mock_api, monkeypatch, capsys, cfg_dir, secrets):
    mock_api(lambda r: httpx.Response(401, json={"error": "bad key"}))
    code, out, _ = run_main(monkeypatch, capsys, LOGIN)
    assert code == 2
    payload = json.loads(out)
    assert (payload["type"], payload["exit_code"]) == ("auth", 2)
    assert payload["error"].startswith("Could not validate credentials: ")
    assert payload["detail"] == "bad key"
    assert_no_secrets(out)
    assert not (cfg_dir / "token.json").exists()


def test_login_403_is_wrapped_as_auth(mock_api, monkeypatch, capsys, cfg_dir, secrets):
    mock_api(lambda r: httpx.Response(403, json={"error": "forbidden"}))
    code, out, _ = run_main(monkeypatch, capsys, LOGIN)
    assert code == 2
    assert json.loads(out)["error"].startswith("Could not validate credentials: ")


def test_login_transport_error_exits_5_as_network(mock_api, monkeypatch, capsys, cfg_dir, secrets):
    def handler(request):
        raise httpx.ConnectError("boom", request=request)

    mock_api(handler)
    code, out, _ = run_main(monkeypatch, capsys, ["--max-retries", "0", *LOGIN])
    assert code == 5
    payload = json.loads(out)
    assert (payload["type"], payload["exit_code"]) == ("network", 5)
    assert "Could not validate credentials" not in payload["error"]
    assert not (cfg_dir / "token.json").exists()


def test_login_timeout_keeps_its_type(mock_api, monkeypatch, capsys, cfg_dir, secrets):
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    mock_api(handler)
    code, out, _ = run_main(monkeypatch, capsys, ["--max-retries", "0", *LOGIN])
    assert code == 5
    assert json.loads(out)["type"] == "timeout"


def test_login_429_exits_1_as_rate_limited(mock_api, monkeypatch, capsys, cfg_dir, secrets):
    requests = mock_api(lambda r: httpx.Response(429, headers={"Retry-After": "600"}, json={}))
    code, out, _ = run_main(monkeypatch, capsys, ["--max-retries", "0", *LOGIN])
    assert code == 1
    payload = json.loads(out)
    assert (payload["type"], payload["status_code"]) == ("rate_limited", 429)
    assert payload["retry_after"] == 600
    assert len(requests) == 1
    assert not (cfg_dir / "token.json").exists()


@pytest.mark.parametrize(("status", "error_type", "code"), [(404, "not_found", 4), (500, "api", 1)])
def test_login_other_api_errors_propagate(
    mock_api, monkeypatch, capsys, cfg_dir, secrets, status, error_type, code
):
    mock_api(lambda r: httpx.Response(status, json={"error": "x"}))
    got, out, _ = run_main(monkeypatch, capsys, LOGIN)
    assert got == code
    payload = json.loads(out)
    assert payload["type"] == error_type
    assert "outcome_unknown" not in payload


def test_login_non_list_body_exits_5_and_saves_nothing(
    mock_api, monkeypatch, capsys, cfg_dir, secrets
):
    mock_api(lambda r: httpx.Response(200, json={"unexpected": True}))
    code, out, _ = run_main(monkeypatch, capsys, LOGIN)
    assert code == 5
    payload = json.loads(out)
    assert payload["type"] == "other"
    assert payload["error"] == "Unexpected response from staff/; credentials were not saved."
    assert not (cfg_dir / "token.json").exists()
    assert not (cfg_dir / "config.toml").exists()


def test_login_http_base_url_warns_once(mock_api, cfg_dir, secrets, monkeypatch):
    monkeypatch.setenv("HFOX_BASE_URL", "http://gw.internal")
    mock_api(staff_ok())
    result = runner.invoke(cli, LOGIN)
    assert result.exit_code == 0, result.stderr
    assert result.stderr.count("Sending credentials in cleartext to http://gw.internal") == 1


def test_login_passes_timeout_and_retries_to_the_probe(monkeypatch, cfg_dir, secrets):
    seen = {}

    class Probe:
        def __init__(self, base_url, api_key, auth_code, **kwargs):
            seen.update(kwargs, base_url=base_url)

        def get(self, path):
            return []

        def close(self):
            seen["closed"] = True

    monkeypatch.setattr(context_mod, "HappyFoxClient", Probe)
    result = runner.invoke(cli, ["--timeout", "2.5", "--max-retries", "1", *LOGIN])
    assert result.exit_code == 0, result.stderr
    assert (seen["timeout"], seen["max_retries"], seen["closed"]) == (2.5, 1, True)


# -- removed flags ---------------------------------------------------------------
@pytest.mark.parametrize(
    ("argv", "flag"),
    [
        (["auth", "login", "--subdomain", "acme", "--api-key", API_KEY], "--api-key"),
        (["auth", "login", "--subdomain", "acme", "--auth-code", AUTH_CODE], "--auth-code"),
        (["auth", "login", "-s", "acme"], "-s"),
        (["auth", "login", "--subdomain", "acme", "-e", "a@x.org"], "-e"),
        (["auth", "status", "--check"], "--check"),
    ],
    ids=["--api-key", "--auth-code", "-s", "-e", "status --check"],
)
def test_removed_auth_flags_are_usage_errors(monkeypatch, capsys, cfg_dir, secrets, argv, flag):
    no_client(monkeypatch)
    code, out, _ = run_main(monkeypatch, capsys, argv)
    assert code == 3
    payload = json.loads(out)
    assert (payload["type"], payload["exit_code"]) == ("usage", 3)
    assert payload["error"].startswith(f"No such option: {flag}")
    assert_no_secrets(out)
    assert not (cfg_dir / "token.json").exists()


# -- status ----------------------------------------------------------------------
STATUS_KEYS = ["config_dir", "subdomain", "region", "base_url", "authenticated",
               "default_staff_id", "api_key"]


def test_status_verified_exits_0_after_one_request(mock_api, cfg_dir):
    saved(cfg_dir)
    requests = mock_api(staff_ok())
    result = runner.invoke(cli, ["auth", "status"])
    assert result.exit_code == 0, result.stderr
    assert [(r.method, str(r.url)) for r in requests] == [
        ("GET", "https://acme.happyfox.com/api/1.1/json/staff/")
    ]
    payload = json.loads(result.stdout)
    assert list(payload) == STATUS_KEYS
    assert payload["authenticated"] is True
    assert payload["config_dir"] == str(cfg_dir)
    assert payload["base_url"] == "https://acme.happyfox.com/api/1.1/json"
    assert payload["default_staff_id"] is None
    assert payload["api_key"] == "KE••••••23"
    assert_no_secrets(result.stdout)
    assert result.stderr == ""


def test_status_missing_credentials_exits_2_and_sends_nothing(monkeypatch, cfg_dir):
    no_client(monkeypatch)
    result = runner.invoke(cli, ["auth", "status"])
    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert list(payload) == STATUS_KEYS
    assert payload["authenticated"] is False
    assert payload["api_key"] is None
    assert payload["base_url"] is None
    assert "Not authenticated. Run `hfox auth login`." in result.stderr


def test_status_missing_credentials_exit_code_through_app(monkeypatch, capsys, cfg_dir):
    no_client(monkeypatch)
    code, out, err = run_main(monkeypatch, capsys, ["auth", "status"])
    assert code == 2
    payload = json.loads(out)
    assert payload["authenticated"] is False
    assert "error" not in payload
    assert "Not authenticated" in err


def test_status_dry_run_without_credentials_still_exits_2(monkeypatch, cfg_dir):
    no_client(monkeypatch)
    result = runner.invoke(cli, ["--dry-run", "auth", "status"])
    assert result.exit_code == 2
    assert json.loads(result.stdout)["authenticated"] is False


def test_status_default_staff_id_is_an_int(mock_api, cfg_dir, monkeypatch):
    saved(cfg_dir)
    mock_api(staff_ok())
    monkeypatch.setenv("HFOX_STAFF_ID", "12")
    payload = json.loads(runner.invoke(cli, ["auth", "status"]).stdout)
    assert payload["default_staff_id"] == 12
    assert "default_staff_id_error" not in payload


def test_status_malformed_env_staff_id_is_null_plus_error(mock_api, cfg_dir, monkeypatch):
    saved(cfg_dir)
    mock_api(staff_ok())
    monkeypatch.setenv("HFOX_STAFF_ID", "1.5")
    result = runner.invoke(cli, ["auth", "status"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["default_staff_id"] is None
    assert "HFOX_STAFF_ID" in payload["default_staff_id_error"]
    assert list(payload) == [*STATUS_KEYS, "default_staff_id_error"]


@pytest.mark.parametrize("status", [401, 403])
def test_status_rejected_credentials_exit_2_as_unauthenticated(
    mock_api, monkeypatch, capsys, cfg_dir, status
):
    saved(cfg_dir)
    mock_api(lambda r: httpx.Response(status, json={"error": "bad key"}))
    code, out, err = run_main(monkeypatch, capsys, ["auth", "status"])
    assert code == 2
    payload = json.loads(out)
    assert list(payload) == [*STATUS_KEYS, "check_error"]
    assert payload["authenticated"] is False
    assert (payload["check_error"]["type"], payload["check_error"]["exit_code"]) == ("auth", 2)
    assert "error" not in payload
    assert payload["check_error"]["error"] in err
    assert_no_secrets(out)


def test_status_network_error_leaves_authenticated_unknown(mock_api, monkeypatch, capsys, cfg_dir):
    saved(cfg_dir)

    def handler(request):
        raise httpx.ConnectError("boom", request=request)

    mock_api(handler)
    code, out, err = run_main(monkeypatch, capsys, ["--max-retries", "0", "auth", "status"])
    assert code == 5
    payload = json.loads(out)
    assert payload["authenticated"] is None
    assert (payload["check_error"]["type"], payload["check_error"]["exit_code"]) == ("network", 5)
    assert payload["check_error"]["error"] in err


@pytest.mark.parametrize(
    ("response", "slug", "code"),
    [
        (lambda r: httpx.Response(500, json={"error": "boom"}), "api", 1),
        (lambda r: httpx.Response(429, json={}), "rate_limited", 1),
        (lambda r: httpx.Response(404, json={"error": "gone"}), "not_found", 4),
    ],
    ids=["500", "429", "404"],
)
def test_status_failed_check_exits_with_that_errors_code(
    mock_api, monkeypatch, capsys, cfg_dir, response, slug, code
):
    saved(cfg_dir)
    mock_api(response)
    got, out, _ = run_main(monkeypatch, capsys, ["--max-retries", "0", "auth", "status"])
    assert got == code
    payload = json.loads(out)
    assert payload["authenticated"] is None
    assert (payload["check_error"]["type"], payload["check_error"]["exit_code"]) == (slug, code)


@pytest.mark.parametrize(
    "response",
    [
        lambda r: httpx.Response(200, html="<html><title>Sign in</title></html>"),
        lambda r: httpx.Response(200, json={"x": 1}),
    ],
    ids=["html", "object"],
)
def test_status_non_list_body_leaves_authenticated_unknown(
    mock_api, monkeypatch, capsys, cfg_dir, response
):
    saved(cfg_dir)
    mock_api(response)
    code, out, err = run_main(monkeypatch, capsys, ["auth", "status"])
    assert code == 5
    payload = json.loads(out)
    assert payload["authenticated"] is None
    assert payload["check_error"] == {
        "error": "Unexpected response from staff/; cannot verify the credentials.",
        "type": "other",
        "exit_code": 5,
    }
    assert "cannot verify the credentials" in err


def test_status_dry_run_previews_the_request(monkeypatch, cfg_dir):
    saved(cfg_dir)
    no_client(monkeypatch)
    result = runner.invoke(cli, ["--dry-run", "auth", "status"])
    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout) == {
        "dry_run": True,
        "method": "GET",
        "url": "https://acme.happyfox.com/api/1.1/json/staff/",
        "params": None,
        "body": None,
        "attachments": None,
    }
    assert_no_secrets(result.stdout)


# -- logout ----------------------------------------------------------------------
def test_logout_removes_token(cfg_dir):
    saved(cfg_dir)
    result = runner.invoke(cli, ["auth", "logout"])
    assert result.exit_code == 0
    assert not (cfg_dir / "token.json").exists()
    assert json.loads(result.stdout) == {"removed": True, "config_dir": str(cfg_dir)}
    assert "Credentials removed." in result.stderr


def test_logout_when_nothing_stored(cfg_dir):
    cfg_dir.mkdir(parents=True)
    result = runner.invoke(cli, ["auth", "logout"])
    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"removed": False, "config_dir": str(cfg_dir)}
    assert "No stored credentials found." in result.stderr


@pytest.mark.parametrize("fmt", [[], ["-f", "table"], ["-f", "csv"]], ids=["json", "table", "csv"])
@pytest.mark.parametrize("stored", [True, False], ids=["stored", "nothing stored"])
def test_logout_dry_run_prints_json_and_keeps_the_token(cfg_dir, fmt, stored):
    cfg_dir.mkdir(parents=True)
    if stored:
        saved(cfg_dir)
    result = runner.invoke(cli, [*fmt, "--dry-run", "auth", "logout"])
    assert result.exit_code == 0, result.stderr
    assert (cfg_dir / "token.json").exists() is stored
    assert json.loads(result.stdout) == {
        "dry_run": True, "would_remove": stored, "config_dir": str(cfg_dir),
    }
    if stored:
        assert f"Dry run: would remove {cfg_dir / 'token.json'}." in result.stderr
    else:
        assert "Dry run: no stored credentials to remove." in result.stderr
