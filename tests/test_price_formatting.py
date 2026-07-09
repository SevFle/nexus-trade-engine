"""Tests for order-body price/quantity serialization.

Targeted code: ``engine.execution.live_backend._format_price`` /
``_format_qty`` — the single source of truth for how ``limit_price`` and
``qty`` are turned into the numeric strings Alpaca's REST API receives.

These tests lock in the regression where a ``Decimal`` limit price carried
its insignificant trailing zeros onto the wire (``Decimal("330.50")`` was
serialised as ``"330.50"`` instead of ``"330.5"``), which is a formatting
mismatch with Alpaca's canonical decimal form and with the ``float`` path.

Coverage:
- ``_format_price`` direct unit tests (parametrised over the regression +
  whole numbers / sub-cent values / large values / float / int / scientific
  notation avoidance).
- ``_format_qty`` direct unit tests (Decimal / int / float / trailing zeros).
- Round-trip: every output of ``_format_price`` is parsed back to the same
  numeric value, proving we only strip *cosmetic* zeros, never precision.
- Integration: ``LiveExecutionBackend.submit_order`` and
  ``AlpacaBrokerAdapter.submit_order`` emit the exact canonical
  ``limit_price`` string on the wire for trailing-zero inputs.
- ``market`` orders never serialise a ``limit_price`` at all.
"""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from engine.brokers.alpaca import AlpacaBrokerAdapter
from engine.execution.live_backend import (
    LiveExecutionBackend,
    _format_price,
    _format_qty,
)

PAPER_URL = "https://paper-api.alpaca.markets"


# ---------------------------------------------------------------------------
# _format_price — direct unit tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("price", "expected"),
    [
        # ---- the regression: trailing zeros must be stripped ----
        (Decimal("330.50"), "330.5"),
        (Decimal("330.00"), "330"),
        (Decimal("0.0"), "0"),
        (Decimal("0.00"), "0"),
        (Decimal("330.050"), "330.05"),
        (Decimal("100.00"), "100"),
        # ---- already-canonical decimals pass through unchanged ----
        (Decimal("330.5"), "330.5"),
        (Decimal("330.55"), "330.55"),
        (Decimal("5"), "5"),
        (Decimal("100"), "100"),
        (Decimal("0"), "0"),
        # ---- sub-cent / fractional precision preserved ----
        (Decimal("0.0001"), "0.0001"),
        (Decimal("0.123456"), "0.123456"),
        (Decimal("1.0001"), "1.0001"),
        # ---- large whole values never degrade to scientific notation ----
        (Decimal("1000000"), "1000000"),
        (Decimal("1000000.00"), "1000000"),
        (Decimal("99999999"), "99999999"),
        # ---- a very small but significant number stays in plain form ----
        (Decimal("0.000001"), "0.000001"),
    ],
)
def test_format_price_decimal_strips_insignificant_trailing_zeros(price, expected):
    """The core regression: Decimal trailing zeros are cosmetic and must be
    stripped to match Alpaca's canonical form (``330.50`` -> ``330.5``)."""
    assert _format_price(price) == expected


@pytest.mark.parametrize(
    ("price", "expected"),
    [
        # str(float) is already the clean canonical form — keep it as-is so we
        # don't introduce float-precision artifacts via Decimal(float).
        (330.50, "330.5"),
        (330.0, "330.0"),
        (0.25, "0.25"),
        (5.0, "5.0"),
        # ints stringify trivially.
        (330, "330"),
        (0, "0"),
        (1000000, "1000000"),
    ],
)
def test_format_price_non_decimal_uses_str(price, expected):
    """float/int inputs go through ``str()`` (the float path was already
    canonical; only the Decimal path had the trailing-zero bug)."""
    assert _format_price(price) == expected


def test_format_price_strips_only_cosmetic_zeros_not_precision():
    """Stripping trailing zeros must never lose a significant digit.

    Every ``_format_price`` output parses back to the original value, so we
    only ever drop zeros that carry no numeric meaning.
    """
    cases = [
        Decimal("330.50"),
        Decimal("330.05"),
        Decimal("100.00"),
        Decimal("0.00010"),
        Decimal("123.4500"),
        Decimal("1000000.0000"),
        Decimal("0.00"),
    ]
    for price in cases:
        serialised = _format_price(price)
        assert "e" not in serialised.lower(), f"scientific notation leaked: {serialised!r}"
        # The serialised string must equal the original numeric value.
        assert Decimal(serialised) == price


# ---------------------------------------------------------------------------
# _format_qty — direct unit tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("qty", "expected"),
    [
        # Decimal keeps full precision (format 'f'), trailing zeros preserved.
        (Decimal("100"), "100"),
        (Decimal("100.00"), "100.00"),
        (Decimal("0.5"), "0.5"),
        (Decimal("50"), "50"),
        (Decimal("12.345"), "12.345"),
        # ints / floats go through str().
        (100, "100"),
        (0, "0"),
        (50.0, "50.0"),
        (1.5, "1.5"),
    ],
)
def test_format_qty(qty, expected):
    """Quantity formatting is distinct from price: Decimal precision (incl.
    trailing zeros) is preserved because qty carries fractional-share
    semantics (e.g. fractional shares)."""
    assert _format_qty(qty) == expected


