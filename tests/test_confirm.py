"""Confirmation and prompt contract: AppContext.confirm, AppContext.prompt and app()."""

import io
import json
import subprocess
import sys

import httpx
import pytest
import typer
from conftest import subprocess_env
from typer.testing import CliRunner

import hfox.cli.main as main_mod
from hfox.cli.context import AppContext
from hfox.cli.main import cli
from hfox.core.config import Config
from hfox.core.errors import CancelledError, ExitCode, ValidationError

runner = CliRunner()

ENV = {
    "HFOX_SUBDOMAIN": "acme",
    "HFOX_REGION": "us",
    "HFOX_API_KEY": "k",
    "HFOX_AUTH_CODE": "c",
    "HFOX_STAFF_ID": "1",
}

_INVOKER = "import sys; sys.argv = ['hfox'] + sys.argv[1:]; from hfox.cli.main import app; app()"


def make_ctx(**kw):
    return AppContext(config=Config(subdomain="acme", api_key="k", auth_code="c"), **kw)


def feed(monkeypatch, text):
    monkeypatch.setattr(sys, "stdin", io.StringIO(text))


def no_prompt(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("prompted")

    monkeypatch.setattr(typer, "confirm", fail)
    monkeypatch.setattr(typer, "prompt", fail)


# -- AppContext.confirm -------------------------------------------------------
def test_confirm_dry_run_returns_without_prompting(monkeypatch, capsys):
    no_prompt(monkeypatch)
    make_ctx(dry_run=True).confirm("Delete ticket 5?", yes=False)
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("", "")


def test_confirm_yes_returns_without_prompting(monkeypatch, tty, capsys):
    no_prompt(monkeypatch)
    make_ctx().confirm("Delete ticket 5?", yes=True)
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("", "")


def test_confirm_without_terminal_names_yes(monkeypatch, capsys):
    no_prompt(monkeypatch)
    with pytest.raises(ValidationError) as exc:
        make_ctx().confirm("Delete ticket 5?", yes=False)
    assert str(exc.value) == (
        "Delete ticket 5? Confirmation is required: pass --yes, since stdin is not a terminal."
    )
    assert exc.value.hint == "Re-run with --yes."
    assert exc.value.to_dict()["type"] == "validation"
    assert capsys.readouterr().out == ""


def test_confirm_accepts_yes_and_prompts_on_stderr(monkeypatch, tty, capsys):
    feed(monkeypatch, "y\n")
    assert make_ctx().confirm("Delete ticket 5?", yes=False) is None
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Delete ticket 5?" in captured.err


@pytest.mark.parametrize("answer", ["n\n", "\n"])
def test_confirm_decline_raises_cancelled(monkeypatch, tty, capsys, answer):
    feed(monkeypatch, answer)
    with pytest.raises(CancelledError) as exc:
        make_ctx().confirm("Delete ticket 5?", yes=False)
    assert str(exc.value) == "Cancelled: confirmation declined."
    assert exc.value.exit_code is ExitCode.OTHER
    assert exc.value.to_dict() == {
        "error": "Cancelled: confirmation declined.",
        "type": "cancelled",
        "exit_code": 5,
    }
    assert capsys.readouterr().out == ""


def test_confirm_eof_raises_cancelled(monkeypatch, tty, capsys):
    feed(monkeypatch, "")
    with pytest.raises(CancelledError) as exc:
        make_ctx().confirm("Delete ticket 5?", yes=False)
    assert str(exc.value) == "Cancelled at the confirmation prompt."
    assert capsys.readouterr().out == ""


def test_confirm_ctrl_c_raises_cancelled(monkeypatch, tty):
    def interrupt(*args, **kwargs):
        raise typer.Abort()

    monkeypatch.setattr(typer, "confirm", interrupt)
    with pytest.raises(CancelledError, match="Cancelled at the confirmation prompt."):
        make_ctx().confirm("Delete ticket 5?", yes=False)


# -- AppContext.prompt --------------------------------------------------------
def test_prompt_returns_answer_and_labels_on_stderr(monkeypatch, capsys):
    feed(monkeypatch, "acme\n")
    assert make_ctx().prompt("HappyFox subdomain") == "acme"
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "HappyFox subdomain" in captured.err


def test_prompt_uses_default_on_empty_answer(monkeypatch, capsys):
    feed(monkeypatch, "\n")
    assert make_ctx().prompt("Data center", default="us") == "us"
    assert capsys.readouterr().out == ""


def test_prompt_eof_raises_cancelled(monkeypatch, capsys):
    feed(monkeypatch, "")
    with pytest.raises(CancelledError) as exc:
        make_ctx().prompt("API key")
    assert str(exc.value) == "Cancelled at the prompt."
    assert exc.value.to_dict()["type"] == "cancelled"
    assert capsys.readouterr().out == ""


# -- delete commands ----------------------------------------------------------
@pytest.mark.parametrize(
    "argv", [("tickets", "delete", "42"), ("assets", "delete", "10")]
)
def test_delete_without_terminal_needs_yes(mock_api, argv):
    captured = mock_api(lambda req: httpx.Response(200, json={"deleted": True}))
    result = runner.invoke(cli, list(argv), input="y\n", env=ENV)
    assert isinstance(result.exception, ValidationError)
    assert "--yes" in str(result.exception)
    assert result.stdout == ""
    assert captured == []


@pytest.mark.parametrize(
    "argv", [("tickets", "delete", "42"), ("assets", "delete", "10")]
)
def test_delete_decline_sends_nothing(mock_api, tty, argv):
    captured = mock_api(lambda req: httpx.Response(200, json={"deleted": True}))
    result = runner.invoke(cli, list(argv), input="n\n", env=ENV)
    assert isinstance(result.exception, CancelledError)
    assert result.stdout == ""
    assert captured == []


@pytest.mark.parametrize(
    ("argv", "path"),
    [
        (("tickets", "delete", "42"), "/ticket/42/delete/"),
        (("assets", "delete", "10"), "/asset/10/"),
    ],
)
def test_delete_confirmed_at_the_prompt(mock_api, tty, argv, path):
    captured = mock_api(lambda req: httpx.Response(200, json={"deleted": True}))
    result = runner.invoke(cli, list(argv), input="y\n", env=ENV)
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"deleted": True}
    assert [req.url.path.endswith(path) for req in captured] == [True]


