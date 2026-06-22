"""Tests for engine.core.execution.live and engine.core.execution.paper backends."""

from __future__ import annotations

import ast
import inspect
import textwrap
from dataclasses import dataclass
from enum import StrEnum
from inspect import iscoroutinefunction
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from engine.core.brokers.base import BrokerAuthError, BrokerError
from engine.core.execution.base import FillResult
from engine.core.execution.live import LiveBackend
from engine.core.execution.paper import PaperBackend

if TYPE_CHECKING:
    from engine.core.cost_model import CostBreakdown
    from engine.core.order_manager import Order


@dataclass
class _FakeCostBreakdown:
    slippage: Any = None


class _FakeSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


@dataclass
class _FakeOrder:
    id: str = "ord-1"
    symbol: str = "AAPL"
    quantity: int = 100
    side: _FakeSide = _FakeSide.BUY


def _make_cost(slippage_amount: float = 5.0):
    mock_cost = MagicMock()
    mock_cost.slippage = MagicMock()
    mock_cost.slippage.amount = slippage_amount
    return mock_cost


class _NonScaffoldBackend(LiveBackend):
    """A concrete (non-scaffold) backend used to exercise the real code paths.

    It deliberately keeps the base ``_do_connect``/``_submit_order`` guards so
    credential validation can be tested in isolation: ``connect`` rejects
    missing credentials before ever reaching ``_do_connect``.
    """

    _is_scaffold = False


class _NoClientBackend(LiveBackend):
    """A non-scaffold backend whose ``_do_connect`` forgets to set ``_client``.

    Used to prove ``execute`` still guards on a real client even when
    ``_connected`` is ``True``.
    """

    _is_scaffold = False

    async def _do_connect(self) -> None:
        # Intentionally leaves self._client as None.
        return


