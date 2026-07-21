"""Focused tests for the legal scoring-compliance gate.

Covers :mod:`engine.legal.scoring_gate` (the validator + ``validate_score``
function) and its single integration point: the scoring API route module
(:mod:`engine.api.routes.scoring`).

Scope mirrors the implementation slice:

* :class:`LegalScoreValidator` — happy path, flagged strategy suppression,
  missing-data suppression, invalid-score (NaN/±inf) suppression, and the
  legal cap.
* :meth:`LegalScoreValidator.validate_result` — survivor filtering + re-rank,
  and the "flagged strategy ⇒ empty survivors" contract.
* Module-level ``validate_score`` convenience function + default-validator
  caching/override affordances.
* Settings parsing (``_parse_flagged_strategies``) and ``from_settings``.
* Route integration (read path): a stored snapshot's scores are gated at
  exposure time via the ``get_score_validator`` dependency override.
"""

from __future__ import annotations

import math

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from engine.api.routes.scoring import _gate_score_dicts, get_score_validator
from engine.app import create_app
from engine.config import settings
from engine.db.models import ScoringSnapshot
from engine.deps import get_db
from engine.legal.scoring_gate import (
    REASON_CAPPED,
    REASON_INVALID_SCORE,
    REASON_MISSING_DATA,
    REASON_STRATEGY_FLAGGED,
    LegalScoreValidator,
    _parse_flagged_strategies,
    get_default_score_validator,
    reset_default_score_validator,
    set_default_score_validator,
    validate_score,
)
from nexus_sdk.scoring import ScoringResult, SymbolScore


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _make_result(
    strategy_id: str = "strat-a",
    composites: list[float] | None = None,
) -> ScoringResult:
    """Build a ScoringResult with a few symbols at the given composites."""
    composites = composites if composites is not None else [80.0, 60.0, 40.0]
    scores = [SymbolScore(symbol=f"S{i}", composite_score=c) for i, c in enumerate(composites)]
    return ScoringResult(strategy_id=strategy_id, scores=scores)


