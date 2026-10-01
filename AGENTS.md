# AGENTS.md

Guidance for AI coding agents and human contributors working on **`hfox`**. The repository
root is the package (`pyproject.toml`, `src/hfox/`, `tests/`).

## Project overview

`hfox` is a dual-audience CLI for the HappyFox REST API (`api/1.1/json`), with the
`hfox <resource> <verb>` structure of the [Google Workspace CLI (`gws`)](https://github.com/googleworkspace/cli).
Humans get `--help`, Rich tables, `--dry-run` and confirmation prompts; machines get JSON by
default, structured errors, stable exit codes and `--page-all`. It ships as a
[`uv` tool](https://docs.astral.sh/uv/) installed with `uv tool install hfox` or run via
`uvx hfox`. `skills/hfox/SKILL.md` is the usage guide for agents that drive the CLI; keep it
and `README.md` in step with any user-visible change.

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
created. `.github/workflows/ci.yml` runs ruff and pytest on every push and pull request, on
Linux, macOS and Windows with Python 3.11 to 3.14.

Typer is capped below the next minor (`typer>=0.27.2,<0.28`) because `cli/main.py` imports
the private `typer._click`. Raise the cap only after checking that import.

### Releases

Publishing a GitHub release runs `.github/workflows/release.yml`. Draft the release in the
web UI with a tag that is `v` plus a canonical PEP 440 public version (`v1.0.0`,
`v1.0.0rc1`); any other tag fails the stamping step. Source keeps the placeholders
`version = "0.0.0"` in `pyproject.toml` and `__version__ = "dev"`, so `hfox --version`
reports `dev` for any build that is not a release.

The jobs run in this order:

1. `binaries` stamps `__version__`, builds a PyInstaller binary per platform and smoke-tests
   `--version`, a dry run and `--show-completion`. It leaves `pyproject.toml` alone, since
   `uv run --locked` rejects a changed project version.
2. `dist` stamps both `pyproject.toml` and `__version__`, runs `uv build` and checks that the
   wheel reports the tag version.
3. `assets` attests every binary, the wheel and the sdist, then attaches all of them to the
   release.
4. `pypi` publishes the wheel and sdist through trusted publishing in the `pypi` environment,
   with no token secret.

PyPI accepts each version once, so `pypi` runs last and a failed run can be re-run from the
Actions tab. The release is public while the jobs run, so GitHub's immutable releases setting
must stay off for the repo. `assets` marks the release as a prerelease when the version has
an `a`, `b`, `rc` or `.dev` segment, so it does not stay Latest. A `.post` version is a
normal release.

A manual `workflow_dispatch` run stops after `binaries` and `dist` and only uploads workflow
artifacts. Actions are pinned by commit SHA with a version comment; look up the current SHA
with `git ls-remote --tags` before changing one. Whether attestation needs
`artifact-metadata: write`, and the macOS and Windows completion smoke tests, are unverified
until the first release.

Build the same binary locally into `dist/` with:

```bash
uv run --isolated --no-dev --group build pyinstaller --onefile --name hfox --hidden-import shellingham.posix --hidden-import shellingham.nt --specpath build src/hfox/__main__.py
```

The two hidden imports are required: shellingham picks its platform module with a dynamic
import that PyInstaller cannot see, and shell completion fails without them.

## Architecture

Two layers with a hard boundary:

```
src/hfox/
├── __init__.py           # __version__ ("dev" in source, stamped by the release workflow)
├── __main__.py           # `python -m hfox`; the script PyInstaller freezes
├── core/                 # UI-agnostic: no Typer, no printing, no sys.exit
│   ├── client.py         # HappyFoxClient: httpx, Basic auth, retries, on_retry, paginate()
│   ├── config.py         # config directory resolution, Config, token.json + config.toml I/O,
│   │                     #   ConfigError, the guard lists, cleartext_host
│   └── errors.py         # HfoxError hierarchy, type slugs + ExitCode (stable, public contract)
└── cli/                  # presentation: Typer apps, formatting, auth, command groups
    ├── main.py           # root Typer app, global flags, misplaced-flag check, app() entry
    ├── context.py        # AppContext (ctx.obj): the contract every command uses
    ├── output.py         # OutputFormat + json/table/csv/yaml rendering, sanitizing, stderr
    ├── auth.py           # `hfox auth` login/status/logout
    ├── cf.py             # custom-field parsing (t-cf-/c-cf-/ccf-/asset)
    ├── _util.py          # validation, CSV/JSON parsing, guarded file reads, attach(),
    │                     #   bulk results, filters, shared help text
    ├── tickets.py        # `hfox tickets`  (one module per resource)
    ├── contacts.py       # `hfox contacts` (+ `groups` sub-app)
    ├── assets.py         # `hfox assets`   (+ `types`, `custom-fields` sub-apps)
    └── system.py         # `hfox system`   (read-only reference data)
```

**Rule:** `core/` must never import from `cli/`, never prints and never exits. Anything core
must surface goes through a return value, an exception or a callback (`on_retry`, `warn`).
A command module never builds `httpx` requests, constructs a client, prompts or touches the
filesystem or stdin directly; it goes through `AppContext` and the `_util`/`cf` helpers.

### Request flow

```
hfox <args> → Typer (main.cli) → callback builds AppContext from global flags + Config
            → command module → AppContext.call()/paginate() → HappyFoxClient → HappyFox API
            → AppContext.render()/render_list() → OutputFormat rendering → stdout
```

`main.app()` runs with `standalone_mode=False` and converts every `HfoxError` into a JSON
error on stdout plus its exit code, so commands **raise** `HfoxError` subclasses rather than
printing and exiting.

### Global flags

The root callback declares, in this order: `--format/-f`, `--dry-run`, `--page-all`,
`--page-limit`, `--page-delay`, `--staff`, `--staff-id`, `--timeout SECONDS`, `--max-retries`,
`--quiet`, `--config-dir`, `--no-color`, `--version/-v`. `--quiet` is long-only, because `-q`
is `--query` on list verbs. `-v` is `--version`; there is no verbose or debug mode.

`--timeout` (`HFOX_TIMEOUT`) and `--max-retries` (`HFOX_MAX_RETRIES`) are Typer options with
`envvar=`, not `Config` fields. The callback rejects a timeout that is not finite and above
0, and `min=0` on `--max-retries` is the only guard against a negative value, which would
make the client send nothing. `_resolve_format` raises `ValidationError` for an unknown
format and names its source: `--format`, `HFOX_FORMAT` or `default_format` in the config file.
Only `None` and `""` count as unset, so `default_format = false`, `0` or `[]` is unknown.

`_RootGroup.parse_args` runs `_check_root_values` before click parses. A root option that
takes a value raises `UsageError` when that value starts with `-`, in the `--opt value` and
`--opt=value` forms and for each repeat. A numeric option keeps a negative number for its
range check. Without it `--staff --dry-run`
would send `--dry-run` as the staff name. The set of options comes from the callback's
params, so an added root option is covered.

A global flag placed after the resource raises `UsageError` saying where it goes. Two
detectors produce it. `_RootGroup.resolve_command` scans the tokens up to a `--` terminator
for a root-only long flag, bare or as `--flag=value`, which also catches one swallowed as the
value of the option before it. `app()` maps a `NoSuchOption` from a subcommand whose name is
a root option, which covers short forms and `--staff`/`--staff-id` on commands that do not
declare them. A subcommand must therefore never declare a long flag with a root-only name;
`tests/test_global_flags.py` walks the command tree to enforce it.

## The AppContext contract (read before adding a command)

Every command's first line is `obj = get_ctx(ctx)`. `AppContext` (in `cli/context.py`) is
the only API a command module uses to talk to HappyFox, read input, prompt or emit output:

| Method | Use for |
|--------|---------|
| `obj.call(method, path, *, params, json, data, files)` | single GETs and **all writes**; returns parsed JSON; `--dry-run` prints the exact wire request and exits |
| `obj.paginate(path, *, params, root_key="data", filters=None)` | paginated lists; honors `--dry-run`. `--page-all` + JSON **streams NDJSON**, one compact line per page or error, then exits, so `render_list` never runs; other formats get the flat record list. Without `--page-all`, the single page. Warns on stderr when `--page-limit` truncates and still exits 0. `filters={field: text}` applies `filter_rows` to every page, NDJSON lines included; without `--page-all` it raises `ValidationError`, even under `--dry-run`. Filters never reach the request |
| `obj.render(data)` | render one object/list in the active `--format` |
| `obj.render_list(body, *, root_key="data")` | render a listing: JSON keeps the `{page_info, data}` envelope; other formats unwrap to rows |
| `obj.resolve_staff_id(staff_id=None, staff=None)` | staff id from the command's `--staff-id` and `--staff` values; both given raises `ValidationError`. Order: command id, command `--staff` lookup, global `--staff-id`, global `--staff` lookup, then the configured default; may be `None` |
| `obj.require_staff_id(staff_id=None, staff=None)` | same, but raises `ValidationError` if none is available |
| `obj.lookup_staff(text)` | id of the one agent whose email, else name, equals `text` through `fold`. Reads GET `staff/` once per invocation, also under `--dry-run`, so it needs credentials. Blank text, digits-only text, no match and several matches raise `ValidationError`; `active` is not consulted |
| `obj.confirm(message, *, yes)` | ask before a destructive action, on stderr. Returns at once for `yes` or `--dry-run`. Raises `ValidationError` naming `--yes` when stdin is not a terminal, and `CancelledError` on a decline, Ctrl-C or EOF |
| `obj.prompt(text, *, default=None, hide_input=False)` | prompt on stderr and return the answer; maps Ctrl-C and EOF to `CancelledError`. It does not check for a terminal, so the caller tests `_util.stdin_is_tty()` first |
| `obj.read_text(source, flag)` | content of a file, or of stdin when `source` is `-`, unmodified apart from a dropped BOM. One stdin read per invocation; a second raises `ValidationError` naming both flags. Applies the config-directory guard and the credential check, and requires strict UTF-8 |
| `obj.read_json(source, flag)` | `read_text` parsed as strict JSON |
| `obj.text_input(inline, file, flag)` | the body given inline or through `<flag>-file`. Both given raises; a file or stdin body that is empty or whitespace only raises. Otherwise returns `inline` untouched, so the caller keeps its own blank handling |
| `obj.attach(body, attachments, *, field="attachments")` | kwargs to splat into `obj.call`, built by `_util.attach` with the guard and the credential check. `-` is an ordinary file name here |
| `obj.make_client(base_url, api_key, auth_code)` | build a `HappyFoxClient` carrying `--timeout`, `--max-retries` and the retry notice; warns once per invocation, even under `--quiet`, for an `http` URL on a non-loopback host. Only `auth login` calls it directly, for its probe |
| `obj.success(msg)` | status line to **stderr**, suppressed by `--quiet` |
| `obj.config` | the resolved `Config` (base URL, region, staff id, …) |

The input readers, `confirm` and staff resolution all run before `obj.call`, so a dry run
refuses the same inputs as a real run. The retry notice
(`Retrying in 1.1s (HTTP 429; retry 1 of 5).`) is printed by `AppContext._on_retry` unless
`--quiet` is set.

Helpers:

- `cli/_util.py`: `compact(dict)` drops `None`; `comma_join(list)`→`"a,b"`;
  `split_csv("a,b")`→`["a","b"]`, `None` when blank; `split_csv_ints("1,2")`→`[1,2]`, ASCII
  digits only, `[]` when blank. `nonblank_or_none` returns `None` for a blank value.
  `require_nonblank`, `validate_ticket_id` and `validate_contact_ref` raise
  `ValidationError`. `parse_json` accepts strict JSON (no NaN/Infinity).
- Shared help text: `STAFF_HELP`, `STAFF_ID_HELP`, `YES_HELP` and `text_file_help(what)`.
  `STDIN` is `"-"`. `stdin_is_tty()` is always called as `_util.stdin_is_tty()`, so tests can
  patch the module attribute. On Windows it also requires `GetConsoleMode` to succeed on the
  stdin handle, since `isatty()` is true for `NUL`. Unverified until Windows CI runs.
- Guarded reads, used only through `AppContext`: `check_readable_path(path, forbidden)`
  refuses a path that resolves to a guarded directory or below one, comparing by file
  identity and component-wise, never by string prefix. `open_guarded(path, *, forbidden,
  what)` adds the missing-file check, refuses a path that is not a regular file and refuses
  a hard link to a guarded `token.json`; the caller reads from the returned handle.
  `check_no_secrets(data, source, secrets)` refuses content holding a credential.
  `read_text_file` and `load_json_file` combine them and decode strict `utf-8-sig`.
  `_read_all(handle)` is the one read point and the patch point for read-error tests.
  `_expand(path)` expands `~` in a user-named path; one it cannot expand stays as written.
- `filter_rows(body, {field: text}, *, root_key="data", paged=False)` keeps the rows of a bare
  list or an envelope whose top-level string `field` contains every non-blank `text`, compared
  through `fold` (strip, NFKC, casefold, NFKC). Other keys such as `page_info` pass through.
  With no active filter it returns `body`. A body without a row list raises `HfoxError`
  (exit 5) unless `paged`, which reads rows as `client._unwrap_page` does so NDJSON pages match
  the flat list. `filter_help(field, *, paged=False)` is the flag's help text. `auth login`
  and `lookup_staff` share `fold` but match exactly.
- `attach(body, attachments, *, field="attachments", forbidden, secrets)` builds the
  `obj.call` kwargs; commands reach it through `obj.attach`. JSON bodies go as given, so
  `compact()` the base body before merging custom fields. Multipart drops `None`, sends every
  other value as text and caps files at `MAX_ATTACHMENT_BYTES` in total.
- `exit_on_failures(result, *, benign=None)`, called after rendering, exits 1 when any entry
  has `success: false`; group removals pass `benign=NOT_IN_GROUP`.
- `cli/cf.py`: `parse_cf_options(items, *, prefix, allowed)` and `parse_cf_json(raw, prefix,
  allowed)` prefix numeric keys and pass through keys carrying an `allowed` prefix (default:
  `prefix`); other keys raise. `parse_asset_cf` takes bare ids only.
- `cli/output.py`: `OutputFormat.parse` raises `ValidationError` for an unknown name.
  `sanitize_cell` drops every `Cc` and `Cf` character except newline, tab, U+200C and U+200D;
  table and CSV cells and headers pass through it. CSV also prefixes `'` to a string cell
  that starts with `=`, `+`, `-`, `@` or tab unless it is a plain signed decimal. CSV rows end
  in one CRLF; `_render_csv` writes encoded bytes to the stream's `buffer`, since a Windows
  text layer would turn each `\n` into `\r\n`. JSON and NDJSON keep the data and write C1,
  DEL, bidi, line-separator and tag characters as `\uXXXX`. YAML is untouched.

## Adding a command or resource

1. **A verb on an existing resource:** add a `@app.command("name")` function to the
   resource module. Signature is `def name(ctx: typer.Context, <id> = typer.Argument(...), <opts> = typer.Option(...))`.
   Route reads through `obj.paginate`/`obj.call` and writes through
   `obj.call(..., **obj.attach(body, attachment))`. Render with `obj.render`/`obj.render_list`.
2. **A new resource group:** create `cli/<resource>.py` exposing a Typer app named
   `app`, then mount it in `main.py` with `cli.add_typer(<resource>.app, name="<resource>")`.
   Sub-groups (like contact `groups`) are their own Typer instances mounted with
   `app.add_typer(sub, name="...")`.
3. Mirror `tickets.py`, the richest example.
4. Document the verb in `README.md` and `skills/hfox/SKILL.md`.

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
- **Staff pair:** a command that needs or names a staff identity declares both
  `staff: str = typer.Option(None, "--staff", help=STAFF_HELP)` and
  `staff_id: int = typer.Option(None, "--staff-id", min=0, help=STAFF_ID_HELP)` and calls
  `obj.require_staff_id(staff_id, staff)`. No other flag spelling names the actor. Of the
  flags that name another agent, `--assignee`, `--assign-to` and `--agents` take staff ids
  and `--alert` takes `s`, `c` or a staff id. None accepts an email or name.
- **Destructive commands** take `yes: bool = typer.Option(False, "--yes", "-y", help=YES_HELP)`
  and call `obj.confirm(message, yes=yes)`. The order is validate, resolve staff, confirm,
  call. Never call `typer.confirm` or `typer.prompt`; use `obj.confirm` and `obj.prompt`.
- **Bodies:** a text flag `--x` gets a sibling `--x-file` with `help=text_file_help(...)`, read
  through `obj.text_input(inline, file, "--x")`. A JSON `--file` flag is read with
  `obj.read_json(file, "--file")`, which accepts `-`. `--cf-json`, `--contact-cf-json` and
  `--new-contact-json` are inline only.
- Comma-join `tags`/`cc`/`bcc` inputs with `comma_join(split_csv(...))`;
  send list-of-ids body fields (subscribe `data`, group `contacts`, forward
  `ticket_attachments`, asset `contact_ids`/`contact_group_ids`) as JSON int arrays via
  `split_csv_ints`. Multipart sends a list as JSON text in one field. Unverified until tested
  against a live helpdesk.
- Raise `ValidationError`/`NotFoundError`/`AuthError`/`APIError` from `core.errors`;
  do not `print()` errors or call `sys.exit()`. Pass `hint=` when the fix is one command or
  flag away.
- Keep stdout pure data: status and warnings go to stderr (`obj.success`, `output.warn`,
  `output.info`). `output.warn` ignores `--quiet`; use it for a message the user must see,
  such as the `set-cf-choices --yes` summary.
- Local filters go only on listings whose endpoint has no server-side search; `tickets list`
  and `contacts list` use `-q`. The flag is long-only and named after the top-level string
  field it matches. A single-GET listing wraps `obj.call` in `filter_rows` before any
  formatting step; a paginated listing passes `filters=` to `obj.paginate`, never filtering
  one page.

## HappyFox API quirks

Sourced from `Docs/` unless marked observed. Honor them exactly.

- **Auth is HTTP Basic only:** API key = username, auth code = password. Redirects are not
  followed, since httpx would drop the auth on a cross-host redirect.
- **Base URL is host-templated:** `https://<subdomain>.happyfox.com/api/1.1/json` (US) or
  `…happyfox.net` (EU, `--region eu`). Region is case-insensitive and an unknown value exits
  3. A dotted `subdomain` (e.g. `support.acme.com`) is a full custom host. `HFOX_BASE_URL` is
  an `http(s)://host` root for proxied accounts, persisted by `auth login`. A trailing
  `/api/1.1/json` is stripped; another scheme, a missing host, a query, a fragment or
  embedded credentials exits 3. `normalize_base_url` raises the credentials error for any
  value containing `@`, before parsing, and never echoes a value containing `@`, `?` or `#`.
- **Rate limits are global:** 500 GET/min, 300 POST/min, then HTTP 429 for a **10-minute**
  cooldown. Without `Retry-After`, the default five 429 retries stop after about 33s; an
  undocumented numeric `Retry-After` is honored up to 600s per attempt. GETs retry any
  transport error; writes retry only when no connection was made, since a write may already
  have applied. Respect `--page-limit`/`--page-delay`.
- **Search sends `status=_all`:** the documented search URL is `tickets/?status=_all&q=...`,
  so `tickets list -q` injects it unless `--status` is given.
  In filters a comma means any-of, and the docs' `+` is an encoded space.
- **Multi-word `q` is AND (observed):** `q=degree works` returns tickets
  containing both words in any order; quoting does not force phrase matching.
- **Reference lists take no query params:** `categories/`, `priorities/`, `staff/`,
  `statuses/`, `ticket_custom_fields/`, `user_custom_fields/` and `contact_groups/` document
  none; all but `priorities/`, whose response is undocumented, return the whole set as a bare
  array. `staff/` ignores params (observed). `assets/`, `asset_types/` and
  `asset_custom_fields/` page but offer no search, and their list verbs send `size` and `page`.
- **Bulk endpoints cap at 100 entries** (tickets create-bulk, contacts create-bulk,
  group update_contacts). Enforce it with a `ValidationError` before the request.
- **Property-only ticket change:** `tickets update` posts to `ticket/<id>/staff_update/`, the
  staff reply endpoint, with `staff` and the properties but no message. It accepts `t-cf-`
  custom fields only and requires at least one property.
- **Explicit null assignee:** `--unassign` sends `"assignee": null`, set after `compact()`.
  It excludes `--assignee` and `--attachment`, since multipart cannot carry a null.
- **Custom-field choices replace the list:** `tickets set-cf-choices` sends PUT
  `ticket_custom_field/<id>/` with `{"choices": [...]}`. A choice keeps its `id`, a new one
  has `id` null, and an existing choice left out is deleted. hfox requires the `id` key on
  every choice and rejects duplicates; other keys such as `dependant_fields` pass through.
  How HappyFox treats an id it does not know is unverified until tested against a live
  helpdesk.
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
  `asset_custom_field/<id>/`, `ticket_custom_field/<id>/`).
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
  update, update-cf), `staff_id` (tags, forward, move, delete), `created_by`/`updated_by`
  (asset create and update, in the body) and `deleted_by` (asset delete, a query param).
  Resolve via `obj.require_staff_id(staff_id, staff)`. On subscribe/unsubscribe, `staff_id`
  is the agent added or removed, not the actor.
