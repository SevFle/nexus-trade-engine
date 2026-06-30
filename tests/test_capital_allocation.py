"""
Unit tests for :mod:`engine.core.capital_allocation`.

These tests pin two things the Hamilton (largest-remainder) apportioner must
guarantee:

1. **bool rejection** — ``bool`` is a subclass of ``int`` in Python, so
   without an explicit guard it would silently be coerced to ``1`` / ``0``.
   Both ``total_capital`` and every weight must reject ``bool`` with
   :class:`CapitalAllocationError`.
2. **Allocation results are unchanged** — the exact-sum-to-the-cent
   invariant and the deterministic cent distribution (largest fractional
   part wins, ties broken by strategy id ascending) are locked in so any
   internal refactor (e.g. dropping a dead ``raw`` dict) cannot drift the
   observable output.
"""

from __future__ import annotations

import math
from decimal import Decimal
from types import MappingProxyType

import pytest

from engine.core.capital_allocation import (
    CapitalAllocationError,
    allocate_capital,
    allocation_total,
)


# ───────────────────────────────────────────────────────────────────── #
#  bool rejection — the fix under test                                  #
# ───────────────────────────────────────────────────────────────────── #
class TestBoolInputsRejected:
    """``bool`` must never be accepted as a number, anywhere it enters."""

    def test_bool_total_capital_raises(self):
        # bool is a subclass of int, so a naive isinstance(int|float)
        # check would let True through as 1.0. It must raise instead.
        with pytest.raises(CapitalAllocationError, match="total_capital"):
            allocate_capital(True, {"a": 1.0})

    def test_bool_false_total_capital_raises(self):
        with pytest.raises(CapitalAllocationError, match="total_capital"):
            allocate_capital(False, {"a": 1.0})

    def test_bool_weight_raises(self):
        with pytest.raises(CapitalAllocationError, match=r"strategy_weights\['a'\]"):
            allocate_capital(100.0, {"a": True})

    def test_bool_false_weight_raises(self):
        with pytest.raises(CapitalAllocationError, match=r"strategy_weights\['a'\]"):
            allocate_capital(100.0, {"a": False})

    def test_bool_weight_among_valid_weights_raises(self):
        # A single bad weight, even mixed in with good ones, must fail.
        with pytest.raises(CapitalAllocationError, match=r"strategy_weights\['b'\]"):
            allocate_capital(100.0, {"a": 1, "b": True, "c": 2})

    def test_bool_error_is_capital_allocation_error_not_value_error(self):
        # CapitalAllocationError subclasses ValueError, but the raised
        # type must be the specific subclass.
        with pytest.raises(CapitalAllocationError) as exc:
            allocate_capital(1.0, {"a": True})
        assert type(exc.value) is CapitalAllocationError
        assert "bool" in str(exc.value)


# ───────────────────────────────────────────────────────────────────── #
#  allocation results unchanged (regression lock-in)                    #
# ───────────────────────────────────────────────────────────────────── #
class TestAllocationResultsUnchanged:
    """Exact cent-level outputs that must not move when internals change."""

    def test_thirds_of_100_distribute_remainder_to_first_id(self):
        # $100 / 3 → 33.33 each with 1 leftover cent. Ties on the
        # fractional part (all 0.3333...) are broken by id ascending, so
        # "a" receives the extra cent.
        result = allocate_capital(100.0, {"a": 1, "b": 1, "c": 1})
        assert result == {"a": Decimal("33.34"), "b": Decimal("33.33"), "c": Decimal("33.33")}

    def test_largest_fractional_part_wins_the_cent(self):
        # $1.00 / {a:1, b:2} → a=0.33, b=0.66 floors, 1 cent left; b has
        # the larger fractional part (0.6666 > 0.3333) so b takes it.
        result = allocate_capital(1.0, {"a": 1, "b": 2})
        assert result == {"a": Decimal("0.33"), "b": Decimal("0.67")}

    def test_clean_split_has_no_remainder(self):
        result = allocate_capital(100.0, {"a": 0.5, "b": 0.3, "c": 0.2})
        assert result == {
            "a": Decimal("50.00"),
            "b": Decimal("30.00"),
            "c": Decimal("20.00"),
        }

    def test_four_way_split_of_ten(self):
        result = allocate_capital(10.0, {"a": 1, "b": 1, "c": 1, "d": 1})
        assert result == {
            "a": Decimal("2.50"),
            "b": Decimal("2.50"),
            "c": Decimal("2.50"),
            "d": Decimal("2.50"),
        }

    @pytest.mark.parametrize(
        ("total", "weights"),
        [
            (100.0, {"a": 1, "b": 1, "c": 1}),
            (1.0, {"a": 1, "b": 2}),
            (100.0, {"a": 0.5, "b": 0.3, "c": 0.2}),
            (0.01, {"a": 1, "b": 1, "c": 1}),
            (1_000_000.01, {"a": 0.7, "b": 0.3}),
            (33.33, {"long_strategy_name": 0.1, "x": 0.9}),
        ],
    )
    def test_amounts_sum_to_exact_total(self, total, weights):
        # The defining invariant: floored-to-the-cent pieces must always
        # reconstruct total_capital exactly, regardless of how ugly the
        # division is.
        result = allocate_capital(total, weights)
        cents = (Decimal(str(total)) * 100).quantize(Decimal(1))
        assert allocation_total(result) == (cents / 100).quantize(Decimal("0.01"))

    def test_returns_mapping_proxy_type(self):
        result = allocate_capital(10.0, {"a": 1, "b": 1})
        assert isinstance(result, MappingProxyType)

    def test_result_is_immutable(self):
        result = allocate_capital(10.0, {"a": 1, "b": 1})
        with pytest.raises(TypeError):
            result["a"] = Decimal("99.00")
        with pytest.raises(TypeError):
            del result["a"]
        with pytest.raises((TypeError, AttributeError)):
            result.pop("a")

    def test_deterministic_across_calls(self):
        first = allocate_capital(99.99, {"a": 0.37, "b": 0.31, "c": 0.32})
        for _ in range(5):
            assert allocate_capital(99.99, {"a": 0.37, "b": 0.31, "c": 0.32}) == first


