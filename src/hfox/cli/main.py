"""Root Typer app: global flags, the `hfox <resource> <verb>` tree, and the exit-code contract."""

from __future__ import annotations

import os
import sys

import typer
from typer._click import exceptions as click_exceptions
from typer.core import TyperGroup

from .. import __version__
from ..core.config import load_config
from ..core.errors import ValidationError
from . import assets, auth, contacts, output, system, tickets
from .context import AppContext, as_hfox_error
from .output import OutputFormat


class _RootGroup(TyperGroup):
    """Root group whose usage line reads "Usage: 🦊 hfox ..." where the terminal can encode it."""

    def format_usage(self, ctx, formatter) -> None:
        # Help writes this line to stdout and usage errors to stderr.
        prog = f"{output.fox(sys.stdout, sys.stderr)}{ctx.command_path}"
        formatter.write_usage(prog, " ".join(self.collect_usage_pieces(ctx)))


cli = typer.Typer(
    name="hfox",
    cls=_RootGroup,
    no_args_is_help=True,
    add_completion=True,
    help="Command-line interface for the HappyFox REST API.",
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
        False, "--page-all", help="Fetch up to --page-limit pages."
    ),
    page_limit: int = typer.Option(
        10, "--page-limit", min=1, help="Max pages to fetch with --page-all."
    ),
    page_delay: int = typer.Option(
        100, "--page-delay", min=0, help="Delay in ms between paginated requests."
    ),
    staff_id: int = typer.Option(
        None, "--staff-id", help="Override the acting staff id for write actions."
    ),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Suppress status messages."),
    config_dir: str = typer.Option(
        None, "--config-dir", help="Override the config directory (default ~/.hfox).",
        envvar="HFOX_CONFIG_DIR",
    ),
    no_color: bool = typer.Option(False, "--no-color", help="Disable colored output."),
    version: bool = typer.Option(
        None, "--version", "-v", callback=_version_callback, is_eager=True,
        help="Show the release version (dev for a source build) and exit.",
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
    ctx.call_on_close(ctx.obj.close)


def app() -> None:
    """Entry point: run the CLI with centralized, structured error handling."""
    # An unencodable character must not crash output after a write already went through.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")
    command = typer.main.get_command(cli)
    try:
        # Non-standalone mode returns typer.Exit's code instead of raising it. A fixed prog_name
        # keeps usage, hints and completion on "hfox" for renamed binaries and `python -m hfox`.
        exit_code = command(args=None, prog_name="hfox", standalone_mode=False)
    except typer.Abort:
        output.warn("Aborted.")
        raise SystemExit(1) from None
    except click_exceptions.NoArgsIsHelpError as exc:
        # Click's default exit code 2 would collide with ExitCode.AUTH.
        exc.show()
        raise SystemExit(0) from None
    except click_exceptions.UsageError as exc:
        exc.show()
        err = ValidationError(exc.format_message())
        output.render(err.to_dict(), OutputFormat.JSON)
        raise SystemExit(int(err.exit_code)) from None
    except click_exceptions.ClickException as exc:  # other click-level errors
        exc.show()
        raise SystemExit(exc.exit_code) from None
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except Exception as exc:
        err = as_hfox_error(exc)
        output.render(err.to_dict(), OutputFormat.JSON)
        raise SystemExit(int(err.exit_code)) from None
    if isinstance(exit_code, int) and exit_code:
        raise SystemExit(exit_code)


if __name__ == "__main__":
    app()
