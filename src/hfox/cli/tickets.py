"""`hfox tickets`: list, read, create and act on tickets."""

from __future__ import annotations

import mimetypes
from typing import Any

import typer

from ..core.errors import ValidationError
from . import output
from ._util import (
    STAFF_HELP,
    STAFF_ID_HELP,
    YES_HELP,
    comma_join,
    compact,
    exit_on_failures,
    nonblank_or_none,
    parse_json,
    require_nonblank,
    split_csv,
    split_csv_ints,
    text_file_help,
    validate_ticket_id,
)
from .cf import CF_HELP, CF_JSON_HELP, parse_cf_json, parse_cf_options
from .context import get_ctx

app = typer.Typer(no_args_is_help=True, help="Manage tickets.")

TICKET_HELP = "Numeric ticket id, not the display id."
CONTACT_CF_HELP = "Contact custom field; same syntax as --cf."
CONTACT_CF_JSON_HELP = "Contact custom fields; same syntax as --cf-json."
UPDATE_TAGS_HELP = "Comma-separated tags (see 'tickets tags' to add or remove)."
UNASSIGN_HELP = "Clear the assignee."
CREATE_UNASSIGN_HELP = "Leave the ticket unassigned."
CHOICES_FILE_HELP = (
    "JSON choices; each existing choice keeps its id, a new one has id null, and an "
    "existing choice not listed is deleted. '-' reads stdin."
)
CHOICE_ID_HINT = "Use the id from `hfox system ticket-custom-fields`, or null for a new choice."

# Keys that make a staff reply or note change the ticket without a message.
_PROPERTY_KEYS = ("status", "priority", "assignee", "time_spent", "due_date", "tags")
_UPDATE_CF_PREFIXES = ("t-cf-", "ccf-")


def _check_unassign(unassign: bool, assignee: int | None, attachment: list[str] | None) -> None:
    if unassign and assignee is not None:
        raise ValidationError("--unassign cannot be combined with --assignee.")
    if unassign and attachment:
        raise ValidationError("--unassign cannot be combined with --attachment.")


def _check_update(body: dict[str, Any], noun: str, attachment: list[str] | None) -> None:
    """Require a message, a file or a property, and reject a null alongside a file."""
    if attachment and None in body.values():
        # Multipart form data would drop the null that clears a field.
        raise ValidationError("A null value cannot be combined with --attachment.")
    if attachment or body.get("html") or body.get("plaintext"):
        return
    if any(k in _PROPERTY_KEYS or k.startswith(_UPDATE_CF_PREFIXES) for k in body):
        return
    raise ValidationError(
        f"Provide a {noun} body with --text/--html, an --attachment, or a property to change."
    )


@app.command("list")
def list_tickets(
    ctx: typer.Context,
    status: str = typer.Option(
        None, "--status", help="Status id, '_all' or '_pending'; -q defaults to '_all'."
    ),
    category: list[int] = typer.Option(
        None, "--category", min=1, help="Category id (repeatable)."
    ),
    query: str = typer.Option(
        None,
        "--query",
        "-q",
        help="Search text or filters, e.g. 'status:\"In Progress\",\"New\"'.",
    ),
    sort: str = typer.Option(
        None,
        "--sort",
        help="Sort key (e.g. 'updated', 'created', 'priorityd', 'last_modifiedd').",
    ),
    minify: bool = typer.Option(False, "--minify", help="Return only ticket ids."),
    fields: str = typer.Option(
        None, "--fields", help="Comma-separated top-level ticket fields."
    ),
    page: int = typer.Option(1, "--page", min=1, help="Page number to fetch."),
    size: int = typer.Option(10, "--size", min=1, max=50, help="Results per page."),
):
    """List, search or filter tickets."""
    obj = get_ctx(ctx)
    query = nonblank_or_none(query)
    if query and not status:
        # The documented search URL is tickets/?status=_all&q=...
        status = "_all"
    params = compact(
        {
            "status": status,
            "category": category or None,
            "q": query,
            "sort": sort,
            "minify_response": minify or None,
            "fields": comma_join(split_csv(fields)),
            "page": page,
            "size": size,
        }
    )
    body = obj.paginate("tickets/", params=params)
    obj.render_list(body)


