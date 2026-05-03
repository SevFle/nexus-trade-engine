"""Tax-jurisdiction Protocol + supporting enums (gh#81)."""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol, runtime_checkable


class LotMethod(StrEnum):
    """Lot-selection method (which lot does a sale consume?).

    - FIFO: oldest lot first. US default.
    - LIFO: newest lot first. Permitted in some jurisdictions.
    - HIFO: highest-cost first. Permitted as "specific identification" in
      the US when the broker / operator can substantiate the choice.
    - AVERAGE_COST: weighted-average per security. Required for some
      mutual-fund-only accounts; permitted in many EU jurisdictions.
    - SPECIFIC_ID: caller picks the lot per disposition.
    """

    FIFO = "fifo"
    LIFO = "lifo"
    HIFO = "hifo"
    AVERAGE_COST = "average_cost"
    SPECIFIC_ID = "specific_id"


@runtime_checkable
class TaxJurisdiction(Protocol):
    """Contract every jurisdiction implements.

    The Protocol is intentionally narrow — it only carries the
    *configurable* knobs the rest of the engine reads. Computation
    lives in the modules that consume the jurisdiction
    (``wash_sale.py``, ``reports/form_1099b.py``, …) so the same
    jurisdiction record can be persisted, serialised, or swapped at
    runtime without re-shaping its API.
    """

    @property
    def code(self) -> str:
        """ISO 3166-1 alpha-2 code (e.g. ``"US"``, ``"GB"``, ``"DE"``).

        Sub-regions append a hyphen and the ISO 3166-2 sub-code
        (e.g. ``"US-CA"`` for California-specific overrides).
        """
        ...

    @property
    def display_name(self) -> str:
        """Human-readable name."""
        ...

    @property
    def currency(self) -> str:
        """ISO 4217 currency code (e.g. ``"USD"``, ``"EUR"``)."""
        ...

    @property
    def long_term_days(self) -> int:
        """Holding-period boundary for long-term treatment (days)."""
        ...

    @property
    def wash_sale_window_days(self) -> int:
        """Wash-sale window in days; ``0`` if the rule does not apply."""
        ...

    @property
    def default_lot_method(self) -> LotMethod:
        """Lot-selection method applied when the operator hasn't
        explicitly specified one for a given account."""
        ...

    @property
    def allowed_lot_methods(self) -> frozenset[LotMethod]:
        """Lot-selection methods this jurisdiction permits."""
        ...
