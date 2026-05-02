"""France tax jurisdiction — PFU / Prélèvement Forfaitaire Unique (gh#81 follow-up).

Sources of truth:
- Code général des impôts (CGI) Article 200 A — flat-tax regime
  ("Prélèvement Forfaitaire Unique") introduced by the 2018 finance
  law. 30 % combined rate (12.8 % income tax + 17.2 % social
  charges). Surcharges are operator-deployed; the engine records
  the *base* concept.
- CGI Article 150-0 D — FIFO ("première entrée, première sortie") is
  mandatory for securities of the same kind held in a single account.
- BOI-RPPM-PVBMI-20-10-30 — administrative doctrine confirming the
  FIFO mandate and the abolition of the prior abattement-pour-durée-
  de-détention with the PFU.

Notable
-------
- **No long-term distinction** under the default PFU regime (the
  prior abattement-pour-durée-de-détention applies only to taxpayers
  who explicitly opt for the barème progressif on pre-2018 lots —
  out of scope here). ``long_term_days = 0``.
- **No wash-sale rule** for cash-equity sales under French tax law.
  ``wash_sale_window_days = 0``.
- **FIFO mandatory.** AVERAGE_COST / HIFO / LIFO / SPECIFIC_ID are
  not permitted.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from engine.core.tax.jurisdictions.base import LotMethod


@dataclass(frozen=True)
class France:
    """FR / PFU jurisdiction record.

    Duck-typed against :class:`engine.core.tax.jurisdictions.base.TaxJurisdiction`.
    """

    code: str = "FR"
    display_name: str = "France"
    currency: str = "EUR"
    # PFU: no long-term boundary applies under the default regime.
    long_term_days: int = 0
    # No wash-sale rule for cash-equity sales under French tax law.
    wash_sale_window_days: int = 0
    # CGI Article 150-0 D mandates FIFO.
    default_lot_method: LotMethod = LotMethod.FIFO
    allowed_lot_methods: frozenset[LotMethod] = field(
        default_factory=lambda: frozenset({LotMethod.FIFO})
    )
