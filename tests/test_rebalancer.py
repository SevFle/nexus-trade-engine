"""Focused unit tests for :class:`engine.portfolio.rebalancer.PortfolioRebalancer`.

Covers the three core questions the rebalancer answers — drift calculation,
threshold logic, and order generation — plus input validation and the
``_clean_weights`` key-stripping behaviour.
"""

from __future__ import annotations

import math

import pytest

from engine.portfolio.rebalancer import (
    PortfolioRebalancer,
    PortfolioRebalancerError,
    RebalanceAction,
    RebalanceOrder,
    _clean_weights,
    _finite,
)

# --------------------------------------------------------------------------- #
# Construction & introspection
# --------------------------------------------------------------------------- #


class TestConstruction:
    def test_target_weights_are_normalised_to_unit_sum(self):
        reb = PortfolioRebalancer({"a": 2, "b": 1}, {"a": 0, "b": 0})
        assert reb.target_weights == pytest.approx({"a": 2 / 3, "b": 1 / 3})

    def test_relative_equal_weights(self):
        reb = PortfolioRebalancer({"a": 1, "b": 1}, {"a": 0, "b": 0})
        assert reb.target_weights == pytest.approx({"a": 0.5, "b": 0.5})

    def test_total_capital_sums_current_values(self):
        reb = PortfolioRebalancer({"a": 1}, {"a": 100, "b": 300})
        assert reb.total_capital == 400.0

    def test_strategy_ids_is_sorted_union(self):
        reb = PortfolioRebalancer({"b": 1, "c": 1}, {"a": 1, "b": 1})
        assert reb.strategy_ids == ["a", "b", "c"]

    def test_default_threshold_is_five_percent(self):
        reb = PortfolioRebalancer({"a": 1}, {"a": 100})
        assert reb.threshold == pytest.approx(0.05)

    def test_custom_threshold(self):
        reb = PortfolioRebalancer({"a": 1}, {"a": 100}, threshold=0.1)
        assert reb.threshold == pytest.approx(0.1)

    def test_target_weight_zero_for_absent_strategy(self):
        reb = PortfolioRebalancer({"a": 1}, {"a": 100, "b": 100})
        assert reb.target_weight("b") == 0.0
        assert reb.target_weight("a") == pytest.approx(1.0)

    def test_current_value_zero_for_absent_strategy(self):
        reb = PortfolioRebalancer({"a": 1, "b": 1}, {"a": 100})
        assert reb.current_value("b") == 0.0
        assert reb.current_value("a") == 100.0

    def test_current_weight_with_zero_capital(self):
        reb = PortfolioRebalancer({"a": 1}, {"a": 0})
        assert reb.current_weight("a") == 0.0

    def test_current_weight_is_share_of_total(self):
        reb = PortfolioRebalancer({"a": 1, "b": 1}, {"a": 100, "b": 300})
        assert reb.current_weight("a") == pytest.approx(0.25)
        assert reb.current_weight("b") == pytest.approx(0.75)

    def test_snapshot_properties_return_copies(self):
        reb = PortfolioRebalancer({"a": 1}, {"a": 100})
        tw = reb.target_weights
        cv = reb.current_values
        tw["a"] = 999
        cv["a"] = 999
        # Internal state must be unaffected by external mutation.
        assert reb.target_weights == {"a": 1.0}
        assert reb.current_values == {"a": 100.0}


# --------------------------------------------------------------------------- #
# _clean_weights — including the strip fix
# --------------------------------------------------------------------------- #


