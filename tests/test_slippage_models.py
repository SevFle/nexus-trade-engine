"""
Comprehensive tests for slippage models — FixedBps, Percentage, SquareRoot,
VolumeWeighted, RandomWalk, and the create_slippage_model factory function.
"""

from __future__ import annotations

import math
import random

import pytest

from engine.core.execution.slippage import (
    FixedBpsSlippage,
    PercentageSlippage,
    RandomWalkSlippage,
    SlippageContext,
    SlippageModel,
    SlippageModelType,
    SquareRootSlippage,
    VolumeWeightedSlippage,
    create_slippage_model,
)


def _ctx(
    symbol: str = "AAPL",
    side: str = "buy",
    quantity: int = 100,
    market_price: float = 150.0,
    avg_volume: int = 0,
) -> SlippageContext:
    return SlippageContext(
        symbol=symbol,
        side=side,
        quantity=quantity,
        market_price=market_price,
        avg_volume=avg_volume,
    )


class TestSlippageContext:
    def test_defaults(self):
        ctx = SlippageContext(symbol="AAPL", side="buy", quantity=100, market_price=150.0)
        assert ctx.symbol == "AAPL"
        assert ctx.side == "buy"
        assert ctx.quantity == 100
        assert ctx.market_price == 150.0
        assert ctx.avg_volume == 0
        assert ctx.costs is None

    def test_with_avg_volume(self):
        ctx = _ctx(avg_volume=1_000_000)
        assert ctx.avg_volume == 1_000_000


class TestSlippageModelType:
    def test_all_types(self):
        assert SlippageModelType.FIXED_BPS == "fixed_bps"
        assert SlippageModelType.PERCENTAGE == "percentage"
        assert SlippageModelType.SQUARE_ROOT == "square_root"
        assert SlippageModelType.VOLUME_WEIGHTED == "volume_weighted"
        assert SlippageModelType.RANDOM_WALK == "random_walk"

    def test_from_string(self):
        for t in SlippageModelType:
            assert SlippageModelType(t.value) is t

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            SlippageModelType("unknown")


class TestFixedBpsSlippage:
    def test_default_bps(self):
        model = FixedBpsSlippage()
        assert model.bps == 5.0

    def test_compute_basic(self):
        model = FixedBpsSlippage(bps=5.0)
        ctx = _ctx(market_price=100.0)
        result = model.compute(ctx)
        assert result == pytest.approx(0.05)

    def test_compute_high_price(self):
        model = FixedBpsSlippage(bps=10.0)
        ctx = _ctx(market_price=1000.0)
        result = model.compute(ctx)
        assert result == pytest.approx(1.0)

    def test_compute_zero_price(self):
        model = FixedBpsSlippage(bps=5.0)
        ctx = _ctx(market_price=0.0)
        assert model.compute(ctx) == 0.0

    def test_compute_small_price(self):
        model = FixedBpsSlippage(bps=1.0)
        ctx = _ctx(market_price=0.01)
        result = model.compute(ctx)
        assert result == pytest.approx(0.000001)

    def test_always_positive(self):
        model = FixedBpsSlippage(bps=100.0)
        ctx = _ctx(market_price=50.0)
        assert model.compute(ctx) > 0

    def test_independent_of_quantity(self):
        model = FixedBpsSlippage(bps=5.0)
        ctx1 = _ctx(quantity=1, market_price=100.0)
        ctx2 = _ctx(quantity=10000, market_price=100.0)
        assert model.compute(ctx1) == model.compute(ctx2)


class TestPercentageSlippage:
    def test_default_pct(self):
        model = PercentageSlippage()
        assert model.pct == 0.0005

    def test_compute_basic(self):
        model = PercentageSlippage(pct=0.001)
        ctx = _ctx(market_price=100.0)
        result = model.compute(ctx)
        assert result == pytest.approx(0.1)

    def test_compute_zero_price(self):
        model = PercentageSlippage(pct=0.001)
        ctx = _ctx(market_price=0.0)
        assert model.compute(ctx) == 0.0

    def test_always_non_negative(self):
        model = PercentageSlippage(pct=0.05)
        ctx = _ctx(market_price=200.0)
        assert model.compute(ctx) >= 0

    def test_independent_of_side(self):
        model = PercentageSlippage(pct=0.001)
        buy_ctx = _ctx(side="buy", market_price=100.0)
        sell_ctx = _ctx(side="sell", market_price=100.0)
        assert model.compute(buy_ctx) == model.compute(sell_ctx)


class TestSquareRootSlippage:
    def test_defaults(self):
        model = SquareRootSlippage()
        assert model.base_bps == 5.0
        assert model.volume_scale == 0.1

    def test_compute_no_volume(self):
        model = SquareRootSlippage(base_bps=5.0)
        ctx = _ctx(market_price=100.0, avg_volume=0)
        result = model.compute(ctx)
        assert result == pytest.approx(0.05)

    def test_compute_with_volume(self):
        model = SquareRootSlippage(base_bps=5.0, volume_scale=0.1)
        ctx = _ctx(quantity=1000, market_price=100.0, avg_volume=10000)
        result = model.compute(ctx)
        base = 0.05
        participation = 1000 / 10000
        expected = base * (1.0 + 0.1 * math.sqrt(participation))
        assert result == pytest.approx(expected)

    def test_compute_zero_quantity_with_volume(self):
        model = SquareRootSlippage(base_bps=5.0)
        ctx = _ctx(quantity=0, market_price=100.0, avg_volume=10000)
        result = model.compute(ctx)
        assert result == pytest.approx(0.05)

    def test_volume_increases_slippage(self):
        model = SquareRootSlippage(base_bps=5.0)
        ctx_small = _ctx(quantity=10, market_price=100.0, avg_volume=100000)
        ctx_large = _ctx(quantity=50000, market_price=100.0, avg_volume=100000)
        assert model.compute(ctx_large) > model.compute(ctx_small)


