"""Remote mode wiring: garmin-health behind mcp-owner-auth (the auth itself is tested there)."""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pyotp
import pytest
from mcp_owner_auth import build_app
from mcp_owner_auth.store import AuthStore
from starlette.testclient import TestClient

from garmin_health_mcp import server as srv

PUBLIC = "https://garmin.test"
CALLBACK = "https://claude.ai/api/mcp/auth_callback"
TOTP = pyotp.random_base32()
MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("GARMIN_HEALTH_MCP_PUBLIC_URL", PUBLIC)
    monkeypatch.setenv("GARMIN_HEALTH_MCP_STATE_DIR", str(tmp_path))
    AuthStore(tmp_path / "auth.db").save_credentials(
        username="owner",
        password="a long enough password",
        totp_secret=TOTP,
        client_id="client",
        client_secret="secret",
    )
    with TestClient(
        build_app(srv.server, srv.auth_config()), base_url=PUBLIC, follow_redirects=False
    ) as test_client:
        yield test_client


def test_config_from_garmin_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GARMIN_HEALTH_MCP_PUBLIC_URL", "https://g.example")
    monkeypatch.setenv("GARMIN_HEALTH_MCP_CLIENT_IP_HEADER", "CF-Connecting-IP")
    monkeypatch.setenv("GARMIN_HEALTH_MCP_STATE_DIR", str(tmp_path))
    config = srv.auth_config()
    assert config.resource == "https://g.example/mcp"
    assert config.scope == "garmin:read"
    assert config.client_ip_header == "CF-Connecting-IP"
    assert config.auth_db == tmp_path / "auth.db"


def test_unauthenticated_mcp_is_401(client: TestClient) -> None:
    response = client.post("/mcp", json={}, headers=MCP_HEADERS)
    assert response.status_code == 401
    metadata = client.get("/.well-known/oauth-protected-resource/mcp").json()
    assert metadata["scopes_supported"] == ["garmin:read"]


def test_oauth_flow_reaches_garmin_tools(client: TestClient) -> None:
    verifier = secrets.token_urlsafe(48)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    )
    authorize = client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": "client",
            "redirect_uri": CALLBACK,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "resource": f"{PUBLIC}/mcp",
        },
    )
    page = client.get(authorize.headers["location"].removeprefix(PUBLIC))
    assert "Garmin Health" in page.text
    form = {
        "request": re.findall(r'name=request value="([^"]+)"', page.text)[0],
        "csrf": re.findall(r'name=csrf value="([^"]+)"', page.text)[0],
    }
    consent = client.post(
        "/login",
        data={
            **form,
            "username": "owner",
            "password": "a long enough password",
            "totp": pyotp.TOTP(TOTP).now(),
        },
    )
    assert "Garmin health and activity data" in consent.text
    allowed = client.post("/consent", data={**form, "decision": "allow"})
    code = parse_qs(urlsplit(allowed.headers["location"]).query)["code"][0]
    token = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": CALLBACK,
            "client_id": "client",
            "client_secret": "secret",
            "code_verifier": verifier,
        },
    ).json()
    assert token["scope"] == "garmin:read"
    headers = {**MCP_HEADERS, "Authorization": f"Bearer {token['access_token']}"}
    listed = client.post(
        "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers=headers
    )
    names = {tool["name"] for tool in listed.json()["result"]["tools"]}
    assert {"get_sleep", "get_activity_splits", "search_activities"} <= names


def test_auth_cli_is_wired(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GARMIN_HEALTH_MCP_STATE_DIR", str(tmp_path))
    with pytest.raises(SystemExit) as exit_info:
        srv.main(["auth", "status"])
    assert exit_info.value.code == 1  # not set up yet
