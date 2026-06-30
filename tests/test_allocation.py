"""Tests for engine.portfolio.allocation.CapitalAllocation.

Covers: valid multi-strategy allocation, weight-sum / sign / max-strategies
constraints, NaN/Inf rejection (comparison-bypass guard), get_allocation
correctness (known + unknown strategy, cent quantisation), total_allocated,
and edge cases (zero capital, single strategy, empty allocation,
validate_assignment).
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from engine.portfolio.allocation import CapitalAllocation

# --------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------- #


def _allocation(
    weights: dict[str, float],
    capital: Decimal | str = "100000.00",
    max_strategies: int = 50,
) -> CapitalAllocation:
    return CapitalAllocation(
        strategy_weights=dict(weights),
        total_capital=Decimal(str(capital)) if not isinstance(capital, Decimal) else capital,
        max_strategies=max_strategies,
    )


# --------------------------------------------------------------------- #
# Valid construction
# --------------------------------------------------------------------- #


def test_valid_equal_split_three_strategies() -> None:
    alloc = _allocation({"alpha": 1 / 3, "beta": 1 / 3, "gamma": 1 / 3})
    assert set(alloc.strategy_weights) == {"alpha", "beta", "gamma"}
    assert alloc.total_capital == Decimal("100000.00")


def test_valid_round_weight_split() -> None:
    alloc = _allocation({"alpha": 0.5, "beta": 0.5})
    assert alloc.strategy_weights == {"alpha": 0.5, "beta": 0.5}


def test_single_strategy_weight_one_is_valid() -> None:
    alloc = _allocation({"alpha": 1.0})
    assert alloc.strategy_weights == {"alpha": 1.0}


def test_empty_weights_is_valid_default_state() -> None:
    alloc = CapitalAllocation(total_capital=Decimal("1000.00"))
    assert alloc.strategy_weights == {}
    assert alloc.get_allocation("alpha") == Decimal("0.00")
    assert alloc.total_allocated() == Decimal("0")


def test_default_max_strategies_is_fifty() -> None:
    # 50 equal slices of 0.02; float drift absorbed by the epsilon guard.
    weights = {f"s{i}": 0.02 for i in range(50)}
    alloc = CapitalAllocation(
        strategy_weights=weights, total_capital=Decimal("1.00")
    )
    assert len(alloc.strategy_weights) == 50


# --------------------------------------------------------------------- #
# Invalid weights
# --------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "weights",
    [
        {"alpha": -0.1, "beta": 1.1},  # negative + compensating >1
        {"alpha": -0.5, "beta": 1.5},
    ],
    ids=["negative-and-over-one", "deeper-negative"],
)
def test_negative_weight_rejected(weights: dict[str, float]) -> None:
    with pytest.raises(ValidationError, match=r"non-negative|<= 1.0"):
        _allocation(weights)


@pytest.mark.parametrize(
    "weights",
    [
        {"alpha": 0.5, "beta": 0.6},  # sum 1.1
        {"alpha": 0.3, "beta": 0.3},  # sum 0.6
        {"alpha": 0.9},  # single strategy, not 1.0
    ],
    ids=["sum-too-high", "sum-too-low", "single-not-one"],
)
def test_sum_not_one(weights: dict[str, float]) -> None:
    with pytest.raises(ValidationError, match=r"sum to 1.0"):
        _allocation(weights)


def test_weight_above_one_rejected() -> None:
    with pytest.raises(ValidationError, match=r"<= 1.0"):
        _allocation({"alpha": 1.2, "beta": -0.2})


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_weight_rejected(bad: float) -> None:
    # NaN/Inf bypass plain comparison guards (nan < 0 is False); the field
    # validator processes alpha first and must reject it on the finite check
    # before the model-level sum validator ever runs.
    with pytest.raises(ValidationError, match="finite"):
        _allocation({"alpha": bad, "beta": 1.0})


def test_blank_strategy_id_rejected() -> None:
    with pytest.raises(ValidationError, match="non-empty string"):
        _allocation({"": 1.0})


def test_negative_total_capital_rejected() -> None:
    with pytest.raises(ValidationError):
        CapitalAllocation(
            strategy_weights={"alpha": 1.0}, total_capital=Decimal("-1.00")
        )


# --------------------------------------------------------------------- #
# max_strategies constraint
# --------------------------------------------------------------------- #


def test_exceeding_max_strategies_rejected() -> None:
    weights = {"a": 0.5, "b": 0.3, "c": 0.2}  # 3 strategies, sum 1.0
    with pytest.raises(ValidationError, match=r"too many strategies: 3 > .* 2"):
        _allocation(weights, max_strategies=2)


def test_exactly_at_max_strategies_ok() -> None:
    weights = {"a": 0.5, "b": 0.5}  # exactly max_strategies=2
    alloc = _allocation(weights, max_strategies=2)
    assert len(alloc.strategy_weights) == 2


# --------------------------------------------------------------------- #
# get_allocation
# --------------------------------------------------------------------- #


def test_get_allocation_known_strategy() -> None:
    alloc = _allocation({"alpha": 0.25, "beta": 0.75}, capital="10000.00")
    assert alloc.get_allocation("alpha") == Decimal("2500.00")
    assert alloc.get_allocation("beta") == Decimal("7500.00")


def test_get_allocation_unknown_strategy_is_zero() -> None:
    alloc = _allocation({"alpha": 1.0}, capital="5000.00")
    assert alloc.get_allocation("nope") == Decimal("0.00")


def test_get_allocation_quantises_to_cents() -> None:
    # 1000.00 * 0.3333 = 333.30 after cent quantisation.
    alloc = _allocation({"alpha": 0.3333, "beta": 0.6667}, capital="1000.00")
    assert alloc.get_allocation("alpha") == Decimal("333.30")
    # 1000 * 0.6667 = 666.70
    assert alloc.get_allocation("beta") == Decimal("666.70")


def test_get_allocation_zero_weight_is_zero() -> None:
    alloc = _allocation({"alpha": 1.0, "beta": 0.0}, capital="1000.00")
    assert alloc.get_allocation("beta") == Decimal("0.00")


# --------------------------------------------------------------------- #
# total_allocated
# --------------------------------------------------------------------- #


def test_total_allocated_equals_capital_for_equal_split() -> None:
    alloc = _allocation({"alpha": 0.5, "beta": 0.5}, capital="1000.00")
    assert alloc.total_allocated() == Decimal("1000.00")


def test_total_allocated_with_carrying_rounding() -> None:
    # 1/3 slices: each allocation rounds to cents; total should still land
    # within a cent of total_capital.
    alloc = _allocation(
        {"a": 1 / 3, "b": 1 / 3, "c": 1 / 3}, capital="9999.99"
    )
    diff = abs(alloc.total_allocated() - Decimal("9999.99"))
    assert diff <= Decimal("0.03")


# --------------------------------------------------------------------- #
# Edge cases
# --------------------------------------------------------------------- #


def test_zero_capital_single_strategy() -> None:
    alloc = _allocation({"alpha": 1.0}, capital="0")
    assert alloc.get_allocation("alpha") == Decimal("0.00")
    assert alloc.total_allocated() == Decimal("0")


def test_total_capital_accepts_int_and_str_coercion() -> None:
    alloc = CapitalAllocation(strategy_weights={"alpha": 1.0}, total_capital=1000)
    assert alloc.total_capital == Decimal(1000)
    assert alloc.get_allocation("alpha") == Decimal("1000.00")


def test_validate_assignment_rejects_bad_update() -> None:
    alloc = _allocation({"alpha": 1.0})
    with pytest.raises(ValidationError, match=r"sum to 1.0"):
        alloc.strategy_weights = {"alpha": 0.5, "beta": 0.4}


def test_weights_sums_to_one_with_float_noise_tolerated() -> None:
    # Classic 0.1+0.2+0.3+0.4 float drift must pass the epsilon guard.
    alloc = _allocation({"a": 0.1, "b": 0.2, "c": 0.3, "d": 0.4})
    assert len(alloc.strategy_weights) == 4
