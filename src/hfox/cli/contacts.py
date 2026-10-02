"""Manage HappyFox contacts (the users endpoint) and contact groups (the `groups` sub-app)."""

from __future__ import annotations

import typer

from ..core.errors import ValidationError
from ._util import (
    NOT_IN_GROUP,
    compact,
    exit_on_failures,
    filter_help,
    filter_rows,
    nonblank_or_none,
    require_nonblank,
    split_csv,
    split_csv_ints,
    validate_contact_ref,
)
from .cf import CF_HELP, CF_JSON_HELP, parse_cf_json, parse_cf_options
from .context import get_ctx

app = typer.Typer(no_args_is_help=True, help="Manage contacts and contact groups.")

# Documented phone types: mobile, work, main, home, other.
PHONE_TYPES = ("mo", "w", "m", "h", "o")


def _validate_phone_type(phone_type: str | None) -> None:
    if phone_type is not None and phone_type not in PHONE_TYPES:
        raise ValidationError(
            f"Invalid phone type '{phone_type}'. Must be one of: {', '.join(PHONE_TYPES)}."
        )


def _single_phone(values: list[str] | None) -> str | None:
    """Return the one --phone value, or None when it is absent or blank."""
    if values and len(values) > 1:
        raise ValidationError("--phone takes one number per call; use create-bulk for more.")
    return nonblank_or_none(values[0] if values else None)


def _login_flag(set_login: bool | None) -> str | None:
    if set_login is None:
        return None
    return "TRUE" if set_login else "FALSE"


def _domains(value: str | None) -> str | None:
    """Normalize --domains to the documented comma string; '' clears."""
    if value is None:
        return None
    return ",".join(split_csv(value) or [])


@app.command("list")
def list_contacts(
    ctx: typer.Context,
    query: str = typer.Option(
        None,
        "--query",
        "-q",
        help=(
            "field:value filters on name, email, phone, updated_since or created_since, "
            "ANDed when space-separated; omit '+' from phone numbers."
        ),
    ),
    page: int = typer.Option(1, "--page", min=1, help="Page number to fetch."),
    size: int = typer.Option(10, "--size", min=1, max=50, help="Records per page."),
) -> None:
    """List or search contacts."""
    obj = get_ctx(ctx)
    params = compact({"q": nonblank_or_none(query), "page": page, "size": size})
    body = obj.paginate("users/", params=params)
    obj.render_list(body)


@app.command("get")
def get_contact(
    ctx: typer.Context,
    contact: str = typer.Argument(..., help="Contact id or email address."),
) -> None:
    """Fetch a single contact by id or email."""
    obj = get_ctx(ctx)
    data = obj.call("GET", f"user/{validate_contact_ref(contact)}/")
    obj.render(data)


@app.command("create")
def create_contact(
    ctx: typer.Context,
    name: str = typer.Option(..., "--name", help="Contact's full name."),
    email: str = typer.Option(None, "--email", help="Email address; required unless --phone."),
    phone: list[str] = typer.Option(None, "--phone", help="Phone number (one per call)."),
    phone_type: str = typer.Option(
        None, "--phone-type", help="Phone type: mo, w, m, h or o (default)."
    ),
    set_login: bool = typer.Option(
        None, "--set-login/--no-set-login", help="Enable or disable portal login."
    ),
    cf: list[str] = typer.Option(None, "--cf", help=CF_HELP),
    cf_json: str = typer.Option(None, "--cf-json", help=CF_JSON_HELP),
) -> None:
    """Add a contact, or edit the one with the same email. Unsent custom fields are reset."""
    obj = get_ctx(ctx)
    require_nonblank(name, "--name")
    number = _single_phone(phone)
    email = nonblank_or_none(email)
    if email is None and number is None:
        raise ValidationError("Provide --email or --phone.")
    _validate_phone_type(phone_type)
    if phone_type is not None and number is None:
        raise ValidationError("--phone-type requires --phone.")
    # The email key is required even for a phone-only contact, as null.
    body = {"name": name, "email": email}
    if number is not None:
        body["phones"] = [{"type": phone_type or "o", "number": number, "is_primary": True}]
    body.update(compact({"is_login_enabled": _login_flag(set_login)}))
    body.update(parse_cf_options(cf, prefix="c-cf-"))
    body.update(parse_cf_json(cf_json, "c-cf-"))
    result = obj.call("POST", "users/", json=body)
    obj.render(result)


