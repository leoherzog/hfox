"""Rendered output reaches the current sys.stdout, even when its codec cannot encode the data."""

import io
import json
import sys

import httpx
from typer.testing import CliRunner

from hfox.cli import main
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
    assert json.loads(result.stdout) == ticket


def test_unencodable_output_is_escaped_not_a_crash(monkeypatch, tmp_path):
    raw_out, raw_err = io.BytesIO(), io.BytesIO()
    stdout = io.TextIOWrapper(raw_out, encoding="ascii")
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", io.TextIOWrapper(raw_err, encoding="ascii"))
    monkeypatch.setattr(sys, "argv", ["hfox", "--dry-run", "tickets", "list", "-q", "café—x"])
    for key, value in {**ENV, "HFOX_CONFIG_DIR": str(tmp_path)}.items():
        monkeypatch.setenv(key, value)

    main.app()

    stdout.flush()
    assert json.loads(raw_out.getvalue().decode("ascii"))["params"]["q"] == "café—x"
