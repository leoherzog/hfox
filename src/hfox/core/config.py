"""Configuration in the config directory: secrets in token.json (0600), settings in
config.toml.

Every field resolves as env var > token.json/config.toml > default. An empty or
whitespace-only string counts as unset in each source, and a set string loses its outer
whitespace.
"""

from __future__ import annotations

import datetime
import ipaddress
import json
import math
import os
import re
import stat
import string
import tempfile
import tomllib
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from .errors import AuthError, ExitCode, HfoxError, ValidationError

DEFAULT_REGION = "us"
DEFAULT_FORMAT = "json"
REGION_HOSTS = {"us": "happyfox.com", "eu": "happyfox.net"}
API_PREFIX = "/api/1.1/json"

TOKEN_FILENAME = "token.json"
CONFIG_FILENAME = "config.toml"

# Shortest credential worth scanning outgoing content for.
MIN_SECRET_LENGTH = 8


class ConfigError(HfoxError):
    """A config or token file that cannot be read, parsed, written or removed, or a config
    directory that cannot be resolved."""

    exit_code = ExitCode.OTHER
    type = "config"


_ASCII_LOWER = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)


def _lower(text: str) -> str:
    """Lowercase the ASCII letters only; str.lower() would turn U+212A into a valid "k"."""
    return text.translate(_ASCII_LOWER)


# A subdomain or dotted custom host: DNS labels of letters, digits and hyphens, with no
# scheme, credentials, port or path.
_HOST_RE = re.compile(r"^(?=.{1,253}$)(?!-)[A-Za-z0-9-]{1,63}(?:\.(?!-)[A-Za-z0-9-]{1,63})*$")


def validate_host(subdomain: str) -> str:
    """Return the subdomain or dotted custom host, stripped and lowercased, or raise
    ValidationError."""
    host = _lower(subdomain.strip())
    if not _HOST_RE.match(host):
        # A value with "@" may hold a password, so the message leaves it out.
        shown = "" if "@" in subdomain else f" {subdomain!r}"
        raise ValidationError(
            f"Invalid subdomain/host{shown}: expected a bare hostname like "
            "'acme' or 'support.acme.com' (no scheme, credentials, port, or path). "
            "Use HFOX_BASE_URL for self-hosted/proxied accounts.",
        )
    return host


_BARE_KEY_RE = re.compile(r"[A-Za-z0-9_-]+")
_TOML_ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
}


def _is_control(ch: str) -> bool:
    return ord(ch) < 0x20 or ord(ch) == 0x7F


def _toml_str(value: str) -> str:
    """Render a TOML basic string, escaping quotes, backslashes and control characters."""
    out = []
    for ch in value:
        if ch in _TOML_ESCAPES:
            out.append(_TOML_ESCAPES[ch])
        elif _is_control(ch):
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    return f'"{"".join(out)}"'


def _toml_key(key: str) -> str:
    return key if _BARE_KEY_RE.fullmatch(key) else _toml_str(key)


def _toml_value(value: Any) -> str:
    """Render one TOML value; a dict becomes an inline table."""
    if isinstance(value, str):
        return _toml_str(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value):
            return "nan"
        if math.isinf(value):
            return "inf" if value > 0 else "-inf"
        return repr(value)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        pairs = (f"{_toml_key(str(k))} = {_toml_value(v)}" for k, v in value.items())
        return "{" + ", ".join(pairs) + "}"
    raise ValidationError(f"Cannot write a {type(value).__name__} value to {CONFIG_FILENAME}.")


def _toml_dumps(data: dict[str, Any]) -> str:
    """Serialize a dict as TOML: a table's non-table values, then its sub-tables as sections."""
    lines: list[str] = []

    def emit(table: dict[str, Any], prefix: str) -> None:
        for key, value in table.items():
            if not isinstance(value, dict):
                lines.append(f"{_toml_key(str(key))} = {_toml_value(value)}")
        for key, value in table.items():
            if isinstance(value, dict):
                name = f"{prefix}.{_toml_key(str(key))}" if prefix else _toml_key(str(key))
                if lines:
                    lines.append("")
                lines.append(f"[{name}]")
                emit(value, name)

    emit(data, "")
    return "\n".join(lines) + "\n"


# The scheme, host and port of a base URL, up to the path.
_ORIGIN_RE = re.compile(r"https?://[^/]*", re.IGNORECASE)

