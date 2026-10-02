"""Root app edges: stream setup, format sources, paging flags, short global flag placement,
entry points, system rendering.
"""

import io
import json
import subprocess
import sys

import httpx
import pytest
import typer
import yaml
from conftest import subprocess_env
from typer.testing import CliRunner

import hfox.cli.context as context_mod
import hfox.cli.main as main_mod
from hfox.cli.main import cli
from hfox.cli.output import OutputFormat
from hfox.core.client import HappyFoxClient
from hfox.core.config import Config

runner = CliRunner()

ENV = {
    "HFOX_SUBDOMAIN": "acme",
    "HFOX_REGION": "us",
    "HFOX_API_KEY": "k",
    "HFOX_AUTH_CODE": "c",
    "HFOX_STAFF_ID": "1",
}


def run(*args, env_extra=None):
    return runner.invoke(cli, list(args), env={**ENV, **(env_extra or {})})


def layer(encoding):
    """Return a strict text stream over bytes, like a redirected stdout or stderr."""
    return io.TextIOWrapper(io.BytesIO(), encoding=encoding, newline="")


def written(stream):
    """Return the text a `layer` or StringIO received."""
    if isinstance(stream, io.StringIO):
        return stream.getvalue()
    stream.flush()
    return stream.buffer.getvalue().decode(stream.encoding)


@pytest.fixture
def run_app(monkeypatch, capsys):
    """Run the real app() in process; return (exit code, stdout, stderr).

    A `stdout` or `stderr` stream replaces the captured one and is read back instead.
    """

    def invoke(*argv, stdout=None, stderr=None, **env_extra):
        for key, value in {**ENV, **env_extra}.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setattr(sys, "argv", ["hfox", *argv])
        if stdout is not None:
            monkeypatch.setattr(sys, "stdout", stdout)
        if stderr is not None:
            monkeypatch.setattr(sys, "stderr", stderr)
        code = 0
        try:
            main_mod.app()
        except SystemExit as exc:
            code = exc.code or 0
        captured = capsys.readouterr()
        out = captured.out if stdout is None else written(stdout)
        err = captured.err if stderr is None else written(stderr)
        return code, out, err

    return invoke


@pytest.fixture
def paged_api(monkeypatch):
    """Return a factory: install(total) serves `total` one-row pages and returns the
    captured requests and the delays the client slept between them.
    """

    def install(total):
        requests: list[httpx.Request] = []
        sleeps: list[float] = []

        def handler(request):
            requests.append(request)
            page = int(request.url.params["page"])
            body = {"page_info": {"page_count": total}, "data": [{"id": page}]}
            return httpx.Response(200, json=body)

        def factory(base_url, api_key, auth_code, **kwargs):
            client = HappyFoxClient(base_url, api_key, auth_code, sleep=sleeps.append)
            client._client = httpx.Client(transport=httpx.MockTransport(handler))
            return client

        monkeypatch.setattr(context_mod, "HappyFoxClient", factory)
        return requests, sleeps

    return install


# -- stream setup -------------------------------------------------------------
def test_unencodable_data_on_stdout_is_escaped(run_app, mock_api):
    mock_api(lambda req: httpx.Response(200, json=[{"id": 1, "name": "Zoë"}]))
    code, out, _ = run_app("-f", "yaml", "system", "staff", stdout=layer("ascii"))
    assert code == 0
    assert "name: Zo\\xeb" in out


def test_unencodable_status_line_does_not_fail_a_finished_write(run_app, mock_api):
    captured = mock_api(lambda req: httpx.Response(200, json={"display_id": "#É1"}))
    code, out, err = run_app(
        "tickets", "create", "--subject", "S", "--category", "3", "--name", "H",
        "--email", "h@x.org", "--text", "b", stderr=layer("ascii"),
    )
    assert code == 0
    assert json.loads(out) == {"display_id": "#É1"}
    assert "#\\xc91" in err
    assert len(captured) == 1


def test_app_runs_with_a_stream_that_cannot_be_reconfigured(run_app):
    code, out, _ = run_app("--version", stdout=io.StringIO())
    assert (code, out) == (0, "hfox dev\n")


# Help writes the usage line to stdout and a usage error writes it to stderr.
@pytest.mark.parametrize(("argv", "narrow"), [(("--help",), "stdout"), (("--bogus",), "stderr")])
def test_usage_line_has_no_fox_when_its_own_stream_cannot_encode_it(run_app, argv, narrow):
    streams = {"stdout": layer("utf-8"), "stderr": layer("utf-8"), narrow: layer("cp1252")}
    _, out, err = run_app(*argv, **streams)
    assert "Usage: hfox [OPTIONS]" in (out if narrow == "stdout" else err)


# -- --format sources and --version -------------------------------------------
def _write_default_format(tmp_path, value):
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "config.toml").write_text(f'default_format = "{value}"\n', encoding="utf-8")
    return cfg / "config.toml"