class TestCleanWeights:
    def test_whitespace_keys_are_stripped(self):
        # Regression: previously the unstripped key (" a ") leaked through,
        # defeating lookups by the bare id "a".
        cleaned = _clean_weights({" a ": 0.5, "b\t": 0.5}, "weights")
        assert set(cleaned.keys()) == {"a", "b"}
        assert cleaned["a"] == pytest.approx(0.5)
        assert cleaned["b"] == pytest.approx(0.5)

    def test_stripped_key_used_in_subsequent_lookups(self):
        # End-to-end proof of the fix: a whitespace-padded target id must
        # resolve via the bare-id lookups inside the rebalancer.
        reb = PortfolioRebalancer({" alpha ": 1.0}, {"alpha": 100})
        assert reb.target_weight("alpha") == pytest.approx(1.0)
        assert reb.current_value("alpha") == 100.0
        assert reb.compute_drift()["alpha"] == pytest.approx(0.0)

    def test_returns_copy_not_internal_alias(self):
        raw = {"a": 0.5}
        cleaned = _clean_weights(raw, "weights")
        cleaned["a"] = 1234.0
        assert raw["a"] == 0.5

    def test_non_dict_raises(self):
        with pytest.raises(PortfolioRebalancerError, match="must be a dict"):
            _clean_weights([("a", 1)], "weights")  # type: ignore[arg-type]

    def test_non_string_key_raises(self):
        with pytest.raises(PortfolioRebalancerError, match="non-empty strings"):
            _clean_weights({1: 0.5}, "weights")  # type: ignore[dict-item]

    def test_empty_string_key_raises(self):
        with pytest.raises(PortfolioRebalancerError, match="non-empty strings"):
            _clean_weights({"": 0.5}, "weights")

    def test_whitespace_only_key_raises(self):
        with pytest.raises(PortfolioRebalancerError, match="non-empty strings"):
            _clean_weights({"   ": 0.5}, "weights")

    def test_negative_weight_raises(self):
        with pytest.raises(PortfolioRebalancerError, match="non-negative"):
            _clean_weights({"a": -0.1}, "weights")


# --------------------------------------------------------------------------- #
# compute_drift
# --------------------------------------------------------------------------- #


class TestComputeDrift:
    def test_balanced_portfolio_has_zero_drift(self):
        reb = PortfolioRebalancer({"a": 1, "b": 1}, {"a": 50, "b": 50})
        assert reb.compute_drift() == pytest.approx({"a": 0.0, "b": 0.0})

    def test_overweight_strategy_is_positive(self):
        # a holds 75% but targets 50% → +0.25 drift (overweight).
        reb = PortfolioRebalancer({"a": 1, "b": 1}, {"a": 75, "b": 25})
        drifts = reb.compute_drift()
        assert drifts["a"] == pytest.approx(0.25)
        assert drifts["b"] == pytest.approx(-0.25)

    def test_drift_is_signed(self):
        reb = PortfolioRebalancer({"a": 0.5, "b": 0.5}, {"a": 30, "b": 70})
        drifts = reb.compute_drift()
        assert drifts["a"] < 0  # underweight
        assert drifts["b"] > 0  # overweight

    def test_held_but_untargeted_is_fully_overweight(self):
        # "b" is not in targets → target weight 0, current weight 1.0.
        reb = PortfolioRebalancer({"a": 1.0}, {"a": 0, "b": 100})
        drifts = reb.compute_drift()
        assert drifts["a"] == pytest.approx(-1.0)
        assert drifts["b"] == pytest.approx(1.0)

    def test_targeted_but_unheld_is_fully_underweight(self):
        reb = PortfolioRebalancer({"a": 0.5, "b": 0.5}, {"a": 100, "b": 0})
        drifts = reb.compute_drift()
        assert drifts["b"] == pytest.approx(-0.5)

    def test_drift_sums_to_zero(self):
        # Weight deviations always net to zero (current & target each sum 1).
        reb = PortfolioRebalancer({"a": 0.6, "b": 0.4}, {"a": 10, "b": 90})
        assert sum(reb.compute_drift().values()) == pytest.approx(0.0, abs=1e-12)

    def test_zero_capital_drift_is_negative_target_weight(self):
        reb = PortfolioRebalancer({"a": 0.7, "b": 0.3}, {"a": 0, "b": 0})
        drifts = reb.compute_drift()
        assert drifts["a"] == pytest.approx(-0.7)
        assert drifts["b"] == pytest.approx(-0.3)

    def test_max_drift_returns_largest_absolute(self):
        reb = PortfolioRebalancer({"a": 0.5, "b": 0.5}, {"a": 10, "b": 90})
        assert reb.max_drift() == pytest.approx(0.4)


