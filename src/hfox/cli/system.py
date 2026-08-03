"""Read-only reference data — the ids you plug into other commands.

These HappyFox endpoints return bare JSON arrays (no page_info envelope), so
each command issues a single GET via `obj.call` and hands the array straight to
`obj.render_list`, which renders rows as-is for table/csv and the full payload
for JSON.
"""

from __future__ import annotations

import typer

from .context import get_ctx

app = typer.Typer(
    no_args_is_help=True,
    help="Read-only reference data (ids you need for other commands).",
)


@app.command("categories")
def categories(ctx: typer.Context):
    """List ticket categories with their ids."""
    obj = get_ctx(ctx)
    obj.render_list(obj.call("GET", "categories/"))


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
    """List ticket custom fields (the t-cf-<id> ids and their choices)."""
    obj = get_ctx(ctx)
    obj.render_list(obj.call("GET", "ticket_custom_fields/"))


@app.command("contact-custom-fields")
def contact_custom_fields(ctx: typer.Context):
    """List contact custom fields (the c-cf-<id> ids and their choices)."""
    obj = get_ctx(ctx)
    obj.render_list(obj.call("GET", "user_custom_fields/"))
