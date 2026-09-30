"""Configuration under ~/.hfox/: secrets in token.json (0600), settings in config.toml.

Every field resolves as env var > token.json/config.toml > default.
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys
import tomllib
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import AuthError, HfoxError, ValidationError

DEFAULT_REGION = "us"
DEFAULT_FORMAT = "json"
REGION_HOSTS = {"us": "happyfox.com", "eu": "happyfox.net"}
API_PREFIX = "/api/1.1/json"

TOKEN_FILENAME = "token.json"
CONFIG_FILENAME = "config.toml"

# A subdomain or custom host: dot-separated DNS labels (alnum + hyphen), no scheme,
# credentials, port, or path. Dotted values are supported for custom domains.
_HOST_RE = re.compile(r"^(?=.{1,253}$)(?!-)[A-Za-z0-9-]{1,63}(?:\.(?!-)[A-Za-z0-9-]{1,63})*$")


def _validate_host_part(subdomain: str) -> str:
    """Validate a bare subdomain or dotted custom host before templating it into a URL."""
    host = subdomain.strip()
    if not _HOST_RE.match(host):
        raise ValidationError(
            f"Invalid subdomain/host {subdomain!r}: expected a bare hostname like "
            "'acme' or 'support.acme.com' (no scheme, credentials, port, or path). "
            "Use HFOX_BASE_URL for self-hosted/proxied accounts.",
        )
    return host


def _toml_str(value: str) -> str:
    r"""Render a TOML basic string, escaping ``\`` and ``"``.

    Control characters raise ValidationError.
    """
    if any(ord(ch) < 0x20 for ch in value):
        raise ValidationError("Config string values may not contain control characters.")
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def normalize_base_url(url: str, source: str = "HFOX_BASE_URL") -> str:
    """Return a base URL override as an http(s) root without trailing slash or API prefix.

    Raises ValidationError naming `source` for another scheme, a missing host, a bad port,
    a query or a fragment.
    """
    root = url.strip().rstrip("/")
    if root.endswith(API_PREFIX):
        root = root[: -len(API_PREFIX)].rstrip("/")
    error = f"Invalid {source} {url!r}: expected http(s)://host."
    try:
        parts = urllib.parse.urlsplit(root)
        _ = parts.port  # raises ValueError for a bad port
    except ValueError as exc:
        raise ValidationError(error) from exc
    # An empty "?" or "#" still splits the appended API path off the wire URL.
    bad_scheme = parts.scheme.lower() not in ("http", "https")
    if bad_scheme or not parts.hostname or "?" in root or "#" in root:
        raise ValidationError(error)
    return root


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


def config_dir(override: str | Path | None = None) -> Path:
    """Resolve the config directory: CLI override > HFOX_CONFIG_DIR > ~/.hfox."""
    # An explicit override of "" is treated as unset (mirrors the env handling below).
    if override is not None and override != "":
        return Path(override).expanduser()
    # Empty-string env vars are treated as unset throughout config resolution.
    env = os.environ.get("HFOX_CONFIG_DIR")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".hfox"


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
        sub = _validate_host_part(self.subdomain)
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


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise HfoxError(f"Failed to read config file {path}: {exc}") from exc


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HfoxError(f"Failed to read token file {path}: {exc}") from exc


def load_config(config_dir_override: str | Path | None = None) -> Config:
    """Load and merge config.toml + token.json + environment variables."""
    cfg_dir = config_dir(config_dir_override)
    settings = _read_toml(cfg_dir / CONFIG_FILENAME)
    token = _read_json(cfg_dir / TOKEN_FILENAME)

    env = os.environ.get

    def pick(env_name: str, *sources: Any) -> Any:
        val = env(env_name)
        if val is not None and val != "":
            return val
        for source in sources:
            if source is not None and source != "":
                return source
        return None

    staff_id, staff_id_error = None, None
    try:
        if env("HFOX_STAFF_ID"):
            staff_id = parse_staff_id(env("HFOX_STAFF_ID"), "HFOX_STAFF_ID")
        else:
            staff_id = parse_staff_id(
                settings.get("default_staff_id"),
                f"default_staff_id in {cfg_dir / CONFIG_FILENAME}",
            )
    except ValidationError as exc:
        staff_id_error = exc
    region = pick("HFOX_REGION", token.get("region"), settings.get("region"), DEFAULT_REGION)
    base_url_source = "HFOX_BASE_URL"
    if not env("HFOX_BASE_URL"):
        name = TOKEN_FILENAME if token.get("base_url") not in (None, "") else CONFIG_FILENAME
        base_url_source = f"base_url in {cfg_dir / name}"

    return Config(
        subdomain=pick("HFOX_SUBDOMAIN", token.get("subdomain"), settings.get("subdomain")),
        region=str(region).strip().lower(),
        api_key=pick("HFOX_API_KEY", token.get("api_key")),
        auth_code=pick("HFOX_AUTH_CODE", token.get("auth_code")),
        default_format=pick("HFOX_FORMAT", settings.get("default_format"), DEFAULT_FORMAT),
        default_staff_id=staff_id,
        staff_id_error=staff_id_error,
        base_url_override=pick("HFOX_BASE_URL", token.get("base_url"), settings.get("base_url")),
        base_url_source=base_url_source,
        dir=cfg_dir,
    )


def _atomic_write(path: Path, text: str, *, mode: int) -> None:
    """Write a file atomically (temp sibling + os.replace) with the given mode."""
    # mode is masked by the umask at creation, so chmod afterwards to be sure the
    # dir is owner-only (0700) before any secrets land in it.
    path.parent.mkdir(parents=True, exist_ok=True, mode=stat.S_IRWXU)
    try:
        path.parent.chmod(stat.S_IRWXU)  # 0700
    except OSError as exc:
        # Don't silently leave the secrets dir world/group-readable: warn loudly.
        print(
            f"hfox: warning: could not restrict permissions on {path.parent} "
            f"to 0700 ({exc}); credentials may be readable by other users.",
            file=sys.stderr,
        )
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.chmod(mode)
    os.replace(tmp, path)


def save_credentials(
    cfg_dir: Path,
    *,
    subdomain: str,
    region: str,
    api_key: str,
    auth_code: str,
    base_url: str | None = None,
) -> Path:
    """Persist secrets to ~/.hfox/token.json with 0600 permissions."""
    payload: dict[str, Any] = {
        "subdomain": subdomain,
        "region": region,
        "api_key": api_key,
        "auth_code": auth_code,
    }
    if base_url:
        payload["base_url"] = base_url
    path = cfg_dir / TOKEN_FILENAME
    _atomic_write(path, json.dumps(payload, indent=2) + "\n", mode=0o600)
    return path


def save_settings(cfg_dir: Path, settings: dict[str, Any]) -> Path:
    """Persist non-secret settings to ~/.hfox/config.toml (merging with existing)."""
    existing = _read_toml(cfg_dir / CONFIG_FILENAME)
    incoming = {k: v for k, v in settings.items() if v is not None}
    staff_id = parse_staff_id(incoming.pop("default_staff_id", None), "default_staff_id")
    if staff_id is not None:
        incoming["default_staff_id"] = staff_id
    existing.update(incoming)
    lines = []
    for key, value in existing.items():
        if isinstance(value, str):
            lines.append(f"{key} = {_toml_str(value)}")
        elif isinstance(value, bool):
            lines.append(f"{key} = {str(value).lower()}")
        else:
            lines.append(f"{key} = {value}")
    path = cfg_dir / CONFIG_FILENAME
    _atomic_write(path, "\n".join(lines) + "\n", mode=0o644)
    return path


def clear_credentials(cfg_dir: Path) -> bool:
    """Remove token.json. Returns True if a file was deleted."""
    path = cfg_dir / TOKEN_FILENAME
    if path.exists():
        path.unlink()
        return True
    return False
