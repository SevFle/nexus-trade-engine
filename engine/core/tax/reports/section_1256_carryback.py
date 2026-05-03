"""IRC § 1212(c) Section 1256 3-year carryback (gh#155 follow-up).

Companion to :mod:`engine.core.tax.reports.form_6781`. A non-
corporate taxpayer with a net Section 1256 loss may *elect* to
carry it back three taxable years, applying oldest-year-first
against prior § 1256 *net gains* only. Anything left after the
3-year carryback survives forward as an ordinary § 1212(b)
capital-loss carryforward.

The carryback retains its 60/40 character: 60 % long-term and 40 %
short-term, mirroring the § 1256(a)(3) split. This module surfaces
the per-year absorption + the residual that flows forward.

What's NOT here (explicit follow-ups)
-------------------------------------
- The actual *election*. § 1212(c)(1) requires the taxpayer to
  affirmatively elect the carryback on Form 6781. Operators decide
  whether to call this function for their workflow.
- Net § 1256 gain limit per year. The function caps the carryback
  to the prior year's net gain — but that gain figure must be
  supplied by the caller (typically loaded from a prior-year
  Form6781Summary).
- Interaction with Form 1045 (application for tentative refund).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

_TWOPLACES = Decimal("0.01")
_ZERO = Decimal("0.00")

CARRYBACK_YEARS: int = 3


@dataclass(frozen=True)
class PriorYearNetGain:
    """Net Section 1256 gain for one prior tax year. ``net_gain`` is
    non-negative — losses do not reduce a prior year's carryback
    capacity."""

    year: int
    net_gain: Decimal

    def __post_init__(self) -> None:
        if self.net_gain < 0:
            raise ValueError("prior-year net_gain must be non-negative")


@dataclass(frozen=True)
class CarrybackAbsorption:
    """One prior-year absorption record. Surfaced for audit so the
    caller can show "we recovered $X against the 2021 1256 gain"."""

    year: int
    amount: Decimal


@dataclass(frozen=True)
class Section1256Carryback:
    """Result of running :func:`apply_section_1256_carryback`.

    - ``loss_absorbed`` — total carryback applied across the up-to-3
      prior years. Always non-negative.
    - ``per_year`` — oldest-first list of (year, amount) absorptions.
    - ``forward_carry`` — net § 1256 loss that survived the carryback
      and now flows into the standard § 1212(b) capital-loss carry-
      forward via ``carryover.py``. Non-negative absolute amount; the
      caller signs it back as a loss when feeding the forward
      carryover.
    """

    loss_absorbed: Decimal
    per_year: tuple[CarrybackAbsorption, ...]
    forward_carry: Decimal


def apply_section_1256_carryback(
    net_loss: Decimal,
    prior_years: list[PriorYearNetGain],
) -> Section1256Carryback:
    """Apply a net Section 1256 loss against the oldest-first prior
    years, capped at the 3-year window.

    ``net_loss`` is a positive amount representing the year's
    aggregated 1256 loss (i.e. ``-Form6781Summary.net_gain_loss``
    when that quantity is negative). Pass the absolute value.

    ``prior_years`` is the caller's record of net 1256 *gains* in the
    three preceding years. Years older than 3 are silently dropped
    (the function takes the most-recent 3 by year).
    """
    if net_loss < 0:
        raise ValueError(
            "net_loss must be a non-negative absolute amount; "
            "pass abs(loss) not the signed value"
        )
    if net_loss == 0:
        return Section1256Carryback(
            loss_absorbed=_ZERO,
            per_year=(),
            forward_carry=_ZERO,
        )

    sorted_years = sorted(prior_years, key=lambda p: p.year)
    # Keep only the most-recent 3 — § 1212(c)(1) caps at 3 years.
    eligible = sorted_years[-CARRYBACK_YEARS:] if sorted_years else []

    remaining = net_loss
    absorptions: list[CarrybackAbsorption] = []
    for prior in eligible:
        if remaining <= 0:
            break
        take = min(remaining, prior.net_gain).quantize(_TWOPLACES)
        if take > 0:
            absorptions.append(
                CarrybackAbsorption(year=prior.year, amount=take)
            )
            remaining = (remaining - take).quantize(_TWOPLACES)

    forward = remaining.quantize(_TWOPLACES) if remaining > 0 else _ZERO
    absorbed = (net_loss - remaining).quantize(_TWOPLACES)

    return Section1256Carryback(
        loss_absorbed=absorbed,
        per_year=tuple(absorptions),
        forward_carry=forward,
    )


__all__ = [
    "CARRYBACK_YEARS",
    "CarrybackAbsorption",
    "PriorYearNetGain",
    "Section1256Carryback",
    "apply_section_1256_carryback",
]