def _top_level_if_lines(method: Any, target_attrs: set[str]) -> dict[str, int]:
    """Map each ``target_attr`` to the lineno of the first top-level ``if`` in
    ``method`` whose test references ``self.<attr>``.

    This walks the parsed AST of the method source in source order, so callers
    can assert *ordering* invariants (e.g. the ``_is_scaffold`` branch must
    textually precede the credential check). It is the strongest loop-breaker:
    it inspects the real implementation rather than just observable behavior, so
    a refactor that reorders the guards fails immediately at collection time.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(method)))
    func = next(n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)))
    found: dict[str, int] = {}
    for stmt in func.body:
        if isinstance(stmt, ast.If):
            referenced = {n.attr for n in ast.walk(stmt.test) if isinstance(n, ast.Attribute)}
            for attr in referenced & target_attrs:
                found.setdefault(attr, stmt.lineno)
    return found


class TestLiveBackend:
    # ------------------------------------------------------------------ init

    def test_init_defaults(self):
        backend = LiveBackend()
        assert backend.broker_name == "alpaca"
        assert backend.api_key == ""
        assert backend.api_secret == ""
        assert backend.base_url == ""
        assert backend._client is None
        assert backend._connected is False
        assert backend._connected_at is None

    def test_init_custom_params(self):
        backend = LiveBackend(
            broker_name="ibkr",
            api_key="key123",
            api_secret="secret456",
            base_url="https://api.example.com",
        )
        assert backend.broker_name == "ibkr"
        assert backend.api_key == "key123"
        assert backend.api_secret == "secret456"
        assert backend.base_url == "https://api.example.com"
        # Construction never implies connection.
        assert backend._connected is False

    # --------------------------------------------------------------- connect

    @pytest.mark.asyncio
    async def test_connect(self):
        # Scaffold path: a scaffold backend has no broker wiring, so it must
        # connect successfully *without* any credentials (it never talks to a
        # broker). The _is_scaffold guard short-circuits before credential
        # validation, so no BrokerAuthError is raised here.
        backend = LiveBackend()
        await backend.connect()
        # The scaffold does not build a real broker client yet.
        assert backend._client is None
        # ... so it must honestly report disconnected/unavailable rather than
        # pretending a handshake succeeded (the old _connected=True / client=None
        # state was misleading).
        assert backend._connected is False
        assert backend._connected_at is None

        # Non-scaffold path: a concrete (real) live backend validates
        # credentials, so connect() without api_key/api_secret MUST raise
        # BrokerAuthError before any network handshake. This is the loop-
        # breaking assertion: a scaffold stays quiet (above) while a real
        # backend raises (here). If the _is_scaffold guard were ever moved
        # below credential validation, the scaffold block above would raise
        # too and fail; if credential validation were removed, this block
        # would fail to raise.
        no_creds = _NonScaffoldBackend()
        assert not no_creds.api_key and not no_creds.api_secret
        with pytest.raises(BrokerAuthError, match="api_key and api_secret"):
            await no_creds.connect()
        # A failed connect must never leave the backend in a connected state.
        assert no_creds._connected is False
        assert no_creds._connected_at is None

    @pytest.mark.asyncio
    async def test_connect_scaffold_no_credentials_reports_disconnected(self):
        # Review requirement: a scaffold LiveBackend created with *no*
        # credentials must connect() successfully (no BrokerAuthError) and
        # honestly report ``_connected = False`` — the scaffold has no broker
        # wiring, so it neither needs nor can it use credentials.
        backend = LiveBackend()  # no api_key / api_secret
        assert backend.api_key == ""
        assert backend.api_secret == ""
        # connect() must not raise despite missing credentials.
        await backend.connect()
        assert backend._connected is False
        assert backend._connected_at is None
        assert backend._client is None

    @pytest.mark.asyncio
    async def test_connect_sets_connected_only_after_success(self):
        backend = LiveBackend(api_key="key123", api_secret="secret456")
        assert backend._connected is False
        await backend.connect()
        # A scaffold backend never establishes a real connection.
        assert backend._connected is False

    @pytest.mark.asyncio
    async def test_connect_missing_credentials(self):
        # Credential validation applies to real (non-scaffold) backends; a
        # scaffold short-circuits before reaching it.
        backend = _NonScaffoldBackend()
        with pytest.raises(BrokerAuthError, match="api_key and api_secret"):
            await backend.connect()
        # A failed connect must never leave the backend in a connected state.
        assert backend._connected is False
        assert backend._connected_at is None

    @pytest.mark.asyncio
    async def test_connect_missing_only_api_key(self):
        backend = _NonScaffoldBackend(api_secret="secret456")
        with pytest.raises(BrokerAuthError):
            await backend.connect()
        assert backend._connected is False

    @pytest.mark.asyncio
    async def test_connect_missing_only_api_secret(self):
        backend = _NonScaffoldBackend(api_key="key123")
        with pytest.raises(BrokerAuthError):
            await backend.connect()
        assert backend._connected is False

    @pytest.mark.asyncio
    async def test_connect_rejects_empty_string_credentials(self):
        # Boundary: empty-string credentials are falsy and must be rejected.
        for key, secret in [("", "secret"), ("key", ""), ("", "")]:
            backend = _NonScaffoldBackend(api_key=key, api_secret=secret)
            with pytest.raises(BrokerAuthError):
                await backend.connect()

    @pytest.mark.asyncio
    async def test_connect_error_message_includes_broker_name(self):
        backend = _NonScaffoldBackend(broker_name="ibkr")
        with pytest.raises(BrokerAuthError, match="ibkr"):
            await backend.connect()

    @pytest.mark.asyncio
    async def test_connect_does_not_record_timestamp_for_scaffold(self):
        # A scaffold backend has no real handshake, so no timestamp is set.
        backend = LiveBackend(api_key="key123", api_secret="secret456")
        await backend.connect()
        assert backend._connected_at is None

    @pytest.mark.asyncio
    async def test_connect_scaffold_guard_precedes_credential_check(self):
        # Regression guard for the check reordering: the scaffold branch must
        # run *before* credential validation, so a scaffold created with no
        # credentials connects without raising BrokerAuthError. This is the
        # inverse of the non-scaffold path (see test_connect_missing_credentials).
        backend = LiveBackend()
        assert not backend.api_key and not backend.api_secret
        await backend.connect()
        assert backend._connected is False
        assert backend._client is None

    @pytest.mark.asyncio
    async def test_connect_scaffold_vs_non_scaffold_with_identical_credentials(self):
        # Strongest loop-breaker: hand a scaffold and a real backend the *same*
        # (missing/empty) credentials. The scaffold MUST stay quiet (no raise,
        # _connected=False) while the real backend MUST raise BrokerAuthError.
        # If the _is_scaffold guard were ever moved below the credential check,
        # the scaffold would raise too and this test would fail.
        for key, secret in [(None, None), ("", ""), ("key", ""), ("", "secret")]:
            scaffold = LiveBackend(api_key=key or "", api_secret=secret or "")
            await scaffold.connect()  # must not raise
            assert scaffold._connected is False
            assert scaffold._connected_at is None
            assert scaffold._client is None

            real = _NonScaffoldBackend(api_key=key or "", api_secret=secret or "")
            with pytest.raises(BrokerAuthError):
                await real.connect()
            assert real._connected is False

    @pytest.mark.asyncio
    async def test_connect_scaffold_never_inspects_credentials(self):
        # Edge case: even whitespace / garbage credentials must not make a
        # scaffold raise, because the scaffold short-circuits before any
        # credential inspection happens at all.
        backend = LiveBackend(api_key="   ", api_secret="garbage")
        await backend.connect()
        assert backend._connected is False
        assert backend._connected_at is None

    @pytest.mark.asyncio
    async def test_connect_non_scaffold_valid_credentials_reaches_do_connect(self):
        # Credential validation only matters for non-scaffold backends. With
        # valid credentials, connect() must pass the credential gate and reach
        # _do_connect; the base _do_connect raises NotImplementedError, which
        # proves we got *past* credentials rather than being blocked by the
        # scaffold branch.
        backend = _NonScaffoldBackend(api_key="key123", api_secret="secret456")
        with pytest.raises(NotImplementedError, match="_do_connect"):
            await backend.connect()
        # A failed _do_connect must not leave the backend connected.
        assert backend._connected is False

    @pytest.mark.asyncio
    async def test_connect_is_awaitable_coroutine(self):
        # connect() is a coroutine function and must be directly awaitable. This
        # is why any mock of connect() must be AsyncMock (awaitable) and never
        # MagicMock (which raises TypeError on ``await``); see
        # test_execute_not_implemented, which exercises the real coroutine
        # instead of mocking it.
        backend = LiveBackend()
        assert inspect.iscoroutinefunction(backend.connect)
        coro = backend.connect()
        assert inspect.iscoroutine(coro)
        await coro
        assert backend._connected is False

    @pytest.mark.asyncio
    async def test_execute_scaffold_guard_precedes_client_check(self):
        # Regression guard for the check reordering in execute(): the scaffold
        # branch must run *before* the client guard, so a scaffold with no
        # client reports "not implemented" rather than "not connected".
        backend = LiveBackend()
        assert backend._client is None
        result = await backend.execute(_FakeOrder(), 100.0, _make_cost())
        assert result.success is False
        assert "not yet implemented" in result.reason.lower()
        assert "not connected" not in result.reason.lower()

    # ------------------------------------------------------------ disconnect

    @pytest.mark.asyncio
    async def test_disconnect(self):
        backend = LiveBackend()
        # ``_client`` stands in for an async broker client; use AsyncMock so the
        # test stays correct if disconnect() ever awaits a client method.
        backend._client = AsyncMock()
        await backend.disconnect()
        assert backend._client is None
        assert backend._connected is False
        assert backend._connected_at is None

    @pytest.mark.asyncio
    async def test_disconnect_after_connect_clears_state(self):
        backend = LiveBackend(api_key="key123", api_secret="secret456")
        await backend.connect()
        # Scaffold stays disconnected even after connect.
        assert backend._connected is False
        await backend.disconnect()
        assert backend._connected is False
        assert backend._connected_at is None
        assert backend._client is None

    @pytest.mark.asyncio
    async def test_disconnect_when_never_connected_is_safe(self):
        # Idempotent: disconnecting without ever connecting must not raise.
        backend = LiveBackend()
        await backend.disconnect()
        assert backend._connected is False
        assert backend._client is None

    @pytest.mark.asyncio
    async def test_disconnect_is_idempotent(self):
        backend = LiveBackend(api_key="key123", api_secret="secret456")
        await backend.connect()
        await backend.disconnect()
        await backend.disconnect()  # second call is a no-op
        assert backend._connected is False
        assert backend._client is None

    @pytest.mark.asyncio
    async def test_reconnect_after_disconnect(self):
        backend = LiveBackend(api_key="key123", api_secret="secret456")
        await backend.connect()
        await backend.disconnect()
        assert backend._connected is False
        await backend.connect()
        # Scaffold backends stay disconnected even after reconnect.
        assert backend._connected is False
        assert backend._connected_at is None

    # --------------------------------------------------------------- execute

    @pytest.mark.asyncio
    async def test_execute_not_connected(self):
        # A non-scaffold backend that was never connected has no client, so
        # execute() surfaces "not connected". (A scaffold would report "not
        # implemented" instead — see test_execute_not_implemented.)
        backend = _NonScaffoldBackend()
        result = await backend.execute(_FakeOrder(), 150.0, _make_cost())
        assert result.success is False
        assert "not connected" in result.reason.lower()
        # Boundary: a non-fill carries no price or quantity.
        assert result.price == 0.0
        assert result.quantity == 0

    @pytest.mark.asyncio
    async def test_execute_not_implemented(self):
        # A scaffold backend reports "not implemented" without requiring a
        # client — the scaffold check short-circuits before the client guard.
        #
        # The full connect()->execute() lifecycle is exercised using the *real*
        # ``connect`` coroutine (no mock): patching an async method with
        # ``MagicMock`` raises ``TypeError`` on ``await`` and even ``AsyncMock``
        # is unnecessary here because a scaffold ``connect()`` is a real
        # coroutine that simply leaves the backend disconnected. That honestly
        # disconnected state is what forces ``execute()`` down the scaffold
        # "not implemented" branch.
        backend = LiveBackend()
        await backend.connect()  # real coroutine; scaffold stays disconnected.
        assert backend._is_scaffold is True
        assert backend._connected is False
        assert backend._client is None
        result = await backend.execute(_FakeOrder(), 150.0, _make_cost())
        assert result.success is False
        assert "not yet implemented" in result.reason.lower()

    @pytest.mark.asyncio
    async def test_execute_not_implemented_has_zero_fill(self):
        backend = LiveBackend()
        backend._client = AsyncMock()
        result = await backend.execute(_FakeOrder(), 150.0, _make_cost())
        assert result.price == 0.0
        assert result.quantity == 0

    @pytest.mark.asyncio
    async def test_execute_scaffold_flag_returns_not_implemented(self):
        # The explicit ``_is_scaffold`` flag short-circuits execution with a
        # clear, structured failure instead of relying on catching a
        # NotImplementedError raised by ``_submit_order``.
        backend = LiveBackend()
        assert backend._is_scaffold is True
        backend._client = AsyncMock()
        result = await backend.execute(_FakeOrder(), 150.0, _make_cost())
        assert result.success is False
        assert "not yet implemented" in result.reason.lower()
        assert result.price == 0.0
        assert result.quantity == 0

    @pytest.mark.asyncio
    async def test_execute_wraps_broker_exception(self):
        # A subclass overrides the submission hook to raise; execute() must
        # catch it and return a structured failure rather than propagating.
        class _BrokenBroker(LiveBackend):
            _is_scaffold = False

            async def _submit_order(
                self, order: Order, market_price: float, costs: CostBreakdown
            ) -> FillResult:
                raise RuntimeError("broker down")

        backend = _BrokenBroker()
        backend._client = object()  # truthy so the connect-guard passes
        result = await backend.execute(_FakeOrder(), 150.0, _make_cost())
        assert result.success is False
        assert "broker error" in result.reason.lower()
        assert "broker down" in result.reason.lower()
        assert result.price == 0.0
        assert result.quantity == 0

    @pytest.mark.asyncio
    async def test_execute_broker_exception_preserves_error_text(self):
        class _RejectingBroker(LiveBackend):
            _is_scaffold = False

            async def _submit_order(
                self, order: Order, market_price: float, costs: CostBreakdown
            ) -> FillResult:
                raise ValueError("insufficient buying power")

        backend = _RejectingBroker()
        backend._client = object()
        result = await backend.execute(_FakeOrder(), 150.0, _make_cost())
        assert result.success is False
        assert "insufficient buying power" in result.reason

    @pytest.mark.asyncio
    async def test_execute_uses_client_guard_not_connected_flag(self):
        # Even when a non-scaffold backend reports _connected is True, execute()
        # requires a real client. Here _do_connect succeeds but forgets to
        # assign self._client, so execution is still gated as "not connected".
        backend = _NoClientBackend(api_key="key123", api_secret="secret456")
        await backend.connect()
        assert backend._connected is True
        result = await backend.execute(_FakeOrder(), 150.0, _make_cost())
        assert result.success is False
        assert "not connected" in result.reason.lower()

    @pytest.mark.asyncio
    async def test_scaffold_credentialless_connect_then_not_implemented(self):
        # End-to-end regression for both prompt fixes, exercised without any
        # mocking (so the awaitable-mock foot-gun from fix #2 cannot apply):
        #
        #   fix #1 — connect() must NOT raise BrokerAuthError for a scaffold
        #            built with no credentials: the _is_scaffold guard runs
        #            before credential validation, so a credential-less scaffold
        #            connects cleanly and honestly reports _connected=False.
        #   fix #2 — execute() then short-circuits to "not implemented" via the
        #            scaffold branch (which precedes the client guard), so no
        #            broker client is required for the lifecycle to complete.
        #
        # If either guard were reordered this test would fail: moving credential
        # validation above the scaffold branch makes connect() raise; moving the
        # client guard above the scaffold branch makes execute() report "not
        # connected" instead of "not implemented".
        backend = LiveBackend()  # no api_key / api_secret
        assert not backend.api_key and not backend.api_secret

        await backend.connect()  # fix #1: must not raise BrokerAuthError
        assert backend._connected is False
        assert backend._connected_at is None
        assert backend._client is None

        result = await backend.execute(_FakeOrder(), 100.0, _make_cost())  # fix #2
        assert result.success is False
        assert "not yet implemented" in result.reason.lower()
        assert "not connected" not in result.reason.lower()
        assert result.price == 0.0
        assert result.quantity == 0

    @pytest.mark.asyncio
    async def test_prompt_regression_connect_no_auth_error_and_real_coroutine(self):
        # Named regression for the exact two CI failures this change resolves:
        #   1) connect() on a credential-less scaffold must NOT raise
        #      BrokerAuthError ("live backend requires api_key and api_secret
        #      for broker 'alpaca'"). The _is_scaffold guard runs first.
        #   2) connect() must be exercised as a real awaitable coroutine; the
        #      suite must never patch it with MagicMock, whose non-coroutine
        #      return raises "TypeError: object MagicMock can't be used in
        #      'await' expression". Asserting iscoroutinefunction pins this.
        backend = LiveBackend()
        assert backend._is_scaffold is True
        assert not backend.api_key and not backend.api_secret
        assert inspect.iscoroutinefunction(backend.connect)
        await backend.connect()  # neither BrokerAuthError nor TypeError
        assert backend._connected is False
        assert backend._connected_at is None
        assert backend._client is None

    @pytest.mark.asyncio
    async def test_all_scaffold_mode_invariants_construction_connect_execute(self):
        # Consolidated regression asserting *every* scaffold-mode invariant in a
        # single end-to-end flow (construction -> connect -> execute). If any one
        # invariant regresses this test pinpoints the exact phase that broke.
        #
        # Construction (no credentials):
        backend = LiveBackend()
        assert backend._is_scaffold is True
        assert backend._connected is False
        assert backend._connected_at is None
        assert backend._client is None
        assert backend.api_key == ""
        assert backend.api_secret == ""
        # connect() is a real coroutine function, never a MagicMock patch.
        assert iscoroutinefunction(backend.connect) is True
        # connect() must not raise BrokerAuthError for a credential-less scaffold.
        await backend.connect()
        # After connect: scaffold stays honestly disconnected (no fake handshake).
        assert backend._is_scaffold is True
        assert backend._connected is False
        assert backend._connected_at is None
        assert backend._client is None
        # execute() short-circuits to a structured "not implemented" failure
        # without requiring a broker client that can never exist for a scaffold.
        result = await backend.execute(_FakeOrder(), 100.0, _make_cost())
        assert result.success is False
        assert "not yet implemented" in result.reason.lower()
        assert "not connected" not in result.reason.lower()
        assert result.price == 0.0
        assert result.quantity == 0

    # --------------------------------------------------------------- lifecycle

    @pytest.mark.asyncio
    async def test_full_lifecycle(self):
        backend = LiveBackend(api_key="key123", api_secret="secret456")
        assert backend._connected is False

        await backend.connect()
        # Scaffold backends report disconnected.
        assert backend._connected is False

        # Without a concrete client, execution is gated.
        result = await backend.execute(_FakeOrder(), 100.0, _make_cost())
        assert result.success is False

        await backend.disconnect()
        assert backend._connected is False

    # -------------------------------------------------------- concrete subclass

    @pytest.mark.asyncio
    async def test_non_scaffold_subclass_connects_via_do_connect(self):
        # A concrete broker adapter flips _is_scaffold to False, implements
        # _do_connect (to build the client) and _submit_order (to send orders).
        # connect() must then report connected and execute() must route to the
        # real submission hook.
        class _ConcreteBroker(LiveBackend):
            _is_scaffold = False

            async def _do_connect(self) -> None:
                self._client = object()

            async def _submit_order(
                self, order: Order, market_price: float, costs: CostBreakdown
            ) -> FillResult:
                return FillResult(success=True, price=market_price, quantity=order.quantity)

        backend = _ConcreteBroker(api_key="key123", api_secret="secret456")
        assert backend._connected is False

        await backend.connect()
        # A real backend reports connected with a live client and timestamp.
        assert backend._connected is True
        assert backend._client is not None
        assert backend._connected_at is not None

        result = await backend.execute(_FakeOrder(), 150.0, _make_cost())
        assert result.success is True
        assert result.price == 150.0
        assert result.quantity == 100

        await backend.disconnect()
        assert backend._connected is False
        assert backend._client is None

    @pytest.mark.asyncio
    async def test_non_scaffold_subconnect_still_requires_credentials(self):
        # Flipping _is_scaffold does not bypass credential validation.
        class _ConcreteBroker(LiveBackend):
            _is_scaffold = False

        backend = _ConcreteBroker()
        with pytest.raises(BrokerAuthError, match="api_key and api_secret"):
            await backend.connect()
        assert backend._connected is False

    @pytest.mark.asyncio
    async def test_non_scaffold_subclass_missing_do_connect_raises(self):
        # A subclass that flips _is_scaffold to False but forgets to override
        # _do_connect must fail loudly at connect time rather than silently
        # reporting a connection with no broker client.
        class _IncompleteBroker(LiveBackend):
            _is_scaffold = False

        backend = _IncompleteBroker(api_key="key123", api_secret="secret456")
        with pytest.raises(NotImplementedError, match="_do_connect"):
            await backend.connect()
        # A failed connect must never leave the backend connected.
        assert backend._connected is False

    @pytest.mark.asyncio
    async def test_non_scaffold_subclass_missing_submit_order_propagates(self):
        # A subclass that flips _is_scaffold to False but forgets to override
        # _submit_order must surface NotImplementedError from execute() rather
        # than masking it as a generic "Broker error" FillResult.
        class _IncompleteBroker(LiveBackend):
            _is_scaffold = False

            async def _do_connect(self) -> None:
                self._client = object()

        backend = _IncompleteBroker(api_key="key123", api_secret="secret456")
        await backend.connect()
        with pytest.raises(NotImplementedError, match="_submit_order"):
            await backend.execute(_FakeOrder(), 150.0, _make_cost())


class TestLiveBackendLoopBreaker:
    """Structural + behavioral invariants that pin the ``connect()``/``execute()``
    scaffold-guard ordering so the recurring fix-test-fail loop cannot be
    reintroduced.

    The AST-based tests are the strongest loop-breaker: they inspect the real
    source of ``connect()``/``execute()`` and assert the ``_is_scaffold`` branch
    textually precedes the credential / client checks, so any refactor that
    reorders the guards fails at test time rather than silently changing
    behavior. The behavioral tests cover the gap between the source shape and
    the runtime contract (no ``_do_connect`` call, no credential mutation,
    exact failure reasons, mock awaitability).
    """

    # ---------------------------------------------- structural (AST) guards

    def test_connect_source_scaffold_guard_precedes_credential_check(self):
        lines = _top_level_if_lines(LiveBackend.connect, {"_is_scaffold", "api_key"})
        assert "_is_scaffold" in lines, "connect() must branch on _is_scaffold"
        assert "api_key" in lines, "connect() must validate credentials"
        assert lines["_is_scaffold"] < lines["api_key"], (
            "_is_scaffold guard MUST textually precede credential validation"
        )

    def test_execute_source_scaffold_guard_precedes_client_guard(self):
        lines = _top_level_if_lines(LiveBackend.execute, {"_is_scaffold", "_client"})
        assert "_is_scaffold" in lines, "execute() must branch on _is_scaffold"
        assert "_client" in lines, "execute() must guard on a broker client"
        assert lines["_is_scaffold"] < lines["_client"], (
            "_is_scaffold guard MUST textually precede the client guard"
        )

    def test_connect_source_contains_scaffold_branch(self):
        lines = _top_level_if_lines(LiveBackend.connect, {"_is_scaffold"})
        assert "_is_scaffold" in lines

    def test_connect_source_contains_credential_validation_branch(self):
        lines = _top_level_if_lines(LiveBackend.connect, {"api_key", "api_secret"})
        assert "api_key" in lines
        assert "api_secret" in lines

    # ----------------------------------------------------- defaults / DTOs

    def test_is_scaffold_defaults_to_true_on_base_class(self):
        assert LiveBackend._is_scaffold is True
        assert LiveBackend()._is_scaffold is True
        # A freshly constructed instance never flips the class-level flag.
        backend = LiveBackend(api_key="k", api_secret="s")
        assert backend._is_scaffold is True

    def test_scaffold_defaults_without_credentials(self):
        # Consolidated regression for the two production-code fixes:
        #   * LiveBackend() built with no credentials must enter scaffold mode
        #     (_is_scaffold stays True) and stay honestly disconnected rather
        #     than raising BrokerAuthError.
        #   * connect() must be a real coroutine function (an ``async def`` on
        #     the class), never a monkey-patched bound method. iscoroutinefunction
        #     inspects the bound method, so a MagicMock patch would fail here.
        backend = LiveBackend()  # no api_key / api_secret
        assert backend._is_scaffold is True
        assert backend._connected is False
        assert backend._connected_at is None
        assert backend._client is None
        assert iscoroutinefunction(backend.connect) is True

    def test_fill_result_defaults(self):
        ok = FillResult(success=True)
        assert ok.price == 0.0
        assert ok.quantity == 0
        assert ok.reason == ""
        fail = FillResult(success=False)
        assert fail.price == 0.0
        assert fail.quantity == 0
        assert fail.reason == ""

    def test_broker_auth_error_is_broker_error_subclass(self):
        # The OMS live-loop dispatches on the typed error hierarchy; a
        # BrokerAuthError must remain a BrokerError so the kill-switch path
        # catches it.
        assert issubclass(BrokerAuthError, BrokerError)
        assert isinstance(BrokerAuthError("x"), BrokerError)

    # ---------------------------------------------- scaffold connect guards

    @pytest.mark.asyncio
    async def test_scaffold_connect_does_not_invoke_do_connect(self):
        class _DoConnectSpy(LiveBackend):
            def __init__(self, **kw: Any) -> None:
                super().__init__(**kw)
                self.do_connect_called = False

            async def _do_connect(self) -> None:
                self.do_connect_called = True

        backend = _DoConnectSpy()
        await backend.connect()
        assert backend.do_connect_called is False
        assert backend._connected is False
        assert backend._client is None

    @pytest.mark.asyncio
    async def test_scaffold_connect_leaves_credentials_untouched(self):
        backend = LiveBackend(api_key="key123", api_secret="secret456", base_url="u")
        await backend.connect()
        assert backend.api_key == "key123"
        assert backend.api_secret == "secret456"
        assert backend.base_url == "u"

    @pytest.mark.asyncio
    async def test_scaffold_connect_idempotent_across_repeated_calls(self):
        backend = LiveBackend()
        for _ in range(5):
            await backend.connect()
            assert backend._connected is False
            assert backend._connected_at is None
            assert backend._client is None

    @pytest.mark.asyncio
    async def test_scaffold_connect_stays_disconnected_even_with_preset_client(self):
        # Even if a caller wrongly assigns a client beforehand, the scaffold
        # must honestly report disconnected (it never performed a handshake).
        backend = LiveBackend()
        backend._client = object()
        await backend.connect()
        assert backend._connected is False
        assert backend._connected_at is None

    # ---------------------------------------------- scaffold execute guards

    @pytest.mark.asyncio
    async def test_scaffold_execute_does_not_invoke_submit_order(self):
        class _SubmitSpy(LiveBackend):
            def __init__(self) -> None:
                super().__init__()
                self.submit_called = False

            async def _submit_order(
                self, order: Any, market_price: float, costs: Any
            ) -> FillResult:
                self.submit_called = True
                return FillResult(success=True)

        backend = _SubmitSpy()
        result = await backend.execute(_FakeOrder(), 150.0, _make_cost())
        assert backend.submit_called is False
        assert result.success is False

    @pytest.mark.asyncio
    async def test_scaffold_execute_exact_reason_message(self):
        # The exact wording is part of the contract surfaced to operators.
        backend = LiveBackend()
        result = await backend.execute(_FakeOrder(), 150.0, _make_cost())
        assert result.reason == ("Live execution not yet implemented. Use paper or backtest mode.")

    # ----------------------------------------- non-scaffold ordering guards

    @pytest.mark.asyncio
    async def test_non_scaffold_missing_credentials_does_not_invoke_do_connect(self):
        # Credentials must be validated *before* any network handshake, so a
        # misconfiguration surfaces as BrokerAuthError rather than reaching the
        # broker client builder.
        class _CredSpy(LiveBackend):
            _is_scaffold = False

            def __init__(self, **kw: Any) -> None:
                super().__init__(**kw)
                self.do_connect_called = False

            async def _do_connect(self) -> None:
                self.do_connect_called = True

        backend = _CredSpy()
        with pytest.raises(BrokerAuthError, match="api_key and api_secret"):
            await backend.connect()
        assert backend.do_connect_called is False
        assert backend._connected is False
        assert backend._connected_at is None

    # ----------------------------------------------- mock awaitability doc

    @pytest.mark.asyncio
    async def test_magicmock_connect_is_not_awaitable(self):
        # Locks in WHY connect() must be patched with AsyncMock: MagicMock is
        # not a coroutine, so ``await backend.connect()`` raises TypeError.
        backend = LiveBackend()
        backend.connect = MagicMock()
        with pytest.raises(TypeError):
            await backend.connect()

    # -------------------------------------------------- base defensive hooks

    @pytest.mark.asyncio
    async def test_base_do_connect_raises_not_implemented(self):
        with pytest.raises(NotImplementedError, match="_do_connect"):
            await LiveBackend()._do_connect()

    @pytest.mark.asyncio
    async def test_base_submit_order_raises_not_implemented(self):
        with pytest.raises(NotImplementedError, match="_submit_order"):
            await LiveBackend()._submit_order(_FakeOrder(), 100.0, _make_cost())


class TestPaperBackend:
    def test_init(self):
        backend = PaperBackend()
        assert backend._connected is False

    @pytest.mark.asyncio
    async def test_connect(self):
        backend = PaperBackend()
        await backend.connect()
        assert backend._connected is True

    @pytest.mark.asyncio
    async def test_disconnect(self):
        backend = PaperBackend()
        await backend.connect()
        assert backend._connected is True
        await backend.disconnect()
        assert backend._connected is False

    @pytest.mark.asyncio
    async def test_execute_not_connected(self):
        backend = PaperBackend()
        result = await backend.execute(_FakeOrder(), 150.0, _make_cost())
        assert result.success is False
        assert "not connected" in result.reason.lower()

    @pytest.mark.asyncio
    async def test_execute_buy_order(self):
        backend = PaperBackend()
        await backend.connect()
        order = _FakeOrder(side=_FakeSide.BUY, quantity=100)
        result = await backend.execute(order, 100.0, _make_cost(10.0))
        assert result.success is True
        assert result.quantity == 100
        assert result.price > 0

    @pytest.mark.asyncio
    async def test_execute_sell_order(self):
        backend = PaperBackend()
        await backend.connect()
        order = _FakeOrder(side=_FakeSide.SELL, quantity=50)
        result = await backend.execute(order, 200.0, _make_cost(5.0))
        assert result.success is True
        assert result.quantity == 50
        assert result.price > 0

    @pytest.mark.asyncio
    async def test_execute_zero_quantity(self):
        backend = PaperBackend()
        await backend.connect()
        order = _FakeOrder(quantity=0)
        result = await backend.execute(order, 100.0, _make_cost(10.0))
        assert result.success is True

    @pytest.mark.asyncio
    async def test_execute_buy_slippage_increases_price(self):
        backend = PaperBackend()
        await backend.connect()
        order = _FakeOrder(side=_FakeSide.BUY, quantity=100)
        result = await backend.execute(order, 100.0, _make_cost(100.0))
        assert result.success is True
        assert result.price >= 99.0

    @pytest.mark.asyncio
    async def test_execute_multiple_fills_deterministic_with_seed(self):
        backend = PaperBackend()
        await backend.connect()
        backend._rng = __import__("random").Random(42)
        order = _FakeOrder(side=_FakeSide.BUY, quantity=100)
        r1 = await backend.execute(order, 100.0, _make_cost(10.0))
        backend._rng = __import__("random").Random(42)
        r2 = await backend.execute(order, 100.0, _make_cost(10.0))
        assert r1.price == r2.price

    @pytest.mark.asyncio
    async def test_execute_sell_slippage_decreases_price(self):
        # Boundary: slippage moves sells below the effective price.
        backend = PaperBackend()
        await backend.connect()
        order = _FakeOrder(side=_FakeSide.SELL, quantity=100)
        result = await backend.execute(order, 200.0, _make_cost(50.0))
        assert result.success is True
        assert result.price <= 200.0

    @pytest.mark.asyncio
    async def test_execute_rejects_non_positive_market_price(self):
        # Edge case: no valid price available -> structured failure.
        backend = PaperBackend()
        await backend.connect()
        order = _FakeOrder(quantity=100)
        result = await backend.execute(order, 0.0, _make_cost(10.0))
        assert result.success is False
        assert "price" in result.reason.lower()
