"""Transport-layer contracts: URL encoding, retries, pagination limits, config validation."""

import json
import subprocess
import sys

import httpx
import pytest
import typer
from conftest import subprocess_env
from typer.testing import CliRunner

import hfox.cli.context as context_mod
import hfox.cli.main as main_mod
from hfox.cli.context import AppContext
from hfox.cli.main import cli
from hfox.cli.output import OutputFormat
from hfox.core.client import OUTCOME_UNKNOWN_HINT, HappyFoxClient, request_url
from hfox.core.config import Config, load_config, save_settings
from hfox.core.errors import NetworkError, RequestTimeoutError, ValidationError

BASE = "https://acme.happyfox.com/api/1.1/json"
ENV = {
    "HFOX_SUBDOMAIN": "acme",
    "HFOX_REGION": "us",
    "HFOX_API_KEY": "k",
    "HFOX_AUTH_CODE": "c",
    "HFOX_STAFF_ID": "1",
}
runner = CliRunner()


def make_client(handler):
    """Return (client, requests, sleeps) backed by a mock transport."""
    requests: list[httpx.Request] = []
    sleeps: list[float] = []

    def wrapped(request):
        requests.append(request)
        return handler(request)

    client = HappyFoxClient(BASE, "k", "c", sleep=sleeps.append)
    client._client = httpx.Client(transport=httpx.MockTransport(wrapped))
    return client, requests, sleeps


def ok(_request):
    return httpx.Response(200, json={"ok": True})


def pages(count):
    def handler(request):
        page = int(request.url.params["page"])
        return httpx.Response(200, json={"page_info": {"page_count": count}, "data": [page]})

    return handler


# -- URL encoding ------------------------------------------------------------
def test_at_sign_stays_literal_on_the_wire():
    client, requests, _ = make_client(ok)
    client.get("user/james@example.com/")
    assert requests[0].url.raw_path == b"/api/1.1/json/user/james@example.com/"


def test_reserved_characters_are_percent_encoded():
    client, requests, _ = make_client(ok)
    client.get("ticket/a#b?c d/")
    assert requests[0].url.raw_path == b"/api/1.1/json/ticket/a%23b%3Fc%20d/"


@pytest.mark.parametrize("path", ["user/5/../../tickets/", "ticket/./", "contact_group/../7/"])
def test_dot_segments_rejected_before_any_request(path):
    client, requests, _ = make_client(ok)
    with pytest.raises(ValidationError):
        client.request("POST", path, json={})
    assert requests == []


def test_request_url_matches_wire():
    params = {"size": 5, "q": "a b", "status": None, "minify": True}
    client, requests, _ = make_client(ok)
    client.get("user/a@b.com/", params=params)
    assert request_url(BASE, "user/a@b.com/", params) == str(requests[0].url)


# -- retries -----------------------------------------------------------------
def raising(exc_type, then=ok, times=1):
    state = {"n": 0}

    def handler(request):
        state["n"] += 1
        if state["n"] <= times:
            raise exc_type("boom", request=request)
        return then(request)

    return handler


@pytest.mark.parametrize(
    ("exc_type", "error_type", "message"),
    [
        (httpx.ReadTimeout, RequestTimeoutError, r"timed out \(ReadTimeout\)"),
        (httpx.WriteTimeout, RequestTimeoutError, r"timed out \(WriteTimeout\)"),
        (httpx.RemoteProtocolError, NetworkError, "Network error"),
        (httpx.ReadError, NetworkError, "Network error"),
    ],
)
def test_write_not_replayed_after_ambiguous_error(exc_type, error_type, message):
    client, requests, sleeps = make_client(raising(exc_type))
    with pytest.raises(NetworkError, match=message) as exc:
        client.request("POST", "tickets/", json={"subject": "x"})
    assert type(exc.value) is error_type
    assert exc.value.outcome_unknown is True
    assert exc.value.hint == OUTCOME_UNKNOWN_HINT
    assert exc.value.to_dict()["outcome_unknown"] is True
    assert len(requests) == 1
    assert sleeps == []


@pytest.mark.parametrize(
    ("exc_type", "attempts"),
    [
        (httpx.ConnectError, 6),
        (httpx.ConnectTimeout, 6),
        (httpx.PoolTimeout, 6),
        (httpx.ProxyError, 1),
        (httpx.UnsupportedProtocol, 1),
    ],
)
def test_write_that_never_left_has_known_outcome(exc_type, attempts):
    client, requests, _ = make_client(raising(exc_type, times=99))
    with pytest.raises(NetworkError) as exc:
        client.request("POST", "tickets/", json={"subject": "x"})
    assert len(requests) == attempts
    assert exc.value.outcome_unknown is False
    assert exc.value.hint is None
    assert "outcome_unknown" not in exc.value.to_dict()