# --------------------------------------------------------------------------- #
# LegalScoreValidator.validate_score
# --------------------------------------------------------------------------- #
class TestValidateScore:
    def test_happy_path_passes_score_through_unchanged(self) -> None:
        v = LegalScoreValidator(flagged_strategies=(), max_score=100.0)
        outcome = v.validate_score("strat-a", 42.5)

        assert outcome.score == 42.5
        assert not outcome.suppressed
        assert not outcome.capped
        assert outcome.reason is None
        assert outcome.passed is True
        assert outcome.original_score == 42.5
        assert outcome.strategy_id == "strat-a"

    def test_flagged_strategy_is_suppressed(self) -> None:
        v = LegalScoreValidator(flagged_strategies={"blocked-strat"}, max_score=100.0)
        outcome = v.validate_score("blocked-strat", 99.0)

        assert outcome.score is None
        assert outcome.suppressed is True
        assert outcome.capped is False
        assert outcome.reason == REASON_STRATEGY_FLAGGED
        assert outcome.passed is False
        # Original preserved for the audit trail even though suppressed.
        assert outcome.original_score == 99.0

    def test_non_flagged_strategy_is_not_suppressed_by_flagged_set(self) -> None:
        v = LegalScoreValidator(flagged_strategies={"blocked-strat"}, max_score=100.0)
        outcome = v.validate_score("clean-strat", 99.0)
        assert not outcome.suppressed
        assert outcome.score == 99.0

    def test_missing_data_none_score_is_suppressed(self) -> None:
        v = LegalScoreValidator()
        outcome = v.validate_score("strat-a", None)

        assert outcome.score is None
        assert outcome.suppressed is True
        assert outcome.reason == REASON_MISSING_DATA
        assert outcome.original_score is None

    def test_nan_score_is_suppressed_as_invalid(self) -> None:
        v = LegalScoreValidator()
        outcome = v.validate_score("strat-a", float("nan"))

        assert outcome.score is None
        assert outcome.suppressed is True
        assert outcome.reason == REASON_INVALID_SCORE

    @pytest.mark.parametrize("bad", [float("inf"), float("-inf")])
    def test_infinite_score_is_suppressed_as_invalid(self, bad: float) -> None:
        v = LegalScoreValidator()
        outcome = v.validate_score("strat-a", bad)

        assert outcome.score is None
        assert outcome.suppressed is True
        assert outcome.reason == REASON_INVALID_SCORE

    def test_score_above_legal_cap_is_clamped(self) -> None:
        v = LegalScoreValidator(flagged_strategies=(), max_score=85.0)
        outcome = v.validate_score("strat-a", 95.0)

        assert outcome.score == 85.0
        assert outcome.capped is True
        assert not outcome.suppressed
        assert outcome.reason == REASON_CAPPED
        assert outcome.original_score == 95.0

    def test_score_at_cap_is_not_capped(self) -> None:
        # Boundary: score == cap should pass through unchanged.
        v = LegalScoreValidator(max_score=85.0)
        outcome = v.validate_score("strat-a", 85.0)
        assert outcome.score == 85.0
        assert outcome.capped is False
        assert outcome.passed is True

    def test_ceiling_above_100_is_normalised_to_100(self) -> None:
        # A misconfigured ceiling must never exceed the technical max that
        # SymbolScore itself would reject.
        v = LegalScoreValidator(max_score=250.0)
        assert v.max_score == 100.0
        outcome = v.validate_score("strat-a", 100.0)
        assert outcome.score == 100.0
        assert outcome.capped is False

    def test_flagged_takes_precedence_over_cap(self) -> None:
        # A flagged strategy with an over-ceiling score is suppressed, not
        # capped — exposure is blocked outright, not merely reduced.
        v = LegalScoreValidator(flagged_strategies={"x"}, max_score=50.0)
        outcome = v.validate_score("x", 90.0)

        assert outcome.suppressed is True
        assert outcome.reason == REASON_STRATEGY_FLAGGED
        assert outcome.score is None


# --------------------------------------------------------------------------- #
# LegalScoreValidator.validate_result
# --------------------------------------------------------------------------- #
class TestValidateResult:
    def test_unflagged_result_passes_through_re_ranked(self) -> None:
        v = LegalScoreValidator(flagged_strategies=(), max_score=100.0)
        result = _make_result("strat-a", [80.0, 60.0, 40.0])

        gated = v.validate_result(result)

        assert [s.symbol for s in gated.scores] == ["S0", "S1", "S2"]
        assert [s.composite_score for s in gated.scores] == [80.0, 60.0, 40.0]
        # Ranks are recomputed (1-indexed, highest composite first).
        assert [s.rank for s in gated.scores] == [1, 2, 3]
        assert gated.strategy_id == "strat-a"

    def test_flagged_strategy_yields_empty_survivors(self) -> None:
        v = LegalScoreValidator(flagged_strategies={"strat-a"}, max_score=100.0)
        result = _make_result("strat-a", [80.0, 60.0, 40.0])

        gated = v.validate_result(result)

        assert gated.scores == []
        assert gated.strategy_id == "strat-a"
        # excluded_factors preserved even when all scores suppressed.
        assert gated.excluded_factors == []

    def test_over_cap_scores_are_capped_in_survivors(self) -> None:
        v = LegalScoreValidator(flagged_strategies=(), max_score=70.0)
        result = _make_result("strat-a", [95.0, 60.0, 30.0])

        gated = v.validate_result(result)

        # 95 clamped to 70; the others untouched. Survivor order is by the
        # *capped* composite desc, so 70 > 60 > 30.
        assert [s.composite_score for s in gated.scores] == [70.0, 60.0, 30.0]
        assert [s.symbol for s in gated.scores] == ["S0", "S1", "S2"]

    def test_original_result_is_not_mutated(self) -> None:
        v = LegalScoreValidator(max_score=50.0)
        result = _make_result("strat-a", [90.0, 10.0])
        original_first = result.scores[0].composite_score

        v.validate_result(result)

        # The caller's ScoringResult must be untouched; gating returns a copy.
        assert result.scores[0].composite_score == original_first == 90.0


