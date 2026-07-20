"""Unit tests for :mod:`engine.portfolio.rebalancer`.

The previous ``test_rebalancer.py`` source was removed (only stale bytecode
remained) which is why :class:`PortfolioRebalancer` sat at ~35% coverage.
These tests rebuild that coverage from scratch and pin every observable
behaviour documented in the module:

* validation helpers (:func:`_finite`, :func:`_strip_keys`,
  :func:`_clean_weights`) — the bool/str/NaN/Inf guards and the
  whitespace-key normalisation;
* construction — empty targets, threshold bounds, the all-zero-target
  equal-shares fallback, and the defensive-copy contract;
* introspection / lookups — including the absent-strategy and
  zero-capital short-circuits;
* drift detection — sign semantics, the threshold boundary (``isclose``
  not-noise test) and the zero-capital no-op;
* order generation — BUY/SELL direction, cent rounding, float-dust
  suppression, deterministic ordering and the zero-capital empty result.
"""

from __future__ import annotations

from unittest import mock

import pytest

from engine.portfolio.rebalancer import (
    PortfolioRebalancer,
    PortfolioRebalancerError,
    RebalanceAction,
    RebalanceOrder,
    _clean_weights,
    _finite,
    _strip_keys,
)


# --------------------------------------------------------------------------- #
# Validation helpers
# --------------------------------------------------------------------------- #
class TestFinite:
    """``_finite`` is the single numeric gate; mirror multi_strategy's helper."""

    @pytest.mark.parametrize("bad", [True, False])
    def test_bool_rejected(self, bad: bool) -> None:
        # bool is a subclass of int and would silently coerce to 1.0/0.0.
        with pytest.raises(PortfolioRebalancerError, match="must be a number"):
            _finite(bad, "x")

    @pytest.mark.parametrize("bad", ["0.5", "1", "abc"])
    def test_string_rejected(self, bad: str) -> None:
        with pytest.raises(PortfolioRebalancerError, match="must be a number"):
            _finite(bad, "x")

    @pytest.mark.parametrize("bad", [None, [1.0], object()])
    def test_non_numeric_rejected(self, bad: object) -> None:
        with pytest.raises(PortfolioRebalancerError, match="must be a number"):
            _finite(bad, "x")  # type: ignore[arg-type]

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_rejected(self, bad: float) -> None:
        with pytest.raises(PortfolioRebalancerError, match="must be finite"):
            _finite(bad, "x")

    def test_zero_allowed_by_default(self) -> None:
        assert _finite(0, "x") == 0.0
        assert _finite(0.0, "x") == 0.0

    def test_zero_rejected_when_disallowed(self) -> None:
        # The ``allow_zero=False`` branch (used for non-zero-required fields)
        # must reject exactly 0.0 — but still accept other finite values.
        with pytest.raises(PortfolioRebalancerError, match="must be non-zero"):
            _finite(0.0, "x", allow_zero=False)
        assert _finite(1.0, "x", allow_zero=False) == 1.0

    def test_int_coerced_to_float(self) -> None:
        assert _finite(3, "x") == 3.0
        assert isinstance(_finite(3, "x"), float)


class TestStripKeys:
    """Key normalisation: strip whitespace, reject bad shapes + collisions."""

    def test_non_string_key_rejected(self) -> None:
        with pytest.raises(PortfolioRebalancerError, match="keys must be strings"):
            _strip_keys({1: 1.0}, "w")  # type: ignore[dict-item]

    def test_empty_key_rejected(self) -> None:
        with pytest.raises(PortfolioRebalancerError, match="non-empty strings"):
            _strip_keys({"": 1.0}, "w")

    def test_whitespace_only_key_rejected(self) -> None:
        with pytest.raises(PortfolioRebalancerError, match="non-empty strings"):
            _strip_keys({"   ": 1.0}, "w")

    def test_surrounding_whitespace_stripped(self) -> None:
        out = _strip_keys({"  a  ": 1.0, "b": 2.0}, "w")
        assert out == {"a": 1.0, "b": 2.0}

    def test_whitespace_collision_rejected(self) -> None:
        # "a " and " a" both reduce to "a" — an ambiguous mapping.
        with pytest.raises(PortfolioRebalancerError, match="whitespace-colliding"):
            _strip_keys({"a ": 1.0, " a": 2.0}, "w")


