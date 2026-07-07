"""Tests for :mod:`engine.portfolio.allocation` (the frozen Pydantic
:class:`CapitalAllocation` value object).

These cover the import re-export in ``engine.portfolio`` as well as the full
validator surface, immutability semantics, the validation-re-running
``model_copy`` override, and the cent-quantised allocation math.

Note: this is a *different* module from ``engine.core.capital_allocation``
(Hamilton largest-remainder apportioner exercised by
``test_capital_allocation.py``); here we test the declarative capital-split
value object.
"""

from __future__ import annotations

import math
from decimal import Decimal

import pytest
from pydantic import ValidationError

from engine.portfolio import CapitalAllocation as CapitalAllocationFromPackage
from engine.portfolio.allocation import (
    CapitalAllocation,
)


# Re-export sanity (covers engine/portfolio/__init__.py lines 3-7).
def test_package_reexports_allocation_model():
    assert CapitalAllocationFromPackage is CapitalAllocation


# ── construction / defaults ────────────────────────────────────────── #
class TestConstruction:
    def test_defaults_are_valid_empty_allocation(self):
        alloc = CapitalAllocation()
        assert alloc.strategy_weights == {}
        assert alloc.total_capital == Decimal("0")
        assert alloc.max_strategies == 50

    def test_valid_weights_summing_to_one(self):
        alloc = CapitalAllocation(
            strategy_weights={"a": 0.5, "b": 0.3, "c": 0.2},
            total_capital=Decimal("1000"),
        )
        assert alloc.strategy_weights == {"a": 0.5, "b": 0.3, "c": 0.2}

    def test_integer_weights_are_coerced_to_float(self):
        # int weights are accepted (they are real numbers, not bools) and
        # stored as floats. A single weight of 1 is the canonical valid
        # integer allocation (three 1s would sum to 3.0 and rightly fail the
        # sum-to-1.0 rule).
        alloc = CapitalAllocation(strategy_weights={"a": 1})
        assert alloc.strategy_weights == {"a": 1.0}
        assert all(isinstance(v, float) for v in alloc.strategy_weights.values())

    def test_total_capital_accepts_int(self):
        # int is a valid Decimal-able value for a Decimal field.
        alloc = CapitalAllocation(total_capital=100)
        assert alloc.total_capital == Decimal("100")

    def test_empty_weights_with_positive_capital_is_valid(self):
        # An empty allocation represents "not yet deployed"; sum-to-1.0 only
        # applies once at least one weight is present.
        alloc = CapitalAllocation(total_capital=Decimal("1000"))
        assert alloc.strategy_weights == {}

    def test_single_strategy_weight_one(self):
        alloc = CapitalAllocation(strategy_weights={"only": 1.0})
        assert alloc.strategy_weights == {"only": 1.0}


# ── per-field weight validation ───────────────────────────────────── #
class TestWeightValidation:
    def test_blank_strategy_id_rejected(self):
        with pytest.raises(ValidationError, match="non-empty string"):
            CapitalAllocation(strategy_weights={"": 1.0})

    def test_whitespace_strategy_id_rejected(self):
        with pytest.raises(ValidationError, match="non-empty string"):
            CapitalAllocation(strategy_weights={"   ": 1.0})

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_weight_rejected(self, bad):
        with pytest.raises(ValidationError, match="finite"):
            CapitalAllocation(strategy_weights={"a": bad})

    def test_negative_weight_rejected(self):
        with pytest.raises(ValidationError, match="non-negative"):
            CapitalAllocation(strategy_weights={"a": 1.5, "b": -0.5})

    def test_weight_above_one_rejected(self):
        with pytest.raises(ValidationError, match=r"<= 1\.0"):
            CapitalAllocation(strategy_weights={"a": 1.5})

    def test_bool_weight_rejected(self):
        # bool is a subclass of int; the explicit guard must reject it.
        with pytest.raises(ValidationError, match="must be a number"):
            CapitalAllocation(strategy_weights={"a": True})

    def test_none_weight_rejected(self):
        with pytest.raises(ValidationError, match="must be a number"):
            CapitalAllocation(strategy_weights={"a": None})

    def test_non_numeric_weight_rejected(self):
        with pytest.raises(ValidationError, match="must be a number"):
            CapitalAllocation(strategy_weights={"a": "half"})  # type: ignore[dict-item]


# ── cross-field validation ────────────────────────────────────────── #
class TestCrossFieldValidation:
    def test_weights_not_summing_to_one_rejected(self):
        with pytest.raises(ValidationError, match=r"must sum to 1\.0"):
            CapitalAllocation(strategy_weights={"a": 0.5, "b": 0.4})

    def test_weights_summing_within_epsilon_accepted(self):
        # Float noise under the 1e-9 epsilon tolerance must be accepted.
        # 0.3 + 0.3 + 0.3 + 0.1 is NOT exactly 1.0 in IEEE-754 (it lands on
        # 0.9999999999999999), but the ~1e-16 shortfall is far inside epsilon.
        # (0.1+0.2+0.7 happens to round to exactly 1.0 on CPython, so it is a
        # poor fixture here — it would make the noise-absorption assertion
        # vacuous.)
        raw_sum = 0.3 + 0.3 + 0.3 + 0.1
        assert raw_sum != 1.0  # sanity: there really is float noise here
        alloc = CapitalAllocation(strategy_weights={"a": 0.3, "b": 0.3, "c": 0.3, "d": 0.1})
        assert alloc.strategy_weights == {"a": 0.3, "b": 0.3, "c": 0.3, "d": 0.1}
        assert math.isclose(sum(alloc.strategy_weights.values()), 1.0)

    def test_too_many_strategies_rejected(self):
        # max_strategies defaults to 50; explicitly set a small cap.
        weights = {f"s{i}": 0.0 for i in range(4)}
        weights["s0"] = 1.0  # sum to one
        with pytest.raises(ValidationError, match="too many strategies"):
            CapitalAllocation(strategy_weights=weights, max_strategies=3)

    def test_max_strategies_must_be_at_least_one(self):
        with pytest.raises(ValidationError):
            CapitalAllocation(max_strategies=0)

    def test_total_capital_must_be_non_negative(self):
        with pytest.raises(ValidationError):
            CapitalAllocation(total_capital=Decimal("-1"))