# --------------------------------------------------------------------------- #
# Module-level convenience function + default-validator cache
# --------------------------------------------------------------------------- #
class TestModuleLevelFunction:
    def setup_method(self) -> None:
        # Each test starts from a clean default-validator cache so a prior
        # test's override never leaks in.
        reset_default_score_validator()

    def teardown_method(self) -> None:
        reset_default_score_validator()

    def test_validate_score_uses_default_validator(self) -> None:
        set_default_score_validator(LegalScoreValidator(flagged_strategies={"d"}))
        outcome = validate_score("d", 50.0)
        assert outcome.suppressed is True
        assert outcome.reason == REASON_STRATEGY_FLAGGED

    def test_explicit_validator_argument_wins(self) -> None:
        # Default flags "d", explicit validator flags "other" — the explicit
        # one must be used, not the default.
        set_default_score_validator(LegalScoreValidator(flagged_strategies={"d"}))
        explicit = LegalScoreValidator(flagged_strategies={"other"})

        outcome = validate_score("d", 50.0, validator=explicit)

        assert not outcome.suppressed  # "d" is not flagged by the explicit set
        assert outcome.score == 50.0

    def test_default_validator_is_memoised(self) -> None:
        first = get_default_score_validator()
        second = get_default_score_validator()
        assert first is second

    def test_setting_none_rebuilds_from_settings_on_next_call(self) -> None:
        first = get_default_score_validator()
        set_default_score_validator(None)
        second = get_default_score_validator()
        assert first is not second


# --------------------------------------------------------------------------- #
# Config parsing
# --------------------------------------------------------------------------- #
class TestConfigParsing:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("", frozenset()),
            ("a", frozenset({"a"})),
            ("a,b,c", frozenset({"a", "b", "c"})),
            (" a , b ,, c ", frozenset({"a", "b", "c"})),
            (",,,", frozenset()),
        ],
    )
    def test_parse_flagged_strategies(self, raw: str, expected: frozenset[str]) -> None:
        assert _parse_flagged_strategies(raw) == expected

    def test_parse_flagged_strategies_non_string_returns_empty(self) -> None:
        # Defensive: a non-string config value must never crash the pipeline.
        assert _parse_flagged_strategies(None) == frozenset()  # type: ignore[arg-type]

    def test_from_settings_reads_flagged_and_cap(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "legal_score_flagged_strategies", "alpha,beta")
        monkeypatch.setattr(settings, "legal_score_max_composite", "77.5")

        v = LegalScoreValidator.from_settings()

        assert v.flagged_strategies == frozenset({"alpha", "beta"})
        assert v.max_score == 77.5
        assert v.is_flagged("alpha")
        assert not v.is_flagged("gamma")


