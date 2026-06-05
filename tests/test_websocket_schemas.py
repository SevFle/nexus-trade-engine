"""Unit tests for engine.api.websocket.schemas (SEV-275)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from engine.api.websocket.constants import WS_PROTOCOL_VERSION
from engine.api.websocket.schemas import (
    AuthFailedFrame,
    AuthFrame,
    AuthOkFrame,
    ErrorFrame,
    MarketDepthEvent,
    MarketTickEvent,
    OrderEvent,
    PingFrame,
    PongFrame,
    PortfolioUpdatedEvent,
    ServerShutdownFrame,
    SubscribedFrame,
    SubscribeFrame,
    UnsubscribedFrame,
    UnsubscribeFrame,
)

# ---------------------------------------------------------------------------
# Round-trip serialisation for every event type
# ---------------------------------------------------------------------------
_ROUND_TRIP_MODELS = [
    AuthFrame(token="jwt.or.api_key"),
    SubscribeFrame(channel="portfolio"),
    SubscribeFrame(channel="market", symbols=["AAPL", "MSFT"]),
    UnsubscribeFrame(channel="market", symbols=["AAPL"]),
    PingFrame(),
    PingFrame(ts=datetime(2026, 1, 1, tzinfo=UTC)),
    AuthOkFrame(user_id="u-1", scopes=["read"]),
    AuthFailedFrame(reason="invalid_token"),
    SubscribedFrame(channel="market", symbols=["AAPL"]),
    UnsubscribedFrame(channel="market"),
    PongFrame(server_ts=datetime(2026, 1, 1, tzinfo=UTC)),
    ServerShutdownFrame(),
    ErrorFrame(code="malformed", detail="oops"),
    PortfolioUpdatedEvent(
        user_id="u-1",
        portfolio_id="p-1",
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        nav=Decimal("100"),
    ),
    OrderEvent(
        type="order.filled",
        user_id="u-1",
        order_id="o-1",
        symbol="AAPL",
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        status="filled",
    ),
    MarketTickEvent(
        symbol="AAPL",
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        last=Decimal("123.45"),
    ),
    MarketDepthEvent(
        symbol="AAPL",
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        bids=[[Decimal("123"), Decimal("5")]],
    ),
]


class TestRoundTrip:
    @pytest.mark.parametrize("model", _ROUND_TRIP_MODELS)
    def test_round_trip(self, model):
        dumped = model.model_dump(mode="json")
        rebuilt = type(model).model_validate(dumped)
        assert dumped == rebuilt.model_dump(mode="json")

    @pytest.mark.parametrize("model", _ROUND_TRIP_MODELS)
    def test_envelope_version_present(self, model):
        dumped = model.model_dump(mode="json")
        # Every frame carries the version field.
        assert dumped["v"] == WS_PROTOCOL_VERSION


# ---------------------------------------------------------------------------
# Validation errors
# ---------------------------------------------------------------------------
class TestValidation:
    def test_auth_frame_requires_token(self):
        with pytest.raises(ValidationError):
            AuthFrame(token="")

    def test_subscribe_channel_is_limited(self):
        with pytest.raises(ValidationError):
            SubscribeFrame(channel="unknown")

    def test_unsubscribe_channel_is_limited(self):
        with pytest.raises(ValidationError):
            UnsubscribeFrame(channel="wizardry")

    def test_error_code_is_limited(self):
        with pytest.raises(ValidationError):
            ErrorFrame(code="not_a_code")

    def test_auth_failed_reason_is_limited(self):
        with pytest.raises(ValidationError):
            AuthFailedFrame(reason="some_random_reason")

    def test_extra_fields_are_forbidden(self):
        with pytest.raises(ValidationError):
            AuthFrame.model_validate({"type": "auth", "token": "x", "extra": True})

    def test_order_event_type_is_limited(self):
        with pytest.raises(ValidationError):
            OrderEvent(
                type="order.unthinkable",
                user_id="u",
                order_id="o",
                symbol="AAPL",
                timestamp=datetime(2026, 1, 1, tzinfo=UTC),
                status="x",
            )

    def test_market_tick_requires_symbol(self):
        with pytest.raises(ValidationError):
            MarketTickEvent(
                symbol="",
                timestamp=datetime(2026, 1, 1, tzinfo=UTC),
            )


# ---------------------------------------------------------------------------
# Discriminator
# ---------------------------------------------------------------------------
class TestDiscriminator:
    def test_auth_frame_validates(self):
        frame = AuthFrame.model_validate({"type": "auth", "token": "abc"})
        assert frame.token == "abc"

    def test_subscribe_frame_validates(self):
        frame = SubscribeFrame.model_validate(
            {"type": "subscribe", "channel": "market", "symbols": ["AAPL"]}
        )
        assert frame.channel == "market"
        assert frame.symbols == ["AAPL"]

    def test_unknown_type_fails_validation(self):
        with pytest.raises(ValidationError):
            AuthFrame.model_validate({"type": "wizardry", "token": "x"})
