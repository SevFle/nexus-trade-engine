"""Tests for ``engine.core.capital_allocation``.

Three reviewer-flagged fixes drive this suite:

1. **Frozen mapping** — the allocation result must reject in-place
   mutation (``MappingProxyType``).
2. **Hamilton / largest-remainder** — exact-sum invariant using Decimal
   floored to cents, with the remainder distributed to the largest
   fractional parts.
3. **Empty-weights validator** — empty ``strategy_weights`` with
   ``total_capital > 0`` must raise.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal
from types import MappingProxyType

import pytest

from engine.core.capital_allocation import (
    CapitalAllocationError,
    allocate_capital,
    allocation_total,
)

# ---------------------------------------------------------------------------
# (1) Frozen mapping — in-place mutation must raise
# ---------------------------------------------------------------------------


class TestFrozenMapping:
    """Fix #1: the returned mapping is immutable."""

    def test_returned_type_is_mappingproxy(self) -> None:
        result = allocate_capital(100.0, {"a": 1.0, "b": 1.0})
        assert isinstance(result, MappingProxyType)

    def test_item_assignment_raises(self) -> None:
        result = allocate_capital(100.0, {"a": 1.0, "b": 1.0})
        with pytest.raises((TypeError, AttributeError)):
            result["a"] = Decimal("999.99")  # type: ignore[index]

    def test_item_deletion_raises(self) -> None:
        result = allocate_capital(100.0, {"a": 1.0, "b": 1.0})
        with pytest.raises((TypeError, AttributeError)):
            del result["a"]  # type: ignore[misc]

    def test_clear_raises(self) -> None:
        result = allocate_capital(100.0, {"a": 1.0, "b": 1.0})
        with pytest.raises((TypeError, AttributeError)):
            result.clear()  # type: ignore[attr-defined]

    def test_pop_raises(self) -> None:
        result = allocate_capital(100.0, {"a": 1.0, "b": 1.0})
        with pytest.raises((TypeError, AttributeError)):
            result.pop("a")  # type: ignore[attr-defined]

    def test_mutation_does_not_leak_into_internal_state(self) -> None:
        """If a caller *could* mutate the source dict, a second call
        must still be correct. Since the result is frozen there is no
        way to corrupt the callee, but we verify the input dict itself
        is left untouched (defensive copy semantics)."""
        weights = {"a": 1.0, "b": 2.0}
        weights_copy = dict(weights)
        allocate_capital(100.0, weights)
        assert weights == weights_copy, "input weights dict was mutated"


# ---------------------------------------------------------------------------
# (2) Hamilton / largest-remainder — exact-sum invariant
# ---------------------------------------------------------------------------


