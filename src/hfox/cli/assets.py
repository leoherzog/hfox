"""Manage HappyFox assets, with read-only `types` and `custom-fields` sub-apps."""

from __future__ import annotations

import typer

from ..core.errors import ValidationError
from ._util import compact, confirm, parse_json, require_nonblank, split_csv_ints
from .cf import CF_HELP, CF_JSON_HELP, parse_asset_cf, parse_cf_json
from .context import get_ctx

app = typer.Typer(no_args_is_help=True, help="Manage assets, asset types, and asset custom fields.")

#: Documented limit on an asset name.
MAX_NAME_LENGTH = 200

_NEW_CONTACT_HELP = (
    "JSON array of new contacts to create and link; needs Manage all Contacts permission."
)


def _parse_new_contacts(raw: str | None) -> list | None:
    """Parse --new-contact-json into a list of objects; None when blank."""
    if raw is None or raw.strip() == "":
        return None
    parsed = parse_json(raw, "--new-contact-json")
    if not isinstance(parsed, list):
        raise ValidationError(
            "Invalid --new-contact-json: expected a JSON array of new-contact objects."
        )
    if not all(isinstance(item, dict) for item in parsed):
        raise ValidationError(
            "Invalid --new-contact-json: each array element must be a JSON object."
        )
    return parsed or None


def _check_name(name: str | None) -> None:
    if name is None:
        return
    require_nonblank(name, "--name")
    if len(name) > MAX_NAME_LENGTH:
        raise ValidationError(f"--name allows up to {MAX_NAME_LENGTH} characters.")


def _check_id_lists(contact_ids: str | None, contact_group_ids: str | None) -> None:
    for flag, raw in (("--contact-ids", contact_ids), ("--contact-group-ids", contact_group_ids)):
        if raw is not None and not split_csv_ints(raw):
            raise ValidationError(f"{flag} needs at least one id.")


def _custom_fields(cf: list[str] | None, cf_json: str | None) -> dict | None:
    fields = parse_asset_cf(cf)
    fields.update(parse_cf_json(cf_json, "", ()))
    return fields or None


# --------------------------------------------------------------------------- #
# Top-level asset commands
# --------------------------------------------------------------------------- #
@app.command("list")
def list_assets(
    ctx: typer.Context,
    asset_type: int = typer.Option(
        None, "--asset-type", min=1, help="Asset type id; defaults to the first type."
    ),
    page: int = typer.Option(1, "--page", min=1, help="Page number to fetch."),
    size: int = typer.Option(10, "--size", min=1, max=50, help="Records per page."),
) -> None:
    """List assets of one asset type."""
    obj = get_ctx(ctx)
    params = compact({"asset_type": asset_type, "size": size, "page": page})
    body = obj.paginate("assets/", params=params)
    obj.render_list(body)


@app.command("get")
def get_asset(
    ctx: typer.Context,
    asset_id: int = typer.Argument(..., min=1, help="Asset id."),
) -> None:
    """Show a single asset."""
    obj = get_ctx(ctx)
    data = obj.call("GET", f"asset/{asset_id}/")
    obj.render(data)


@app.command("create")
def create_asset(
    ctx: typer.Context,
    name: str = typer.Option(..., "--name", help="Asset name, up to 200 characters."),
    display_id: str = typer.Option(..., "--display-id", help="Human-facing display id."),
    asset_type: int = typer.Option(
        None,
        "--asset-type",
        min=1,
        help="Asset type id; defaults to the first type, and --cf ids must belong to it.",
    ),
    created_by: int = typer.Option(
        None, "--created-by", help="Acting agent id (defaults to resolved staff id)."
    ),
    contact_ids: str = typer.Option(
        None, "--contact-ids", help="Comma-separated ids of existing contacts to link."
    ),
    contact_group_ids: str = typer.Option(
        None, "--contact-group-ids", help="Comma-separated ids of contact groups to link."
    ),
    new_contact_json: str = typer.Option(None, "--new-contact-json", help=_NEW_CONTACT_HELP),
    cf: list[str] = typer.Option(None, "--cf", help=CF_HELP),
    cf_json: str = typer.Option(None, "--cf-json", help=CF_JSON_HELP),
) -> None:
    """Create an asset."""
    obj = get_ctx(ctx)
    _check_name(name)
    require_nonblank(display_id, "--display-id")
    _check_id_lists(contact_ids, contact_group_ids)
    created_by = obj.require_staff_id(created_by)
    body = compact(
        {
            "name": name,
            "display_id": display_id,
            "created_by": created_by,
            "contact_ids": split_csv_ints(contact_ids),
            "contact_group_ids": split_csv_ints(contact_group_ids),
            "contacts": _parse_new_contacts(new_contact_json),
            "custom_fields": _custom_fields(cf, cf_json),
        }
    )
    params = compact({"asset_type": asset_type})
    data = obj.call("POST", "assets/", params=params, json=body)
    obj.render(data)


