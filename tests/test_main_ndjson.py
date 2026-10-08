"""NDJSON framing: under --page-all with JSON output every JSON document on stdout is one
compact line, whether a page, an unpaged listing, an object, a dry-run preview or an error.
"""

import io
import json
import sys

import httpx2
import pytest
from typer.testing import CliRunner

import hfox.cli.main as main_mod
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

CATEGORIES = [{"id": 1, "name": "Support"}, {"id": 2, "name": "Sales"}]
TICKET = {"id": 1, "subject": "Printer", "tags": ["a", "b"]}


@pytest.fixture
def run_app(monkeypatch, capsys):
    """Run the real app() in process; return (exit code, stdout, stderr)."""

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
        return code, captured.out, captured.err

    return invoke


@pytest.fixture
def api(mock_api):
    """Serve categories/ and ticket/1/, and 404 for anything else."""

    def handler(request):
        path = request.url.path.removeprefix("/api/1.1/json/")
        if path == "categories/":
            return httpx2.Response(200, json=CATEGORIES)
        if path == "ticket/1/":
            return httpx2.Response(200, json=TICKET)
        return httpx2.Response(404, json={"error": "Not found"})

    return mock_api(handler)


@pytest.fixture
def no_requests(mock_api):
    def handler(request):
        pytest.fail(f"request sent: {request.method} {request.url}")

    return mock_api(handler)


def one_line(out):
    """Return the single JSON document `out` holds as one compact line."""
    payload = json.loads(out)
    assert out == json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
    return payload


def pretty(out):
    """Return the JSON document `out` holds as a two-space indented document."""
    assert out.startswith(("{\n  ", "[\n  ")), out
    return json.loads(out)


# -- success output -----------------------------------------------------------
def test_unpaged_listing_is_one_line(run_app, api):
    code, out, _ = run_app("--page-all", "system", "categories")
    assert code == 0
    assert one_line(out) == CATEGORIES


def test_single_object_is_one_line(run_app, api):
    code, out, _ = run_app("--page-all", "tickets", "get", "1")
    assert code == 0
    assert one_line(out) == TICKET


def test_filtered_unpaged_listing_is_one_line(run_app, api):
    code, out, _ = run_app("--page-all", "system", "categories", "--name", "sales")
    assert code == 0
    assert one_line(out) == [CATEGORIES[1]]


def test_explicit_json_format_is_one_line(run_app, api):
    code, out, _ = run_app("-f", "json", "--page-all", "tickets", "get", "1")
    assert code == 0
    assert one_line(out) == TICKET


def test_without_page_all_json_stays_indented(run_app, api):
    code, out, _ = run_app("tickets", "get", "1")
    assert code == 0
    assert pretty(out) == TICKET


# -- dry-run previews ---------------------------------------------------------
@pytest.mark.parametrize(
    ("argv", "method", "path"),
    [
        (("tickets", "reply", "5", "--text", "hi"), "POST", "ticket/5/staff_update/"),
        (("tickets", "get", "5"), "GET", "ticket/5/"),
        (("tickets", "list"), "GET", "tickets/"),
        (("system", "categories"), "GET", "categories/"),
    ],
    ids=["reply", "get", "list", "categories"],
)
def test_dry_run_preview_is_one_line(run_app, no_requests, argv, method, path):
    code, out, _ = run_app("--page-all", "--dry-run", *argv)
    assert code == 0
    preview = one_line(out)
    assert preview["dry_run"] is True
    assert preview["method"] == method
    assert preview["url"].split("?")[0].endswith("/api/1.1/json/" + path)
    assert no_requests == []


def test_logout_dry_run_is_one_line(run_app):
    code, out, _ = run_app("--page-all", "--dry-run", "auth", "logout")
    assert code == 0
    assert one_line(out)["dry_run"] is True


def test_dry_run_preview_without_page_all_stays_indented(run_app, no_requests):
    code, out, _ = run_app("--dry-run", "tickets", "get", "5")
    assert code == 0
    assert pretty(out)["dry_run"] is True


