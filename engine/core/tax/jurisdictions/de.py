"""Germany tax jurisdiction — KESt / Abgeltungsteuer (gh#81 follow-up).

Sources of truth:
- EStG § 20 (income from capital assets) — defines the categories
  taxed under the flat-rate Abgeltungsteuer regime.
- EStG § 32d — flat 25% withholding rate (plus 5.5% solidarity
  surcharge, plus 8/9% church tax where applicable). The engine
  records the *base* rate; surcharge handling is operator-deployed.
- BMF letter 18.01.2016 (income from capital assets) — confirms
  FIFO as the mandatory lot-selection method for shares held in a
  domestic securities account.

Notable differences from the US and UK models
---------------------------------------------
- **No long-term / short-term distinction.** The Abgeltungsteuer
  reform (2009) abolished the prior Spekulationsfrist for newly-
  acquired shares. ``long_term_days = 0`` flags the concept as
  not applicable.
- **No wash-sale rule for shares.** Germany has no equivalent of
  the US Section 1091 rule for cash-equity sales (a separate rule
  applies to certain derivatives — out of scope for this slice).
  ``wash_sale_window_days = 0``.
- **FIFO is mandatory.** §20 EStG and the BMF letter both require
  FIFO. AVERAGE_COST / HIFO / SPECIFIC_ID are not permitted.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from engine.core.tax.jurisdictions.base import LotMethod


@dataclass(frozen=True)
class Germany:
    """DE / KESt jurisdiction record.

    Duck-typed against :class:`engine.core.tax.jurisdictions.base.TaxJurisdiction`.
    """

    code: str = "DE"
    display_name: str = "Germany"
    currency: str = "EUR"
    # Abgeltungsteuer reform abolished the holding-period boundary
    # for newly-acquired shares (2009). Flag as not-applicable.
    long_term_days: int = 0
    # No wash-sale rule for cash-equity sales under German tax law.
    wash_sale_window_days: int = 0
    # §20 EStG + BMF letter 18.01.2016 mandate FIFO.
    default_lot_method: LotMethod = LotMethod.FIFO
    allowed_lot_methods: frozenset[LotMethod] = field(
        default_factory=lambda: frozenset({LotMethod.FIFO})
    )