@app.command("update")
def update_asset(
    ctx: typer.Context,
    asset_id: int = typer.Argument(..., min=1, help="Asset id."),
    name: str = typer.Option(None, "--name", help="New asset name, up to 200 characters."),
    display_id: str = typer.Option(None, "--display-id", help="New display id."),
    updated_by: int = typer.Option(
        None, "--updated-by", help="Acting agent id (defaults to resolved staff id)."
    ),
    contact_ids: str = typer.Option(
        None, "--contact-ids", help="Comma-separated ids of existing contacts to link."
    ),
    contact_group_ids: str = typer.Option(
        None, "--contact-group-ids", help="Comma-separated ids of contact groups to link."
    ),
    new_contact_json: str = typer.Option(None, "--new-contact-json", help=_NEW_CONTACT_HELP),
    cf: list[str] = typer.Option(None, "--cf", help=CF_HELP),
    cf_json: str = typer.Option(None, "--cf-json", help=CF_JSON_HELP),
) -> None:
    """Update an asset."""
    obj = get_ctx(ctx)
    _check_name(name)
    if display_id is not None:
        require_nonblank(display_id, "--display-id")
    fields = compact(
        {
            "name": name,
            "display_id": display_id,
            "contact_ids": split_csv_ints(contact_ids),
            "contact_group_ids": split_csv_ints(contact_group_ids),
            "contacts": _parse_new_contacts(new_contact_json),
            "custom_fields": _custom_fields(cf, cf_json),
        }
    )
    if not fields:
        raise ValidationError("Nothing to update; pass at least one field.")
    body = {"updated_by": obj.require_staff_id(updated_by), **fields}
    data = obj.call("PUT", f"asset/{asset_id}/", json=body)
    obj.render(data)


@app.command("delete")
def delete_asset(
    ctx: typer.Context,
    asset_id: int = typer.Argument(..., min=1, help="Asset id."),
    deleted_by: int = typer.Option(
        None, "--deleted-by", help="Acting agent id (defaults to resolved staff id)."
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Delete an asset. The acting agent must be active with Manage Assets permission."""
    obj = get_ctx(ctx)
    deleted_by = obj.require_staff_id(deleted_by)
    if not yes:
        confirm(f"Delete asset {asset_id}?")
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
    type_id: int = typer.Argument(..., min=1, help="Asset type id."),
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
        None, "--asset-type", min=1, help="Asset type id; defaults to the first type."
    ),
    page: int = typer.Option(1, "--page", min=1, help="Page number to fetch."),
    size: int = typer.Option(10, "--size", min=1, max=50, help="Records per page."),
) -> None:
    """List asset custom field definitions for one asset type."""
    obj = get_ctx(ctx)
    params = compact({"asset_type": asset_type, "size": size, "page": page})
    body = obj.paginate("asset_custom_fields/", params=params)
    obj.render_list(body)


@custom_fields.command("get")
def get_asset_custom_field(
    ctx: typer.Context,
    field_id: int = typer.Argument(..., min=1, help="Asset custom field id."),
) -> None:
    """Show a single asset custom field definition."""
    obj = get_ctx(ctx)
    data = obj.call("GET", f"asset_custom_field/{field_id}/")
    obj.render(data)


app.add_typer(custom_fields, name="custom-fields")