@app.command("get")
def get_ticket(
    ctx: typer.Context,
    ticket_id: str = typer.Argument(..., help=TICKET_HELP),
    show_cf_changes: bool = typer.Option(
        False, "--show-cf-changes", help="Include custom-field change history."
    ),
):
    """Fetch one ticket."""
    obj = get_ctx(ctx)
    validate_ticket_id(ticket_id)
    params = compact({"show_cf_changes": show_cf_changes or None})
    data = obj.call("GET", f"ticket/{ticket_id}/", params=params)
    obj.render(data)


@app.command("create")
def create_ticket(
    ctx: typer.Context,
    subject: str = typer.Option(..., "--subject", help="Ticket subject."),
    category: int = typer.Option(
        ..., "--category", help="Public category id (see hfox system categories)."
    ),
    name: str = typer.Option(None, "--name", help="Contact name (for a new contact)."),
    email: str = typer.Option(None, "--email", help="Contact email (for a new contact)."),
    client: int = typer.Option(None, "--client", help="Existing contact (client) id."),
    phone: str = typer.Option(None, "--phone", help="Contact phone number."),
    text: str = typer.Option(None, "--text", help="Plain-text ticket body."),
    text_file: str = typer.Option(
        None, "--text-file", help=text_file_help("plain-text ticket body")
    ),
    html: str = typer.Option(None, "--html", help="HTML ticket body."),
    html_file: str = typer.Option(
        None, "--html-file", help=text_file_help("HTML ticket body")
    ),
    priority: int = typer.Option(None, "--priority", help="Priority id."),
    assignee: int = typer.Option(None, "--assignee", help="Assignee staff id."),
    unassign: bool = typer.Option(False, "--unassign", help=CREATE_UNASSIGN_HELP),
    tags: str = typer.Option(None, "--tags", help="Comma-separated tags."),
    cc: str = typer.Option(None, "--cc", help="Comma-separated CC addresses."),
    bcc: str = typer.Option(None, "--bcc", help="Comma-separated BCC addresses."),
    created_at: str = typer.Option(None, "--created-at", help="Override creation timestamp."),
    due_date: str = typer.Option(None, "--due-date", help="Due date."),
    visible_only_staff: bool = typer.Option(
        False, "--visible-only-staff", help="Create the ticket as private (staff only)."
    ),
    cf: list[str] = typer.Option(None, "--cf", help=CF_HELP),
    cf_json: str = typer.Option(None, "--cf-json", help=CF_JSON_HELP),
    contact_cf: list[str] = typer.Option(None, "--contact-cf", help=CONTACT_CF_HELP),
    contact_cf_json: str = typer.Option(
        None, "--contact-cf-json", help=CONTACT_CF_JSON_HELP
    ),
    attachment: list[str] = typer.Option(
        None, "--attachment", help="File path to attach (repeatable)."
    ),
):
    """Create a ticket for a new or existing contact."""
    obj = get_ctx(ctx)
    require_nonblank(subject, "--subject")
    _check_unassign(unassign, assignee, attachment)
    text = nonblank_or_none(obj.text_input(text, text_file, "--text"))
    html = nonblank_or_none(obj.text_input(html, html_file, "--html"))
    name, email = nonblank_or_none(name), nonblank_or_none(email)
    if not (text or html):
        raise ValidationError("Provide a ticket body with --text or --html.")
    if not ((name and email) or client):
        raise ValidationError(
            "Identify the contact: pass both --name and --email, or --client <id>."
        )
    body = compact(
        {
            "name": name,
            "email": email,
            "client": client,
            "phone": phone,
            "subject": subject,
            "text": text,
            "html": html,
            "category": category,
            "priority": priority,
            "assignee": assignee,
            "tags": comma_join(split_csv(tags)),
            "cc": comma_join(split_csv(cc)),
            "bcc": comma_join(split_csv(bcc)),
            "created_at": created_at,
            "due_date": due_date,
            "visible_only_staff": visible_only_staff or None,
        }
    )
    if unassign:
        body["assignee"] = None
    body.update(parse_cf_options(cf, prefix="t-cf-", allowed=("t-cf-", "c-cf-")))
    body.update(parse_cf_json(cf_json, "t-cf-", ("t-cf-", "c-cf-")))
    body.update(parse_cf_options(contact_cf, prefix="c-cf-", json_flag="--contact-cf-json"))
    body.update(parse_cf_json(contact_cf_json, "c-cf-", flag="--contact-cf-json"))
    result = obj.call("POST", "tickets/", **obj.attach(body, attachment))
    obj.render(result)
    if isinstance(result, dict) and result.get("display_id"):
        obj.success(f"{output.fox()}Created ticket {result['display_id']}.")


