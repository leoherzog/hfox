"""Root Typer application: global flags, error handling, and command tree.

Command taxonomy mirrors gws — `hfox <resource> <verb>`:

  hfox auth      login | status | logout
  hfox tickets   list | get | create | create-bulk | reply | note | user-reply
                 | update-cf | tags | subscribe | unsubscribe | forward | move | delete
  hfox contacts  list | get | create | create-bulk | update | groups ...
  hfox assets    list | get | create | update | delete | types ... | custom-fields ...
  hfox system    categories | staff | statuses | ticket-custom-fields | contact-custom-fields
"""

from __future__ import annotations

import os

import typer
from typer._click import exceptions as click_exceptions

from .. import __version__
from ..core.config import load_config
from ..core.errors import HfoxError, ValidationError
from . import assets, auth, contacts, output, system, tickets
from .context import AppContext
from .output import OutputFormat

cli = typer.Typer(
    name="hfox",
    no_args_is_help=True,
    add_completion=True,
    help="A modern command-line interface for the HappyFox REST API.",
    rich_markup_mode="rich",
)

cli.add_typer(auth.app, name="auth")
cli.add_typer(tickets.app, name="tickets")
cli.add_typer(contacts.app, name="contacts")
cli.add_typer(assets.app, name="assets")
cli.add_typer(system.app, name="system")


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"hfox {__version__}")
        raise typer.Exit(0)


@cli.callback()
def main(
    ctx: typer.Context,
    fmt: str = typer.Option(
        None, "--format", "-f", help="Output format: json (default), table, csv, yaml.",
        metavar="FMT",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Build and show the request without sending it."
    ),
    page_all: bool = typer.Option(
        False, "--page-all", help="Auto-paginate list commands and return all records."
    ),
    page_limit: int = typer.Option(
        10, "--page-limit", help="Max pages to fetch with --page-all."
    ),
    page_delay: int = typer.Option(
        100, "--page-delay", help="Delay in ms between paginated requests."
    ),
    staff_id: int = typer.Option(
        None, "--staff-id", help="Override the acting staff id for write actions."
    ),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Suppress status messages."),
    config_dir: str = typer.Option(
        None, "--config-dir", help="Override the config directory (default ~/.hfox).",
        envvar="HFOX_CONFIG_DIR",
    ),
    no_color: bool = typer.Option(
        False, "--no-color", help="Disable colored output.", envvar="NO_COLOR"
    ),
    version: bool = typer.Option(
        None, "--version", callback=_version_callback, is_eager=True,
        help="Show version and exit.",
    ),
) -> None:
    """Configure shared context for all subcommands."""
    if no_color:
        os.environ["NO_COLOR"] = "1"
    config = load_config(config_dir)
    resolved_fmt = OutputFormat.parse(fmt or config.default_format)
    ctx.obj = AppContext(
        config=config,
        fmt=resolved_fmt,
        dry_run=dry_run,
        page_all=page_all,
        page_limit=page_limit,
        page_delay_ms=page_delay,
        quiet=quiet,
        staff_id_override=staff_id,
    )
    # Ensure the AppContext's httpx client is closed once the command finishes
    # (Click invokes close callbacks in a finally as it tears down the context).
    ctx.call_on_close(ctx.obj.close)


def app() -> None:
    """Entry point: run the CLI with centralized, structured error handling."""
    command = typer.main.get_command(cli)
    try:
        command(args=None, standalone_mode=False)
    except typer.Exit as exc:  # typer.Exit / --help / --version
        raise SystemExit(exc.exit_code) from None
    except typer.Abort:
        output.warn("Aborted.")
        raise SystemExit(1) from None
    except click_exceptions.NoArgsIsHelpError as exc:
        # A bare group (`hfox tickets`) intentionally shows help; exit 0 like
        # --help. Click's default exit code 2 would collide with ExitCode.AUTH.
        exc.show()
        raise SystemExit(0) from None
    except click_exceptions.UsageError as exc:
        # Bad flag/argument/command: human hint on stderr (exc.show), structured
        # JSON on stdout, and ExitCode.VALIDATION (3) — click's 2 means auth here.
        exc.show()
        err = ValidationError(exc.format_message())
        output.render(err.to_dict(), OutputFormat.JSON)
        raise SystemExit(int(err.exit_code)) from None
    except click_exceptions.ClickException as exc:  # other click-level errors
        exc.show()
        raise SystemExit(exc.exit_code) from None
    except HfoxError as exc:
        output.render(exc.to_dict(), OutputFormat.JSON)
        raise SystemExit(int(exc.exit_code)) from None
    except KeyboardInterrupt:
        raise SystemExit(130) from None


if __name__ == "__main__":
    app()