- **Attachments are multipart/form-data**, 25 MB total per request; use `obj.attach()`.
  `ticket-inline-attachment` takes one image in field `file` and returns a temporary `{url}`.
- All response timestamps are UTC.

## Configuration & secrets

The config directory is `$XDG_CONFIG_HOME/hfox` when `XDG_CONFIG_HOME` is an absolute path,
otherwise `~/.config/hfox`, on every OS. `--config-dir` and then `HFOX_CONFIG_DIR` override
it. One account, no profiles; `--profile`, `HFOX_PROFILE` and `--columns` are reserved names.

- `token.json` holds **secrets**: `api_key`, `auth_code`, `subdomain`, `region`, optional
  `base_url`. `config.toml` holds `subdomain`, `region`, `default_format`, `default_staff_id`.
  It also accepts a hand-written `base_url`, read after `HFOX_BASE_URL` and `token.json`;
  hfox never writes that key there.
- Both are written atomically through a temp file that is `0600` from creation, then
  `os.replace`. `token.json` ends at `0600` and the directory at `0700`. When the directory
  cannot be restricted, the `warn` callback of `save_credentials`/`save_settings` gets the
  message; core prints nothing.
- `save_settings(cfg_dir, settings, *, remove=())` merges into the existing file, keeps
  tables, arrays and dates it finds there, then deletes the `remove` keys. An incoming string
  with a control character raises `ValidationError`.