# ---------------------------------------------------------------------------
# Integration: LiveExecutionBackend.submit_order wire format
# ---------------------------------------------------------------------------


def _capturing_handler(captured: list[httpx.Request], *, order_type: str = "limit"):
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "id": "order-1",
                "status": "new",
                "symbol": "AAPL",
                "type": order_type,
                "qty": "50",
                "filled_qty": "0",
                "filled_avg_price": None,
            },
        )

    return handler


@pytest.mark.asyncio
async def test_live_backend_submit_order_serialises_canonical_limit_price():
    """The regression end-to-end through the backend: a trailing-zero
    Decimal limit price reaches the wire as ``330.5``, not ``330.50``."""
    captured: list[httpx.Request] = []
    client = httpx.AsyncClient(
        base_url=PAPER_URL, transport=httpx.MockTransport(_capturing_handler(captured))
    )
    backend = LiveExecutionBackend("k", "s", client=client)

    await backend.submit_order(
        "AAPL", Decimal("50"), "sell", "limit", limit_price=Decimal("330.50")
    )

    body = captured[0].read().decode()
    assert '"limit_price":"330.5"' in body
    assert "330.50" not in body  # the buggy form must not appear
    await client.aclose()


@pytest.mark.asyncio
async def test_live_backend_market_order_omits_limit_price():
    """A market order must not include a limit_price key at all."""
    captured: list[httpx.Request] = []
    client = httpx.AsyncClient(
        base_url=PAPER_URL,
        transport=httpx.MockTransport(_capturing_handler(captured, order_type="market")),
    )
    backend = LiveExecutionBackend("k", "s", client=client)

    await backend.submit_order("AAPL", Decimal("50"), "buy", "market")

    body = captured[0].read().decode()
    assert "limit_price" not in body
    await client.aclose()


@pytest.mark.parametrize(
    ("limit_price", "expected_fragment"),
    [
        (Decimal("330.50"), '"limit_price":"330.5"'),
        (Decimal("330.00"), '"limit_price":"330"'),
        (Decimal("100.00"), '"limit_price":"100"'),
        (Decimal("0.0001"), '"limit_price":"0.0001"'),
        (Decimal("1000000"), '"limit_price":"1000000"'),
        (330.50, '"limit_price":"330.5"'),  # float path stays canonical too
    ],
)
@pytest.mark.asyncio
async def test_live_backend_limit_price_wire_format_parametrised(limit_price, expected_fragment):
    """Multiple price shapes all land on the canonical wire fragment."""
    captured: list[httpx.Request] = []
    client = httpx.AsyncClient(
        base_url=PAPER_URL, transport=httpx.MockTransport(_capturing_handler(captured))
    )
    backend = LiveExecutionBackend("k", "s", client=client)

    await backend.submit_order("AAPL", Decimal("50"), "sell", "limit", limit_price=limit_price)

    body = captured[0].read().decode()
    assert expected_fragment in body
    # No scientific notation ever leaks onto the wire.
    assert "e+" not in body and "E+" not in body
    await client.aclose()


# ---------------------------------------------------------------------------
# Integration: AlpacaBrokerAdapter.submit_order wire format
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_alpaca_adapter_submit_order_serialises_canonical_limit_price():
    """The same regression through the public adapter (which delegates to the
    backend). This is the test that was failing before the fix."""
    captured: list[httpx.Request] = []
    client = httpx.AsyncClient(
        base_url=PAPER_URL, transport=httpx.MockTransport(_capturing_handler(captured))
    )
    adapter = AlpacaBrokerAdapter(api_key="k", api_secret="s", client=client)

    order = await adapter.submit_order(
        "AAPL", Decimal("50"), "sell", "LIMIT", limit_price=Decimal("330.50")
    )

    assert order["id"] == "order-1"
    body = captured[0].read().decode()
    # The canonical form — exactly what the test previously expected.
    assert '"limit_price":"330.5"' in body
    assert '"330.50"' not in body
    await client.aclose()


@pytest.mark.asyncio
async def test_alpaca_adapter_market_order_omits_limit_price():
    """Through the adapter, a market order carries no limit_price."""
    captured: list[httpx.Request] = []
    client = httpx.AsyncClient(
        base_url=PAPER_URL,
        transport=httpx.MockTransport(_capturing_handler(captured, order_type="market")),
    )
    adapter = AlpacaBrokerAdapter(api_key="k", api_secret="s", client=client)

    await adapter.submit_order("AAPL", Decimal("50"), "buy", "market")

    body = captured[0].read().decode()
    assert "limit_price" not in body
    await client.aclose()
