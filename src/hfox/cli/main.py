"""Root Typer app: global flags, the `hfox <resource> <verb>` tree, and the exit-code contract."""

from __future__ import annotations

import math
import os
import sys
from typing import NoReturn

import typer
from typer._click import exceptions as click_exceptions
from typer._click import types as click_types
from typer.core import TyperGroup

from .. import __version__
from ..core.config import CONFIG_FILENAME, load_config
from ..core.errors import CancelledError, HfoxError, UsageError, ValidationError
from . import assets, auth, contacts, output, system, tickets
from .context import AppContext, as_hfox_error
from .output import OutputFormat

_FORMAT_NAMES = {*(f.value for f in OutputFormat), "yml"}

# Declared by commands that take a staff identity, so the token scan cannot claim them.
_PER_COMMAND_FLAGS = {"--staff", "--staff-id"}


def _root_flags(command) -> set[str]:
    """Return every option name of the root command except --help."""
    names: set[str] = set()
    for param in getattr(command, "params", ()):
        names.update(getattr(param, "opts", ()))
        names.update(getattr(param, "secondary_opts", ()))
    names.discard("--help")
    return names


def _misplaced(name: str) -> UsageError:
    return UsageError(
        f"{name} is a global flag and goes before the resource: hfox {name} <resource> <verb>.",
        hint="Global flags precede the resource; see `hfox --help`.",
    )


def _is_number(text: str) -> bool:
    try:
        float(text)
    except ValueError:
        return False
    return True


def _value_options(command) -> dict[str, bool]:
    """Map every root option name that takes a value to whether that value is a number."""
    names: dict[str, bool] = {}
    for param in getattr(command, "params", ()):
        if not getattr(param, "is_flag", True) and not getattr(param, "count", False):
            numeric = isinstance(param.type, (click_types.IntParamType, click_types.FloatParamType))
            names.update(dict.fromkeys(getattr(param, "opts", ()), numeric))
    return names


def _check_root_values(command, args: list[str]) -> None:
    """Raise UsageError when a root option's value starts with `-`.

    An option missing its value would otherwise take the next flag, such as --dry-run, and a
    repeat hides the swallowed one. A numeric option keeps a negative number for its range check.
    """
    takes_value = _value_options(command)
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--" or not token.startswith("-"):
            return
        name, equals, value = token.partition("=")
        index += 1
        if name not in takes_value:
            continue
        if not equals:
            if index == len(args):
                return
            value = args[index]
            index += 1
        if value.startswith("-") and not (takes_value[name] and _is_number(value)):
            raise UsageError(
                f"{name} needs a value, got {value!r}.",
                hint=f"Pass {name} a value or drop it; a value cannot start with '-'.",
            )