@app.command("create-bulk")
def create_bulk(
    ctx: typer.Context,
    file: str = typer.Option(
        ..., "--file", help="JSON array of ticket objects; '-' reads stdin."
    ),
):
    """Create up to 100 tickets from a JSON array file."""
    obj = get_ctx(ctx)
    payload = obj.read_json(file, "--file")
    if not isinstance(payload, list):
        raise ValidationError("Bulk file must contain a JSON array of ticket objects.")
    if not (1 <= len(payload) <= 100):
        raise ValidationError("Bulk create accepts between 1 and 100 tickets.")
    result = obj.call("POST", "tickets/", json=payload)
    obj.render(result)
    exit_on_failures(result)


@app.command("inline-attachment")
def inline_attachment(
    ctx: typer.Context,
    file: str = typer.Argument(..., help="Image file, 25 MB max."),
):
    """Upload an image for an HTML ticket body; returns its temporary url."""
    obj = get_ctx(ctx)
    mime, _ = mimetypes.guess_type(file)
    if not (mime and mime.startswith("image/")):
        raise ValidationError(f"Inline attachments must be image files: {file}")
    # The documented path has no trailing slash.
    result = obj.call(
        "POST", "ticket-inline-attachment", **obj.attach({}, [file], field="file")
    )
    obj.render(result)


