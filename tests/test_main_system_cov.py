"""Coverage for hfox.cli.main (root callback, global flags, version, app() wrapper)
and hfox.cli.system (read-only reference-data commands).

main.py targets: 44-45 (_version_callback), 85 (--no-color), 105-120 (app() error
handling branches), 124 (__main__).
system.py targets: every command (categories/staff/statuses/ticket-cf/contact-cf).
"""

import json
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
# main.py :: --version (_version_callback, lines 44-45)
# ---------------------------------------------------------------------------
def test_version_flag_prints_version_and_exits_zero():
    from hfox import __version__

    result = run("--version")
    assert result.exit_code == 0
    assert result.stdout.strip() == f"hfox {__version__}"


# ---------------------------------------------------------------------------
# main.py :: --no-color sets NO_COLOR env (line 85)
# ---------------------------------------------------------------------------
def test_no_color_flag_sets_env(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    # --dry-run short-circuits before any network; the callback still runs and
    # must set NO_COLOR=1 from the --no-color flag (line 85).
    result = run("--no-color", "--dry-run", "tickets", "reply", "9", "--text", "hi")
    assert result.exit_code == 0
    import os

    assert os.environ.get("NO_COLOR") == "1"


# ---------------------------------------------------------------------------
# main.py :: --format global flag resolves OutputFormat
# ---------------------------------------------------------------------------
def test_format_flag_resolves(mock_api):
    captured = mock_api(lambda req: httpx.Response(200, json=[{"id": 1, "name": "Sales"}]))
    result = run("-f", "json", "system", "categories")
    assert result.exit_code == 0
    assert captured[0].method == "GET"


# ---------------------------------------------------------------------------
# main.py :: app() entry-point error handling (lines 105-120)
#
# CliRunner invokes the bare `cli`; the structured wrapper lives in app(). We
# drive app() directly, swapping the constructed command for a stub that raises
# each exception type, to cover every branch.
# ---------------------------------------------------------------------------
def _install_stub_command(monkeypatch, exc):
    def fake_command(args=None, standalone_mode=True):
        raise exc

    monkeypatch.setattr(main_mod.typer.main, "get_command", lambda _cli: fake_command)


def test_app_exit_branch(monkeypatch):
    # typer.Exit / --help / --version path -> SystemExit(exit_code) (108-109)
    _install_stub_command(monkeypatch, typer.Exit(0))
    with pytest.raises(SystemExit) as ei:
        app()
    assert ei.value.code == 0


def test_app_exit_branch_nonzero(monkeypatch):
    _install_stub_command(monkeypatch, typer.Exit(7))
    with pytest.raises(SystemExit) as ei:
        app()
    assert ei.value.code == 7


def test_app_abort_branch(monkeypatch, capsys):
    # click Abort -> warn("Aborted.") + SystemExit(1) (110-112)
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
    # HfoxError -> JSON on stdout + SystemExit(exit_code) (116-118)
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
    # KeyboardInterrupt -> SystemExit(130) (119-120)
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
# main.py :: __main__ guard (line 124) — exercise via subprocess running app()
# ---------------------------------------------------------------------------
def test_module_run_as_script_version():
    proc = subprocess.run(
        [sys.executable, "-m", "hfox.cli.main", "--version"],
        env=ENV,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0
    assert proc.stdout.strip().startswith("hfox ")


# ---------------------------------------------------------------------------
# system.py :: each read-only reference-data command
# Endpoints return bare JSON arrays; commands render the list as-is.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "command, path, sample",
    [
        ("categories", "/categories/", [{"id": 1, "name": "Sales"}]),
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
