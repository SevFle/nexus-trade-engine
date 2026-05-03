"""German Verlustvortrag carry-forward (§ 20 Abs. 6 EStG).

Companion to :mod:`engine.core.tax.reports.kest`. Handles the
multi-year capital-loss carry-forward state German private investors
maintain under § 20 Abs. 6 EStG: net losses survive into future years
and offset future capital income, with the equity ring-fence
preserved across years.

Two buckets, both non-negative loss amounts:

- ``equity`` — losses that may *only* offset future equity gains
  (§ 20 Abs. 6 Satz 4 EStG).
- ``other`` — losses against any other capital income (interest,
  dividends, non-equity gains).

Algorithm
---------
1. Apply the prior carryover into the current-year per-bucket
   gain/loss totals (treating prior losses as a deduction against
   gains).
2. Net intra-year per the existing :func:`summarize_kest` rules: only
   positive equity contributes to the taxable base; the ``other``
   bucket contributes signed.
3. The combined base goes through allowance + KESt + SolZ + optional
   church tax via :func:`summarize_kest`.
4. Whatever remains as a *loss* on either bucket carries forward into
   the next year's :class:`KestCarryover`.

Out of scope (explicit follow-ups)
----------------------------------
- Mindestgewinnbesteuerung. Applies only to corporate income tax
  (§ 10d EStG / § 8 KStG), not § 32d EStG. Documented for posterity.
- Carryback. § 20 Abs. 6 forbids carryback for capital income; only
  forward carryover applies.
- Spousal Verlustverrechnung (joint filers may net losses across
  spouses) — modelled by the caller upstream.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from engine.core.tax.reports.kest import (
    SPARER_PAUSCHBETRAG_2024,
    AssetClass,
    KestDisposal,
    KestSummary,
    summarize_kest,
)

_TWOPLACES = Decimal("0.01")
_ZERO = Decimal("0.00")


@dataclass(frozen=True)
class KestCarryover:
    """Two-bucket carryover record. Both legs are non-negative loss
    amounts — gains do not carry over."""

    equity: Decimal = _ZERO
    other: Decimal = _ZERO

    @classmethod
    def zero(cls) -> KestCarryover:
        return cls(equity=_ZERO, other=_ZERO)

    @property
    def total(self) -> Decimal:
        return (self.equity + self.other).quantize(_TWOPLACES)


@dataclass(frozen=True)
class KestApplication:
    """Result of running :func:`apply_kest_carryover` for one tax year.

    ``summary`` is the standard :class:`KestSummary` produced after the
    prior carryover was applied. ``next_year_carryover`` is the
    carryover the taxpayer brings into the following year.
    """

    summary: KestSummary
    next_year_carryover: KestCarryover


def apply_kest_carryover(
    disposals: list[KestDisposal],
    prior: KestCarryover | None = None,
    *,
    allowance: Decimal = SPARER_PAUSCHBETRAG_2024,
    church_tax_rate: Decimal | None = None,
) -> KestApplication:
    """Apply ``prior`` losses to ``disposals``, then compute the year's
    KESt summary and the carryover that survives into next year.

    The prior carryover is applied at the per-bucket level *before*
    intra-year netting so that prior losses get a fresh shot at
    offsetting any new gains. The ring-fence rule still holds: prior
    equity losses can only reduce equity gains; prior ``other`` losses
    can only reduce the ``other`` bucket.
    """
    prior = prior or KestCarryover.zero()
    if prior.equity < 0 or prior.other < 0:
        raise ValueError("prior carryover legs must be non-negative loss amounts")

    # Sum current-year per-bucket gain/loss without going through the
    # full summariser yet — we need the per-bucket numbers to apply
    # the prior carryover surgically.
    eq = _ZERO
    other = _ZERO
    for d in disposals:
        delta = d.gain_loss
        if d.asset_class == AssetClass.EQUITY:
            eq += delta
        else:
            other += delta

    eq_after_prior = (eq - prior.equity).quantize(_TWOPLACES)
    other_after_prior = (other - prior.other).quantize(_TWOPLACES)

    # Build a synthetic per-bucket disposal list so summarize_kest can
    # still own the allowance + tax computations.
    summary = summarize_kest(
        _synthesize(eq_after_prior, other_after_prior),
        allowance=allowance,
        church_tax_rate=church_tax_rate,
    )

    next_carry = KestCarryover(
        equity=(-eq_after_prior).quantize(_TWOPLACES) if eq_after_prior < 0 else _ZERO,
        other=(-other_after_prior).quantize(_TWOPLACES) if other_after_prior < 0 else _ZERO,
    )

    return KestApplication(
        summary=_with_disposal_count(summary, len(disposals)),
        next_year_carryover=next_carry,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _synthesize(equity_net: Decimal, other_net: Decimal) -> list[KestDisposal]:
    """Build the smallest set of synthetic disposals that reproduce the
    given per-bucket nets. Used so :func:`summarize_kest` can still own
    the allowance + tax computations."""
    out: list[KestDisposal] = []
    if equity_net != 0:
        proceeds = equity_net if equity_net > 0 else _ZERO
        cost = -equity_net if equity_net < 0 else _ZERO
        out.append(
            KestDisposal(
                description="(carry-adjusted equity bucket)",
                acquired=date(2024, 1, 1),
                disposed=date(2024, 12, 31),
                proceeds=proceeds,
                cost=cost,
                asset_class=AssetClass.EQUITY,
            )
        )
    if other_net != 0:
        proceeds = other_net if other_net > 0 else _ZERO
        cost = -other_net if other_net < 0 else _ZERO
        out.append(
            KestDisposal(
                description="(carry-adjusted other bucket)",
                acquired=date(2024, 1, 1),
                disposed=date(2024, 12, 31),
                proceeds=proceeds,
                cost=cost,
                asset_class=AssetClass.OTHER,
            )
        )
    return out


def _with_disposal_count(summary: KestSummary, count: int) -> KestSummary:
    """Replace the synthesised disposal count with the real one. The
    summary is otherwise identical."""
    return KestSummary(
        disposal_count=count,
        proceeds_total=summary.proceeds_total,
        cost_total=summary.cost_total,
        equity_net=summary.equity_net,
        other_net=summary.other_net,
        taxable_income=summary.taxable_income,
        allowance_used=summary.allowance_used,
        kest=summary.kest,
        solidarity_surcharge=summary.solidarity_surcharge,
        church_tax=summary.church_tax,
        total_tax=summary.total_tax,
    )


__all__ = [
    "KestApplication",
    "KestCarryover",
    "apply_kest_carryover",
]