- `stored_account(cfg_dir)` returns subdomain, region, base URL and default staff id as the
  files hold them, ignoring the environment.
- A file that cannot be read or parsed raises `ConfigError` (type `config`, exit 5), defined
  in `core/config.py`. So does a `subdomain`, `region` or `base_url` in either file, or an
  `api_key` or `auth_code` in `token.json`, that is set and not a string; the message names
  the file and key, never the value, and nothing is coerced. The hint is "Fix or delete the
  file." for `config.toml` and "Delete the file and run `hfox auth login`." for `token.json`.
- `config_dir` raises `ConfigError` when the default needs a home directory that cannot be
  determined, and when the `~` of `--config-dir` or `HFOX_CONFIG_DIR` cannot be expanded.

Every field resolves as **env var > token.json/config.toml > default**.
Env overrides: `HFOX_SUBDOMAIN`, `HFOX_REGION`, `HFOX_API_KEY`, `HFOX_AUTH_CODE`,
`HFOX_BASE_URL`, `HFOX_STAFF_ID`, `HFOX_FORMAT`, `HFOX_CONFIG_DIR`. An empty value counts as
unset. `HFOX_TIMEOUT` and `HFOX_MAX_RETRIES` feed the global flags instead. A configured
staff id that is not a whole number exits 3 in commands that need one.

