from __future__ import annotations

import logging
from pathlib import Path

import pytest

from garmin_health_mcp.credentials import (
    KEY_ENV_VAR,
    KEY_FILE_ENV_VAR,
    STATE_DIR_ENV_VAR,
    CredentialsMissingError,
    load_api_key,
    state_dir,
)


@pytest.fixture
def keyring_value(monkeypatch: pytest.MonkeyPatch) -> dict[str, str | None]:
    box: dict[str, str | None] = {"value": None}
    monkeypatch.setattr("keyring.get_password", lambda _s, _n: box["value"])
    return box


def test_key_file_wins(tmp_path: Path, keyring_value: dict[str, str | None]) -> None:
    key_file = tmp_path / "key"
    key_file.write_text("from-file\n")
    key_file.chmod(0o600)
    keyring_value["value"] = "from-keyring"
    env = {KEY_FILE_ENV_VAR: str(key_file), KEY_ENV_VAR: "from-env"}
    assert load_api_key(env) == "from-file"


def test_env_before_keyring(keyring_value: dict[str, str | None]) -> None:
    keyring_value["value"] = "from-keyring"
    assert load_api_key({KEY_ENV_VAR: " from-env "}) == "from-env"


def test_keyring_fallback(keyring_value: dict[str, str | None]) -> None:
    keyring_value["value"] = "from-keyring"
    assert load_api_key({}) == "from-keyring"


def test_missing_everywhere(keyring_value: dict[str, str | None]) -> None:
    with pytest.raises(CredentialsMissingError, match="setup"):
        load_api_key({})


def test_unreadable_key_file_does_not_fall_back(
    tmp_path: Path, keyring_value: dict[str, str | None]
) -> None:
    keyring_value["value"] = "from-keyring"
    with pytest.raises(CredentialsMissingError):
        load_api_key({KEY_FILE_ENV_VAR: str(tmp_path / "missing")})


def test_world_readable_key_file_warns_without_leaking(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, keyring_value: dict[str, str | None]
) -> None:
    key_file = tmp_path / "key"
    key_file.write_text("super-secret-key")
    key_file.chmod(0o644)
    with caplog.at_level(logging.WARNING):
        assert load_api_key({KEY_FILE_ENV_VAR: str(key_file)}) == "super-secret-key"
    assert "chmod 600" in caplog.text
    assert "super-secret-key" not in caplog.text


def test_state_dir_is_private(tmp_path: Path) -> None:
    path = state_dir({STATE_DIR_ENV_VAR: str(tmp_path / "state")})
    assert path.is_dir()
    assert path.stat().st_mode & 0o777 == 0o700