def test_unknown_format_flag_is_named_over_a_set_environment_format(run_app, tmp_path):
    _write_default_format(tmp_path, "csv")
    code, out, _ = run_app("-f", "bogus", "--dry-run", "system", "statuses", HFOX_FORMAT="table")
    payload = json.loads(out)
    assert (code, payload["type"]) == (3, "validation")
    assert "--format" in payload["error"]
    assert "HFOX_FORMAT" not in payload["error"]
    assert "default_format" not in payload["error"]


def test_empty_env_format_leaves_the_config_file_as_the_named_source(run_app, tmp_path):
    path = _write_default_format(tmp_path, "xml")
    code, out, _ = run_app("--dry-run", "system", "statuses", HFOX_FORMAT="")
    payload = json.loads(out)
    assert (code, payload["type"]) == (3, "validation")
    assert f"default_format in {path}" in payload["error"]
    assert "HFOX_FORMAT" not in payload["error"]


@pytest.mark.parametrize("unset", [None, ""])
def test_resolve_format_counts_none_and_empty_as_unset(unset):
    assert main_mod._resolve_format(None, Config(default_format=unset)) is OutputFormat.JSON


@pytest.mark.parametrize("blank", ["", " ", "  ", "\t", "\n", " "], ids=repr)
def test_resolve_format_counts_a_blank_flag_as_not_given(blank):
    assert main_mod._resolve_format(blank, Config(default_format="table")) is OutputFormat.TABLE
    assert main_mod._resolve_format(blank, Config(default_format=None)) is OutputFormat.JSON


def test_resolve_format_keeps_a_padded_flag():
    assert main_mod._resolve_format(" CSV ", Config(default_format="table")) is OutputFormat.CSV


def test_version_is_reported_before_other_root_options_are_validated(run_app):
    code, out, _ = run_app("--page-limit", "0", "--version")
    assert (code, out) == (0, "hfox dev\n")


