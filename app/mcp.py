"""The MCP server: exposes ``api_app``'s routes as MCP tools.

Built from the API's own OpenAPI schema via fastmcp, so every route gets a
tool for free with no separate definitions to maintain. Routes tagged
"health" or "meta" are infra/operational endpoints (liveness probes, the
root banner) rather than data an MCP client would want to browse, so they're
excluded from the generated tool list.

The server also carries ``instructions`` - the semantic document from
``app.knowledge``. MCP clients may add it to the system prompt, so it is where
dataset-wide facts live that no single tool description can carry: what each
source covers, how each country's figures must be described, and above all which
dimensions must never be summed. The spec makes injecting it optional, so nothing here relies on
it alone; the same facts are enforced in responses.

Every tool call is logged by ``app.observability.ToolCallLogger``.

The generated tools call ``api_app`` in-process, and so pass through its bearer
auth like any other request. They authenticate with ``app.auth``'s per-process
token, since fastmcp does not forward the caller's own ``Authorization`` header.
"""

from typing import Any

from fastapi import FastAPI
from fastmcp import FastMCP
from fastmcp.server.providers.openapi import OpenAPITool
from fastmcp.server.providers.openapi.routing import MCPType, RouteMap
from fastmcp.utilities.openapi import HTTPRoute

from app.auth import internal_headers
from app.knowledge.loader import instructions
from app.observability import ToolCallLogger

EXCLUDED_TAGS = ("health", "meta")

# The names clients such as claude.ai display. Without them fastmcp title-cases
# the tool name, which gives "Get Hiv Data".
TOOL_TITLES = {
    "get_hiv_data": "Get HIV Data",
    "search_hiv_metadata": "Search HIV Metadata",
}


def _set_title(route: HTTPRoute, component: Any) -> None:
    if isinstance(component, OpenAPITool) and route.operation_id in TOOL_TITLES:
        component.title = TOOL_TITLES[route.operation_id]


def build_mcp_app(api_app: FastAPI, *, name: str = "hivtools", path: str = "/mcp"):
    """Wrap ``api_app`` as an MCP ASGI app (streamable-HTTP) serving at ``path``."""
    mcp = FastMCP.from_fastapi(
        app=api_app,
        name=name,
        route_maps=[RouteMap(tags={tag}, mcp_type=MCPType.EXCLUDE) for tag in EXCLUDED_TAGS],
        mcp_component_fn=_set_title,
        httpx_client_kwargs={"headers": internal_headers()},
    )
    mcp.instructions = instructions()
    mcp.add_middleware(ToolCallLogger())
    return mcp.http_app(path=path)