# --------------------------------------------------------------------------- #
# needs_rebalance — threshold logic
# --------------------------------------------------------------------------- #


class TestNeedsRebalance:
    def test_within_threshold_returns_false(self):
        reb = PortfolioRebalancer({"a": 0.5, "b": 0.5}, {"a": 52, "b": 48})
        assert reb.needs_rebalance() is False

    def test_beyond_threshold_returns_true(self):
        reb = PortfolioRebalancer({"a": 0.5, "b": 0.5}, {"a": 70, "b": 30})
        assert reb.needs_rebalance() is True

    def test_exactly_at_threshold_returns_false(self):
        # Strict comparison: |drift| == threshold does NOT trip a rebalance.
        # 0.55 vs 0.50 → drift exactly 0.05 == default threshold.
        reb = PortfolioRebalancer({"a": 0.5, "b": 0.5}, {"a": 55, "b": 45})
        assert reb.max_drift() == pytest.approx(0.05)
        assert reb.needs_rebalance() is False

    def test_custom_threshold_respected(self):
        reb = PortfolioRebalancer(
            {"a": 0.5, "b": 0.5}, {"a": 56, "b": 44}, threshold=0.10
        )
        assert reb.needs_rebalance() is False

    def test_custom_threshold_trips(self):
        reb = PortfolioRebalancer(
            {"a": 0.5, "b": 0.5}, {"a": 56, "b": 44}, threshold=0.05
        )
        assert reb.needs_rebalance() is True

    def test_zero_threshold_trips_on_any_drift(self):
        reb = PortfolioRebalancer({"a": 0.5, "b": 0.5}, {"a": 51, "b": 49}, threshold=0.0)
        assert reb.needs_rebalance() is True

    def test_zero_capital_never_rebalances(self):
        reb = PortfolioRebalancer({"a": 0.5, "b": 0.5}, {"a": 0, "b": 0})
        assert reb.needs_rebalance() is False

    def test_perfectly_balanced_with_zero_threshold(self):
        reb = PortfolioRebalancer({"a": 0.5, "b": 0.5}, {"a": 50, "b": 50}, threshold=0.0)
        assert reb.needs_rebalance() is False


# --------------------------------------------------------------------------- #
# generate_rebalance_orders
# --------------------------------------------------------------------------- #


