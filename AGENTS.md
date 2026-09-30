# AGENTS.md

Guidance for AI coding agents and human contributors working on **`hfox`**. The repository
root is the package (`pyproject.toml`, `src/hfox/`, `tests/`).

## Project overview

`hfox` is a dual-audience CLI for the HappyFox REST API (`api/1.1/json`), with the
`hfox <resource> <verb>` structure of the [Google Workspace CLI (`gws`)](https://github.com/googleworkspace/cli).
Humans get `--help`, Rich tables, `--dry-run` and confirmation prompts; machines get JSON by
default, structured errors, stable exit codes and `--page-all`. It ships as a
[`uv` tool](https://docs.astral.sh/uv/) run via `uvx hfox`.

## Development commands

Always use `uv` (never the system `python`/`pip`).

```bash
uv sync                  # create or update the virtualenv
uv run hfox --help       # run the CLI from source
uv run python -m pytest  # run the test suite
uv run python -m pytest -q tests/test_client.py   # a single test file
uv run ruff check .      # lint
uv run ruff check --fix . # autofix lint findings
uv build                 # build sdist + wheel into dist/
uvx --from . hfox ...    # smoke-test the packaged entry point
uv run Docs/sync.py      # refresh Docs/ from HappyFox's REST API help articles
```

Use `python -m pytest`; the bare `pytest` shebang can break in a venv that was not freshly
created.

## Architecture

Two layers with a hard boundary:

```
src/hfox/
├── core/                 # UI-agnostic: no Typer, no printing, no sys.exit
│   ├── client.py         # HappyFoxClient: httpx, Basic auth, retries, paginate()
│   ├── config.py         # ~/.hfox/ resolution, Config, token.json + config.toml I/O
│   └── errors.py         # HfoxError hierarchy + ExitCode (stable, public contract)
└── cli/                  # presentation: Typer apps, formatting, auth, command groups
    ├── main.py           # root Typer app, global flags, app() entry + error handling
    ├── context.py        # AppContext (ctx.obj): the contract every command uses
    ├── output.py         # OutputFormat enum + json/table/csv/yaml rendering, stderr
    ├── auth.py           # `hfox auth` login/status/logout
    ├── cf.py             # custom-field parsing (t-cf-/c-cf-/ccf-/asset)
    ├── _util.py          # input validation, CSV/JSON parsing, attach(), bulk results
    ├── tickets.py        # `hfox tickets`  (one module per resource)
    ├── contacts.py       # `hfox contacts` (+ `groups` sub-app)
    ├── assets.py         # `hfox assets`   (+ `types`, `custom-fields` sub-apps)
    └── system.py         # `hfox system`   (read-only reference data)
```

**Rule:** `core/` must never import from `cli/`. A command module never builds `httpx`
requests or touches the filesystem directly; it goes through `AppContext` and the
`_util`/`cf` helpers.

### Request flow

```
hfox <args> → Typer (main.cli) → callback builds AppContext from global flags + Config
            → command module → AppContext.call()/paginate() → HappyFoxClient → HappyFox API
            → AppContext.render()/render_list() → OutputFormat rendering → stdout
```

`main.app()` runs with `standalone_mode=False` and converts every `HfoxError` into a JSON
error on stdout plus its exit code, so commands **raise** `HfoxError` subclasses rather than
printing and exiting.

## The AppContext contract (read before adding a command)

Every command's first line is `obj = get_ctx(ctx)`. `AppContext` (in `cli/context.py`) is
the only API a command module uses to talk to HappyFox or emit output:

| Method | Use for |
|--------|---------|
| `obj.call(method, path, *, params, json, data, files)` | single GETs and **all writes**; returns parsed JSON; `--dry-run` prints the exact wire request and exits |
| `obj.paginate(path, *, params, root_key="data")` | paginated lists; honors `--dry-run`. `--page-all` + JSON **streams NDJSON**, one compact line per page or error, then exits, so `render_list` never runs; other formats get the flat record list. Without `--page-all`, the single page. Warns on stderr when `--page-limit` truncates |
| `obj.render(data)` | render one object/list in the active `--format` |
| `obj.render_list(body, *, root_key="data")` | render a listing: JSON keeps the `{page_info, data}` envelope; other formats unwrap to rows |
| `obj.resolve_staff_id(explicit)` | staff id: explicit arg → `--staff-id` → configured default; may be `None` |
| `obj.require_staff_id(explicit)` | same, but raises `ValidationError` if none is available |
| `obj.success(msg)` | status line to **stderr**, suppressed by `--quiet` |
| `obj.config` | the resolved `Config` (base URL, region, staff id, …) |

Helpers:

- `cli/_util.py`: `compact(dict)` drops `None`; `comma_join(list)`→`"a,b"`;
  `split_csv("a,b")`→`["a","b"]`, `None` when blank; `split_csv_ints("1,2")`→`[1,2]`, ASCII
  digits only, `[]` when blank. `require_nonblank`, `validate_ticket_id` and
  `validate_contact_ref` raise `ValidationError`. `parse_json`/`load_json_file` accept
  strict UTF-8 JSON (no NaN/Infinity).
- `attach(body, attachments, *, field="attachments")` → kwargs to splat into `obj.call`.
  JSON bodies go as given, so `compact()` the base body before merging custom fields.
  Multipart drops `None` and caps files at `MAX_ATTACHMENT_BYTES` in total.
- `exit_on_failures(result, *, benign=None)`, called after rendering, exits 1 when any entry
  has `success: false`; group removals pass `benign=NOT_IN_GROUP`.
- `cli/cf.py`: `parse_cf_options(items, *, prefix, allowed)` and `parse_cf_json(raw, prefix,
  allowed)` prefix numeric keys and pass through keys carrying an `allowed` prefix (default:
  `prefix`); other keys raise. `parse_asset_cf` takes bare ids only.

## Adding a command or resource

1. **A verb on an existing resource:** add a `@app.command("name")` function to the
   resource module. Signature is `def name(ctx: typer.Context, <id> = typer.Argument(...), <opts> = typer.Option(...))`.
   Route reads through `obj.paginate`/`obj.call` and writes through
   `obj.call(..., **attach(body, attachment))`. Render with `obj.render`/`obj.render_list`.
   Confirm destructive actions with `_util.confirm(message)` unless `--yes/-y` is passed.
2. **A new resource group:** create `cli/<resource>.py` exposing a Typer app named
   `app`, then mount it in `main.py` with `cli.add_typer(<resource>.app, name="<resource>")`.
   Sub-groups (like contact `groups`) are their own Typer instances mounted with
   `app.add_typer(sub, name="...")`.
3. Mirror `tickets.py`, the richest example.

### Conventions

- Module header: `"""docstring"""`, then `from __future__ import annotations`, then
  imports, then `app = typer.Typer(no_args_is_help=True, help="…")`.
- Optional flags are `x: str = typer.Option(None, …)`; repeatable options are `list[str]`.
- CLI verb names use hyphens (`create-bulk`, `update-cf`) and are set explicitly in
  `@app.command("…")`.
- Paths are **relative** to the base URL (which already includes `/api/1.1/json`) and
  **keep their trailing slash** unless `Docs/` shows none (`ticket-inline-attachment`). The
  client rejects `.`/`..` segments and sends `@` literally.
- Validate path ids with the `_util` validators or `int` with `min=1`. `--size` is 1–50 and
  `--page` at least 1. Required strings use `require_nonblank`.
- Comma-join `tags`/`cc`/`bcc` inputs with `comma_join(split_csv(...))`;
  send list-of-ids body fields (subscribe `data`, group `contacts`, forward
  `ticket_attachments`, asset `contact_ids`/`contact_group_ids`) as JSON int arrays via
  `split_csv_ints`. Multipart sends a list as JSON text in one field. Unverified until tested
  against a live helpdesk.
- Raise `ValidationError`/`NotFoundError`/`AuthError`/`APIError` from `core.errors`;
  do not `print()` errors or call `sys.exit()`.
- Keep stdout pure data: status and warnings go to stderr (`obj.success`, `output.warn`,
  `output.info`).

## HappyFox API quirks

Sourced from `Docs/` unless marked observed. Honor them exactly.

- **Auth is HTTP Basic only:** API key = username, auth code = password.
- **Base URL is host-templated:** `https://<subdomain>.happyfox.com/api/1.1/json` (US) or
  `…happyfox.net` (EU, `--region eu`). Region is case-insensitive and an unknown value exits
  3. A dotted `subdomain` (e.g. `support.acme.com`) is a full custom host. `HFOX_BASE_URL` is
  an `http(s)://host` root for proxied accounts, persisted by `auth login`. A trailing
  `/api/1.1/json` is stripped; another scheme, a missing host, a query or a fragment exits 3.
- **Rate limits are global:** 500 GET/min, 300 POST/min, then HTTP 429 for a **10-minute**
  cooldown. Without `Retry-After`, 429 retries stop after about 33s; an undocumented numeric
  `Retry-After` is honored up to 600s per attempt. GETs retry any transport error; writes
  retry only when no connection was made, since a write may already have applied. Respect
  `--page-limit`/`--page-delay`.
- **Search sends `status=_all`:** the documented search URL is `tickets/?status=_all&q=...`,
  so `tickets list -q` injects it unless `--status` is given.
  In filters a comma means any-of, and the docs' `+` is an encoded space.
- **Multi-word `q` is AND (observed):** `q=degree works` returns tickets
  containing both words in any order; quoting does not force phrase matching.
- **Bulk endpoints cap at 100 entries** (tickets create-bulk, contacts create-bulk,
  group update_contacts). Enforce it with a `ValidationError` before the request.
- **Contact create is add-or-edit:** POST `users/` edits the contact with the same email, as
  the docs' example implies, and resets custom fields missing from the payload. Whether
  `user/<id>/` also resets them is unverified until tested against a live helpdesk. Always
  send `email`, as null when only a phone is given, so keep it out of `compact()`.
- **Contact phones:** editing a phone requires its `id` in the `phones` entry; without it
  the API **adds** a new phone. Types are `mo/w/m/h/o`, and an omitted type falls back to `o`,
  so `--phone-id` requires `--phone-type`. `update` sends `is_primary` only when given; one
  phone per call. The docs conflict: section 14 posts phone edits to `user/<id>/`, which
  `update` uses, while `Docs/1092:315` says to use `users/`, which only `create-bulk` reaches.
- **Singular vs plural paths:** collections are plural (`tickets/`, `users/`, `assets/`,
  `contact_groups/`, `asset_types/`, `asset_custom_fields/`); single-resource ops are singular
  (`ticket/<id>/`, `user/<id>/`, `asset/<id>/`, `contact_group/<id>/`, `asset_type/<id>/`,
  `asset_custom_field/<id>/`).
- **Pagination:** `size` defaults to 10, **max 50**; the client clamps it. Responses wrap
  rows in `{"page_info": {"page_count", …}, "data": [...]}`. `paginate()` yields flat records
  and `paginate_pages()` raw bodies; both also read a top-level `page_count`.
- **Custom-field encoding:** ticket fields use `t-cf-<id>`; contact fields use `c-cf-<id>`
  on ticket create and contact create/edit, and `ccf-<id>` on staff reply/note; assets use a
  `custom_fields` object keyed by bare id. Each call site allows only its own prefixes.
  Values: text→string, number→int/float, dropdown→choice id, **multiple-option→array of
  option ids** (`--cf ID=[a,b]`), date→`YYYY-MM-DD`. `--cf` makes numbers only from canonical
  decimals, never splits on commas and rejects an all-blank value; `--cf-json` sends values
  uncoerced. Field ids come from the API, not agent-portal URLs.
- **Contacts are the `users` endpoint.** Tickets are addressed by `ticket_number`
  (numeric `id`), not the `display_id` (e.g. `#DC00000003`); `q=id:DC00000003` finds one.
- **Acting staff id:** needed only where the docs list a staff field: `staff` (reply, note,
  update-cf), `staff_id` (tags, forward, move, delete), `created_by`/`updated_by`/`deleted_by`
  (assets). Resolve via `obj.require_staff_id(...)`. On subscribe/unsubscribe, `staff_id` is
  the agent added or removed, not the actor.
- **Attachments are multipart/form-data**, 25 MB total per request; use `attach()`.
  `ticket-inline-attachment` takes one image in field `file` and returns a temporary `{url}`.
- All response timestamps are UTC.

## Configuration & secrets

Everything lives under `~/.hfox/` (override with `HFOX_CONFIG_DIR`), one account, no profiles.

- `token.json` holds **secrets**: `api_key`, `auth_code`, `subdomain`, `region`, optional
  `base_url`. Written atomically (temp + `os.replace`) at mode `0600`; dir at `0700`.
- `config.toml` holds `subdomain`, `region`, `default_format`, `default_staff_id`.

Every field resolves as **env var > token.json/config.toml > default**.
Env overrides: `HFOX_SUBDOMAIN`, `HFOX_REGION`, `HFOX_API_KEY`, `HFOX_AUTH_CODE`,
`HFOX_BASE_URL`, `HFOX_STAFF_ID`, `HFOX_FORMAT`, `HFOX_CONFIG_DIR`. A configured staff id
that is not a whole number exits 3 in commands that need one.

Never log or echo `api_key`/`auth_code`. `auth status` masks the key.

## Exit codes (stable public contract; do not renumber)

`0` success · `1` API error · `2` auth error · `3` validation · `4` not found · `5` other.
Defined in `core/errors.py` (`ExitCode`); each `HfoxError` subclass carries its code. Any
other exception becomes a JSON error naming its type, exit 5, without a traceback.

- **Usage errors** (unknown flag/command, missing argument) exit **3** with a JSON error on
  stdout and the usage hint on stderr; click's default 2 would collide with `ExitCode.AUTH`.
- **Bare group invocations** (`hfox`, `hfox tickets`) print help and exit **0**.
- **Partial bulk failure:** bulk and group-membership commands print the response unchanged,
  warn the failed count on stderr and exit **1**; "not part of the group" on remove is benign.

## Testing

- `tests/` uses `pytest` + `typer.testing.CliRunner` and never hits the real API: the HTTP
  layer uses `httpx.MockTransport` with an injected `sleep`, so backoff is instant.
- A new command needs a `--dry-run` test asserting method/URL/body, plus a unit test if it
  touches the client or encoding.
- CliRunner bypasses `main.app()`; test exit codes that depend on it through `app()`.
- PyYAML is a dev-only dependency for YAML round-trip tests.

## Scope

Shipped: tickets, contacts/groups, assets/types/custom-fields, system reference data,
auth. Planned: Reports, Knowledge Base export and ticket custom-field choice management.