# The host and optional port of a base URL: a bracketed address, or dot-separated labels with
# an optional root dot. A label holds ASCII letters, digits, hyphens and underscores, and
# non-ASCII characters, which httpx checks as IDNA.
_LABEL = r"[A-Za-z0-9_\-\x80-\U0010FFFF]+"
_AUTHORITY_RE = re.compile(rf"(?:\[[^\[\]]+\]|{_LABEL}(?:\.{_LABEL})*\.?)(?::[0-9]*)?")


def normalize_base_url(url: str, source: str = "HFOX_BASE_URL") -> str:
    """Return a base URL override as an http(s) root without trailing slash or API prefix.

    The scheme and host are lowercased; any other path is kept in its own case. Raises
    ValidationError naming `source` for another scheme, a missing host, a bad port, a query,
    a fragment, credentials, a control character, or a host that is not a host name or an IP
    address, which includes one holding whitespace. Whitespace in the path is kept and sent
    percent-encoded. A value holding `@`, `?` or `#` is never echoed.
    """
    # A valid root has no "@", so any form of userinfo is caught before parsing.
    if "@" in url:
        raise ValidationError(
            f"Invalid {source}: credentials in the URL are not supported; expected http(s)://host."
        )
    root = url.strip().rstrip("/")
    if root.endswith(API_PREFIX):
        root = root[: -len(API_PREFIX)].rstrip("/")
    if "?" in url or "#" in url:
        error = f"Invalid {source}: a query or fragment is not supported; expected http(s)://host."
    else:
        error = f"Invalid {source} {url!r}: expected http(s)://host."
    # urlsplit drops tab, CR, LF and leading control characters, which the request keeps.
    if any(_is_control(ch) for ch in root):
        raise ValidationError(error)
    try:
        parts = urllib.parse.urlsplit(root)
    except ValueError as exc:
        raise ValidationError(error) from exc
    try:
        _ = parts.port  # raises ValueError for a bad port
    except ValueError as exc:
        raise ValidationError(error) from exc
    # An empty "?" or "#" still splits the appended API path off the wire URL.
    bad_scheme = parts.scheme.lower() not in ("http", "https")
    if bad_scheme or not parts.hostname or "?" in root or "#" in root:
        raise ValidationError(error)
    # httpx percent-encodes whitespace, which in the host fails only when the request is sent.
    if any(ch.isspace() for ch in parts.netloc) or not _AUTHORITY_RE.fullmatch(parts.netloc):
        raise ValidationError(error)
    # httpx builds the request, so a host it refuses is refused here. Reading `host` decodes
    # an `xn--` label, which fails with a UnicodeError when the label is not Punycode.
    try:
        _ = httpx.URL(root).host
    except (httpx.InvalidURL, UnicodeError) as exc:
        raise ValidationError(error) from exc
    origin = _ORIGIN_RE.match(root)
    return root if origin is None else _lower(origin[0]) + root[origin.end() :]


def parse_staff_id(value: Any, source: str) -> int | None:
    """Return a non-negative staff id, or None when unset; raise ValidationError naming `source`."""
    if isinstance(value, str):
        value = value.strip()
    if value is None or value == "":
        return None
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    if isinstance(value, str) and value.isascii() and value.isdigit():
        return int(value)
    raise ValidationError(f"{source} must be a non-negative integer, got {value!r}.")


def cleartext_host(base_url: str) -> str | None:
    """Return the lowercased host when `base_url` is http to a non-loopback host, else None."""
    try:
        parts = urllib.parse.urlsplit(base_url.strip())
        host = parts.hostname
    except (AttributeError, ValueError):
        return None
    if parts.scheme.lower() != "http" or not host:
        return None
    host = host.lower()
    if host == "localhost" or host.endswith(".localhost"):
        return None
    try:
        if ipaddress.ip_address(host).is_loopback:
            return None
    except ValueError:
        pass
    return host