@app.command("reply")
def reply(
    ctx: typer.Context,
    ticket_id: str = typer.Argument(..., help=TICKET_HELP),
    staff: str = typer.Option(None, "--staff", help=STAFF_HELP),
    staff_id: int = typer.Option(None, "--staff-id", min=0, help=STAFF_ID_HELP),
    html: str = typer.Option(None, "--html", help="HTML reply body."),
    html_file: str = typer.Option(
        None, "--html-file", help=text_file_help("HTML reply body")
    ),
    text: str = typer.Option(None, "--text", help="Plain-text reply body."),
    text_file: str = typer.Option(
        None, "--text-file", help=text_file_help("plain-text reply body")
    ),
    status: int = typer.Option(None, "--status", help="New status id."),
    priority: int = typer.Option(None, "--priority", help="New priority id."),
    assignee: int = typer.Option(None, "--assignee", help="New assignee staff id."),
    unassign: bool = typer.Option(False, "--unassign", help=UNASSIGN_HELP),
    time_spent: int = typer.Option(
        None, "--time-spent", help="Minutes spent; some categories require it."
    ),
    due_date: str = typer.Option(None, "--due-date", help="Due date."),
    tags: str = typer.Option(None, "--tags", help=UPDATE_TAGS_HELP),
    cc: str = typer.Option(None, "--cc", help="Comma-separated CC addresses."),
    bcc: str = typer.Option(None, "--bcc", help="Comma-separated BCC addresses."),
    subject: str = typer.Option(None, "--subject", help="Override the reply subject."),
    update_customer: bool = typer.Option(
        False, "--update-customer", help="Notify the customer of this reply."
    ),
    send_survey: bool = typer.Option(
        False, "--send-survey", help="Send a satisfaction survey."
    ),
    last_staff_message: int = typer.Option(
        None, "--last-staff-message", help="Id of the last staff message seen."
    ),
    parent_update: int = typer.Option(
        None,
        "--parent-update",
        help="Id of the parent update (for tickets from Facebook/Twitter conversations).",
    ),
    cf: list[str] = typer.Option(None, "--cf", help=CF_HELP),
    cf_json: str = typer.Option(None, "--cf-json", help=CF_JSON_HELP),
    contact_cf: list[str] = typer.Option(None, "--contact-cf", help=CONTACT_CF_HELP),
    contact_cf_json: str = typer.Option(
        None, "--contact-cf-json", help=CONTACT_CF_JSON_HELP
    ),
    attachment: list[str] = typer.Option(
        None, "--attachment", help="File path to attach (repeatable)."
    ),
):
    """Post a public staff reply to a ticket, or change its properties."""
    obj = get_ctx(ctx)
    validate_ticket_id(ticket_id)
    _check_unassign(unassign, assignee, attachment)
    body = compact(
        {
            "html": nonblank_or_none(obj.text_input(html, html_file, "--html")),
            "plaintext": nonblank_or_none(obj.text_input(text, text_file, "--text")),
            "status": status,
            "priority": priority,
            "assignee": assignee,
            "time_spent": time_spent,
            "due_date": due_date,
            "tags": comma_join(split_csv(tags)),
            "cc": comma_join(split_csv(cc)),
            "bcc": comma_join(split_csv(bcc)),
            "subject": subject,
            "update_customer": update_customer or None,
            "send_survey": send_survey or None,
            "last_staff_message": last_staff_message,
            "parent_update": parent_update,
        }
    )
    if unassign:
        body["assignee"] = None
    body.update(parse_cf_options(cf, prefix="t-cf-", allowed=("t-cf-", "ccf-")))
    body.update(parse_cf_json(cf_json, "t-cf-", ("t-cf-", "ccf-")))
    body.update(parse_cf_options(contact_cf, prefix="ccf-", json_flag="--contact-cf-json"))
    body.update(parse_cf_json(contact_cf_json, "ccf-", flag="--contact-cf-json"))
    _check_update(body, "reply", attachment)
    body = {"staff": obj.require_staff_id(staff_id, staff), **body}
    result = obj.call(
        "POST", f"ticket/{ticket_id}/staff_update/", **obj.attach(body, attachment)
    )
    obj.render(result)


@app.command("note")
def note(
    ctx: typer.Context,
    ticket_id: str = typer.Argument(..., help=TICKET_HELP),
    staff: str = typer.Option(None, "--staff", help=STAFF_HELP),
    staff_id: int = typer.Option(None, "--staff-id", min=0, help=STAFF_ID_HELP),
    html: str = typer.Option(None, "--html", help="HTML note body."),
    html_file: str = typer.Option(
        None, "--html-file", help=text_file_help("HTML note body")
    ),
    text: str = typer.Option(None, "--text", help="Plain-text note body."),
    text_file: str = typer.Option(
        None, "--text-file", help=text_file_help("plain-text note body")
    ),
    alert: str = typer.Option(
        None,
        "--alert",
        help="Alert 's' (all subscribers), 'c' (category agents) or an agent id.",
    ),
    status: int = typer.Option(None, "--status", help="New status id."),
    priority: int = typer.Option(None, "--priority", help="New priority id."),
    assignee: int = typer.Option(None, "--assignee", help="New assignee staff id."),
    unassign: bool = typer.Option(False, "--unassign", help=UNASSIGN_HELP),
    time_spent: int = typer.Option(
        None, "--time-spent", help="Minutes spent; some categories require it."
    ),
    due_date: str = typer.Option(None, "--due-date", help="Due date."),
    tags: str = typer.Option(None, "--tags", help=UPDATE_TAGS_HELP),
    cf: list[str] = typer.Option(None, "--cf", help=CF_HELP),
    cf_json: str = typer.Option(None, "--cf-json", help=CF_JSON_HELP),
    contact_cf: list[str] = typer.Option(None, "--contact-cf", help=CONTACT_CF_HELP),
    contact_cf_json: str = typer.Option(
        None, "--contact-cf-json", help=CONTACT_CF_JSON_HELP
    ),
    attachment: list[str] = typer.Option(
        None, "--attachment", help="File path to attach (repeatable)."
    ),
):
    """Add a private note to a ticket, or change its properties."""
    obj = get_ctx(ctx)
    validate_ticket_id(ticket_id)
    _check_unassign(unassign, assignee, attachment)
    body = compact(
        {
            "html": nonblank_or_none(obj.text_input(html, html_file, "--html")),
            "plaintext": nonblank_or_none(obj.text_input(text, text_file, "--text")),
            "alert": alert,
            "status": status,
            "priority": priority,
            "assignee": assignee,
            "time_spent": time_spent,
            "due_date": due_date,
            "tags": comma_join(split_csv(tags)),
        }
    )
    if unassign:
        body["assignee"] = None
    body.update(parse_cf_options(cf, prefix="t-cf-", allowed=("t-cf-", "ccf-")))
    body.update(parse_cf_json(cf_json, "t-cf-", ("t-cf-", "ccf-")))
    body.update(parse_cf_options(contact_cf, prefix="ccf-", json_flag="--contact-cf-json"))
    body.update(parse_cf_json(contact_cf_json, "ccf-", flag="--contact-cf-json"))
    _check_update(body, "note", attachment)
    body = {"staff": obj.require_staff_id(staff_id, staff), **body}
    result = obj.call(
        "POST", f"ticket/{ticket_id}/staff_pvtnote/", **obj.attach(body, attachment)
    )
    obj.render(result)


