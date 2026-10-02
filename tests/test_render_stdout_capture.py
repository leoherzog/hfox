"""Rendered output reaches the current sys.stdout, even when its codec cannot encode the data,
and every JSON document hfox prints is strict JSON.
"""

import io
import json
import sys

import httpx
import pytest
from typer.testing import CliRunner

from hfox.cli import main
from hfox.cli.context import emit_dry_run
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


# --- strict JSON ----------------------------------------------------------------


def _strict_loads(text: str):
    """Parse JSON, failing on the bare NaN, Infinity and -Infinity that json.loads accepts."""

    def refuse(name: str):
        raise AssertionError(f"{name} is not JSON")

    return json.loads(text, parse_constant=refuse)


def _raw(status: int, content: bytes) -> httpx.Response:
    return httpx.Response(status, content=content, headers={"content-type": "application/json"})


# 1e999 is valid JSON that Python reads as infinity.
NON_FINITE_ROW = b'{"id": 1, "score": NaN, "high": Infinity, "low": [-Infinity, 1e999], "ok": 1.5}'
NULLED_ROW = {"id": 1, "score": None, "high": None, "low": [None, None], "ok": 1.5}
NON_FINITE_ERROR = b'{"error": {"limit": Infinity, "seen": [NaN, -1e999]}}'
NULLED_DETAIL = {"limit": None, "seen": [None, None]}


def test_non_finite_numbers_in_a_response_render_as_null(mock_api):
    mock_api(lambda req: _raw(200, NON_FINITE_ROW))

    result = runner.invoke(cli, ["tickets", "get", "1"], env=ENV)

    assert result.exit_code == 0, result.stdout
    assert _strict_loads(result.stdout) == NULLED_ROW


def test_non_finite_numbers_in_an_ndjson_page_render_as_null(mock_api):
    page = b'{"page_info": {"page_count": 1}, "data": [' + NON_FINITE_ROW + b"]}"
    mock_api(lambda req: _raw(200, page))

    result = runner.invoke(cli, ["--page-all", "tickets", "list"], env=ENV)

    assert result.exit_code == 0, result.stdout
    lines = result.stdout.splitlines()
    assert [_strict_loads(line) for line in lines] == [
        {"page_info": {"page_count": 1}, "data": [NULLED_ROW]}
    ]


def test_non_finite_numbers_in_an_ndjson_error_line_render_as_null(mock_api):
    mock_api(lambda req: _raw(400, NON_FINITE_ERROR))

    result = runner.invoke(cli, ["--page-all", "tickets", "list"], env=ENV)

    assert result.exit_code == 1
    lines = result.stdout.splitlines()
    assert len(lines) == 1
    assert _strict_loads(lines[0])["detail"] == NULLED_DETAIL


def test_non_finite_numbers_in_the_error_object_render_as_null(mock_api, monkeypatch, capsys):
    mock_api(lambda req: _raw(400, NON_FINITE_ERROR))
    monkeypatch.setattr(sys, "argv", ["hfox", "tickets", "get", "1"])
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)

    with pytest.raises(SystemExit) as exc:
        main.app()

    assert exc.value.code == 1
    error = _strict_loads(capsys.readouterr().out)
    assert error["type"] == "api"
    assert error["detail"] == NULLED_DETAIL


def test_non_finite_numbers_in_a_dry_run_preview_render_as_null(capsys):
    body = {"t-cf-1": float("nan"), "t-cf-2": [float("inf"), float("-inf")]}

    emit_dry_run("https://acme.happyfox.com/api/1.1/json", "POST", "tickets/", json=body)

    preview = _strict_loads(capsys.readouterr().out)
    assert preview["body"] == {"t-cf-1": None, "t-cf-2": [None, None]}