class TestGenerateRebalanceOrders:
    def test_on_target_yields_no_orders(self):
        reb = PortfolioRebalancer({"a": 0.5, "b": 0.5}, {"a": 50, "b": 50})
        assert reb.generate_rebalance_orders() == []

    def test_buy_order_for_underweight_strategy(self):
        # "a" is underweight (30 vs target 50) → BUY 20.
        reb = PortfolioRebalancer({"a": 0.5, "b": 0.5}, {"a": 30, "b": 70})
        orders = reb.generate_rebalance_orders()
        buy = next(o for o in orders if o.strategy_id == "a")
        assert buy.action is RebalanceAction.BUY
        assert buy.amount == pytest.approx(20.0)
        assert buy.target_value == pytest.approx(50.0)
        assert buy.current_value == pytest.approx(30.0)
        assert buy.drift < 0  # underweight

    def test_sell_order_for_overweight_strategy(self):
        reb = PortfolioRebalancer({"a": 0.5, "b": 0.5}, {"a": 80, "b": 20})
        orders = reb.generate_rebalance_orders()
        sell = next(o for o in orders if o.strategy_id == "a")
        assert sell.action is RebalanceAction.SELL
        assert sell.amount == pytest.approx(30.0)  # 80 - 50

    def test_orders_are_signed_consistently(self):
        reb = PortfolioRebalancer({"a": 0.5, "b": 0.5}, {"a": 10, "b": 90})
        orders = {o.strategy_id: o for o in reb.generate_rebalance_orders()}
        assert orders["a"].action is RebalanceAction.BUY  # needs capital
        assert orders["b"].action is RebalanceAction.SELL  # give back capital

    def test_orders_sum_to_zero_net_dollars(self):
        reb = PortfolioRebalancer({"a": 0.5, "b": 0.5}, {"a": 13, "b": 87})
        orders = reb.generate_rebalance_orders()
        signed = sum(
            o.amount if o.action is RebalanceAction.BUY else -o.amount for o in orders
        )
        assert signed == pytest.approx(0.0, abs=1e-6)

    def test_orders_sorted_by_strategy_id(self):
        reb = PortfolioRebalancer(
            {"a": 0.25, "b": 0.25, "c": 0.25, "d": 0.25},
            {"a": 40, "b": 20, "c": 20, "d": 20},
        )
        ids = [o.strategy_id for o in reb.generate_rebalance_orders()]
        assert ids == sorted(ids)

    def test_zero_capital_yields_no_orders(self):
        reb = PortfolioRebalancer({"a": 0.5, "b": 0.5}, {"a": 0, "b": 0})
        assert reb.generate_rebalance_orders() == []

    def test_float_dust_suppressed(self):
        # 100.0 - 100.0 can produce ~1e-14 dust; no phantom order should fire.
        reb = PortfolioRebalancer({"a": 1.0}, {"a": 100.0})
        assert reb.generate_rebalance_orders() == []

    def test_order_carry_full_provenance(self):
        reb = PortfolioRebalancer({"a": 0.5, "b": 0.5}, {"a": 40, "b": 60})
        order = reb.generate_rebalance_orders()[0]
        assert isinstance(order, RebalanceOrder)
        assert order.current_weight == pytest.approx(0.4)
        assert order.target_weight == pytest.approx(0.5)

    def test_untargeted_held_position_is_targeted_for_exit(self):
        # "b" held but not in targets → fully sold off.
        reb = PortfolioRebalancer({"a": 1.0}, {"a": 50, "b": 50})
        orders = {o.strategy_id: o for o in reb.generate_rebalance_orders()}
        assert orders["b"].action is RebalanceAction.SELL
        assert orders["b"].amount == pytest.approx(50.0)
        assert orders["a"].action is RebalanceAction.BUY

    def test_amounts_rounded_to_cent(self):
        reb = PortfolioRebalancer({"a": 1 / 3, "b": 2 / 3}, {"a": 1.0, "b": 99.0})
        orders = reb.generate_rebalance_orders()
        for o in orders:
            # round(..., 2) produces values with at most 2 decimal places.
            assert round(o.amount, 2) == o.amount


# --------------------------------------------------------------------------- #
# Validation / error paths
# --------------------------------------------------------------------------- #


