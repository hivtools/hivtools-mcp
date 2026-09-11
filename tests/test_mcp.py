"""Tests for the MCP server mounted onto ``app`` at ``/mcp``.

Talks real streamable-HTTP JSON-RPC over the ASGI ``TestClient`` rather than
calling into fastmcp's objects directly - the bug this guards against only
shows up when the app's own lifespan runs for real, so the test needs to run
through the same path a live server does.
"""

import json
import re

import pytest
from fastapi.testclient import TestClient

MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


def _sse_json(response) -> dict:
    """Pull the JSON-RPC payload out of a streamable-HTTP SSE response."""
    matches = re.findall(r"^data: (.*)$", response.text, re.MULTILINE)
    assert matches, f"no SSE data line in response: {response.text!r}"
    return json.loads(matches[-1])


def _rpc(client: TestClient, session_id: str, method: str, params: dict | None = None) -> dict:
    body = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        body["params"] = params
    response = client.post("/mcp", json=body, headers={**MCP_HEADERS, "mcp-session-id": session_id})
    return _sse_json(response)["result"]


@pytest.fixture
def mcp_session(client: TestClient) -> str:
    """Complete the MCP initialize handshake and return the session id."""
    response = client.post(
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
        headers=MCP_HEADERS,
    )
    session_id = response.headers["mcp-session-id"]
    client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers={**MCP_HEADERS, "mcp-session-id": session_id},
    )
    return session_id


def test_only_the_data_route_is_exposed_as_a_tool(client: TestClient, mcp_session: str):
    tools = _rpc(client, mcp_session, "tools/list")["tools"]
    assert [tool["name"] for tool in tools] == ["get_hiv_data"]


def test_tool_and_every_parameter_are_documented(client: TestClient, mcp_session: str):
    # The tool's schema is all an MCP client has to go on when deciding how to
    # call it - an opaque param like `area_id` or `calendar_quarter` is a guess
    # without a description. This doesn't check wording (that'd be brittle),
    # just that nobody adds a new filter to `get_data` without documenting it.
    tool = _rpc(client, mcp_session, "tools/list")["tools"][0]
    assert tool["description"]
    properties = tool["inputSchema"]["properties"]
    assert set(properties) == {
        "country",
        "area_level",
        "area_id",
        "sex",
        "age_group",
        "calendar_quarter",
        "indicator",
        "columns",
        "limit",
        "offset",
        "sig_figs",
    }
    undocumented = [name for name, schema in properties.items() if not schema.get("description")]
    assert not undocumented


def test_tool_call_returns_real_data(client: TestClient, mcp_session: str):
    # Regression test: fastmcp's generated tool calls back into the *same*
    # FastAPI app instance over an in-process ASGI transport, so it depends
    # on that instance's own lifespan (opening the DuckDB connection) having
    # actually run. When the app serving `/mcp` wasn't the app whose lifespan
    # fired, this call succeeded at the transport level but failed inside
    # with "'State' object has no attribute 'db'" - asserting on the real
    # payload, not just the absence of a 5xx, is what catches that.
    result = _rpc(client, mcp_session, "tools/call", {"name": "get_hiv_data", "arguments": {"limit": 2}})
    assert result["isError"] is False
    payload = json.loads(result["content"][0]["text"])
    assert payload["total"] == 4
    assert len(payload["data"]) == 2
