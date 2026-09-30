# hfox

A command-line interface for the [HappyFox](https://www.happyfox.com/) REST API.

`hfox` covers tickets, contacts, contact groups, assets, asset types and reference data
with a `noun verb` structure modeled on the [Google Workspace CLI (`gws`)](https://github.com/googleworkspace/cli).
Humans get `--help`, tables and `--dry-run`; scripts and AI agents get JSON by default,
structured errors and stable exit codes.

## Install / run

`hfox` is a [`uv` tool](https://docs.astral.sh/uv/) and requires Python 3.11+.

```bash
uvx --from . hfox --help     # run from a checkout without installing
uv tool install .            # or install it as a persistent tool
uvx hfox --help              # once published to PyPI
```

## Authenticate

`hfox` needs an **API key** and its **auth code**. In HappyFox, open *Apps → Goodies → API*,
click **Enable**, add a key with **+**, then hover its row and click **see auth code**.

```bash
hfox auth login            # prompts for subdomain, region, API key, auth code
hfox auth login --subdomain acme --region us \
  --api-key XXXX --auth-code YYYY \
  --email agent@acme.com   # sets a default staff id from your email
hfox auth status
hfox auth logout
```

Login checks the credentials against `/staff/` before saving them.

### Configuration

Everything lives under `~/.hfox/` (override with `HFOX_CONFIG_DIR`):

| File | Contents |
|------|----------|
| `token.json` | secrets: `api_key`, `auth_code`, `subdomain`, `region`, optional `base_url` (mode `0600`) |
| `config.toml` | non-secret: `subdomain`, `region`, `default_format`, `default_staff_id` |

Environment overrides: `HFOX_SUBDOMAIN`, `HFOX_REGION`, `HFOX_API_KEY`, `HFOX_AUTH_CODE`,
`HFOX_BASE_URL`, `HFOX_STAFF_ID`, `HFOX_FORMAT`.

`--region eu` targets `*.happyfox.net`; a custom domain goes in `--subdomain` as the full host
(`support.acme.com`). For proxied accounts, set `HFOX_BASE_URL` to an `http(s)://host` root;
a trailing `/api/1.1/json` is stripped.

## Global flags

Global flags go before the resource: `hfox -f table tickets list`, not `hfox tickets list -f table`.

| Flag | Purpose |
|------|---------|
| `-f, --format {json\|table\|csv\|yaml}` | output format (default `json`) |
| `--dry-run` | print the exact request without sending it |
| `--page-all` | fetch up to `--page-limit` pages from `--page`; JSON streams NDJSON, one page per line |
| `--page-limit N` / `--page-delay MS` | bound `--page-all` (default 10 pages / 100 ms); warns when the limit cuts it short |
| `--staff-id N` | acting staff id for write actions |
| `-q, --quiet` | suppress status messages |
| `--config-dir DIR` | override the config directory |
| `--no-color` | disable color (also honors `NO_COLOR`) |
| `--version` | show version and exit |

For full exports, pass `--size 50`. On `tickets list`, add a stable `--sort` such as
`ticketa`; `-q` searches always sort by relevance.

On `tickets list` and `contacts list`, `-q` means `--query`:

- Tickets: text or filters such as `status:"In Progress","New"`; a comma means any-of and a
  space replaces the docs' `+`. Multi-word text is ANDed, which HappyFox does not document.
- Contacts: `field:value` filters on `name`, `email`, `phone`, `updated_since` or
  `created_since`, ANDed when space-separated. Omit `+` from phone numbers.

Exit codes are stable: `0` success, `1` API error, `2` auth error, `3` validation, `4` not
found, `5` other. Usage errors exit `3` with a JSON error on stdout. Bulk and group-membership
commands print the response, then exit `1` if any entry failed; removing a contact that is not
in the group does not count. Bare group invocations (`hfox tickets`) print help and exit `0`.

## Command overview

```
hfox auth      login | status | logout
hfox tickets   list | get | create | create-bulk | inline-attachment | reply | note | user-reply
               update-cf | tags | subscribe | unsubscribe | forward | move | delete
hfox contacts  list | get | create | update | create-bulk
hfox contacts groups   list | get | create | update | add-contacts | remove-contacts
hfox assets    list | get | create | update | delete
hfox assets types          list | get
hfox assets custom-fields  list | get
hfox system    categories | priorities | staff | statuses | ticket-custom-fields | contact-custom-fields
```

Ticket commands take the numeric ticket id, not the display id. `contacts create` also edits
the contact with the same email and resets custom fields it does not send.

## Examples

```bash
# Reference data (ids you need elsewhere)
hfox -f table system priorities
hfox -f table system ticket-custom-fields    # choices shown as text=id

# Tickets
hfox -f table tickets list --status _all --size 50 --fields id,display_id,subject
hfox --page-all tickets list -q 'priority:"CRITICAL"' --size 50
hfox tickets list -q 'id:DC00000003'         # find the numeric id of a display id
hfox tickets get 1234 --show-cf-changes
hfox tickets create --subject "Printer down" --category 3 \
  --name "Han Solo" --email han@rebels.org --text "It is on fire." \
  --cf 7=Urgent --attachment ./photo.png
hfox tickets reply 1234 --text "On it." --status 5 --update-customer
hfox tickets note 1234 --status 3 --unassign  # change properties without a message
hfox tickets inline-attachment ./diagram.png  # temporary url for an <img src>
hfox tickets move 1234 --to-category 4 --note "Wrong queue"
hfox tickets delete 1234 --yes

# Contacts & groups
hfox -f table contacts list -q name:han
hfox contacts create --name "Leia" --email leia@rebels.org --cf 4=VIP
hfox contacts groups add-contacts 12 --contacts 41,200 --access-tickets

# Assets
hfox -f table assets list --asset-type 1
hfox assets create --asset-type 1 --name "MBP-14" --display-id "LAP-001" \
  --cf 5=4 --cf '6=[3,4]'
```

### Custom fields

`--cf <id>=<value>` is repeatable. Canonical decimals become numbers, `<id>=[a,b]` sends a
list, and anything else is sent as the exact string.

```bash
--cf 3="In Progress"     # text field
--cf 5=4                 # dropdown choice id
--cf '6=[3,4,5]'         # multiple-option ids -> [3,4,5]; '6=[4]' -> [4]
--cf 7=2025-12-25        # date
--cf 8=02134             # stays the string "02134"
```

An all-blank value such as `--cf 5=` exits 3. `--cf-json '{"5": ""}'` sends values uncoerced.
With `--attachment`, `null` values are dropped, or rejected on reply and note. `tickets create`,
`reply` and `note` take contact fields through `--contact-cf` and `--contact-cf-json`.

Keys are numeric ids from `hfox system ticket-custom-fields`, `contact-custom-fields` or
`hfox assets custom-fields list`, not agent-portal URLs. A key may carry its endpoint's
prefix (`t-cf-5`); any other key exits 3.

## Architecture

```
src/hfox/
├── core/     # HTTP client, config/secrets, errors and exit codes; no Typer, no printing
└── cli/      # Typer apps and output formatting, one module per resource
```

## Development

```bash
uv sync                          # set up the environment
uv run hfox --help               # run from source
uv run python -m pytest          # offline tests
uv run ruff check .              # lint
uv build                         # build sdist + wheel
```

Read [AGENTS.md](AGENTS.md) before extending the CLI.

## Scope

Reports, Knowledge Base export and ticket custom-field choice management are planned.
