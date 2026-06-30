"""
Capital allocation across strategies.

A :class:`CapitalAllocation` records how total deployable capital is split
between competing strategies. Strategy weights always sum to exactly 1.0
(within a small epsilon, to absorb float-accumulation noise), are
individually non-negative, and the number of distinct strategies is capped
by ``max_strategies``.

This model is a pure value object: it owns no state beyond its fields and
performs no I/O. The engine consults it when sizing positions per strategy.
"""

from __future__ import annotations

import math
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator, model_validator

# A weight vector must land within epsilon of 1.0 once summed. Exact float
# equality is brittle (0.1 + 0.2 + 0.3 + 0.4 != 1.0 in IEEE-754), so we allow
# a 1e-9 tolerance — still tight enough to reject a forgotten 5% slice.
_WEIGHT_SUM_EPSILON = 1e-9

# Default cap on how many strategies a single allocation may reference. Keeps
# pathological inputs (thousands of near-zero slices) from sneaking through.
_DEFAULT_MAX_STRATEGIES = 50

# Money is denominated to the cent throughout the cost/allocation stack.
_TWOPLACES = Decimal("0.01")
_ZERO = Decimal("0")


class CapitalAllocation(BaseModel):
    """Capital split across trading strategies.

    ``strategy_weights`` maps a strategy id to its share of ``total_capital``
    (each weight in ``[0.0, 1.0]``). The shares must be non-negative and sum
    to 1.0 (± epsilon), and the number of entries cannot exceed
    ``max_strategies``.

    Notes:
        - An empty ``strategy_weights`` is valid and represents a
          not-yet-deployed allocation; the sum-to-1.0 rule only applies once
          at least one weight is present.
        - Money is quantised to the cent on read-back (see
          :meth:`get_allocation`), matching the convention used by the
          execution-cost modules.
    """

    model_config = {"validate_assignment": True}

    strategy_weights: dict[str, float] = Field(
        default_factory=dict,
        description=(
            "Per-strategy capital share. Each value is in [0.0, 1.0]; "
            "non-empty dicts must sum to 1.0 (± epsilon)."
        ),
    )
    total_capital: Decimal = Field(
        default=_ZERO,
        ge=0,
        description="Total deployable capital backing this allocation (>= 0).",
    )
    max_strategies: int = Field(
        default=_DEFAULT_MAX_STRATEGIES,
        ge=1,
        description="Upper bound on the number of distinct strategies.",
    )

    # ── per-field validation ────────────────────────────────────────── #
    @field_validator("strategy_weights")
    @classmethod
    def _validate_each_weight(cls, weights: dict[str, float]) -> dict[str, float]:
        """Reject non-finite (NaN/Inf), negative, or >1.0 weights, and blank
        strategy ids. ``math.isfinite`` is essential: NaN comparisons all
        return False, so a bare ``w < 0`` check would silently admit NaN."""
        cleaned: dict[str, float] = {}
        for sid, w in weights.items():
            if not isinstance(sid, str) or not sid.strip():
                raise ValueError("strategy id must be a non-empty string")
            if w is None or isinstance(w, bool) or not isinstance(w, int | float):
                raise ValueError(f"weight for {sid!r} must be a number")
            wf = float(w)
            if not math.isfinite(wf):
                raise ValueError(f"weight for {sid!r} must be finite (got {w!r})")
            if wf < 0.0:
                raise ValueError(f"weight for {sid!r} must be non-negative (got {wf})")
            if wf > 1.0:
                raise ValueError(f"weight for {sid!r} must be <= 1.0 (got {wf})")
            cleaned[sid] = wf
        return cleaned

    # ── cross-field validation ──────────────────────────────────────── #
    @model_validator(mode="after")
    def _check_count_and_sum(self) -> CapitalAllocation:
        n = len(self.strategy_weights)
        if n > self.max_strategies:
            raise ValueError(
                f"too many strategies: {n} > max_strategies ({self.max_strategies})"
            )
        if n == 0:
            return self
        total = math.fsum(self.strategy_weights.values())
        if abs(total - 1.0) > _WEIGHT_SUM_EPSILON:
            raise ValueError(
                "strategy_weights must sum to 1.0 (± "
                f"{_WEIGHT_SUM_EPSILON}); got {total!r}"
            )
        return self

    # ── allocation math ─────────────────────────────────────────────── #
    def get_allocation(self, strategy_id: str) -> Decimal:
        """Capital assigned to ``strategy_id``, quantised to the cent.

        Unknown strategies resolve to ``Decimal("0.00")`` rather than
        raising, so callers can safely iterate over an arbitrary set of
        strategy ids (e.g. taken from a position snapshot) without first
        filtering to the allocation's keys.
        """
        weight = self.strategy_weights.get(strategy_id, 0.0)
        if weight <= 0.0:
            return _ZERO
        # Decimal * float is unsupported and direct Decimal(float) carries
        # binary noise; str() gives the shortest round-tripping repr so the
        # multiplication stays exact before cent-level quantisation.
        amount = self.total_capital * Decimal(str(weight))
        return amount.quantize(_TWOPLACES)

    def total_allocated(self) -> Decimal:
        """Sum of every strategy's allocation.

        For a valid allocation this equals ``total_capital`` modulo
        cent-level rounding introduced by :meth:`get_allocation`.
        """
        if not self.strategy_weights:
            return _ZERO
        return sum(
            (self.get_allocation(sid) for sid in self.strategy_weights),
            start=_ZERO,
        )


__all__ = ["CapitalAllocation"]
