"""Portfolio rebalancing — drift detection and target-weight adjustment.

A :class:`PortfolioRebalancer` compares a multi-strategy portfolio's
*target* allocations (the policy the operator wants to hold) against its
*current* dollar allocation (the live state, which drifts as strategies
gain and lose value at different rates). It answers three questions:

1. **How far has each strategy drifted?** — :meth:`compute_drift` returns
   the signed weight deviation ``current_weight - target_weight`` per
   strategy (positive = overweight, negative = underweight).
2. **Is a rebalance warranted?** — :meth:`needs_rebalance` compares the
   largest absolute drift against a configurable ``threshold`` (default
   ``0.05`` = 5%).
3. **What trades reach the targets?** — :meth:`generate_rebalance_orders`
   emits :class:`RebalanceOrder` *signals* — one per strategy whose dollar
   value differs from target — that a downstream execution layer can turn
   into actual capital transfers.

Design notes
------------
* **Pure / no I/O.** The rebalancer is a synchronous, stateless-in-practice
  function over its construction inputs (target weights, current values,
  threshold). It performs no network calls and never places a trade; the
  orders it produces are advisory signals only. This keeps the scope tight
  and matches the rest of the ``engine.portfolio`` package, which owns no
  execution.
* **Robust to NaN/Inf/bool.** Every numeric input is funnelled through
  :func:`_finite`, which rejects ``bool`` (a sneaky subclass of ``int``
  that would otherwise coerce to ``1.0``/``0.0``), non-numeric strings,
  ``None``, and non-finite values. ``math.isfinite`` is the gate because
  bare comparisons (``w < 0``) silently admit NaN.
* **Zero-capital is a valid no-op.** When total current value is ``0``
  there are no current weights to drift from, nothing to rebalance, and no
  orders can move money — mirroring :class:`MultiStrategyPortfolio`'s
  zero-capital short-circuit.
* **Relative target weights.** Target weights are normalised internally so
  ``{"a": 1, "b": 1}`` is treated identically to ``{"a": 0.5, "b": 0.5}``,
  matching :class:`MultiStrategyPortfolio`'s relative-weight convention.

The class owns no global state.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import structlog

logger = structlog.get_logger()


class PortfolioRebalancerError(ValueError):
    """Bad rebalancer configuration: a non-finite / negative / out-of-range
    weight, value, or threshold, or an empty target allocation."""


class RebalanceAction(StrEnum):
    """Direction of a :class:`RebalanceOrder`.

    ``BUY`` means add capital to the strategy (it is underweight);
    ``SELL`` means withdraw capital (it is overweight).
    """

    BUY = "buy"
    SELL = "sell"


# Below this |dollar delta| no order is emitted: it is float dust (e.g.
# 100.0 - 100.0 == 1.4e-14) rather than a genuine rebalance. Using a tiny
# absolute floor rather than a relative one keeps a $0.0001 phantom off the
# book without suppressing a real sub-cent adjustment.
_ORDER_EPSILON = 1e-9

# Default drift threshold (5%). A strategy whose |current - target| weight
# exceeds this trips :meth:`needs_rebalance`.
_DEFAULT_THRESHOLD = 0.05


@dataclass(frozen=True)
class RebalanceOrder:
    """One strategy's target-weight adjustment signal.

    ``action`` is the direction needed to move ``current_value`` toward
    ``target_value``; ``amount`` is always the (positive) dollars to
    buy/sell. The full provenance — current/target weights and the signed
    drift — is carried along so an audit trail can reconstruct *why* the
    order was emitted without re-deriving the inputs.
    """

    strategy_id: str
    action: RebalanceAction
    amount: float
    current_value: float
    target_value: float
    current_weight: float
    target_weight: float
    drift: float


# --------------------------------------------------------------------- #
# Validation helpers (module-private)
# --------------------------------------------------------------------- #


def _finite(value: Any, label: str, *, allow_zero: bool = True) -> float:
    """Coerce ``value`` to ``float`` and require it to be a finite number.

    Mirrors the homonymous helper in ``multi_strategy.py``: strings (even
    numeric ones like ``"0.5"``) are rejected outright so a caller cannot
    smuggle in a value that ``float()`` would parse with surprising
    semantics; only genuine ``int``/``float`` are accepted. ``bool`` is a
    subclass of ``int`` and is rejected explicitly — without this guard it
    would silently coerce to ``1.0``/``0.0``.
    """
    if isinstance(value, bool):
        raise PortfolioRebalancerError(f"{label} must be a number, got {value!r}")
    if isinstance(value, str):
        raise PortfolioRebalancerError(f"{label} must be a number, got {value!r}")
    try:
        num = float(value)
    except (TypeError, ValueError) as exc:
        raise PortfolioRebalancerError(f"{label} must be a number, got {value!r}") from exc
    if not math.isfinite(num):
        raise PortfolioRebalancerError(f"{label} must be finite, got {value!r}")
    if not allow_zero and num == 0.0:
        raise PortfolioRebalancerError(f"{label} must be non-zero")
    return num


def _strip_keys(raw: dict[str, Any], label: str) -> dict[str, Any]:
    """Return ``raw`` with each key's surrounding whitespace stripped,
    raising :class:`PortfolioRebalancerError` on a non-string key, a key
    that is empty or only whitespace, or a *whitespace collision*: two
    distinct raw keys that reduce to the same stripped id.

    Surrounding whitespace on a strategy id is almost always a typo
    (``"a "`` vs ``" a"``). Silently keeping both would let the rebalancer
    hold a phantom position it can never reference by name, so the keys are
    normalised and a collision — which makes the intended mapping ambiguous
    — is rejected outright. A key that is empty or only whitespace names no
    strategy and is rejected for the same reason.

    Deduplication is dict-based: the accumulator is keyed by the stripped
    form, so a second key that reduces to a form already present is caught
    at the point of collision. The stripped key keeps its original value,
    preserving the correct key/value correspondence.
    """
    stripped: dict[str, Any] = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            raise PortfolioRebalancerError(
                f"{label} keys must be strings, got {key!r}"
            )
        norm = key.strip()
        if not norm:
            raise PortfolioRebalancerError(
                f"{label} keys must be non-empty strings, got {key!r}"
            )
        if norm in stripped:
            raise PortfolioRebalancerError(
                f"{label} has whitespace-colliding keys: {key!r} shadows "
                f"another key that also reduces to {norm!r}"
            )
        stripped[norm] = value
    return stripped


def _clean_weights(raw: dict[str, Any], label: str) -> dict[str, float]:
    """Validate a ``{strategy_id: weight}`` mapping into a plain
    ``dict[str, float]`` of finite, non-negative values keyed by non-empty,
    whitespace-stripped string ids.

    Keys are whitespace-stripped via :func:`_strip_keys` first, so
    ``{" a ": 1}`` is treated as ``{"a": 1.0}`` and a whitespace collision
    (e.g. ``{"a ": 1, " a": 2}``) raises before it can create a phantom
    strategy. The stripped key keeps its original value, preserving the
    correct key/value correspondence. Returns a *copy* so callers cannot
    mutate the rebalancer's internal map.
    """
    if not isinstance(raw, dict):
        raise PortfolioRebalancerError(f"{label} must be a dict, got {type(raw).__name__}")
    cleaned: dict[str, float] = {}
    for sid, w in _strip_keys(raw, label).items():
        cleaned[sid] = _finite(w, f"{label}[{sid!r}]")
        if cleaned[sid] < 0.0:
            raise PortfolioRebalancerError(
                f"{label}[{sid!r}] must be non-negative, got {cleaned[sid]}"
            )
    return cleaned


# --------------------------------------------------------------------- #
# The rebalancer
# --------------------------------------------------------------------- #


class PortfolioRebalancer:
    """Drift detector and target-weight order generator for a
    multi-strategy portfolio.

    Construct once with the *target* policy weights and the *current*
    dollar allocation per strategy, then query :meth:`compute_drift`,
    :meth:`needs_rebalance`, and :meth:`generate_rebalance_orders`.

    Parameters
    ----------
    target_weights:
        Desired share of total capital per strategy. Each value is in
        ``[0.0, ∞)``; weights are **normalised** internally so they need not
        sum to 1.0 (``{"a": 1, "b": 1}`` ≡ ``{"a": 0.5, "b": 0.5}``).
    current_values:
        Live dollar allocation per strategy (``>= 0``). Strategies may appear
        here that are absent from the targets (they will be targeted for
        exit) and vice-versa (they will be targeted for entry).
    threshold:
        Drift (|current_weight - target_weight|) above which a rebalance is
        warranted. Must be a finite number in ``[0.0, 1.0]``; default
        ``0.05`` (5%).

    Raises
    ------
    PortfolioRebalancerError
        On any non-finite/negative/out-of-range input, an empty
        ``target_weights``, or a non-dict mapping argument.
    """

    def __init__(
        self,
        target_weights: dict[str, float],
        current_values: dict[str, float],
        *,
        threshold: float = _DEFAULT_THRESHOLD,
    ) -> None:
        targets = _clean_weights(target_weights, "target_weights")
        if not targets:
            raise PortfolioRebalancerError("target_weights must not be empty")
        current = _clean_weights(current_values, "current_values")

        thr = _finite(threshold, "threshold")
        if thr < 0.0:
            raise PortfolioRebalancerError(f"threshold must be non-negative, got {thr}")
        if thr > 1.0:
            raise PortfolioRebalancerError(f"threshold must be <= 1.0, got {thr}")

        # Normalise targets to sum to 1.0 so a caller that passes relative
        # weights (e.g. {"a": 2, "b": 1}) gets the expected behaviour.
        # Division is safe: ``targets`` is non-empty and non-negative with
        # at least one positive value is *not* guaranteed by validation
        # alone (all-zero is legal), so guard the zero-sum case explicitly
        # and fall back to equal shares — otherwise we'd divide by zero and
        # produce NaN weights that slip past downstream checks.
        target_sum = sum(targets.values())
        if target_sum <= 0.0:
            share = 1.0 / len(targets)
            self._target_weights = dict.fromkeys(targets, share)
        else:
            self._target_weights = {sid: w / target_sum for sid, w in targets.items()}

        # Defensive copies so external mutation of the caller's dicts cannot
        # corrupt the rebalancer after construction.
        self._current_values = dict(current)
        self._threshold = thr

        self._total_capital = float(sum(self._current_values.values()))

        logger.info(
            "portfolio.rebalancer.init",
            strategies=len(targets),
            total_capital=self._total_capital,
            threshold=thr,
        )

    # -- introspection -------------------------------------------------

    @property
    def threshold(self) -> float:
        """Configured drift threshold (the level above which a rebalance is
        triggered)."""
        return self._threshold

    @property
    def total_capital(self) -> float:
        """Sum of all current strategy values (the capital being
        rebalanced). Zero for an empty / all-zero current state."""
        return self._total_capital

    @property
    def strategy_ids(self) -> list[str]:
        """Union of target and current strategy ids (sorted for stable
        iteration)."""
        return sorted(set(self._target_weights) | set(self._current_values))

    @property
    def target_weights(self) -> dict[str, float]:
        """Snapshot of the normalised target weights (sum to 1.0)."""
        return dict(self._target_weights)

    @property
    def current_values(self) -> dict[str, float]:
        """Snapshot of the current per-strategy dollar values."""
        return dict(self._current_values)

    # -- core weight lookups ------------------------------------------

    def target_weight(self, strategy_id: str) -> float:
        """Normalised target weight for ``strategy_id``.

        Strategies absent from the targets resolve to ``0.0`` (a current
        position in such a strategy is fully overweight and targeted for
        exit). Lookup is total — callers never need to pre-filter keys.
        """
        return self._target_weights.get(strategy_id, 0.0)

    def current_value(self, strategy_id: str) -> float:
        """Current dollar value for ``strategy_id`` (``0.0`` if absent)."""
        return self._current_values.get(strategy_id, 0.0)

    def current_weight(self, strategy_id: str) -> float:
        """Current share of total capital held by ``strategy_id``.

        Returns ``0.0`` when total capital is zero (there is nothing to
        weight), so downstream drift math stays finite instead of
        dividing by zero.
        """
        if self._total_capital <= 0.0:
            return 0.0
        return self.current_value(strategy_id) / self._total_capital

    # -- drift detection ----------------------------------------------

    def compute_drift(self) -> dict[str, float]:
        """Return the signed weight deviation per strategy.

        ``drift[strategy_id] = current_weight - target_weight``. Positive
        means the strategy is **overweight** (holding more than its policy
        share — capital should be withdrawn); negative means
        **underweight** (capital should be added).

        With zero total capital every current weight is ``0.0``, so the
        drift for each target strategy is ``-target_weight`` and for any
        held-but-untargeted strategy it is ``0.0``. :meth:`needs_rebalance`
        treats this zero-capital state as a no-op regardless.

        Returns a dict keyed by every strategy in the union of targets and
        current values.
        """
        drifts: dict[str, float] = {}
        for sid in self.strategy_ids:
            drifts[sid] = self.current_weight(sid) - self.target_weight(sid)
        return drifts

    def max_drift(self) -> float:
        """Largest absolute drift across all strategies (``0.0`` when the
        union of strategies is empty — only reachable if both inputs are
        empty, which is otherwise rejected at construction)."""
        drifts = self.compute_drift()
        if not drifts:
            return 0.0
        return max(abs(d) for d in drifts.values())

    def needs_rebalance(self) -> bool:
        """True when any strategy's absolute drift strictly exceeds the
        configured ``threshold``.

        The comparison is strict (``drift > threshold``): a strategy
        sitting *exactly* on the threshold is considered within tolerance
        and does not trip a rebalance. Because floating-point rounding can
        render an exactly-at-threshold drift as ``threshold + ε`` (which a
        naive ``>`` would spuriously trip) or ``threshold - ε`` (which it
        would spuriously suppress), values that are *close* to the
        threshold (per :func:`math.isclose` with tight tolerances) are
        treated as being on it and therefore not triggering. Only a drift
        that is genuinely — not just noisily — larger trips a rebalance.
        This boundary is deliberate and pinned by tests; loosen the
        tolerances only with care.

        Zero total capital never needs rebalancing (nothing can move), so
        this returns ``False`` regardless of the computed drifts.
        """
        if self._total_capital <= 0.0:
            return False
        drift = self.max_drift()
        if math.isclose(drift, self._threshold, rel_tol=1e-9, abs_tol=1e-12):
            return False
        return drift > self._threshold

    # -- order generation ---------------------------------------------

    def generate_rebalance_orders(self) -> list[RebalanceOrder]:
        """Emit one :class:`RebalanceOrder` per strategy whose current
        dollar value differs (beyond float dust) from its target value.

        ``target_value = target_weight * total_capital`` and
        ``delta = target_value - current_value``: a positive delta is a
        ``BUY`` (add ``delta`` dollars), a negative delta is a ``SELL`` (of
        ``|delta|`` dollars). Amounts are rounded to the cent. Orders are
        returned sorted by strategy id for deterministic output.

        When ``total_capital`` is zero there is no money to move and no
        target value is reachable, so an empty list is returned.

        The returned orders are advisory *signals* — nothing here executes
        them. A portfolio that is already on target yields an empty list
        (every delta falls within :data:`_ORDER_EPSILON`).
        """
        if self._total_capital <= 0.0:
            return []

        orders: list[RebalanceOrder] = []
        drifts = self.compute_drift()
        for sid in self.strategy_ids:
            target_value = self.target_weight(sid) * self._total_capital
            current_value = self.current_value(sid)
            delta = target_value - current_value
            if abs(delta) <= _ORDER_EPSILON:
                continue  # already on target — float dust, not a real trade
            action = RebalanceAction.BUY if delta > 0 else RebalanceAction.SELL
            orders.append(
                RebalanceOrder(
                    strategy_id=sid,
                    action=action,
                    amount=round(abs(delta), 2),
                    current_value=round(current_value, 2),
                    target_value=round(target_value, 2),
                    current_weight=self.current_weight(sid),
                    target_weight=self.target_weight(sid),
                    drift=drifts[sid],
                )
            )
        return orders


__all__ = [
    "PortfolioRebalancer",
    "PortfolioRebalancerError",
    "RebalanceAction",
    "RebalanceOrder",
]
