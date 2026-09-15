"""Logging, and a record of how MCP clients use the tools.

Every MCP tool call is logged as one line carrying what the model sent, what came
back, and whatever the request says about where it came from. claude.ai sends no
user or conversation id, so the joins available are:

- ``mcp_session_id``: the ``Mcp-Session-Id`` header, stable across the calls one
  client session makes.
- ``traceparent``: the W3C trace header claude.ai attaches to each request.
- ``user_question``: an optional tool argument the model is asked to fill with the
  user's question verbatim. It is the only thing that ties a call to what a person
  actually typed, which is what makes these logs matchable against chat transcripts.

In production (``log_json``) each record is a JSON line on stdout, which Container
Apps ships to Log Analytics; query the fields under ``record.extra``.
"""

import json
import sys
import time
from typing import Annotated, Any

from fastapi import Query
from fastmcp.server.dependencies import get_http_headers
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.base import ToolResult
from loguru import logger

from app.settings import settings

UserQuestion = Annotated[
    str | None,
    Query(
        description="Always pass this: the user's question that led to this call, copied verbatim from "
        "their message rather than paraphrased. It is only recorded to understand how the tool is "
        "used and never changes the result."
    ),
]


def configure_logging() -> None:
    logger.remove()
    if settings.log_json:
        logger.add(sys.stdout, level=settings.log_level, serialize=True)
    else:
        logger.add(
            sys.stderr,
            level=settings.log_level,
            format="{time:HH:mm:ss} | <level>{level: <7}</level> | {message} | {extra}",
        )


def _text(result: ToolResult) -> str:
    return "".join(text for block in result.content if isinstance(text := getattr(block, "text", None), str))


def _outcome(text: str) -> dict[str, Any]:
    """The fields worth querying on directly: ``total == 0`` finds the empty results,
    and ``diagnostic`` says what the model was told about them."""
    try:
        payload = json.loads(text)
    except ValueError:
        return {}
    if not isinstance(payload, dict):
        return {}
    return {key: payload[key] for key in ("total", "diagnostic") if key in payload}


class ToolCallLogger(Middleware):
    """One log line per tool call: the arguments the model chose and what it got back."""

    async def on_call_tool(self, context: MiddlewareContext[Any], call_next: CallNext[Any, ToolResult]) -> ToolResult:
        arguments = dict(context.message.arguments or {})
        headers = get_http_headers(include={"mcp-session-id"})
        log = logger.bind(
            event="tool_call",
            tool=context.message.name,
            user_question=arguments.get("user_question"),
            arguments=arguments,
            mcp_session_id=headers.get("mcp-session-id"),
            traceparent=headers.get("traceparent"),
        )
        started = time.perf_counter()
        try:
            result = await call_next(context)
        except Exception as exc:
            duration_ms = round((time.perf_counter() - started) * 1000, 1)
            log.bind(duration_ms=duration_ms, is_error=True, error=str(exc)).warning(
                "tool call {} failed", context.message.name
            )
            raise

        duration_ms = round((time.perf_counter() - started) * 1000, 1)
        text = _text(result)
        log.bind(
            duration_ms=duration_ms,
            is_error=result.is_error,
            response_chars=len(text),
            response=text[: settings.log_response_chars],
            **_outcome(text),
        ).info("tool call {}", context.message.name)
        return result
