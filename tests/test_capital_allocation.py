"""Focused unit tests for ``engine.core.capital_allocation``.

Covers the public ``allocate_capital`` / ``allocation_total`` API:

* input type validation (booleans, strings, non-finite, negatives)
* zero / negative capital
* single-asset precision (100% allocation, exact to the cent)
* multi-asset *largest-remainder* (Hamilton) apportionment correctness,
  including the sum invariant and deterministic tie-breaking
* empty ``strategy_weights`` (rejected with positive capital; a no-op
  with zero capital)
* a 150-asset stress test verifying the exact-sum invariant and that the
  remainder is distributed to the assets with the largest fractions
* immutability of the returned ``MappingProxyType``
"""

from __future__ import annotations

from decimal import Decimal
from types import MappingProxyType

import pytest

from engine.core.capital_allocation import (
    CapitalAllocationError,
    allocate_capital,
    allocation_total,
)

CENT = Decimal("0.01")


def _sum_cents(allocation: dict[str, Decimal]) -> int:
    """Return the sum of an allocation expressed in whole cents."""
    return int(allocation_total(allocation) * 100)


# ---------------------------------------------------------------------------
# Input type validation
# ---------------------------------------------------------------------------
class TestTypeValidation:
    """``allocate_capital`` must reject non-real inputs with the public error."""

    @pytest.mark.parametrize("capital", [True, False])
    def test_boolean_capital_rejected(self, capital: bool) -> None:
        # bool is a subclass of int; it must NOT be treated as 1/0 dollars.
        with pytest.raises(CapitalAllocationError, match="total_capital"):
            allocate_capital(capital, {"a": 1.0})

    @pytest.mark.parametrize("weight", [True, False])
    def test_boolean_weight_rejected(self, weight: bool) -> None:
        with pytest.raises(CapitalAllocationError, match="strategy_weights"):
            allocate_capital(100.0, {"a": weight})

    def test_string_capital_rejected(self) -> None:
        with pytest.raises(CapitalAllocationError, match="real number"):
            allocate_capital("100.0", {"a": 1.0})  # type: ignore[arg-type]

    def test_none_capital_rejected(self) -> None:
        with pytest.raises(CapitalAllocationError, match="real number"):
            allocate_capital(None, {"a": 1.0})  # type: ignore[arg-type]

    def test_non_finite_capital_rejected(self) -> None:
        for bad in (float("nan"), float("inf"), float("-inf")):
            with pytest.raises(CapitalAllocationError, match="finite"):
                allocate_capital(bad, {"a": 1.0})

    def test_non_finite_weight_rejected(self) -> None:
        for bad in (float("nan"), float("inf"), float("-inf")):
            with pytest.raises(CapitalAllocationError, match="finite"):
                allocate_capital(100.0, {"a": bad})


# ---------------------------------------------------------------------------
# Capital edge cases: zero, negative, sub-cent flooring
# ---------------------------------------------------------------------------
class TestCapitalBoundaries:
    def test_negative_capital_rejected(self) -> None:
        with pytest.raises(CapitalAllocationError, match="non-negative"):
            allocate_capital(-5.0, {"a": 1.0})

    def test_zero_capital_allocates_all_zero(self) -> None:
        result = allocate_capital(0.0, {"a": 1.0, "b": 2.0, "c": 0.5})
        assert dict(result) == {"a": Decimal("0.00"), "b": Decimal("0.00"), "c": Decimal("0.00")}
        assert allocation_total(result) == Decimal("0.00")

    def test_sub_cent_capital_is_floored_to_cent(self) -> None:
        # $100.999 has 3 fractional cents; documented behaviour floors to $100.99.
        result = allocate_capital(100.999, {"a": 1.0})
        assert result["a"] == Decimal("100.99")
        assert allocation_total(result) == Decimal("100.99")