# ───────────────────────────────────────────────────────────────────── #
#  other behaviour / error paths                                        #
# ───────────────────────────────────────────────────────────────────── #
class TestAllocationBehaviour:
    def test_single_strategy_takes_everything(self):
        result = allocate_capital(123.45, {"only": 1.0})
        assert result == {"only": Decimal("123.45")}

    def test_zero_weight_strategy_gets_zero(self):
        # A listed strategy with weight 0 is legal and receives $0.00.
        result = allocate_capital(100.0, {"a": 1.0, "b": 0.0})
        assert result["b"] == Decimal("0.00")
        assert allocation_total(result) == Decimal("100.00")

    def test_weights_need_not_normalize(self):
        # Weights are scaled by their sum, so {a:2,b:1} == {a:2/3,b:1/3}.
        result = allocate_capital(90.0, {"a": 2, "b": 1})
        assert result == {"a": Decimal("60.00"), "b": Decimal("30.00")}

    def test_integer_capital_accepted(self):
        # ints are real numbers too (but not bools).
        result = allocate_capital(100, {"a": 1, "b": 1})
        assert allocation_total(result) == Decimal("100.00")

    def test_zero_capital_empty_weights_is_noop(self):
        assert allocate_capital(0.0, {}) == {}

    def test_zero_capital_with_weights_returns_all_zero(self):
        result = allocate_capital(0.0, {"a": 1, "b": 2})
        assert result == {"a": Decimal("0.00"), "b": Decimal("0.00")}

    def test_capital_floored_to_cent(self):
        # Fractional cents below the cent are dropped (ROUND_DOWN).
        result = allocate_capital(10.009, {"a": 1.0})
        assert result == {"a": Decimal("10.00")}


class TestAllocationErrors:
    def test_empty_weights_with_positive_capital_raises(self):
        with pytest.raises(CapitalAllocationError, match="empty but total_capital"):
            allocate_capital(1.0, {})

    def test_all_zero_weights_raises(self):
        with pytest.raises(CapitalAllocationError, match="sum to zero"):
            allocate_capital(100.0, {"a": 0.0, "b": 0.0})

    def test_negative_capital_raises(self):
        with pytest.raises(CapitalAllocationError, match="total_capital must be non-negative"):
            allocate_capital(-1.0, {"a": 1.0})

    def test_negative_weight_raises(self):
        with pytest.raises(CapitalAllocationError, match=r"must be non-negative"):
            allocate_capital(100.0, {"a": 1.0, "b": -0.5})

    def test_nan_capital_raises(self):
        with pytest.raises(CapitalAllocationError, match="finite"):
            allocate_capital(float("nan"), {"a": 1.0})

    def test_inf_capital_raises(self):
        with pytest.raises(CapitalAllocationError, match="finite"):
            allocate_capital(float("inf"), {"a": 1.0})

    def test_nan_weight_raises(self):
        with pytest.raises(CapitalAllocationError, match="finite"):
            allocate_capital(100.0, {"a": 1.0, "b": float("nan")})

    def test_non_numeric_capital_raises(self):
        with pytest.raises(CapitalAllocationError, match="total_capital must be a real number"):
            allocate_capital("100", {"a": 1.0})  # type: ignore[arg-type]

    def test_non_numeric_weight_raises(self):
        with pytest.raises(CapitalAllocationError, match=r"strategy_weights\['a'\]"):
            allocate_capital(100.0, {"a": "half"})  # type: ignore[dict-item]


# ───────────────────────────────────────────────────────────────────── #
#  allocation_total helper                                             #
# ───────────────────────────────────────────────────────────────────── #
class TestAllocationTotal:
    def test_sums_and_quantizes(self):
        result = allocate_capital(100.0, {"a": 1, "b": 1, "c": 1})
        assert allocation_total(result) == Decimal("100.00")

    def test_empty_is_zero(self):
        assert allocation_total({}) == Decimal("0.00")

    def test_isfinite_consistent_with_math_isfinite(self):
        # Sanity check that NaN comparisons really are False in this env,
        # which is the reason _to_decimal uses math.isfinite rather than a
        # bare `< 0` guard.
        assert math.isfinite(float("nan")) is False
        assert math.isfinite(float("inf")) is False
        assert math.isfinite(1.0) is True