# -- error objects ------------------------------------------------------------
def test_subcommand_parse_error_is_one_line(run_app, no_requests):
    code, out, err = run_app("--page-all", "tickets", "list", "--size", "99")
    assert code == 3
    payload = one_line(out)
    assert (payload["type"], payload["exit_code"]) == ("validation", 3)
    assert "--size" in payload["error"]
    assert "Usage" in err
    assert no_requests == []


def test_api_error_is_one_line(run_app, api):
    code, out, _ = run_app("--page-all", "tickets", "get", "2")
    assert code == 4
    assert one_line(out)["type"] == "not_found"


def test_validation_error_from_a_command_is_one_line(run_app, no_requests):
    code, out, _ = run_app("--page-all", "--dry-run", "tickets", "get", "0")
    assert code == 3
    assert one_line(out)["type"] == "validation"


def test_misplaced_global_flag_is_one_line(run_app, no_requests):
    code, out, _ = run_app("--page-all", "tickets", "list", "--dry-run")
    assert code == 3
    assert one_line(out)["type"] == "usage"


def test_unknown_command_is_one_line(run_app, no_requests):
    code, out, _ = run_app("--page-all", "tikets", "list")
    assert code == 3
    assert one_line(out)["type"] == "usage"


@pytest.mark.parametrize(
    ("root", "kind"),
    [
        (("--page-all", "--bogus"), "usage"),
        (("--page-all", "--page-limit", "0"), "validation"),
        (("--page-all", "--timeout", "abc"), "validation"),
        (("--page-all", "--staff", "--quiet"), "usage"),
        (("--page-all", "--timeout", "0"), "validation"),
        (("--page-all", "--staff", "a", "--staff-id", "1"), "validation"),
        (("--page-all", "-f", "bogus"), "validation"),
        (("--page-all", "-f", "json", "--bogus"), "usage"),
        (("--page-all", "-fjson", "--bogus"), "usage"),
        (("--page-all", "--format", "  ", "--bogus"), "usage"),
        (("--bogus", "--page-all"), "usage"),
    ],
    ids=lambda value: " ".join(value) if isinstance(value, tuple) else value,
)
def test_error_before_the_format_is_resolved_is_one_line_under_page_all(
    run_app, no_requests, root, kind
):
    code, out, _ = run_app(*root, "system", "statuses")
    assert code == 3
    assert one_line(out)["type"] == kind


def test_config_error_is_one_line_under_page_all(run_app, no_requests, tmp_path):
    cfg = tmp_path / "broken"
    cfg.mkdir()
    (cfg / "config.toml").write_text("not = [toml", encoding="utf-8")
    code, out, _ = run_app("--page-all", "--config-dir", str(cfg), "system", "statuses")
    assert code == 5
    assert one_line(out)["type"] == "config"


@pytest.mark.parametrize(
    "root",
    [
        ("--page-all", "-f", "table", "--bogus"),
        ("--page-all", "--format=csv", "--bogus"),
        ("--page-all", "-fyaml", "--bogus"),
        ("--page-all", "-f", "yml", "--page-limit", "0"),
        ("-f", "table", "--page-all", "--staff", "a", "--staff-id", "1"),
        # Read as the value of --staff, or as the resource after the terminator.
        ("--staff", "--page-all"),
        ("--", "--page-all"),
    ],
    ids=" ".join,
)
def test_early_error_stays_indented_when_the_root_flags_rule_out_ndjson(
    run_app, no_requests, root
):
    code, out, _ = run_app(*root, "system", "statuses")
    assert code == 3
    assert pretty(out)["exit_code"] == 3


def test_environment_format_keeps_an_early_error_indented(run_app, no_requests):
    code, out, _ = run_app("--page-all", "--bogus", "system", "statuses", HFOX_FORMAT="table")
    assert code == 3
    assert pretty(out)["type"] == "usage"


