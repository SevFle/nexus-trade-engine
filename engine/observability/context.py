"""Per-request and per-task observability context.

Built on :mod:`contextvars` so each asyncio Task gets isolated state. The
HTTP middleware, taskiq broker middleware, and outbound HTTP client all
read from this module to thread `correlation_id` end-to-end.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


_FIELDS: tuple[str, ...] = (
    "correlation_id",
    "request_id",
    "span_id",
    "user_id",
    "role",
    "portfolio_id",
    "strategy_id",
    "broker",
    "order_id",
    "tool",
)

_VARS: dict[str, ContextVar[str | None]] = {
    name: ContextVar(f"obs_{name}", default=None) for name in _FIELDS
}


def _set(field: str, value: str | None) -> None:
    _VARS[field].set(value)


def _get(field: str) -> str | None:
    return _VARS[field].get()


def bind_request_scope(
    *, correlation_id: str, request_id: str, span_id: str
) -> list[Any]:
    """Bind the per-request triple and return tokens for later
    :func:`reset_tokens`. Use this when you must restore the prior context
    (e.g., from raw ASGI middleware running inside an inlined caller)."""
    return [
        _VARS["correlation_id"].set(correlation_id),
        _VARS["request_id"].set(request_id),
        _VARS["span_id"].set(span_id),
    ]


def reset_tokens(tokens: list[Any]) -> None:
    """Reset previously captured contextvars tokens in reverse order."""
    for tok in reversed(tokens):
        try:
            tok.var.reset(tok)
        except (LookupError, ValueError):
            # token may already have been reset, e.g., test teardown
            continue


def bind_correlation_id(value: str) -> None:
    _set("correlation_id", value)


def get_correlation_id() -> str | None:
    return _get("correlation_id")


def ensure_correlation_id() -> str:
    """Return existing correlation id or generate and bind a new UUID4."""
    existing = get_correlation_id()
    if existing:
        return existing
    new = str(uuid.uuid4())
    bind_correlation_id(new)
    return new


def bind_request_id(value: str) -> None:
    _set("request_id", value)


def get_request_id() -> str | None:
    return _get("request_id")


def new_span_id(value: str | None = None) -> str:
    sid = value if value is not None else uuid.uuid4().hex[:16]
    _set("span_id", sid)
    return sid


def get_span_id() -> str | None:
    return _get("span_id")


def bind_user_context(*, user_id: str | None = None, role: str | None = None) -> None:
    if user_id is not None:
        _set("user_id", user_id)
    if role is not None:
        _set("role", role)


def bind_domain_context(
    *,
    portfolio_id: str | None = None,
    strategy_id: str | None = None,
    broker: str | None = None,
    order_id: str | None = None,
    tool: str | None = None,
) -> None:
    for k, v in (
        ("portfolio_id", portfolio_id),
        ("strategy_id", strategy_id),
        ("broker", broker),
        ("order_id", order_id),
        ("tool", tool),
    ):
        if v is not None:
            _set(k, v)


def snapshot() -> dict[str, Any]:
    """Return a dict of all bound, non-None context fields."""
    return {name: v for name in _FIELDS if (v := _VARS[name].get()) is not None}


def clear_context() -> None:
    for var in _VARS.values():
        var.set(None)


@asynccontextmanager
async def use_correlation_id(value: str) -> AsyncIterator[None]:
    """Override the correlation id within an async block, restoring on exit."""
    token = _VARS["correlation_id"].set(value)
    try:
        yield
    finally:
        _VARS["correlation_id"].reset(token)


__all__ = [
    "bind_correlation_id",
    "bind_domain_context",
    "bind_request_id",
    "bind_request_scope",
    "bind_user_context",
    "clear_context",
    "ensure_correlation_id",
    "get_correlation_id",
    "get_request_id",
    "get_span_id",
    "new_span_id",
    "reset_tokens",
    "snapshot",
    "use_correlation_id",
]
