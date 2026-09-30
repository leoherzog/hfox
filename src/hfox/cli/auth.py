"""`hfox auth`: login, status and logout.

Login checks credentials against /staff/ before saving them.
"""

from __future__ import annotations

import typer

from ..core.client import HappyFoxClient
from ..core.config import (
    REGION_HOSTS,
    Config,
    clear_credentials,
    load_config,
    normalize_base_url,
    save_credentials,
    save_settings,
)
from ..core.errors import AuthError, HfoxError
from . import output
from ._util import fold, nonblank_or_none
from .context import emit_dry_run, get_ctx

app = typer.Typer(no_args_is_help=True, help="Manage HappyFox credentials.")


def _resolve_staff_id(staff: list, email: str | None) -> int | None:
    """Return the first agent id whose email equals `email`, ignoring case and outer spaces."""
    target = fold(nonblank_or_none(email))
    if target is None:
        return None
    for member in staff:
        if isinstance(member, dict) and fold(member.get("email")) == target:
            return member.get("id")
    return None


@app.command()
def login(
    ctx: typer.Context,
    subdomain: str = typer.Option(
        None, "--subdomain", "-s", help="Account subdomain or full custom host."
    ),
    region: str = typer.Option(None, "--region", help="Data center: us or eu."),
    api_key: str = typer.Option(
        None,
        "--api-key",
        help="API key; omit it to enter it at a hidden prompt, out of shell history.",
    ),
    auth_code: str = typer.Option(
        None,
        "--auth-code",
        help="Auth code; omit it to enter it at a hidden prompt, out of shell history.",
    ),
    email: str = typer.Option(
        None, "--email", "-e", help="Agent email, used to set a default staff id."
    ),
) -> None:
    """Configure and validate HappyFox credentials."""
    obj = get_ctx(ctx)
    cfg_dir = obj.config.dir

    subdomain = subdomain or typer.prompt("HappyFox subdomain (e.g. acme)")
    if not region:
        region = typer.prompt("Data center [us/eu]", default="us")
    region = region.strip().lower()
    if region not in REGION_HOSTS:
        raise typer.BadParameter(f"region must be one of {list(REGION_HOSTS)}")
    api_key = api_key or typer.prompt("API key", hide_input=True)
    auth_code = auth_code or typer.prompt("Auth code", hide_input=True)

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
    if obj.dry_run:
        emit_dry_run(probe.base_url, "GET", "staff/")
        raise typer.Exit(0)
    client = HappyFoxClient(probe.base_url, api_key, auth_code)
    try:
        staff = client.get("staff/")
    except HfoxError as exc:
        raise AuthError(f"Could not validate credentials: {exc}") from exc
    finally:
        client.close()

    if not isinstance(staff, list):
        raise AuthError("Unexpected response from /staff/; credentials may be invalid.")

    staff_id = _resolve_staff_id(staff, email)

    save_credentials(
        cfg_dir,
        subdomain=subdomain,
        region=region,
        api_key=api_key,
        auth_code=auth_code,
        base_url=override,
    )
    settings: dict = {"subdomain": subdomain, "region": region}
    if staff_id is not None:
        try:
            staff_id = int(staff_id)
        except (TypeError, ValueError):
            output.warn(f"Ignoring non-numeric staff id {staff_id!r}; default not set.")
            staff_id = None
    if staff_id is not None:
        settings["default_staff_id"] = staff_id
    save_settings(cfg_dir, settings)

    obj.success(f"Authenticated to {probe.base_url} ({len(staff)} agents visible).")
    if staff_id is not None:
        obj.success(f"Default staff id set to {staff_id} (from {email}).")
    elif email:
        output.warn(f"No agent matched {email}; default staff id not set.")
    obj.success(f"Credentials saved to {cfg_dir / 'token.json'}")


@app.command()
def status(ctx: typer.Context) -> None:
    """Show the active account and whether credentials are present."""
    obj = get_ctx(ctx)
    cfg = obj.config
    payload = {
        "config_dir": str(cfg.dir),
        "subdomain": cfg.subdomain,
        "region": cfg.region,
        "base_url": cfg.base_url if (cfg.subdomain or cfg.base_url_override) else None,
        "authenticated": cfg.is_authenticated,
        "default_staff_id": (
            cfg.default_staff_id if cfg.staff_id_error is None else str(cfg.staff_id_error)
        ),
        "api_key": _mask(cfg.api_key),
    }
    obj.render(payload)


@app.command()
def logout(ctx: typer.Context) -> None:
    """Remove stored credentials (token.json)."""
    obj = get_ctx(ctx)
    if obj.dry_run:
        obj.success(f"Dry run: would remove {obj.config.dir / 'token.json'}.")
        raise typer.Exit(0)
    removed = clear_credentials(obj.config.dir)
    if removed:
        obj.success("Credentials removed.")
    else:
        obj.success("No stored credentials found.")


def _mask(secret: str | None) -> str | None:
    if not secret:
        return None
    # Fixed-length mask: never reveal the secret's length or short-key chars.
    if len(secret) < 8:
        return "••••••••"
    return f"{secret[:2]}{'•' * 6}{secret[-2:]}"


# Re-export for callers that want a fresh Config (used by tests).
__all__ = ["app", "load_config"]
