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

The generated tools call ``api_app`` in-process, and so pass through its API key
check like any other request. They authenticate with ``app.auth``'s per-process
token, which takes priority over any caller headers fastmcp forwards.
"""

from fastapi import FastAPI
from fastmcp import FastMCP
from fastmcp.server.providers.openapi.routing import MCPType, RouteMap

from app.auth import internal_headers
from app.knowledge.loader import instructions
from app.observability import ToolCallLogger

EXCLUDED_TAGS = ("health", "meta")


def build_mcp_app(api_app: FastAPI, *, name: str = "hivtools", path: str = "/mcp"):
    """Wrap ``api_app`` as an MCP ASGI app (streamable-HTTP) serving at ``path``."""
    mcp = FastMCP.from_fastapi(
        app=api_app,
        name=name,
        route_maps=[RouteMap(tags={tag}, mcp_type=MCPType.EXCLUDE) for tag in EXCLUDED_TAGS],
        httpx_client_kwargs={"headers": internal_headers()},
    )
    mcp.instructions = instructions()
    mcp.add_middleware(ToolCallLogger())
    return mcp.http_app(path=path)
