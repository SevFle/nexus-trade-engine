"""Error model for the Nexus MCP server.

Two error surfaces exist in MCP:

* **JSON-RPC protocol errors** — raised to fail the whole request with a
  numeric error code (``-32xxx`` range). Used for transport/auth level
  rejections where no tool result is meaningful.
* **Tool execution errors** — returned as a :class:`mcp.types.CallToolResult`
  with ``isError=True``. This is the spec-recommended way to surface
  validation, engine, and operational errors to the assistant without
  tearing down the session.

This module defines a typed exception hierarchy plus helpers to map engine
exceptions onto MCP errors.
"""

from __future__ import annotations

from typing import Any

# ── JSON-RPC standard error codes ──
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# ── Server-defined error codes (reserved range -32000 .. -32099) ──
AUTHENTICATION_ERROR = -32001
AUTHORIZATION_ERROR = -32002
RATE_LIMIT_ERROR = -32003
ENGINE_ERROR = -32004
NOT_FOUND_ERROR = -32005


class MCPError(Exception):
    """Base class for all MCP-server errors.

    Each subclass carries an MCP/JSON-RPC ``code`` and a human-readable
    ``message``. ``data`` holds optional structured detail that is safe to
    expose to the assistant.
    """

    code: int = INTERNAL_ERROR

    def __init__(self, message: str, *, data: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.data = data

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data:
            out["data"] = self.data
        return out


class AuthenticationError(MCPError):
    """The caller did not provide valid credentials."""

    code = AUTHENTICATION_ERROR


class AuthorizationError(MCPError):
    """The caller is authenticated but lacks the required RBAC role."""

    code = AUTHORIZATION_ERROR


class RateLimitError(MCPError):
    """The caller has exceeded the configured request rate."""

    code = RATE_LIMIT_ERROR


class ValidationError(MCPError):
    """Tool arguments failed validation."""

    code = INVALID_PARAMS


class NotFoundError(MCPError):
    """A referenced entity (strategy, portfolio, ...) does not exist."""

    code = NOT_FOUND_ERROR


class EngineError(MCPError):
    """An engine-level failure occurred while executing a tool."""

    code = ENGINE_ERROR


def map_engine_exception(exc: BaseException) -> MCPError:
    """Translate an arbitrary engine exception into an :class:`MCPError`.

    Known engine value errors (insufficient cash, unknown symbol, missing
    strategy) map to validation/not-found errors; everything else becomes an
    opaque :class:`EngineError` so internal tracebacks are never leaked to
    the assistant.
    """
    if isinstance(exc, MCPError):
        return exc
    msg = str(exc) or exc.__class__.__name__
    lower = msg.lower()
    if "not found" in lower or "no position" in lower:
        return NotFoundError(msg)
    if isinstance(exc, ValueError | TypeError):
        return ValidationError(msg)
    # Never expose internal engine tracebacks to the LLM.
    return EngineError(f"Engine operation failed: {exc.__class__.__name__}")


__all__ = [
    "AUTHENTICATION_ERROR",
    "AUTHORIZATION_ERROR",
    "ENGINE_ERROR",
    "INTERNAL_ERROR",
    "INVALID_PARAMS",
    "NOT_FOUND_ERROR",
    "RATE_LIMIT_ERROR",
    "AuthenticationError",
    "AuthorizationError",
    "EngineError",
    "MCPError",
    "NotFoundError",
    "RateLimitError",
    "ValidationError",
    "map_engine_exception",
]
