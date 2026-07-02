"""
Capital allocation across strategies.

A :class:`CapitalAllocation` records how total deployable capital is split
between competing strategies. Strategy weights always sum to exactly 1.0
(within a small epsilon, to absorb float-accumulation noise), are
individually non-negative, and the number of distinct strategies is capped
by ``max_strategies``.

Immutability (mutation is blocked)
----------------------------------
This is a frozen value object. In-place mutation is blocked on two levels:

- **Field assignment** (``alloc.total_capital = ...``,
  ``alloc.strategy_weights = ...``) raises ``ValidationError`` because the
  model is declared ``frozen``.
- **Mapping mutation** (``alloc.strategy_weights["x"] = 0.5``) raises
  ``TypeError`` because the stored weights are wrapped in a read-only
  :class:`types.MappingProxyType`.

Callers that need a changed allocation must go through
``model_copy(update={...})``. The default Pydantic ``model_copy`` splats the
``update`` dict straight into ``__dict__`` and skips validation, which would
silently bypass the weight constraints. We override it to rebuild through
``model_validate`` so every validator re-runs on the merged field set —
exactly mirroring the pattern used by :class:`engine.core.instruments.Instrument`.

The model owns no state beyond its fields and performs no I/O.
"""

from __future__ import annotations

import math
from decimal import Decimal
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

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
        - The model is **frozen**: mutate via ``model_copy(update={...})``,
          which re-runs every validator (see :meth:`model_copy`).
        - An empty ``strategy_weights`` is valid and represents a
          not-yet-deployed allocation; the sum-to-1.0 rule only applies once
          at least one weight is present.
        - Money is quantised to the cent on read-back (see
          :meth:`get_allocation`), matching the convention used by the
          execution-cost modules.
    """

    # `frozen` blocks field assignment; `validate_assignment` is kept for
    # clarity and is a no-op in practice once `frozen` is on (assignment is
    # rejected before validation runs).
    model_config = ConfigDict(frozen=True, validate_assignment=True)

    strategy_weights: dict[str, float] = Field(
        default_factory=dict,
        description=(
            "Per-strategy capital share. Each value is in [0.0, 1.0]; "
            "non-empty dicts must sum to 1.0 (± epsilon). Read-only on the "
            "instance (wrapped in a MappingProxyType)."
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
    # Runs in ``mode="before"`` so we inspect the *raw* input before
    # Pydantic's ``dict[str, float]`` coercion. That matters for two cases
    # the default (``after``) mode cannot catch:
    #   - ``True``/``False``: ``bool`` is a subclass of ``int``, so by the time
    #     Pydantic coerces the field it has already turned ``True`` into
    #     ``1.0`` and the bool guard below could never fire. Rejecting before
    #     coercion is the only way to see the original type.
    #   - ``None`` / ``"half"``: Pydantic's float coercion would raise its own
    #     ``float_type``/``float_parsing`` errors; running first lets us emit
    #     the uniform ``"must be a number"`` message the rest of the stack
    #     matches.
    @field_validator("strategy_weights", mode="before")
    @classmethod
    def _validate_each_weight(cls, weights: Any) -> dict[str, float]:
        """Reject non-numeric, non-finite (NaN/Inf), negative, or >1.0 weights
        and blank strategy ids. ``math.isfinite`` is essential: NaN
        comparisons all return False, so a bare ``w < 0`` check would silently
        admit NaN.

        The non-negative check runs in a dedicated first pass so a negative
        weight is always reported ahead of a stray >1.0 weight in the same
        dict — otherwise ``{"a": 1.5, "b": -0.5}`` would surface the >1.0
        error for ``a`` first (iteration is insertion order) and mask the
        real problem on ``b``.
        """
        # Accept plain dicts and read-only mappings (a frozen instance holds
        # its weights in a MappingProxyType); anything else is a type error.
        if isinstance(weights, dict):
            items = weights.items()
        elif hasattr(weights, "items"):  # MappingProxyType and other mappings
            items = dict(weights).items()
        else:
            raise ValueError("strategy_weights must be a dict")

        cleaned: dict[str, float] = {}
        # Pass 1: id shape, value type (a real number, not bool/None/str),
        # finiteness, and non-negativity.
        for sid, w in items:
            if not isinstance(sid, str) or not sid.strip():
                raise ValueError("strategy id must be a non-empty string")
            if w is None or isinstance(w, bool) or not isinstance(w, int | float):
                raise ValueError(f"weight for {sid!r} must be a number")
            wf = float(w)
            if not math.isfinite(wf):
                raise ValueError(f"weight for {sid!r} must be finite (got {w!r})")
            if wf < 0.0:
                raise ValueError(f"weight for {sid!r} must be non-negative (got {wf})")
            cleaned[sid] = wf
        # Pass 2: upper bound. Kept separate so a pass-1 non-negative failure
        # always wins the race when both kinds of error are present.
        for sid, wf in cleaned.items():
            if wf > 1.0:
                raise ValueError(f"weight for {sid!r} must be <= 1.0 (got {wf})")
        return cleaned

    # ── serialization: dump the frozen mapping as a plain dict ──────── #
    # The instance holds a read-only MappingProxyType (see
    # ``_check_count_sum_and_freeze``) so callers cannot mutate weights in
    # place. Pydantic's generated serializer, however, is typed for
    # ``dict[str, float]`` and warns when it meets a mappingproxy. Convert
    # back to a plain dict on dump so ``model_dump()`` / ``model_dump_json()``
    # produce a normal, JSON-friendly value.
    @field_serializer("strategy_weights")
    def _serialize_weights(self, weights: Mapping[str, float]) -> dict[str, float]:
        return dict(weights)

    # ── cross-field validation + immutability wrap ─────────────────── #
    @model_validator(mode="after")
    def _check_count_sum_and_freeze(self) -> CapitalAllocation:
        n = len(self.strategy_weights)
        if n > self.max_strategies:
            raise ValueError(f"too many strategies: {n} > max_strategies ({self.max_strategies})")
        if n > 0:
            # Sum via ``Decimal(str(w))`` rather than raw float addition: each
            # weight's ``str()`` is its shortest round-tripping repr, so the
            # decimal sum carries no binary-accumulation noise and the epsilon
            # only ever has to absorb genuine representation slop (e.g. three
            # copies of a truncated 1/3). Comparing in Decimal-land keeps the
            # tolerance exact instead of compounding float error into the
            # tolerance check itself.
            total = sum(
                (Decimal(str(w)) for w in self.strategy_weights.values()),
                start=_ZERO,
            )
            if abs(total - Decimal("1")) > Decimal(str(_WEIGHT_SUM_EPSILON)):
                raise ValueError(
                    f"strategy_weights must sum to 1.0 (± {_WEIGHT_SUM_EPSILON}); got {total}"
                )
        # Block in-place mutation of the weights dict. `frozen` above only
        # guards field reassignment; this wrap makes item-level mutation
        # (`alloc.strategy_weights["x"] = ...`, `del`, `pop`, `clear`) raise
        # TypeError. object.__setattr__ is the documented escape hatch for
        # mutating a frozen Pydantic model from inside a validator.
        object.__setattr__(
            self,
            "strategy_weights",
            MappingProxyType(dict(self.strategy_weights)),
        )
        return self

    # ── copy that re-runs validation ────────────────────────────────── #
    def model_copy(  # type: ignore[override]
        self,
        *,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> CapitalAllocation:
        """Return a copy, re-running every validator on the new field set.

        Pydantic's default ``model_copy`` short-circuits validation: it
        splats the ``update`` values straight into ``__dict__``. Because the
        model is frozen, normal assignment (``alloc.strategy_weights = ...``)
        is blocked, so the only sanctioned way to change a field is this
        method — and it must re-validate, otherwise a caller could produce an
        allocation whose weights no longer sum to 1.0. We merge ``update``
        over ``model_dump()`` and rebuild through ``model_validate``. With no
        ``update`` we fall through to the cheap parent copy.
        """
        if not update:
            return super().model_copy(update=update, deep=deep)
        merged: dict[str, Any] = {**self.model_dump(), **update}
        return type(self).model_validate(merged)

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
