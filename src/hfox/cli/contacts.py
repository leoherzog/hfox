"""Manage HappyFox contacts and contact groups.

Contacts live under the "users" endpoint in the HappyFox REST API. Contact
groups have their own "contact_groups"/"contact_group" endpoints and are exposed
here as the `groups` sub-app.
"""

from __future__ import annotations

import typer

from ..core.errors import ValidationError
from ._util import compact, load_json_file, split_csv, split_csv_ints
from .cf import parse_cf_json, parse_cf_options
from .context import get_ctx

app = typer.Typer(no_args_is_help=True, help="Manage contacts and contact groups.")

# Documented HappyFox phone types: mobile, work, main, home, other.
PHONE_TYPES = ("mo", "w", "m", "h", "o")


def _validate_phone_type(phone_type: str) -> None:
    if phone_type not in PHONE_TYPES:
        raise ValidationError(
            f"Invalid phone type '{phone_type}'. Must be one of: {', '.join(PHONE_TYPES)}."
        )


# -- contacts --------------------------------------------------------------
@app.command("list")
def list_contacts(
    ctx: typer.Context,
    query: str = typer.Option(
        None,
        "--query",
        "-q",
        help=(
            "Search term to filter contacts. Supports field:value syntax "
            "(e.g. 'name:adam', 'email:a@x.com', 'phone:5551234')."
        ),
    ),
    page: int = typer.Option(1, "--page", help="Page number to fetch."),
    size: int = typer.Option(10, "--size", help="Records per page."),
) -> None:
    """List contacts (optionally filtered by a search query)."""
    obj = get_ctx(ctx)
    body = obj.paginate(
        "users/",
        params=compact({"q": query, "page": page, "size": size}),
    )
    obj.render_list(body)


@app.command("get")
def get_contact(
    ctx: typer.Context,
    contact: str = typer.Argument(..., help="Contact id or email address."),
) -> None:
    """Fetch a single contact by id or email."""
    obj = get_ctx(ctx)
    data = obj.call("GET", f"user/{contact}/")
    obj.render(data)


@app.command("create")
def create_contact(
    ctx: typer.Context,
    name: str = typer.Option(..., "--name", help="Contact's full name."),
    email: str = typer.Option(None, "--email", help="Contact's email address."),
    phone: str = typer.Option(None, "--phone", help="Contact's phone number."),
    phone_type: str = typer.Option(
        "o",
        "--phone-type",
        help="Phone type: mo (mobile), w (work), m (main), h (home), o (other).",
    ),
    cf: list[str] = typer.Option(
        None, "--cf", help="Contact custom field as '<id>=<value>' (repeatable)."
    ),
    cf_json: str = typer.Option(
        None,
        "--cf-json",
        help=(
            "Custom fields as a JSON object mapping field id -> value, passed "
            "through with no coercion (escape hatch for explicit value types)."
        ),
    ),
) -> None:
    """Create a contact."""
    obj = get_ctx(ctx)
    # Email is required even if a phone is given, but it may be null. Allow
    # either, and build the phones payload when a phone number was provided.
    phones = None
    if phone is not None:
        _validate_phone_type(phone_type)
        phones = [{"type": phone_type, "number": phone, "is_primary": True}]
    body = compact({"name": name, "email": email, "phones": phones})
    if email is None and phone is not None:
        # HappyFox requires the email key even for phone-only contacts; it
        # accepts an explicit null, so add it back after compact() drops it.
        body["email"] = None
    body.update(parse_cf_options(cf, prefix="c-cf-"))
    body.update(parse_cf_json(cf_json, "c-cf-"))
    result = obj.call("POST", "users/", json=body)
    obj.render(result)


@app.command("update")
def update_contact(
    ctx: typer.Context,
    contact_id: str = typer.Argument(..., help="Id of the contact to update."),
    name: str = typer.Option(None, "--name", help="New name."),
    email: str = typer.Option(None, "--email", help="New email address."),
    phone: str = typer.Option(None, "--phone", help="New phone number."),
    phone_id: int = typer.Option(
        None,
        "--phone-id",
        help="Id of the existing phone record to edit (omit to add a new phone).",
    ),
    phone_type: str = typer.Option(
        "o",
        "--phone-type",
        help="Phone type: mo (mobile), w (work), m (main), h (home), o (other).",
    ),
    cf: list[str] = typer.Option(
        None, "--cf", help="Contact custom field as '<id>=<value>' (repeatable)."
    ),
    cf_json: str = typer.Option(
        None,
        "--cf-json",
        help=(
            "Custom fields as a JSON object mapping field id -> value, passed "
            "through with no coercion (escape hatch for explicit value types)."
        ),
    ),
    set_login: bool = typer.Option(
        None,
        "--set-login/--no-set-login",
        help="Enable or disable the contact's login.",
    ),
) -> None:
    """Update a contact (only the fields you pass are changed)."""
    obj = get_ctx(ctx)
    phones = None
    if phone is not None:
        _validate_phone_type(phone_type)
        # Including the existing record's id edits that phone in place; without
        # an id HappyFox adds the number as a NEW phone record.
        phones = [
            compact(
                {"id": phone_id, "type": phone_type, "number": phone, "is_primary": True}
            )
        ]
    # Only send provided fields — anything omitted from the payload is reset
    # by HappyFox, so compact() keeps us from clobbering untouched fields.
    is_login_enabled = None
    if set_login is not None:
        is_login_enabled = "TRUE" if set_login else "FALSE"
    body = compact(
        {
            "name": name,
            "email": email,
            "phones": phones,
            "is_login_enabled": is_login_enabled,
        }
    )
    body.update(parse_cf_options(cf, prefix="c-cf-"))
    body.update(parse_cf_json(cf_json, "c-cf-"))
    result = obj.call("POST", f"user/{contact_id}/", json=body)
    obj.render(result)


