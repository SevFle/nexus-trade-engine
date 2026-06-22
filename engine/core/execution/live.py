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
    The base class is a **scaffold**: it tracks connection state but does
    **not** talk to any broker and does **not** validate credentials (it has
    no broker wiring, so credentials would be neither consumed nor checked).
    Credential validation only kicks in for concrete (non-scaffold) subclasses
    that flip ``_is_scaffold`` to ``False``.

    To wire up a concrete broker, subclass and:

    1. Set ``_is_scaffold = False``.
    2. Override :meth:`_do_connect` to build the broker client
       (assign it to ``self._client``) and perform any handshake.
    3. Override :meth:`_submit_order` to translate an internal order into a
       broker-specific request and return the resulting :class:`FillResult`.
    """

    #: When ``True`` the backend has no real broker wiring. Subclasses flip
    #: this to ``False`` once they implement ``_do_connect`` and
    #: ``_submit_order``. The flag replaces the old pattern of letting
    #: ``_submit_order`` raise ``NotImplementedError`` and catching it in
    #: ``execute``.
    _is_scaffold: bool = True

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

    def _reset_connection_state(self) -> None:
        """Force the backend into the honestly-disconnected state.

        Centralised so the scaffold early-return and every error path share a
        single definition of "disconnected", preventing the state-inconsistency
        bugs (``_connected = True`` with ``_client = None``) that the recurring
        test loop was about.
        """
        self._connected = False
        self._connected_at = None
        self._client = None

    async def connect(self) -> None:
        # INVARIANT: the ``_is_scaffold`` guard MUST run before any credential
        # validation. A scaffold has no broker wiring, so it can neither use nor
        # validate credentials. Checking credentials first would make a bare
        # scaffold raise BrokerAuthError even though it never talks to a broker.
        if self._is_scaffold:
            # The base class has no real broker wiring, so it can neither use
            # credentials nor build a client. Rather than require credentials
            # it will never consume (or pretend a connection exists with
            # ``_connected = True`` / ``_client = None``), stay honestly
            # disconnected without touching credentials at all. ``execute``
            # surfaces a clear "not implemented" message.
            logger.warning(
                "live.backend.scaffold_mode",
                broker=self.broker_name,
                msg="no broker client; staying disconnected",
            )
            # A scaffold must NOT clear _client here — a caller may have preset
            # it. We only ensure the connection *flags* are honest. Use the
            # scoped reset that clears flags but preserves any preset client so
            # the scaffold path is a pure no-op on credentials/client wiring.
            self._connected = False
            self._connected_at = None
            return

        # Real backends validate credentials *before* attempting any network
        # work so a misconfiguration surfaces as BrokerAuthError rather than a
        # noisy connection failure deep inside the broker client.
        if not self.api_key or not self.api_secret:
            self._reset_connection_state()
            raise BrokerAuthError(
                f"live backend requires api_key and api_secret for broker '{self.broker_name}'"
            )

        # Concrete subclasses build the broker client inside ``_do_connect``.
        await self._do_connect()
        self._connected = True
        self._connected_at = time.monotonic()
        logger.info("live.backend.connected", broker=self.broker_name)

    async def _do_connect(self) -> None:
        """Construct and validate the concrete broker client.

        Concrete subclasses override this to build a real broker client
        (e.g. ``self._client = alpaca.REST(...)``) and perform any
        connection handshake. It is only invoked when ``_is_scaffold`` is
        ``False``.

        The base implementation raises :class:`NotImplementedError` so a
        subclass that flips ``_is_scaffold`` to ``False`` without overriding
        this hook fails loudly at connect time, rather than silently claiming
        a connection while leaving ``self._client`` unset. This mirrors the
        defensive guard in :meth:`_submit_order`.
        """
        raise NotImplementedError("_do_connect must be overridden when _is_scaffold is False.")

    async def disconnect(self) -> None:
        # Idempotent: safe to call when never connected or already disconnected.
        self._reset_connection_state()
        logger.info("live.backend.disconnected", broker=self.broker_name)

    async def execute(self, order: Order, market_price: float, costs: CostBreakdown) -> FillResult:
        if self._is_scaffold:
            # No broker wiring: short-circuit with a structured failure. This
            # check runs *before* the client guard so a scaffold reports a
            # clear "not implemented" without requiring a client that can
            # never exist (a scaffold never establishes one).
            logger.warning("live.backend.not_implemented", order_id=order.id)
            return FillResult(
                success=False,
                reason="Live execution not yet implemented. Use paper or backtest mode.",
            )

        if self._client is None:
            return FillResult(success=False, reason="Broker client not connected")

        try:
            return await self._submit_order(order, market_price, costs)
        except NotImplementedError:
            # A subclass flipped _is_scaffold to False without overriding
            # _submit_order. Surface the programming error instead of masking
            # it as a generic broker failure (the generic handler below is only
            # meant for transient/runtime broker errors).
            raise
        except Exception as e:
            logger.exception("live.execution_error", order_id=order.id, error=str(e))
            return FillResult(success=False, reason=f"Broker error: {e!s}")

    async def _submit_order(
        self, order: Order, market_price: float, costs: CostBreakdown
    ) -> FillResult:
        """Submit a single order to the broker.

        Concrete broker adapters override this to translate the internal
        order into a broker-specific request and return the resulting
        :class:`FillResult`. It is only reached when ``_is_scaffold`` is
        ``False``.

        The base body is a defensive guard — :meth:`execute` gates on the
        ``_is_scaffold`` flag *before* calling this, so the guard is never
        reached during normal operation. It exists solely to fail loudly if a
        subclass flips ``_is_scaffold`` to ``False`` without overriding the
        hook (unlike the old design, ``execute`` no longer *catches* this to
        detect scaffold mode).
        """
        raise NotImplementedError("_submit_order must be overridden when _is_scaffold is False.")