@app.command("user-reply")
def user_reply(
    ctx: typer.Context,
    ticket_id: str = typer.Argument(..., help=TICKET_HELP),
    user: int = typer.Option(..., "--user", help="Contact (user) id posting the reply."),
    text: str = typer.Option(None, "--text", help="Reply body."),
    text_file: str = typer.Option(None, "--text-file", help=text_file_help("reply body")),
    cc: str = typer.Option(None, "--cc", help="Comma-separated CC addresses."),
    bcc: str = typer.Option(None, "--bcc", help="Comma-separated BCC addresses."),
    attachment: list[str] = typer.Option(
        None, "--attachment", help="File path to attach (repeatable)."
    ),
):
    """Post a reply to a ticket as the contact (user)."""
    obj = get_ctx(ctx)
    validate_ticket_id(ticket_id)
    text = obj.text_input(text, text_file, "--text")
    if text is None:
        raise ValidationError("Provide the reply body with --text or --text-file.")
    require_nonblank(text, "--text")
    body = compact(
        {
            "user": user,
            "text": text,
            "cc": comma_join(split_csv(cc)),
            "bcc": comma_join(split_csv(bcc)),
        }
    )
    result = obj.call(
        "POST", f"ticket/{ticket_id}/user_reply/", **obj.attach(body, attachment)
    )
    obj.render(result)


@app.command("update")
def update(
    ctx: typer.Context,
    ticket_id: str = typer.Argument(..., help=TICKET_HELP),
    status: int = typer.Option(None, "--status", help="New status id."),
    priority: int = typer.Option(None, "--priority", help="New priority id."),
    assignee: int = typer.Option(None, "--assignee", help="New assignee staff id."),
    unassign: bool = typer.Option(False, "--unassign", help=UNASSIGN_HELP),
    due_date: str = typer.Option(None, "--due-date", help="Due date."),
    tags: str = typer.Option(None, "--tags", help=UPDATE_TAGS_HELP),
    time_spent: int = typer.Option(
        None, "--time-spent", help="Minutes spent; some categories require it."
    ),
    cf: list[str] = typer.Option(None, "--cf", help=CF_HELP),
    cf_json: str = typer.Option(None, "--cf-json", help=CF_JSON_HELP),
    staff: str = typer.Option(None, "--staff", help=STAFF_HELP),
    staff_id: int = typer.Option(None, "--staff-id", min=0, help=STAFF_ID_HELP),
):
    """Change ticket properties without posting a message."""
    obj = get_ctx(ctx)
    validate_ticket_id(ticket_id)
    _check_unassign(unassign, assignee, None)
    body = compact(
        {
            "status": status,
            "priority": priority,
            "assignee": assignee,
            "time_spent": time_spent,
            "due_date": due_date,
            "tags": comma_join(split_csv(tags)),
        }
    )
    if unassign:
        body["assignee"] = None
    body.update(parse_cf_options(cf, prefix="t-cf-"))
    body.update(parse_cf_json(cf_json, "t-cf-"))
    if not body:
        raise ValidationError("Provide at least one property to change.")
    body = {"staff": obj.require_staff_id(staff_id, staff), **body}
    result = obj.call("POST", f"ticket/{ticket_id}/staff_update/", json=body)
    obj.render(result)


