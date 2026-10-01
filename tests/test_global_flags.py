"""Global flags: placement, --format sources, --timeout, --max-retries, the retry notice
and the cleartext warning.
"""

import json
import sys

import httpx
import pytest
import typer
from typer.testing import CliRunner

import hfox.cli.context as context_mod
import hfox.cli.main as main_mod
from hfox.cli.context import AppContext
from hfox.cli.main import cli
from hfox.core.client import DEFAULT_TIMEOUT, MAX_RETRIES, HappyFoxClient
from hfox.core.config import Config
from hfox.core.errors import UsageError, ValidationError

runner = CliRunner()

ENV = {
    "HFOX_SUBDOMAIN": "acme",
    "HFOX_REGION": "us",
    "HFOX_API_KEY": "k",
    "HFOX_AUTH_CODE": "c",
    "HFOX_STAFF_ID": "1",
}

HINT = "Global flags precede the resource; see `hfox --help`."


def placement(name):
    return f"{name} is a global flag and goes before the resource: hfox {name} <resource> <verb>."


def run(*args, env_extra=None):
    return runner.invoke(cli, list(args), env={**ENV, **(env_extra or {})})


@pytest.fixture
def run_app(monkeypatch, capsys):
    """Run the real app() in process; return (exit code, parsed stdout or None, stderr)."""

    def invoke(*argv, **env_extra):
        for key, value in {**ENV, **env_extra}.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setattr(sys, "argv", ["hfox", *argv])
        code = 0
        try:
            main_mod.app()
        except SystemExit as exc:
            code = exc.code or 0
        captured = capsys.readouterr()
        return code, json.loads(captured.out) if captured.out.strip() else None, captured.err

    return invoke


@pytest.fixture
def client_kwargs(monkeypatch):
    """Record the keyword arguments each HappyFoxClient is built with."""
    seen: list[dict] = []

    def factory(base_url, api_key, auth_code, **kwargs):
        seen.append(kwargs)
        client = HappyFoxClient(base_url, api_key, auth_code, sleep=lambda _s: None)
        client._client = httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[]))
        )
        return client

    monkeypatch.setattr(context_mod, "HappyFoxClient", factory)
    return seen


# -- --quiet is long-only -----------------------------------------------------
def test_short_q_is_not_a_global_flag(run_app):
    code, payload, err = run_app("-q", "system", "statuses")
    assert code == 3
    assert payload == {"error": "No such option: -q", "type": "usage", "exit_code": 3}
    assert "No such option: -q" in err


def test_list_q_still_means_query():
    result = run("--dry-run", "tickets", "list", "-q", "printer")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["params"]["q"] == "printer"


def test_quiet_and_query_combine(mock_api):
    captured = mock_api(lambda req: httpx.Response(200, json={"data": []}))
    result = run("--quiet", "tickets", "list", "-q", "x")
    assert result.exit_code == 0, result.output
    assert captured[0].url.params["q"] == "x"


# -- misplaced global flags ---------------------------------------------------
@pytest.mark.parametrize(
    ("argv", "name"),
    [
        (("tickets", "list", "--dry-run"), "--dry-run"),
        (("tickets", "--dry-run", "list"), "--dry-run"),
        (("tickets", "list", "--format=table"), "--format"),
        (("tickets", "list", "--format", "table"), "--format"),
        (("tickets", "list", "-f", "table"), "-f"),
        (("tickets", "list", "--page-all"), "--page-all"),
        (("tickets", "get", "5", "--staff-id", "3"), "--staff-id"),
        (("tickets", "get", "5", "--staff", "alice"), "--staff"),
        (("system", "statuses", "--quiet"), "--quiet"),
        (("system", "statuses", "--timeout", "5"), "--timeout"),
        (("system", "statuses", "--max-retries=0"), "--max-retries"),
        (("system", "statuses", "--config-dir", "/x"), "--config-dir"),
        (("contacts", "groups", "list", "--no-color"), "--no-color"),
        (("assets", "types", "list", "--page-limit", "2"), "--page-limit"),
        (("tickets", "list", "-v"), "-v"),
    ],
)
def test_misplaced_global_flag_exits_3_with_placement_message(run_app, mock_api, argv, name):
    captured = mock_api(lambda req: httpx.Response(200, json={}))
    code, payload, err = run_app(*argv)
    assert code == 3
    assert payload == {
        "error": placement(name),
        "type": "usage",
        "exit_code": 3,
        "hint": HINT,
    }
    assert err == ""
    assert captured == []