@app.command("create-bulk")
def create_bulk(
    ctx: typer.Context,
    file: str = typer.Option(
        ..., "--file", help="Path to a JSON file with an array of contacts to add/edit."
    ),
) -> None:
    """Create or edit multiple contacts from a JSON array file."""
    obj = get_ctx(ctx)
    payload = load_json_file(file)
    if not isinstance(payload, list):
        raise ValidationError("Bulk file must contain a JSON array of contacts.")
    if not (1 <= len(payload) <= 100):
        raise ValidationError("Bulk create accepts between 1 and 100 contacts.")
    result = obj.call("POST", "users/", json=payload)
    obj.render(result)


# -- contact groups --------------------------------------------------------
groups = typer.Typer(no_args_is_help=True, help="Manage contact groups.")
app.add_typer(groups, name="groups")


@groups.command("list")
def list_groups(ctx: typer.Context) -> None:
    """List all contact groups."""
    obj = get_ctx(ctx)
    body = obj.call("GET", "contact_groups/")
    obj.render_list(body)


@groups.command("get")
def get_group(
    ctx: typer.Context,
    group_id: str = typer.Argument(..., help="Id of the contact group."),
) -> None:
    """Fetch a single contact group."""
    obj = get_ctx(ctx)
    data = obj.call("GET", f"contact_group/{group_id}/")
    obj.render(data)


@groups.command("create")
def create_group(
    ctx: typer.Context,
    name: str = typer.Option(..., "--name", help="Contact group name."),
    description: str = typer.Option(None, "--description", help="Group description."),
    domains: str = typer.Option(
        None, "--domains", help="Comma-separated list of tagged domains."
    ),
) -> None:
    """Create a contact group."""
    obj = get_ctx(ctx)
    body = compact(
        {
            "name": name,
            "description": description,
            "tagged_domains": split_csv(domains),
        }
    )
    result = obj.call("POST", "contact_groups/", json=body)
    obj.render(result)


@groups.command("update")
def update_group(
    ctx: typer.Context,
    group_id: str = typer.Argument(..., help="Id of the contact group to update."),
    description: str = typer.Option(None, "--description", help="New description."),
    domains: str = typer.Option(
        None, "--domains", help="Comma-separated list of tagged domains."
    ),
) -> None:
    """Update a contact group."""
    obj = get_ctx(ctx)
    body = compact(
        {
            "description": description,
            "tagged_domains": split_csv(domains),
        }
    )
    result = obj.call("POST", f"contact_group/{group_id}/", json=body)
    obj.render(result)


@groups.command("add-contacts")
def add_contacts(
    ctx: typer.Context,
    group_id: str = typer.Argument(..., help="Id of the contact group."),
    contacts: str = typer.Option(
        ..., "--contacts", help="Comma-separated contact ids to add."
    ),
    access_tickets: bool = typer.Option(
        False,
        "--access-tickets/--no-access-tickets",
        help="Grant added contacts access to each other's tickets.",
    ),
) -> None:
    """Add contacts to a contact group."""
    obj = get_ctx(ctx)
    contact_ids = split_csv_ints(contacts)
    if not (1 <= len(contact_ids) <= 100):
        raise ValidationError("Adding contacts accepts between 1 and 100 contact ids.")
    # HappyFox wants a JSON array, one entry per contact.
    payload = [
        {"contact": cid, "access_tickets": access_tickets} for cid in contact_ids
    ]
    result = obj.call("POST", f"contact_group/{group_id}/update_contacts/", json=payload)
    obj.render(result)


@groups.command("remove-contacts")
def remove_contacts(
    ctx: typer.Context,
    group_id: str = typer.Argument(..., help="Id of the contact group."),
    contacts: str = typer.Option(
        ..., "--contacts", help="Comma-separated contact ids to remove."
    ),
) -> None:
    """Remove contacts from a contact group."""
    obj = get_ctx(ctx)
    body = {"contacts": split_csv_ints(contacts)}
    result = obj.call("POST", f"contact_group/{group_id}/delete_contacts/", json=body)
    obj.render(result)