@app.command("update-cf")
def update_cf(
    ctx: typer.Context,
    ticket_id: str = typer.Argument(..., help=TICKET_HELP),
    staff: str = typer.Option(None, "--staff", help=STAFF_HELP),
    staff_id: int = typer.Option(None, "--staff-id", min=0, help=STAFF_ID_HELP),
    cf: list[str] = typer.Option(None, "--cf", help=CF_HELP),
    cf_json: str = typer.Option(None, "--cf-json", help=CF_JSON_HELP),
):
    """Update one or more custom fields on a ticket."""
    obj = get_ctx(ctx)
    validate_ticket_id(ticket_id)
    fields = parse_cf_options(cf, prefix="t-cf-")
    fields.update(parse_cf_json(cf_json, "t-cf-"))
    if not fields:
        raise ValidationError("Provide at least one --cf '<id>=<value>' or --cf-json.")
    body = compact({"staff": obj.require_staff_id(staff_id, staff)})
    body.update(fields)
    result = obj.call("POST", f"ticket/{ticket_id}/update_custom_fields/", json=body)
    obj.render(result)


@app.command("tags")
def tags(
    ctx: typer.Context,
    ticket_id: str = typer.Argument(..., help=TICKET_HELP),
    add: str = typer.Option(None, "--add", help="Comma-separated tags to add."),
    remove: str = typer.Option(None, "--remove", help="Comma-separated tags to remove."),
    staff: str = typer.Option(None, "--staff", help=STAFF_HELP),
    staff_id: int = typer.Option(None, "--staff-id", min=0, help=STAFF_ID_HELP),
):
    """Add and/or remove tags on a ticket."""
    obj = get_ctx(ctx)
    validate_ticket_id(ticket_id)
    add_tags = comma_join(split_csv(add))
    remove_tags = comma_join(split_csv(remove))
    if not (add_tags or remove_tags):
        raise ValidationError("Provide tags to --add and/or --remove.")
    body = compact(
        {
            "add": add_tags,
            "remove": remove_tags,
            "staff_id": obj.require_staff_id(staff_id, staff),
        }
    )
    result = obj.call("POST", f"ticket/{ticket_id}/update_tags/", json=body)
    obj.render(result)


@app.command("subscribe")
def subscribe(
    ctx: typer.Context,
    ticket_id: str = typer.Argument(..., help=TICKET_HELP),
    staff: str = typer.Option(
        None, "--staff", help="Agent to subscribe, by email or name."
    ),
    staff_id: int = typer.Option(
        None, "--staff-id", min=0, help="Agent id to subscribe (defaults to your staff id)."
    ),
    agents: str = typer.Option(
        None, "--agents", help="More agent ids to subscribe, comma-separated."
    ),
):
    """Subscribe agents to a ticket."""
    obj = get_ctx(ctx)
    validate_ticket_id(ticket_id)
    body = compact(
        {
            "staff_id": obj.require_staff_id(staff_id, staff),
            "data": split_csv_ints(agents),
        }
    )
    result = obj.call("POST", f"ticket/{ticket_id}/subscribe/", json=body)
    obj.render(result)


@app.command("unsubscribe")
def unsubscribe(
    ctx: typer.Context,
    ticket_id: str = typer.Argument(..., help=TICKET_HELP),
    staff: str = typer.Option(
        None, "--staff", help="Agent to unsubscribe, by email or name."
    ),
    staff_id: int = typer.Option(
        None, "--staff-id", min=0, help="Agent id to unsubscribe (defaults to your staff id)."
    ),
):
    """Unsubscribe an agent from a ticket."""
    obj = get_ctx(ctx)
    validate_ticket_id(ticket_id)
    body = {"staff_id": obj.require_staff_id(staff_id, staff)}
    result = obj.call("POST", f"ticket/{ticket_id}/unsubscribe/", json=body)
    obj.render(result)


