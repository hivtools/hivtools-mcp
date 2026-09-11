"""The MCP server: exposes ``api_app``'s routes as MCP tools.

Built from the API's own OpenAPI schema via fastmcp, so every route gets a
tool for free with no separate definitions to maintain. Routes tagged
"health" or "meta" are infra/operational endpoints (liveness probes, the
root banner) rather than data an MCP client would want to browse, so they're
excluded from the generated tool list.
"""

from fastapi import FastAPI
from fastmcp import FastMCP
from fastmcp.server.providers.openapi.routing import MCPType, RouteMap

EXCLUDED_TAGS = ("health", "meta")


def build_mcp_app(api_app: FastAPI, *, name: str = "hivtools", path: str = "/mcp"):
    """Wrap ``api_app`` as an MCP ASGI app (streamable-HTTP) serving at ``path``."""
    mcp = FastMCP.from_fastapi(
        app=api_app,
        name=name,
        route_maps=[RouteMap(tags={tag}, mcp_type=MCPType.EXCLUDE) for tag in EXCLUDED_TAGS],
    )
    return mcp.http_app(path=path)
