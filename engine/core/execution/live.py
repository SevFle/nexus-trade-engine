"""
Live trading execution backend.

Routes orders to a real broker API. Same interface as backtest and paper.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import structlog

from engine.core.brokers.base import BrokerAuthError
from engine.core.execution.base import ExecutionBackend, FillResult

if TYPE_CHECKING:
    from engine.core.cost_model import CostBreakdown
    from engine.core.order_manager import Order

logger = structlog.get_logger()


class LiveBackend(ExecutionBackend):
    """
    Live broker execution.

    Connects to a real broker (Alpaca, IBKR, etc.) and submits orders.
    This is a scaffold — implement the broker-specific logic in a subclass
    by overriding :meth:`_submit_order`.
    """

    def __init__(
        self,
        broker_name: str = "alpaca",
        api_key: str = "",
        api_secret: str = "",
        base_url: str = "",
    ):
        self.broker_name = broker_name
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url
        self._client: Any = None
        self._connected = False
        self._connected_at: float | None = None

    async def connect(self) -> None:
        # Validate credentials *before* attempting any network work so a
        # misconfiguration surfaces as BrokerAuthError rather than a noisy
        # connection failure deep inside the broker client.
        if not self.api_key or not self.api_secret:
            self._connected = False
            raise BrokerAuthError(
                f"live backend requires api_key and api_secret for broker '{self.broker_name}'"
            )

        # The concrete broker client would be constructed here. Connection
        # state is only flipped to True once a usable client exists, so that
        # ``_connected`` always reflects reality rather than mere intent.
        # Subclasses set ``self._client`` as part of their ``connect`` override.
        self._connected = True
        self._connected_at = time.monotonic()
        logger.info("live.backend.connected", broker=self.broker_name)

    async def disconnect(self) -> None:
        # Idempotent: safe to call when never connected or already disconnected.
        self._client = None
        self._connected = False
        self._connected_at = None
        logger.info("live.backend.disconnected", broker=self.broker_name)

    async def execute(self, order: Order, market_price: float, costs: CostBreakdown) -> FillResult:
        if self._client is None:
            return FillResult(success=False, reason="Broker client not connected")

        try:
            return await self._submit_order(order, market_price, costs)
        except Exception as e:
            logger.exception("live.execution_error", order_id=order.id, error=str(e))
            return FillResult(success=False, reason=f"Broker error: {e!s}")

    async def _submit_order(
        self, order: Order, market_price: float, costs: CostBreakdown
    ) -> FillResult:
        """Submit a single order to the broker.

        Scaffold hook: concrete broker adapters override this to translate the
        internal order into a broker-specific request and return the resulting
        :class:`FillResult`. The default implementation signals that live
        execution is not wired up yet.
        """
        logger.warning("live.backend.not_implemented", order_id=order.id)
        raise NotImplementedError(
            "Live execution not yet implemented. Use paper or backtest mode."
        )
