"""``garmin-health-mcp setup``: store the Personal API key in the system keyring.

Reads the key without echo, verifies it with one call (profile settings) and only then saves it.
Nothing is written to disk except Garmin's bearer-token cache (0600) in the state directory.
"""

from __future__ import annotations

import getpass
import sys

from garmin_personal_api import GarminPersonalApiError

from .client import describe_error, open_client, status_code
from .credentials import delete_api_key, save_api_key, state_dir


def run_setup(*, delete: bool = False) -> int:
    if delete:
        delete_api_key()
        cache = state_dir() / "bearer-token.json"
        cache.unlink(missing_ok=True)
        print("Removed the API key from the keyring and the cached bearer token.")
        return 0

    print("Garmin Connect Personal API key")
    print("Create one at https://connect.garmin.com/app/settings/personalApiToken. It is stored")
    print("only in the system keyring and is never printed or logged.\n")
    api_key = getpass.getpass("API key (input hidden): ").strip()
    if not api_key:
        print("No key entered.", file=sys.stderr)
        return 1

    print("Exchanging the key for a token and reading profile settings ...")
    try:
        with open_client(api_key, force_refresh=True) as client:
            client.get_user_profile_settings()
    except GarminPersonalApiError as exc:
        print(f"Verification failed: {describe_error(exc)}", file=sys.stderr)
        if status_code(exc) in (401, 403):
            print("The key was rejected. Check it or create a new one.", file=sys.stderr)
        return 1

    save_api_key(api_key)
    print("Key verified and saved to the system keyring (service 'garmin-health-mcp').")
    return 0
