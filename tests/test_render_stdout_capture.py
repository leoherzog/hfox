"""Regression: rendered (non-dry-run) command output must reach the *current*
sys.stdout so typer.testing.CliRunner can capture it.

output.render() previously bound ``stream=sys.stdout`` as a default argument,
which captured the stdout object that existed at import time. Under CliRunner
(which swaps sys.stdout per-invocation) that meant rendered data was written to
the real terminal and ``result.stdout`` came back empty. render() now resolves
sys.stdout at call time; this test fails if that regresses.
"""

import json

import httpx
from typer.testing import CliRunner

from hfox.cli.main import cli

runner = CliRunner()

ENV = {
    "HFOX_SUBDOMAIN": "acme",
    "HFOX_REGION": "us",
    "HFOX_API_KEY": "k",
    "HFOX_AUTH_CODE": "c",
    "HFOX_STAFF_ID": "1",
}


def test_rendered_get_is_captured_on_stdout(mock_api):
    ticket = {"id": 42, "subject": "Printer down", "display_id": "#DC00000042"}
    mock_api(lambda req: httpx.Response(200, json=ticket))

    result = runner.invoke(cli, ["tickets", "get", "42"], env=ENV)

    assert result.exit_code == 0, result.stdout
    # The default JSON render must land in captured stdout, not the real terminal.
    assert json.loads(result.stdout) == ticket
