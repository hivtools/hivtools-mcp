"""Bearer-token authentication for everything except the probes.

One shared token, sent as an ``Authorization: Bearer`` header - which is how the
claude.ai connector sends a static credential (an organisation admin enters it
when adding the connector). Not all of the data served is public, so the token
guards the API routes as well as ``/mcp``: the MCP tools are those same routes.

Pure ASGI middleware rather than a FastAPI dependency, so that it wraps the
mounted MCP app too, and so no route added later can forget to depend on it.

The MCP tools call back into this same app in-process (see ``app.mcp``), and
fastmcp deliberately drops ``Authorization`` from the headers it forwards. Those
calls carry a per-process secret instead. It never leaves memory, and it is only
ever presented after the outer ``/mcp`` request has itself been authenticated.

With no token configured the API is open, which is only for local development.
The container image sets ``require_auth``, so a deployment that is missing its
token refuses to start rather than serving the data to anyone.
"""

import secrets
from collections.abc import AsyncIterator, Awaitable, Callable, MutableMapping
from contextlib import asynccontextmanager
from typing import Any

from fastapi.datastructures import Headers
from fastapi.responses import JSONResponse

from app.settings import settings

# The ASGI interface, as starlette.types spells it (starlette is only a
# transitive dependency, via FastAPI).
Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

# Answer without a token: the platform's probes, the version the release smoke
# test reads, and the schema and docs pages. None of them carry data.
PUBLIC_PATHS = frozenset({
    "/",
    "/version",
    "/health",
    "/health/ready",
    "/openapi.json",
    "/docs",
    "/docs/oauth2-redirect",
    "/redoc",
})

_INTERNAL_TOKEN = secrets.token_urlsafe(32)


def internal_headers() -> dict[str, str]:
    """Headers for the in-process client the MCP tools call the API with."""
    return {"Authorization": f"Bearer {_INTERNAL_TOKEN}"}


def check_configuration() -> None:
    if settings.require_auth and settings.api_token is None:
        msg = "HIVTOOLS_MCP_REQUIRE_AUTH is set but HIVTOOLS_MCP_API_TOKEN is not; refusing to serve data without auth"
        raise RuntimeError(msg)


@asynccontextmanager
async def lifespan(app: Any) -> AsyncIterator[None]:
    """Fail at startup, not at the first request, when the token is missing."""
    check_configuration()
    yield


def _same(presented: str, expected: str) -> bool:
    return secrets.compare_digest(presented.encode(), expected.encode())


def _authorised(scope: Scope, token: str) -> bool:
    scheme, _, credentials = Headers(scope=scope).get("authorization", "").partition(" ")
    if scheme.lower() != "bearer":
        return False
    presented = credentials.strip()
    return _same(presented, token) or _same(presented, _INTERNAL_TOKEN)


class BearerAuthMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Read per request, not at startup, so the setting can be changed in tests.
        token = settings.api_token
        if (
            scope["type"] != "http"
            or token is None
            or scope["path"] in PUBLIC_PATHS
            or _authorised(scope, token.get_secret_value())
        ):
            await self.app(scope, receive, send)
            return
        response = JSONResponse(
            {"detail": "Missing or invalid bearer token"},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )
        await response(scope, receive, send)
