"""
Golden-file regression tests for the backtest pipeline.

Pins key outputs of representative backtest scenarios. If math drifts in
backtest_runner / portfolio / order_manager / cost_model / metrics, these
tests fail and the diff explains why.

Run with `UPDATE_GOLDEN=1` env var to regenerate after an intentional change.
See `tests/golden/README.md`.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from engine.core.backtest_runner import (
    BacktestConfig,
    BacktestResult,
    BacktestRunner,
)
from engine.core.signal import Side, Signal
from engine.data.feeds import MarketDataProvider

GOLDEN_DIR = Path(__file__).parent / "golden"
UPDATE = os.environ.get("UPDATE_GOLDEN") == "1"


# ─── helpers ────────────────────────────────────────────────────────────────


class _DeterministicProvider(MarketDataProvider):
    def __init__(self, df: pd.DataFrame) -> None:
        self._df = df

    async def get_latest_price(self, symbol: str) -> float | None:
        return float(self._df["close"].iloc[-1]) if not self._df.empty else None

    async def get_ohlcv(
        self, symbol: str, period: str = "1y", interval: str = "1d"
    ) -> pd.DataFrame:
        return self._df

    async def get_multiple_prices(self, symbols: list[str]) -> dict[str, float]:
        price = await self.get_latest_price(symbols[0]) if symbols else None
        return dict.fromkeys(symbols, price or 0.0)


def _seeded_ohlcv(seed: int, n_bars: int, base: float, drift: float) -> pd.DataFrame:
    """Pure deterministic OHLCV generator. Same inputs ⇒ identical bytes."""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2025-01-02", periods=n_bars)
    noise = rng.normal(0, 1, n_bars)
    close = base + np.cumsum(noise * 0.4 + drift)
    close = np.maximum(close, 1.0)
    return pd.DataFrame(
        {
            "open": close - 0.1,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": rng.integers(100_000, 1_000_000, n_bars),
        },
        index=dates,
    )


def _checksum(items: list[tuple]) -> str:
    payload = json.dumps(items, sort_keys=True, default=str).encode()
    return hashlib.sha256(payload).hexdigest()[:16]


def _equity_checksum(equity_curve: list[dict[str, Any]]) -> str:
    rows = [
        (str(r["timestamp"]), round(r.get("total_value", 0.0), 2), round(r.get("cash", 0.0), 2))
        for r in equity_curve
    ]
    return _checksum(rows)


def _trades_signature(trades: list[dict[str, Any]]) -> str:
    rows = [
        (
            t.get("symbol"),
            t.get("side"),
            int(t.get("quantity", 0)),
            round(float(t.get("fill_price", 0.0)), 4),
        )
        for t in trades
    ]
    return _checksum(rows)


def _snapshot(result: BacktestResult, realized_pnl: float) -> dict[str, Any]:
    return {
        "final_capital": round(result.final_capital, 2),
        "total_return_pct": round(result.total_return_pct, 4),
        "realized_pnl": round(realized_pnl, 2),
        "total_trades": len(result.trades),
        "closed_trades": sum(1 for t in result.trades if t.get("side") == "sell"),
        "equity_points": len(result.equity_curve),
        "equity_curve_checksum": _equity_checksum(result.equity_curve),
        "trades_signature": _trades_signature(result.trades),
    }


def _compare_golden(name: str, snapshot: dict[str, Any]) -> None:
    path = GOLDEN_DIR / f"{name}.json"
    if UPDATE or not path.exists():
        path.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n")
        if not UPDATE:
            pytest.skip(f"Golden seeded at {path} — re-run to compare.")
        return
    expected = json.loads(path.read_text())
    if expected != snapshot:
        diff_lines = ["Golden-file mismatch (run UPDATE_GOLDEN=1 to refresh):"]
        for key in sorted(set(expected) | set(snapshot)):
            ev = expected.get(key, "<missing>")
            av = snapshot.get(key, "<missing>")
            if ev != av:
                diff_lines.append(f"  {key}: expected={ev!r}  actual={av!r}")
        pytest.fail("\n".join(diff_lines))


# ─── deterministic strategies ──────────────────────────────────────────────


class _BuyHoldStrategy:
    """Buys 100 shares on the first bar after warmup, holds forever."""

    name = "buy_hold"
    version = "1.0.0"

    def __init__(self) -> None:
        self._fired = False

    def on_bar(self, state, portfolio):
        if self._fired:
            return []
        self._fired = True
        return [Signal(symbol="TEST", side=Side.BUY, quantity=100, strategy_id="bh")]


class _CycleStrategy:
    """Buys at bar 30, sells at bar 60, buys again at bar 90, sells at bar 120.

    Designed to exercise: cost model, tax-lot creation, FIFO consumption,
    realized PnL accumulation, and trade counting.
    """

    name = "cycle"
    version = "1.0.0"

    def __init__(self) -> None:
        self._n = 0

    def on_bar(self, state, portfolio):
        self._n += 1
        if self._n == 30:
            return [Signal(symbol="TEST", side=Side.BUY, quantity=100, strategy_id="c")]
        if self._n == 60:
            return [Signal(symbol="TEST", side=Side.SELL, quantity=100, strategy_id="c")]
        if self._n == 90:
            return [Signal(symbol="TEST", side=Side.BUY, quantity=50, strategy_id="c")]
        if self._n == 120:
            return [Signal(symbol="TEST", side=Side.SELL, quantity=50, strategy_id="c")]
        return []


# ─── tests ──────────────────────────────────────────────────────────────────


class TestBacktestGolden:
    """Pinned regression tests for the backtest pipeline."""

    async def _run(
        self,
        df: pd.DataFrame,
        strategy,
        strategy_name: str,
    ) -> tuple[BacktestResult, float]:
        config = BacktestConfig(
            strategy_name=strategy_name,
            symbol="TEST",
            start_date=str(df.index[0].date()),
            end_date=str(df.index[-1].date()),
            initial_capital=100_000.0,
            min_bars=5,
            random_seed=42,
        )
        runner = BacktestRunner(
            config=config,
            strategy=strategy,
            provider=_DeterministicProvider(df),
        )
        result = await runner.run()
        # We snapshot realized_pnl from the in-result trades since the runner
        # tears down the portfolio. closed PnL == sum of sell trade realized_pnl.
        realized = sum(float(t.get("realized_pnl", 0.0)) for t in result.trades)
        return result, realized

    async def test_bull_market_buy_hold(self) -> None:
        df = _seeded_ohlcv(seed=1001, n_bars=200, base=100.0, drift=0.10)
        result, realized = await self._run(df, _BuyHoldStrategy(), "buy_hold")
        _compare_golden("bull_market_buy_hold", _snapshot(result, realized))

    async def test_whipsaw_cycle_strategy(self) -> None:
        df = _seeded_ohlcv(seed=2002, n_bars=200, base=100.0, drift=0.0)
        result, realized = await self._run(df, _CycleStrategy(), "cycle")
        _compare_golden("whipsaw_cycle_strategy", _snapshot(result, realized))

    async def test_bear_market_buy_hold(self) -> None:
        df = _seeded_ohlcv(seed=3003, n_bars=200, base=200.0, drift=-0.15)
        result, realized = await self._run(df, _BuyHoldStrategy(), "buy_hold")
        _compare_golden("bear_market_buy_hold", _snapshot(result, realized))