class TestLargestRemainder:
    """Fix #2: Decimal apportionment, exact to the cent."""

    def test_values_are_decimal_quantized_to_cents(self) -> None:
        result = allocate_capital(100.0, {"a": 1.0, "b": 2.0})
        for value in result.values():
            assert isinstance(value, Decimal)
            assert value == value.quantize(Decimal("0.01"))
            assert value.as_tuple().exponent == -2

    def test_exact_sum_three_strategies_equal_weights(self) -> None:
        """$100 across 3 equal weights: 100/3 = 33.33(3) — classic
        penny-can't-divide case. Sum must be exactly $100.00."""
        result = allocate_capital(100.0, {"a": 1.0, "b": 1.0, "c": 1.0})
        assert allocation_total(result) == Decimal("100.00")
        # one strategy absorbs the leftover cent (33.34), two get 33.33
        values = sorted(result.values(), reverse=True)
        assert values == [Decimal("33.34"), Decimal("33.33"), Decimal("33.33")]

    def test_exact_sum_three_strategies_one_third_weights(self) -> None:
        """Same invariant with fractional 1/3 weights (sum to 1.0)."""
        third = 1.0 / 3.0
        result = allocate_capital(100.0, {"a": third, "b": third, "c": third})
        assert allocation_total(result) == Decimal("100.00")

    @pytest.mark.parametrize("n", list(range(1, 13)))
    def test_exact_sum_invariant_across_n_strategies(self, n: int) -> None:
        """For any N in 1..12 with equal weights, the allocation of
        $100.00 must sum to exactly $100.00 (the core invariant)."""
        weights = {f"s{i}": 1.0 for i in range(n)}
        result = allocate_capital(100.00, weights)
        assert allocation_total(result) == Decimal("100.00")

    @pytest.mark.parametrize(
        ("total", "n"),
        [
            (0.03, 7),  # 3 cents / 7 strategies — minimal remainder case
            (1.00, 3),
            (10.00, 6),
            (99.99, 11),
            (1234.56, 9),
        ],
    )
    def test_exact_sum_awkward_amounts(self, total: float, n: int) -> None:
        weights = {f"s{i}": float(i + 1) for i in range(n)}
        result = allocate_capital(total, weights)
        expected_cents = int(
            (Decimal(str(total)) * 100).quantize(Decimal(1), rounding=ROUND_DOWN)
        )
        expected = (Decimal(expected_cents) / 100).quantize(Decimal("0.01"))
        assert allocation_total(result) == expected

    def test_remainder_goes_to_largest_fractional_part(self) -> None:
        """With weights {a:1, b:2, c:3} and $100:
        raw cents = 1666.67, 3333.33, 5000.00
        floors      = 1666,    3333,    5000   (sum 9999, remainder 1)
        The single leftover cent goes to ``a`` (largest fractional .67)."""
        result = allocate_capital(100.0, {"a": 1.0, "b": 2.0, "c": 3.0})
        assert result["a"] == Decimal("16.67")
        assert result["b"] == Decimal("33.33")
        assert result["c"] == Decimal("50.00")
        assert allocation_total(result) == Decimal("100.00")

    def test_single_strategy_gets_everything(self) -> None:
        result = allocate_capital(1234.56, {"only": 7.0})
        assert result["only"] == Decimal("1234.56")
        assert allocation_total(result) == Decimal("1234.56")

    def test_zero_weight_strategy_gets_zero(self) -> None:
        result = allocate_capital(100.0, {"a": 1.0, "b": 0.0})
        assert result["a"] == Decimal("100.00")
        assert result["b"] == Decimal("0.00")
        assert allocation_total(result) == Decimal("100.00")

    def test_proportionality_for_clean_split(self) -> None:
        """23/77 split of $100 lands exactly on the cent — no remainder."""
        result = allocate_capital(100.0, {"a": 23.0, "b": 77.0})
        assert result["a"] == Decimal("23.00")
        assert result["b"] == Decimal("77.00")

    def test_sub_cent_total_is_floored_not_rounded(self) -> None:
        """$0.009 floors to 0 cents → nothing to distribute."""
        result = allocate_capital(0.009, {"a": 1.0, "b": 1.0})
        # total floored to 0 cents
        assert allocation_total(result) == Decimal("0.00")

    def test_deterministic_tie_break(self) -> None:
        """Equal fractional parts break ties by strategy id ascending,
        so repeated calls are byte-identical."""
        first = allocate_capital(0.02, {"b": 1.0, "a": 1.0, "c": 1.0})
        second = allocate_capital(0.02, {"b": 1.0, "a": 1.0, "c": 1.0})
        assert dict(first) == dict(second)
        # 2 cents, 3 strategies: raw = 0.667 each, floors all 0,
        # remainder 2 → top two by (frac desc, id asc) = a, b.
        assert first["a"] == Decimal("0.01")
        assert first["b"] == Decimal("0.01")
        assert first["c"] == Decimal("0.00")

    def test_float_drift_does_not_break_invariant(self) -> None:
        """0.1 + 0.2 + 0.3 style float drift must not corrupt the sum."""
        total = 0.1 + 0.2  # 0.30000000000000004
        result = allocate_capital(total, {"a": 1.0, "b": 1.0, "c": 1.0})
        assert allocation_total(result) == Decimal("0.30")


# ---------------------------------------------------------------------------
# (3) Empty-weights validator
# ---------------------------------------------------------------------------