# ---------------------------------------------------------------------------
# Single-asset precision
# ---------------------------------------------------------------------------
class TestSingleAsset:
    def test_single_asset_gets_exactly_the_capital(self) -> None:
        result = allocate_capital(100.0, {"only": 1.0})
        assert result["only"] == Decimal("100.00")
        assert allocation_total(result) == Decimal("100.00")

    def test_single_asset_weight_greater_than_one(self) -> None:
        # Weight is normalised internally; a weight of 3.0 is the same as 1.0.
        result = allocate_capital(50.0, {"only": 3.0})
        assert result["only"] == Decimal("50.00")

    def test_single_asset_with_zero_weight_listed(self) -> None:
        # A listed zero-weight strategy simply receives $0.00.
        result = allocate_capital(100.0, {"real": 1.0, "empty": 0.0})
        assert result["real"] == Decimal("100.00")
        assert result["empty"] == Decimal("0.00")


# ---------------------------------------------------------------------------
# Largest-remainder / Hamilton apportionment correctness
# ---------------------------------------------------------------------------
class TestLargestRemainder:
    def test_three_way_equal_split_is_exact_to_the_cent(self) -> None:
        # $100 / 3 = $33.333... -> floor 33.33 each (99.99), remainder 1 cent.
        # All fractions tie, so the extra cent goes to the smallest id ("a").
        result = allocate_capital(100.0, {"a": 1.0, "b": 1.0, "c": 1.0})
        assert result["a"] == Decimal("33.34")
        assert result["b"] == Decimal("33.33")
        assert result["c"] == Decimal("33.33")
        assert allocation_total(result) == Decimal("100.00")

    def test_extra_cent_goes_to_largest_fractional_part(self) -> None:
        # weights sum to 1.0; capital = $0.03 (3 cents)
        #   raw: a=1.2  b=1.05  c=0.75
        #   floor: 1, 1, 0 -> remainder 1 cent
        #   fracs: a=0.2  b=0.05  c=0.75  -> c has the largest fraction
        # naive proportional rounding would also starve 'c'; the Hamilton
        # method must instead bump 'c' (largest remainder) to 1 cent.
        result = allocate_capital(0.03, {"a": 0.4, "b": 0.35, "c": 0.25})
        assert result["a"] == Decimal("0.01")
        assert result["b"] == Decimal("0.01")
        assert result["c"] == Decimal("0.01")
        assert allocation_total(result) == Decimal("0.03")

    def test_tie_break_is_ascending_strategy_id(self) -> None:
        # 1 cent across three equal weights; fractions tie, so the
        # lexicographically smallest id wins regardless of insertion order.
        result = allocate_capital(0.01, {"zulu": 1.0, "alpha": 1.0, "mid": 1.0})
        assert result["alpha"] == Decimal("0.01")
        assert result["mid"] == Decimal("0.00")
        assert result["zulu"] == Decimal("0.00")

    def test_apportionment_is_deterministic(self) -> None:
        weights = {"a": 0.37, "b": 0.33, "c": 0.20, "d": 0.10}
        first = dict(allocate_capital(1_000_000.00, weights))
        # Run many times; result must be byte-for-byte identical.
        for _ in range(50):
            again = dict(allocate_capital(1_000_000.00, weights))
            assert again == first


# ---------------------------------------------------------------------------
# Empty weights
# ---------------------------------------------------------------------------
class TestEmptyWeights:
    def test_empty_weights_positive_capital_raises(self) -> None:
        with pytest.raises(CapitalAllocationError, match="empty but total_capital"):
            allocate_capital(100.0, {})

    def test_empty_weights_zero_capital_is_noop(self) -> None:
        # Empty weights + zero capital is a well-formed no-op (not an error).
        result = allocate_capital(0.0, {})
        assert len(result) == 0
        assert allocation_total(result) == Decimal("0.00")

    def test_all_zero_weights_raises(self) -> None:
        with pytest.raises(CapitalAllocationError, match="sum to zero"):
            allocate_capital(100.0, {"a": 0.0, "b": 0.0})

    def test_negative_weight_rejected(self) -> None:
        with pytest.raises(CapitalAllocationError, match="non-negative"):
            allocate_capital(100.0, {"a": 1.0, "b": -0.5})