@app.command("forward")
def forward(
    ctx: typer.Context,
    ticket_id: str = typer.Argument(..., help=TICKET_HELP),
    to: str = typer.Option(..., "--to", help="Comma-separated recipient addresses."),
    subject: str = typer.Option(..., "--subject", help="Forward subject."),
    message: str = typer.Option(None, "--message", help="Forward message body."),
    message_file: str = typer.Option(
        None, "--message-file", help=text_file_help("forward message body")
    ),
    cc: str = typer.Option(None, "--cc", help="Comma-separated CC addresses."),
    bcc: str = typer.Option(None, "--bcc", help="Comma-separated BCC addresses."),
    staff: str = typer.Option(None, "--staff", help=STAFF_HELP),
    staff_id: int = typer.Option(None, "--staff-id", min=0, help=STAFF_ID_HELP),
    to_include_contact: bool = typer.Option(
        False, "--to-include-contact", help="Add the ticket contact to the To line."
    ),
    cc_include_contact: bool = typer.Option(
        False, "--cc-include-contact", help="Add the ticket contact to the Cc line."
    ),
    send_all_messages: bool = typer.Option(
        True, "--send-all-messages/--no-send-all-messages", help="Include all messages."
    ),
    include_private_notes: bool = typer.Option(
        False, "--include-private-notes", help="Include private notes in the forward."
    ),
    convert_replies: bool = typer.Option(
        True,
        "--convert-replies/--no-convert-replies",
        help="Convert replies into a new ticket.",
    ),
    ticket_attachments: str = typer.Option(
        None, "--ticket-attachments", help="Comma-separated existing attachment ids to include."
    ),
    attachment: list[str] = typer.Option(
        None, "--attachment", help="File path to attach (repeatable)."
    ),
):
    """Forward a ticket to one or more recipients."""
    obj = get_ctx(ctx)
    validate_ticket_id(ticket_id)
    recipients = comma_join(split_csv(to))
    if recipients is None:
        raise ValidationError("--to must list at least one address.")
    require_nonblank(subject, "--subject")
    message = obj.text_input(message, message_file, "--message")
    if message is None:
        raise ValidationError("Provide the forward message with --message or --message-file.")
    require_nonblank(message, "--message")
    body = compact(
        {
            "to": recipients,
            "subject": subject,
            "message": message,
            "cc": comma_join(split_csv(cc)),
            "bcc": comma_join(split_csv(bcc)),
            "staff_id": obj.require_staff_id(staff_id, staff),
            "to_include_ticket_contact": to_include_contact or None,
            "cc_include_ticket_contact": cc_include_contact or None,
            "send_all_messages": send_all_messages,
            "include_pvt_notes": include_private_notes or None,
            "convert_replies_as_new_ticket": convert_replies,
            "ticket_attachments": split_csv_ints(ticket_attachments),
        }
    )
    result = obj.call("POST", f"ticket/{ticket_id}/forward/", **obj.attach(body, attachment))
    obj.render(result)


@app.command("move")
def move(
    ctx: typer.Context,
    ticket_id: str = typer.Argument(..., help=TICKET_HELP),
    to_category: int = typer.Option(..., "--to-category", help="Target category id."),
    staff: str = typer.Option(None, "--staff", help=STAFF_HELP),
    staff_id: int = typer.Option(None, "--staff-id", min=0, help=STAFF_ID_HELP),
    note: str = typer.Option(None, "--note", help="Note to record with the move."),
    note_file: str = typer.Option(None, "--note-file", help=text_file_help("move note")),
    assign_to: int = typer.Option(None, "--assign-to", help="Assignee staff id after the move."),
):
    """Move a ticket to another category; the acting agent's role needs the move permission."""
    obj = get_ctx(ctx)
    validate_ticket_id(ticket_id)
    note = obj.text_input(note, note_file, "--note")
    body = compact(
        {
            "staff_id": obj.require_staff_id(staff_id, staff),
            "target_category_id": to_category,
            "move_note": note,
            "assign_to": assign_to,
        }
    )
    result = obj.call("POST", f"ticket/{ticket_id}/move/", json=body)
    obj.render(result)


