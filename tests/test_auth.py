"""Tests for the token that guards the data.

The data is not all public, so what matters is that nothing that serves it -
the API routes or the MCP tools built from them - answers without the token,
and that the MCP tools still work with it despite calling the API in-process.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app import settings as settings_module
from app.main import app
from app.ratelimit import limiter
from tests.test_mcp import MCP_HEADERS, _sse_json

TOKEN = "correct-horse-battery-staple"  # noqa: S105 - a test fixture, not a credential
API_KEY = {"X-API-Key": TOKEN}


@pytest.fixture
def guarded(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr(settings_module.settings, "naomi_data_dir", data_dir)
    monkeypatch.setattr(settings_module.settings, "api_token", SecretStr(TOKEN))
    monkeypatch.setattr(limiter, "enabled", False)
    with TestClient(app) as test_client:
        yield test_client


def _initialize(client: TestClient, headers: dict[str, str]):
    return client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "1.0"},
            },
        },
        headers={**MCP_HEADERS, **headers},
    )


@pytest.mark.parametrize("path", ["/data", "/search?q=x"])
def test_data_routes_need_the_token(guarded: TestClient, path: str):
    response = guarded.get(path)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == 'ApiKey header="X-API-Key"'
    assert guarded.get(path, headers=API_KEY).status_code == 200


@pytest.mark.parametrize(
    "headers",
    [
        {"X-API-Key": f"{TOKEN}x"},
        {"X-API-Key": f"Bearer {TOKEN}"},
        {"X-API-Key": ""},
        {"Authorization": f"Bearer {TOKEN}"},
    ],
    ids=["wrong token", "with a scheme", "empty", "as a bearer token"],
)
def test_anything_but_the_token_is_refused(guarded: TestClient, headers: dict[str, str]):
    assert guarded.get("/data", headers=headers).status_code == 401


@pytest.mark.parametrize("path", ["/", "/version", "/favicon.ico", "/health", "/health/ready", "/openapi.json"])
def test_probes_and_metadata_stay_open(guarded: TestClient, path: str):
    """The platform's probes and the release smoke test carry no token."""
    assert guarded.get(path).status_code == 200


def test_mcp_needs_the_token(guarded: TestClient):
    assert _initialize(guarded, {}).status_code == 401


def test_mcp_tools_still_reach_the_api_with_the_token(guarded: TestClient):
    """The tools call the API in-process, so those calls have to get past the
    token check too, or every tool call would fail with a 401 inside it."""
    response = _initialize(guarded, API_KEY)
    session = {**MCP_HEADERS, **API_KEY, "mcp-session-id": response.headers["mcp-session-id"]}
    guarded.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=session)
    call = guarded.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "get_hiv_data", "arguments": {"country": ["MWI"]}},
        },
        headers=session,
    )
    result = _sse_json(call)["result"]
    assert result["isError"] is False
    assert '"total":3' in result["content"][0]["text"].replace(" ", "")


def test_the_api_is_open_when_no_token_is_configured(client: TestClient):
    """Local development: nothing configured, nothing required."""
    assert client.get("/data").status_code == 200


def test_requiring_auth_without_a_token_refuses_to_start(data_dir: Path, monkeypatch: pytest.MonkeyPatch):
    """The image requires auth, so a deployment missing its token fails loudly
    instead of serving the data to anyone."""
    monkeypatch.setattr(settings_module.settings, "naomi_data_dir", data_dir)
    monkeypatch.setattr(settings_module.settings, "require_auth", True)
    with pytest.raises(RuntimeError, match="HIVTOOLS_MCP_API_TOKEN"), TestClient(app):
        pass


def test_requiring_auth_with_a_token_starts(guarded: TestClient, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings_module.settings, "require_auth", True)
    with TestClient(app) as client:
        assert client.get("/data", headers=API_KEY).status_code == 200
