"""Focused unit tests for :class:`engine.portfolio.rebalancer.PortfolioRebalancer`.

These tests pin every construction-time validation branch (including the
all-zero ``target_weights`` guard that was previously masked by a silent
"equal shares" fallback) and exercise the full drift / needs-rebalance /
order-generation surface against the edge cases enumerated in the module's
own docstrings.
"""

from __future__ import annotations

import math

import pytest

from engine.portfolio.rebalancer import (
    PortfolioRebalancer,
    PortfolioRebalancerError,
    RebalanceAction,
    RebalanceOrder,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# A balanced two-strategy book used as the starting point for many drift
# scenarios. Targets and current values agree, so it is already on policy.
_BAL_TARGETS: dict[str, float] = {"a": 0.5, "b": 0.5}
_BAL_CURRENT: dict[str, float] = {"a": 50.0, "b": 50.0}


# ===========================================================================
# Construction-time validation (the all-zero target_weights bug fix lives here)
# ===========================================================================


class TestAllZeroTargetWeights:
    """The regression: an all-zero target policy must be rejected with a
    clear message rather than silently turned into arbitrary equal shares
    (or, worse, NaN weights from a divide-by-zero)."""

    @pytest.mark.parametrize(
        "targets",
        [
            {"a": 0.0, "b": 0.0},
            {"a": 0.0, "b": 0.0, "c": 0.0},
            {"a": 0.0},  # single all-zero entry
            {"a": 0, "b": 0},  # int zeros
        ],
        ids=["two-zeros", "three-zeros", "single-zero", "int-zeros"],
    )
    def test_all_zero_targets_raises(self, targets):
        with pytest.raises(PortfolioRebalancerError) as exc:
            PortfolioRebalancer(targets, {"a": 100.0, "b": 100.0})
        # The message must call out the root cause unambiguously.
        msg = str(exc.value)
        assert "non-zero" in msg
        assert "zero" in msg

    def test_all_zero_message_mentions_count(self):
        with pytest.raises(PortfolioRebalancerError, match="3 weight"):
            PortfolioRebalancer({"x": 0.0, "y": 0.0, "z": 0.0}, {"x": 10.0})

    def test_mixed_zero_and_nonzero_is_valid(self):
        # A *single* zero weight alongside a positive weight is fine — only
        # the all-zero *aggregate* is rejected.
        reb = PortfolioRebalancer({"a": 0.0, "b": 1.0}, _BAL_CURRENT)
        assert reb.target_weights == {"a": 0.0, "b": 1.0}

    def test_all_zero_does_not_silently_equal_weight(self):
        """Guard against the old behaviour: equal-share fallback would have
        produced {a: 0.5, b: 0.5}. We now raise instead."""
        with pytest.raises(PortfolioRebalancerError):
            PortfolioRebalancer({"a": 0.0, "b": 0.0}, _BAL_CURRENT)


class TestEmptyTargetWeights:
    def test_empty_targets_raises(self):
        with pytest.raises(PortfolioRebalancerError, match="must not be empty"):
            PortfolioRebalancer({}, {"a": 100.0})


class TestBadWeightValues:
    """Per-weight validation in ``_clean_weights`` / ``_finite``."""

    @pytest.mark.parametrize(
        "bad_weight",
        [
            -0.1,  # negative
            float("nan"),  # NaN slips past bare comparisons
            float("inf"),  # +Inf
            float("-inf"),  # -Inf
        ],
        ids=["negative", "nan", "inf", "neginf"],
    )
    def test_bad_target_weight_raises(self, bad_weight):
        with pytest.raises(PortfolioRebalancerError):
            PortfolioRebalancer({"a": bad_weight, "b": 0.5}, _BAL_CURRENT)

    @pytest.mark.parametrize("bad_weight", [True, False, "0.5", None, "abc"])
    def test_non_numeric_target_weight_raises(self, bad_weight):
        with pytest.raises(PortfolioRebalancerError, match="must be a number"):
            PortfolioRebalancer({"a": bad_weight, "b": 0.5}, _BAL_CURRENT)

    def test_negative_current_value_raises(self):
        with pytest.raises(PortfolioRebalancerError, match="non-negative"):
            PortfolioRebalancer({"a": 0.5, "b": 0.5}, {"a": -10.0, "b": 10.0})

    def test_nan_current_value_raises(self):
        with pytest.raises(PortfolioRebalancerError):
            PortfolioRebalancer({"a": 0.5, "b": 0.5}, {"a": float("nan"), "b": 10.0})


class TestBadStrategyIds:
    @pytest.mark.parametrize("bad_key", ["", "   "])
    def test_blank_strategy_id_raises(self, bad_key):
        with pytest.raises(PortfolioRebalancerError, match="non-empty strings"):
            PortfolioRebalancer({bad_key: 0.5, "b": 0.5}, _BAL_CURRENT)

    def test_non_string_strategy_id_raises(self):
        with pytest.raises(PortfolioRebalancerError, match="non-empty strings"):
            PortfolioRebalancer({1: 0.5, "b": 0.5}, _BAL_CURRENT)  # type: ignore[dict-item]


class TestNonDictInputs:
    def test_non_dict_targets_raises(self):
        with pytest.raises(PortfolioRebalancerError, match="target_weights must be a dict"):
            PortfolioRebalancer([("a", 0.5)], _BAL_CURRENT)  # type: ignore[arg-type]

    def test_non_dict_current_raises(self):
        with pytest.raises(PortfolioRebalancerError, match="current_values must be a dict"):
            PortfolioRebalancer(_BAL_TARGETS, None)  # type: ignore[arg-type]


class TestThresholdValidation:
    @pytest.mark.parametrize("bad_thr", [-0.01, 1.0001, 2.0])
    def test_out_of_range_threshold_raises(self, bad_thr):
        with pytest.raises(PortfolioRebalancerError, match="threshold"):
            PortfolioRebalancer(_BAL_TARGETS, _BAL_CURRENT, threshold=bad_thr)

    @pytest.mark.parametrize("bad_thr", [float("nan"), float("inf")])
    def test_non_finite_threshold_raises(self, bad_thr):
        with pytest.raises(PortfolioRebalancerError, match="finite"):
            PortfolioRebalancer(_BAL_TARGETS, _BAL_CURRENT, threshold=bad_thr)

    @pytest.mark.parametrize("bad_thr", [True, False, "0.1", None])
    def test_non_numeric_threshold_raises(self, bad_thr):
        with pytest.raises(PortfolioRebalancerError, match="must be a number"):
            PortfolioRebalancer(_BAL_TARGETS, _BAL_CURRENT, threshold=bad_thr)  # type: ignore[arg-type]

    def test_boundary_thresholds_accepted(self):
        # 0.0 and 1.0 are the inclusive endpoints and must be valid.
        for thr in (0.0, 1.0):
            reb = PortfolioRebalancer(_BAL_TARGETS, _BAL_CURRENT, threshold=thr)
            assert reb.threshold == thr


# ===========================================================================
# Normalisation of target weights
# ===========================================================================


class TestTargetNormalisation:
    def test_relative_weights_normalised(self):
        # {"a": 2, "b": 1} ≡ {"a": 2/3, "b": 1/3}
        reb = PortfolioRebalancer({"a": 2.0, "b": 1.0}, {"a": 0.0, "b": 0.0})
        assert reb.target_weights == pytest.approx({"a": 2 / 3, "b": 1 / 3})

    def test_already_normalised_weights_preserved(self):
        reb = PortfolioRebalancer(_BAL_TARGETS, _BAL_CURRENT)
        assert reb.target_weights == pytest.approx({"a": 0.5, "b": 0.5})

    def test_single_strategy_weights_to_one(self):
        reb = PortfolioRebalancer({"only": 3.0}, {"only": 100.0})
        assert reb.target_weights == {"only": 1.0}

    def test_int_weights_accepted(self):
        reb = PortfolioRebalancer({"a": 1, "b": 1}, {"a": 10.0, "b": 10.0})
        assert reb.target_weights == pytest.approx({"a": 0.5, "b": 0.5})


# ===========================================================================
# Introspection properties & defensive copying
# ===========================================================================


class TestIntrospection:
    def test_threshold_property(self):
        reb = PortfolioRebalancer(_BAL_TARGETS, _BAL_CURRENT, threshold=0.1)
        assert reb.threshold == 0.1

    def test_total_capital_is_sum_of_current(self):
        reb = PortfolioRebalancer(_BAL_TARGETS, {"a": 30.0, "b": 70.0})
        assert reb.total_capital == 100.0

    def test_total_capital_zero_for_all_zero_current(self):
        reb = PortfolioRebalancer({"a": 1.0}, {"a": 0.0})
        assert reb.total_capital == 0.0

    def test_strategy_ids_union_and_sorted(self):
        # "b" only in current, "c" only in targets -> union, sorted.
        reb = PortfolioRebalancer({"a": 0.5, "c": 0.5}, {"a": 10.0, "b": 10.0})
        assert reb.strategy_ids == ["a", "b", "c"]

    def test_target_weights_returns_independent_copy(self):
        reb = PortfolioRebalancer(_BAL_TARGETS, _BAL_CURRENT)
        snap = reb.target_weights
        snap["a"] = 999.0
        # Mutating the snapshot must not leak back into the rebalancer.
        assert reb.target_weights == pytest.approx({"a": 0.5, "b": 0.5})

    def test_current_values_returns_independent_copy(self):
        reb = PortfolioRebalancer(_BAL_TARGETS, _BAL_CURRENT)
        snap = reb.current_values
        snap["a"] = 999.0
        assert reb.current_values == _BAL_CURRENT


class TestDefensiveCopies:
    def test_mutation_of_caller_target_dict_is_isolated(self):
        targets = {"a": 0.5, "b": 0.5}
        reb = PortfolioRebalancer(targets, _BAL_CURRENT)
        targets["a"] = 0.9  # caller mutates its own dict afterwards
        assert reb.target_weights == pytest.approx({"a": 0.5, "b": 0.5})

    def test_mutation_of_caller_current_dict_is_isolated(self):
        current = {"a": 50.0, "b": 50.0}
        reb = PortfolioRebalancer(_BAL_TARGETS, current)
        current["a"] = 1_000.0
        assert reb.current_values == {"a": 50.0, "b": 50.0}
        assert reb.total_capital == 100.0


# ===========================================================================
# Weight lookups
# ===========================================================================


class TestWeightLookups:
    def test_target_weight_known_strategy(self):
        reb = PortfolioRebalancer({"a": 0.5, "b": 0.5}, _BAL_CURRENT)
        assert reb.target_weight("a") == 0.5

    def test_target_weight_absent_strategy_is_zero(self):
        reb = PortfolioRebalancer({"a": 1.0}, {"a": 100.0})
        assert reb.target_weight("ghost") == 0.0

    def test_current_value_known_strategy(self):
        reb = PortfolioRebalancer(_BAL_TARGETS, {"a": 30.0, "b": 70.0})
        assert reb.current_value("b") == 70.0

    def test_current_value_absent_strategy_is_zero(self):
        reb = PortfolioRebalancer({"a": 1.0}, {"a": 100.0})
        assert reb.current_value("ghost") == 0.0

    def test_current_weight_zero_capital_is_zero(self):
        reb = PortfolioRebalancer({"a": 1.0}, {"a": 0.0})
        assert reb.current_weight("a") == 0.0

    def test_current_weight_normal_case(self):
        reb = PortfolioRebalancer(_BAL_TARGETS, {"a": 25.0, "b": 75.0})
        assert reb.current_weight("a") == 0.25
        assert reb.current_weight("b") == 0.75


# ===========================================================================
# Drift detection
# ===========================================================================


class TestComputeDrift:
    def test_signed_drift_balanced_is_zero(self):
        reb = PortfolioRebalancer(_BAL_TARGETS, _BAL_CURRENT)
        assert reb.compute_drift() == {"a": 0.0, "b": 0.0}

    def test_signed_drift_overweight_positive(self):
        # a holds 70% but targets 50% -> +0.20 overweight.
        reb = PortfolioRebalancer(_BAL_TARGETS, {"a": 70.0, "b": 30.0})
        drifts = reb.compute_drift()
        assert drifts["a"] == pytest.approx(0.2)
        assert drifts["b"] == pytest.approx(-0.2)

    def test_drift_covers_union_of_strategies(self):
        # "c" is held but untargeted -> target 0, fully overweight.
        reb = PortfolioRebalancer({"a": 0.5, "b": 0.5}, {"a": 50.0, "b": 50.0, "c": 50.0})
        assert set(reb.compute_drift()) == {"a", "b", "c"}

    def test_zero_capital_drift_is_negative_target_weight(self):
        # With no capital, every current weight is 0, so drift = -target.
        reb = PortfolioRebalancer({"a": 0.7, "b": 0.3}, {"a": 0.0, "b": 0.0})
        drifts = reb.compute_drift()
        assert drifts["a"] == pytest.approx(-0.7)
        assert drifts["b"] == pytest.approx(-0.3)


class TestMaxDrift:
    def test_balanced_is_zero(self):
        reb = PortfolioRebalancer(_BAL_TARGETS, _BAL_CURRENT)
        assert reb.max_drift() == 0.0

    def test_returns_largest_absolute_drift(self):
        reb = PortfolioRebalancer({"a": 0.5, "b": 0.5}, {"a": 80.0, "b": 20.0})
        # drifts: a +0.3, b -0.3 -> max abs 0.3
        assert reb.max_drift() == pytest.approx(0.3)


# ===========================================================================
# needs_rebalance (strict threshold boundary)
# ===========================================================================


class TestNeedsRebalance:
    def test_balanced_returns_false(self):
        reb = PortfolioRebalancer(_BAL_TARGETS, _BAL_CURRENT)
        assert reb.needs_rebalance() is False

    def test_drift_below_threshold_returns_false(self):
        # drift ±0.05, threshold 0.1 -> within tolerance.
        reb = PortfolioRebalancer(
            _BAL_TARGETS, {"a": 55.0, "b": 45.0}, threshold=0.1
        )
        assert reb.max_drift() == pytest.approx(0.05)
        assert reb.needs_rebalance() is False

    def test_drift_exactly_at_threshold_returns_false(self):
        # Strict comparison: drift == threshold must NOT trip a rebalance.
        reb = PortfolioRebalancer(
            _BAL_TARGETS, {"a": 60.0, "b": 40.0}, threshold=0.1
        )
        assert reb.max_drift() == pytest.approx(0.1)
        assert reb.needs_rebalance() is False

    def test_drift_above_threshold_returns_true(self):
        reb = PortfolioRebalancer(
            _BAL_TARGETS, {"a": 65.0, "b": 35.0}, threshold=0.1
        )
        assert reb.max_drift() == pytest.approx(0.15)
        assert reb.needs_rebalance() is True

    def test_zero_capital_never_needs_rebalance(self):
        # Even though drifts are non-zero, there's no capital to move.
        reb = PortfolioRebalancer({"a": 0.7, "b": 0.3}, {"a": 0.0, "b": 0.0})
        assert reb.needs_rebalance() is False


# ===========================================================================
# Order generation
# ===========================================================================


class TestGenerateRebalanceOrders:
    def test_balanced_yields_no_orders(self):
        reb = PortfolioRebalancer(_BAL_TARGETS, _BAL_CURRENT)
        assert reb.generate_rebalance_orders() == []

    def test_zero_capital_yields_no_orders(self):
        reb = PortfolioRebalancer({"a": 0.5, "b": 0.5}, {"a": 0.0, "b": 0.0})
        assert reb.generate_rebalance_orders() == []

    def test_underweight_strategy_emits_buy(self):
        # a targets 50 but holds 40 of 100 -> BUY 10.
        reb = PortfolioRebalancer(_BAL_TARGETS, {"a": 40.0, "b": 60.0})
        orders = {o.strategy_id: o for o in reb.generate_rebalance_orders()}
        assert orders["a"].action is RebalanceAction.BUY
        assert orders["a"].amount == 10.0

    def test_overweight_strategy_emits_sell(self):
        # b targets 50 but holds 60 of 100 -> SELL 10.
        reb = PortfolioRebalancer(_BAL_TARGETS, {"a": 40.0, "b": 60.0})
        orders = {o.strategy_id: o for o in reb.generate_rebalance_orders()}
        assert orders["b"].action is RebalanceAction.SELL
        assert orders["b"].amount == 10.0

    def test_exit_order_for_untargeted_holding(self):
        # "c" is held but absent from targets -> targeted for full exit (SELL).
        reb = PortfolioRebalancer({"a": 1.0}, {"a": 100.0, "c": 50.0})
        orders = {o.strategy_id: o for o in reb.generate_rebalance_orders()}
        assert orders["c"].action is RebalanceAction.SELL
        assert orders["c"].amount == 50.0

    def test_entry_order_for_targeted_but_unheld_strategy(self):
        # "b" is targeted but has no current position -> BUY into it.
        reb = PortfolioRebalancer({"a": 0.5, "b": 0.5}, {"a": 100.0})
        orders = {o.strategy_id: o for o in reb.generate_rebalance_orders()}
        assert orders["b"].action is RebalanceAction.BUY
        assert orders["b"].amount == 50.0

    def test_orders_sorted_by_strategy_id(self):
        reb = PortfolioRebalancer(
            {"a": 0.25, "b": 0.25, "c": 0.25, "d": 0.25},
            {"a": 10.0, "b": 20.0, "c": 30.0, "d": 40.0},
        )
        ids = [o.strategy_id for o in reb.generate_rebalance_orders()]
        assert ids == sorted(ids)

    def test_float_dust_does_not_emit_phantom_order(self):
        # Already on target: deltas are ~0 (well within _ORDER_EPSILON).
        reb = PortfolioRebalancer(_BAL_TARGETS, {"a": 50.0, "b": 50.0})
        assert reb.generate_rebalance_orders() == []

    def test_amounts_rounded_to_cent(self):
        # 1/3 target of 100 -> 33.33..., current 0 -> BUY ~33.33 (2dp).
        reb = PortfolioRebalancer({"a": 1.0, "b": 2.0}, {"a": 0.0, "b": 0.0})
        # total capital 0 -> no orders; use non-zero current instead.
        reb = PortfolioRebalancer({"a": 1.0, "b": 2.0}, {"a": 0.0, "b": 99.99})
        for order in reb.generate_rebalance_orders():
            # amount must have at most 2 decimal places.
            assert round(order.amount, 2) == order.amount

    def test_order_carries_full_provenance(self):
        reb = PortfolioRebalancer(_BAL_TARGETS, {"a": 40.0, "b": 60.0})
        order = reb.generate_rebalance_orders()[0]
        # Every field is populated so an audit trail can reconstruct inputs.
        assert order.current_value in {40.0, 60.0}
        assert order.target_value == 50.0
        assert order.drift == pytest.approx(
            order.current_weight - order.target_weight
        )

    def test_order_action_enum_values(self):
        assert RebalanceAction.BUY.value == "buy"
        assert RebalanceAction.SELL.value == "sell"

    def test_rebalance_order_is_frozen(self):
        order = RebalanceOrder(
            strategy_id="a",
            action=RebalanceAction.BUY,
            amount=10.0,
            current_value=40.0,
            target_value=50.0,
            current_weight=0.4,
            target_weight=0.5,
            drift=0.1,
        )
        with pytest.raises((AttributeError, Exception)):
            order.amount = 999.0  # type: ignore[misc]


# ===========================================================================
# End-to-end smoke: a realistic drift scenario through all three questions
# ===========================================================================


class TestEndToEnd:
    def test_three_strategy_drift_scenario(self):
        targets = {"momentum": 0.5, "meanrev": 0.3, "carry": 0.2}
        # momentum ran ahead, carry lagged.
        current = {"momentum": 70.0, "meanrev": 25.0, "carry": 5.0}
        reb = PortfolioRebalancer(targets, current)

        assert reb.total_capital == 100.0

        # 1. drift direction is as expected per strategy.
        drifts = reb.compute_drift()
        assert drifts["momentum"] > 0  # overweight
        assert drifts["carry"] < 0  # underweight

        # 2. drift is large enough to warrant a rebalance at 5% threshold.
        assert reb.max_drift() > 0.05
        assert reb.needs_rebalance() is True

        # 3. orders net out: sells on overweight, buys on underweight.
        orders = {o.strategy_id: o for o in reb.generate_rebalance_orders()}
        assert orders["momentum"].action is RebalanceAction.SELL
        assert orders["carry"].action is RebalanceAction.BUY
        # Total bought equals total sold (capital is conserved).
        bought = sum(o.amount for o in orders.values() if o.action is RebalanceAction.BUY)
        sold = sum(o.amount for o in orders.values() if o.action is RebalanceAction.SELL)
        assert math.isclose(bought, sold, abs_tol=0.01)

    def test_executing_orders_brings_book_onto_target(self):
        """Applying the generated orders should zero out the drift."""
        targets = {"a": 0.6, "b": 0.4}
        current = {"a": 20.0, "b": 80.0}  # badly off-target
        reb = PortfolioRebalancer(targets, current)
        orders = reb.generate_rebalance_orders()

        new_current = dict(current)
        for order in orders:
            delta = order.amount if order.action is RebalanceAction.BUY else -order.amount
            new_current[order.strategy_id] += delta

        reb2 = PortfolioRebalancer(targets, new_current)
        assert reb2.max_drift() < 1e-6
        assert reb2.generate_rebalance_orders() == []