# -- --config-dir, paging flags and client lifetime ---------------------------
def test_config_dir_flag_beats_the_environment(tmp_path):
    # HFOX_CONFIG_DIR already points at tmp_path / "cfg".
    for name, subdomain in (("cfg", "fromenv"), ("flagged", "fromflag")):
        (tmp_path / name).mkdir()
        (tmp_path / name / "config.toml").write_text(
            f'subdomain = "{subdomain}"\n', encoding="utf-8"
        )
    result = runner.invoke(
        cli, ["--config-dir", str(tmp_path / "flagged"), "--dry-run", "system", "statuses"]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["url"] == "https://fromflag.happyfox.com/api/1.1/json/statuses/"


def test_page_all_defaults_to_ten_pages_a_tenth_of_a_second_apart(paged_api):
    requests, sleeps = paged_api(12)
    result = run("--page-all", "-f", "csv", "assets", "list")
    assert result.exit_code == 0, result.output
    assert [r.url.params["page"] for r in requests] == [str(n) for n in range(1, 11)]
    assert sleeps == [0.1] * 9
    assert "--page-limit" in result.stderr


@pytest.mark.parametrize(
    ("flags", "pages", "delays"),
    [
        (("--page-limit", "3", "--page-delay", "250"), 3, [0.25, 0.25]),
        (("--page-limit", "1"), 1, []),
        (("--page-limit", "2", "--page-delay", "0"), 2, []),
    ],
)
def test_page_limit_and_page_delay_flags_bound_the_walk(paged_api, flags, pages, delays):
    requests, sleeps = paged_api(12)
    result = run("--page-all", *flags, "-f", "csv", "assets", "list")
    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == ["id", *(str(n) for n in range(1, pages + 1))]
    assert len(requests) == pages
    assert sleeps == delays


@pytest.mark.parametrize("status", [200, 500])
def test_http_client_is_closed_when_the_command_ends(monkeypatch, status):
    built: list[httpx.Client] = []

    def factory(base_url, api_key, auth_code, **kwargs):
        client = HappyFoxClient(base_url, api_key, auth_code, sleep=lambda _s: None)
        transport = httpx.MockTransport(lambda req: httpx.Response(status, json=[]))
        client._client = httpx.Client(transport=transport)
        built.append(client._client)
        return client

    monkeypatch.setattr(context_mod, "HappyFoxClient", factory)
    run("system", "statuses")
    assert [http.is_closed for http in built] == [True]


# -- exit contract through app() ----------------------------------------------
@pytest.mark.parametrize(
    "argv", [("system", "statuses"), ("--page-all", "tickets", "list")], ids=" ".join
)
def test_ctrl_c_during_the_first_request_exits_130_and_prints_nothing(run_app, mock_api, argv):
    def interrupted(request):
        raise KeyboardInterrupt

    mock_api(interrupted)
    code, out, _ = run_app(*argv)
    assert (code, out) == (130, "")


# NDJSON pages already written stay on stdout; other formats print only after the walk.
@pytest.mark.parametrize(
    ("flags", "streamed"),
    [((), '{"page_info":{"page_count":3},"data":[{"id":1}]}\n'), (("-f", "csv"), "")],
    ids=["json", "csv"],
)
def test_ctrl_c_after_the_first_page_exits_130_and_prints_no_error_object(
    run_app, mock_api, flags, streamed
):
    def handler(request):
        page = int(request.url.params["page"])
        if page > 1:
            raise KeyboardInterrupt
        return httpx.Response(200, json={"page_info": {"page_count": 3}, "data": [{"id": page}]})

    requests = mock_api(handler)
    code, out, err = run_app("--page-all", *flags, "tickets", "list")
    assert [request.url.params["page"] for request in requests] == ["1", "2"]
    assert (code, out, err) == (130, streamed, "")


def _walk(command, path=()):
    yield path, command
    for name, sub in getattr(command, "commands", {}).items():
        yield from _walk(sub, (*path, name))


def test_every_bare_group_prints_its_help_and_exits_0(run_app):
    root = typer.main.get_command(cli)
    groups = [path for path, command in _walk(root) if path and hasattr(command, "commands")]
    assert {("system",), ("auth",), ("contacts", "groups")} <= set(groups)
    for path in groups:
        code, out, _ = run_app(*path)
        assert code == 0, path
        assert f"Usage: hfox {' '.join(path)} [OPTIONS]" in out, path


# -- short global flags after the resource ------------------------------------
@pytest.mark.parametrize(
    "argv",
    [
        ("tickets", "note", "5", "--text", "-v"),
        ("tickets", "note", "5", "--text", "-f"),
        ("tickets", "list", "-q", "-v"),
    ],
    ids=" ".join,
)
def test_short_global_flag_swallowed_as_an_option_value_sends_nothing(run_app, mock_api, argv):
    captured = mock_api(lambda req: httpx.Response(200, json={}))
    code, out, err = run_app(*argv)
    flag = argv[-1]
    assert code == 3
    assert json.loads(out) == {
        "error": f"{flag} is a global flag and goes before the resource: "
        f"hfox {flag} <resource> <verb>.",
        "type": "usage",
        "exit_code": 3,
        "hint": "Global flags precede the resource; see `hfox --help`.",
    }
    assert err == ""
    assert captured == []


@pytest.mark.parametrize("value", ["-v", "-f"])
def test_equals_form_sends_a_literal_short_flag(value):
    result = run("--dry-run", "tickets", "note", "5", f"--text={value}")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["body"]["plaintext"] == value


# A short flag with text attached cannot be told from data such as `-fixed`.
@pytest.mark.parametrize("value", ["-fjson", "-f=json", "-forward", "-version"])
def test_value_that_only_starts_with_a_short_global_flag_is_sent(value):
    result = run("--dry-run", "tickets", "note", "5", "--text", value)
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["body"]["plaintext"] == value


def test_short_global_flag_after_the_terminator_is_left_alone(run_app):
    code, out, _ = run_app("--dry-run", "tickets", "get", "--", "-v")
    payload = json.loads(out)
    assert (code, payload["type"]) == (3, "validation")
    assert "global flag" not in payload["error"]
    assert "'-v'" in payload["error"]


# -- python -m hfox -----------------------------------------------------------
def _python_m_hfox(*argv, **env):
    return subprocess.run(
        [sys.executable, "-m", "hfox", *argv],
        env=subprocess_env(**ENV, **env),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )


def test_python_m_hfox_runs_the_app_under_the_name_hfox():
    proc = _python_m_hfox("tickets", "get")
    assert proc.returncode == 3
    assert json.loads(proc.stdout)["type"] == "usage"
    assert "Usage: hfox tickets get" in proc.stderr


def test_shell_completion_answers_to_the_hfox_variable():
    proc = _python_m_hfox(_HFOX_COMPLETE="complete_bash", COMP_WORDS="hfox ti", COMP_CWORD="1")
    assert proc.returncode == 0
    assert proc.stdout.split() == ["tickets"]


# -- system custom-field rendering --------------------------------------------
def test_custom_field_choices_flattened_in_yaml(mock_api):
    fields = [
        {"id": 61, "choices": [{"text": "No", "id": 2}, {"text": "Yes", "id": 1}]},
        {"id": 4, "choices": None},
    ]
    mock_api(lambda req: httpx.Response(200, json=fields))
    result = run("-f", "yaml", "system", "ticket-custom-fields")
    assert result.exit_code == 0, result.output
    assert yaml.safe_load(result.stdout) == [
        {"id": 61, "choices": "No=2, Yes=1"},
        {"id": 4, "choices": None},
    ]


def test_custom_fields_body_that_is_not_a_list_renders_unchanged(mock_api):
    mock_api(lambda req: httpx.Response(200, json={"detail": "x"}))
    result = run("-f", "csv", "system", "ticket-custom-fields")
    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == ["detail", "x"]


def test_custom_field_rows_and_choices_of_unexpected_shape_pass_through(mock_api):
    body = [
        {"id": 1, "choices": [{"text": "No", "id": 2}, "Other", 7]},
        {"id": 2, "choices": "n/a"},
        "odd",
    ]
    mock_api(lambda req: httpx.Response(200, json=body))
    result = run("-f", "csv", "system", "contact-custom-fields")
    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == [
        "id,choices,value",
        '1,"No=2, Other, 7",',
        "2,n/a,",
        ",,odd",
    ]
