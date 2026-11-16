"""Read-only reference data: the ids other commands take.

These endpoints document no query params and return a bare JSON array (the priorities/ response
is undocumented), so each command is a single GET and its --name/--email flags filter locally.
"""

from __future__ import annotations

import typer

from ._util import filter_help, filter_rows
from .context import get_ctx

app = typer.Typer(
    no_args_is_help=True,
    help="Read-only reference data (ids you need for other commands).",
)

_NAME_HELP = filter_help("name")


def _render_rows(ctx: typer.Context, path: str, filters: dict[str, str | None]) -> None:
    """GET a reference list and render the rows matching `filters`."""
    obj = get_ctx(ctx)
    obj.render_list(filter_rows(obj.call("GET", path), filters))


@app.command("categories")
def categories(
    ctx: typer.Context,
    name: str = typer.Option(None, "--name", help=_NAME_HELP),
):
    """List ticket categories with their ids.

    New tickets need a public one; time_spent_mandatory ones need reply/note/update --time-spent.
    """
    _render_rows(ctx, "categories/", {"name": name})


@app.command("priorities")
def priorities(
    ctx: typer.Context,
    name: str = typer.Option(None, "--name", help=_NAME_HELP),
):
    """List ticket priorities with their ids."""
    _render_rows(ctx, "priorities/", {"name": name})


@app.command("staff")
def staff(
    ctx: typer.Context,
    email: str = typer.Option(None, "--email", help=filter_help("email")),
    name: str = typer.Option(None, "--name", help=_NAME_HELP),
):
    """List staff agents with their ids."""
    _render_rows(ctx, "staff/", {"email": email, "name": name})


@app.command("statuses")
def statuses(
    ctx: typer.Context,
    name: str = typer.Option(None, "--name", help=_NAME_HELP),
):
    """List ticket statuses with their ids."""
    _render_rows(ctx, "statuses/", {"name": name})
