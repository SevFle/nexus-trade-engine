"""Tests for :mod:`engine.portfolio.allocation` (the frozen Pydantic
:class:`CapitalAllocation` value object).

These cover the import re-export in ``engine.portfolio`` as well as the full
validator surface, immutability semantics, the validation-re-running
``model_copy`` override, and the cent-quantised allocation math.

Note: this is a *different* module from ``engine.core.capital_allocation``
(the Hamilton largest-remainder apportioner exercised by
``test_capital_allocation.py``); here we test the declarative capital-split
value object.
"""

from __future__ import annotations

from decimal import Decimal
from types import MappingProxyType

import pytest
from pydantic import ValidationError

# Importing through the package exercises the re-export in
# ``engine.portfolio.__init__`` (covering lines 3-7 there) *and* the model.
from engine.portfolio import CapitalAllocation as CapitalAllocationFromPkg
from engine.portfolio.allocation import CapitalAllocation


class TestReExport:
    def test_package_re_exports_capital_allocation(self):
        # The ``engine.portfolio`` package must re-export the model so callers
        # can do ``from engine.portfolio import CapitalAllocation``.
        assert CapitalAllocationFromPkg is CapitalAllocation

    def test_package_all(self):
        import engine.portfolio as pkg

        assert pkg.__all__ == ["CapitalAllocation"]


class TestConstruction:
    def test_empty_default_is_valid(self):
        # An empty allocation (not yet deployed) must be legal; the sum-to-1.0
        # rule only bites once at least one weight exists.
        alloc = CapitalAllocation()
        assert dict(alloc.strategy_weights) == {}
        assert alloc.total_capital == Decimal("0")
        assert alloc.max_strategies == 50

    def test_single_strategy_full_weight(self):
        alloc = CapitalAllocation(
            strategy_weights={"only": 1.0},
            total_capital=Decimal("1000"),
        )
        assert alloc.strategy_weights["only"] == 1.0

    def test_multi_strategy_split(self):
        alloc = CapitalAllocation(
            strategy_weights={"a": 0.5, "b": 0.5},
            total_capital=Decimal("1000"),
        )
        assert alloc.strategy_weights == {"a": 0.5, "b": 0.5}

    def test_total_capital_accepts_int_and_str(self):
        # The field is Decimal-typed; Pydantic coerces int / numeric str.
        alloc = CapitalAllocation(
            strategy_weights={"a": 1.0}, total_capital=1000
        )
        assert alloc.total_capital == Decimal("1000")
        alloc2 = CapitalAllocation(
            strategy_weights={"a": 1.0}, total_capital="250.5"
        )
        assert alloc2.total_capital == Decimal("250.5")

    def test_negative_total_capital_rejected(self):
        with pytest.raises(ValidationError):
            CapitalAllocation(strategy_weights={"a": 1.0}, total_capital=-1)


