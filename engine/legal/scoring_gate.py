"""Legal compliance gate for strategy scoring surfaces.

This module is the *single source of truth* for suppressing or capping a
strategy's computed score before it reaches any external surface — the
marketplace search/listing, backtest result summary, or the scoring API's
``run_scoring`` / ``get_scoring_results`` responses.

Why a dedicated gate?
---------------------
Scores are derived from quantitative factors (see
:mod:`nexus_sdk.scoring` and :class:`engine.plugins.scoring_executor.ScoringExecutor`),
but *exposing* a score is also a legal/compliance act: a strategy that is
under review, withdrawn, or flagged for a data-licensing or regulatory reason
must not have its ranking surfaced to users even though the maths still
produces a number. Similarly, an operator may need to impose a hard compliance
ceiling below the technical 0-100 range.

Rather than scatter ad-hoc ``if strategy in BLOCKED:`` checks across every
route, this module centralises the rule as one small, well-tested validator
that every scoring surface composes.

Scope (intentionally narrow)
----------------------------
* Per-strategy flagging keyed on ``strategy_id``.
* Optional hard cap on the composite score.
* Graceful handling of missing / non-finite data (treated as suppressed).
* A :meth:`LegalScoreValidator.validate_result` helper that rebuilds a
  :class:`nexus_sdk.scoring.ScoringResult` with survivors re-ranked.

Out of scope: per-symbol flagging, time-windowed holds, per-user overrides,
and persistence of the flagged-strategy set (sourced from settings/env for
now; a future slice can swap in a DB-backed source behind the same class).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

from engine.config import settings
from nexus_sdk.scoring import ScoringResult, SymbolScore

if TYPE_CHECKING:
    from collections.abc import Iterable

logger = structlog.get_logger()


# Reasons attached to a :class:`ScoreValidationResult` so consumers (and audit
# logs) can distinguish *why* a score was altered. Kept as plain strings rather
# than an Enum so the value serialises cleanly into structured-log payloads and
# JSON error bodies without a custom encoder.
REASON_STRATEGY_FLAGGED: str = "strategy_flagged"
REASON_MISSING_DATA: str = "missing_data"
REASON_INVALID_SCORE: str = "invalid_score"
REASON_CAPPED: str = "capped_to_legal_max"


@dataclass(frozen=True)
class ScoreValidationResult:
    """Outcome of validating a single ``(strategy_id, score)`` pair.

    Attributes
    ----------
    strategy_id:
        The strategy the validated score belongs to (echoed for auditability).
    score:
        The score to surface. ``None`` when the score was suppressed (flagged
        strategy or missing/invalid data) — callers must treat ``None`` as
        "do not expose this score".
    suppressed:
        ``True`` when the score must not be exposed at all.
    capped:
        ``True`` when the score was clamped down to the legal ceiling (it is
        still exposed, just at the capped value).
    reason:
        Machine-readable reason code (one of the ``REASON_*`` constants) when
        the score was altered, else ``None`` for an unmodified pass-through.
    original_score:
        The unmodified input score, kept for audit logging. ``None`` when the
        input itself was missing.
    """

    strategy_id: str
    score: float | None
    suppressed: bool
    capped: bool
    reason: str | None
    original_score: float | None

    @property
    def passed(self) -> bool:
        """``True`` when the score is exposed unchanged (no suppression/cap)."""
        return not self.suppressed and not self.capped


def _parse_flagged_strategies(raw: str) -> frozenset[str]:
    """Parse a comma-separated flagged-strategy list into a normalised set.

    Whitespace is stripped and empties dropped so ``"a, b,,c"`` becomes
    ``{"a", "b", "c"}``. Non-string / falsy input yields an empty set rather
    than raising — configuration noise must never block the scoring pipeline.
    """
    if not raw or not isinstance(raw, str):
        return frozenset()
    return frozenset(token.strip() for token in raw.split(",") if token.strip())


class LegalScoreValidator:
    """Suppress or cap strategy scores against compliance rules.

    Construct with an explicit configuration, or use
    :meth:`from_settings` to read the operator-configured flagged set and
    ceiling from :mod:`engine.config`.

    The validator is stateless beyond its immutable configuration, so a single
    shared instance is safe to reuse across requests (see
    :func:`get_default_score_validator`).
    """

    def __init__(
        self,
        flagged_strategies: Iterable[str] | None = None,
        max_score: float = 100.0,
    ) -> None:
        self._flagged: frozenset[str] = frozenset(flagged_strategies or ())
        # Defensive clamp: a misconfigured ceiling above the technical max is
        # silently normalised down so the cap can never exceed what
        # :class:`nexus_sdk.scoring.SymbolScore` would accept anyway.
        self._max_score: float = min(float(max_score), 100.0)

    @property
    def flagged_strategies(self) -> frozenset[str]:
        """Read-only copy of the flagged-strategy set."""
        return self._flagged

    @property
    def max_score(self) -> float:
        """The legal ceiling applied to composite scores."""
        return self._max_score

    @classmethod
    def from_settings(cls) -> LegalScoreValidator:
        """Build a validator from the operator-configured settings.

        Reads ``legal_score_flagged_strategies`` (comma-separated ids) and
        ``legal_score_max_composite`` (ceiling). Failures to parse the ceiling
        fall back to the no-op default (100.0) so a bad env value can never
        take the scoring surface down — a warning is logged instead.
        """
        flagged = _parse_flagged_strategies(settings.legal_score_flagged_strategies)
        try:
            ceiling = float(settings.legal_score_max_composite)
        except (TypeError, ValueError):  # pragma: no cover - config is typed
            logger.warning(
                "legal.score_gate.invalid_max_composite",
                value=settings.legal_score_max_composite,
                fallback=100.0,
            )
            ceiling = 100.0
        return cls(flagged_strategies=flagged, max_score=ceiling)

    def is_flagged(self, strategy_id: str | None) -> bool:
        """Return ``True`` when ``strategy_id`` is on the compliance hold list."""
        return bool(strategy_id) and strategy_id in self._flagged

    def validate_score(
        self,
        strategy_id: str | None,
        score: float | None,
    ) -> ScoreValidationResult:
        """Validate a single ``(strategy_id, score)`` pair.

        Decision tree (first match wins):

        1. **Missing data** — ``score is None`` → suppressed, reason
           ``missing_data``. A score that was never computed must never be
           exposed as if it were a real (zero) value.
        2. **Invalid score** — non-finite (NaN/±inf) → suppressed, reason
           ``invalid_score``. Mirrors the ``math.isfinite`` guards already used
           in the backtest PnL path to stop numeric garbage leaking out.
        3. **Flagged strategy** — ``strategy_id`` on the hold list → suppressed,
           reason ``strategy_flagged``. The maths may be fine, but exposure is
           blocked for compliance reasons.
        4. **Cap** — score exceeds the legal ceiling → clamped down, ``capped``
           flag set, reason ``capped_to_legal_max``.
        5. **Pass-through** — otherwise the score is returned unchanged.

        ``strategy_id`` is intentionally *not* validated for emptiness here: a
        missing strategy id with a real score is a caller bug, but suppressing
        in that case would silently hide data. Instead the cap/pass-through
        path applies and the caller surfaces the score; flagging lookups
        simply miss on an empty id.
        """
        # 1. Missing data — never expose an absent score as a number.
        if score is None:
            return ScoreValidationResult(
                strategy_id=strategy_id or "",
                score=None,
                suppressed=True,
                capped=False,
                reason=REASON_MISSING_DATA,
                original_score=None,
            )

        # 2. Invalid (non-finite) score — treat as suppress to avoid emitting
        #    NaN/Infinity into JSON responses or downstream consumers.
        if not isinstance(score, (int, float)) or math.isinf(score) or math.isnan(score):
            return ScoreValidationResult(
                strategy_id=strategy_id or "",
                score=None,
                suppressed=True,
                capped=False,
                reason=REASON_INVALID_SCORE,
                original_score=None if not isinstance(score, (int, float)) else score,
            )

        # 3. Flagged strategy — block exposure regardless of the computed value.
        if self.is_flagged(strategy_id):
            logger.info(
                "legal.score_gate.suppressed",
                strategy_id=strategy_id,
                reason=REASON_STRATEGY_FLAGGED,
            )
            return ScoreValidationResult(
                strategy_id=strategy_id or "",
                score=None,
                suppressed=True,
                capped=False,
                reason=REASON_STRATEGY_FLAGGED,
                original_score=float(score),
            )

        original = float(score)

        # 4. Cap — clamp down to the legal ceiling.
        if original > self._max_score:
            logger.info(
                "legal.score_gate.capped",
                strategy_id=strategy_id,
                original=original,
                capped=self._max_score,
            )
            return ScoreValidationResult(
                strategy_id=strategy_id or "",
                score=self._max_score,
                suppressed=False,
                capped=True,
                reason=REASON_CAPPED,
                original_score=original,
            )

        # 5. Pass-through.
        return ScoreValidationResult(
            strategy_id=strategy_id or "",
            score=original,
            suppressed=False,
            capped=False,
            reason=None,
            original_score=original,
        )

    def validate_result(self, result: ScoringResult) -> ScoringResult:
        """Apply the gate to every score in a :class:`ScoringResult`.

        Returns a **new** ``ScoringResult`` containing only the survivors
        (suppressed entries are dropped entirely) with composite scores capped
        where necessary. Because ``ScoringResult.model_post_init`` re-sorts and
        re-ranks on construction, survivors are re-ranked relative to the
        surviving set — exactly the behaviour a gated surface should show.

        ``excluded_factors`` and ``strategy_id`` are preserved. A flagged
        ``strategy_id`` yields an empty survivor list (every score suppressed),
        which correctly renders as "no scores available for this strategy".
        """
        survivors: list[SymbolScore] = []
        suppressed_count = 0
        capped_count = 0
        for sym in result.scores:
            outcome = self.validate_score(result.strategy_id, sym.composite_score)
            if outcome.suppressed:
                suppressed_count += 1
                continue
            if outcome.capped:
                capped_count += 1
                # Build a fresh SymbolScore so the original object is not
                # mutated in place (callers may still hold a reference).
                survivors.append(
                    SymbolScore(
                        symbol=sym.symbol,
                        composite_score=outcome.score,
                        factor_scores=dict(sym.factor_scores),
                    )
                )
            else:
                survivors.append(sym)

        if suppressed_count or capped_count:
            logger.info(
                "legal.score_gate.result_filtered",
                strategy_id=result.strategy_id,
                input_count=len(result.scores),
                suppressed=suppressed_count,
                capped=capped_count,
                survivors=len(survivors),
            )

        return ScoringResult(
            strategy_id=result.strategy_id,
            scores=survivors,
            excluded_factors=list(result.excluded_factors),
        )


# --------------------------------------------------------------------------- #
# Process-wide default validator + module-level convenience function
# --------------------------------------------------------------------------- #
#
# Mirrors the singleton pattern used by the marketplace ratings/catalog stores.
# A dict holder (rather than a bare module global) lets
# :func:`set_default_score_validator` swap the instance in place so any holder
# of the previously-returned validator sees the change — which is what tests
# need to inject a controlled validator without touching settings/env vars.

_default_state: dict[str, LegalScoreValidator | None] = {"validator": None}


def get_default_score_validator() -> LegalScoreValidator:
    """Return the process-wide default :class:`LegalScoreValidator`.

    Lazily built from settings on first use and memoised so the comma-split +
    float parse runs once. Tests override it via
    :func:`set_default_score_validator`.
    """
    if _default_state["validator"] is None:
        _default_state["validator"] = LegalScoreValidator.from_settings()
    return _default_state["validator"]  # type: ignore[return-value]


def set_default_score_validator(validator: LegalScoreValidator | None) -> None:
    """Override (or clear, with ``None``) the default validator.

    Test affordance: inject a validator with a controlled flagged set / cap
    without mutating ``settings`` or environment variables. Reset to ``None``
    so the next :func:`get_default_score_validator` call rebuilds from settings.
    """
    _default_state["validator"] = validator


def reset_default_score_validator() -> None:
    """Drop the cached default validator (isolates tests from one another)."""
    _default_state["validator"] = None


def validate_score(
    strategy_id: str | None,
    score: float | None,
    *,
    validator: LegalScoreValidator | None = None,
) -> ScoreValidationResult:
    """Module-level convenience wrapper around the default validator.

    Uses :func:`get_default_score_validator` unless an explicit ``validator``
    is supplied (useful in tests or for request-scoped overrides). This is the
    function the task brief names as the integration entry point — a thin,
    side-effect-free delegate so call sites read as ``validate_score(...)``.
    """
    active = validator if validator is not None else get_default_score_validator()
    return active.validate_score(strategy_id, score)