def _default_config_dir() -> Path:
    """Return $XDG_CONFIG_HOME/hfox when that variable is an absolute path, else ~/.config/hfox."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg and Path(xdg).is_absolute():
        return Path(xdg) / "hfox"
    return Path.home() / ".config" / "hfox"


def _expand_dir(value: str | Path) -> Path:
    try:
        return Path(value).expanduser()
    except RuntimeError as exc:
        raise ConfigError(
            f"Cannot expand the config directory {str(value)!r}: its home directory is "
            "unknown; use an absolute path."
        ) from exc


def config_dir(override: str | Path | None = None) -> Path:
    """Resolve the config directory: override > HFOX_CONFIG_DIR > XDG default.

    An empty or whitespace-only override or env value counts as unset; a set value is used
    as written. Raises ConfigError when the result depends on a home directory that cannot
    be determined.
    """
    if not _unset(override):
        return _expand_dir(override)
    env = os.environ.get("HFOX_CONFIG_DIR")
    if not _unset(env):
        return _expand_dir(env)
    try:
        return _default_config_dir()
    except RuntimeError as exc:
        raise ConfigError(
            "Cannot determine the home directory; set HFOX_CONFIG_DIR to an absolute path."
        ) from exc


def guarded_dirs(active: Path) -> tuple[Path, ...]:
    """Return the directories user-named files must stay out of, deduplicated.

    Order: `active`, the env-resolved directory, the XDG or home default, so an override
    cannot move the guard off the real one. An entry that needs an unknown home is left out.
    """
    dirs: list[Path] = []
    for resolve in (lambda: Path(active), lambda: config_dir(None), _default_config_dir):
        try:
            candidate = resolve()
        except (ConfigError, RuntimeError):
            continue
        if candidate not in dirs:
            dirs.append(candidate)
    return tuple(dirs)


def guarded_secrets(config: Config) -> tuple[str, ...]:
    """Return the credentials outgoing content must not contain, deduplicated.

    Covers the resolved credentials and those in token.json of every guarded directory,
    each without its outer whitespace, as `load_config` resolves it. Unreadable or malformed
    files are skipped; values then under MIN_SECRET_LENGTH are dropped.
    """
    candidates: list[Any] = [config.api_key, config.auth_code]
    for directory in guarded_dirs(config.dir):
        try:
            token = json.loads((directory / TOKEN_FILENAME).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(token, dict):
            candidates += [token.get("api_key"), token.get("auth_code")]
    secrets: list[str] = []
    for value in candidates:
        if not isinstance(value, str):
            continue
        value = value.strip()
        if len(value) >= MIN_SECRET_LENGTH and value not in secrets:
            secrets.append(value)
    return tuple(secrets)


@dataclass
class Config:
    """Resolved configuration for one HappyFox account."""

    subdomain: str | None = None
    region: str = DEFAULT_REGION
    api_key: str | None = None
    auth_code: str | None = None
    default_format: str = DEFAULT_FORMAT
    default_staff_id: int | None = None
    # A malformed configured staff id, raised only by commands that need one.
    staff_id_error: ValidationError | None = field(default=None, repr=False)
    base_url_override: str | None = None
    base_url_source: str = "HFOX_BASE_URL"
    dir: Path = field(default_factory=lambda: config_dir())

    @property
    def base_url(self) -> str:
        """Full API base URL including the /api/1.1/json prefix (no trailing slash)."""
        if self.base_url_override:
            return normalize_base_url(self.base_url_override, self.base_url_source) + API_PREFIX
        if not self.subdomain:
            raise AuthError("No HappyFox account configured. Run `hfox auth login` first.")
        sub = validate_host(self.subdomain)
        # A dotted subdomain is a full custom host and ignores the region.
        if "." in sub:
            return f"https://{sub}{API_PREFIX}"
        host = REGION_HOSTS.get(self.region.strip().lower())
        if host is None:
            raise ValidationError(
                f"Unknown region {self.region!r}: expected one of {', '.join(REGION_HOSTS)}."
            )
        return f"https://{sub}.{host}{API_PREFIX}"

    @property
    def is_authenticated(self) -> bool:
        return bool(self.api_key and self.auth_code and (self.subdomain or self.base_url_override))

    def require_auth(self) -> None:
        if not self.is_authenticated:
            raise AuthError(
                "Not authenticated. Run `hfox auth login` to configure credentials.",
            )


_CONFIG_HINT = "Fix or delete the file."
_TOKEN_HINT = "Delete the file and run `hfox auth login`."
_WRITE_HINT = "Check the permissions on the config directory, or choose another with --config-dir."

# Absence is decided by the open or unlink itself, since Path.exists() answers for an
# unsearchable directory differently across Python versions.
_ABSENT = (FileNotFoundError, NotADirectoryError)


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except _ABSENT:
        return {}
    except (OSError, ValueError) as exc:
        raise ConfigError(f"Cannot read config file {path}: {exc}", hint=_CONFIG_HINT) from exc


def _read_json(path: Path) -> dict[str, Any]:
    try:
        token = json.loads(path.read_text(encoding="utf-8"))
    except _ABSENT:
        return {}
    except (OSError, ValueError) as exc:
        raise ConfigError(f"Cannot read token file {path}: {exc}", hint=_TOKEN_HINT) from exc
    if not isinstance(token, dict):
        raise ConfigError(
            f"Cannot read token file {path}: expected a JSON object", hint=_TOKEN_HINT
        )
    return token


def _unset(value: Any) -> bool:
    """Return True for None and for an empty or whitespace-only string."""
    return value is None or (isinstance(value, str) and not value.strip())


# Keys whose value is case-insensitive.
_CASELESS = ("subdomain", "region")


def _normalize(key: str, value: Any) -> Any:
    """Return a string without its outer whitespace, lowercased for a caseless key.

    Any other value is returned unchanged.
    """
    if not isinstance(value, str):
        return value
    value = value.strip()
    return _lower(value) if key in _CASELESS else value


def _require_strings(data: dict[str, Any], keys: tuple[str, ...], message: str, hint: str) -> None:
    """Raise ConfigError naming the first of `keys` whose value is set and not a string.

    The value is never echoed, since it may be a credential.
    """
    for key in keys:
        if data.get(key) is not None and not isinstance(data[key], str):
            raise ConfigError(f"{message}: {key} must be a string", hint=hint)


def load_config(config_dir_override: str | Path | None = None) -> Config:
    """Load and merge config.toml + token.json + environment variables.

    An empty or whitespace-only string is unset in every source. A set string loses its
    outer whitespace, and the subdomain and region are lowercased.
    """
    cfg_dir = config_dir(config_dir_override)
    settings = _read_toml(cfg_dir / CONFIG_FILENAME)
    token = _read_json(cfg_dir / TOKEN_FILENAME)
    _require_strings(
        settings,
        ("subdomain", "region", "base_url"),
        f"Cannot read config file {cfg_dir / CONFIG_FILENAME}",
        _CONFIG_HINT,
    )
    _require_strings(
        token,
        ("subdomain", "region", "base_url", "api_key", "auth_code"),
        f"Cannot read token file {cfg_dir / TOKEN_FILENAME}",
        _TOKEN_HINT,
    )

    env = os.environ.get

    def pick(key: str, env_name: str, *sources: Any) -> Any:
        for value in (env(env_name), *sources):
            if not _unset(value):
                return _normalize(key, value)
        return None

    staff_id, staff_id_error = None, None
    try:
        if not _unset(env("HFOX_STAFF_ID")):
            staff_id = parse_staff_id(env("HFOX_STAFF_ID"), "HFOX_STAFF_ID")
        else:
            staff_id = parse_staff_id(
                settings.get("default_staff_id"),
                f"default_staff_id in {cfg_dir / CONFIG_FILENAME}",
            )
    except ValidationError as exc:
        staff_id_error = exc
    region = pick(
        "region", "HFOX_REGION", token.get("region"), settings.get("region"), DEFAULT_REGION
    )
    base_url_source = "HFOX_BASE_URL"
    if _unset(env("HFOX_BASE_URL")):
        name = CONFIG_FILENAME if _unset(token.get("base_url")) else TOKEN_FILENAME
        base_url_source = f"base_url in {cfg_dir / name}"

    return Config(
        subdomain=pick(
            "subdomain", "HFOX_SUBDOMAIN", token.get("subdomain"), settings.get("subdomain")
        ),
        region=region,
        api_key=pick("api_key", "HFOX_API_KEY", token.get("api_key")),
        auth_code=pick("auth_code", "HFOX_AUTH_CODE", token.get("auth_code")),
        default_format=pick(
            "default_format", "HFOX_FORMAT", settings.get("default_format"), DEFAULT_FORMAT
        ),
        default_staff_id=staff_id,
        staff_id_error=staff_id_error,
        base_url_override=pick(
            "base_url", "HFOX_BASE_URL", token.get("base_url"), settings.get("base_url")
        ),
        base_url_source=base_url_source,
        dir=cfg_dir,
    )


def stored_account(cfg_dir: Path) -> dict[str, Any]:
    """Return subdomain, region, base_url and default_staff_id as the files hold them.

    The environment is ignored. token.json wins over config.toml for the first three, which
    are normalized as `load_config` does; a value that is unset to it or not a string is
    None. default_staff_id is the raw config.toml value.
    """
    settings = _read_toml(cfg_dir / CONFIG_FILENAME)
    token = _read_json(cfg_dir / TOKEN_FILENAME)

    def pick(key: str) -> str | None:
        for source in (token, settings):
            value = source.get(key)
            if isinstance(value, str) and not _unset(value):
                return _normalize(key, value)
        return None

    return {
        "subdomain": pick("subdomain"),
        "region": pick("region"),
        "base_url": pick("base_url"),
        "default_staff_id": settings.get("default_staff_id"),
    }


def _atomic_write(
    path: Path, text: str, *, mode: int, warn: Callable[[str], None] | None = None
) -> None:
    """Write `text` to `path` atomically; the temp sibling is 0600 from creation.

    `warn` receives a message when the directory cannot be restricted to 0700.
    """
    # mkdir's mode is masked by the umask and skipped for an existing directory.
    path.parent.mkdir(parents=True, exist_ok=True, mode=stat.S_IRWXU)
    try:
        path.parent.chmod(stat.S_IRWXU)
    except OSError as exc:
        if warn is not None:
            warn(
                f"could not restrict permissions on {path.parent} to 0700 ({exc}); "
                "credentials may be readable by other users."
            )
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def save_credentials(
    cfg_dir: Path,
    *,
    subdomain: str,
    region: str,
    api_key: str,
    auth_code: str,
    base_url: str | None = None,
    warn: Callable[[str], None] | None = None,
) -> Path:
    """Persist secrets to token.json in the config directory at mode 0600.

    Raises ConfigError when the file cannot be written.
    """
    payload: dict[str, Any] = {
        "subdomain": subdomain,
        "region": region,
        "api_key": api_key,
        "auth_code": auth_code,
    }
    if base_url:
        payload["base_url"] = base_url
    path = cfg_dir / TOKEN_FILENAME
    try:
        _atomic_write(path, json.dumps(payload, indent=2) + "\n", mode=0o600, warn=warn)
    except OSError as exc:
        raise ConfigError(f"Cannot write token file {path}: {exc}", hint=_WRITE_HINT) from exc
    return path


def save_settings(
    cfg_dir: Path,
    settings: dict[str, Any],
    *,
    remove: tuple[str, ...] = (),
    warn: Callable[[str], None] | None = None,
) -> Path:
    """Merge non-secret settings into config.toml, then delete the `remove` keys.

    An incoming string with a control character raises ValidationError; values already in
    the file are kept and escaped. Raises ConfigError when the file cannot be written.
    """
    existing = _read_toml(cfg_dir / CONFIG_FILENAME)
    incoming = {k: v for k, v in settings.items() if v is not None}
    staff_id = parse_staff_id(incoming.pop("default_staff_id", None), "default_staff_id")
    if staff_id is not None:
        incoming["default_staff_id"] = staff_id
    for value in incoming.values():
        if isinstance(value, str) and any(_is_control(ch) for ch in value):
            raise ValidationError("Config string values may not contain control characters.")
    existing.update(incoming)
    for key in remove:
        existing.pop(key, None)
    path = cfg_dir / CONFIG_FILENAME
    try:
        _atomic_write(path, _toml_dumps(existing), mode=0o644, warn=warn)
    except OSError as exc:
        raise ConfigError(f"Cannot write config file {path}: {exc}", hint=_WRITE_HINT) from exc
    return path


def clear_credentials(cfg_dir: Path) -> bool:
    """Remove token.json. Returns True if a file was deleted.

    Raises ConfigError when the file is there and cannot be removed.
    """
    path = cfg_dir / TOKEN_FILENAME
    try:
        path.unlink()
    except _ABSENT:
        return False
    except OSError as exc:
        raise ConfigError(f"Cannot remove token file {path}: {exc}", hint=_WRITE_HINT) from exc
    return True
