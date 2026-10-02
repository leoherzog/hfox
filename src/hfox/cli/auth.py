"""`hfox auth`: login, status and logout.

Login checks credentials against staff/ before saving them. Each command renders one
document that never holds a secret.
"""

from __future__ import annotations

import typer

from ..core.config import (
    CONFIG_FILENAME,
    DEFAULT_REGION,
    REGION_HOSTS,
    TOKEN_FILENAME,
    Config,
    clear_credentials,
    normalize_base_url,
    parse_staff_id,
    save_credentials,
    save_settings,
    stored_account,
    validate_host,
)
from ..core.errors import AuthError, HfoxError, ValidationError
from . import _util, output
from ._util import fold, nonblank_or_none
from .context import emit_dry_run, get_ctx

app = typer.Typer(no_args_is_help=True, help="Manage HappyFox credentials.")


# Flag to environment variable, for the values login cannot prompt for without a terminal.
_LOGIN_ENV = {
    "--subdomain": "HFOX_SUBDOMAIN",
    "--api-key": "HFOX_API_KEY",
    "--auth-code": "HFOX_AUTH_CODE",
}


def _missing_values_error(missing: list[str]) -> ValidationError:
    """Name the missing login values and their variables; only the subdomain may go on argv."""
    names = [_LOGIN_ENV[flag] for flag in missing]
    variables = names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"
    message = f"Missing {', '.join(missing)}; stdin is not a terminal, so set {variables}."
    if "--subdomain" in missing:
        message += " The subdomain can also be passed with --subdomain."
    return ValidationError(message)


def _resolve_staff_id(staff: list, email: str | None) -> tuple[int | None, str | None]:
    """Return (id, None) for the one agent whose email equals `email`, else (None, reason).

    The match ignores case and outer spaces. A blank `email` gives (None, None).
    """
    target = fold(nonblank_or_none(email))
    if target is None:
        return None, None
    matches = [m for m in staff if isinstance(m, dict) and fold(m.get("email")) == target]
    if not matches:
        return None, f"No agent matched {email}; default staff id not set."
    if len(matches) > 1:
        return None, f"{len(matches)} agents matched {email}; default staff id not set."
    raw = matches[0].get("id")
    try:
        staff_id = parse_staff_id(raw, "staff id")
    except ValidationError:
        staff_id = None
    if staff_id is None:
        return None, f"Ignoring unexpected staff id {raw!r}; default staff id not set."
    return staff_id, None


def _target(subdomain: str | None, region: str | None, base_url: str | None) -> tuple:
    """Return the comparable identity of a login target."""
    if base_url:
        try:
            base_url = normalize_base_url(base_url)
        except ValidationError:
            pass
    return (
        (subdomain or "").strip().lower() or None,
        (region or DEFAULT_REGION).strip().lower(),
        base_url or None,
    )


def _stored_staff_id(raw: object) -> int | None:
    """Return a stored default staff id, or None when unset or malformed."""
    try:
        return parse_staff_id(raw, "default_staff_id")
    except ValidationError:
        return None