Never log or echo `api_key`/`auth_code`. `auth status` masks the key, and no `auth` payload
holds a secret. hfox-authored auth JSON names `config_dir`, never a file path, and
`default_staff_id` is an int or null.

**File guard.** `guarded_dirs(active)` lists the active directory, the env-resolved one and
the XDG or home default, so `--config-dir` cannot move the guard off the real directory. An
entry that needs an undeterminable home directory is left out.
`guarded_secrets(config)` lists the resolved credentials plus those in each guarded
`token.json`, dropping values shorter than `MIN_SECRET_LENGTH`. `AppContext` computes both
lazily, so a command that reads no input never opens a token file. Every user-named file and
stdin goes through them: a prompt-injected agent must not be able to send `token.json` out.

**`auth` behavior.** `login` takes each value from its flag, then `HFOX_SUBDOMAIN`,
`HFOX_REGION`, `HFOX_API_KEY` or `HFOX_AUTH_CODE`, then `obj.prompt`. A whitespace-only
subdomain or region counts as unset. On a non-terminal stdin a missing subdomain, key or code
raises `ValidationError` before any prompt, and a missing region is `us`. The message names
each missing flag and its `HFOX_` variable, and suggests the flag form only for
`--subdomain`, so secrets stay off argv. Only an `AuthError` from the probe is rewrapped;
network, rate-limit and other API errors keep their type.

