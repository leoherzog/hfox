# Changelog

Releases of `hfox`, newest first. Each heading is a version on PyPI and a `v` tag on GitHub.

## 1.0.0rc4

Released 2026-11-16. This release renames commands and flags ahead of the 1.0 contract. No
name from 1.0.0rc3 is kept as an alias; an old one exits 3 with type `usage`.

### Renamed

| 1.0.0rc3 | 1.0.0rc4 |
|----------|----------|
| `hfox system ticket-custom-fields` | `hfox tickets custom-fields list` |
| `hfox system contact-custom-fields` | `hfox contacts custom-fields list` |
| `hfox tickets set-cf-choices` | `hfox tickets custom-fields set-choices` |
| `tickets subscribe --agents` | `tickets subscribe --staff-ids` |
| `tickets forward --ticket-attachments` | `tickets forward --attachment-ids` |
| `--enable-login/--disable-login` on `contacts create` and `update` | `--login/--no-login` |

### Changed

- `--page-delay` takes seconds, with a default of `0.1`. It took milliseconds, so
  `--page-delay 250` waits 250 seconds between pages.
- `tickets list --category` takes a comma-separated list such as `--category 34,5`. A
  repeated `--category` keeps only its last value.
- A blank `--tags`, `--cc` or `--bcc` exits 3, and so does a blank `tickets tags --add` or
  `--remove`.
- In `auth status`, `default_staff_id_error` is an error object with `error`, `type` and
  `exit_code`, the shape of `check_error`.
- `--subject` on `tickets create` and `forward`, `--name` on `contacts create` and
  `contacts groups create`, and `--name` and `--display-id` on `assets create` are sent
  without outer whitespace.
- `tickets custom-fields list` and `contacts custom-fields list` show choices as `text=id`
  in table output only. CSV and YAML keep the nested choices.

### Fixed

- A failed write to stdout exits 5. A closed pipe, as with `| head -1`, prints nothing more.
  Any other failure prints one line on stderr.
- `assets create` validates its input before it looks up `--staff`.

### Documentation

- `contacts list -q` accepts the `last_login_before` filter that HappyFox added.
- Exit 2 covers HTTP 403 as well as 401. The README and the agent skill say how to tell a
  missing role permission from rejected credentials.

### Dependencies

- Typer 0.28 and httpx2 2.16.2.

## 1.0.0rc1 to 1.0.0rc3

The first release candidates: tickets, contacts and contact groups, assets with their types
and custom fields, reference data and custom-field choice management.
