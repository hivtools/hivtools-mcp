"""Tests for the token that guards the data.

The data is not all public, so what matters is that nothing that serves it -
the API routes or the MCP tools built from them - answers without the token,
and that the MCP tools still work with it despite calling the API in-process.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from loguru import logger
from pydantic import SecretStr

from app import settings as settings_module
from app.main import app
from app.ratelimit import limiter
from tests.test_mcp import MCP_HEADERS, _sse_json

TOKEN = "correct-horse-battery-staple"  # noqa: S105 - a test fixture, not a credential
AUTH = {"Authorization": f"Bearer {TOKEN}"}


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
    assert response.headers["www-authenticate"] == "Bearer"
    assert guarded.get(path, headers=AUTH).status_code == 200


@pytest.mark.parametrize(
    "headers",
    [
        {"Authorization": f"Bearer {TOKEN}x"},
        {"Authorization": f"Basic {TOKEN}"},
        {"Authorization": TOKEN},
        {"Authorization": "Bearer "},
        {"X-API-Key": TOKEN},
    ],
    ids=["wrong token", "wrong scheme", "no scheme", "empty", "as an api key"],
)
def test_anything_but_the_token_is_refused(guarded: TestClient, headers: dict[str, str]):
    assert guarded.get("/data", headers=headers).status_code == 401


def test_the_scheme_is_case_insensitive(guarded: TestClient):
    assert guarded.get("/data", headers={"Authorization": f"bearer {TOKEN}"}).status_code == 200


@pytest.mark.parametrize("path", ["/", "/version", "/favicon.ico", "/health", "/health/ready", "/openapi.json"])
def test_probes_and_metadata_stay_open(guarded: TestClient, path: str):
    """The platform's probes and the release smoke test carry no token."""
    assert guarded.get(path).status_code == 200


def test_refusals_are_logged_without_the_key(guarded: TestClient):
    """The log says why a request was refused and which headers it had, so a
    client sending the key wrongly can be told apart from one not sending it."""
    records = []
    sink = logger.add(lambda message: records.append(message.record), level="INFO")
    try:
        guarded.get("/data", headers={"Authorization": "Bearer not-the-key", "X-API-Key": "also-secret"})
        guarded.get("/data")
        guarded.get("/data", headers=AUTH)
    finally:
        logger.remove(sink)
    refused = [record["extra"] for record in records if record["extra"].get("event") == "auth_refused"]
    assert [entry["reason"] for entry in refused] == ["bearer token does not match", "no bearer token"]
    assert {"authorization", "x-api-key"} <= set(refused[0]["headers"])
    assert refused[0]["path"] == "/data"
    logged = str(records)
    assert "not-the-key" not in logged
    assert "also-secret" not in logged
    assert TOKEN not in logged


def test_mcp_needs_the_token(guarded: TestClient):
    assert _initialize(guarded, {}).status_code == 401


def test_mcp_tools_still_reach_the_api_with_the_token(guarded: TestClient):
    """The tools call the API in-process, and fastmcp drops the caller's
    Authorization header on the way. Without the internal token those calls
    would be refused, and every tool call would fail with a 401 inside it."""
    response = _initialize(guarded, AUTH)
    session = {**MCP_HEADERS, **AUTH, "mcp-session-id": response.headers["mcp-session-id"]}
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
        assert client.get("/data", headers=AUTH).status_code == 200