`login --email` sets the default staff id only when exactly one `staff/` row has that email
and its id passes `parse_staff_id`. No match, several matches or an unusable id warns, sets
no default and removes a stored one. Without `--email` the stored default survives only when
`_target` is unchanged: the same subdomain, region and base URL override as the files held
before the save. The rendered `default_staff_id` is read back from `config.toml` after the
save, so it agrees with `auth status`.

When the base URL override comes from a file and `--subdomain` was passed on the command
line, `login` warns that the stored base URL overrides it, also under `--dry-run`. The
warning names `hfox auth logout` for a `token.json` value and the `config.toml` key otherwise.

`status` exits 2 without credentials after rendering, and `--check` adds `verified`, plus
`check_error` on failure. A 2xx `staff/` body that is not a list is a failed check with
`HfoxError`, exit 5. `logout --dry-run` renders `{dry_run, removed, config_dir}`.

## Exit codes and errors (stable public contract; do not renumber)

`0` success · `1` API error · `2` auth error · `3` validation · `4` not found · `5` other.
Defined in `core/errors.py` (`ExitCode`); each `HfoxError` subclass carries its code and a
`type` slug.

| Class | `type` | Exit |
|-------|--------|------|
| `HfoxError` | `other` | 5 |
| `InternalError` | `internal` | 5 |
| `ConfigError` (in `core/config.py`) | `config` | 5 |
| `CancelledError` | `cancelled` | 5 |
| `NetworkError` | `network` | 5 |
| `RequestTimeoutError` | `timeout` | 5 |
| `AuthError` | `auth` | 2 |
| `ValidationError` | `validation` | 3 |
| `UsageError` | `usage` | 3 |
| `NotFoundError` | `not_found` | 4 |
| `APIError` | `api` | 1 |
| `RateLimitError` | `rate_limited` | 1 |