def test_global_flag_swallowed_as_an_option_value_sends_nothing(run_app, mock_api):
    captured = mock_api(lambda req: httpx.Response(200, json={}))
    code, payload, _ = run_app("tickets", "note", "5", "--text", "--dry-run")
    assert code == 3
    assert (payload["type"], payload["error"]) == ("usage", placement("--dry-run"))
    assert captured == []


# Every root option that takes a value, with a flag where its value belongs.
SWALLOWED = [
    ("--staff", "--dry-run"),
    ("--staff=--dry-run",),
    ("--staff", "-x"),
    # Only a numeric option may take a negative number.
    ("--staff", "-5"),
    ("--staff=-5",),
    ("--config-dir", "-5"),
    ("--format", "-1"),
    ("--config-dir", "--dry-run"),
    ("--config-dir=--dry-run",),
    ("--format", "--dry-run"),
    ("-f", "--dry-run"),
    ("--page-limit", "--dry-run"),
    ("--page-delay", "--dry-run"),
    ("--staff-id", "--dry-run"),
    ("--timeout", "--dry-run"),
    ("--max-retries", "--dry-run"),
    # A repeat would otherwise replace the swallowed value before it is validated.
    ("--max-retries", "--dry-run", "--max-retries", "0"),
    ("--staff", "--dry-run", "--staff", "alice"),
    ("--dry-run", "--staff", "--quiet"),
]


@pytest.mark.parametrize("root", SWALLOWED, ids=" ".join)
def test_root_option_cannot_swallow_a_flag(run_app, mock_api, root):
    captured = mock_api(lambda req: httpx.Response(200, json={}))
    code, payload, err = run_app(*root, "tickets", "delete", "5", "--staff-id", "1", "--yes")
    name = root[-2] if root[0] == "--dry-run" else root[0].partition("=")[0]
    value = "--quiet" if root[0] == "--dry-run" else root[0].partition("=")[2] or root[1]
    assert code == 3
    assert payload == {
        "error": f"{name} needs a value, got {value!r}.",
        "type": "usage",
        "exit_code": 3,
        "hint": f"Pass {name} a value or drop it; a value cannot start with '-'.",
    }
    assert err == ""
    assert captured == []


def test_root_value_scan_covers_every_value_option():
    root = typer.main.get_command(cli)
    covered = {args[0].partition("=")[0] for args in SWALLOWED}
    options = main_mod._value_options(root)
    assert set(options) <= covered | {"--help"}
    assert not {"--dry-run", "--quiet", "--version"} & set(options)
    numeric = {"--page-limit", "--page-delay", "--staff-id", "--timeout", "--max-retries"}
    assert {name for name, is_number in options.items() if is_number} == numeric
    assert {"--staff", "--config-dir", "--format", "-f"} == set(options) - numeric


def test_root_value_check_runs_before_config_is_loaded(run_app, monkeypatch):
    monkeypatch.setattr(main_mod, "load_config", lambda _dir: pytest.fail("config loaded"))
    code, payload, _ = run_app("--config-dir", "--dry-run", "system", "statuses")
    assert (code, payload["type"]) == (3, "usage")


@pytest.mark.parametrize(
    ("root", "message"),
    [
        (("--staff-id", "-1"), "'--staff-id': -1 is not in the range"),
        (("--page-delay", "-1"), "'--page-delay': -1 is not in the range"),
        (("--timeout", "-inf"), "--timeout must be a positive number"),
    ],
)
def test_negative_number_reaches_the_option_validator(run_app, root, message):
    code, payload, _ = run_app(*root, "--dry-run", "system", "statuses")
    assert (code, payload["type"]) == (3, "validation")
    assert message in payload["error"]


def test_root_values_with_inner_hyphens_and_a_dangling_option_pass_the_scan(run_app):
    code, payload, _ = run_app("--dry-run", "--staff-id=2", "--timeout", "5", "system", "statuses")
    assert (code, payload["dry_run"]) == (0, True)
    code, payload, _ = run_app("--dry-run", "--staff")
    assert (code, payload["type"]) == (3, "usage")
    assert "needs a value" not in payload["error"]


def test_completion_parsing_skips_the_root_value_scan():
    root = typer.main.get_command(cli)
    ctx = root.make_context("hfox", ["--staff", "--dry-run", "tickets"], resilient_parsing=True)
    assert ctx.params["staff"] == "--dry-run"


def test_equals_form_sends_a_literal_flag_name():
    result = run("--dry-run", "tickets", "note", "5", "--text=--dry-run")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["body"]["plaintext"] == "--dry-run"