@pytest.mark.parametrize("exc_type", [httpx.ReadTimeout, httpx.RemoteProtocolError])
def test_get_exhausting_retries_has_known_outcome(exc_type):
    client, requests, sleeps = make_client(raising(exc_type, times=99))
    with pytest.raises(NetworkError) as exc:
        client.get("tickets/")
    assert len(requests) == client.max_retries + 1
    assert len(sleeps) == client.max_retries
    assert exc.value.outcome_unknown is False
    assert exc.value.hint is None
    assert exc.value.detail == exc_type.__name__


@pytest.mark.parametrize("exc_type", [httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout])
def test_write_retried_when_connection_never_made(exc_type):
    client, requests, _ = make_client(raising(exc_type))
    assert client.request("POST", "tickets/", json={}) == {"ok": True}
    assert len(requests) == 2


def test_get_retried_after_read_timeout():
    client, requests, _ = make_client(raising(httpx.ReadTimeout))
    assert client.get("tickets/") == {"ok": True}
    assert len(requests) == 2


@pytest.mark.parametrize("header", ["-5", "nan", "inf", "-inf", ""])
def test_invalid_retry_after_falls_back_to_backoff(header):
    state = {"n": 0}

    def handler(request):
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": header}, json={})
        return httpx.Response(200, json={"ok": True})

    client, _, sleeps = make_client(handler)
    assert client.get("tickets/") == {"ok": True}
    assert sleeps == [client._backoff(0)]


# -- pagination ----------------------------------------------------------------
def test_page_size_clamped_to_50_even_when_params_set_it():
    client, requests, _ = make_client(pages(1))
    list(client.paginate_pages("tickets/", params={"size": 100}))
    assert requests[0].url.params["size"] == "50"


def test_truncation_reported_when_page_limit_stops_walk():
    seen = []
    client, _, _ = make_client(pages(5))
    walk = client.paginate("tickets/", page_limit=2, on_truncated=lambda *a: seen.append(a))
    records = list(walk)
    assert records == [1, 2]
    assert seen == [(2, 5)]


def test_truncation_not_reported_when_walk_completes():
    seen = []
    client, _, _ = make_client(pages(2))
    list(client.paginate("tickets/", page_limit=2, on_truncated=lambda *a: seen.append(a)))
    assert seen == []


def test_page_limit_below_one_rejected():
    client, requests, _ = make_client(pages(1))
    with pytest.raises(ValidationError):
        list(client.paginate_pages("tickets/", page_limit=0))
    assert requests == []


