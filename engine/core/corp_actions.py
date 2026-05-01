"""Corporate-action back-adjustment.

Given a chronological log of corporate actions per symbol, this module
adjusts a historical bar's price (or volume) so that an analysis run at
``as_of`` sees a continuous, comparable series across splits, cash
dividends, spinoffs, and stock mergers.

Conventions (PR1):

- **Splits** use ``ratio`` interpreted as new-shares-per-old. A 2-for-1 has
  ``ratio=2.0``; a 1-for-2 reverse split has ``ratio=0.5``. Pre-split
  prices are divided by ``ratio``; pre-split volumes are multiplied
  by ``ratio``.
- **Cash dividends** use ``cash_amount`` per share. Pre-ex-date prices are
  multiplied by ``(close_pre - cash_amount) / close_pre`` (CRSP-style
  total-return adjustment). The caller passes the ex-date raw close as
  ``ex_date_close``; without it the dividend factor is skipped.

Conventions (PR2):

- **Spinoffs** use ``ratio`` interpreted as the parent's *retained
  fraction* of pre-spinoff value (0 < ratio <= 1). Example: parent was
  $100 pre-spinoff, spinco worth $20, parent post = $80 -> ``ratio=0.80``.
  Pre-spinoff prices are multiplied by ``ratio``. Volume is unchanged
  (the parent's share count does not change in a spinoff -- the spinco
  shares are issued new).
- **Stock mergers** use ``ratio`` interpreted as new-shares-per-old (same
  semantics as a split) plus ``new_symbol`` to record the surviving
  ticker. Pre-merger bars under the acquired symbol are adjusted using
  the same math as a split. Cash-only mergers are accepted at
  construction time (``cash_amount`` set instead of ``ratio``) but
  produce no price/volume adjustment -- the symbol terminates on the
  effective date and downstream code is expected to stop emitting bars
  for it.
- **Symbol changes** record a rename (``new_symbol`` required). They do
  not change price or volume -- they are kept in the log so downstream
  code can stitch the old + new symbol histories together.

As-of window for every action type:

- Only actions with ``effective_date <= as_of`` are applied. Actions
  newer than ``as_of`` are ignored, so an as-of analysis never sees a
  future split.
- Actions with ``effective_date > bar_date`` *and* ``effective_date <=
  as_of`` are applied -- the bar pre-dates the action and so must be
  adjusted into post-action share units.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date  # noqa: TC003 - runtime use as dataclass field type
from typing import Literal

ActionType = Literal[
    "split",
    "cash_dividend",
    "spinoff",
    "merger",
    "symbol_change",
]
_VALID_ACTION_TYPES: frozenset[str] = frozenset(
    ["split", "cash_dividend", "spinoff", "merger", "symbol_change"]
)


class CorporateActionError(Exception):
    """Raised on malformed corporate-action input."""


def _require_finite(value: float, label: str) -> None:
    """Reject NaN / inf early. NaN comparisons all return False, which
    would silently bypass the ``ratio > 0`` / ``ratio <= 1`` guards and
    produce a NaN-contaminated price downstream."""
    if not math.isfinite(value):
        raise CorporateActionError(f"{label} must be finite, got {value!r}")


@dataclass(frozen=True)
class CorporateAction:
    """One corporate action against one symbol on one effective date."""

    action_type: ActionType
    symbol: str
    effective_date: date
    ratio: float | None = None
    cash_amount: float | None = None
    new_symbol: str | None = None

    def __post_init__(self) -> None:
        if self.action_type not in _VALID_ACTION_TYPES:
            msg = (
                f"unknown action_type {self.action_type!r}; "
                f"expected one of {sorted(_VALID_ACTION_TYPES)}"
            )
            raise CorporateActionError(msg)
        if not self.symbol.strip():
            msg = "symbol must be non-empty"
            raise CorporateActionError(msg)

        if self.action_type == "split":
            if self.ratio is None:
                msg = "split requires a ratio"
                raise CorporateActionError(msg)
            _require_finite(self.ratio, "split ratio")
            if self.ratio <= 0:
                msg = f"split ratio must be positive; got {self.ratio}"
                raise CorporateActionError(msg)

        elif self.action_type == "cash_dividend":
            if self.cash_amount is None:
                msg = "cash_dividend requires a cash_amount"
                raise CorporateActionError(msg)
            _require_finite(self.cash_amount, "cash_dividend cash_amount")
            if self.cash_amount < 0:
                msg = (
                    "cash_dividend cash_amount must be non-negative; "
                    f"got {self.cash_amount}"
                )
                raise CorporateActionError(msg)

        elif self.action_type == "spinoff":
            if self.ratio is None:
                msg = (
                    "spinoff requires a ratio (parent's retained fraction "
                    "of pre-spinoff value, in (0, 1])"
                )
                raise CorporateActionError(msg)
            _require_finite(self.ratio, "spinoff ratio")
            if not (0.0 < self.ratio <= 1.0):
                msg = f"spinoff ratio must be in (0, 1]; got {self.ratio}"
                raise CorporateActionError(msg)

        elif self.action_type == "merger":
            # Two flavors: stock-for-stock (ratio + new_symbol) or
            # cash-only (cash_amount). PR2 implements stock mergers;
            # cash-only is accepted at construction but adjust_price /
            # adjust_volume treat it as a terminating no-op.
            stock = self.ratio is not None
            cash = self.cash_amount is not None
            if not stock and not cash:
                msg = (
                    "merger requires either a ratio (stock merger) "
                    "or a cash_amount (cash merger)"
                )
                raise CorporateActionError(msg)
            if stock and cash:
                msg = (
                    "merger cannot specify both ratio and cash_amount; "
                    "stock and cash mergers are distinct events"
                )
                raise CorporateActionError(msg)
            if stock:
                assert self.ratio is not None  # narrowed by `stock`
                _require_finite(self.ratio, "merger ratio")
                if self.ratio <= 0:
                    msg = f"merger ratio must be positive; got {self.ratio}"
                    raise CorporateActionError(msg)
                if not self.new_symbol or not self.new_symbol.strip():
                    msg = "stock merger requires new_symbol (surviving ticker)"
                    raise CorporateActionError(msg)
            if cash:
                assert self.cash_amount is not None  # narrowed by `cash`
                _require_finite(self.cash_amount, "merger cash_amount")
                if self.cash_amount < 0:
                    msg = (
                        "merger cash_amount must be non-negative; "
                        f"got {self.cash_amount}"
                    )
                    raise CorporateActionError(msg)

        elif self.action_type == "symbol_change":
            if not self.new_symbol or not self.new_symbol.strip():
                msg = "symbol_change requires new_symbol"
                raise CorporateActionError(msg)


class CorporateActionLog:
    """Append-only chronological log of corporate actions across symbols."""

    def __init__(self, actions: list[CorporateAction]) -> None:
        self._actions: list[CorporateAction] = list(actions)

    def append(self, action: CorporateAction) -> None:
        self._actions.append(action)

    def actions_for(self, symbol: str) -> list[CorporateAction]:
        """All actions for ``symbol`` sorted by effective_date ascending."""
        out = [a for a in self._actions if a.symbol == symbol]
        out.sort(key=lambda a: a.effective_date)
        return out


def _applicable(
    actions: list[CorporateAction], bar_date: date, as_of: date
) -> list[CorporateAction]:
    """Actions that adjust ``bar_date`` given ``as_of`` cutoff.

    Action applies iff ``bar_date < effective_date <= as_of``.
    """
    return [a for a in actions if bar_date < a.effective_date <= as_of]


def adjust_price(
    log: CorporateActionLog,
    *,
    symbol: str,
    bar_date: date,
    raw_price: float,
    as_of: date,
    ex_date_close: float | None = None,
) -> float:
    """Return ``raw_price`` adjusted backward through corporate actions.

    ``ex_date_close`` is the raw close of the ex-dividend bar; required
    to compute the dividend back-adjustment factor. When omitted, cash
    dividends are skipped (only structural adjustments are applied).
    """
    # Explicit `is None` guards rather than `assert` — `python -O` strips
    # asserts, so guarding production loops with them is a latent crash.
    # Construction-time validation already enforces presence of these
    # fields, but defense in depth is cheap here.
    actions = _applicable(log.actions_for(symbol), bar_date, as_of)
    factor = 1.0
    for a in actions:
        if a.action_type == "split":
            if a.ratio is None:
                continue
            factor /= a.ratio
        elif a.action_type == "cash_dividend":
            if ex_date_close is None or ex_date_close <= 0:
                continue
            if a.cash_amount is None:
                continue
            div_factor = (ex_date_close - a.cash_amount) / ex_date_close
            if div_factor <= 0:
                continue
            factor *= div_factor
        elif a.action_type == "spinoff":
            # Parent retained `ratio` fraction of pre-spinoff value; pre-
            # spinoff prices must be scaled DOWN by that fraction to
            # match the post-spinoff parent series.
            if a.ratio is None:
                continue
            factor *= a.ratio
        elif a.action_type == "merger":
            # Stock merger is structurally identical to a split for
            # purposes of back-adjustment: each old share now represents
            # `ratio` new shares of the surviving symbol. Cash merger
            # terminates the symbol -- bars after effective_date should
            # not exist for this symbol; we still don't apply a factor.
            if a.ratio is not None:
                factor /= a.ratio
            # else: cash merger; no per-share-price adjustment.
        # symbol_change is a pure rename -- no price math.
    return raw_price * factor


def adjust_volume(
    log: CorporateActionLog,
    *,
    symbol: str,
    bar_date: date,
    raw_volume: int,
    as_of: date,
) -> int:
    """Return ``raw_volume`` adjusted backward through structural actions.

    Splits and stock mergers reshare the float; spinoffs do not (parent
    share count is unchanged -- only spinco shares are newly issued).
    Cash dividends, cash mergers, and symbol changes do not adjust
    volume.
    """
    actions = _applicable(log.actions_for(symbol), bar_date, as_of)
    multiplier = 1.0
    for a in actions:
        if a.action_type == "split":
            if a.ratio is None:
                continue
            multiplier *= a.ratio
        elif a.action_type == "merger":
            if a.ratio is not None:
                multiplier *= a.ratio
            # else: cash merger; volume unchanged.
        # spinoff / cash_dividend / symbol_change leave parent volume alone.
    return round(raw_volume * multiplier)


__all__ = [
    "ActionType",
    "CorporateAction",
    "CorporateActionError",
    "CorporateActionLog",
    "adjust_price",
    "adjust_volume",
]