class TestValidation:
    def test_empty_target_weights_raises(self):
        with pytest.raises(PortfolioRebalancerError, match="must not be empty"):
            PortfolioRebalancer({}, {"a": 100})

    def test_non_dict_target_weights_raises(self):
        with pytest.raises(PortfolioRebalancerError, match="must be a dict"):
            PortfolioRebalancer(None, {"a": 100})  # type: ignore[arg-type]

    def test_non_dict_current_values_raises(self):
        with pytest.raises(PortfolioRebalancerError, match="must be a dict"):
            PortfolioRebalancer({"a": 1}, "nope")  # type: ignore[arg-type]

    def test_negative_current_value_raises(self):
        with pytest.raises(PortfolioRebalancerError, match="non-negative"):
            PortfolioRebalancer({"a": 1}, {"a": -10})

    @pytest.mark.parametrize("bad", [float("inf"), float("nan"), float("-inf")])
    def test_non_finite_value_raises(self, bad):
        with pytest.raises(PortfolioRebalancerError, match="finite"):
            PortfolioRebalancer({"a": bad}, {"a": 100})

    @pytest.mark.parametrize("bad", [float("inf"), float("nan")])
    def test_non_finite_threshold_raises(self, bad):
        with pytest.raises(PortfolioRebalancerError, match="finite"):
            PortfolioRebalancer({"a": 1}, {"a": 100}, threshold=bad)

    def test_threshold_below_zero_raises(self):
        with pytest.raises(PortfolioRebalancerError, match="non-negative"):
            PortfolioRebalancer({"a": 1}, {"a": 100}, threshold=-0.01)

    def test_threshold_above_one_raises(self):
        with pytest.raises(PortfolioRebalancerError, match=r"<= 1.0"):
            PortfolioRebalancer({"a": 1}, {"a": 100}, threshold=1.5)

    def test_bool_value_rejected(self):
        with pytest.raises(PortfolioRebalancerError, match="must be a number"):
            PortfolioRebalancer({"a": True}, {"a": 100})

    def test_bool_current_value_rejected(self):
        with pytest.raises(PortfolioRebalancerError, match="must be a number"):
            PortfolioRebalancer({"a": 1}, {"a": False})

    def test_string_value_rejected(self):
        with pytest.raises(PortfolioRebalancerError, match="must be a number"):
            PortfolioRebalancer({"a": "0.5"}, {"a": 100})  # type: ignore[dict-item]

    def test_none_value_rejected(self):
        with pytest.raises(PortfolioRebalancerError, match="must be a number"):
            PortfolioRebalancer({"a": None}, {"a": 100})  # type: ignore[dict-item]

    def test_non_numeric_threshold_rejected(self):
        with pytest.raises(PortfolioRebalancerError, match="must be a number"):
            PortfolioRebalancer({"a": 1}, {"a": 100}, threshold="0.1")  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# _finite helper (covers allow_zero=False branch)
# --------------------------------------------------------------------------- #


class TestFiniteHelper:
    def test_accepts_int_and_float(self):
        assert _finite(1, "x") == 1.0
        assert _finite(2.5, "x") == 2.5

    def test_rejects_bool(self):
        with pytest.raises(PortfolioRebalancerError, match="must be a number"):
            _finite(True, "x")

    def test_rejects_string(self):
        with pytest.raises(PortfolioRebalancerError, match="must be a number"):
            _finite("1", "x")

    def test_rejects_nan_and_inf(self):
        with pytest.raises(PortfolioRebalancerError, match="finite"):
            _finite(float("nan"), "x")
        with pytest.raises(PortfolioRebalancerError, match="finite"):
            _finite(float("inf"), "x")

    def test_rejects_none(self):
        with pytest.raises(PortfolioRebalancerError, match="must be a number"):
            _finite(None, "x")

    def test_allow_zero_false_rejects_zero(self):
        with pytest.raises(PortfolioRebalancerError, match="non-zero"):
            _finite(0, "x", allow_zero=False)

    def test_allow_zero_false_accepts_nonzero(self):
        assert _finite(5, "x", allow_zero=False) == 5.0

    def test_all_zero_targets_fall_back_to_equal_shares(self):
        # Sum of targets is zero (and non-negative) → equal-share fallback so
        # we never divide by zero and produce NaN weights.
        reb = PortfolioRebalancer({"a": 0, "b": 0}, {"a": 100, "b": 100})
        assert reb.target_weights == pytest.approx({"a": 0.5, "b": 0.5})
        # No NaN must ever leak out.
        assert all(math.isfinite(w) for w in reb.target_weights.values())


# --------------------------------------------------------------------------- #
# RebalanceAction enum
# --------------------------------------------------------------------------- #


class TestRebalanceAction:
    def test_action_values(self):
        assert RebalanceAction.BUY.value == "buy"
        assert RebalanceAction.SELL.value == "sell"

    def test_action_is_comparable(self):
        assert RebalanceAction.BUY is RebalanceAction.BUY
        assert RebalanceAction.BUY is not RebalanceAction.SELL
