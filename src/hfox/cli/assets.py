"""Assets — list, read, create, update, and delete HappyFox assets.

Also exposes read-only sub-groups for asset types (`types`) and asset custom
field definitions (`custom-fields`). Asset write operations require an acting
agent id (created_by / updated_by / deleted_by); when not passed explicitly we
fall back to the resolved staff id (--staff-id global or configured default).
"""

from __future__ import annotations

import json

import typer

from ..core.errors import ValidationError
from ._util import compact, split_csv_ints
from .cf import parse_asset_cf, parse_cf_json
from .context import get_ctx

app = typer.Typer(no_args_is_help=True, help="Manage assets, asset types, and asset custom fields.")


def _parse_new_contacts(raw: str | None) -> list:
    """Parse a --new-contact-json string into a list of new-contact objects.

    Returns [] for None/empty. Raises ValidationError on invalid JSON or a
    payload that is not a JSON array of objects.
    """
    if raw is None or raw.strip() == "":
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValidationError(f"Invalid --new-contact-json: {exc}") from exc
    if not isinstance(parsed, list):
        raise ValidationError(
            "Invalid --new-contact-json: expected a JSON array of new-contact objects."
        )
    if not all(isinstance(item, dict) for item in parsed):
        raise ValidationError(
            "Invalid --new-contact-json: each array element must be a JSON object."
        )
    return parsed


# --------------------------------------------------------------------------- #
# Top-level asset commands
# --------------------------------------------------------------------------- #
@app.command("list")
def list_assets(
    ctx: typer.Context,
    asset_type: int = typer.Option(
        None,
        "--asset-type",
        help="Asset type id to list. If omitted, the API returns the first asset type.",
    ),
    page: int = typer.Option(1, "--page", help="Page number (ignored with --page-all)."),
    size: int = typer.Option(10, "--size", help="Records per page."),
) -> None:
    """List assets, optionally filtered by asset type."""
    obj = get_ctx(ctx)
    params = compact({"asset_type": asset_type, "size": size, "page": page})
    body = obj.paginate("assets/", params=params)
    obj.render_list(body)


@app.command("get")
def get_asset(
    ctx: typer.Context,
    asset_id: int = typer.Argument(..., help="Asset id."),
) -> None:
    """Show a single asset."""
    obj = get_ctx(ctx)
    data = obj.call("GET", f"asset/{asset_id}/")
    obj.render(data)


@app.command("create")
def create_asset(
    ctx: typer.Context,
    name: str = typer.Option(..., "--name", help="Asset name."),
    display_id: str = typer.Option(..., "--display-id", help="Human-facing display id."),
    asset_type: int = typer.Option(
        None, "--asset-type", help="Asset type id to create under (query param)."
    ),
    created_by: int = typer.Option(
        None, "--created-by", help="Acting agent id (defaults to resolved staff id)."
    ),
    contact_ids: str = typer.Option(
        None, "--contact-ids", help="Comma-separated ids of existing contacts to link."
    ),
    new_contact_json: str = typer.Option(
        None,
        "--new-contact-json",
        help="JSON array of new-contact objects to create and link (sent as 'contacts').",
    ),
    cf: list[str] = typer.Option(
        None, "--cf", help="Asset custom field '<id>=<value>' (repeatable)."
    ),
    cf_json: str = typer.Option(
        None,
        "--cf-json",
        help="Asset custom fields as a JSON object {id: value}; merged with --cf (no coercion).",
    ),
) -> None:
    """Create an asset."""
    obj = get_ctx(ctx)
    created_by = obj.require_staff_id(created_by)
    custom_fields = parse_asset_cf(cf)
    custom_fields.update(parse_cf_json(cf_json, ""))
    body = compact(
        {
            "name": name,
            "display_id": display_id,
            "created_by": created_by,
            "contact_ids": split_csv_ints(contact_ids),
            "contacts": _parse_new_contacts(new_contact_json) or None,
            "custom_fields": custom_fields or None,
        }
    )
    params = compact({"asset_type": asset_type})
    data = obj.call("POST", "assets/", params=params, json=body)
    obj.render(data)


