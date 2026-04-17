"""
Golden-file regression tests for the backtest engine.

Each fixture in tests/golden/backtests/ represents a curated
(strategy, data range, seed, config) tuple whose expected outputs are
pinned in the repo. Any unintended change to core math fails CI.

Run with --update-golden to regenerate baselines.
See tests/golden/backtests/README.md for details.
"""

from __future__ import annotations

import csv
import gzip
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
import yaml

from engine.core.backtest_runner import BacktestConfig, BacktestRunner
from engine.core.signal import Side, Signal
from engine.data.feeds import MarketDataProvider

BACKTESTS_DIR = Path(__file__).parent / "golden" / "backtests"
SNAPSHOTS_DIR = Path(__file__).parent / "data" / "snapshots"
EQUITY_SAMPLE_COUNT = 50


# ─── Data Generation ────────────────────────────────────────────────────────


def generate_ohlcv(
    seed: int,
    n_bars: int,
    base_price: float,
    drift: float = 0.0,
    volatility: float = 0.02,
    start_date: str = "2020-01-02",
) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(start_date, periods=n_bars)
    returns = rng.normal(drift, volatility, n_bars)
    close = base_price * np.exp(np.cumsum(returns))
    close = np.maximum(close, 0.01)
    spread = close * rng.uniform(0.001, 0.005, n_bars)
    high = close + spread * rng.uniform(0.5, 1.0, n_bars)
    low = close - spread * rng.uniform(0.5, 1.0, n_bars)
    return pd.DataFrame(
        {
            "open": close - spread / 2,
            "high": high,
            "low": low,
            "close": close,
            "volume": rng.integers(100_000, 10_000_000, n_bars),
        },
        index=dates,
    )


class DeterministicProvider(MarketDataProvider):
    def __init__(self, data: dict[str, pd.DataFrame]) -> None:
        self._data = data

    async def get_latest_price(self, symbol: str) -> float | None:
        df = self._data.get(symbol)
        return float(df["close"].iloc[-1]) if df is not None and not df.empty else None

    async def get_ohlcv(
        self,
        symbol: str,
        period: str = "1y",  # noqa: ARG002
        interval: str = "1d",  # noqa: ARG002
    ) -> pd.DataFrame:
        return self._data.get(symbol, pd.DataFrame())

    async def get_multiple_prices(self, symbols: list[str]) -> dict[str, float]:
        prices: dict[str, float] = {}
        for s in symbols:
            p = await self.get_latest_price(s)
            prices[s] = p or 0.0
        return prices


# ─── Strategy Implementations ───────────────────────────────────────────────


class BuyHoldStrategy:
    name = "buy_hold"
    version = "1.0.0"

    def __init__(self, quantity: int = 100) -> None:
        self._quantity = quantity
        self._fired = False

    def on_bar(self, state: Any, portfolio: Any) -> list[Signal]:  # noqa: ARG002
        if self._fired:
            return []
        self._fired = True
        prices = getattr(state, "prices", {})
        symbol = next(iter(prices.keys())) if prices else "TEST"
        return [Signal(symbol=symbol, side=Side.BUY, quantity=self._quantity, strategy_id="bh")]


class SMACrossoverStrategy:
    name = "sma_crossover"
    version = "1.0.0"

    def __init__(self, fast_period: int = 50, slow_period: int = 200, quantity: int = 50) -> None:
        self._fast = fast_period
        self._slow = slow_period
        self._quantity = quantity
        self._position = False

    def on_bar(self, state: Any, portfolio: Any) -> list[Signal]:  # noqa: ARG002
        prices = getattr(state, "prices", {})
        if not prices:
            return []
        symbol = next(iter(prices.keys()))
        fast_ma = state.sma(symbol, self._fast)
        slow_ma = state.sma(symbol, self._slow)
        if fast_ma is None or slow_ma is None:
            return []
        signals: list[Signal] = []
        if fast_ma > slow_ma and not self._position:
            self._position = True
            signals.append(
                Signal(symbol=symbol, side=Side.BUY, quantity=self._quantity, strategy_id="sma")
            )
        elif fast_ma < slow_ma and self._position:
            self._position = False
            signals.append(
                Signal(symbol=symbol, side=Side.SELL, quantity=self._quantity, strategy_id="sma")
            )
        return signals


