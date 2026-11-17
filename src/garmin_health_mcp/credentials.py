"""Where the Garmin Personal API key comes from, and where state lives.

The key is looked up in this order:

1. ``GARMIN_PERSONAL_API_KEY_FILE``: a file holding the key (Docker secrets, e.g.
   ``/run/secrets/garmin_api_key``)
2. ``GARMIN_PERSONAL_API_KEY``: the key itself
3. the system keyring, service ``garmin-health-mcp`` (written by ``garmin-health-mcp setup``)

The key is never logged, printed or written to ``.env``.
"""

from __future__ import annotations

import contextlib
import logging
import os
import stat
from collections.abc import Mapping
from pathlib import Path

import keyring
from keyring.errors import PasswordDeleteError

KEYRING_SERVICE = "garmin-health-mcp"
_KEYRING_ITEM = "personal_api_key"

KEY_FILE_ENV_VAR = "GARMIN_PERSONAL_API_KEY_FILE"
KEY_ENV_VAR = "GARMIN_PERSONAL_API_KEY"
STATE_DIR_ENV_VAR = "GARMIN_HEALTH_MCP_STATE_DIR"
DEFAULT_STATE_DIR = Path("~/.config/garmin-health-mcp")

SETUP_HINT = (
    "Run `uvx garmin-health-mcp setup` (or `uv run garmin-health-mcp setup` in a checkout), "
    "or create a new key at Garmin Connect and run setup again."
)

_LOGGER = logging.getLogger(__name__)


class CredentialsMissingError(RuntimeError):
    """Raised when no API key can be found."""


def load_api_key(env: Mapping[str, str] | None = None) -> str:
    environment = os.environ if env is None else env

    key_file = environment.get(KEY_FILE_ENV_VAR, "").strip()
    if key_file:
        path = Path(key_file).expanduser()
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise CredentialsMissingError(
                f"{KEY_FILE_ENV_VAR} points to {path}, which cannot be read."
            ) from exc
        warn_if_readable_by_others(path)
        if not value:
            raise CredentialsMissingError(f"The key file {path} is empty.")
        return value

    value = environment.get(KEY_ENV_VAR, "").strip()
    if value:
        return value

    stored = keyring.get_password(KEYRING_SERVICE, _KEYRING_ITEM)
    if stored and stored.strip():
        return stored.strip()
    raise CredentialsMissingError(f"No Garmin Personal API key found. {SETUP_HINT}")


def save_api_key(api_key: str) -> None:
    keyring.set_password(KEYRING_SERVICE, _KEYRING_ITEM, api_key.strip())


def delete_api_key() -> None:
    with contextlib.suppress(PasswordDeleteError):
        keyring.delete_password(KEYRING_SERVICE, _KEYRING_ITEM)


def warn_if_readable_by_others(path: Path) -> bool:
    """Log a warning (without the contents) if group/other can read ``path``."""
    if os.name != "posix":
        return False
    if path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        _LOGGER.warning("%s is readable by other users; restrict it with chmod 600", path)
        return True
    return False


def state_dir(env: Mapping[str, str] | None = None) -> Path:
    """The private state directory (token cache, later FIT cache and auth.db), mode 0700."""
    environment = os.environ if env is None else env
    raw = environment.get(STATE_DIR_ENV_VAR, "").strip()
    path = Path(raw).expanduser() if raw else DEFAULT_STATE_DIR.expanduser()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":
        path.chmod(0o700)
    return path


def token_cache_file(env: Mapping[str, str] | None = None) -> Path:
    return state_dir(env) / "bearer-token.json"