# --------------------------------------------------------------------------- #
# Route integration: read path gates stored snapshots at exposure time
# --------------------------------------------------------------------------- #
class TestScoringRouteGateIntegration:
    """The scoring route's read path must re-apply the legal gate."""

    async def _snapshot(
        self,
        db: AsyncSession,
        strategy_id: str,
        scores: list[dict[str, object]],
    ) -> None:
        # Flush (not commit): the per-test db_session fixture wraps everything
        # in a nested SAVEPOINT that rolls back at teardown, so we only flush
        # to make the row visible to the handler's SELECT on the same session.
        db.add(
            ScoringSnapshot(
                strategy_id=strategy_id,
                universe_size=len(scores),
                excluded_factors=[],
                results={
                    "strategy_id": strategy_id,
                    "scores": scores,
                    "excluded_factors": [],
                },
            )
        )
        await db.flush()

    async def test_flagged_strategy_scores_suppressed_on_read(
        self, db_session: AsyncSession
    ) -> None:
        await self._snapshot(
            db_session,
            "flagged-strat",
            [
                {"symbol": "A", "composite_score": 90.0, "rank": 1},
                {"symbol": "B", "composite_score": 70.0, "rank": 2},
            ],
        )

        app = create_app()
        app.dependency_overrides[get_db] = _session_dep(db_session)
        app.dependency_overrides[get_score_validator] = lambda: LegalScoreValidator(
            flagged_strategies={"flagged-strat"}
        )
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/v1/scoring/flagged-strat/results")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["count"] == 1
        # Every score suppressed → exposed scores list is empty even though
        # the stored snapshot had two.
        assert body["results"][0]["scores"] == []

    async def test_over_cap_scores_clamped_on_read(self, db_session: AsyncSession) -> None:
        await self._snapshot(
            db_session,
            "capped-strat",
            [
                {"symbol": "A", "composite_score": 99.0, "rank": 1},
                {"symbol": "B", "composite_score": 40.0, "rank": 2},
            ],
        )

        app = create_app()
        app.dependency_overrides[get_db] = _session_dep(db_session)
        app.dependency_overrides[get_score_validator] = lambda: LegalScoreValidator(max_score=85.0)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/v1/scoring/capped-strat/results")

        assert resp.status_code == 200, resp.text
        scores = resp.json()["results"][0]["scores"]
        composites = sorted(s["composite_score"] for s in scores)
        # 99 clamped to 85; 40 untouched.
        assert composites == [40.0, 85.0]

    async def test_clean_strategy_passes_through_on_read(self, db_session: AsyncSession) -> None:
        await self._snapshot(
            db_session,
            "clean-strat",
            [{"symbol": "A", "composite_score": 50.0, "rank": 1}],
        )

        app = create_app()
        app.dependency_overrides[get_db] = _session_dep(db_session)
        app.dependency_overrides[get_score_validator] = lambda: LegalScoreValidator(
            flagged_strategies={"something-else"}, max_score=100.0
        )
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/v1/scoring/clean-strat/results")

        assert resp.status_code == 200, resp.text
        scores = resp.json()["results"][0]["scores"]
        assert len(scores) == 1
        assert scores[0]["composite_score"] == 50.0


# --------------------------------------------------------------------------- #
# _gate_score_dicts unit (read-path helper)
# --------------------------------------------------------------------------- #
class TestGateScoreDicts:
    def test_suppresses_flagged_drops_entries(self) -> None:
        v = LegalScoreValidator(flagged_strategies={"f"})
        out = _gate_score_dicts(
            "f",
            [{"symbol": "A", "composite_score": 10.0}, {"symbol": "B", "composite_score": 20.0}],
            v,
        )
        assert out == []

    def test_caps_above_ceiling(self) -> None:
        v = LegalScoreValidator(max_score=80.0)
        out = _gate_score_dicts(
            "s",
            [{"symbol": "A", "composite_score": 99.0}, {"symbol": "B", "composite_score": 50.0}],
            v,
        )
        assert {e["symbol"]: e["composite_score"] for e in out} == {"A": 80.0, "B": 50.0}

    def test_missing_composite_dropped(self) -> None:
        v = LegalScoreValidator()
        out = _gate_score_dicts(
            "s",
            [{"symbol": "A"}, {"symbol": "B", "composite_score": 50.0}],
            v,
        )
        # Missing composite_score → None → suppressed.
        assert [e["symbol"] for e in out] == ["B"]

    def test_nan_composite_dropped(self) -> None:
        v = LegalScoreValidator()
        out = _gate_score_dicts(
            "s",
            [{"symbol": "A", "composite_score": float("nan")}],
            v,
        )
        assert out == []

    def test_nan_constant_is_genuine_nan(self) -> None:
        # Sanity guard against accidental literal-string "nan" usage.
        assert math.isnan(float("nan"))


# --------------------------------------------------------------------------- #
# Small helper so FastAPI's get_db override yields the shared per-test
# session (the route commits, so we hand it the real session).
# --------------------------------------------------------------------------- #
def _session_dep(session: AsyncSession):
    async def _override() -> AsyncSession:
        yield session

    return _override
