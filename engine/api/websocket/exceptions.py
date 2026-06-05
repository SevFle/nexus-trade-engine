"""Exception hierarchy for the WebSocket API (SEV-275).

Handshake / lifecycle failures raise these; route handlers convert
them to the documented close codes + structured error frames.
"""

from __future__ import annotations

from engine.api.websocket.constants import CloseCode


class WebSocketError(Exception):
    """Base class. Carries the application close code to emit."""

    code: int = CloseCode.INTERNAL_ERROR
    reason: str = "internal_error"

    def __init__(self, reason: str | None = None, *, code: int | None = None) -> None:
        if reason is not None:
            self.reason = reason
        if code is not None:
            self.code = code
        super().__init__(self.reason)


class AuthTimeoutError(WebSocketError):
    code = CloseCode.AUTH_TIMEOUT
    reason = "auth_timeout"


class AuthRequiredError(WebSocketError):
    code = CloseCode.AUTH_FAILED
    reason = "auth_required"


class InvalidTokenError(WebSocketError):
    code = CloseCode.AUTH_FAILED
    reason = "invalid_token"


class InactiveUserError(WebSocketError):
    code = CloseCode.AUTH_FAILED
    reason = "inactive_user"


class ForbiddenError(WebSocketError):
    """Authenticated but lacking the scope required for this channel."""

    code = CloseCode.FORBIDDEN
    reason = "forbidden"


class MalformedFrameError(WebSocketError):
    code = CloseCode.MALFORMED
    reason = "malformed"


class SubscriptionLimitError(WebSocketError):
    """Client tried to exceed per-connection subscription caps."""

    code = CloseCode.POLICY_VIOLATION
    reason = "too_many_subscriptions"


class RateLimitedError(WebSocketError):
    code = CloseCode.RATE_LIMITED
    reason = "rate_limited"


class SlowConsumerError(WebSocketError):
    """Raised by the connection's send loop when the outbound queue
    has overflowed beyond the slow-consumer grace budget."""

    code = CloseCode.POLICY_VIOLATION
    reason = "slow_consumer"


__all__ = [
    "AuthRequiredError",
    "AuthTimeoutError",
    "ForbiddenError",
    "InactiveUserError",
    "InvalidTokenError",
    "MalformedFrameError",
    "RateLimitedError",
    "SlowConsumerError",
    "SubscriptionLimitError",
    "WebSocketError",
]