class MeanReversionStrategy:
    name = "mean_reversion"
    version = "1.0.0"

    def __init__(
        self, lookback: int = 20, entry_z: float = 2.0, exit_z: float = 0.5, quantity: int = 50
    ) -> None:
        self._lookback = lookback
        self._entry_z = entry_z
        self._exit_z = exit_z
        self._quantity = quantity
        self._position = False

    def on_bar(self, state: Any, portfolio: Any) -> list[Signal]:  # noqa: ARG002
        prices = getattr(state, "prices", {})
        if not prices:
            return []
        symbol = next(iter(prices.keys()))
        mean = state.sma(symbol, self._lookback)
        std = state.std(symbol, self._lookback)
        if mean is None or std is None or std == 0:
            return []
        price = prices[symbol]
        z = (price - mean) / std
        signals: list[Signal] = []
        if z < -self._entry_z and not self._position:
            self._position = True
            signals.append(
                Signal(symbol=symbol, side=Side.BUY, quantity=self._quantity, strategy_id="mr")
            )
        elif z > self._exit_z and self._position:
            self._position = False
            signals.append(
                Signal(symbol=symbol, side=Side.SELL, quantity=self._quantity, strategy_id="mr")
            )
        return signals


class DCAStrategy:
    name = "dca"
    version = "1.0.0"

    def __init__(self, frequency: int = 7, quantity_per_buy: int = 10) -> None:
        self._frequency = frequency
        self._quantity = quantity_per_buy
        self._bar_count = 0

    def on_bar(self, state: Any, portfolio: Any) -> list[Signal]:  # noqa: ARG002
        self._bar_count += 1
        if self._bar_count % self._frequency != 0:
            return []
        prices = getattr(state, "prices", {})
        if not prices:
            return []
        symbol = next(iter(prices.keys()))
        return [Signal(symbol=symbol, side=Side.BUY, quantity=self._quantity, strategy_id="dca")]


STRATEGY_REGISTRY: dict[str, type] = {
    "BuyHoldStrategy": BuyHoldStrategy,
    "SMACrossoverStrategy": SMACrossoverStrategy,
    "MeanReversionStrategy": MeanReversionStrategy,
    "DCAStrategy": DCAStrategy,
}


# ─── Fixture Loading ────────────────────────────────────────────────────────


@dataclass
class FixtureConfig:
    name: str
    description: str
    strategy_class: str
    strategy_params: dict[str, Any]
    backtest: dict[str, Any]
    data: dict[str, Any]
    tolerances: dict[str, float]
    timeout_seconds: int
    status: str
    pending_reason: str | None


def load_fixture_config(fixture_dir: Path) -> FixtureConfig:
    config_path = fixture_dir / "config.yaml"
    with open(config_path) as f:
        raw = yaml.safe_load(f)
    return FixtureConfig(
        name=raw["name"],
        description=raw.get("description", ""),
        strategy_class=raw["strategy"]["class"],
        strategy_params=raw["strategy"].get("params", {}),
        backtest=raw["backtest"],
        data=raw["data"],
        tolerances=raw.get("tolerances", {}),
        timeout_seconds=raw.get("timeout_seconds", 30),
        status=raw.get("status", "active"),
        pending_reason=raw.get("pending_reason"),
    )


def discover_fixtures() -> list[Path]:
    if not BACKTESTS_DIR.exists():
        return []
    return sorted(
        p for p in BACKTESTS_DIR.iterdir() if p.is_dir() and (p / "config.yaml").exists()
    )


# ─── Comparison Helpers ─────────────────────────────────────────────────────


def _metric_tolerance(key: str, tolerances: dict[str, float]) -> float:
    key_map: dict[str, str] = {
        "sharpe_ratio": "sharpe",
        "sortino_ratio": "sortino",
        "annualized_return_pct": "cagr",
        "total_return_pct": "total_return",
        "max_drawdown_pct": "max_drawdown",
        "volatility_annual_pct": "volatility",
        "win_rate": "win_rate",
        "profit_factor": "profit_factor",
    }
    mapped = key_map.get(key, "default")
    return tolerances.get(mapped, tolerances.get("default", 0.0001))


def compare_metrics(actual: dict, expected: dict, tolerances: dict[str, float]) -> list[str]:
    diffs: list[str] = []
    exact_keys = {"total_trades", "max_consecutive_wins", "max_consecutive_losses"}

    for key, ev in expected.items():
        if key == "rolling_metrics":
            continue
        if key not in actual:
            diffs.append(f"  {key}: missing in actual (expected={ev!r})")
            continue
        av = actual[key]
        if ev is None and av is None:
            continue
        if key in exact_keys:
            if ev != av:
                diffs.append(f"  {key}: expected={ev!r}  actual={av!r} (exact match required)")
        elif isinstance(ev, (int, float)) and isinstance(av, (int, float)):
            tol = _metric_tolerance(key, tolerances)
            if abs(float(av) - float(ev)) > tol:
                diffs.append(f"  {key}: expected={ev!r}  actual={av!r}  tolerance=+/-{tol}")
        elif ev != av:
            diffs.append(f"  {key}: expected={ev!r}  actual={av!r}")
    return diffs


