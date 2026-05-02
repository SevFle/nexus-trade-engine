"""United Kingdom tax jurisdiction — HMRC CGT (gh#81 follow-up).

Sources of truth:
- Taxation of Chargeable Gains Act 1992 (TCGA), s.104 (the "section
  104 holding" / pooled cost-basis rule).
- TCGA s.105 (the 30-day "bed & breakfasting" rule that disallows
  matching a sale with a *prior-30-day* purchase of the same
  security — analogous to but not identical to the US Section 1091
  wash-sale rule).
- HMRC HS284 ("Shares and Capital Gains Tax").

Notable differences from the US model
-------------------------------------
- **No long-term capital-gains distinction.** Capital gains in the
  UK are taxed at a single rate based on income band; there is no
  one-year holding-period boundary. We therefore set
  ``long_term_days = 0`` to mark the concept as not applicable —
  consumers should special-case zero rather than treating one day
  as the boundary.
- **30-day "bed & breakfasting" window.** The UK rule is symmetric
  with the US wash-sale window, so ``wash_sale_window_days = 30``.
- **Lot method = average cost** (the s.104 holding pool). FIFO and
  HIFO are *not* permitted for GB tax purposes.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from engine.core.tax.jurisdictions.base import LotMethod


@dataclass(frozen=True)
class UnitedKingdom:
    """UK CGT jurisdiction record.

    Duck-typed against :class:`engine.core.tax.jurisdictions.base.TaxJurisdiction`.
    """

    code: str = "GB"
    display_name: str = "United Kingdom"
    currency: str = "GBP"
    # No long-term boundary under HMRC CGT; flag as not-applicable.
    long_term_days: int = 0
    # 30-day "bed & breakfasting" rule.
    wash_sale_window_days: int = 30
    # HMRC requires the s.104 holding pool: average cost of all
    # currently-held shares of the same class.
    default_lot_method: LotMethod = LotMethod.AVERAGE_COST
    allowed_lot_methods: frozenset[LotMethod] = field(
        default_factory=lambda: frozenset({LotMethod.AVERAGE_COST})
    )
