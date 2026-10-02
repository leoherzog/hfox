"""`hfox tickets` list parameters and ticket-id validation under --dry-run."""

import json
import subprocess
import sys

import pytest
from conftest import subprocess_env
from typer.testing import CliRunner

from hfox.cli.main import cli
from hfox.core.errors import ValidationError

runner = CliRunner()

ENV = {
    "HFOX_SUBDOMAIN": "acme",
    "HFOX_REGION": "us",
    "HFOX_API_KEY": "k",
    "HFOX_AUTH_CODE": "c",
    "HFOX_STAFF_ID": "1",
}

_INVOKER = "import sys; sys.argv = ['hfox'] + sys.argv[1:]; from hfox.cli.main import app; app()"


def preview(*args):
    result = runner.invoke(cli, ["--dry-run", *args], env=ENV)
    assert result.exit_code == 0, result.stdout
    return json.loads(result.stdout)


def run_app(*args):
    """Run the real entry point so the exit-code mapping in main.app() applies."""
    return subprocess.run(
        [sys.executable, "-c", _INVOKER, "--dry-run", *args],
        env=subprocess_env(**ENV),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )


def test_list_repeats_category_key():
    p = preview("tickets", "list", "--category", "34", "--category", "5")
    assert p["method"] == "GET"
    assert "/tickets/?category=34&category=5&page=1&size=10" in p["url"]
    assert p["params"]["category"] == [34, 5]
    assert p["body"] is None


def test_list_query_is_sent_verbatim_with_status_all():
    p = preview("tickets", "list", "-q", 'status:"In Progress","New"')
    assert p["method"] == "GET"
    assert p["params"]["q"] == 'status:"In Progress","New"'
    assert p["params"]["status"] == "_all"
    assert "q=status%3A%22In+Progress%22%2C%22New%22" in p["url"]
    assert p["body"] is None


@pytest.mark.parametrize("query", ["", "   "])
def test_list_blank_query_is_omitted(query):
    p = preview("tickets", "list", "-q", query)
    assert p["method"] == "GET"
    assert p["url"].endswith("/tickets/?page=1&size=10")
    assert "q" not in p["params"] and "status" not in p["params"]
    assert p["body"] is None


def test_list_blank_fields_is_omitted():
    p = preview("tickets", "list", "--fields", " , ")
    assert p["method"] == "GET"
    assert "fields" not in p["params"]
    assert p["url"].endswith("/tickets/?page=1&size=10")


def test_list_size_50_is_allowed():
    p = preview("tickets", "list", "--size", "50")
    assert p["params"]["size"] == 50


@pytest.mark.parametrize(
    "flags",
    [
        ["--size", "100"],
        ["--size", "0"],
        ["--page", "0"],
        ["--category", "0"],
        ["--category", "Sales"],
    ],
)
def test_list_out_of_range_options_exit_3(flags):
    proc = run_app("tickets", "list", *flags)
    assert proc.returncode == 3
    assert json.loads(proc.stdout)["exit_code"] == 3


VERBS = [
    ["get"],
    ["reply", "--text", "x"],
    ["note", "--text", "x"],
    ["user-reply", "--user", "9", "--text", "x"],
    ["update", "--status", "3"],
    ["update-cf", "--cf", "1=x"],
    ["tags", "--add", "a"],
    ["subscribe"],
    ["unsubscribe"],
    ["forward", "--to", "a@x.org", "--subject", "s", "--message", "m"],
    ["move", "--to-category", "2"],
    ["delete", "--yes"],
]


@pytest.mark.parametrize("verb", VERBS, ids=[v[0] for v in VERBS])
@pytest.mark.parametrize("ticket_id", ["5/../../users", "#DC00000003", "DC00000003", " 42", ""])
def test_per_ticket_verbs_reject_non_numeric_id(verb, ticket_id):
    args = ["--dry-run", "tickets", verb[0], ticket_id, *verb[1:]]
    result = runner.invoke(cli, args, env=ENV)
    assert isinstance(result.exception, ValidationError), result.exception
    assert result.stdout == ""


def test_display_id_exits_3_through_entry_point():
    proc = run_app("tickets", "get", "#DC00000003")
    assert proc.returncode == 3
    assert "numeric ticket number" in json.loads(proc.stdout)["error"]
