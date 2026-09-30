"""Coverage for hfox.cli.main (root callback, global flags, version, app() wrapper)
and hfox.cli.system (read-only reference-data commands).
"""

import json
import os
import subprocess
import sys

import httpx
import pytest
import typer
from typer._click import exceptions as click_exceptions
from typer.testing import CliRunner

import hfox.cli.main as main_mod
from hfox.cli.main import app, cli
from hfox.core.errors import APIError, AuthError, HfoxError, NotFoundError

runner = CliRunner()

ENV = {
    "HFOX_SUBDOMAIN": "acme",
    "HFOX_REGION": "us",
    "HFOX_API_KEY": "k",
    "HFOX_AUTH_CODE": "c",
    "HFOX_STAFF_ID": "1",
}


def run(*args, env_extra=None):
    env = dict(ENV)
    if env_extra:
        env.update(env_extra)
    return runner.invoke(cli, list(args), env=env)


# ---------------------------------------------------------------------------
# main.py :: --version (_version_callback)
# ---------------------------------------------------------------------------
def test_version_flag_prints_version_and_exits_zero():
    from hfox import __version__

    result = run("--version")
    assert result.exit_code == 0
    assert result.stdout.strip() == f"hfox {__version__}"


# ---------------------------------------------------------------------------
# main.py :: --no-color sets NO_COLOR env
# ---------------------------------------------------------------------------
def test_no_color_flag_sets_env(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    result = run("--no-color", "--dry-run", "tickets", "reply", "9", "--text", "hi")
    assert result.exit_code == 0
    assert os.environ.get("NO_COLOR") == "1"


def test_any_no_color_value_is_accepted():
    result = run("--dry-run", "system", "statuses", env_extra={"NO_COLOR": "yes-please"})
    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# main.py :: --format global flag resolves OutputFormat
# ---------------------------------------------------------------------------
def test_format_flag_resolves(mock_api):
    captured = mock_api(lambda req: httpx.Response(200, json=[{"id": 1, "name": "Sales"}]))
    result = run("-f", "json", "system", "categories")
    assert result.exit_code == 0
    assert captured[0].method == "GET"


# ---------------------------------------------------------------------------
# main.py :: app() entry-point error handling
#
# CliRunner invokes the bare `cli`; the structured wrapper lives in app(). We
# drive app() directly, swapping the constructed command for a stub that raises
# each exception type, to cover every branch.
# ---------------------------------------------------------------------------
def _install_stub_command(monkeypatch, exc):
    def fake_command(args=None, standalone_mode=True):
        raise exc

    monkeypatch.setattr(main_mod.typer.main, "get_command", lambda _cli: fake_command)


def test_app_returns_exit_code(monkeypatch):
    def fake_command(args=None, standalone_mode=True):
        return 7

    monkeypatch.setattr(main_mod.typer.main, "get_command", lambda _cli: fake_command)
    with pytest.raises(SystemExit) as ei:
        app()
    assert ei.value.code == 7


def test_app_abort_branch(monkeypatch, capsys):
    # click Abort -> warn("Aborted.") + SystemExit(1)
    _install_stub_command(monkeypatch, typer.Abort())
    with pytest.raises(SystemExit) as ei:
        app()
    assert ei.value.code == 1
    assert "Aborted." in capsys.readouterr().err


def test_app_no_args_help_branch(monkeypatch):
    # NoArgsIsHelpError (bare group like `hfox tickets`) -> help + SystemExit(0),
    # matching --help; click's default 2 would collide with ExitCode.AUTH.
    import typer._click as click

    command = main_mod.typer.main.get_command(cli)
    exc = click_exceptions.NoArgsIsHelpError(click.Context(command))
    _install_stub_command(monkeypatch, exc)
    with pytest.raises(SystemExit) as ei:
        app()
    assert ei.value.code == 0


def test_app_usage_error_branch(monkeypatch, capsys):
    # UsageError -> hint on stderr + JSON validation error on stdout + exit 3.
    exc = click_exceptions.UsageError("bad usage")
    _install_stub_command(monkeypatch, exc)
    with pytest.raises(SystemExit) as ei:
        app()
    assert ei.value.code == 3
    out, err = capsys.readouterr()
    assert json.loads(out) == {"error": "bad usage", "exit_code": 3}
    assert "bad usage" in err


def test_app_click_exception_branch(monkeypatch, capsys):
    # Other ClickException -> exc.show() + SystemExit(exc.exit_code)
    exc = click_exceptions.ClickException("boom")
    _install_stub_command(monkeypatch, exc)
    with pytest.raises(SystemExit) as ei:
        app()
    assert ei.value.code == exc.exit_code
    assert "boom" in capsys.readouterr().err


def test_app_hfox_error_branch_emits_json(monkeypatch, capsys):
    # HfoxError -> JSON on stdout + SystemExit(exit_code)
    _install_stub_command(monkeypatch, AuthError("no creds"))
    with pytest.raises(SystemExit) as ei:
        app()
    assert ei.value.code == 2  # ExitCode.AUTH
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["error"] == "no creds"
    assert payload["exit_code"] == 2


def test_app_hfox_error_not_found_exit_4(monkeypatch, capsys):
    _install_stub_command(monkeypatch, NotFoundError("gone"))
    with pytest.raises(SystemExit) as ei:
        app()
    assert ei.value.code == 4
    assert json.loads(capsys.readouterr().out)["exit_code"] == 4


def test_app_hfox_error_api_includes_status_code(monkeypatch, capsys):
    _install_stub_command(monkeypatch, APIError("boom", status_code=503))
    with pytest.raises(SystemExit) as ei:
        app()
    assert ei.value.code == 1  # ExitCode.API
    payload = json.loads(capsys.readouterr().out)
    assert payload["status_code"] == 503
    assert payload["exit_code"] == 1


def test_app_base_hfox_error_exit_5(monkeypatch, capsys):
    _install_stub_command(monkeypatch, HfoxError("weird"))
    with pytest.raises(SystemExit) as ei:
        app()
    assert ei.value.code == 5  # ExitCode.OTHER default
    assert json.loads(capsys.readouterr().out)["exit_code"] == 5


def test_app_keyboard_interrupt_branch(monkeypatch):
    # KeyboardInterrupt -> SystemExit(130)
    _install_stub_command(monkeypatch, KeyboardInterrupt())
    with pytest.raises(SystemExit) as ei:
        app()
    assert ei.value.code == 130


def test_app_success_path(monkeypatch):
    # A command that returns cleanly: no exception, app() returns None.
    def fake_command(args=None, standalone_mode=True):
        return None

    monkeypatch.setattr(main_mod.typer.main, "get_command", lambda _cli: fake_command)
    assert app() is None


# ---------------------------------------------------------------------------
# main.py :: __main__ guard, exercised via a subprocess running app()
# ---------------------------------------------------------------------------
def test_module_run_as_script_version():
    proc = subprocess.run(
        [sys.executable, "-m", "hfox.cli.main", "--version"],
        env={**ENV, "HFOX_CONFIG_DIR": os.environ["HFOX_CONFIG_DIR"]},
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0
    assert proc.stdout.strip().startswith("hfox ")


# ---------------------------------------------------------------------------
# system.py :: each read-only reference-data command
# Endpoints return bare JSON arrays.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "command, path, sample",
    [
        ("categories", "/categories/", [{"id": 1, "name": "Sales"}]),
        ("priorities", "/priorities/", [{"id": 6, "name": "High"}]),
        ("staff", "/staff/", [{"id": 2, "name": "Alice"}]),
        ("statuses", "/statuses/", [{"id": 3, "name": "Open"}]),
        ("ticket-custom-fields", "/ticket_custom_fields/", [{"id": 4, "name": "Priority"}]),
        ("contact-custom-fields", "/user_custom_fields/", [{"id": 5, "name": "Company"}]),
    ],
)
def test_system_command_hits_endpoint_and_renders(mock_api, command, path, sample):
    captured = mock_api(lambda req: httpx.Response(200, json=sample))
    result = run("system", command)
    assert result.exit_code == 0
    assert len(captured) == 1
    assert captured[0].method == "GET"
    assert captured[0].url.path.endswith(path)
    # JSON format keeps the bare array intact.
    assert json.loads(result.stdout) == sample


def test_system_categories_empty_array(mock_api):
    captured = mock_api(lambda req: httpx.Response(200, json=[]))
    result = run("system", "categories")
    assert result.exit_code == 0
    assert json.loads(result.stdout) == []
    assert captured[0].url.path.endswith("/categories/")


def test_system_staff_table_format_unwraps_rows(mock_api):
    mock_api(lambda req: httpx.Response(200, json=[{"id": 2, "name": "Alice"}]))
    result = run("-f", "table", "system", "staff")
    assert result.exit_code == 0
    assert "Alice" in result.stdout
    assert "name" in result.stdout


def test_system_dry_run_short_circuits(mock_api):
    # --dry-run on a read command emits the request preview and never hits the
    # (mock) transport.
    captured = mock_api(lambda req: httpx.Response(200, json=[]))
    result = run("--dry-run", "system", "statuses")
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["dry_run"] is True
    assert payload["method"] == "GET"
    assert payload["url"].endswith("/statuses/")
    assert captured == []


def test_system_priorities_dry_run():
    result = run("--dry-run", "system", "priorities")
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert (payload["method"], payload["body"]) == ("GET", None)
    assert payload["url"].endswith("/api/1.1/json/priorities/")


_FIELDS = [
    {
        "id": 61,
        "name": "Request Survey",
        "type": "choice",
        "choices": [
            {"text": "No", "id": 2, "dependant_fields": []},
            {"text": "Yes", "id": 1, "dependant_fields": [{"id": 9}]},
        ],
    },
    {"id": 4, "name": "Account Number", "type": "text", "choices": None},
]


@pytest.mark.parametrize("command", ["ticket-custom-fields", "contact-custom-fields"])
def test_custom_field_choices_flattened_in_csv(mock_api, command):
    mock_api(lambda req: httpx.Response(200, json=_FIELDS))
    result = run("-f", "csv", "system", command)
    assert result.exit_code == 0
    lines = result.stdout.splitlines()
    assert lines[0] == "id,name,type,choices"
    assert lines[1] == '61,Request Survey,choice,"No=2, Yes=1"'
    assert lines[2] == "4,Account Number,text,"


def test_custom_field_choices_flattened_in_table(mock_api):
    many = [{"text": f"Option {i}", "id": 100 + i} for i in range(10)]
    mock_api(lambda req: httpx.Response(200, json=[{"id": 5, "choices": many}]))
    result = run(
        "-f", "table", "system", "ticket-custom-fields", env_extra={"COLUMNS": "400"}
    )
    assert result.exit_code == 0
    assert "Option 9=109" in result.stdout
    assert "dependant" not in result.stdout


def test_custom_field_choices_kept_nested_in_json(mock_api):
    mock_api(lambda req: httpx.Response(200, json=_FIELDS))
    result = run("system", "contact-custom-fields")
    assert json.loads(result.stdout) == _FIELDS