class TestCleanWeights:
    def test_non_dict_rejected(self) -> None:
        with pytest.raises(PortfolioRebalancerError, match="must be a dict"):
            _clean_weights([("a", 1.0)], "w")  # type: ignore[arg-type]

    def test_negative_weight_rejected(self) -> None:
        with pytest.raises(PortfolioRebalancerError, match="non-negative"):
            _clean_weights({"a": -0.1}, "w")

    def test_non_numeric_weight_rejected(self) -> None:
        with pytest.raises(PortfolioRebalancerError, match="must be a number"):
            _clean_weights({"a": "x"}, "w")  # type: ignore[dict-item]

    def test_non_finite_weight_rejected(self) -> None:
        with pytest.raises(PortfolioRebalancerError, match="must be finite"):
            _clean_weights({"a": float("inf")}, "w")

    def test_returns_plain_float_dict(self) -> None:
        out = _clean_weights({"a": 1, "b": 2.0}, "w")
        assert out == {"a": 1.0, "b": 2.0}
        assert all(isinstance(v, float) for v in out.values())


# --------------------------------------------------------------------------- #
# Construction & validation
# --------------------------------------------------------------------------- #
class TestConstruction:
    def test_valid_construction_normalises_targets(self) -> None:
        # Relative weights {"a": 2, "b": 1} normalise to {a: 2/3, b: 1/3}.
        rb = PortfolioRebalancer(
            target_weights={"a": 2, "b": 1},
            current_values={"a": 60.0, "b": 40.0},
        )
        assert rb.target_weights == {"a": pytest.approx(2 / 3), "b": pytest.approx(1 / 3)}
        assert rb.current_values == {"a": 60.0, "b": 40.0}

    def test_empty_targets_rejected(self) -> None:
        with pytest.raises(PortfolioRebalancerError, match="target_weights must not be empty"):
            PortfolioRebalancer(target_weights={}, current_values={"a": 1.0})

    @pytest.mark.parametrize("bad", [-0.01, -1.0])
    def test_negative_threshold_rejected(self, bad: float) -> None:
        with pytest.raises(PortfolioRebalancerError, match="threshold must be non-negative"):
            PortfolioRebalancer({"a": 1.0}, {"a": 1.0}, threshold=bad)

    @pytest.mark.parametrize("bad", [1.0001, 2.0])
    def test_threshold_above_one_rejected(self, bad: float) -> None:
        with pytest.raises(PortfolioRebalancerError, match=r"threshold must be <= 1\.0"):
            PortfolioRebalancer({"a": 1.0}, {"a": 1.0}, threshold=bad)

    def test_non_finite_threshold_rejected(self) -> None:
        with pytest.raises(PortfolioRebalancerError, match="must be finite"):
            PortfolioRebalancer({"a": 1.0}, {"a": 1.0}, threshold=float("nan"))

    def test_non_numeric_threshold_rejected(self) -> None:
        with pytest.raises(PortfolioRebalancerError, match="must be a number"):
            PortfolioRebalancer({"a": 1.0}, {"a": 1.0}, threshold="x")  # type: ignore[arg-type]

    def test_all_zero_targets_fall_back_to_equal_shares(self) -> None:
        # Sum of targets is 0 (all zero) — guard avoids div-by-zero NaN by
        # distributing equal shares.
        rb = PortfolioRebalancer(
            target_weights={"a": 0.0, "b": 0.0},
            current_values={"a": 50.0, "b": 50.0},
        )
        assert rb.target_weights == {"a": 0.5, "b": 0.5}

    def test_zero_current_values_is_valid(self) -> None:
        rb = PortfolioRebalancer({"a": 1.0}, {})
        assert rb.total_capital == 0.0
        assert rb.current_values == {}

    def test_current_values_may_include_untracked_strategy(self) -> None:
        # A current position absent from targets is fully overweight (exit).
        rb = PortfolioRebalancer({"a": 1.0}, {"a": 50.0, "ghost": 50.0})
        assert "ghost" in rb.strategy_ids