@pytest.mark.parametrize(
    "argv", [("tickets", "delete", "42"), ("assets", "delete", "10")]
)
def test_delete_dry_run_never_prompts(monkeypatch, argv):
    no_prompt(monkeypatch)
    result = runner.invoke(cli, ["--dry-run", *argv], env=ENV)
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["dry_run"] is True
    assert "Delete" not in result.stderr


# -- app() --------------------------------------------------------------------
def test_app_decline_prints_cancelled_json_and_exits_5(monkeypatch, mock_api, tty, capsys):
    captured = mock_api(lambda req: httpx.Response(200, json={"deleted": True}))
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(sys, "argv", ["hfox", "tickets", "delete", "42"])
    feed(monkeypatch, "n\n")
    with pytest.raises(SystemExit) as exc:
        main_mod.app()
    assert exc.value.code == 5
    out = capsys.readouterr().out
    assert json.loads(out) == {
        "error": "Cancelled: confirmation declined.",
        "type": "cancelled",
        "exit_code": 5,
    }
    assert captured == []


def test_app_non_terminal_delete_exits_3_naming_yes():
    proc = subprocess.run(
        [sys.executable, "-c", _INVOKER, "tickets", "delete", "5"],
        env=subprocess_env(**ENV),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 3
    payload = json.loads(proc.stdout)
    assert (payload["type"], payload["exit_code"]) == ("validation", 3)
    assert "--yes" in payload["error"]
    assert "--yes" in payload["hint"]


def test_app_dry_run_delete_with_empty_stdin_previews_without_prompt():
    proc = subprocess.run(
        [sys.executable, "-c", _INVOKER, "--dry-run", "tickets", "delete", "5"],
        env=subprocess_env(**ENV),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout
    payload = json.loads(proc.stdout)
    assert payload["method"] == "POST"
    assert payload["url"].endswith("/ticket/5/delete/")
    assert payload["body"] == {"staff_id": 1}
    assert "Delete ticket" not in proc.stderr
