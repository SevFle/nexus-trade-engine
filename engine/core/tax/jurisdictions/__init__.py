"""Configurable tax jurisdictions (gh#81).

A *jurisdiction* is the set of rules a country (or a sub-region of
one) applies to capital-gains accounting. Each jurisdiction declares:

- Lot-selection method (FIFO, LIFO, average cost, …).
- Long-term holding-period boundary (in days).
- Wash-sale window (in days, or 0 if the rule does not apply).
- Currency.

The :class:`TaxJurisdiction` Protocol pins the contract; the
registry lets operators register their own implementations under a
stable string key and look one up at runtime via configuration.

Out of scope (explicit follow-ups):
- Per-jurisdiction report generators (1099-B already lives in
  :mod:`engine.core.tax.reports`; MiFID II / HMRC CGT / KESt are
  tracked under gh#155).
- Per-jurisdiction wash-sale variants. Today the detector at
  :mod:`engine.core.tax.wash_sale` is US-shaped; non-US
  jurisdictions can ignore it by setting wash_sale_window_days = 0.
- Mark-to-market regimes (Section 1256, KESt §32d).
- Income-vs-capital classification heuristics.
"""

from engine.core.tax.jurisdictions.base import LotMethod, TaxJurisdiction
from engine.core.tax.jurisdictions.registry import (
    get_jurisdiction,
    list_jurisdictions,
    register_jurisdiction,
)
from engine.core.tax.jurisdictions.gb import UnitedKingdom
from engine.core.tax.jurisdictions.us import UnitedStates

__all__ = [
    "LotMethod",
    "TaxJurisdiction",
    "UnitedKingdom",
    "UnitedStates",
    "get_jurisdiction",
    "list_jurisdictions",
    "register_jurisdiction",
]
