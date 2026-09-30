"""Read-only reference data: the ids other commands take.

These endpoints return bare JSON arrays and take no query params, so each command is a single
GET and its --name/--email flags filter the whole array locally.
"""

from __future__ import annotations

from typing import Any

import typer

from ._util import filter_help, filter_rows
from .context import get_ctx
from .output import OutputFormat

app = typer.Typer(
    no_args_is_help=True,
    help="Read-only reference data (ids you need for other commands).",
)

_NAME_HELP = filter_help("name")


def _flatten_choices(rows: Any) -> Any:
    """Replace each field's choices with "text=id" pairs, dropping nested dependant fields."""
    if not isinstance(rows, list):
        return rows
    out = []
    for row in rows:
        if isinstance(row, dict) and isinstance(row.get("choices"), list):
            pairs = [
                f"{c.get('text')}={c.get('id')}" if isinstance(c, dict) else str(c)
                for c in row["choices"]
            ]
            row = {**row, "choices": ", ".join(pairs)}
        out.append(row)
    return out


def _render_rows(
    ctx: typer.Context, path: str, filters: dict[str, str | None], *, choices: bool = False
) -> None:
    """GET a reference list and render the rows matching `filters`.

    `choices` shows custom-field choices as "text=id" in non-JSON formats, after filtering.
    """
    obj = get_ctx(ctx)
    rows = filter_rows(obj.call("GET", path), filters)
    if choices and obj.fmt is not OutputFormat.JSON:
        rows = _flatten_choices(rows)
    obj.render_list(rows)


@app.command("categories")
def categories(
    ctx: typer.Context,
    name: str = typer.Option(None, "--name", help=_NAME_HELP),
):
    """List ticket categories with their ids.

    New tickets need a public one; time_spent_mandatory ones need reply/note --time-spent.
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


@app.command("ticket-custom-fields")
def ticket_custom_fields(
    ctx: typer.Context,
    name: str = typer.Option(None, "--name", help=_NAME_HELP),
):
    """List ticket custom fields and their choice ids."""
    _render_rows(ctx, "ticket_custom_fields/", {"name": name}, choices=True)


@app.command("contact-custom-fields")
def contact_custom_fields(
    ctx: typer.Context,
    name: str = typer.Option(None, "--name", help=_NAME_HELP),
):
    """List contact custom fields and their choice ids."""
    _render_rows(ctx, "user_custom_fields/", {"name": name}, choices=True)