def compare_equity_curve(
    actual_curve: list[dict[str, Any]],
    expected_path: Path,
    tolerance_pct: float,
) -> list[str]:
    if not expected_path.exists():
        return ["  equity_curve: expected file not found"]
    with gzip.open(expected_path, "rt") as f:
        reader = csv.DictReader(f)
        expected_points = list(reader)
    if not expected_points:
        return []
    step = max(1, len(actual_curve) // EQUITY_SAMPLE_COUNT)
    sampled = actual_curve[::step][: len(expected_points)]
    diffs: list[str] = []
    for i, (actual_pt, expected_row) in enumerate(zip(sampled, expected_points, strict=False)):
        actual_val = actual_pt.get("total_value", 0.0)
        expected_val = float(expected_row["total_value"])
        if expected_val > 0:
            diff_pct = abs(actual_val - expected_val) / expected_val * 100
            if diff_pct > tolerance_pct:
                diffs.append(
                    f"  equity_curve[{i}]: actual={actual_val:.2f}"
                    f"  expected={expected_val:.2f}  diff={diff_pct:.4f}%"
                )
    if len(sampled) != len(expected_points):
        diffs.append(
            f"  equity_curve length: sampled={len(sampled)}  expected={len(expected_points)}"
        )
    return diffs


def compare_trades(actual_trades: list[dict[str, Any]], expected_path: Path) -> list[str]:
    if not expected_path.exists():
        return ["  trades: expected file not found"]
    with gzip.open(expected_path, "rt") as f:
        reader = csv.DictReader(f)
        expected_trades = list(reader)
    diffs: list[str] = []
    if len(actual_trades) != len(expected_trades):
        diffs.append(
            f"  trade count: actual={len(actual_trades)}  expected={len(expected_trades)}"
        )
    for i, (at, et) in enumerate(zip(actual_trades, expected_trades, strict=False)):
        if at.get("side") != et.get("side"):
            diffs.append(
                f"  trade[{i}].side: actual={at.get('side')!r}  expected={et.get('side')!r}"
            )
        if int(at.get("quantity", 0)) != int(et.get("quantity", 0)):
            diffs.append(
                f"  trade[{i}].quantity:"
                f" actual={at.get('quantity')!r}  expected={et.get('quantity')!r}"
            )
    return diffs


# ─── Update Helpers ─────────────────────────────────────────────────────────


def write_metrics(fixture_dir: Path, metrics: dict[str, Any]) -> None:
    skip_keys = {"equity_curve", "drawdown_curve", "rolling_metrics"}
    filtered = {k: v for k, v in metrics.items() if k not in skip_keys}
    path = fixture_dir / "expected_metrics.json"
    path.write_text(json.dumps(filtered, indent=2, sort_keys=True, default=str) + "\n")


def write_equity_curve(fixture_dir: Path, equity_curve: list[dict[str, Any]]) -> None:
    step = max(1, len(equity_curve) // EQUITY_SAMPLE_COUNT)
    sampled = equity_curve[::step]
    path = fixture_dir / "expected_equity_curve.csv.gz"
    with gzip.open(path, "wt") as f:
        writer = csv.DictWriter(f, fieldnames=["timestamp", "total_value", "cash"])
        writer.writeheader()
        for pt in sampled:
            writer.writerow(
                {
                    "timestamp": str(pt.get("timestamp", ""))[:19],
                    "total_value": f"{pt.get('total_value', 0.0):.4f}",
                    "cash": f"{pt.get('cash', 0.0):.4f}",
                }
            )


def write_trades(fixture_dir: Path, trades: list[dict[str, Any]]) -> None:
    path = fixture_dir / "expected_trades.csv.gz"
    with gzip.open(path, "wt") as f:
        writer = csv.DictWriter(
            f, fieldnames=["timestamp", "symbol", "side", "quantity", "fill_price", "realized_pnl"]
        )
        writer.writeheader()
        for t in trades:
            writer.writerow(
                {
                    "timestamp": str(t.get("timestamp", ""))[:19],
                    "symbol": t.get("symbol", ""),
                    "side": t.get("side", ""),
                    "quantity": t.get("quantity", 0),
                    "fill_price": f"{t.get('fill_price', 0.0):.4f}",
                    "realized_pnl": f"{t.get('realized_pnl', 0.0):.4f}",
                }
            )


# ─── Test Runner ────────────────────────────────────────────────────────────


class TestGoldenBacktests:
    @pytest.fixture(autouse=True)
    def _setup(self, request: pytest.FixtureRequest) -> None:
        self._update = (
            request.config.getoption("--update-golden", default=False)
            or os.environ.get("UPDATE_GOLDEN") == "1"
        )

    async def _run_fixture(self, fixture_dir: Path) -> None:
        config = load_fixture_config(fixture_dir)

        if config.status == "pending":
            pytest.skip(f"Fixture pending: {config.pending_reason or 'engine support needed'}")

        strategy_cls = STRATEGY_REGISTRY.get(config.strategy_class)
        if strategy_cls is None:
            pytest.skip(f"Strategy class {config.strategy_class} not in registry")

        strategy = strategy_cls(**config.strategy_params)
        bt = config.backtest
        data_cfg = config.data

        df = generate_ohlcv(
            seed=data_cfg["seed"],
            n_bars=data_cfg["n_bars"],
            base_price=data_cfg["base_price"],
            drift=data_cfg.get("drift", 0.0),
            volatility=data_cfg.get("volatility", 0.02),
            start_date=data_cfg.get("start_date", "2020-01-02"),
        )

        symbol = bt["symbol"]
        start = bt["start_date"]
        end = bt["end_date"]
        mask = (df.index >= pd.Timestamp(start)) & (df.index <= pd.Timestamp(end))
        df_range = df.loc[mask]
        if df_range.empty:
            pytest.fail(f"No data in range {start} to {end}")

        bt_config = BacktestConfig(
            strategy_name=config.name,
            symbol=symbol,
            start_date=start,
            end_date=end,
            initial_capital=bt.get("initial_capital", 100_000.0),
            min_bars=bt.get("min_bars", 50),
            random_seed=bt.get("random_seed", 42),
        )

        provider = DeterministicProvider({symbol: df})
        runner = BacktestRunner(config=bt_config, strategy=strategy, provider=provider)

        t0 = time.monotonic()
        result = await runner.run()
        elapsed = time.monotonic() - t0

        if elapsed > config.timeout_seconds:
            pytest.fail(f"Runtime budget exceeded: {elapsed:.1f}s > {config.timeout_seconds}s")

        if self._update:
            write_metrics(fixture_dir, result.metrics)
            write_equity_curve(fixture_dir, result.equity_curve)
            write_trades(fixture_dir, result.trades)
            return

        all_diffs: list[str] = []

        expected_metrics_path = fixture_dir / "expected_metrics.json"
        if expected_metrics_path.exists():
            expected_metrics = json.loads(expected_metrics_path.read_text())
            all_diffs.extend(compare_metrics(result.metrics, expected_metrics, config.tolerances))
        else:
            all_diffs.append("  expected_metrics.json not found (run with --update-golden)")

        equity_tolerance = config.tolerances.get("equity_pct", 0.01)
        all_diffs.extend(
            compare_equity_curve(
                result.equity_curve, fixture_dir / "expected_equity_curve.csv.gz", equity_tolerance
            )
        )
        all_diffs.extend(compare_trades(result.trades, fixture_dir / "expected_trades.csv.gz"))

        if all_diffs:
            msg = (
                f"Golden-file mismatch for {config.name}.\n"
                f"To update: pytest tests/test_golden_backtests.py --update-golden\n"
                + "\n".join(all_diffs)
            )
            pytest.fail(msg)

    async def test_buy_and_hold_spy(self) -> None:
        await self._run_fixture(BACKTESTS_DIR / "buy_and_hold_spy_2020_2024")

    async def test_sma_crossover_spy(self) -> None:
        await self._run_fixture(BACKTESTS_DIR / "sma_crossover_spy_50_200")

    async def test_mean_reversion_aapl(self) -> None:
        await self._run_fixture(BACKTESTS_DIR / "mean_reversion_aapl_2022")

    async def test_dca_btc(self) -> None:
        await self._run_fixture(BACKTESTS_DIR / "crypto_dca_btc_2023_2024")

    async def test_pairs_trading_ko_pep(self) -> None:
        await self._run_fixture(BACKTESTS_DIR / "pairs_trading_ko_pep_2023")

    async def test_covered_call_spy(self) -> None:
        await self._run_fixture(BACKTESTS_DIR / "covered_call_spy_2023")

    async def test_forex_carry(self) -> None:
        await self._run_fixture(BACKTESTS_DIR / "forex_carry_2023")

    async def test_multi_strategy_portfolio(self) -> None:
        await self._run_fixture(BACKTESTS_DIR / "multi_strategy_portfolio_2023")