`to_dict()` emits `error`, `type`, `exit_code`, then only when set `status_code`, `detail`,
`hint`, `retry_after`, `outcome_unknown`. `usage` is structural (unknown or misplaced flag,
unknown command, missing argument, flag-shaped root option value); a value that fails a
range, type or content check is
`validation`, whichever layer rejects it. A failed `--staff` lookup is exit 3, since exit 4
means HTTP 404. `detail` content is diagnostic and not contract, apart from `staff_ids` on an
ambiguous `--staff`.

`outcome_unknown` is true for a write that failed after the request may have left, and for a
write answered with HTTP 500 or above; both carry `OUTCOME_UNKNOWN_HINT` from
`core/client.py`. Failures to connect, pool timeouts, proxy errors and an unsupported URL
scheme (`_NOT_SENT`) never set it, and neither does a GET. Every other transport error on a
write sets it, `RemoteProtocolError` and `LocalProtocolError` included. `retry_after` is the
unclamped `Retry-After` of a 429 that outlasted the retries.

- **Usage errors** exit **3** with a JSON error on stdout and the usage hint on stderr;
  click's default 2 would collide with `ExitCode.AUTH`. A misplaced global flag and a
  flag-shaped root option value print the JSON only.
- **Other click exceptions** exit **5** with a JSON error; click's exit 1 would read as an
  API error. Any other exception becomes an `InternalError` naming its type, without a
  traceback.