class TestWeightValidation:
    def test_blank_strategy_id_rejected(self):
        with pytest.raises(ValidationError, match="non-empty string"):
            CapitalAllocation(strategy_weights={"": 1.0})
        with pytest.raises(ValidationError, match="non-empty string"):
            CapitalAllocation(strategy_weights={"   ": 1.0})

    def test_nan_weight_rejected(self):
        # math.isfinite guard: NaN comparisons all return False, so a bare
        # ``w < 0`` check would silently admit NaN.
        with pytest.raises(ValidationError, match="finite"):
            CapitalAllocation(strategy_weights={"a": float("nan")})

    def test_inf_weight_rejected(self):
        with pytest.raises(ValidationError, match="finite"):
            CapitalAllocation(strategy_weights={"a": float("inf")})
        with pytest.raises(ValidationError, match="finite"):
            CapitalAllocation(strategy_weights={"a": float("-inf")})

    def test_negative_weight_rejected(self):
        with pytest.raises(ValidationError, match="non-negative"):
            CapitalAllocation(strategy_weights={"a": -0.1, "b": 1.1})

    def test_over_one_weight_rejected(self):
        with pytest.raises(ValidationError, match=r"<= 1\.0"):
            CapitalAllocation(strategy_weights={"a": 1.5})

    def test_bool_true_coerced_to_one(self):
        # bool is a subclass of int; Pydantic's schema coercion turns ``True``
        # into ``1.0`` before the after-mode field validator runs, so a
        # single ``True`` weight is accepted as a full allocation.
        alloc = CapitalAllocation(strategy_weights={"a": True})
        assert alloc.strategy_weights["a"] == 1.0

    def test_bool_false_rejected_by_sum_check(self):
        # ``False`` coerces to ``0.0`` and is then caught by the sum-to-1.0
        # cross-field rule.
        with pytest.raises(ValidationError, match=r"must sum to 1\.0"):
            CapitalAllocation(strategy_weights={"a": False})

    def test_none_weight_rejected(self):
        # Pydantic's own schema validation rejects None before the custom
        # validator runs.
        with pytest.raises(ValidationError):
            CapitalAllocation(strategy_weights={"a": None})

    def test_string_weight_rejected(self):
        with pytest.raises(ValidationError):
            CapitalAllocation(strategy_weights={"a": "half"})


class TestCrossFieldValidation:
    def test_weights_must_sum_to_one(self):
        with pytest.raises(ValidationError, match=r"must sum to 1\.0"):
            CapitalAllocation(strategy_weights={"a": 0.3, "b": 0.3})

    def test_sum_within_epsilon_accepted(self):
        # 0.1 + 0.2 + 0.3 + 0.4 lands within 1e-9 of 1.0 even though the raw
        # IEEE-754 sum is not exactly 1.0.
        alloc = CapitalAllocation(
            strategy_weights={"a": 0.1, "b": 0.2, "c": 0.3, "d": 0.4}
        )
        assert len(alloc.strategy_weights) == 4

    def test_too_many_strategies_rejected(self):
        weights = {f"s{i}": 0.5 for i in range(2)}
        with pytest.raises(ValidationError, match="too many strategies"):
            CapitalAllocation(strategy_weights=weights, max_strategies=1)

    def test_custom_max_strategies_enforced(self):
        # Three equal-ish weights that sum to 1.0 but exceed max_strategies=2.
        weights = {"s0": 0.34, "s1": 0.33, "s2": 0.33}
        with pytest.raises(ValidationError, match="too many strategies"):
            CapitalAllocation(strategy_weights=weights, max_strategies=2)


class TestImmutability:
    def test_field_assignment_blocked(self):
        alloc = CapitalAllocation(strategy_weights={"a": 1.0}, total_capital=1000)
        # ``frozen`` blocks field reassignment.
        with pytest.raises(ValidationError):
            alloc.total_capital = Decimal("2000")  # type: ignore[misc]
        with pytest.raises(ValidationError):
            alloc.strategy_weights = {"a": 1.0}  # type: ignore[misc]

    def test_in_place_weight_mutation_blocked(self):
        # The weights mapping is wrapped in a read-only MappingProxyType so
        # item-level mutation raises TypeError.
        alloc = CapitalAllocation(strategy_weights={"a": 1.0}, total_capital=1000)
        assert isinstance(alloc.strategy_weights, MappingProxyType)
        with pytest.raises(TypeError):
            alloc.strategy_weights["a"] = 0.5  # type: ignore[index]


