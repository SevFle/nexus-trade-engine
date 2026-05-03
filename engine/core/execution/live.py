"""
Live trading execution backend.

Routes orders to a real broker API. Same interface as backtest and paper.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from engine.core.execution.base import ExecutionBackend, FillResult

if TYPE_CHECKING:
    from engine.core.cost_model import CostBreakdown
    from engine.core.order_manager import Order

logger = structlog.get_logger()


class LiveBackend(ExecutionBackend):
    """
    Live broker execution.

    Connects to a real broker (Alpaca, IBKR, etc.) and submits orders.
    This is a scaffold — implement the broker-specific logic in a subclass.
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
        self._client = None

    async def connect(self) -> None:
        # TODO: Initialize broker client
        logger.info("live.backend.connected", broker=self.broker_name)

    async def disconnect(self) -> None:
        self._client = None
        logger.info("live.backend.disconnected", broker=self.broker_name)

    async def execute(
        self, order: Order, market_price: float, costs: CostBreakdown  # noqa: ARG002
    ) -> FillResult:
        if self._client is None:
            return FillResult(success=False, reason="Broker client not connected")

        try:
            # TODO: Implement broker-specific order submission

            logger.warning("live.backend.not_implemented", order_id=order.id)
            return FillResult(
                success=False,
                reason="Live execution not yet implemented. Use paper or backtest mode.",
            )

        except Exception as e:
            logger.exception("live.execution_error", order_id=order.id, error=str(e))
            return FillResult(success=False, reason=f"Broker error: {e!s}")