@app.command("update")
def update_asset(
    ctx: typer.Context,
    asset_id: int = typer.Argument(..., help="Asset id."),
    name: str = typer.Option(None, "--name", help="New asset name."),
    display_id: str = typer.Option(None, "--display-id", help="New display id."),
    updated_by: int = typer.Option(
        None, "--updated-by", help="Acting agent id (defaults to resolved staff id)."
    ),
    contact_ids: str = typer.Option(
        None, "--contact-ids", help="Comma-separated ids of existing contacts to link."
    ),
    new_contact_json: str = typer.Option(
        None,
        "--new-contact-json",
        help="JSON array of new-contact objects to create and link (sent as 'contacts').",
    ),
    cf: list[str] = typer.Option(
        None, "--cf", help="Asset custom field '<id>=<value>' (repeatable)."
    ),
    cf_json: str = typer.Option(
        None,
        "--cf-json",
        help="Asset custom fields as a JSON object {id: value}; merged with --cf (no coercion).",
    ),
) -> None:
    """Update an asset."""
    obj = get_ctx(ctx)
    updated_by = obj.require_staff_id(updated_by)
    custom_fields = parse_asset_cf(cf)
    custom_fields.update(parse_cf_json(cf_json, ""))
    body = compact(
        {
            "name": name,
            "display_id": display_id,
            "updated_by": updated_by,
            "contact_ids": split_csv_ints(contact_ids),
            "contacts": _parse_new_contacts(new_contact_json) or None,
            "custom_fields": custom_fields or None,
        }
    )
    data = obj.call("PUT", f"asset/{asset_id}/", json=body)
    obj.render(data)


@app.command("delete")
def delete_asset(
    ctx: typer.Context,
    asset_id: int = typer.Argument(..., help="Asset id."),
    deleted_by: int = typer.Option(
        None, "--deleted-by", help="Acting agent id (defaults to resolved staff id)."
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip the confirmation prompt."
    ),
) -> None:
    """Delete an asset."""
    obj = get_ctx(ctx)
    deleted_by = obj.require_staff_id(deleted_by)
    if not yes:
        typer.confirm(f"Delete asset {asset_id}?", abort=True)
    data = obj.call("DELETE", f"asset/{asset_id}/", params={"deleted_by": deleted_by})
    obj.success(f"Deleted asset {asset_id}.")
    obj.render(data)


# --------------------------------------------------------------------------- #
# Asset types (read-only)
# --------------------------------------------------------------------------- #
types = typer.Typer(no_args_is_help=True, help="Read asset types.")


@types.command("list")
def list_asset_types(ctx: typer.Context) -> None:
    """List asset types."""
    obj = get_ctx(ctx)
    body = obj.paginate("asset_types/")
    obj.render_list(body)


@types.command("get")
def get_asset_type(
    ctx: typer.Context,
    type_id: int = typer.Argument(..., help="Asset type id."),
) -> None:
    """Show a single asset type."""
    obj = get_ctx(ctx)
    data = obj.call("GET", f"asset_type/{type_id}/")
    obj.render(data)


app.add_typer(types, name="types")


# --------------------------------------------------------------------------- #
# Asset custom fields (read-only)
# --------------------------------------------------------------------------- #
custom_fields = typer.Typer(no_args_is_help=True, help="Read asset custom fields.")


@custom_fields.command("list")
def list_asset_custom_fields(
    ctx: typer.Context,
    asset_type: int = typer.Option(
        None,
        "--asset-type",
        help="Asset type id. If omitted, the API uses the first asset type.",
    ),
    page: int = typer.Option(1, "--page", help="Page number (ignored with --page-all)."),
    size: int = typer.Option(10, "--size", help="Records per page."),
) -> None:
    """List asset custom field definitions."""
    obj = get_ctx(ctx)
    params = compact({"asset_type": asset_type, "size": size, "page": page})
    body = obj.paginate("asset_custom_fields/", params=params)
    obj.render_list(body)


@custom_fields.command("get")
def get_asset_custom_field(
    ctx: typer.Context,
    field_id: int = typer.Argument(..., help="Asset custom field id."),
) -> None:
    """Show a single asset custom field definition."""
    obj = get_ctx(ctx)
    data = obj.call("GET", f"asset_custom_fields/{field_id}/")
    obj.render(data)


app.add_typer(custom_fields, name="custom-fields")