class TestVolumeWeightedSlippage:
    def test_defaults(self):
        model = VolumeWeightedSlippage()
        assert model.base_bps == 5.0
        assert model.max_impact_bps == 50.0

    def test_compute_no_volume(self):
        model = VolumeWeightedSlippage(base_bps=5.0)
        ctx = _ctx(market_price=100.0, avg_volume=0)
        result = model.compute(ctx)
        assert result == pytest.approx(0.05)

    def test_compute_with_volume(self):
        model = VolumeWeightedSlippage(base_bps=5.0, max_impact_bps=50.0)
        ctx = _ctx(quantity=100, market_price=100.0, avg_volume=1000)
        base = 0.05
        participation = 100 / 1000
        impact_bps = min(participation * 100, 50.0)
        impact = 100.0 * (impact_bps / 10000)
        expected = base + impact
        assert model.compute(ctx) == pytest.approx(expected)

    def test_max_impact_capped(self):
        model = VolumeWeightedSlippage(base_bps=5.0, max_impact_bps=10.0)
        ctx = _ctx(quantity=100000, market_price=100.0, avg_volume=1000)
        result = model.compute(ctx)
        base = 0.05
        max_impact = 100.0 * (10.0 / 10000)
        assert result == pytest.approx(base + max_impact)

    def test_zero_quantity_returns_base(self):
        model = VolumeWeightedSlippage(base_bps=5.0)
        ctx = _ctx(quantity=0, market_price=100.0, avg_volume=1000)
        result = model.compute(ctx)
        assert result == pytest.approx(0.05)


class TestRandomWalkSlippage:
    def test_defaults(self):
        model = RandomWalkSlippage()
        assert model.base_bps == 5.0
        assert model.volatility_factor == 0.5

    def test_deterministic_with_seed(self):
        rng = random.Random(42)  # noqa: S311
        model = RandomWalkSlippage(base_bps=5.0, volatility_factor=0.0, rng=rng)
        ctx = _ctx(market_price=100.0)
        result = model.compute(ctx)
        assert result == pytest.approx(0.05)

    def test_always_non_negative(self):
        rng = random.Random(42)  # noqa: S311
        model = RandomWalkSlippage(base_bps=5.0, volatility_factor=10.0, rng=rng)
        for _ in range(100):
            ctx = _ctx(market_price=100.0)
            result = model.compute(ctx)
            assert result >= 0.0

    def test_varies_with_randomness(self):
        results = set()
        for seed in range(10):
            rng = random.Random(seed)  # noqa: S311
            model = RandomWalkSlippage(base_bps=5.0, volatility_factor=1.0, rng=rng)
            ctx = _ctx(market_price=100.0)
            results.add(round(model.compute(ctx), 6))
        assert len(results) > 1

    def test_zero_price_returns_zero(self):
        model = RandomWalkSlippage(base_bps=5.0, volatility_factor=0.0)
        ctx = _ctx(market_price=0.0)
        assert model.compute(ctx) == 0.0


class TestCreateSlippageModel:
    def test_create_fixed_bps(self):
        model = create_slippage_model("fixed_bps")
        assert isinstance(model, FixedBpsSlippage)

    def test_create_percentage(self):
        model = create_slippage_model("percentage")
        assert isinstance(model, PercentageSlippage)

    def test_create_square_root(self):
        model = create_slippage_model("square_root")
        assert isinstance(model, SquareRootSlippage)

    def test_create_volume_weighted(self):
        model = create_slippage_model("volume_weighted")
        assert isinstance(model, VolumeWeightedSlippage)

    def test_create_random_walk(self):
        model = create_slippage_model("random_walk")
        assert isinstance(model, RandomWalkSlippage)

    def test_create_from_enum(self):
        model = create_slippage_model(SlippageModelType.FIXED_BPS)
        assert isinstance(model, FixedBpsSlippage)

    def test_invalid_type_raises(self):
        with pytest.raises(ValueError):
            create_slippage_model("invalid_model")

    def test_passes_kwargs(self):
        model = create_slippage_model("fixed_bps", bps=20.0)
        assert isinstance(model, FixedBpsSlippage)
        assert model.bps == 20.0

    def test_passes_kwargs_square_root(self):
        model = create_slippage_model("square_root", base_bps=10.0, volume_scale=0.5)
        assert isinstance(model, SquareRootSlippage)
        assert model.base_bps == 10.0
        assert model.volume_scale == 0.5


class TestSlippageModelProtocol:
    def test_all_models_are_subclasses(self):
        for cls in [
            FixedBpsSlippage,
            PercentageSlippage,
            SquareRootSlippage,
            VolumeWeightedSlippage,
            RandomWalkSlippage,
        ]:
            assert issubclass(cls, SlippageModel)

    def test_all_models_return_float(self):
        models = [
            FixedBpsSlippage(),
            PercentageSlippage(),
            SquareRootSlippage(),
            VolumeWeightedSlippage(),
            RandomWalkSlippage(rng=random.Random(0)),  # noqa: S311
        ]
        ctx = _ctx(market_price=100.0, avg_volume=50000)
        for model in models:
            result = model.compute(ctx)
            assert isinstance(result, float)
