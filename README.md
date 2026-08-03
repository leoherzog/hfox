# hfox

A modern command-line interface for the [HappyFox](https://www.happyfox.com/) REST API.

`hfox` gives you fast, scriptable access to every core HappyFox endpoint — tickets,
contacts, contact groups, assets, asset types, and reference data — with a clean
`noun verb` command structure inspired by the [Google Workspace CLI (`gws`)](https://github.com/googleworkspace/cli).
It is dual-audience by design: friendly for humans (`--help` everywhere, tables,
`--dry-run`) and first-class for scripts and AI agents (JSON by default, structured
errors, stable exit codes).

## Install / run

`hfox` is distributed as a [`uv` tool](https://docs.astral.sh/uv/), so no manual
virtualenv is needed.

```bash
# Run without installing (from a checkout):
uvx --from . hfox --help

# Or install it as a persistent tool:
uv tool install .
hfox --help

# Once published to PyPI:
uvx hfox --help
uv tool install hfox
```

Requires Python 3.11+.

## Authenticate

HappyFox authenticates with an **API key** and an **auth code** sent as HTTP Basic
credentials. Generate them in HappyFox under *Manage → Integrations → HappyFox API*.

```bash
hfox auth login            # prompts for subdomain, region, API key, auth code
hfox auth login \
  --subdomain acme --region us \
  --api-key XXXX --auth-code YYYY \
  --email agent@acme.com     # resolves a default staff id from your email
hfox auth status
hfox auth logout
```

Login validates the credentials against `/staff/` and stores them in
`~/.hfox/token.json` (chmod `0600`), with non-secret settings in `~/.hfox/config.toml`.

### Configuration

Everything lives under `~/.hfox/` (override with `HFOX_CONFIG_DIR`):

| File | Contents |
|------|----------|
| `token.json` | secrets: `api_key`, `auth_code`, `subdomain`, `region` (mode `0600`) |
| `config.toml` | non-secret: `subdomain`, `region`, `default_format`, `default_staff_id` |

Environment overrides (handy for CI): `HFOX_SUBDOMAIN`, `HFOX_REGION`,
`HFOX_API_KEY`, `HFOX_AUTH_CODE`, `HFOX_BASE_URL`, `HFOX_STAFF_ID`, `HFOX_FORMAT`.

EU-hosted accounts are reached automatically via `--region eu` (`*.happyfox.net`);
custom domains work by passing the full host as `--subdomain` (e.g. `support.acme.com`).

## Global flags

| Flag | Purpose |
|------|---------|
| `-f, --format {json\|table\|csv\|yaml}` | output format (default `json`) |
| `--dry-run` | build and print the request without sending it |
| `--page-all` | auto-paginate list commands; with `json` format, streams NDJSON (one page per line) |
| `--page-limit N` / `--page-delay MS` | bound auto-pagination (default 10 pages / 100 ms) |
| `--staff-id N` | acting staff id for write actions |
| `-q, --quiet` | suppress status messages |
| `--config-dir DIR` | override the config directory (default `~/.hfox`; envvar `HFOX_CONFIG_DIR`) |
| `--no-color` | disable color (also honors `NO_COLOR`) |
| `--version` | show version and exit |

> Note: `-q` is the global `--quiet` short flag, but `tickets list` and
> `contacts list` rebind `-q` to `--query` (full-text search). On those commands
> `-q` takes a search string rather than toggling quiet mode. Multi-word ticket
> queries are ANDed — `-q "degree works"` matches tickets containing both words
> anywhere, in any order; quoting does not force an exact-phrase match.

Exit codes are stable: `0` success, `1` API error, `2` auth error, `3` validation,
`4` not found, `5` other. Usage errors (unknown flag/command, missing argument)
exit `3` with a JSON error on stdout; bare group invocations (`hfox tickets`)
print help and exit `0`, like `--help`.

## Command overview

```
hfox auth      login | status | logout
hfox tickets   list | get | create | create-bulk | reply | note | user-reply
               update-cf | tags | subscribe | unsubscribe | forward | move | delete
hfox contacts  list | get | create | update | create-bulk
hfox contacts groups   list | get | create | update | add-contacts | remove-contacts
hfox assets    list | get | create | update | delete
hfox assets types          list | get
hfox assets custom-fields  list | get
hfox system    categories | staff | statuses | ticket-custom-fields | contact-custom-fields
```

## Examples

```bash
# Reference data (ids you need elsewhere)
hfox system categories -f table
hfox system staff -f table

# Tickets
hfox tickets list --status _all --size 50 -f table
hfox tickets list -q 'priority:"CRITICAL"' --page-all
hfox tickets get 1234 --show-cf-changes
hfox tickets create --subject "Printer down" --category 3 \
  --name "Han Solo" --email han@rebels.org --text "It is on fire." \
  --cf 7=Urgent --attachment ./photo.png
hfox tickets reply 1234 --text "On it." --status 5 --update-customer
hfox tickets move 1234 --to-category 4 --note "Wrong queue"
hfox tickets delete 1234 --yes

# Contacts & groups
hfox contacts list -q han -f table
hfox contacts create --name "Leia" --email leia@rebels.org --cf 4=VIP
hfox contacts groups add-contacts 12 --contacts 41,200 --access-tickets

# Assets
hfox assets list --asset-type 1 -f table
hfox assets create --asset-type 1 --name "MBP-14" --display-id "LAP-001" \
  --cf 5=4 --cf 6=3,4
```

### Custom fields

Pass `--cf <id>=<value>` (repeatable). Values with commas become multi-option
arrays; numbers are coerced automatically:

```bash
--cf 3="In Progress"     # text / dropdown label
--cf 5=4                 # dropdown option id
--cf 6=3,4,5             # multiple-option ids -> [3,4,5]
--cf 7=2025-12-25        # date
```

An all-blank value such as `--cf 5=` is rejected with a validation error. To send a
raw custom-field value with no coercion (commas, numbers, or explicit empties left
as-is), use the `--cf-json` escape hatch with a JSON object mapping field id to value,
e.g. `--cf-json '{"5": ""}'`.

Custom-field ids come from `hfox system ticket-custom-fields` /
`hfox system contact-custom-fields` / `hfox assets custom-fields list` — **not** the
ids shown in agent-portal URLs.

## Architecture

A clean two-layer split (UI-agnostic library vs. presentation):

```
src/hfox/
├── core/     # HappyFoxClient (httpx, Basic auth, 429 backoff, pagination),
│             # config/secrets, error + exit-code model — no Typer, no printing
└── cli/      # Typer apps, output formatting, auth, one module per resource
```

## Development

```bash
uv sync                          # set up the environment
uv run hfox --help               # run from source
uv run python -m pytest          # tests (offline — httpx MockTransport, no network)
uv run ruff check .              # lint
uv build                         # build sdist + wheel
```

Run `pytest` via `uv run python -m pytest` (rather than bare `uv run pytest`) — the
module form is the reliable invocation; the bare `pytest` entrypoint shebang can break
if the venv is not freshly created.

Contributing or extending the CLI? See **[AGENTS.md](AGENTS.md)** for the architecture,
the `AppContext` contract every command uses, how to add a command or resource, and the
HappyFox API quirks (auth, base URL, custom-field encoding, pagination, rate limits).

## Scope

This first release covers tickets, contacts/groups, assets/types, system reference
data, and auth. Reports, Knowledge Base export, and ticket custom-field choice
management (dynamic dropdowns) are planned follow-ups.