- **Cancelled prompts** exit **5** with type `cancelled`. Ctrl-C outside a prompt exits 130
  with empty stdout.
- **Bare group invocations** (`hfox`, `hfox tickets`) print help and exit **0**.
- **Partial bulk failure:** bulk and group-membership commands print the response unchanged,
  warn the failed count on stderr and exit **1**; "not part of the group" on remove is benign.
- **`auth status`** prints its payload, not an error object, when it exits 2 or when
  `--check` fails.

The stability policy in `README.md` is the public statement of this contract. Adding a key,
a `type` slug, a flag or an env var is compatible; removing or renaming one is breaking.

## Testing

- `tests/` uses `pytest` + `typer.testing.CliRunner` and never hits the real API: the HTTP
  layer uses `httpx.MockTransport` with an injected `sleep`, so backoff is instant.
- The autouse `_isolated_config` fixture clears every `HFOX_*` variable and
  `XDG_CONFIG_HOME`, and points `HFOX_CONFIG_DIR`, `HOME` and `USERPROFILE` into `tmp_path`.
  No test may read or write the real home or config directory.
- `mock_api(handler)` patches `hfox.cli.context.HappyFoxClient`, the only place the CLI
  constructs a client, and returns the captured requests. It forwards `max_retries` and
  `on_retry` and ignores `timeout`. Auth tests use the same patch point.
- The `tty` fixture patches `hfox.cli._util.stdin_is_tty` to true, so prompts run and read
  CliRunner input. Without it every confirm and login prompt takes the non-terminal path.
- Subprocess tests build their environment with `conftest.subprocess_env(**extra)` and pass
  `stdin=subprocess.DEVNULL` unless they feed input, so the child sees the isolated home and
  no terminal.
- A new command needs a `--dry-run` test asserting method/URL/body, plus a unit test if it
  touches the client or encoding.
- A destructive command needs tests for `--yes`, for the non-terminal `ValidationError`, for
  a decline under `tty` that sends nothing, and for a dry run that never prompts.
- A local filter needs a dry-run test showing it is not sent and a zero-match test; on a
  paginated listing, also a test that it requires `--page-all`.
- CliRunner bypasses `main.app()`; test exit codes that depend on it through `app()`.
- Permission-mode, symlink and hard-link assertions carry
  `@pytest.mark.skipif(sys.platform == "win32", ...)`, since CI runs on Windows.
- PyYAML is a dev-only dependency for YAML round-trip tests.

## Scope

Shipped: tickets including custom-field choice management, contacts/groups,
assets/types/custom-fields, system reference data, auth. `system` stays read-only.
