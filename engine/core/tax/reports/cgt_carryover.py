"""HMRC CGT loss carry-forward.

Companion to :mod:`engine.core.tax.reports.hmrc_cgt`. Allowable
capital losses that exceed in-year gains carry forward indefinitely
(no time limit on use, but the loss itself must be *claimed* within
four years of the end of the tax year in which it arose — that
claim-window state lives outside this module).

HMRC's order of operations
--------------------------
1. Net same-year gains and losses inside :func:`summarize_cgt`.
2. Apply the Annual Exempt Amount (AEA) to the in-year *net gain*.
3. Apply any prior-year carry-forward loss to the *taxable gain after
   the AEA*. Carry-forward losses cannot reduce a year's gains below
   the AEA — that allowance survives in full when there is enough
   gain to absorb it.
4. Anything left of the prior loss after step 3 carries forward into
   the next tax year. Same-year net losses also push into the
   carryover so the taxpayer can use them later.

What's NOT here (explicit follow-ups)
-------------------------------------
- The four-year *claim* window for in-year losses. HMRC requires the
  loss to be reported on a Self Assessment return within four years
  of the tax-year end; tracking that deadline is a separate workflow
  on the operator side.
- Clogged losses (TCGA 1992 s.18 — connected-party transactions),
  pre-1996 losses, and other ring-fenced loss types.
- Allowable losses on residential property (different rate but same
  carry-forward mechanics — caller can model them separately).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from engine.core.tax.reports.hmrc_cgt import (
    ANNUAL_EXEMPT_AMOUNT_2024_25,
    CgtDisposal,
    CgtSummary,
    summarize_cgt,
)

_TWOPLACES = Decimal("0.01")
_ZERO = Decimal("0.00")


@dataclass(frozen=True)
class CgtCarryover:
    """Single-bucket loss carryover (GBP). UK CGT does not ring-fence
    losses by asset class for individuals."""

    loss: Decimal = _ZERO

    @classmethod
    def zero(cls) -> CgtCarryover:
        return cls(loss=_ZERO)


@dataclass(frozen=True)
class CgtApplication:
    """Result of running :func:`apply_cgt_carryover` for one tax year.

    - ``summary`` — the in-year :class:`CgtSummary` produced by
      :func:`summarize_cgt`. Carry-forward absorption is reflected in
      ``carryover_loss_used`` and ``taxable_gain_after_carryover``.
    - ``carryover_loss_used`` — amount of prior loss consumed by this
      year's taxable gain (zero if the year's taxable gain was zero).
    - ``taxable_gain_after_carryover`` — taxable gain net of both AEA
      and the prior carryover.
    - ``next_year_carryover`` — what carries to the following year.
    """

    summary: CgtSummary
    carryover_loss_used: Decimal
    taxable_gain_after_carryover: Decimal
    next_year_carryover: CgtCarryover


def apply_cgt_carryover(
    disposals: list[CgtDisposal],
    prior: CgtCarryover | None = None,
    *,
    annual_exempt_amount: Decimal = ANNUAL_EXEMPT_AMOUNT_2024_25,
) -> CgtApplication:
    """Apply the prior-year loss to ``disposals``'s net gain *after*
    the AEA, then surface the remaining carryover.

    Same-year net losses push into ``next_year_carryover`` on top of
    whatever prior loss survives the year's taxable gain.
    """
    if annual_exempt_amount < 0:
        raise ValueError("annual_exempt_amount must be non-negative")
    prior = prior or CgtCarryover.zero()
    if prior.loss < 0:
        raise ValueError("prior carryover loss must be non-negative")

    summary = summarize_cgt(
        disposals, annual_exempt_amount=annual_exempt_amount
    )

    # Step 3: prior loss applied AFTER the AEA. ``taxable_gain`` is
    # already post-AEA per summarize_cgt.
    used = min(prior.loss, summary.taxable_gain).quantize(_TWOPLACES)
    remaining_prior = (prior.loss - used).quantize(_TWOPLACES)
    taxable_after = (summary.taxable_gain - used).quantize(_TWOPLACES)

    # Same-year net loss compounds with whatever prior loss survived.
    next_loss = (remaining_prior + summary.net_loss).quantize(_TWOPLACES)

    return CgtApplication(
        summary=summary,
        carryover_loss_used=used,
        taxable_gain_after_carryover=taxable_after,
        next_year_carryover=CgtCarryover(loss=next_loss),
    )


def roll_forward(prior: CgtCarryover) -> CgtCarryover:
    """Return the prior carryover unchanged (zero current activity).

    Convenience for audit workflows that need to roll a prior loss
    forward without computing a year's worth of disposals.
    """
    if prior.loss < 0:
        raise ValueError("prior carryover loss must be non-negative")
    return CgtCarryover(loss=prior.loss.quantize(_TWOPLACES))


__all__ = [
    "CgtApplication",
    "CgtCarryover",
    "apply_cgt_carryover",
    "roll_forward",
]