def test_flag_format_beats_the_environment_for_an_early_error(run_app, no_requests):
    code, out, _ = run_app(
        "--page-all", "-f", "json", "--bogus", "system", "statuses", HFOX_FORMAT="table"
    )
    assert code == 3
    assert one_line(out)["type"] == "usage"


def test_configured_table_format_keeps_a_later_error_indented(run_app, no_requests, tmp_path):
    cfg = tmp_path / "cfg"
    cfg.mkdir(exist_ok=True)
    (cfg / "config.toml").write_text('default_format = "table"\n', encoding="utf-8")
    code, out, _ = run_app("--page-all", "tickets", "list", "--size", "99")
    assert code == 3
    assert pretty(out)["type"] == "validation"


def test_errors_without_page_all_stay_indented(run_app, no_requests):
    code, out, _ = run_app("tickets", "list", "--size", "99")
    assert code == 3
    assert pretty(out)["type"] == "validation"
    code, out, _ = run_app("--bogus", "system", "statuses")
    assert code == 3
    assert pretty(out)["type"] == "usage"


# -- other formats ------------------------------------------------------------
def test_table_under_page_all_is_unchanged(run_app, api):
    code, walked, _ = run_app("-f", "table", "--page-all", "system", "categories")
    assert code == 0
    code, plain, _ = run_app("-f", "table", "system", "categories")
    assert code == 0
    assert walked == plain
    assert "Support" in walked and "Sales" in walked


def test_table_under_page_all_keeps_json_documents_indented(run_app, api):
    code, out, _ = run_app("-f", "table", "--page-all", "tickets", "get", "2")
    assert code == 4
    assert pretty(out)["type"] == "not_found"


def test_table_dry_run_under_page_all_stays_indented(run_app, no_requests):
    code, out, _ = run_app("-f", "table", "--page-all", "--dry-run", "tickets", "get", "5")
    assert code == 0
    assert pretty(out)["dry_run"] is True


def test_table_subcommand_parse_error_under_page_all_stays_indented(run_app, no_requests):
    code, out, _ = run_app("-f", "table", "--page-all", "tickets", "list", "--size", "99")
    assert code == 3
    assert pretty(out)["type"] == "validation"


# -- the framing switch -------------------------------------------------------
def test_framing_applies_to_a_direct_json_render_and_is_restored():
    buf = io.StringIO()
    with output.ndjson_framing(True):
        output.render({"a": [1, 2]}, OutputFormat.JSON, stream=buf)
        output.render({"b": 1}, OutputFormat.TABLE, stream=io.StringIO())
    output.render({"a": [1, 2]}, OutputFormat.JSON, stream=buf)
    assert buf.getvalue() == '{"a":[1,2]}\n{\n  "a": [\n    1,\n    2\n  ]\n}\n'


def test_framing_is_restored_when_the_block_raises():
    with pytest.raises(RuntimeError), output.ndjson_framing(True):
        raise RuntimeError
    buf = io.StringIO()
    output.render([1], OutputFormat.JSON, stream=buf)
    assert buf.getvalue() == "[\n  1\n]\n"


def test_framing_ends_with_the_cli_runner_invocation(api):
    result = runner.invoke(cli, ["--page-all", "tickets", "get", "1"], env=ENV)
    assert result.exit_code == 0, result.output
    assert one_line(result.stdout) == TICKET
    buf = io.StringIO()
    output.render({"id": 1}, OutputFormat.JSON, stream=buf)
    assert buf.getvalue() == '{\n  "id": 1\n}\n'


def test_framing_ends_with_the_app_invocation(run_app, api):
    code, _, _ = run_app("--page-all", "tickets", "get", "2")
    assert code == 4
    buf = io.StringIO()
    output.render({"id": 1}, OutputFormat.JSON, stream=buf)
    assert buf.getvalue() == '{\n  "id": 1\n}\n'
