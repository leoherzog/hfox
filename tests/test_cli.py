import json

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


def run(*args, env_extra=None):
    env = dict(ENV)
    if env_extra:
        env.update(env_extra)
    return runner.invoke(cli, list(args), env=env)


def test_dry_run_ticket_create_builds_body():
    result = run(
        "--dry-run", "tickets", "create",
        "--subject", "Down", "--category", "3",
        "--name", "Han", "--email", "h@x.org", "--text", "fire",
        "--cf", "7=Urgent", "--cf", "6=3,4",
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["method"] == "POST"
    assert payload["url"].endswith("/tickets/")
    assert payload["body"]["t-cf-7"] == "Urgent"
    assert payload["body"]["t-cf-6"] == [3, 4]
    assert payload["body"]["category"] == 3


def test_dry_run_reply_uses_default_staff():
    result = run("--dry-run", "tickets", "reply", "55", "--text", "ok", "--status", "5")
    payload = json.loads(result.stdout)
    assert payload["url"].endswith("/ticket/55/staff_update/")
    assert payload["body"]["staff"] == 1
    assert payload["body"]["plaintext"] == "ok"


def test_dry_run_asset_create_custom_fields_object():
    result = run(
        "--dry-run", "assets", "create",
        "--asset-type", "1", "--name", "MBP", "--display-id", "L-1",
        "--cf", "5=4", "--cf", "6=3,4",
    )
    payload = json.loads(result.stdout)
    assert payload["params"] == {"asset_type": 1}
    assert payload["body"]["custom_fields"] == {"5": 4, "6": [3, 4]}
    assert payload["body"]["created_by"] == 1


def test_create_without_body_is_validation_error():
    result = run("tickets", "create", "--subject", "x", "--category", "1",
                 "--name", "n", "--email", "e@x.org")
    assert result.exit_code != 0


def test_format_table_flag_accepted():
    result = run("--dry-run", "-f", "table", "tickets", "list")
    assert result.exit_code == 0
