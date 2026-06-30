"""Capital allocation across strategies (largest-remainder / Hamilton).

Splits a ``total_capital`` dollar amount across strategies proportional
to ``strategy_weights`` using the largest-remainder (a.k.a. Hamilton)
apportionment method, computed in fixed-point ``Decimal`` so the result
is exact to the cent:

1. For each strategy compute the *raw* share = ``total * weight / Σ``.
2. Floor each raw share to the cent (``ROUND_DOWN``).
3. The fractional cents dropped in step 2 form a remainder, in whole
   cents.
4. Distribute the remaining cents one-by-one to the strategies with the
   largest fractional parts until the remainder is exhausted.

Guarantees
----------
- **Exact-sum invariant**: the returned amounts always sum to exactly
  ``total_capital`` (to the cent), even for inputs like ``$100`` split
  across 3 strategies by equal ``1/3`` weights.
- **Immutability**: the returned mapping is a frozen
  :class:`types.MappingProxyType`; in-place mutation (assignment, ``del``,
  ``pop``, ``clear``) raises ``TypeError``.
- **Validation**: a positive ``total_capital`` paired with empty
  ``strategy_weights`` is a misconfiguration and is rejected.
"""

from __future__ import annotations

import math
from decimal import ROUND_DOWN, Decimal
from types import MappingProxyType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

#: One cent, expressed as a Decimal dollar amount.
_CENT = Decimal("0.01")
#: ``Decimal(1)`` reused for quantizing to whole cents.
_WHOLE = Decimal(1)


class CapitalAllocationError(ValueError):
    """Bad capital allocation input.

    Raised for empty weights with positive capital, negative or
    non-finite weights, non-finite / negative capital, or weights that
    sum to zero.
    """


def _to_decimal(value: float, *, name: str) -> Decimal:
    """Coerce a float input to ``Decimal`` via ``str()``.

    ``str(float)`` round-trips to the shortest decimal that reproduces
    the float, so we avoid binary artefacts (e.g. ``0.1 + 0.2``) while
    still rejecting non-finite values up front.

    ``bool`` is a subclass of ``int`` in Python, so it would otherwise
    sneak through the numeric check and be silently treated as ``1`` /
    ``0``. We reject it explicitly up front.
    """
    if isinstance(value, bool):
        raise CapitalAllocationError(
            f"{name} must be a real number (int or float), got bool {value!r}"
        )
    if not isinstance(value, (int, float)):
        raise CapitalAllocationError(f"{name} must be a real number, got {type(value).__name__}")
    if not math.isfinite(value):
        raise CapitalAllocationError(f"{name} must be finite, got {value!r}")
    return Decimal(str(value))


def allocate_capital(
    total_capital: float,
    strategy_weights: Mapping[str, float],
) -> MappingProxyType[str, Decimal]:
    """Allocate ``total_capital`` across strategies by ``strategy_weights``.

    Parameters
    ----------
    total_capital:
        Dollar amount to apportion. Floored to whole cents; must be
        finite and non-negative.
    strategy_weights:
        ``{strategy_id: weight}``. Weights must be finite and
        non-negative and must not all be zero. ``weight == 0`` for a
        listed strategy is allowed (it receives ``$0.00``).

    Returns
    -------
    MappingProxyType[str, Decimal]
        Frozen ``{strategy_id: amount}`` quantized to the cent. The
        amounts always sum to exactly ``total_capital`` (to the cent).
        The mapping is read-only: in-place mutation raises ``TypeError``.

    Raises
    ------
    CapitalAllocationError
        If ``strategy_weights`` is empty while ``total_capital > 0``;
        if any weight is negative, non-finite, or all weights sum to
        zero; or if ``total_capital`` is negative or non-finite.
    """
    # --- validate capital -------------------------------------------------
    capital_dec = _to_decimal(total_capital, name="total_capital")
    if capital_dec < 0:
        raise CapitalAllocationError(f"total_capital must be non-negative, got {total_capital!r}")

    # --- validate weights -------------------------------------------------
    if strategy_weights is None:  # pragma: no cover - defensive
        raise CapitalAllocationError("strategy_weights must not be None")

    # Empty weights + positive capital is the misconfiguration the
    # validator exists to catch. Empty weights + zero capital is a
    # well-formed no-op.
    if len(strategy_weights) == 0:
        if capital_dec > 0:
            raise CapitalAllocationError(
                "strategy_weights is empty but total_capital > 0; "
                "cannot apportion capital with no strategies"
            )
        return MappingProxyType({})

    weights_dec: dict[str, Decimal] = {}
    weight_sum = Decimal(0)
    for sid, w in strategy_weights.items():
        w_dec = _to_decimal(w, name=f"strategy_weights[{sid!r}]")
        if w_dec < 0:
            raise CapitalAllocationError(
                f"strategy_weights[{sid!r}] must be non-negative, got {w!r}"
            )
        weights_dec[sid] = w_dec
        weight_sum += w_dec

    if weight_sum == 0:
        raise CapitalAllocationError(
            "strategy_weights sum to zero; at least one positive weight "
            "is required to apportion capital"
        )

    # --- apportion in whole cents (Hamilton / largest-remainder) ----------
    # Work in integer cents to keep the remainder arithmetic exact.
    total_cents = int((capital_dec * 100).quantize(_WHOLE, rounding=ROUND_DOWN))

    floors: dict[str, int] = {}
    fracs: dict[str, Decimal] = {}
    assigned = 0
    for sid, w_dec in weights_dec.items():
        raw_cents = Decimal(total_cents) * w_dec / weight_sum
        floor_cents = int(raw_cents.quantize(_WHOLE, rounding=ROUND_DOWN))
        floors[sid] = floor_cents
        fracs[sid] = raw_cents - Decimal(floor_cents)
        assigned += floor_cents

    remainder = total_cents - assigned
    # Defensive: a correct floor-based apportionment always leaves a
    # non-negative remainder smaller than the number of strategies.
    if remainder < 0 or remainder > len(weights_dec):  # pragma: no cover
        raise CapitalAllocationError(
            f"internal apportionment remainder out of range ({remainder}); this is a bug"
        )

    # Distribute the leftover cents to the largest fractional parts.
    # Ties are broken by strategy id (ascending) so the result is
    # fully deterministic across runs and Python versions.
    ranking = sorted(weights_dec, key=lambda sid: (-fracs[sid], sid))
    cents = dict(floors)
    for sid in ranking[:remainder]:
        cents[sid] += 1

    allocation: dict[str, Decimal] = {
        sid: (Decimal(c) / 100).quantize(_CENT) for sid, c in cents.items()
    }
    return MappingProxyType(allocation)


#: Sum the (Decimal) values of an allocation, quantized to the cent.
def allocation_total(allocation: Mapping[str, Decimal]) -> Decimal:
    """Sum the Decimal values of an ``allocate_capital`` result."""
    return sum(allocation.values(), start=Decimal("0.00")).quantize(_CENT)


__all__ = [
    "CapitalAllocationError",
    "allocate_capital",
    "allocation_total",
]
