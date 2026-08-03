"""`hfox tickets` — create, read, and act on HappyFox tickets.

Listing goes through the plural `tickets/` collection; everything that touches a
single ticket uses the singular `ticket/<id>/...` form. Writes and single GETs
run through `obj.call`; the listing runs through `obj.paginate`, so every global
flag (--dry-run, --page-all, --format) is honored without any extra work here.
"""

from __future__ import annotations

import typer

from ..core.errors import ValidationError
from ._util import attach, comma_join, compact, load_json_file, split_csv, split_csv_ints
from .cf import parse_cf_json, parse_cf_options
from .context import get_ctx

app = typer.Typer(no_args_is_help=True, help="Manage tickets.")


@app.command("list")
def list_tickets(
    ctx: typer.Context,
    status: str = typer.Option(
        None,
        "--status",
        help="Filter by status id or name (defaults to '_all' when --query is given).",
    ),
    category: str = typer.Option(None, "--category", help="Filter by category id or name."),
    query: str = typer.Option(
        None,
        "--query",
        "-q",
        help=(
            "Full-text search query. Multiple words are ANDed: tickets must "
            "contain every word, in any order (quoting does not force an "
            "exact-phrase match)."
        ),
    ),
    sort: str = typer.Option(
        None,
        "--sort",
        help="Sort key (e.g. 'updated', 'created', 'priorityd', 'last_modifiedd').",
    ),
    minify: bool = typer.Option(
        False, "--minify", help="Return a minified (lighter) response."
    ),
    fields: str = typer.Option(
        None, "--fields", help="Comma-separated list of fields to include."
    ),
    page: int = typer.Option(1, "--page", help="Page number to fetch."),
    size: int = typer.Option(10, "--size", help="Results per page."),
):
    """List tickets, optionally filtered, searched, and paginated."""
    obj = get_ctx(ctx)
    if query and not status:
        # Search URLs are `?status=_all&q=...`: span all statuses unless narrowed.
        status = "_all"
    params = compact(
        {
            "status": status,
            "category": category,
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
    ticket_id: str = typer.Argument(..., help="Ticket id or display id."),
    show_cf_changes: bool = typer.Option(
        False, "--show-cf-changes", help="Include custom-field change history."
    ),
):
    """Fetch a single ticket by id."""
    obj = get_ctx(ctx)
    params = compact({"show_cf_changes": show_cf_changes or None})
    data = obj.call("GET", f"ticket/{ticket_id}/", params=params)
    obj.render(data)


@app.command("create")
def create_ticket(
    ctx: typer.Context,
    subject: str = typer.Option(..., "--subject", help="Ticket subject."),
    category: int = typer.Option(..., "--category", help="Category id."),
    name: str = typer.Option(None, "--name", help="Contact name (for a new contact)."),
    email: str = typer.Option(None, "--email", help="Contact email (for a new contact)."),
    client: int = typer.Option(None, "--client", help="Existing contact (client) id."),
    phone: str = typer.Option(None, "--phone", help="Contact phone number."),
    text: str = typer.Option(None, "--text", help="Plain-text ticket body."),
    html: str = typer.Option(None, "--html", help="HTML ticket body."),
    priority: int = typer.Option(None, "--priority", help="Priority id."),
    assignee: int = typer.Option(None, "--assignee", help="Assignee staff id."),
    tags: str = typer.Option(None, "--tags", help="Comma-separated tags."),
    cc: str = typer.Option(None, "--cc", help="Comma-separated CC addresses."),
    bcc: str = typer.Option(None, "--bcc", help="Comma-separated BCC addresses."),
    created_at: str = typer.Option(None, "--created-at", help="Override creation timestamp."),
    due_date: str = typer.Option(None, "--due-date", help="Due date."),
    visible_only_staff: bool = typer.Option(
        False, "--visible-only-staff", help="Make the first message staff-only."
    ),
    cf: list[str] = typer.Option(
        None, "--cf", help="Ticket custom field '<id>=<value>' (repeatable)."
    ),
    cf_json: str = typer.Option(
        None,
        "--cf-json",
        help="Raw JSON object of custom fields {id: value}, sent without coercion "
        "(e.g. '{\"1\":\"Acme, Inc.\",\"2\":[5]}').",
    ),
    contact_cf: list[str] = typer.Option(
        None, "--contact-cf", help="Contact custom field '<id>=<value>' (repeatable)."
    ),
    attachment: list[str] = typer.Option(
        None, "--attachment", help="File path to attach (repeatable)."
    ),
):
    """Create a ticket for a new or existing contact."""
    obj = get_ctx(ctx)
    # A ticket needs a body and an identifiable contact.
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
    body.update(parse_cf_options(cf, prefix="t-cf-"))
    body.update(parse_cf_json(cf_json, "t-cf-"))
    body.update(parse_cf_options(contact_cf, prefix="c-cf-"))
    result = obj.call("POST", "tickets/", **attach(body, attachment))
    obj.render(result)
    if isinstance(result, dict) and result.get("display_id"):
        obj.success(f"Created ticket {result['display_id']}.")


@app.command("create-bulk")
def create_bulk(
    ctx: typer.Context,
    file: str = typer.Option(..., "--file", help="Path to a JSON array of ticket objects."),
):
    """Create up to 100 tickets from a JSON array file."""
    obj = get_ctx(ctx)
    payload = load_json_file(file)
    if not isinstance(payload, list):
        raise ValidationError("Bulk file must contain a JSON array of ticket objects.")
    if not (1 <= len(payload) <= 100):
        raise ValidationError("Bulk create accepts between 1 and 100 tickets.")
    result = obj.call("POST", "tickets/", json=payload)
    obj.render(result)


@app.command("reply")
def reply(
    ctx: typer.Context,
    ticket_id: str = typer.Argument(..., help="Ticket id or display id."),
    staff: int = typer.Option(None, "--staff", help="Staff id posting the reply."),
    html: str = typer.Option(None, "--html", help="HTML reply body."),
    text: str = typer.Option(None, "--text", help="Plain-text reply body."),
    status: int = typer.Option(None, "--status", help="New status id."),
    priority: int = typer.Option(None, "--priority", help="New priority id."),
    assignee: int = typer.Option(None, "--assignee", help="New assignee staff id."),
    time_spent: int = typer.Option(None, "--time-spent", help="Time spent (minutes)."),
    due_date: str = typer.Option(None, "--due-date", help="Due date."),
    tags: str = typer.Option(None, "--tags", help="Comma-separated tags to set."),
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
    cf: list[str] = typer.Option(
        None, "--cf", help="Ticket custom field '<id>=<value>' (repeatable)."
    ),
    cf_json: str = typer.Option(
        None,
        "--cf-json",
        help="Raw JSON object of custom fields {id: value}, sent without coercion "
        "(e.g. '{\"1\":\"Acme, Inc.\",\"2\":[5]}').",
    ),
    contact_cf: list[str] = typer.Option(
        None, "--contact-cf", help="Contact custom field '<id>=<value>' (repeatable)."
    ),
    contact_cf_json: str = typer.Option(
        None,
        "--contact-cf-json",
        help="Raw JSON object of contact custom fields {id: value}, sent without coercion.",
    ),
    attachment: list[str] = typer.Option(
        None, "--attachment", help="File path to attach (repeatable)."
    ),
):
    """Post a public staff reply to a ticket."""
    obj = get_ctx(ctx)
    if not (html or text):
        raise ValidationError("Provide a reply body with --html or --text.")
    staff_id = obj.require_staff_id(staff)
    body = compact(
        {
            "staff": staff_id,
            "html": html,
            "plaintext": text,
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
    body.update(parse_cf_options(cf, prefix="t-cf-"))
    body.update(parse_cf_json(cf_json, "t-cf-"))
    body.update(parse_cf_options(contact_cf, prefix="ccf-"))
    body.update(parse_cf_json(contact_cf_json, "ccf-"))
    result = obj.call(
        "POST", f"ticket/{ticket_id}/staff_update/", **attach(body, attachment)
    )
    obj.render(result)


@app.command("note")
def note(
    ctx: typer.Context,
    ticket_id: str = typer.Argument(..., help="Ticket id or display id."),
    staff: int = typer.Option(None, "--staff", help="Staff id posting the note."),
    html: str = typer.Option(None, "--html", help="HTML note body."),
    text: str = typer.Option(None, "--text", help="Plain-text note body."),
    alert: str = typer.Option(
        None, "--alert", help="Alert recipients: 's' (staff), 'c' (category), or an agent id."
    ),
    status: int = typer.Option(None, "--status", help="New status id."),
    priority: int = typer.Option(None, "--priority", help="New priority id."),
    assignee: int = typer.Option(None, "--assignee", help="New assignee staff id."),
    time_spent: int = typer.Option(None, "--time-spent", help="Time spent (minutes)."),
    due_date: str = typer.Option(None, "--due-date", help="Due date."),
    tags: str = typer.Option(None, "--tags", help="Comma-separated tags to set."),
    cf: list[str] = typer.Option(
        None, "--cf", help="Ticket custom field '<id>=<value>' (repeatable)."
    ),
    cf_json: str = typer.Option(
        None,
        "--cf-json",
        help="Raw JSON object of custom fields {id: value}, sent without coercion "
        "(e.g. '{\"1\":\"Acme, Inc.\",\"2\":[5]}').",
    ),
    contact_cf: list[str] = typer.Option(
        None, "--contact-cf", help="Contact custom field '<id>=<value>' (repeatable)."
    ),
    contact_cf_json: str = typer.Option(
        None,
        "--contact-cf-json",
        help="Raw JSON object of contact custom fields {id: value}, sent without coercion.",
    ),
    attachment: list[str] = typer.Option(
        None, "--attachment", help="File path to attach (repeatable)."
    ),
):
    """Add a private (staff-only) note to a ticket."""
    obj = get_ctx(ctx)
    if not (html or text):
        raise ValidationError("Provide a note body with --html or --text.")
    staff_id = obj.require_staff_id(staff)
    body = compact(
        {
            "staff": staff_id,
            "html": html,
            "plaintext": text,
            "alert": alert,
            "status": status,
            "priority": priority,
            "assignee": assignee,
            "time_spent": time_spent,
            "due_date": due_date,
            "tags": comma_join(split_csv(tags)),
        }
    )
    body.update(parse_cf_options(cf, prefix="t-cf-"))
    body.update(parse_cf_json(cf_json, "t-cf-"))
    body.update(parse_cf_options(contact_cf, prefix="ccf-"))
    body.update(parse_cf_json(contact_cf_json, "ccf-"))
    result = obj.call(
        "POST", f"ticket/{ticket_id}/staff_pvtnote/", **attach(body, attachment)
    )
    obj.render(result)


@app.command("user-reply")
def user_reply(
    ctx: typer.Context,
    ticket_id: str = typer.Argument(..., help="Ticket id or display id."),
    user: int = typer.Option(..., "--user", help="Contact (user) id posting the reply."),
    text: str = typer.Option(..., "--text", help="Reply body."),
    cc: str = typer.Option(None, "--cc", help="Comma-separated CC addresses."),
    bcc: str = typer.Option(None, "--bcc", help="Comma-separated BCC addresses."),
    attachment: list[str] = typer.Option(
        None, "--attachment", help="File path to attach (repeatable)."
    ),
):
    """Post a reply to a ticket as the contact (user)."""
    obj = get_ctx(ctx)
    body = compact(
        {
            "user": user,
            "text": text,
            "cc": comma_join(split_csv(cc)),
            "bcc": comma_join(split_csv(bcc)),
        }
    )
    result = obj.call(
        "POST", f"ticket/{ticket_id}/user_reply/", **attach(body, attachment)
    )
    obj.render(result)


@app.command("update-cf")
def update_cf(
    ctx: typer.Context,
    ticket_id: str = typer.Argument(..., help="Ticket id or display id."),
    staff: int = typer.Option(None, "--staff", help="Staff id making the change."),
    cf: list[str] = typer.Option(
        None, "--cf", help="Ticket custom field '<id>=<value>' (repeatable)."
    ),
    cf_json: str = typer.Option(
        None,
        "--cf-json",
        help="Raw JSON object of custom fields {id: value}, sent without coercion "
        "(e.g. '{\"1\":\"Acme, Inc.\",\"2\":[5]}').",
    ),
):
    """Update one or more custom fields on a ticket."""
    obj = get_ctx(ctx)
    fields = parse_cf_options(cf, prefix="t-cf-")
    fields.update(parse_cf_json(cf_json, "t-cf-"))
    if not fields:
        raise ValidationError("Provide at least one --cf '<id>=<value>' or --cf-json.")
    body = compact({"staff": obj.require_staff_id(staff)})
    body.update(fields)
    result = obj.call("POST", f"ticket/{ticket_id}/update_custom_fields/", json=body)
    obj.render(result)


@app.command("tags")
def tags(
    ctx: typer.Context,
    ticket_id: str = typer.Argument(..., help="Ticket id or display id."),
    add: str = typer.Option(None, "--add", help="Comma-separated tags to add."),
    remove: str = typer.Option(None, "--remove", help="Comma-separated tags to remove."),
    staff_id: int = typer.Option(None, "--staff-id", help="Staff id making the change."),
):
    """Add and/or remove tags on a ticket."""
    obj = get_ctx(ctx)
    if not (add or remove):
        raise ValidationError("Provide tags to --add and/or --remove.")
    body = compact(
        {
            "add": comma_join(split_csv(add)),
            "remove": comma_join(split_csv(remove)),
            "staff_id": obj.require_staff_id(staff_id),
        }
    )
    result = obj.call("POST", f"ticket/{ticket_id}/update_tags/", json=body)
    obj.render(result)


@app.command("subscribe")
def subscribe(
    ctx: typer.Context,
    ticket_id: str = typer.Argument(..., help="Ticket id or display id."),
    staff_id: int = typer.Option(None, "--staff-id", help="Staff id making the change."),
    agents: str = typer.Option(
        None, "--agents", help="Comma-separated agent ids to subscribe."
    ),
):
    """Subscribe agents to a ticket."""
    obj = get_ctx(ctx)
    body = compact(
        {
            "staff_id": obj.require_staff_id(staff_id),
            "data": split_csv_ints(agents),
        }
    )
    result = obj.call("POST", f"ticket/{ticket_id}/subscribe/", json=body)
    obj.render(result)


@app.command("unsubscribe")
def unsubscribe(
    ctx: typer.Context,
    ticket_id: str = typer.Argument(..., help="Ticket id or display id."),
    staff_id: int = typer.Option(None, "--staff-id", help="Staff id making the change."),
):
    """Unsubscribe the staff member from a ticket."""
    obj = get_ctx(ctx)
    body = {"staff_id": obj.require_staff_id(staff_id)}
    result = obj.call("POST", f"ticket/{ticket_id}/unsubscribe/", json=body)
    obj.render(result)


@app.command("forward")
def forward(
    ctx: typer.Context,
    ticket_id: str = typer.Argument(..., help="Ticket id or display id."),
    to: str = typer.Option(..., "--to", help="Comma-separated recipient addresses."),
    subject: str = typer.Option(..., "--subject", help="Forward subject."),
    message: str = typer.Option(..., "--message", help="Forward message body."),
    cc: str = typer.Option(None, "--cc", help="Comma-separated CC addresses."),
    bcc: str = typer.Option(None, "--bcc", help="Comma-separated BCC addresses."),
    staff_id: int = typer.Option(None, "--staff-id", help="Staff id forwarding."),
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
    body = compact(
        {
            "to": comma_join(split_csv(to)),
            "subject": subject,
            "message": message,
            "cc": comma_join(split_csv(cc)),
            "bcc": comma_join(split_csv(bcc)),
            "staff_id": obj.require_staff_id(staff_id),
            "to_include_ticket_contact": to_include_contact or None,
            "cc_include_ticket_contact": cc_include_contact or None,
            "send_all_messages": send_all_messages,
            "include_pvt_notes": include_private_notes or None,
            "convert_replies_as_new_ticket": convert_replies,
            "ticket_attachments": split_csv_ints(ticket_attachments),
        }
    )
    result = obj.call("POST", f"ticket/{ticket_id}/forward/", **attach(body, attachment))
    obj.render(result)


@app.command("move")
def move(
    ctx: typer.Context,
    ticket_id: str = typer.Argument(..., help="Ticket id or display id."),
    to_category: int = typer.Option(..., "--to-category", help="Target category id."),
    staff_id: int = typer.Option(None, "--staff-id", help="Staff id making the move."),
    note: str = typer.Option(None, "--note", help="Note to record with the move."),
    assign_to: int = typer.Option(None, "--assign-to", help="Assignee staff id after the move."),
):
    """Move a ticket to a different category."""
    obj = get_ctx(ctx)
    body = compact(
        {
            "staff_id": obj.require_staff_id(staff_id),
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
    ticket_id: str = typer.Argument(..., help="Ticket id or display id."),
    staff_id: int = typer.Option(None, "--staff-id", help="Staff id performing the delete."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
):
    """Delete a ticket (destructive)."""
    obj = get_ctx(ctx)
    if not yes:
        typer.confirm(f"Delete ticket {ticket_id}?", abort=True)
    body = {"staff_id": obj.require_staff_id(staff_id)}
    result = obj.call("POST", f"ticket/{ticket_id}/delete/", json=body)
    obj.success(f"Deleted ticket {ticket_id}.")
    obj.render(result)
