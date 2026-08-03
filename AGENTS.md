# AGENTS.md

Guidance for AI coding agents (Claude, Codex, Gemini, etc.) and human contributors
working on **`hfox`**, the HappyFox REST API command-line interface in this directory.

> This repository root **is** the `hfox` CLI project: the package (`pyproject.toml`,
> `src/hfox/`, `tests/`) lives directly here. Everything below — architecture, the
> `AppContext` contract, and the HappyFox API quirks — describes this CLI.

## Project overview

`hfox` is a modern, dual-audience CLI for the HappyFox REST API (`api/1.1/json`).
It is built for both humans (`--help` everywhere, Rich tables, `--dry-run`,
confirmation prompts) and machines (JSON by default, structured errors, stable exit
codes, `--page-all` for bulk reads). The command structure — `hfox <resource> <verb>`
— is modeled on the [Google Workspace CLI (`gws`)](https://github.com/googleworkspace/cli).

It is distributed as a [`uv` tool](https://docs.astral.sh/uv/) and runs via
`uvx hfox` / `uv tool run hfox`.

## Development commands

Always use `uv` (never the system `python`/`pip`).

```bash
uv sync                  # create/update the virtualenv from pyproject + lockfile
uv run hfox --help       # run the CLI from source
uv run python -m pytest  # run the test suite (no network — uses httpx MockTransport)
uv run python -m pytest -q tests/test_client.py   # a single test file
uv run ruff check .      # lint
uv run ruff check --fix .# autofix imports/format
uv build                 # build sdist + wheel into dist/
uvx --from . hfox ...    # smoke-test the packaged entry point (simulates `uvx hfox`)
```

Use the `python -m pytest` module form rather than bare `uv run pytest`: it is the
reliable invocation, since the bare `pytest` entrypoint shebang can break if the venv
is not freshly created.

There is no network access in tests. Never write a test that hits the real HappyFox
API; use `httpx.MockTransport` (see `tests/test_client.py`) or `--dry-run`.

## Architecture

Two layers with a hard boundary — mirror `gws`'s library-vs-CLI split:

```
src/hfox/
├── core/                 # UI-agnostic: no Typer, no printing, no sys.exit
│   ├── client.py         # HappyFoxClient — httpx, Basic auth, 429 backoff, paginate()
│   ├── config.py         # ~/.hfox/ resolution, Config, token.json + config.toml I/O
│   └── errors.py         # HfoxError hierarchy + ExitCode (stable, public contract)
└── cli/                  # presentation: Typer apps, formatting, auth, command groups
    ├── main.py           # root Typer app, global flags, app() entry + error handling
    ├── context.py        # AppContext (ctx.obj) — the contract every command uses
    ├── output.py         # OutputFormat enum + json/table/csv/yaml rendering, stderr
    ├── auth.py           # `hfox auth` login/status/logout
    ├── cf.py             # custom-field option parsing (t-cf-/c-cf-/asset)
    ├── _util.py          # compact/comma_join/split_csv/attach/load_json_file helpers
    ├── tickets.py        # `hfox tickets`  (one module per resource)
    ├── contacts.py       # `hfox contacts` (+ `groups` sub-app)
    ├── assets.py         # `hfox assets`   (+ `types`, `custom-fields` sub-apps)
    └── system.py         # `hfox system`   (read-only reference data)
```

**Rule:** `core/` must never import from `cli/`. Keep all transport, auth, and config
logic in `core/`; keep all output, prompting, and Typer wiring in `cli/`. A command
module should not build `httpx` requests or touch the filesystem directly — go
through `AppContext` and the `_util`/`cf` helpers.

### Request flow

```
hfox <args> → Typer (main.cli) → callback builds AppContext from global flags + Config
            → command module → AppContext.call()/paginate() → HappyFoxClient → HappyFox API
            → AppContext.render()/render_list() → OutputFormat rendering → stdout
```

`main.app()` wraps execution with `standalone_mode=False` and converts every
`HfoxError` into a JSON error object on stdout plus the matching exit code. This is
why commands should **raise** `HfoxError` subclasses rather than printing and exiting.

## The AppContext contract (read before adding a command)

Every command's first line is `obj = get_ctx(ctx)`. `AppContext` (in `cli/context.py`)
is the only API a command module should use to talk to HappyFox or emit output:

| Method | Use for |
|--------|---------|
| `obj.call(method, path, *, params, json, data, files)` | single GETs and **all writes**; returns parsed JSON; honors `--dry-run` (prints the request and exits) |
| `obj.paginate(path, *, params, root_key="data")` | paginated list endpoints; honors `--dry-run`. With `--page-all` + JSON format it **streams NDJSON** — one compact page envelope per line as each page arrives (gws behavior) — then exits 0 (the command's `render_list` never runs, same pattern as `--dry-run`). With `--page-all` + table/csv/yaml it returns the flat record list; without `--page-all` it returns the single-page response |
| `obj.render(data)` | render one object/list in the active `--format` |
| `obj.render_list(body, *, root_key="data")` | render a listing — JSON keeps the full `{page_info, data}` envelope; table/csv/yaml unwrap to rows |
| `obj.resolve_staff_id(explicit)` | staff id: explicit arg → `--staff-id` → configured default; may be `None` |
| `obj.require_staff_id(explicit)` | same, but raises `ValidationError` if none is available |
| `obj.success(msg)` | status line to **stderr** (suppressed by `--quiet`); never put status on stdout |
| `obj.config` | the resolved `Config` (base URL, region, staff id, …) |

Helpers:

- `cli/_util.py`: `compact(dict)` drops `None`; `comma_join(list)`→`"a,b"`;
  `split_csv("a,b")`→`["a","b"]`; `split_csv_ints("1,2")`→`[1,2]`;
  `attach(body, attachments)`→ kwargs to splat into `obj.call` (`{"json": …}` or
  `{"data": …, "files": …}` for multipart); `load_json_file(path)`.
- `cli/cf.py`: `parse_cf_options(items, prefix="t-cf-")` (use `"c-cf-"` for contact
  fields on create, `"ccf-"` on staff updates/notes); `parse_asset_cf(items)`
  → `{"<id>": value}` object.

## Adding a command or resource

1. **A verb on an existing resource:** add a `@app.command("name")` function to the
   resource module. Signature is `def name(ctx: typer.Context, <id>: str = typer.Argument(...), <opts> = typer.Option(...))`.
   First line `obj = get_ctx(ctx)`. Build the request, route reads through
   `obj.paginate`/`obj.call` and writes through `obj.call(..., **attach(body, attachment))`.
   Render with `obj.render`/`obj.render_list`. Confirm destructive actions with
   `typer.confirm(..., abort=True)` unless `--yes/-y` is passed.
2. **A new resource group:** create `cli/<resource>.py` exposing a Typer app named
   `app`, then mount it in `main.py` with `cli.add_typer(<resource>.app, name="<resource>")`.
   Sub-groups (like contact `groups`) are their own Typer instances mounted with
   `app.add_typer(sub, name="...")`.
3. Mirror the patterns in `tickets.py` (richest example) for options, validation,
   custom fields, and attachments.

### Conventions

- Module header: `"""docstring"""`, then `from __future__ import annotations`, then
  imports, then `app = typer.Typer(no_args_is_help=True, help="…")`.
- `from __future__ import annotations` is required so `x: str = typer.Option(None, …)`
  works for optional flags; repeatable options are annotated `list[str]`.
- CLI verb names use hyphens (`create-bulk`, `update-cf`, `contact-custom-fields`);
  set them explicitly in `@app.command("…")`.
- Paths are **relative** to the base URL (which already includes `/api/1.1/json`) and
  **keep their trailing slash**: `"tickets/"`, `"ticket/{id}/staff_update/"`.
- Comma-join `tags`/`cc`/`bcc` string inputs with `comma_join(split_csv(...))`; send
  "list of ids" body fields (subscribe `data`, group `contacts`, forward
  `ticket_attachments`) as real JSON int arrays via `split_csv_ints`.
- Raise `ValidationError`/`NotFoundError`/`AuthError`/`APIError` from `core.errors`;
  do not `print()` errors or call `sys.exit()` — `main.app()` handles that uniformly.
- Keep stdout pure data: all human status/warnings go to stderr (`obj.success`,
  `output.warn`, `output.info`).

## HappyFox API quirks (authoritative — verified against the docs + the Worker client)

These bite anyone touching the client or a command. Honor them exactly.

- **Auth is HTTP Basic only:** API key = username, auth code = password. There is no
  bearer/token scheme.
- **Base URL is host-templated:** `https://<subdomain>.happyfox.com/api/1.1/json` (US)
  or `…happyfox.net` (EU, `--region eu`). A dotted `subdomain` (e.g. `support.acme.com`)
  is treated as a full custom host. `HFOX_BASE_URL` overrides the whole thing
  (self-hosted/proxied accounts) and is persisted by `auth login` when set.
- **Rate limits are global:** 500 GET/min, 300 POST/min, then HTTP 429 for a **10-minute**
  cooldown. The client retries 429 with exponential backoff + jitter (base 1s, max 60s,
  5 retries) and honors `Retry-After` up to `MAX_RETRY_AFTER_DELAY` (600s — the full
  documented cooldown; the 60s cap applies only to the no-header backoff path). Don't
  hammer; respect `--page-limit`/`--page-delay`.
- **Search needs `status=_all`:** the documented search URL shape is
  `tickets/?status=_all&q=...`; otherwise `q` only spans the default status set.
  `tickets list -q` auto-injects `status=_all` when `--status` is not given.
- **Multi-word `q` is AND, not OR or exact phrase:** `q=degree works` returns only
  tickets containing both words, anywhere and in any order (`q=works degree` gives
  the identical result set); quoting (`q="degree works"`) does not force phrase
  matching. Observed behavior verified empirically against a live helpdesk
  (2026-06) — HappyFox does not document free-text semantics, so re-verify if
  their search backend changes.
- **Bulk endpoints cap at 100 entries** (tickets create-bulk, contacts create-bulk,
  group update_contacts). Enforce client-side with a `ValidationError` before the
  HTTP call — mirror `tickets.py`'s create-bulk check.
- **Contact phones:** editing an existing phone record requires its `id` in the
  `phones` entry (`contacts update --phone-id`); without it the API **adds** a new
  phone. Types are `mo/w/m/h/o` (default `o`). On create, `email` must be present
  even if null when only a phone is supplied — re-add `"email": None` after
  `compact()` rather than dropping the key.
- **Singular vs plural paths:** collections are plural (`tickets/`, `users/`, `assets/`,
  `contact_groups/`, `asset_types/`); single-resource ops are singular (`ticket/<id>/`,
  `user/<id>/`, `asset/<id>/`, `contact_group/<id>/`, `asset_type/<id>/`). Easy to get wrong.
- **Pagination:** `size` defaults to 10, **max 50** — CLI `--size` flags default to 10
  to match the API; pass `--size 50` for bulk `--page-all` exports (fewer requests).
  Responses wrap rows in `{"page_info": {"page_count", "count", …}, "data": [...]}`
  (reports use a top-level `page_count`/`rows` shape). `HappyFoxClient.paginate()`
  yields flat records; `paginate_pages()` yields raw page bodies (used for NDJSON
  streaming). Both handle either envelope.
- **Custom-field encoding:** ticket fields use the key `t-cf-<id>`, contact fields
  `c-cf-<id>` (create) / `ccf-<id>` (staff updates), assets a `custom_fields` object
  keyed by bare id. Values: text→string, number→int/float, dropdown→option id,
  **multiple-option→array of option ids**, date→`YYYY-MM-DD`. Field **ids come from the
  API** (`hfox system ticket-custom-fields`, etc.), NOT from agent-portal URLs.
  An all-blank `--cf <id>=` now raises `ValidationError`; the `--cf-json` escape hatch
  (`parse_cf_json`) sends a raw JSON `{id: value}` object with NO coercion (use it for
  explicit empties or precise types).
- **Contacts are the `users` endpoint.** Tickets are addressed by `ticket_number`
  (numeric `id`), distinct from the `display_id` (e.g. `#DC00000003`).
- **Writes need an acting staff id** (`staff`/`staff_id`/`created_by`/`updated_by`/
  `deleted_by`, depending on endpoint — note the inconsistent field names). Resolve via
  `obj.require_staff_id(...)`.
- **Attachments are multipart/form-data**, 25 MB total per request. Use `attach()`.
- All response timestamps are UTC.

## Configuration & secrets

Everything lives under `~/.hfox/` (override with `HFOX_CONFIG_DIR`). Single account
(no profiles).

- `token.json` — **secrets**: `api_key`, `auth_code`, `subdomain`, `region`, optional
  `base_url`. Written atomically (temp + `os.replace`) at mode `0600`; dir at `0700`.
- `config.toml` — non-secret: `subdomain`, `region`, `default_format`, `default_staff_id`.

Resolution precedence for every field: **env var > token.json/config.toml > default**.
Env overrides: `HFOX_SUBDOMAIN`, `HFOX_REGION`, `HFOX_API_KEY`, `HFOX_AUTH_CODE`,
`HFOX_BASE_URL`, `HFOX_STAFF_ID`, `HFOX_FORMAT`, `HFOX_CONFIG_DIR`.

Never log or echo `api_key`/`auth_code`. `auth status` masks the key.

## Exit codes (stable public contract — do not renumber)

`0` success · `1` API error · `2` auth error · `3` validation · `4` not found · `5` other.
Defined in `core/errors.py` (`ExitCode`). Each `HfoxError` subclass carries its code;
`main.app()` serializes the error to JSON on stdout and exits with the code.

Click-level errors are remapped in `main.app()` so the contract holds everywhere:

- **Usage errors** (unknown flag/command, missing argument) exit **3** with the same
  JSON error object on stdout and the human usage hint on stderr — click's default
  exit code 2 would collide with `ExitCode.AUTH`.
- **Bare group invocations** (`hfox`, `hfox tickets`) print help and exit **0**,
  same as `--help`.

## Testing

- `tests/` uses `pytest` + `typer.testing.CliRunner`; the HTTP layer is exercised with
  `httpx.MockTransport` (deterministic, no network, `sleep` is injected so backoff is
  instant). Use `--dry-run` assertions for request-shape coverage.
- When you add a command, add: a `--dry-run` test asserting method/URL/body, and (if it
  touches the client or encoding) a unit test. Keep the suite offline and fast.

## Scope

Shipped: tickets, contacts/groups, assets/types/custom-fields, system reference data,
auth. Planned follow-ups (already cataloged in the research output): Reports (8 views),
Knowledge Base export, and ticket custom-field **choice** management (dynamic dropdowns).
When adding these, follow the same module-per-resource pattern.