# ---------------------------------------------------------------------------
# Rounding sum invariant (incl. 100+ asset stress test)
# ---------------------------------------------------------------------------
class TestSumInvariant:
    @pytest.mark.parametrize(
        ("capital", "weights"),
        [
            (0.01, {"a": 1.0, "b": 1.0}),
            (1.00, {"a": 1.0, "b": 1.0, "c": 1.0}),
            (100.00, {"a": 0.4, "b": 0.35, "c": 0.25}),
            (33.33, {"a": 0.5, "b": 0.5}),
            (9_999_999.99, {"a": 0.1, "b": 0.2, "c": 0.3, "d": 0.4}),
        ],
    )
    def test_sums_equal_capital_to_the_cent(
        self, capital: float, weights: dict[str, float]
    ) -> None:
        result = allocate_capital(capital, weights)
        expected = (Decimal(str(capital))).quantize(CENT)
        assert allocation_total(result) == expected

    def test_each_amount_quantized_to_cent(self) -> None:
        result = allocate_capital(100.00, {"a": 0.4, "b": 0.35, "c": 0.25})
        for amount in result.values():
            assert amount.as_tuple().exponent == -2  # exactly 2 decimal places

    def test_stress_150_assets_sum_and_apportionment(self) -> None:
        # 150 equal-weight strategies splitting $100.00 produces a large
        # remainder: each floor is 66c (150 * 66 = 9900c), leaving a
        # 100-cent remainder. All fractions tie, so tie-break by ascending
        # id means the first 100 ids get bumped to 67c and the last 50 stay
        # at 66c.
        n = 150
        ids = [f"s{i:03d}" for i in range(n)]
        weights = dict.fromkeys(ids, 1.0)
        result = allocate_capital(100.00, weights)

        # 1. Exact-sum invariant holds even with a 100-cent remainder.
        assert allocation_total(result) == Decimal("100.00")
        assert _sum_cents(dict(result)) == 10_000

        # 2. The full remainder (100 cents) is distributed, no cent lost.
        bumped = [sid for sid in ids if result[sid] == Decimal("0.67")]
        kept = [sid for sid in ids if result[sid] == Decimal("0.66")]
        assert len(bumped) == 100
        assert len(kept) == 50

        # 3. Largest-remainder tie-break: bumped ids are the 100 smallest.
        assert bumped == ids[:100]
        assert kept == ids[100:]

    def test_stress_unequal_weights_sum_invariant(self) -> None:
        # 120 strategies with wildly unequal (but deterministic) weights;
        # only the exact-sum invariant is asserted (no cent lost/created).
        weights = {f"s{i:03d}": (i + 1) * 0.013 for i in range(120)}
        capital = 12_345.67
        result = allocate_capital(capital, weights)
        assert allocation_total(result) == (Decimal(str(capital))).quantize(CENT)


# ---------------------------------------------------------------------------
# Return-type / immutability
# ---------------------------------------------------------------------------
class TestReturnType:
    def test_returns_mappingproxy(self) -> None:
        result = allocate_capital(100.0, {"a": 1.0})
        assert isinstance(result, MappingProxyType)

    def test_result_is_immutable(self) -> None:
        result = allocate_capital(100.0, {"a": 1.0, "b": 1.0})
        # Item-level mutation raises TypeError (read-only mapping).
        with pytest.raises(TypeError):
            result["a"] = Decimal("5.00")  # type: ignore[index]
        with pytest.raises(TypeError):
            del result["a"]  # type: ignore[misc]
        # The mutable-mapping methods (pop / clear) don't even exist on a
        # ``MappingProxyType``; referencing them raises AttributeError.
        # Either way, the allocation cannot be mutated in place.
        with pytest.raises(AttributeError):
            result.pop("a")  # type: ignore[attr-defined]
        with pytest.raises(AttributeError):
            result.clear()  # type: ignore[attr-defined]
        # And nothing above changed the underlying data.
        assert dict(result) == {"a": Decimal("50.00"), "b": Decimal("50.00")}

    def test_allocation_total_of_empty_is_zero(self) -> None:
        assert allocation_total({}) == Decimal("0.00")