def test_terminator_stops_the_scan(run_app):
    code, payload, _ = run_app("--dry-run", "tickets", "get", "--", "--page-all")
    assert code == 3
    assert "global flag" not in payload["error"]
    assert "--page-all" in payload["error"]


def test_scan_raises_usage_error_under_cli_runner():
    result = run("tickets", "list", "--page-all")
    assert isinstance(result.exception, UsageError)
    assert result.exception.hint == HINT
    assert result.stdout == ""


def test_misplaced_flag_is_caught_before_config_is_loaded(run_app, monkeypatch):
    def boom(_dir):
        raise AssertionError("config loaded")

    monkeypatch.setattr(main_mod, "load_config", boom)
    code, payload, _ = run_app("tickets", "list", "--dry-run")
    assert (code, payload["type"]) == (3, "usage")


def test_unknown_flag_keeps_the_plain_message(run_app):
    code, payload, err = run_app("tickets", "list", "--bogus")
    assert code == 3
    assert payload["type"] == "usage"
    assert payload["error"].startswith("No such option: --bogus")
    assert "hint" not in payload
    assert "Usage" in err


def test_unknown_flag_at_the_root_is_not_called_misplaced(run_app):
    code, payload, _ = run_app("--bogus", "tickets", "list")
    assert code == 3
    assert payload["type"] == "usage"
    assert "global flag" not in payload["error"]


def _walk(command, path=()):
    yield path, command
    for name, sub in getattr(command, "commands", {}).items():
        yield from _walk(sub, (*path, name))


def test_no_subcommand_declares_a_root_only_long_flag():
    root = typer.main.get_command(cli)
    root_only = {
        name
        for name in main_mod._root_flags(root) - main_mod._PER_COMMAND_FLAGS
        if name.startswith("--")
    }
    assert {"--dry-run", "--format", "--page-all", "--quiet", "--timeout"} <= root_only
    assert "--help" not in root_only
    seen = 0
    for path, command in _walk(root):
        if not path:
            continue
        seen += 1
        declared = {opt for p in command.params for opt in (*p.opts, *p.secondary_opts)}
        assert not declared & root_only, f"{' '.join(path)} declares {declared & root_only}"
    assert seen > 20


def test_no_verbose_or_debug_flag():
    root = typer.main.get_command(cli)
    names = main_mod._root_flags(root)
    assert "--verbose" not in names
    assert "--debug" not in names
    version = next(p for p in root.params if p.name == "version")
    assert version.opts == ["--version", "-v"]


def test_global_options_are_in_the_documented_order():
    root = typer.main.get_command(cli)
    order = [p.opts[0] for p in root.params]
    expected = [
        "--format", "--dry-run", "--page-all", "--page-limit", "--page-delay", "--staff",
        "--staff-id", "--timeout", "--max-retries", "--quiet", "--config-dir", "--no-color",
        "--version",
    ]
    assert order[: len(expected)] == expected


def test_completion_parsing_skips_the_scan():
    root = typer.main.get_command(cli)
    ctx = root.make_context("hfox", ["tickets"], resilient_parsing=True)
    name, command, rest = root.resolve_command(ctx, ["tickets", "list", "--dry-run"])
    assert name == "tickets"
    assert rest == ["list", "--dry-run"]


# -- usage versus validation --------------------------------------------------
def test_missing_argument_is_a_usage_error(run_app):
    code, payload, err = run_app("tickets", "get")
    assert code == 3
    assert (payload["type"], payload["exit_code"]) == ("usage", 3)
    assert "ticket_id" in payload["error"].lower()
    assert "Usage" in err


def test_unknown_command_is_a_usage_error(run_app):
    code, payload, _ = run_app("nonexistent", "list")
    assert code == 3
    assert payload["type"] == "usage"


def test_out_of_range_value_is_a_validation_error(run_app):
    code, payload, _ = run_app("--dry-run", "tickets", "list", "--size", "51")
    assert code == 3
    assert payload["type"] == "validation"


# -- --format -----------------------------------------------------------------
def test_unknown_format_flag_exits_3_naming_the_flag(run_app):
    code, payload, err = run_app("-f", "bogus", "--dry-run", "system", "statuses")
    assert code == 3
    assert payload == {
        "error": "Unknown output format 'bogus' from --format; expected json, table, csv or yaml.",
        "type": "validation",
        "exit_code": 3,
    }
    assert "warning" not in err


