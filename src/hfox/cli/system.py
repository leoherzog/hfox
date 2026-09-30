"""Read-only reference data: the ids other commands take.

These endpoints return bare JSON arrays, so each command is a single GET.
"""

from __future__ import annotations

from typing import Any

import typer

from .context import get_ctx
from .output import OutputFormat

app = typer.Typer(
    no_args_is_help=True,
    help="Read-only reference data (ids you need for other commands).",
)


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


def _render_fields(ctx: typer.Context, path: str) -> None:
    obj = get_ctx(ctx)
    rows = obj.call("GET", path)
    obj.render_list(rows if obj.fmt is OutputFormat.JSON else _flatten_choices(rows))


@app.command("categories")
def categories(ctx: typer.Context):
    """List ticket categories with their ids.

    New tickets need a public one; time_spent_mandatory ones need reply/note --time-spent.
    """
    obj = get_ctx(ctx)
    obj.render_list(obj.call("GET", "categories/"))


@app.command("priorities")
def priorities(ctx: typer.Context):
    """List ticket priorities with their ids."""
    obj = get_ctx(ctx)
    obj.render_list(obj.call("GET", "priorities/"))


@app.command("staff")
def staff(ctx: typer.Context):
    """List staff agents with their ids."""
    obj = get_ctx(ctx)
    obj.render_list(obj.call("GET", "staff/"))


@app.command("statuses")
def statuses(ctx: typer.Context):
    """List ticket statuses with their ids."""
    obj = get_ctx(ctx)
    obj.render_list(obj.call("GET", "statuses/"))


@app.command("ticket-custom-fields")
def ticket_custom_fields(ctx: typer.Context):
    """List ticket custom fields and their choice ids."""
    _render_fields(ctx, "ticket_custom_fields/")


@app.command("contact-custom-fields")
def contact_custom_fields(ctx: typer.Context):
    """List contact custom fields and their choice ids."""
    _render_fields(ctx, "user_custom_fields/")
