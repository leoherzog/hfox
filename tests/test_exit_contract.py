"""Exit codes through app() in a subprocess, output rendering, color and staff-id flow."""

import contextlib
import io
import json
import os
import subprocess
import sys

import pytest
from conftest import subprocess_env
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
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-c", _INVOKER, *argv],
        env=subprocess_env(**env),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )


def run_app_into(argv, *, stdout=subprocess.PIPE, stderr=subprocess.PIPE, base_env=ENV):
    """Run `app()` in a subprocess with the given stdout and stderr; return CompletedProcess."""
    return subprocess.run(
        [sys.executable, "-c", _INVOKER, *argv],
        env=subprocess_env(**base_env),
        stdin=subprocess.DEVNULL,
        stdout=stdout,
        stderr=stderr,
        text=True,
    )


@contextlib.contextmanager
def closed_pipe():
    """Yield the write end of a pipe whose read end is already closed."""
    read_end, write_end = os.pipe()
    os.close(read_end)
    try:
        yield write_end
    finally:
        os.close(write_end)


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
        ["--dry-run", "tickets", "subscribe", "42", "--staff-ids", "3"],
        base_env=env,
    )
    assert proc.returncode == 3
    payload = json.loads(proc.stdout)
    assert payload["exit_code"] == 3
    assert payload["type"] == "validation"
    assert "staff identity" in payload["error"]


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
    mock_api(lambda req: __import__("httpx2").Response(200, json={"display_id": "#D1"}))
    result = runner.invoke(
        cli,
        ["tickets", "create", "--subject", "S", "--category", "3",
         "--name", "H", "--email", "h@x.org", "--text", "b"],
        env=ENV,
    )
    assert result.exit_code == 0
    assert "Created ticket #D1." in result.stderr
    assert "Created ticket" not in result.stdout


def test_quiet_suppresses_success_message(mock_api):
    mock_api(lambda req: __import__("httpx2").Response(200, json={"display_id": "#D1"}))
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
    assert payload["type"] == "usage"
    assert "bogus" in payload["error"]
    # The human usage hint stays on stderr.
    assert "Usage" in proc.stderr


def test_unknown_command_exit_code_exactly_3():
    proc = run_app(["nonexistent-resource", "list"])
    assert proc.returncode == 3
    payload = json.loads(proc.stdout)
    assert (payload["exit_code"], payload["type"]) == (3, "usage")


# -- bare group invocations show help and exit 0, like --help ---------------
def test_bare_group_shows_help_and_exits_0():
    proc = run_app(["tickets"])
    assert proc.returncode == 0
    assert "Usage" in proc.stdout


def test_bare_root_shows_help_and_exits_0():
    proc = run_app([])
    assert proc.returncode == 0
    assert "Usage" in proc.stdout


# -- a failed stdout write exits 5 without a traceback -----------------------
_NO_CREDENTIALS = {"HFOX_SUBDOMAIN": "acme"}

# Every way hfox writes stdout: JSON, NDJSON, error objects, a table, help and the version.
STDOUT_PATHS = [
    pytest.param(["--dry-run", "tickets", "list"], ENV, id="json"),
    pytest.param(["--dry-run", "--page-all", "tickets", "list"], ENV, id="ndjson"),
    pytest.param(["tickets", "get", "0"], ENV, id="error"),
    pytest.param(["--page-all", "tickets", "get", "0"], ENV, id="ndjson-error"),
    pytest.param(["-f", "table", "auth", "status"], _NO_CREDENTIALS, id="table"),
    pytest.param(["--help"], ENV, id="help"),
    pytest.param(["tickets"], ENV, id="bare-group"),
    pytest.param(["--version"], ENV, id="version"),
]


def _no_traceback(stderr):
    return "Traceback" not in stderr and "Exception ignored" not in stderr


@pytest.mark.parametrize(("argv", "env"), STDOUT_PATHS)
def test_closed_stdout_exits_5_without_a_traceback(argv, env):
    with closed_pipe() as write_end:
        proc = run_app_into(argv, stdout=write_end, base_env=env)
    assert proc.returncode == 5
    assert _no_traceback(proc.stderr)
    assert len(proc.stderr.splitlines()) <= 1


@pytest.mark.skipif(sys.platform == "win32", reason="Windows reports a closed pipe as EINVAL")
@pytest.mark.parametrize(("argv", "env"), STDOUT_PATHS[:2] + STDOUT_PATHS[5:6])
def test_closed_stdout_writes_nothing_to_stderr(argv, env):
    with closed_pipe() as write_end:
        proc = run_app_into(argv, stdout=write_end, base_env=env)
    assert proc.returncode == 5
    assert proc.stderr == ""


@pytest.mark.skipif(not os.path.exists("/dev/full"), reason="needs /dev/full")
@pytest.mark.parametrize(("argv", "env"), STDOUT_PATHS)
def test_full_stdout_exits_5_with_one_stderr_line(argv, env):
    with open("/dev/full", "w") as full:
        proc = run_app_into(argv, stdout=full, base_env=env)
    assert proc.returncode == 5
    assert proc.stderr == "error: cannot write output: No space left on device.\n"


def test_closed_stderr_exits_5():
    # The usage text of an unknown flag goes to stderr before the error object.
    with closed_pipe() as write_end:
        proc = run_app_into(["tickets", "list", "--bogus"], stderr=write_end)
    assert proc.returncode == 5