def test_unknown_env_format_exits_3_naming_the_variable(run_app):
    code, payload, _ = run_app("--dry-run", "system", "statuses", HFOX_FORMAT="xml")
    assert code == 3
    assert payload["type"] == "validation"
    assert payload["error"] == (
        "Unknown output format 'xml' from HFOX_FORMAT; expected json, table, csv or yaml."
    )


@pytest.mark.parametrize(
    "line",
    [
        'default_format = "xml"',
        "default_format = 5",
        "default_format = true",
        "default_format = false",
        "default_format = 0",
        "default_format = []",
    ],
)
def test_unknown_config_format_exits_3_naming_the_file(run_app, tmp_path, monkeypatch, line):
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "config.toml").write_text(f"{line}\n", encoding="utf-8")
    code, payload, _ = run_app("--dry-run", "system", "statuses")
    assert code == 3
    assert payload["type"] == "validation"
    assert f"from default_format in {cfg / 'config.toml'};" in payload["error"]


def test_format_flag_beats_a_bad_configured_format(run_app):
    code, payload, _ = run_app("-f", "json", "--dry-run", "system", "statuses", HFOX_FORMAT="xml")
    assert code == 0
    assert payload["dry_run"] is True


def test_empty_configured_format_counts_as_unset(run_app, tmp_path):
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "config.toml").write_text('default_format = ""\n', encoding="utf-8")
    code, payload, _ = run_app("--dry-run", "system", "statuses")
    assert code == 0
    assert payload["dry_run"] is True


@pytest.mark.parametrize("value", ["json", "TABLE", " csv ", "yaml", "yml"])
def test_known_formats_are_accepted(value):
    result = run("-f", value, "--dry-run", "system", "statuses")
    assert result.exit_code == 0, result.output


def test_empty_env_format_counts_as_unset():
    result = run("--dry-run", "system", "statuses", env_extra={"HFOX_FORMAT": ""})
    assert result.exit_code == 0, result.output


# -- --timeout and --max-retries ----------------------------------------------
@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "-inf"])
def test_timeout_must_be_a_positive_finite_number(run_app, value):
    code, payload, _ = run_app("--timeout", value, "--dry-run", "system", "statuses")
    assert code == 3
    assert payload == {
        "error": "--timeout must be a positive number of seconds.",
        "type": "validation",
        "exit_code": 3,
    }


def test_env_timeout_must_be_a_number(run_app):
    code, payload, _ = run_app("--dry-run", "system", "statuses", HFOX_TIMEOUT="abc")
    assert code == 3
    assert payload["type"] == "validation"
    assert "HFOX_TIMEOUT" in payload["error"]


def test_env_timeout_must_be_positive(run_app):
    code, payload, _ = run_app("--dry-run", "system", "statuses", HFOX_TIMEOUT="0")
    assert (code, payload["type"]) == (3, "validation")


def test_max_retries_must_not_be_negative(run_app):
    code, payload, _ = run_app("--max-retries", "-1", "--dry-run", "system", "statuses")
    assert code == 3
    assert payload["type"] == "validation"
    assert "--max-retries" in payload["error"]


def test_env_max_retries_must_not_be_negative(run_app):
    code, payload, _ = run_app("--dry-run", "system", "statuses", HFOX_MAX_RETRIES="-1")
    assert code == 3
    assert payload["type"] == "validation"
    assert "HFOX_MAX_RETRIES" in payload["error"]


def test_timeout_and_retries_default(client_kwargs):
    assert run("system", "statuses").exit_code == 0
    assert client_kwargs[0]["timeout"] == DEFAULT_TIMEOUT == 30.0
    assert client_kwargs[0]["max_retries"] == MAX_RETRIES == 5
    assert callable(client_kwargs[0]["on_retry"])


def test_timeout_and_retries_from_env(client_kwargs):
    env = {"HFOX_TIMEOUT": "7.5", "HFOX_MAX_RETRIES": "2"}
    assert run("system", "statuses", env_extra=env).exit_code == 0
    assert (client_kwargs[0]["timeout"], client_kwargs[0]["max_retries"]) == (7.5, 2)


def test_flag_beats_env_for_timeout_and_retries(client_kwargs):
    env = {"HFOX_TIMEOUT": "7.5", "HFOX_MAX_RETRIES": "2"}
    result = run("--timeout", "3", "--max-retries", "0", "system", "statuses", env_extra=env)
    assert result.exit_code == 0, result.output
    assert (client_kwargs[0]["timeout"], client_kwargs[0]["max_retries"]) == (3.0, 0)