class TestModelCopyValidation:
    def test_copy_without_update_is_cheap_parent_copy(self):
        alloc = CapitalAllocation(strategy_weights={"a": 1.0}, total_capital=1000)
        clone = alloc.model_copy()
        assert clone is not alloc
        assert clone.total_capital == Decimal("1000")
        assert dict(clone.strategy_weights) == {"a": 1.0}

    def test_copy_with_update_re_runs_validators(self):
        alloc = CapitalAllocation(strategy_weights={"a": 1.0}, total_capital=1000)
        grown = alloc.model_copy(update={"total_capital": Decimal("2000")})
        assert grown.total_capital == Decimal("2000")
        assert dict(grown.strategy_weights) == {"a": 1.0}

    def test_copy_update_rejects_invalid_weight_sum(self):
        # A caller must not be able to produce a weights dict that no longer
        # sums to 1.0 by going through model_copy.
        alloc = CapitalAllocation(strategy_weights={"a": 1.0}, total_capital=1000)
        with pytest.raises(ValidationError, match=r"must sum to 1\.0"):
            alloc.model_copy(update={"strategy_weights": {"a": 0.5}})

    def test_copy_update_rejects_too_many_strategies(self):
        # model_copy must re-run the max_strategies cross-field check: two
        # valid weights (summing to 1.0) pushed under max_strategies=1 must
        # be rejected.
        alloc = CapitalAllocation(strategy_weights={"a": 1.0}, total_capital=1000)
        with pytest.raises(ValidationError, match="too many strategies"):
            alloc.model_copy(
                update={"strategy_weights": {"a": 0.5, "b": 0.5}, "max_strategies": 1}
            )


class TestAllocationMath:
    def test_get_allocation_known_strategy_quantised_to_cent(self):
        alloc = CapitalAllocation(
            strategy_weights={"a": 0.3333, "b": 0.6667},
            total_capital=Decimal("100"),
        )
        # 100 * 0.3333 = 33.33 (quantised to the cent)
        assert alloc.get_allocation("a") == Decimal("33.33")
        assert alloc.get_allocation("b") == Decimal("66.67")

    def test_get_allocation_unknown_strategy_returns_zero(self):
        alloc = CapitalAllocation(strategy_weights={"a": 1.0}, total_capital=100)
        # Unknown strategy resolves to 0.00 rather than raising so callers can
        # iterate over an arbitrary strategy set safely.
        assert alloc.get_allocation("nope") == Decimal("0")

    def test_get_allocation_zero_weight_strategy_returns_zero(self):
        alloc = CapitalAllocation(
            strategy_weights={"a": 0.0, "b": 1.0}, total_capital=100
        )
        # Zero weight short-circuits to 0 before the multiplication.
        assert alloc.get_allocation("a") == Decimal("0")
        assert alloc.get_allocation("b") == Decimal("100.00")

    def test_total_allocated_sums_each_strategy(self):
        alloc = CapitalAllocation(
            strategy_weights={"a": 0.5, "b": 0.5}, total_capital=Decimal("1000")
        )
        assert alloc.total_allocated() == Decimal("1000.00")

    def test_total_allocated_empty_is_zero(self):
        assert CapitalAllocation().total_allocated() == Decimal("0")

    def test_total_allocated_rounding(self):
        # 1/3 + 2/3 of 1000 -> 333.33 + 666.67 = 1000.00
        alloc = CapitalAllocation(
            strategy_weights={"a": 0.3333, "b": 0.6667},
            total_capital=Decimal("1000"),
        )
        assert alloc.total_allocated() == Decimal("1000.00")


class TestSerialization:
    def test_model_dump_returns_plain_dict_weights(self):
        # The instance holds a MappingProxyType; the serializer must convert it
        # back to a plain JSON-friendly dict on dump.
        alloc = CapitalAllocation(strategy_weights={"a": 1.0}, total_capital=100)
        dumped = alloc.model_dump()
        assert dumped["strategy_weights"] == {"a": 1.0}
        assert not isinstance(dumped["strategy_weights"], MappingProxyType)

    def test_model_dump_json_round_trip(self):
        alloc = CapitalAllocation(strategy_weights={"a": 1.0}, total_capital=1000)
        import json

        data = json.loads(alloc.model_dump_json())
        assert data["strategy_weights"] == {"a": 1.0}
