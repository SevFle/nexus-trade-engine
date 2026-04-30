"""Tests for engine.core.strategy_lifecycle — promotion state machine."""

from __future__ import annotations

import pytest

from engine.core.strategy_lifecycle import (
    InvalidTransitionError,
    LifecycleEvidence,
    LifecycleStage,
    LifecycleTransition,
    StrategyLifecycleService,
)


@pytest.fixture
def service():
    return StrategyLifecycleService()


def _ev(**kwargs) -> LifecycleEvidence:
    return LifecycleEvidence(**kwargs)


class TestLinearPath:
    @pytest.mark.asyncio
    async def test_draft_to_backtest_no_evidence_required(self, service):
        sid = "s-1"
        await service.set_stage(sid, LifecycleStage.DRAFT)
        out = await service.promote(
            sid, target=LifecycleStage.BACKTEST, evidence=_ev()
        )
        assert out.target == LifecycleStage.BACKTEST

    @pytest.mark.asyncio
    async def test_backtest_to_paper_requires_evidence(self, service):
        sid = "s-1"
        await service.set_stage(sid, LifecycleStage.BACKTEST)
        with pytest.raises(InvalidTransitionError):
            await service.promote(
                sid, target=LifecycleStage.PAPER, evidence=_ev()
            )

    @pytest.mark.asyncio
    async def test_backtest_to_paper_with_valid_evidence(self, service):
        sid = "s-1"
        await service.set_stage(sid, LifecycleStage.BACKTEST)
        out = await service.promote(
            sid,
            target=LifecycleStage.PAPER,
            evidence=_ev(backtest_id="bt-1", sharpe=1.2, max_drawdown_pct=8.0),
        )
        assert out.target == LifecycleStage.PAPER

    @pytest.mark.asyncio
    async def test_backtest_to_paper_low_sharpe_rejected(self, service):
        sid = "s-1"
        await service.set_stage(sid, LifecycleStage.BACKTEST)
        with pytest.raises(InvalidTransitionError, match="sharpe"):
            await service.promote(
                sid,
                target=LifecycleStage.PAPER,
                evidence=_ev(
                    backtest_id="bt-1", sharpe=0.1, max_drawdown_pct=8.0
                ),
            )

    @pytest.mark.asyncio
    async def test_paper_to_live_requires_paper_window(self, service):
        sid = "s-1"
        await service.set_stage(sid, LifecycleStage.PAPER)
        with pytest.raises(InvalidTransitionError, match="paper"):
            await service.promote(
                sid,
                target=LifecycleStage.LIVE,
                evidence=_ev(paper_days=2),
            )

    @pytest.mark.asyncio
    async def test_paper_to_live_with_valid_window(self, service):
        sid = "s-1"
        await service.set_stage(sid, LifecycleStage.PAPER)
        out = await service.promote(
            sid,
            target=LifecycleStage.LIVE,
            evidence=_ev(paper_days=14, paper_sharpe=1.0),
        )
        assert out.target == LifecycleStage.LIVE


class TestForbiddenSkips:
    @pytest.mark.asyncio
    async def test_draft_to_live_rejected(self, service):
        sid = "s-1"
        await service.set_stage(sid, LifecycleStage.DRAFT)
        with pytest.raises(InvalidTransitionError):
            await service.promote(
                sid,
                target=LifecycleStage.LIVE,
                evidence=_ev(paper_days=14, paper_sharpe=1.0),
            )

    @pytest.mark.asyncio
    async def test_draft_to_paper_rejected(self, service):
        sid = "s-1"
        await service.set_stage(sid, LifecycleStage.DRAFT)
        with pytest.raises(InvalidTransitionError):
            await service.promote(
                sid,
                target=LifecycleStage.PAPER,
                evidence=_ev(backtest_id="bt-1", sharpe=1.2, max_drawdown_pct=8.0),
            )


class TestRetire:
    @pytest.mark.asyncio
    async def test_retire_allowed_from_live(self, service):
        sid = "s-1"
        await service.set_stage(sid, LifecycleStage.LIVE)
        out = await service.promote(
            sid, target=LifecycleStage.RETIRED, evidence=_ev()
        )
        assert out.target == LifecycleStage.RETIRED

    @pytest.mark.asyncio
    async def test_retire_allowed_from_paper(self, service):
        sid = "s-1"
        await service.set_stage(sid, LifecycleStage.PAPER)
        out = await service.promote(
            sid, target=LifecycleStage.RETIRED, evidence=_ev()
        )
        assert out.target == LifecycleStage.RETIRED

    @pytest.mark.asyncio
    async def test_retired_cannot_be_promoted(self, service):
        sid = "s-1"
        await service.set_stage(sid, LifecycleStage.RETIRED)
        with pytest.raises(InvalidTransitionError):
            await service.promote(
                sid, target=LifecycleStage.DRAFT, evidence=_ev()
            )


class TestHistory:
    @pytest.mark.asyncio
    async def test_transitions_recorded_in_order(self, service):
        sid = "s-1"
        await service.set_stage(sid, LifecycleStage.DRAFT)
        await service.promote(
            sid, target=LifecycleStage.BACKTEST, evidence=_ev()
        )
        await service.promote(
            sid,
            target=LifecycleStage.PAPER,
            evidence=_ev(backtest_id="bt-1", sharpe=1.2, max_drawdown_pct=8.0),
        )
        history = await service.history(sid)
        assert [t.target for t in history] == [
            LifecycleStage.DRAFT,
            LifecycleStage.BACKTEST,
            LifecycleStage.PAPER,
        ]

    @pytest.mark.asyncio
    async def test_failed_transitions_not_recorded(self, service):
        sid = "s-1"
        await service.set_stage(sid, LifecycleStage.DRAFT)
        with pytest.raises(InvalidTransitionError):
            await service.promote(
                sid, target=LifecycleStage.LIVE, evidence=_ev()
            )
        history = await service.history(sid)
        assert len(history) == 1
        assert history[0].target == LifecycleStage.DRAFT


class TestEvidence:
    def test_evidence_dataclass_default(self):
        ev = LifecycleEvidence()
        assert ev.backtest_id is None
        assert ev.sharpe is None

    def test_evidence_dataclass_full(self):
        ev = LifecycleEvidence(
            backtest_id="bt-1",
            sharpe=1.5,
            max_drawdown_pct=12.0,
            paper_days=30,
            paper_sharpe=1.1,
        )
        assert ev.backtest_id == "bt-1"


class TestServiceIntrospection:
    @pytest.mark.asyncio
    async def test_get_current_stage(self, service):
        sid = "s-1"
        await service.set_stage(sid, LifecycleStage.PAPER)
        assert await service.current_stage(sid) == LifecycleStage.PAPER

    @pytest.mark.asyncio
    async def test_unknown_strategy_returns_none(self, service):
        assert await service.current_stage("never-set") is None


class TestTransitionRecord:
    def test_transition_carries_target_and_evidence(self):
        t = LifecycleTransition(
            strategy_id="s-1",
            target=LifecycleStage.PAPER,
            evidence=LifecycleEvidence(backtest_id="bt-1"),
            at_epoch=1.0,
        )
        assert t.target == LifecycleStage.PAPER
        assert t.evidence.backtest_id == "bt-1"