def test_empty_env_values_count_as_unset(client_kwargs):
    env = {"HFOX_TIMEOUT": "", "HFOX_MAX_RETRIES": ""}
    assert run("system", "statuses", env_extra=env).exit_code == 0
    assert (client_kwargs[0]["timeout"], client_kwargs[0]["max_retries"]) == (30.0, 5)


def test_max_retries_zero_sends_one_attempt(mock_api):
    captured = mock_api(lambda req: httpx.Response(429, json={}))
    result = run("--max-retries", "0", "system", "statuses")
    assert result.exception.to_dict()["type"] == "rate_limited"
    assert len(captured) == 1
    assert "Retrying" not in result.stderr


# -- retry notice -------------------------------------------------------------
def _flaky(failures):
    state = {"n": 0}

    def handler(request):
        state["n"] += 1
        if state["n"] <= failures:
            return httpx.Response(429, headers={"Retry-After": "2"}, json={})
        return httpx.Response(200, json=[{"id": 1}])

    return handler


def test_retry_notice_printed_once_per_retry_on_stderr(mock_api):
    captured = mock_api(_flaky(2))
    result = run("system", "statuses")
    assert result.exit_code == 0, result.output
    assert len(captured) == 3
    assert json.loads(result.stdout) == [{"id": 1}]
    assert result.stderr.splitlines() == [
        "Retrying in 2.0s (HTTP 429; retry 1 of 5).",
        "Retrying in 2.0s (HTTP 429; retry 2 of 5).",
    ]


def test_retry_notice_reports_the_configured_maximum(mock_api):
    mock_api(_flaky(1))
    result = run("--max-retries", "3", "system", "statuses")
    assert result.stderr.splitlines() == ["Retrying in 2.0s (HTTP 429; retry 1 of 3)."]


def test_retry_notice_names_a_network_error(mock_api):
    state = {"n": 0}

    def handler(request):
        state["n"] += 1
        if state["n"] == 1:
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(200, json=[])

    mock_api(handler)
    result = run("system", "statuses")
    assert result.exit_code == 0, result.output
    lines = result.stderr.splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("Retrying in ")
    assert lines[0].endswith("s (network error: ReadTimeout; retry 1 of 5).")


def test_quiet_suppresses_the_retry_notice(mock_api):
    captured = mock_api(_flaky(2))
    result = run("--quiet", "system", "statuses")
    assert result.exit_code == 0, result.output
    assert len(captured) == 3
    assert result.stderr == ""


# -- cleartext warning --------------------------------------------------------
WARNING = "Sending credentials in cleartext to http://gw.example.com; use https."


@pytest.mark.parametrize("quiet", [(), ("--quiet",)])
def test_cleartext_warning_for_a_remote_http_base_url(mock_api, quiet):
    captured = mock_api(lambda req: httpx.Response(200, json=[]))
    result = run(
        *quiet, "system", "statuses", env_extra={"HFOX_BASE_URL": "http://GW.example.com:8080"}
    )
    assert result.exit_code == 0, result.output
    assert result.stderr.count(WARNING) == 1
    assert str(captured[0].url).startswith("http://gw.example.com:8080/")


@pytest.mark.parametrize(
    "base_url",
    ["http://localhost:8080", "http://127.0.0.1:9", "http://[::1]:9", "https://gw.example.com"],
)
def test_no_cleartext_warning_for_loopback_or_https(mock_api, base_url):
    mock_api(lambda req: httpx.Response(200, json=[]))
    result = run("system", "statuses", env_extra={"HFOX_BASE_URL": base_url})
    assert result.exit_code == 0, result.output
    assert "cleartext" not in result.stderr


def test_cleartext_warning_printed_once_per_invocation(mock_api, capsys):
    mock_api(lambda req: httpx.Response(200, json=[]))
    ctx = AppContext(config=Config(subdomain="acme", api_key="k", auth_code="c"))
    ctx.make_client("http://gw.example.com/api/1.1/json", "k", "c").close()
    ctx.make_client("http://gw.example.com/api/1.1/json", "k", "c").close()
    assert capsys.readouterr().err.count(WARNING) == 1


def test_no_cleartext_warning_under_dry_run():
    result = run(
        "--dry-run", "system", "statuses", env_extra={"HFOX_BASE_URL": "http://gw.example.com"}
    )
    assert result.exit_code == 0, result.output
    assert "cleartext" not in result.stderr


def test_bad_parameter_through_cli_runner_is_a_click_error():
    # CliRunner bypasses app(); click reports the bad value itself.
    result = run("--timeout", "abc", "system", "statuses")
    assert result.exit_code == 2
    assert not isinstance(result.exception, ValidationError)
