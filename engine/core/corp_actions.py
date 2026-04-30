"""Corporate-action back-adjustment.

Given a chronological log of corporate actions per symbol, this module
adjusts a historical bar's price (or volume) so that an analysis run at
``as_of`` sees a continuous, comparable series across splits and cash
dividends.

Conventions:

- Splits use ``ratio`` interpreted as new-shares-per-old. A 2-for-1 has
  ``ratio=2.0``; a 1-for-2 reverse split has ``ratio=0.5``. Pre-split
  prices are divided by ``ratio``; pre-split volumes are multiplied
  by ``ratio``.
- Cash dividends use ``cash_amount`` per share. Pre-ex-date prices are
  multiplied by ``(close_pre - cash_amount) / close_pre`` (CRSP-style
  total-return adjustment). The caller passes the ex-date raw close as
  ``ex_date_close``; without it the dividend factor is skipped.
- Only actions with ``effective_date <= as_of`` are applied. Actions
  newer than ``as_of`` are ignored, so an as-of analysis never sees a
  future split.
- Actions with ``effective_date > bar_date`` *and* ``effective_date <=
  as_of`` are applied — the bar pre-dates the action and so must be
  adjusted into post-action share units.

This is PR1: splits + cash dividends. Spinoffs, mergers, and symbol
changes are accepted by the dataclass but their adjustment math lands
in follow-up PRs.
"""

from __future__ import annotations

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
            if self.ratio <= 0:
                msg = f"split ratio must be positive; got {self.ratio}"
                raise CorporateActionError(msg)
        if self.action_type == "cash_dividend":
            if self.cash_amount is None:
                msg = "cash_dividend requires a cash_amount"
                raise CorporateActionError(msg)
            if self.cash_amount < 0:
                msg = (
                    "cash_dividend cash_amount must be non-negative; "
                    f"got {self.cash_amount}"
                )
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
    dividends are skipped (only splits are applied).
    """
    actions = _applicable(log.actions_for(symbol), bar_date, as_of)
    factor = 1.0
    for a in actions:
        if a.action_type == "split":
            assert a.ratio is not None
            factor /= a.ratio
        elif a.action_type == "cash_dividend":
            if ex_date_close is None or ex_date_close <= 0:
                continue
            assert a.cash_amount is not None
            div_factor = (ex_date_close - a.cash_amount) / ex_date_close
            if div_factor <= 0:
                continue
            factor *= div_factor
    return raw_price * factor


def adjust_volume(
    log: CorporateActionLog,
    *,
    symbol: str,
    bar_date: date,
    raw_volume: int,
    as_of: date,
) -> int:
    """Return ``raw_volume`` adjusted backward through splits.

    Cash dividends do not change share count, so volume adjustment only
    consumes the split actions in the window.
    """
    actions = _applicable(log.actions_for(symbol), bar_date, as_of)
    multiplier = 1.0
    for a in actions:
        if a.action_type == "split":
            assert a.ratio is not None
            multiplier *= a.ratio
    return round(raw_volume * multiplier)


__all__ = [
    "ActionType",
    "CorporateAction",
    "CorporateActionError",
    "CorporateActionLog",
    "adjust_price",
    "adjust_volume",
]
