"""United States tax jurisdiction (gh#81).

Sources of truth:
- 26 U.S.C. § 1222 (long-term boundary: more than 1 year held).
- 26 U.S.C. § 1091 (wash-sale window: ±30 days around loss sale).
- IRS Pub. 550, "Investment Income and Expenses" (lot-selection
  defaults; default is FIFO when the broker / operator hasn't
  documented specific identification).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from engine.core.tax.jurisdictions.base import LotMethod


@dataclass(frozen=True)
class UnitedStates:
    """US federal tax-jurisdiction record.

    Duck-typed against the :class:`TaxJurisdiction` Protocol.
    Operator overrides for specific accounts (e.g. mutual-fund-only
    accounts that mandate average cost) live on the account, not on
    the jurisdiction.
    """

    code: str = "US"
    display_name: str = "United States"
    currency: str = "USD"
    long_term_days: int = 365
    wash_sale_window_days: int = 30
    default_lot_method: LotMethod = LotMethod.FIFO
    allowed_lot_methods: frozenset[LotMethod] = field(
        default_factory=lambda: frozenset(
            {
                LotMethod.FIFO,
                LotMethod.HIFO,
                LotMethod.SPECIFIC_ID,
                LotMethod.AVERAGE_COST,  # mutual-fund-only accounts
            }
        )
    )