class _RootGroup(TyperGroup):
    """Root group: the fox on the usage line and the misplaced global flag checks."""

    def parse_args(self, ctx, args):
        if not ctx.resilient_parsing:
            _check_root_values(self, list(args))
        return super().parse_args(ctx, args)

    def format_usage(self, ctx, formatter) -> None:
        # Help writes this line to stdout and usage errors to stderr.
        prog = f"{output.fox(sys.stdout, sys.stderr)}{ctx.command_path}"
        formatter.write_usage(prog, " ".join(self.collect_usage_pieces(ctx)))

    def resolve_command(self, ctx, args):
        # A root-only flag after the resource would otherwise be an unknown option, or be
        # swallowed as the value of the option before it. `--opt=--dry-run` sends the literal.
        if not ctx.resilient_parsing:
            flags = _root_flags(self) - _PER_COMMAND_FLAGS
            for token in args:
                if token == "--":
                    break
                # A short flag matches only as a whole token: `-fjson` may be data like `-fixed`.
                name = token.partition("=")[0] if token.startswith("--") else token
                if name in flags:
                    raise _misplaced(name)
        return super().resolve_command(ctx, args)


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
    staff: str = typer.Option(
        None, "--staff",
        help="Acting staff email or name for write actions; excludes --staff-id.",
    ),
    staff_id: int = typer.Option(
        None, "--staff-id", min=0,
        help="Acting staff id for write actions; excludes --staff.",
    ),
    timeout: float = typer.Option(
        None, "--timeout", envvar="HFOX_TIMEOUT", metavar="SECONDS",
        help="Per-attempt timeout in seconds for connecting and for each read or write "
        "(default 30).",
    ),
    max_retries: int = typer.Option(
        None, "--max-retries", min=0, envvar="HFOX_MAX_RETRIES",
        help="Retries after HTTP 429 or a network error (default 5).",
    ),
    quiet: bool = typer.Option(
        False, "--quiet", help="Suppress status messages and retry notices."
    ),
    config_dir: str = typer.Option(
        None, "--config-dir", envvar="HFOX_CONFIG_DIR",
        help="Config directory (default $XDG_CONFIG_HOME/hfox or ~/.config/hfox).",
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
    if staff is not None and staff_id is not None:
        raise ValidationError("--staff and --staff-id are mutually exclusive.")
    if timeout is not None and not (math.isfinite(timeout) and timeout > 0):
        raise ValidationError("--timeout must be a positive number of seconds.")
    config = load_config(config_dir)
    ctx.obj = AppContext(
        config=config,
        fmt=_resolve_format(fmt, config),
        dry_run=dry_run,
        page_all=page_all,
        page_limit=page_limit,
        page_delay_ms=page_delay,
        quiet=quiet,
        staff_id_override=staff_id,
        staff_override=staff,
        timeout=timeout,
        max_retries=max_retries,
    )
    ctx.call_on_close(ctx.obj.close)


def _resolve_format(flag: str | None, config) -> OutputFormat:
    """Parse the output format; an unknown name raises ValidationError naming its source."""
    value = flag if flag else config.default_format
    if value is None or value == "":
        return OutputFormat.JSON
    if str(value).strip().lower() not in _FORMAT_NAMES:
        if flag:
            source = "--format"
        elif os.environ.get("HFOX_FORMAT"):
            source = "HFOX_FORMAT"
        else:
            source = f"default_format in {config.dir / CONFIG_FILENAME}"
        raise ValidationError(
            f"Unknown output format {value!r} from {source}; expected json, table, csv or yaml."
        )
    return OutputFormat.parse(value)


def _fail(err: HfoxError) -> NoReturn:
    """Print the error JSON on stdout and exit with its code."""
    output.render(err.to_dict(), OutputFormat.JSON)
    raise SystemExit(int(err.exit_code))


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
        _fail(CancelledError("Cancelled."))
    except click_exceptions.NoArgsIsHelpError as exc:
        # Click's default exit code 2 would collide with ExitCode.AUTH.
        exc.show()
        raise SystemExit(0) from None
    except click_exceptions.NoSuchOption as exc:
        # Covers short flags with text attached, such as `-fjson`, and the per-command names
        # the token scan leaves alone.
        parent = getattr(exc.ctx, "parent", None)
        if parent is not None and exc.option_name in _root_flags(command):
            _fail(_misplaced(exc.option_name))
        exc.show()
        _fail(UsageError(exc.format_message()))
    except click_exceptions.MissingParameter as exc:
        exc.show()
        _fail(UsageError(exc.format_message()))
    except click_exceptions.BadParameter as exc:
        # A rejected value is a validation error whichever layer rejects it.
        exc.show()
        _fail(ValidationError(exc.format_message()))
    except click_exceptions.UsageError as exc:
        exc.show()
        _fail(UsageError(exc.format_message()))
    except click_exceptions.ClickException as exc:
        # Click's own exit code 1 would read as an API error.
        exc.show()
        _fail(HfoxError(exc.format_message()))
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except Exception as exc:
        _fail(as_hfox_error(exc))
    if isinstance(exit_code, int) and exit_code:
        raise SystemExit(exit_code)


if __name__ == "__main__":
    app()