class TestEmptyWeightsValidator:
    """Fix #3: reject empty strategy_weights when total_capital > 0."""

    def test_empty_weights_positive_capital_raises(self) -> None:
        with pytest.raises(CapitalAllocationError, match="empty but total_capital > 0"):
            allocate_capital(100.0, {})

    def test_empty_weights_zero_capital_is_ok(self) -> None:
        result = allocate_capital(0.0, {})
        assert isinstance(result, MappingProxyType)
        assert len(result) == 0

    def test_empty_weights_negative_capital_raises_first(self) -> None:
        """Negative capital is its own error and must be caught before
        the empty-weights check would matter."""
        with pytest.raises(CapitalAllocationError, match="non-negative"):
            allocate_capital(-1.0, {})

    def test_message_mentions_strategies(self) -> None:
        with pytest.raises(CapitalAllocationError, match="strategies"):
            allocate_capital(1.0, {})


# ---------------------------------------------------------------------------
# Input validation — bad weights / bad capital
# ---------------------------------------------------------------------------


class TestInputValidation:
    def test_negative_weight_raises(self) -> None:
        with pytest.raises(CapitalAllocationError, match="non-negative"):
            allocate_capital(100.0, {"a": 1.0, "b": -0.5})

    def test_nan_weight_raises(self) -> None:
        with pytest.raises(CapitalAllocationError, match="finite"):
            allocate_capital(100.0, {"a": float("nan")})

    def test_inf_weight_raises(self) -> None:
        with pytest.raises(CapitalAllocationError, match="finite"):
            allocate_capital(100.0, {"a": float("inf")})

    def test_all_zero_weights_raises(self) -> None:
        with pytest.raises(CapitalAllocationError, match="sum to zero"):
            allocate_capital(100.0, {"a": 0.0, "b": 0.0})

    def test_nan_capital_raises(self) -> None:
        with pytest.raises(CapitalAllocationError, match="finite"):
            allocate_capital(float("nan"), {"a": 1.0})

    def test_inf_capital_raises(self) -> None:
        with pytest.raises(CapitalAllocationError, match="finite"):
            allocate_capital(float("inf"), {"a": 1.0})

    def test_negative_capital_raises(self) -> None:
        with pytest.raises(CapitalAllocationError, match="non-negative"):
            allocate_capital(-50.0, {"a": 1.0})

    def test_non_numeric_weight_raises(self) -> None:
        with pytest.raises(CapitalAllocationError, match="real number"):
            allocate_capital(100.0, {"a": "half"})  # type: ignore[dict-item]

    def test_zero_capital_with_weights_returns_all_zero(self) -> None:
        result = allocate_capital(0.0, {"a": 1.0, "b": 2.0})
        assert result["a"] == Decimal("0.00")
        assert result["b"] == Decimal("0.00")
        assert allocation_total(result) == Decimal("0.00")


# ---------------------------------------------------------------------------
# Property-style cross-check: invariant never violated for random splits
# ---------------------------------------------------------------------------


class TestInvariantProperty:
    @pytest.mark.parametrize(
        ("total", "weights"),
        [
            (1000.00, {"a": 0.5, "b": 0.5}),
            (333.33, {"x": 1.0, "y": 1.0, "z": 1.0, "w": 1.0}),
            (1.00, {"a": 0.1, "b": 0.2, "c": 0.3, "d": 0.4}),
            (999999.99, {"big": 99.0, "small": 1.0}),
            (0.07, {f"s{i}": float(i + 1) for i in range(5)}),
        ],
    )
    def test_sum_equals_total_to_the_cent(
        self, total: float, weights: dict[str, float]
    ) -> None:
        result = allocate_capital(total, weights)
        # every value is a cent-multiple Decimal
        for value in result.values():
            assert value.as_tuple().exponent == -2
            assert value >= 0
        # exact sum equals the (floored) total
        expected_cents = int(
            (Decimal(str(total)) * 100).quantize(Decimal(1), rounding=ROUND_DOWN)
        )
        expected = (Decimal(expected_cents) / 100).quantize(Decimal("0.01"))
        assert allocation_total(result) == expected
