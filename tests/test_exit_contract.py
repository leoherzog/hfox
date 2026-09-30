"""Exit codes through app() in a subprocess, output rendering, color and staff-id flow."""

import io
import json
import os
import subprocess
import sys

from typer.testing import CliRunner

from hfox.cli import output
from hfox.cli.main import cli
from hfox.cli.output import OutputFormat

runner = CliRunner()

ENV = {
    "HFOX_SUBDOMAIN": "acme",
    "HFOX_REGION": "us",
    "HFOX_API_KEY": "k",
    "HFOX_AUTH_CODE": "c",
    "HFOX_STAFF_ID": "1",
}

_INVOKER = (
    "import sys; sys.argv = ['hfox'] + sys.argv[1:]; "
    "from hfox.cli.main import app; app()"
)


def run_app(argv, env_extra=None, base_env=None):
    """Run the real `app()` entry point in a subprocess; return CompletedProcess."""
    env = dict(base_env if base_env is not None else ENV)
    env["HFOX_CONFIG_DIR"] = os.environ["HFOX_CONFIG_DIR"]
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-c", _INVOKER, *argv],
        env=env,
        capture_output=True,
        text=True,
    )


# -- exit code 3 + JSON error object on stdout ------------------------------
def test_validation_failure_exit_code_exactly_3_and_json_on_stdout():
    proc = run_app(
        ["--dry-run", "tickets", "create", "--subject", "x", "--category", "1",
         "--name", "n", "--email", "e@x.org"],  # no body -> ValidationError
    )
    assert proc.returncode == 3
    payload = json.loads(proc.stdout)
    assert payload["exit_code"] == 3
    assert "error" in payload
    # The error object goes to stdout; stderr carries no JSON payload.
    assert "exit_code" not in proc.stderr


def test_local_filter_without_page_all_exits_3():
    proc = run_app(["--dry-run", "assets", "list", "--name", "x"])
    assert proc.returncode == 3
    payload = json.loads(proc.stdout)
    assert payload["exit_code"] == 3
    hint = "global --page-all before the resource (hfox --page-all <resource> ...)"
    assert hint in payload["error"]


def test_missing_required_staff_id_exits_3():
    env = {k: v for k, v in ENV.items() if k != "HFOX_STAFF_ID"}
    proc = run_app(
        ["--dry-run", "tickets", "subscribe", "42", "--agents", "3"],
        base_env=env,
    )
    assert proc.returncode == 3
    payload = json.loads(proc.stdout)
    assert payload["exit_code"] == 3
    assert "staff id" in payload["error"].lower()


# -- --staff-id override flows into a write body ----------------------------
def test_staff_id_override_flows_into_write_body():
    # --staff-id 99 must win over the configured HFOX_STAFF_ID=1 default.
    result = runner.invoke(
        cli,
        ["--dry-run", "--staff-id", "99", "tickets", "reply", "42", "--text", "ok"],
        env=ENV,
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["body"]["staff"] == 99


# -- output rendering: table / csv / yaml / json ----------------------------
def test_render_json():
    buf = io.StringIO()
    output.render({"a": 1}, OutputFormat.JSON, stream=buf)
    assert json.loads(buf.getvalue()) == {"a": 1}


def test_render_table_lists_columns():
    buf = io.StringIO()
    output.render([{"id": 1, "name": "a"}], OutputFormat.TABLE, stream=buf)
    out = buf.getvalue()
    assert "id" in out
    assert "name" in out


def test_render_csv_has_header_and_row():
    buf = io.StringIO()
    output.render([{"id": 1, "name": "a"}], OutputFormat.CSV, stream=buf)
    lines = buf.getvalue().splitlines()
    assert lines[0] == "id,name"
    assert lines[1] == "1,a"


def test_render_yaml_scalars():
    buf = io.StringIO()
    output.render({"a": 1, "b": "x"}, OutputFormat.YAML, stream=buf)
    assert buf.getvalue() == "a: 1\nb: x\n"


# -- NO_COLOR routing -------------------------------------------------------
class _FakeTTY:
    def isatty(self) -> bool:
        return True


def test_no_color_env_disables_color(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    assert output._color_enabled(_FakeTTY()) is False


def test_color_enabled_on_tty_without_no_color(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    assert output._color_enabled(_FakeTTY()) is True


def test_color_disabled_on_non_tty(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)

    class _NotTTY:
        def isatty(self) -> bool:
            return False

    assert output._color_enabled(_NotTTY()) is False


# -- status/success goes to stderr, not stdout ------------------------------
def test_success_message_goes_to_stderr(mock_api):
    mock_api(lambda req: __import__("httpx").Response(200, json={"display_id": "#D1"}))
    result = runner.invoke(
        cli,
        ["tickets", "create", "--subject", "S", "--category", "3",
         "--name", "H", "--email", "h@x.org", "--text", "b"],
        env=ENV,
    )
    assert result.exit_code == 0
    # The "Created ticket ..." status line is a stderr-only side channel.
    assert "Created ticket #D1." in result.stderr
    assert "Created ticket" not in result.stdout


def test_quiet_suppresses_success_message(mock_api):
    mock_api(lambda req: __import__("httpx").Response(200, json={"display_id": "#D1"}))
    result = runner.invoke(
        cli,
        ["--quiet", "tickets", "create", "--subject", "S", "--category", "3",
         "--name", "H", "--email", "h@x.org", "--text", "b"],
        env=ENV,
    )
    assert result.exit_code == 0
    assert "Created ticket" not in result.stderr


# -- usage errors map to ExitCode.VALIDATION, not click's 2 (= auth) --------
def test_unknown_flag_exit_code_exactly_3_and_json_on_stdout():
    proc = run_app(["tickets", "list", "--bogus"])
    assert proc.returncode == 3
    payload = json.loads(proc.stdout)
    assert payload["exit_code"] == 3
    assert "bogus" in payload["error"]
    # The human usage hint stays on stderr.
    assert "Usage" in proc.stderr


def test_unknown_command_exit_code_exactly_3():
    proc = run_app(["nonexistent-resource", "list"])
    assert proc.returncode == 3
    assert json.loads(proc.stdout)["exit_code"] == 3


# -- bare group invocations show help and exit 0, like --help ---------------
def test_bare_group_shows_help_and_exits_0():
    proc = run_app(["tickets"])
    assert proc.returncode == 0
    assert "Usage" in proc.stdout


def test_bare_root_shows_help_and_exits_0():
    proc = run_app([])
    assert proc.returncode == 0
    assert "Usage" in proc.stdout