@app.command()
def login(
    ctx: typer.Context,
    subdomain: str = typer.Option(
        None,
        "--subdomain",
        "-s",
        envvar="HFOX_SUBDOMAIN",
        help="Account subdomain or full custom host.",
    ),
    region: str = typer.Option(
        None, "--region", envvar="HFOX_REGION", help="Data center: us or eu."
    ),
    api_key: str = typer.Option(
        None,
        "--api-key",
        envvar="HFOX_API_KEY",
        help="API key; omit it to use HFOX_API_KEY or a hidden prompt, out of shell history.",
    ),
    auth_code: str = typer.Option(
        None,
        "--auth-code",
        envvar="HFOX_AUTH_CODE",
        help="Auth code; omit it to use HFOX_AUTH_CODE or a hidden prompt, out of shell history.",
    ),
    email: str = typer.Option(
        None, "--email", "-e", help="Agent email, used to set a default staff id."
    ),
) -> None:
    """Configure and validate HappyFox credentials."""
    obj = get_ctx(ctx)
    cfg_dir = obj.config.dir
    given = ctx.get_parameter_source("subdomain")
    subdomain_flag = given is not None and given.name == "COMMANDLINE"

    subdomain = nonblank_or_none(subdomain)
    region = nonblank_or_none(region)
    api_key = api_key or None
    auth_code = auth_code or None

    interactive = _util.stdin_is_tty()
    required = (("--subdomain", subdomain), ("--api-key", api_key), ("--auth-code", auth_code))
    missing = [flag for flag, value in required if value is None]
    if missing and not interactive:
        raise _missing_values_error(missing)
    if subdomain is None:
        subdomain = obj.prompt("HappyFox subdomain (e.g. acme)")
    # The subdomain is stored even when a base URL override routes the requests.
    validate_host(subdomain)
    if region is None:
        region = obj.prompt("Data center [us/eu]", default="us") if interactive else "us"
    region = region.strip().lower()
    if region not in REGION_HOSTS:
        raise ValidationError(f"region must be one of {list(REGION_HOSTS)}")
    if api_key is None:
        api_key = obj.prompt("API key", hide_input=True)
    if auth_code is None:
        auth_code = obj.prompt("Auth code", hide_input=True)

    override = obj.config.base_url_override
    if override:
        override = normalize_base_url(override, obj.config.base_url_source)
    probe = Config(
        subdomain=subdomain,
        region=region,
        api_key=api_key,
        auth_code=auth_code,
        base_url_override=override,
    )
    base_url = probe.base_url
    source = obj.config.base_url_source
    if override and subdomain_flag and source != "HFOX_BASE_URL":
        # Logout removes token.json only, so a config.toml value needs a hand edit.
        if source.endswith(TOKEN_FILENAME):
            remedy = "`hfox auth logout` clears it."
        else:
            remedy = f"remove base_url from {cfg_dir / CONFIG_FILENAME} to clear it."
        output.warn(f"The stored base URL {override} overrides --subdomain; {remedy}")
    if obj.dry_run:
        emit_dry_run(base_url, "GET", "staff/")
        raise typer.Exit(0)
    client = obj.make_client(base_url, api_key, auth_code)
    try:
        staff = client.get("staff/")
    except AuthError as exc:
        # Network, rate-limit and other API errors keep their own type and exit code.
        raise AuthError(f"Could not validate credentials: {exc}", detail=exc.detail) from exc
    finally:
        client.close()

    if not isinstance(staff, list):
        raise HfoxError("Unexpected response from staff/; credentials were not saved.")

    staff_id, problem = _resolve_staff_id(staff, email)
    before = stored_account(cfg_dir)
    changed = _target(before["subdomain"], before["region"], before["base_url"]) != _target(
        subdomain, region, override
    )
    # A default from another target, or one --email failed to replace, must not stay active.
    drop = staff_id is None and (changed or problem is not None)

    save_credentials(
        cfg_dir,
        subdomain=subdomain,
        region=region,
        api_key=api_key,
        auth_code=auth_code,
        base_url=override,
        warn=output.warn,
    )
    save_settings(
        cfg_dir,
        {"subdomain": subdomain, "region": region, "default_staff_id": staff_id},
        remove=("default_staff_id",) if drop else (),
        warn=output.warn,
    )
    dropped = drop and before["default_staff_id"] is not None
    stored = _stored_staff_id(stored_account(cfg_dir)["default_staff_id"])

    obj.success(f"{output.fox()}Authenticated to {base_url} ({len(staff)} agents visible).")
    if staff_id is not None:
        obj.success(f"Default staff id set to {staff_id} (from {email}).")
    else:
        if problem is not None:
            output.warn(problem)
        if dropped:
            reason = ": the login target changed" if changed else ""
            output.warn(f"Removed the stored default staff id{reason}.")
        elif stored is not None:
            obj.success(f"Default staff id {stored} kept.")
    obj.success(f"Credentials saved to {cfg_dir / 'token.json'}")
    obj.render(
        {
            "authenticated": True,
            "subdomain": subdomain,
            "region": region,
            "base_url": base_url,
            "default_staff_id": stored,
            "config_dir": str(cfg_dir),
        }
    )


@app.command()
def status(
    ctx: typer.Context,
    check: bool = typer.Option(
        False, "--check", help="Verify the credentials with one request."
    ),
) -> None:
    """Show the active account; exit 2 when credentials are missing."""
    obj = get_ctx(ctx)
    cfg = obj.config
    payload: dict = {
        "config_dir": str(cfg.dir),
        "subdomain": cfg.subdomain,
        "region": cfg.region,
        "base_url": cfg.base_url if (cfg.subdomain or cfg.base_url_override) else None,
        "authenticated": cfg.is_authenticated,
        "default_staff_id": cfg.default_staff_id if cfg.staff_id_error is None else None,
        "api_key": _mask(cfg.api_key),
    }
    if cfg.staff_id_error is not None:
        payload["default_staff_id_error"] = str(cfg.staff_id_error)
    if not cfg.is_authenticated:
        if check:
            payload["verified"] = False
        obj.render(payload)
        output.warn("Not authenticated. Run `hfox auth login`.")
        raise typer.Exit(2)
    if check:
        try:
            if not isinstance(obj.call("GET", "staff/"), list):
                raise HfoxError(
                    "Unexpected response from staff/; cannot verify the credentials."
                )
        except HfoxError as err:
            # Stdout holds one document, so the failure rides inside the payload.
            payload["verified"] = False
            payload["check_error"] = err.to_dict()
            obj.render(payload)
            output.warn(err.message)
            raise typer.Exit(int(err.exit_code)) from None
        payload["verified"] = True
    obj.render(payload)


@app.command()
def logout(ctx: typer.Context) -> None:
    """Remove stored credentials (token.json)."""
    obj = get_ctx(ctx)
    cfg_dir = obj.config.dir
    if obj.dry_run:
        obj.success(f"Dry run: would remove {cfg_dir / 'token.json'}.")
        obj.render({"dry_run": True, "removed": False, "config_dir": str(cfg_dir)})
        raise typer.Exit(0)
    removed = clear_credentials(cfg_dir)
    if removed:
        obj.success("Credentials removed.")
    else:
        obj.success("No stored credentials found.")
    obj.render({"removed": removed, "config_dir": str(cfg_dir)})


def _mask(secret: str | None) -> str | None:
    if not secret:
        return None
    # Fixed-length mask: never reveal the secret's length or short-key chars.
    if len(secret) < 8:
        return "••••••••"
    return f"{secret[:2]}{'•' * 6}{secret[-2:]}"
