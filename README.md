# 🦊 hfox

A command-line interface for the [HappyFox](https://www.happyfox.com/) REST API.

`hfox` covers tickets, contacts, contact groups, assets, asset types and reference data
with a `noun verb` structure modeled on the [Google Workspace CLI (`gws`)](https://github.com/googleworkspace/cli).
Humans get `--help`, tables and `--dry-run`; scripts and AI agents get JSON by default,
structured errors and stable exit codes.

## Install / run

`hfox` is a [`uv` tool](https://docs.astral.sh/uv/) and requires Python 3.11+.

```bash
uv tool install hfox         # install it as a persistent tool
uvx hfox --help              # or run it without installing
uvx --from . hfox --help     # run from a checkout
```

Each [GitHub release](https://github.com/leoherzog/hfox/releases) also attaches single-file
binaries for Linux, macOS and Windows that need no Python. Mark a download executable with
`chmod +x`; on macOS, a browser download also needs `xattr -d com.apple.quarantine <file>`.
Every binary, the wheel and the sdist carry a build provenance attestation, checked with
`gh attestation verify <file> --repo leoherzog/hfox`.

## Authenticate

`hfox` needs an **API key** and its **auth code**. In HappyFox, open *Apps → Goodies → API*,
click **Enable**, add a key with **+**, then hover its row and click **see auth code**.

```bash
hfox auth login                        # prompts for subdomain, region, API key, auth code
hfox auth login --subdomain acme --email agent@acme.com   # also sets your default staff id
HFOX_API_KEY=... HFOX_AUTH_CODE=... hfox auth login --subdomain acme   # scripted
hfox auth status --check
hfox auth logout
```

Login checks the credentials against `staff/` before saving them. It takes each value from
its flag, then from `HFOX_SUBDOMAIN`, `HFOX_REGION`, `HFOX_API_KEY` or `HFOX_AUTH_CODE`, then
from a prompt on stderr. Keep the two secrets off the command line, where the shell history
and the process list expose them. When stdin is not a terminal, a missing region means `us`,
and a missing subdomain, key or code exits 3 with an error naming each missing flag and its
variable.

`--email` saves the id of the one agent with that email as the default staff id. Without
`--email`, a login to the same subdomain, region and base URL keeps the stored default, and
any other login removes it. An `--email` that matches no agent, several agents or an agent
with an unusable id sets no default and removes a stored one; the login still succeeds.
`default_staff_id` in the login JSON is the value stored after the save.

Each `auth` command prints one document that never holds a secret. `auth status` masks
the API key and exits 2 when credentials are missing, after printing its payload.
`auth status --check` sends one request and adds `verified`; a failed check adds
`check_error` and exits with that error's code. A `staff/` answer that is not a list fails
the check with exit 5.

### Configuration

The config directory is `$XDG_CONFIG_HOME/hfox` when that variable holds an absolute path,
otherwise `~/.config/hfox`, on every OS. `--config-dir` or `HFOX_CONFIG_DIR` overrides both.

| File | Contents |
|------|----------|
| `token.json` | secrets: `api_key`, `auth_code`, `subdomain`, `region`, optional `base_url` (mode `0600`) |
| `config.toml` | non-secret: `subdomain`, `region`, `default_format`, `default_staff_id` |

Every field resolves as environment variable, then file, then default. `config.toml` also
accepts a hand-written `base_url`, read after `HFOX_BASE_URL` and `token.json`.

A file that cannot be read or parsed, or that holds a non-string `subdomain`, `region`,
`base_url`, `api_key` or `auth_code`, exits 5 with an error of type `config` that names the
file and the key. So does a home directory that cannot be determined, or a `~` in the config
directory that cannot be expanded; give the config directory as an absolute path.

| Variable | Sets |
|----------|------|
| `HFOX_SUBDOMAIN`, `HFOX_REGION` | the account host |
| `HFOX_API_KEY`, `HFOX_AUTH_CODE` | the credentials |
| `HFOX_BASE_URL` | an `http(s)://host` root for a proxied account |
| `HFOX_STAFF_ID` | the default acting staff id |
| `HFOX_FORMAT` | the default output format |
| `HFOX_TIMEOUT`, `HFOX_MAX_RETRIES` | the defaults of `--timeout` and `--max-retries` |
| `HFOX_CONFIG_DIR` | the config directory |

`--region eu` targets `*.happyfox.net`; a custom domain goes in `--subdomain` as the full host
(`support.acme.com`) and ignores the region. For proxied accounts, set `HFOX_BASE_URL`; a
trailing `/api/1.1/json` is stripped and `auth login` stores the value. A stored base URL
wins over `--subdomain`, so `auth login --subdomain` warns on stderr when one is in effect.

A base URL with another scheme, no host, a bad port, embedded credentials, a query or a
fragment exits 3. The error leaves out any value that holds `@`, `?` or `#`, since it may
carry a secret. An `http` base URL on a non-loopback host sends the credentials in cleartext;
hfox warns on stderr once per invocation and proceeds.

`HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY` and `NO_PROXY` are honored through httpx. TLS trust
comes from the certifi bundle unless `SSL_CERT_FILE` or `SSL_CERT_DIR` names another one.
Redirects are never followed.
[SECURITY.md](https://github.com/leoherzog/hfox/blob/main/SECURITY.md) describes the
credential model.

## Global flags

Global flags go before the resource: `hfox -f table tickets list`, not `hfox tickets list -f table`.

| Flag | Purpose |
|------|---------|
| `-f, --format {json\|table\|csv\|yaml}` | output format (default `json`) |
| `--dry-run` | print the exact request without sending it |
| `--page-all` | fetch up to `--page-limit` pages from `--page`; JSON streams NDJSON, one page per line |
| `--page-limit N` / `--page-delay MS` | bound `--page-all` (default 10 pages / 100 ms) |
| `--staff TEXT` / `--staff-id N` | acting staff for write actions, by email or name, or by id |
| `--timeout SECONDS` | per-attempt timeout for connecting and for each read or write (default 30) |
| `--max-retries N` | retries after HTTP 429 or a network error (default 5) |
| `--quiet` | suppress status messages and retry notices |
| `--config-dir DIR` | override the config directory |
| `--no-color` | disable color (also honors `NO_COLOR`) |
| `-v, --version` | show the release version, or `dev` when run from source |
| `--install-completion` / `--show-completion` | install or print shell completion |

A global flag after the resource exits 3 with an error of type `usage` that says where the
flag goes. That also holds when the flag follows an option that takes a value, so
`tickets note 5 --text --dry-run` is refused. Write `--text=--dry-run` to send such a literal.
In that position a short flag with its value attached, such as `-ftable`, is sent as the
value.

A global option that takes a value refuses one that starts with `-`, in the `--opt=value`
form too, so `hfox --staff --dry-run tickets list` exits 3 with type `usage` instead of
reading `--dry-run` as the staff name.

An unknown format from `--format`, `HFOX_FORMAT` or `default_format` exits 3. In `config.toml`
only an empty string means unset, so `default_format = false` is an unknown format.

`--timeout` does not bound the whole command: retries, their backoff and any `Retry-After`
wait come on top. Each retry prints one stderr line with the reason and the wait. GETs retry
any network error; writes retry only when no connection was made.

## Listing and search

For full exports, pass `--size 50` with `--page-all`. When `--page-limit` stops the walk
early, hfox warns on stderr and still exits 0, so raise the limit for a complete export. On
`tickets list`, add a stable `--sort` such as `ticketa`; `-q` searches always sort by
relevance.

On `tickets list` and `contacts list`, `-q` means `--query`:

- Tickets: text or filters such as `status:"In Progress","New"`; a comma means any-of and a
  space replaces the docs' `+`. Multi-word text is ANDed, which HappyFox does not document.
- Contacts: `field:value` filters on `name`, `email`, `phone`, `updated_since` or
  `created_since`, ANDed when space-separated. Omit `+` from phone numbers.

Endpoints without server-side search take local filters instead. Every `system` command and
`contacts groups list` take `--name`, and `system staff` also takes `--email`. A filter keeps
rows whose field contains the text, ignoring case; every filter must match, and a missing or
non-text field never matches. Filters are never sent to HappyFox, and no match prints an empty
listing with exit `0`.

`assets list`, `assets types list` and `assets custom-fields list` take `--page` and `--size`.
They take `--name` only with the global `--page-all`, since one page would miss matches. Each
NDJSON line keeps the server's `page_info`, which counts rows before filtering.

## Acting staff

Commands that HappyFox records against an agent take `--staff`, an email or a name, or
`--staff-id`, never both. The pair is on ticket `reply`, `note`, `update`, `update-cf`,
`tags`, `forward`, `move` and `delete`, and on asset `create`, `update` and `delete`. On
`tickets subscribe` and `unsubscribe` the pair names the agent being added or removed.

The identity resolves in this order: the flag on the command, the global flag,
`HFOX_STAFF_ID`, then the default saved by `auth login --email`. `--staff` reads `staff/` and
takes the agent whose whole email, or failing that whole name, equals the text, ignoring
case. No match, several matches and a value made of digits exit 3. The lookup is a read, so
it is also sent under `--dry-run` and needs credentials.

`--assignee`, `--assign-to` and `--agents` take staff ids; `--alert` takes `s`, `c` or a
staff id. None accepts an email or name. Find the ids with `hfox system staff`.

## Input from files and stdin

Message bodies can come from a file: `--text-file` and `--html-file` on `tickets create`,
`reply` and `note`, `--text-file` on `user-reply`, `--message-file` on `forward` and
`--note-file` on `move`. Pass `-` to read stdin. The inline flag and its file form exclude
each other, and a file or stdin body that is empty or whitespace only exits 3.

The JSON `--file` flags of `tickets create-bulk`, `contacts create-bulk` and
`tickets set-cf-choices` accept `-` as well. Only one input per invocation may read stdin.
Files and stdin are read as strict UTF-8, and a leading byte order mark is dropped.
`--attachment` always names a file.

A path must name a regular file. A directory, FIFO, device or process substitution exits 3
with "is not a regular file", and a missing path exits 3 with "not found".

hfox refuses to read a file you name when it lies inside the config directory, including
through a symlink or a hard link to `token.json`. It also refuses to send a file or stdin
whose content holds the configured API key or auth code. Both checks run under `--dry-run`.

## Confirmation

`tickets delete`, `assets delete` and `tickets set-cf-choices` ask before acting, on stderr.
`--yes` skips the question, and `--dry-run` never asks. When stdin is not a terminal the
command exits 3 unless `--yes` is given. Declining, or pressing Ctrl-C at the prompt, prints
an error of type `cancelled` and exits 5.

## Output

Status lines, warnings and prompts go to stderr, so stdout stays parseable. CSV ends every
row with one CRLF on every platform, and a newline inside a cell stays a bare LF.

Table and CSV cells drop control, bidi and zero-width characters, so ticket text cannot drive
the terminal. Newline, tab and the zero-width joiner and non-joiner are kept. A CSV text cell
that starts with `=`, `+`, `-`, `@` or a tab gets a leading apostrophe so a spreadsheet does
not run it as a formula; plain signed decimals such as `-5` and `+15551234567` stay as they
are.

JSON and NDJSON write C1 controls, bidi controls, line and paragraph separators and tag
characters as `\uXXXX` escapes, and YAML escapes non-printable characters in a quoted string.
A parser returns the same strings.

## Errors and exit codes

| Code | Meaning |
|------|---------|
| `0` | success |
| `1` | HappyFox returned an error response, or part of a bulk request failed |
| `2` | missing or rejected credentials |
| `3` | bad arguments or input |
| `4` | resource not found (HTTP 404) |
| `5` | anything else: network, timeout, config file, cancelled prompt, internal |

A failure prints one JSON object on stdout:

```json
{
  "error": "Rate limit exceeded (HTTP 429). HappyFox enforces a 10-minute cooldown after the limit is hit.",
  "type": "rate_limited",
  "exit_code": 1,
  "status_code": 429,
  "retry_after": 600
}
```

`error`, `type` and `exit_code` are always present. `status_code`, `detail`, `hint`,
`retry_after` and `outcome_unknown` appear only when set. `type` is one of `api`,
`rate_limited`, `auth`, `validation`, `usage`, `not_found`, `network`, `timeout`, `config`,
`cancelled`, `internal` and `other`. `usage` is a structural mistake such as an unknown or
misplaced flag, a missing argument or a global option without its value, and `validation` is
a rejected value.

`retry_after` is the server's `Retry-After` in seconds on a 429 that outlasted the retries.
`outcome_unknown: true` marks a write that failed after the request may have left, or that
HappyFox answered with HTTP 500 or above. The write may have been applied, so check the
resource before sending it again. A failure to connect, a pool timeout, a proxy error and an
unsupported URL scheme never set it, and neither does a GET.

A nonzero exit carries that error object except in these cases:

- A partial bulk or group-membership failure prints the response and exits 1. Removing a
  contact that is not in the group does not count.
- `auth status` prints its payload when it exits 2 and when `--check` fails.
- With `--page-all` in JSON, the error is the last NDJSON line.
- Ctrl-C outside a prompt exits 130 and prints no error object.

Usage errors also print the usage hint on stderr, except a misplaced global flag and a global
option without its value. Bare group invocations (`hfox tickets`) print help and exit `0`.

## Stability

These are contract from 1.0 on: the JSON that hfox itself produces, CSV output, exit codes,
flag names and environment variable names. The hfox JSON is the error object, the
`--dry-run` preview `{dry_run, method, url, params, body, attachments}`, the `auth` payloads
and the NDJSON framing of one page per line. Truncation at `--page-limit` keeps exit 0 and
the stderr warning.

These are not contract: table layout, the files inside the config directory, the bodies
HappyFox returns and hfox passes through, and the content of `detail` apart from `staff_ids`
on an ambiguous `--staff`.

Minor releases may add keys, `type` values, flags and environment variables, so ignore the
ones you do not know. Removing or renaming any of them is a breaking change. The names
`--columns`, `--profile` and `HFOX_PROFILE` are reserved.

## Command overview

```
hfox auth      login | status | logout
hfox tickets   list | get | create | create-bulk | inline-attachment | reply | note | user-reply
               update | update-cf | tags | subscribe | unsubscribe | forward | move | delete
               set-cf-choices
hfox contacts  list | get | create | update | create-bulk
hfox contacts groups   list | get | create | update | add-contacts | remove-contacts
hfox assets    list | get | create | update | delete
hfox assets types          list | get
hfox assets custom-fields  list | get
hfox system    categories | priorities | staff | statuses | ticket-custom-fields | contact-custom-fields
```

Ticket commands take the numeric ticket id, not the display id. `tickets update` changes
status, priority, assignee, due date, tags, time spent and custom fields without posting a
message. `--unassign` on `create`, `reply`, `note` and `update` sends a null assignee.
`contacts create` and `create-bulk` also edit the contact with the same email and reset
custom fields they do not send.

## Examples

```bash
# Reference data (ids you need elsewhere)
hfox -f table system priorities
hfox -f table system ticket-custom-fields    # choices shown as text=id
hfox system staff --email jane@               # find a staff id

# Tickets
hfox -f table tickets list --status _all --size 50 --fields id,display_id,subject
hfox --page-all tickets list -q 'priority:"CRITICAL"' --size 50
hfox tickets list -q 'id:DC00000003'         # find the numeric id of a display id
hfox tickets get 1234 --show-cf-changes
hfox tickets create --subject "Printer down" --category 3 \
  --name "Han Solo" --email han@rebels.org --text "It is on fire." \
  --cf 7=Urgent --attachment ./photo.png
hfox tickets reply 1234 --text "On it." --status 5 --update-customer
hfox tickets reply 1234 --html-file reply.html --staff jane@acme.com
git log -5 | hfox tickets note 1234 --text-file -
hfox tickets update 1234 --status 3 --unassign   # change properties without a message
hfox tickets inline-attachment ./diagram.png  # temporary url for an <img src>
hfox tickets move 1234 --to-category 4 --note "Wrong queue"
hfox --dry-run tickets delete 1234            # preview, no prompt
hfox tickets delete 1234 --yes

# Contacts & groups
hfox -f table contacts list -q name:han
hfox contacts create --name "Leia" --email leia@rebels.org --cf 4=VIP
hfox contacts create-bulk --file - < contacts.json
hfox contacts groups add-contacts 12 --contacts 41,200 --access-tickets

# Assets
hfox -f table assets list --asset-type 1
hfox --page-all -f table assets list --asset-type 1 --size 50 --name latitude
hfox assets types list --page 2 --size 50
hfox assets create --asset-type 1 --name "MBP-14" --display-id "LAP-001" \
  --cf 5=4 --cf '6=[3,4]' --staff-id 12
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

### Custom-field choices

`tickets set-cf-choices <field_id>` replaces the whole choice list of a ticket dropdown or
multiple-option field. The choices come from `--file`, stdin or `--choices-json`, as a JSON
array or as an object `{"choices": [...]}`.

```bash
hfox tickets set-cf-choices 7 --yes \
  --choices-json '[{"id": 11, "text": "Low"}, {"id": 12, "text": "High"}, {"id": null, "text": "Urgent"}]'
```

Every choice needs a non-blank `text` and an `id`. An existing choice keeps its id, a new one
has `"id": null`, and an existing choice left out of the list is deleted. Read the current
ids with `hfox system ticket-custom-fields` first and carry each one you want to keep. The
command states how many choices are kept and how many are new before it asks.

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

Read [AGENTS.md](https://github.com/leoherzog/hfox/blob/main/AGENTS.md) before extending the
CLI. [skills/hfox/SKILL.md](https://github.com/leoherzog/hfox/blob/main/skills/hfox/SKILL.md)
is a usage guide for AI agents that drive it.