# -- config ------------------------------------------------------------------
@pytest.mark.parametrize("value", ["abc", "1.5", "-3", "7 7", "٣"])
def test_env_staff_id_must_be_digits(tmp_path, monkeypatch, value):
    monkeypatch.setenv("HFOX_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("HFOX_STAFF_ID", value)
    ctx = AppContext(config=load_config())
    with pytest.raises(ValidationError, match="HFOX_STAFF_ID"):
        ctx.require_staff_id(None, None)


@pytest.mark.parametrize("value", [" 7", "7\r"])
def test_env_staff_id_whitespace_trimmed(tmp_path, monkeypatch, value):
    monkeypatch.setenv("HFOX_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("HFOX_STAFF_ID", value)
    assert load_config().default_staff_id == 7


@pytest.mark.parametrize("toml_value", ["1.9", "true", "-2", '"x"'])
def test_toml_staff_id_must_be_int(tmp_path, monkeypatch, toml_value):
    monkeypatch.setenv("HFOX_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("HFOX_STAFF_ID", raising=False)
    (tmp_path / "config.toml").write_text(f"default_staff_id = {toml_value}\n")
    ctx = AppContext(config=load_config())
    with pytest.raises(ValidationError, match="config.toml"):
        ctx.resolve_staff_id(None, None)


@pytest.mark.parametrize("value", [7, "7", " 7", "7\r"])
def test_valid_staff_ids_accepted(tmp_path, monkeypatch, value):
    monkeypatch.setenv("HFOX_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("HFOX_STAFF_ID", raising=False)
    save_settings(tmp_path, {"default_staff_id": value})
    assert load_config().default_staff_id == 7


def test_save_settings_rejects_float_staff_id(tmp_path):
    with pytest.raises(ValidationError):
        save_settings(tmp_path, {"default_staff_id": 1.9})


def test_region_is_normalized(tmp_path, monkeypatch):
    monkeypatch.setenv("HFOX_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("HFOX_SUBDOMAIN", "acme")
    monkeypatch.setenv("HFOX_REGION", " EU ")
    cfg = load_config()
    assert cfg.region == "eu"
    assert cfg.base_url == "https://acme.happyfox.net/api/1.1/json"


def test_unknown_region_rejected():
    with pytest.raises(ValidationError, match="region"):
        _ = Config(subdomain="acme", region="mars").base_url


def test_custom_host_ignores_region():
    assert Config(subdomain="help.acme.com", region="mars").base_url == (
        "https://help.acme.com/api/1.1/json"
    )


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        ("https://gw.internal", "https://gw.internal/api/1.1/json"),
        ("https://gw.internal/api/1.1/json/", "https://gw.internal/api/1.1/json"),
        ("http://127.0.0.1:9", "http://127.0.0.1:9/api/1.1/json"),
    ],
)
def test_base_url_override_is_a_root(override, expected):
    assert Config(base_url_override=override).base_url == expected


@pytest.mark.parametrize(
    "override",
    [
        "gw.internal",
        "ftp://gw.internal",
        "https://",
        "https://gw.internal?a=1",
        "https://gw.internal#x",
        "https://gw.internal?",
        "https://user@",
        "https://:80",
        "https://[::1",
        "https://gw.internal:abc",
    ],
)
def test_base_url_override_requires_http_scheme(override):
    with pytest.raises(ValidationError, match="HFOX_BASE_URL"):
        _ = Config(base_url_override=override).base_url


@pytest.mark.parametrize(
    ("filename", "text"),
    [
        ("token.json", '{"base_url": "gw.internal:8443"}'),
        ("config.toml", 'base_url = "gw.internal:8443"\n'),
    ],
)
def test_saved_base_url_error_names_its_file(tmp_path, monkeypatch, filename, text):
    monkeypatch.setenv("HFOX_CONFIG_DIR", str(tmp_path))
    (tmp_path / filename).write_text(text)
    with pytest.raises(ValidationError, match=f"Invalid base_url in .*{filename} 'gw"):
        _ = load_config().base_url


def test_status_reports_malformed_staff_id(tmp_path, monkeypatch):
    monkeypatch.setenv("HFOX_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.toml").write_text("default_staff_id = 1.5\n")
    result = runner.invoke(cli, ["auth", "status"], env=ENV | {"HFOX_STAFF_ID": ""})
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["default_staff_id"] is None
    assert "config.toml" in payload["default_staff_id_error"]


def test_login_persists_normalized_base_url(monkeypatch, tmp_path):
    seen = {}

    def factory(base_url, api_key, auth_code, **kwargs):
        seen["base_url"] = base_url
        client = HappyFoxClient(base_url, api_key, auth_code, sleep=lambda _s: None)
        client._client = httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[]))
        )
        return client

    monkeypatch.setattr(context_mod, "HappyFoxClient", factory)
    result = runner.invoke(
        cli,
        ["auth", "login", "--subdomain", "acme", "--region", "us",
         "--api-key", "K", "--auth-code", "C"],
        env={"HFOX_CONFIG_DIR": str(tmp_path), "HFOX_BASE_URL": "https://gw.internal/api/1.1/json"},
    )
    assert result.exit_code == 0, result.output
    assert seen["base_url"] == "https://gw.internal/api/1.1/json"
    assert json.loads((tmp_path / "token.json").read_text())["base_url"] == "https://gw.internal"


# -- AppContext ----------------------------------------------------------------
def make_ctx(monkeypatch, handler, **kw):
    monkeypatch.setattr(context_mod, "HappyFoxClient", lambda *a, **k: make_client(handler)[0])
    cfg = Config(subdomain="acme", region="us", api_key="k", auth_code="c")
    return AppContext(config=cfg, page_delay_ms=0, **kw)


def test_ndjson_error_is_one_compact_line(monkeypatch, capsys):
    def handler(request):
        if request.url.params["page"] == "2":
            return httpx.Response(500, json={"error": "boom"})
        return httpx.Response(200, json={"page_info": {"page_count": 3}, "data": [1]})

    ctx = make_ctx(monkeypatch, handler, page_all=True)
    with pytest.raises(typer.Exit) as exc:
        ctx.paginate("tickets/", params={"size": 1})
    assert exc.value.exit_code == 1
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["data"] == [1]
    error = json.loads(lines[1])
    assert (error["status_code"], error["type"], error["exit_code"]) == (500, "api", 1)


