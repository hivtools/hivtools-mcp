"""Tests for the MCP server mounted onto ``app`` at ``/mcp``.

Talks real streamable-HTTP JSON-RPC over the ASGI ``TestClient`` rather than
calling into fastmcp's objects directly - the bug this guards against only
shows up when the app's own lifespan runs for real, so the test needs to run
through the same path a live server does.
"""

import json
import re
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from loguru import logger

from app.knowledge.loader import instructions
from app.settings import settings

MCP_HEADERS = {"Accept": "application/json, text/event-stream"}
TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


def _sse_json(response) -> dict:
    """Pull the JSON-RPC payload out of a streamable-HTTP SSE response."""
    matches = re.findall(r"^data: (.*)$", response.text, re.MULTILINE)
    assert matches, f"no SSE data line in response: {response.text!r}"
    return json.loads(matches[-1])


def _rpc(
    client: TestClient, session_id: str, method: str, params: dict | None = None, headers: dict | None = None
) -> dict:
    body = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        body["params"] = params
    response = client.post("/mcp", json=body, headers={**MCP_HEADERS, "mcp-session-id": session_id, **(headers or {})})
    return _sse_json(response)["result"]


@pytest.fixture
def tool_logs() -> Iterator[list[dict]]:
    """The ``extra`` fields of every tool-call log record emitted during the test."""
    records: list[dict] = []
    handler_id = logger.add(
        lambda message: records.append(message.record["extra"]),
        filter=lambda record: record["extra"].get("event") == "tool_call",
    )
    yield records
    logger.remove(handler_id)


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


def test_only_the_data_routes_are_exposed_as_tools(client: TestClient, mcp_session: str):
    tools = _rpc(client, mcp_session, "tools/list")["tools"]
    assert sorted(tool["name"] for tool in tools) == ["get_hiv_data", "search_hiv_metadata"]


def test_every_tool_and_parameter_is_documented(client: TestClient, mcp_session: str):
    # A tool's schema is all an MCP client has to go on when deciding how to call
    # it - an opaque param like `area_id` or `field` is a guess without a
    # description. This doesn't check wording (that'd be brittle), just that
    # nobody adds a parameter without documenting it.
    for tool in _rpc(client, mcp_session, "tools/list")["tools"]:
        assert tool["description"], tool["name"]
        undocumented = [
            name for name, schema in tool["inputSchema"]["properties"].items() if not schema.get("description")
        ]
        assert not undocumented, f"{tool['name']}: {undocumented}"


def test_data_tool_exposes_every_filter(client: TestClient, mcp_session: str):
    tools = {tool["name"]: tool for tool in _rpc(client, mcp_session, "tools/list")["tools"]}
    properties = tools["get_hiv_data"]["inputSchema"]["properties"]
    assert set(properties) == {
        "country",
        "source",
        "area_level",
        "area_id",
        "sex",
        "age_group",
        "risk_group",
        "age_partition",
        "calendar_quarter",
        "indicator",
        "columns",
        "limit",
        "offset",
        "sig_figs",
        "user_question",
    }


def test_data_tool_tells_the_model_to_resolve_ids_with_search(client: TestClient, mcp_session: str):
    # claude.ai drops the server `instructions`, so the search-first workflow only
    # reaches the model if it is in get_hiv_data's own description.
    tools = {tool["name"]: tool for tool in _rpc(client, mcp_session, "tools/list")["tools"]}
    data_tool = tools["get_hiv_data"]
    assert "search_hiv_metadata" in data_tool["description"]
    for name in ("indicator", "area_id", "age_group", "age_partition", "risk_group", "calendar_quarter", "source"):
        assert "search_hiv_metadata" in data_tool["inputSchema"]["properties"][name]["description"], name


def test_instructions_only_name_real_tools(client: TestClient, mcp_session: str):
    tool_names = {tool["name"] for tool in _rpc(client, mcp_session, "tools/list")["tools"]}
    mentioned = set(re.findall(r"`((?:get|search)\w*)`", instructions()))
    assert mentioned
    assert mentioned <= tool_names, f"instructions name tools that do not exist: {sorted(mentioned - tool_names)}"


def test_tool_call_is_logged_with_what_links_it_to_a_conversation(
    client: TestClient, mcp_session: str, tool_logs: list[dict]
):
    question = "What is HIV prevalence in Malawi?"
    _rpc(
        client,
        mcp_session,
        "tools/call",
        {"name": "get_hiv_data", "arguments": {"country": ["MWI"], "limit": 1, "user_question": question}},
        headers={"traceparent": TRACEPARENT},
    )
    [record] = tool_logs
    assert record["tool"] == "get_hiv_data"
    assert record["user_question"] == question
    assert record["arguments"]["country"] == ["MWI"]
    assert record["mcp_session_id"] == mcp_session
    assert record["traceparent"] == TRACEPARENT
    assert record["is_error"] is False
    assert record["total"] == 3
    assert json.loads(record["response"])["total"] == 3


def test_logged_response_is_truncated(
    client: TestClient, mcp_session: str, tool_logs: list[dict], monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings, "log_response_chars", 10)
    _rpc(client, mcp_session, "tools/call", {"name": "get_hiv_data", "arguments": {}})
    [record] = tool_logs
    assert len(record["response"]) == 10
    assert record["response_chars"] > 10
    # Pulled from the full response, so it survives truncation.
    assert record["total"] == 8


def test_empty_result_logs_its_diagnostic(client: TestClient, mcp_session: str, tool_logs: list[dict]):
    _rpc(client, mcp_session, "tools/call", {"name": "get_hiv_data", "arguments": {"indicator": ["no_such_thing"]}})
    [record] = tool_logs
    assert record["total"] == 0
    assert record["diagnostic"]


def test_failed_tool_call_is_logged(client: TestClient, mcp_session: str, tool_logs: list[dict]):
    _rpc(
        client,
        mcp_session,
        "tools/call",
        {"name": "search_hiv_metadata", "arguments": {"q": ["Lilongwe"], "field": ["not_a_field"]}},
    )
    [record] = tool_logs
    assert record["tool"] == "search_hiv_metadata"
    assert record["is_error"] is True


def test_search_tool_can_be_called(client: TestClient, mcp_session: str):
    result = _rpc(
        client,
        mcp_session,
        "tools/call",
        {"name": "search_hiv_metadata", "arguments": {"q": ["treatment gap"], "country": "MWI"}},
    )
    assert result["isError"] is False
    payload = json.loads(result["content"][0]["text"])
    assert payload["results"][0]["results"][0]["id"] == "treatment_gap"


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
    assert payload["total"] == 8
    assert len(payload["data"]) == 2


def test_initialize_returns_the_semantic_document(client: TestClient):
    # The knowledge layer only reaches the model if it rides out on the
    # initialize handshake, which is easy to break silently - nothing else
    # fails when `instructions` is empty. Assert on the load-bearing claims
    # rather than the wording, which is expected to change.
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
    instructions = _sse_json(response)["result"]["instructions"]
    assert "synthetic demonstration data" in instructions
    assert "Never sum across these dimensions" in instructions