# --------------------------------------------------------------------------- #
# Introspection & lookups
# --------------------------------------------------------------------------- #
class TestIntrospection:
    def test_threshold_property(self) -> None:
        rb = PortfolioRebalancer({"a": 1.0}, {"a": 100.0}, threshold=0.1)
        assert rb.threshold == 0.1

    def test_default_threshold_is_5pct(self) -> None:
        rb = PortfolioRebalancer({"a": 1.0}, {"a": 100.0})
        assert rb.threshold == pytest.approx(0.05)

    def test_total_capital_sums_current_values(self) -> None:
        rb = PortfolioRebalancer({"a": 1.0}, {"a": 30.0, "b": 20.0})
        assert rb.total_capital == 50.0

    def test_strategy_ids_is_sorted_union(self) -> None:
        rb = PortfolioRebalancer({"b": 1.0, "a": 1.0}, {"a": 1.0, "c": 1.0})
        assert rb.strategy_ids == ["a", "b", "c"]

    def test_target_weights_snapshot_is_independent_copy(self) -> None:
        rb = PortfolioRebalancer({"a": 1.0}, {"a": 1.0})
        snap = rb.target_weights
        snap["a"] = 999.0  # mutating the snapshot must not corrupt internals
        assert rb.target_weights == {"a": 1.0}

    def test_current_values_snapshot_is_independent_copy(self) -> None:
        rb = PortfolioRebalancer({"a": 1.0}, {"a": 10.0})
        snap = rb.current_values
        snap["a"] = 999.0
        assert rb.current_values == {"a": 10.0}

    def test_target_weight_absent_strategy_is_zero(self) -> None:
        rb = PortfolioRebalancer({"a": 1.0}, {"a": 1.0, "ghost": 1.0})
        assert rb.target_weight("ghost") == 0.0
        assert rb.target_weight("a") == 1.0

    def test_current_value_absent_strategy_is_zero(self) -> None:
        rb = PortfolioRebalancer({"a": 1.0, "b": 1.0}, {"a": 10.0})
        assert rb.current_value("b") == 0.0
        assert rb.current_value("a") == 10.0

    def test_current_weight_zero_capital_short_circuits_to_zero(self) -> None:
        rb = PortfolioRebalancer({"a": 1.0}, {})
        assert rb.current_weight("a") == 0.0

    def test_current_weight_normal_case(self) -> None:
        rb = PortfolioRebalancer({"a": 1.0}, {"a": 75.0, "b": 25.0})
        assert rb.current_weight("a") == pytest.approx(0.75)
        assert rb.current_weight("b") == pytest.approx(0.25)


# --------------------------------------------------------------------------- #
# Drift detection
# --------------------------------------------------------------------------- #
class TestDrift:
    def test_compute_drift_signs(self) -> None:
        # Overweight (holding more than target share) -> positive drift;
        # underweight -> negative.
        rb = PortfolioRebalancer(
            target_weights={"a": 0.5, "b": 0.5},
            current_values={"a": 60.0, "b": 40.0},
        )
        drift = rb.compute_drift()
        assert drift["a"] == pytest.approx(0.1)   # 0.6 - 0.5
        assert drift["b"] == pytest.approx(-0.1)  # 0.4 - 0.5

    def test_compute_drift_covers_union_of_strategies(self) -> None:
        rb = PortfolioRebalancer({"a": 1.0}, {"a": 50.0, "ghost": 50.0})
        assert set(rb.compute_drift()) == {"a", "ghost"}
        # ghost is fully overweight (0 target, 0.5 current).
        assert rb.compute_drift()["ghost"] == pytest.approx(0.5)

    def test_max_drift_is_largest_absolute(self) -> None:
        rb = PortfolioRebalancer(
            target_weights={"a": 0.5, "b": 0.5},
            current_values={"a": 60.0, "b": 40.0},
        )
        assert rb.max_drift() == pytest.approx(0.1)

    def test_needs_rebalance_true_above_threshold(self) -> None:
        rb = PortfolioRebalancer(
            target_weights={"a": 0.5, "b": 0.5},
            current_values={"a": 60.0, "b": 40.0},
            threshold=0.05,
        )
        assert rb.needs_rebalance() is True

    def test_needs_rebalance_false_within_threshold(self) -> None:
        rb = PortfolioRebalancer(
            target_weights={"a": 0.5, "b": 0.5},
            current_values={"a": 52.0, "b": 48.0},  # drift 0.02 < 0.05
            threshold=0.05,
        )
        assert rb.needs_rebalance() is False

    def test_needs_rebalance_false_on_threshold_boundary(self) -> None:
        # A drift sitting exactly on the threshold is treated as on it
        # (math.isclose) and must NOT trip a rebalance.
        rb = PortfolioRebalancer(
            target_weights={"a": 0.5, "b": 0.5},
            current_values={"a": 60.0, "b": 40.0},  # drift 0.1
            threshold=0.1,
        )
        assert rb.needs_rebalance() is False

    def test_needs_rebalance_false_with_zero_capital(self) -> None:
        rb = PortfolioRebalancer({"a": 1.0}, {})
        assert rb.needs_rebalance() is False

    def test_max_drift_raises_on_empty_drifts_invariant_violation(self) -> None:
        # Empty ``target_weights`` is rejected at construction, so under the
        # public API ``compute_drift`` is never empty and the guard in
        # ``max_drift`` is unreachable. Patch ``compute_drift`` to return an
        # empty mapping, forcing the broken invariant to surface as a
        # PortfolioRebalancerError instead of being masked by a silent 0.0.
        # Using a mock (rather than mutating private ``_target_weights`` /
        # ``_current_values``) keeps the test decoupled from internal state
        # and makes the precondition explicit.
        rb = PortfolioRebalancer({"a": 1.0}, {"a": 100.0})
        assert len(rb.compute_drift()) > 0  # sanity: non-empty in normal state

        with mock.patch.object(rb, "compute_drift", return_value={}), pytest.raises(
            PortfolioRebalancerError, match="drifts unexpectedly empty"
        ):
            rb.max_drift()