@app.command("update")
def update_contact(
    ctx: typer.Context,
    contact_id: str = typer.Argument(
        ..., help="Contact id; an email address is documented only with --set-login."
    ),
    name: str = typer.Option(None, "--name", help="New name."),
    email: str = typer.Option(None, "--email", help="New email address."),
    phone: list[str] = typer.Option(None, "--phone", help="Phone number to add or edit."),
    phone_id: int = typer.Option(
        None, "--phone-id", min=1, help="Id of the phone record to edit; omit to add a phone."
    ),
    phone_type: str = typer.Option(
        None, "--phone-type", help="Phone type: mo, w, m, h or o; required with --phone-id."
    ),
    primary: bool = typer.Option(
        None, "--primary/--no-primary", help="Set whether the phone is primary."
    ),
    cf: list[str] = typer.Option(None, "--cf", help=CF_HELP),
    cf_json: str = typer.Option(None, "--cf-json", help=CF_JSON_HELP),
    set_login: bool = typer.Option(
        None, "--set-login/--no-set-login", help="Enable or disable portal login."
    ),
) -> None:
    """Edit a contact. Only the fields you pass are sent; unsent custom fields may still reset."""
    obj = get_ctx(ctx)
    contact_id = validate_contact_ref(contact_id)
    number = _single_phone(phone)
    _validate_phone_type(phone_type)
    if number is None and any(v is not None for v in (phone_id, phone_type, primary)):
        raise ValidationError("--phone-id, --phone-type and --primary require --phone.")
    # An edited phone without a type would fall back to the documented default 'o'.
    if phone_id is not None and phone_type is None:
        raise ValidationError("--phone-id requires --phone-type.")
    phones = None
    if number is not None:
        entry = {"id": phone_id, "type": phone_type, "number": number, "is_primary": primary}
        phones = [compact(entry)]
    body = compact(
        {
            "name": name,
            "email": email,
            "phones": phones,
            "is_login_enabled": _login_flag(set_login),
        }
    )
    body.update(parse_cf_options(cf, prefix="c-cf-"))
    body.update(parse_cf_json(cf_json, "c-cf-"))
    if not body:
        raise ValidationError("Nothing to update; pass at least one field.")
    result = obj.call("POST", f"user/{contact_id}/", json=body)
    obj.render(result)


@app.command("create-bulk")
def create_bulk(
    ctx: typer.Context,
    file: str = typer.Option(
        ...,
        "--file",
        help=(
            "JSON array of users/ payloads; a phones entry with an id edits that phone. "
            "'-' reads stdin."
        ),
    ),
) -> None:
    """Add or edit up to 100 contacts from a JSON file. Unsent custom fields are reset."""
    obj = get_ctx(ctx)
    payload = obj.read_json(file, "--file")
    if not isinstance(payload, list):
        raise ValidationError("Bulk file must contain a JSON array of contacts.")
    if not (1 <= len(payload) <= 100):
        raise ValidationError("Bulk create accepts between 1 and 100 contacts.")
    result = obj.call("POST", "users/", json=payload)
    obj.render(result)
    exit_on_failures(result)


groups = typer.Typer(no_args_is_help=True, help="Manage contact groups.")
app.add_typer(groups, name="groups")


@groups.command("list")
def list_groups(
    ctx: typer.Context,
    name: str = typer.Option(None, "--name", help=filter_help("name")),
) -> None:
    """List all contact groups."""
    obj = get_ctx(ctx)
    obj.render_list(filter_rows(obj.call("GET", "contact_groups/"), {"name": name}))


@groups.command("get")
def get_group(
    ctx: typer.Context,
    group_id: int = typer.Argument(..., min=1, help="Id of the contact group."),
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
    domains: str = typer.Option(None, "--domains", help="Comma-separated tagged domains."),
) -> None:
    """Create a contact group."""
    obj = get_ctx(ctx)
    require_nonblank(name, "--name")
    body = compact(
        {"name": name, "description": description, "tagged_domains": _domains(domains)}
    )
    result = obj.call("POST", "contact_groups/", json=body)
    obj.render(result)


@groups.command("update")
def update_group(
    ctx: typer.Context,
    group_id: int = typer.Argument(..., min=1, help="Id of the contact group to update."),
    description: str = typer.Option(None, "--description", help="New description."),
    domains: str = typer.Option(
        None, "--domains", help="Comma-separated tagged domains; '' clears them."
    ),
) -> None:
    """Update a contact group."""
    obj = get_ctx(ctx)
    body = compact({"description": description, "tagged_domains": _domains(domains)})
    if not body:
        raise ValidationError("Nothing to update; pass at least one field.")
    result = obj.call("POST", f"contact_group/{group_id}/", json=body)
    obj.render(result)


@groups.command("add-contacts")
def add_contacts(
    ctx: typer.Context,
    group_id: int = typer.Argument(..., min=1, help="Id of the contact group."),
    contacts: str = typer.Option(..., "--contacts", help="Comma-separated contact ids (max 100)."),
    access_tickets: bool = typer.Option(
        None,
        "--access-tickets/--no-access-tickets",
        help="Let every listed contact see tickets of all contacts in the group.",
    ),
) -> None:
    """Add or edit contacts of a contact group."""
    obj = get_ctx(ctx)
    contact_ids = split_csv_ints(contacts)
    if not contact_ids or len(contact_ids) > 100:
        raise ValidationError("Adding contacts accepts between 1 and 100 contact ids.")
    payload = [
        compact({"contact": cid, "access_tickets": access_tickets}) for cid in contact_ids
    ]
    result = obj.call("POST", f"contact_group/{group_id}/update_contacts/", json=payload)
    obj.render(result)
    exit_on_failures(result)


@groups.command("remove-contacts")
def remove_contacts(
    ctx: typer.Context,
    group_id: int = typer.Argument(..., min=1, help="Id of the contact group."),
    contacts: str = typer.Option(..., "--contacts", help="Comma-separated contact ids."),
) -> None:
    """Remove contacts from a contact group."""
    obj = get_ctx(ctx)
    contact_ids = split_csv_ints(contacts)
    if not contact_ids:
        raise ValidationError("Provide at least one contact id.")
    body = {"contacts": contact_ids}
    result = obj.call("POST", f"contact_group/{group_id}/delete_contacts/", json=body)
    obj.render(result)
    exit_on_failures(result, benign=NOT_IN_GROUP)