def test_page_limit_truncation_warns_on_stderr(monkeypatch, capsys):
    ctx = make_ctx(monkeypatch, pages(4), page_all=True, page_limit=2, fmt=OutputFormat.CSV)
    assert ctx.paginate("tickets/") == [1, 2]
    captured = capsys.readouterr()
    assert "page 2 of 4" in captured.err
    assert captured.out == ""


def test_dry_run_url_matches_wire_encoding(capsys):
    ctx = AppContext(config=Config(subdomain="acme"), dry_run=True)
    with pytest.raises(typer.Exit):
        ctx.call("GET", "user/a@b.com/", params={"q": "x y"})
    out = json.loads(capsys.readouterr().out)
    assert out["url"] == f"{BASE}/user/a@b.com/?q=x+y"


def test_dry_run_rejects_dot_segments():
    ctx = AppContext(config=Config(subdomain="acme"), dry_run=True)
    with pytest.raises(ValidationError):
        ctx.call("POST", "user/5/../../tickets/", json={})


def test_cli_dry_run_contact_get_keeps_at_sign():
    result = runner.invoke(
        cli, ["--dry-run", "contacts", "get", "james@example.com"], env=ENV
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["method"] == "GET"
    assert payload["url"] == f"{BASE}/user/james@example.com/"
    assert payload["body"] is None


# -- main.app() --------------------------------------------------------------
_INVOKER = "import sys; sys.argv = ['hfox'] + sys.argv[1:]; from hfox.cli.main import app; app()"


def run_app(argv, **env_extra):
    """Run the real `app()` entry point; --dry-run keeps a regression off the network."""
    return subprocess.run(
        [sys.executable, "-c", _INVOKER, "--dry-run", *argv],
        env=subprocess_env(**{**ENV, **env_extra}),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize("argv", [["--page-limit", "0"], ["--page-delay", "-1"]])
def test_page_flag_bounds_exit_3(argv):
    proc = run_app([*argv, "--page-all", "system", "statuses"])
    assert proc.returncode == 3
    assert json.loads(proc.stdout)["exit_code"] == 3


def test_bad_env_staff_id_exits_3_naming_source():
    proc = run_app(["tickets", "delete", "5", "--yes"], HFOX_STAFF_ID="1.5")
    assert proc.returncode == 3
    assert "HFOX_STAFF_ID" in json.loads(proc.stdout)["error"]


@pytest.mark.parametrize("argv", [["system", "statuses"], ["tickets", "--help"]])
def test_bad_staff_id_spares_commands_that_do_not_need_one(argv):
    proc = run_app(argv, HFOX_STAFF_ID="1.5")
    assert proc.returncode == 0, proc.stdout


def test_explicit_staff_id_overrides_bad_configured_one():
    proc = run_app(["tickets", "delete", "5", "--yes", "--staff-id", "2"], HFOX_STAFF_ID="1.5")
    assert proc.returncode == 0, proc.stdout
    assert json.loads(proc.stdout)["body"] == {"staff_id": 2}


def test_unexpected_exception_is_json_exit_5(monkeypatch, capsys):
    def boom(_dir):
        raise KeyError("oops")

    monkeypatch.setattr(main_mod, "load_config", boom)
    monkeypatch.setattr(sys, "argv", ["hfox", "system", "statuses"])
    with pytest.raises(SystemExit) as exc:
        main_mod.app()
    assert exc.value.code == 5
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert (payload["exit_code"], payload["type"]) == (5, "internal")
    assert "KeyError" in payload["error"]
    assert "Traceback" not in captured.err


def test_app_propagates_typer_exit_code_from_streaming_error(monkeypatch, capsys):
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("HFOX_CONFIG_DIR", "/nonexistent-hfox")
    monkeypatch.setattr(
        context_mod,
        "HappyFoxClient",
        lambda *a, **k: make_client(lambda r: httpx.Response(503, json={}))[0],
    )
    monkeypatch.setattr(sys, "argv", ["hfox", "--page-all", "tickets", "list"])
    with pytest.raises(SystemExit) as exc:
        main_mod.app()
    assert exc.value.code == 1
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1
    error = json.loads(lines[0])
    assert (error["status_code"], error["type"]) == (503, "api")