# ── immutability ──────────────────────────────────────────────────── #
class TestImmutability:
    def _alloc(self) -> CapitalAllocation:
        return CapitalAllocation(
            strategy_weights={"a": 0.6, "b": 0.4}, total_capital=Decimal("1000")
        )

    def test_field_assignment_blocked(self):
        alloc = self._alloc()
        with pytest.raises(ValidationError):
            alloc.total_capital = Decimal("2000")  # type: ignore[misc]
        with pytest.raises(ValidationError):
            alloc.strategy_weights = {"a": 1.0}  # type: ignore[misc]

    def test_mapping_mutation_blocked(self):
        alloc = self._alloc()
        # The stored weights are a read-only MappingProxyType.
        with pytest.raises(TypeError):
            alloc.strategy_weights["a"] = 0.99
        with pytest.raises(TypeError):
            del alloc.strategy_weights["a"]


# ── model_copy re-runs validation ─────────────────────────────────── #
class TestModelCopyValidation:
    def test_copy_without_update_is_cheap_copy_equal(self):
        alloc = CapitalAllocation(strategy_weights={"a": 1.0}, total_capital=Decimal("1000"))
        copy = alloc.model_copy()
        assert copy == alloc
        assert copy is not alloc

    def test_copy_with_update_re_runs_validators(self):
        alloc = CapitalAllocation(strategy_weights={"a": 1.0}, total_capital=Decimal("1000"))
        # A valid update produces a new valid allocation.
        new = alloc.model_copy(update={"total_capital": Decimal("5000")})
        assert new.total_capital == Decimal("5000")
        assert new.strategy_weights == {"a": 1.0}

    def test_copy_update_that_breaks_sum_rejected(self):
        # Without re-running validation a caller could produce weights that no
        # longer sum to 1.0; the override must catch this.
        alloc = CapitalAllocation(strategy_weights={"a": 1.0})
        with pytest.raises(ValidationError, match=r"must sum to 1\.0"):
            alloc.model_copy(update={"strategy_weights": {"a": 0.5, "b": 0.4}})

    def test_copy_update_too_many_strategies_rejected(self):
        alloc = CapitalAllocation(strategy_weights={"a": 1.0})
        with pytest.raises(ValidationError, match="too many strategies"):
            alloc.model_copy(
                update={"max_strategies": 1, "strategy_weights": {"a": 0.0, "b": 1.0}}
            )


# ── allocation math ────────────────────────────────────────────────── #
class TestAllocationMath:
    def test_get_allocation_quantised_to_cent(self):
        alloc = CapitalAllocation(
            strategy_weights={"a": 0.3333, "b": 0.6667},
            total_capital=Decimal("100"),
        )
        assert alloc.get_allocation("a") == Decimal("33.33")
        assert alloc.get_allocation("b") == Decimal("66.67")

    def test_get_allocation_unknown_strategy_is_zero(self):
        alloc = CapitalAllocation(strategy_weights={"a": 1.0}, total_capital=Decimal("100"))
        assert alloc.get_allocation("nope") == Decimal("0")

    def test_get_allocation_zero_weight_is_zero(self):
        alloc = CapitalAllocation(
            strategy_weights={"a": 1.0, "b": 0.0}, total_capital=Decimal("100")
        )
        assert alloc.get_allocation("b") == Decimal("0")

    def test_total_allocated_sums_each_strategy(self):
        alloc = CapitalAllocation(
            strategy_weights={"a": 0.5, "b": 0.5}, total_capital=Decimal("100")
        )
        assert alloc.total_allocated() == Decimal("100.00")

    def test_total_allocated_empty_is_zero(self):
        alloc = CapitalAllocation()
        assert alloc.total_allocated() == Decimal("0")


# ── serialization ──────────────────────────────────────────────────── #
class TestSerialization:
    def test_model_dump_returns_plain_dict(self):
        # The field holds a read-only MappingProxyType internally, but
        # model_dump must yield a plain JSON-friendly dict.
        alloc = CapitalAllocation(
            strategy_weights={"a": 0.5, "b": 0.5}, total_capital=Decimal("100")
        )
        dumped = alloc.model_dump()
        assert isinstance(dumped["strategy_weights"], dict)
        assert dumped["strategy_weights"] == {"a": 0.5, "b": 0.5}
        # Mutating the dumped dict must not affect the frozen instance.
        dumped["strategy_weights"]["a"] = 99.0
        assert alloc.strategy_weights == {"a": 0.5, "b": 0.5}

    def test_model_dump_json_round_trips(self):
        alloc = CapitalAllocation(strategy_weights={"a": 1.0}, total_capital=Decimal("1000"))
        js = alloc.model_dump_json()
        rebuilt = CapitalAllocation.model_validate_json(js)
        assert rebuilt == alloc