# --------------------------------------------------------------------------- #
# Order generation
# --------------------------------------------------------------------------- #
class TestRebalanceOrders:
    def test_buy_and_sell_emitted_for_drifted_strategies(self) -> None:
        rb = PortfolioRebalancer(
            target_weights={"a": 0.5, "b": 0.5},
            current_values={"a": 60.0, "b": 40.0},
        )
        orders = rb.generate_rebalance_orders()
        by_sid = {o.strategy_id: o for o in orders}
        # 'a' is overweight -> SELL 10; 'b' is underweight -> BUY 10.
        assert by_sid["a"].action is RebalanceAction.SELL
        assert by_sid["a"].amount == pytest.approx(10.0)
        assert by_sid["b"].action is RebalanceAction.BUY
        assert by_sid["b"].amount == pytest.approx(10.0)

    def test_orders_sorted_by_strategy_id(self) -> None:
        rb = PortfolioRebalancer(
            target_weights={"c": 1 / 3, "b": 1 / 3, "a": 1 / 3},
            current_values={"a": 10.0, "b": 20.0, "c": 70.0},
        )
        orders = rb.generate_rebalance_orders()
        assert [o.strategy_id for o in orders] == sorted(o.strategy_id for o in orders)

    def test_order_carries_full_provenance(self) -> None:
        rb = PortfolioRebalancer(
            target_weights={"a": 0.5, "b": 0.5},
            current_values={"a": 60.0, "b": 40.0},
        )
        order = rb.generate_rebalance_orders()[0]
        assert isinstance(order, RebalanceOrder)
        # current/target values + weights + drift are all populated.
        assert order.current_value == pytest.approx(60.0)
        assert order.target_value == pytest.approx(50.0)
        assert order.current_weight == pytest.approx(0.6)
        assert order.target_weight == pytest.approx(0.5)
        assert order.drift == pytest.approx(0.1)

    def test_amounts_rounded_to_cent(self) -> None:
        # target_value = 1/3 * 100 = 33.333..., current = 0 -> BUY 33.33.
        rb = PortfolioRebalancer(
            target_weights={"a": 1 / 3, "b": 1 / 3, "c": 1 / 3},
            current_values={"a": 0.0, "b": 0.0, "c": 100.0},
        )
        orders = {o.strategy_id: o for o in rb.generate_rebalance_orders()}
        # Rounded to 2 decimal places.
        assert orders["a"].amount == round(100.0 / 3.0, 2)
        assert orders["b"].amount == round(100.0 / 3.0, 2)

    def test_on_target_portfolio_emits_no_orders(self) -> None:
        rb = PortfolioRebalancer(
            target_weights={"a": 0.5, "b": 0.5},
            current_values={"a": 50.0, "b": 50.0},
        )
        assert rb.generate_rebalance_orders() == []

    def test_zero_capital_emits_no_orders(self) -> None:
        rb = PortfolioRebalancer({"a": 1.0}, {})
        assert rb.generate_rebalance_orders() == []

    def test_untracked_current_position_targeted_for_exit(self) -> None:
        # A strategy present in current_values but absent from targets is
        # fully overweight and must be sold down to zero.
        rb = PortfolioRebalancer(
            target_weights={"a": 1.0},
            current_values={"a": 50.0, "ghost": 50.0},
        )
        orders = {o.strategy_id: o for o in rb.generate_rebalance_orders()}
        assert orders["ghost"].action is RebalanceAction.SELL
        assert orders["ghost"].amount == pytest.approx(50.0)

    def test_untargeted_strategy_with_zero_current_emits_entry_order(self) -> None:
        # A target strategy with no current position is fully underweight ->
        # a BUY order to build the target value.
        rb = PortfolioRebalancer(
            target_weights={"a": 0.5, "b": 0.5},
            current_values={"a": 100.0, "b": 0.0},
        )
        orders = {o.strategy_id: o for o in rb.generate_rebalance_orders()}
        assert orders["b"].action is RebalanceAction.BUY
        assert orders["b"].amount == pytest.approx(50.0)


# --------------------------------------------------------------------------- #
# Public enum surface
# --------------------------------------------------------------------------- #
class TestRebalanceActionEnum:
    def test_values(self) -> None:
        assert RebalanceAction.BUY.value == "buy"
        assert RebalanceAction.SELL.value == "sell"