@app.command("delete")
def delete(
    ctx: typer.Context,
    ticket_id: str = typer.Argument(..., help=TICKET_HELP),
    staff: str = typer.Option(None, "--staff", help=STAFF_HELP),
    staff_id: int = typer.Option(None, "--staff-id", min=0, help=STAFF_ID_HELP),
    yes: bool = typer.Option(False, "--yes", "-y", help=YES_HELP),
):
    """Delete a ticket (destructive)."""
    obj = get_ctx(ctx)
    validate_ticket_id(ticket_id)
    body = {"staff_id": obj.require_staff_id(staff_id, staff)}
    obj.confirm(f"Delete ticket {ticket_id}?", yes=yes)
    result = obj.call("POST", f"ticket/{ticket_id}/delete/", json=body)
    obj.success(f"Deleted ticket {ticket_id}.")
    obj.render(result)


def _check_choices(payload: Any) -> list[dict[str, Any]]:
    """Return the choice list of a payload given as an array or as {"choices": [...]}.

    Raises ValidationError unless every choice has a non-blank `text` and an `id` that is
    null or a unique whole number of at least 1.
    """
    choices = payload.get("choices") if isinstance(payload, dict) else payload
    if not isinstance(choices, list):
        raise ValidationError(
            'Choices must be a JSON array of choice objects, or an object {"choices": [...]}.'
        )
    seen: set[int] = set()
    for position, choice in enumerate(choices, start=1):
        if not isinstance(choice, dict):
            raise ValidationError(f"Choice {position} must be a JSON object.")
        text = choice.get("text")
        if not isinstance(text, str) or text.strip() == "":
            raise ValidationError(f"Choice {position} needs a non-blank string 'text'.")
        if "id" not in choice:
            raise ValidationError(f"Choice {position} has no 'id'.", hint=CHOICE_ID_HINT)
        choice_id = choice["id"]
        if choice_id is None:
            continue
        if not isinstance(choice_id, int) or isinstance(choice_id, bool) or choice_id < 1:
            raise ValidationError(
                f"Choice {position} has an invalid 'id'; expected a whole number of at "
                "least 1, or null.",
                hint=CHOICE_ID_HINT,
            )
        if choice_id in seen:
            raise ValidationError(f"Choice id {choice_id} is listed more than once.")
        seen.add(choice_id)
    return choices


@app.command("set-cf-choices")
def set_cf_choices(
    ctx: typer.Context,
    field_id: int = typer.Argument(..., min=1, help="Ticket custom field id."),
    file: str = typer.Option(None, "--file", help=CHOICES_FILE_HELP),
    choices_json: str = typer.Option(
        None, "--choices-json", help="The same JSON choices given inline."
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help=YES_HELP),
):
    """Replace the whole choice list of a ticket custom field (destructive)."""
    obj = get_ctx(ctx)
    if (file is None) == (choices_json is None):
        raise ValidationError("Provide the choices with exactly one of --file or --choices-json.")
    if file is not None:
        payload = obj.read_json(file, "--file")
    else:
        payload = parse_json(choices_json, "--choices-json")
    choices = _check_choices(payload)
    new = sum(1 for choice in choices if choice["id"] is None)
    summary = (
        f"Replace the choices of ticket custom field {field_id}: {len(choices) - new} kept "
        f"or edited by id, {new} new. Every other existing choice is deleted."
    )
    obj.confirm(summary, yes=yes)
    if yes and not obj.dry_run:
        output.warn(summary)
    # HappyFox's handling of an id it does not know is unverified until tested against a
    # live helpdesk.
    result = obj.call("PUT", f"ticket_custom_field/{field_id}/", json={"choices": choices})
    obj.render(result)
