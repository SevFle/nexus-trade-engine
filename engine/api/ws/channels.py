"""Channel resolver and subscription logic (SEV-275)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

from engine.api.ws.permissions import check_channel_access, resolve_room_name
from engine.api.ws.protocol import VALID_CHANNELS, SubscribeMessage, UnsubscribeMessage

if TYPE_CHECKING:
    from engine.api.ws.connection_manager import ConnectionManager

logger = structlog.get_logger()


@dataclass
class SubscriptionResult:
    success: bool
    room: str | None = None
    error_code: str | None = None
    message: str | None = None


class ChannelResolver:
    """Resolves subscription requests to rooms with permission checks."""

    def __init__(
        self,
        manager: ConnectionManager,
        max_subscriptions_per_connection: int = 50,
    ) -> None:
        self._manager = manager
        self._max_subscriptions = max_subscriptions_per_connection

    async def handle_subscribe(
        self,
        connection_id: str,
        message: SubscribeMessage,
        user_id: str,
        scopes: list[str],
    ) -> SubscriptionResult:
        if message.channel not in VALID_CHANNELS:
            return SubscriptionResult(
                success=False,
                error_code="404",
                message=f"unknown channel: {message.channel}",
            )

        allowed, _error_code = check_channel_access(
            message.channel, scopes, message.params, user_id
        )
        if not allowed:
            return SubscriptionResult(
                success=False,
                error_code="403",
                message="permission denied",
            )

        room = resolve_room_name(message.channel, message.params)
        if room is None:
            return SubscriptionResult(
                success=False,
                error_code="400",
                message="missing required parameters",
            )

        current_rooms = self._manager.get_rooms(connection_id)
        non_user_rooms = [r for r in current_rooms if not r.startswith("user:")]
        if len(non_user_rooms) >= self._max_subscriptions:
            return SubscriptionResult(
                success=False,
                error_code="429",
                message="max subscriptions reached",
            )

        try:
            await self._manager.join_room(connection_id, room)
            logger.debug(
                "ws.subscribed",
                connection_id=connection_id[:8],
                user_id=user_id,
                room=room,
                channel=message.channel,
            )
            return SubscriptionResult(success=True, room=room)
        except Exception as exc:
            return SubscriptionResult(
                success=False,
                error_code="500",
                message=str(exc),
            )

    async def handle_unsubscribe(
        self,
        connection_id: str,
        message: UnsubscribeMessage,
        user_id: str,
    ) -> SubscriptionResult:
        room = resolve_room_name(message.channel, message.params)
        if room is None:
            return SubscriptionResult(success=True)

        current_rooms = self._manager.get_rooms(connection_id)
        if room in current_rooms:
            await self._manager.leave_room(connection_id, room)
            logger.debug(
                "ws.unsubscribed",
                connection_id=connection_id[:8],
                user_id=user_id,
                room=room,
                channel=message.channel,
            )
        return SubscriptionResult(success=True, room=room)
