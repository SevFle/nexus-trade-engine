"""WebSocket API exceptions (SEV-275)."""

from __future__ import annotations


class WebSocketError(Exception):
    def __init__(self, code: int, reason: str) -> None:
        self.code = code
        self.reason = reason
        super().__init__(f"[{code}] {reason}")


class AuthError(WebSocketError):
    pass


class SubscriptionLimitError(WebSocketError):
    pass


class ChannelPermissionError(WebSocketError):
    pass


class QueueFullError(WebSocketError):
    pass


class ConnectionLimitError(WebSocketError):
    pass
