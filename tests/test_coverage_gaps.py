from __future__ import annotations

import time
from datetime import date

import pytest

from engine.plugins.sandbox.core.policy import ResourcePolicy
from engine.plugins.sandbox.core.violation import ResourceExhausted
from engine.plugins.sandbox.layers.resource_limiter import (
    ResourceLimiter,
    _CPUTimer,
    _WallTimer,
)
from engine.reference.classification import is_valid_gics_path
from engine.reference.model import InstrumentIds, Listing, RefInstrument
from engine.reference.resolver import Resolver
from engine.reference.search import SearchIndex, _within_one_edit


def _aapl() -> RefInstrument:
    return RefInstrument(
        primary_ticker="AAPL",
        primary_venue="XNAS",
        asset_class="equity",
        name="Apple Inc.",
        currency="USD",
        ids=InstrumentIds(
            isin="US0378331005",
            cusip="037833100",
            figi="BBG000B9XRY4",
            cik="0000320193",
        ),
        listings=[
            Listing(
                venue="XNAS",
                ticker="AAPL",
                currency="USD",
                active_from=date(1980, 12, 12),
            ),
        ],
    )


class TestRegistryCoverage:
    def test_is_scoring_strategy_with_actual_instance(self) -> None:
        from engine.plugins.registry import is_scoring_strategy
        from nexus_sdk.scoring import IScoringStrategy

        class _Impl(IScoringStrategy):
            @property
            def id(self) -> str:
                return "t"

            @property
            def name(self) -> str:
                return "t"

            @property
            def version(self) -> str:
                return "0.0.1"

            def get_scoring_factors(self):
                return []

            async def score_universe(self, universe, market, costs):
                ...

            async def initialize(self, config):
                ...

            async def dispose(self):
                ...

            async def evaluate(self, portfolio, market, costs):
                return []

            def get_config_schema(self):
                return {}

        assert is_scoring_strategy(_Impl()) is True

    def test_is_scoring_strategy_with_non_instance(self) -> None:
        from engine.plugins.registry import is_scoring_strategy

        assert is_scoring_strategy(object()) is False

    def test_load_strategy_class_raises_on_missing_module(self) -> None:
        from engine.plugins.registry import load_strategy_class

        with pytest.raises(ImportError, match="Cannot load strategy"):
            load_strategy_class("/nonexistent/path/strategy.py")

    def test_load_strategy_class_raises_on_missing_class(self, tmp_path) -> None:
        from engine.plugins.registry import load_strategy_class

        module = tmp_path / "strategy.py"
        module.write_text("x = 1\n")
        with pytest.raises(AttributeError, match="does not define a 'Strategy' class"):
            load_strategy_class(str(module))

    def test_load_strategy_class_success(self, tmp_path) -> None:
        from engine.plugins.registry import load_strategy_class

        module = tmp_path / "strategy.py"
        module.write_text("class Strategy:\n    pass\n")
        cls = load_strategy_class(str(module))
        assert cls is not None
        assert cls.__name__ == "Strategy"


class TestScoringExecutorCoverage:
    def test_compute_scores_zero_total_weight(self) -> None:
        from engine.plugins.scoring_executor import ScoringExecutor
        from nexus_sdk.scoring import FactorDirection, ScoringFactor

        class _Strat:
            id = "t"
            name = "t"
            version = "0.0.1"

            def get_scoring_factors(self):
                return [ScoringFactor(name="f", weight=0.0, direction=FactorDirection.HIGHER_IS_BETTER)]

        executor = ScoringExecutor(_Strat(), min_data_points=1)
        result = executor.compute_scores(
            universe=["A"],
            raw_data={"A": {"f": 1.0}},
        )
        assert result.scores == []


class TestClassificationCoverage:
    def test_invalid_sub_industry_rejected(self) -> None:
        assert not is_valid_gics_path(
            "Information Technology",
            "Software & Services",
            "Software",
            "Nonexistent Sub",
        )

    def test_invalid_industry_rejected(self) -> None:
        assert not is_valid_gics_path(
            "Information Technology",
            "Software & Services",
            "Nonexistent Industry",
            "Anything",
        )

    def test_invalid_industry_group_rejected(self) -> None:
        assert not is_valid_gics_path(
            "Information Technology",
            "Nonexistent Group",
            "Software",
            "Application Software",
        )


class TestModelCoverage:
    def test_whitespace_ticker_rejected(self) -> None:
        with pytest.raises(ValueError):
            RefInstrument(
                primary_ticker=" AAPL ",
                primary_venue="XNAS",
                asset_class="equity",
                name="Apple",
            )

    def test_empty_ticker_rejected(self) -> None:
        with pytest.raises(ValueError):
            RefInstrument(
                primary_ticker=" ",
                primary_venue="XNAS",
                asset_class="equity",
                name="Apple",
            )


class TestResolverCoverage:
    def test_resolve_rejects_non_str_non_dict(self) -> None:
        r = Resolver()
        with pytest.raises(TypeError, match="resolve query must be str or dict"):
            r.resolve(12345)

    def test_resolve_dict_garbage_ticker_returns_none(self) -> None:
        r = Resolver()
        r.register(_aapl())
        assert r.resolve({"ticker": "<script>", "venue": "XNAS"}) is None

    def test_resolve_dict_no_recognized_keys_returns_none(self) -> None:
        r = Resolver()
        assert r.resolve({"unknown_key": "value"}) is None

    def test_resolve_dict_ticker_only_routes_to_resolve(self) -> None:
        r = Resolver()
        r.register(_aapl())
        out = r.resolve({"ticker": "AAPL"})
        assert out is not None
        assert out.primary_ticker == "AAPL"

    def test_resolve_dict_ticker_venue_match(self) -> None:
        r = Resolver()
        r.register(_aapl())
        out = r.resolve({"ticker": "AAPL", "venue": "XNAS"})
        assert out is not None

    def test_resolve_dict_isin_miss(self) -> None:
        r = Resolver()
        assert r.resolve({"isin": "MISSING12345"}) is None

    def test_resolve_dict_cusip_miss(self) -> None:
        r = Resolver()
        assert r.resolve({"cusip": "MISSING12"}) is None

    def test_resolve_dict_figi_miss(self) -> None:
        r = Resolver()
        assert r.resolve({"figi": "MISSINGFIGI12"}) is None

    def test_resolve_dict_cik_miss(self) -> None:
        r = Resolver()
        assert r.resolve({"cik": "9999999999"}) is None


class TestSearchCoverage:
    def test_search_empty_query(self) -> None:
        idx = SearchIndex()
        idx.add(_aapl())
        assert idx.search("") == []
        assert idx.search("   ") == []

    def test_search_too_long_query(self) -> None:
        idx = SearchIndex()
        idx.add(_aapl())
        assert idx.search("a" * 100) == []

    def test_suggest_too_long_query(self) -> None:
        idx = SearchIndex()
        idx.add(_aapl())
        assert idx.suggest("a" * 100) == []

    def test_suggest_fuzzy_with_asset_class_filter(self) -> None:
        idx = SearchIndex()
        idx.add(_aapl())
        result = idx.suggest("Aple", asset_class="crypto")
        assert result == []

    def test_suggest_name_exact_match_tier(self) -> None:
        idx = SearchIndex()
        idx.add(_aapl())
        out = idx.suggest("apple inc.")
        assert len(out) > 0
        assert out[0].score == 90

    def test_search_name_exact_match(self) -> None:
        idx = SearchIndex()
        idx.add(_aapl())
        results = idx.search("apple inc.")
        assert len(results) > 0
        assert results[0].primary_ticker == "AAPL"

    def test_search_ticker_contains(self) -> None:
        idx = SearchIndex()
        idx.add(_aapl())
        results = idx.search("ppl")
        assert any(r.primary_ticker == "AAPL" for r in results)

    def test_search_name_prefix(self) -> None:
        idx = SearchIndex()
        idx.add(_aapl())
        results = idx.search("apple")
        assert len(results) > 0

    def test_search_word_token_prefix(self) -> None:
        idx = SearchIndex()
        idx.add(
            RefInstrument(
                primary_ticker="BRK.B",
                primary_venue="XNYS",
                asset_class="equity",
                name="Berkshire Hathaway Inc.",
            )
        )
        results = idx.search("Hath")
        assert any(r.primary_ticker == "BRK.B" for r in results)

    def test_search_name_contains(self) -> None:
        idx = SearchIndex()
        idx.add(_aapl())
        results = idx.search("pple")
        assert any(r.primary_ticker == "AAPL" for r in results)

    def test_within_one_edit_identical(self) -> None:
        assert _within_one_edit("abc", "abc") is True

    def test_within_one_edit_trailing_char(self) -> None:
        assert _within_one_edit("ab", "abc") is True

    def test_within_one_edit_two_edits(self) -> None:
        assert _within_one_edit("ab", "cde") is False

    def test_within_one_edit_substitution(self) -> None:
        assert _within_one_edit("abc", "axc") is True

    def test_within_one_edit_insertion(self) -> None:
        assert _within_one_edit("ac", "abc") is True

    def test_within_one_edit_length_diff_gt_one(self) -> None:
        assert _within_one_edit("a", "abc") is False


class TestCPUTimerNewMethods:
    def test_cancel_method(self) -> None:
        t = _CPUTimer(10.0)
        t.start()
        t.cancel()
        assert t._thread is None

    def test_del_safety_net(self) -> None:
        t = _CPUTimer(10.0)
        t.start()
        t.__del__()
        assert t._thread is None

    def test_cpu_time_uses_os_times(self) -> None:
        import os

        t = _CPUTimer(10.0)
        cpu = t._cpu_time()
        ot = os.times()
        assert abs(cpu - (ot[0] + ot[1])) < 1.0

    def test_timer_expires_after_cpu_burn(self) -> None:
        t = _CPUTimer(0.01)
        t.start()
        _burn_cpu(0.05)
        assert t.expired
        t.cancel()

    def test_start_time_backward_compat(self) -> None:
        t = _CPUTimer(0.01)
        t._start_time = time.monotonic() - 10.0
        with pytest.raises(ResourceExhausted):
            t.check()
        assert t.expired

    def test_on_timeout_backward_compat(self) -> None:
        t = _CPUTimer(10.0)
        t._on_timeout()
        assert t.expired

    def test_timer_property_is_none(self) -> None:
        t = _CPUTimer(10.0)
        assert t._timer is None
        t.start()
        assert t._timer is None
        t.cancel()


class TestWallTimerNewMethods:
    def test_cancel_method(self) -> None:
        t = _WallTimer(10.0)
        t.start()
        t.cancel()
        assert t._timer is None

    def test_del_safety_net(self) -> None:
        t = _WallTimer(10.0)
        t.start()
        t.__del__()
        assert t._timer is None

    def test_check_elapsed_exceeds_limit(self) -> None:
        t = _WallTimer(0.01)
        t._start_time = time.monotonic() - 10.0
        with pytest.raises(ResourceExhausted, match="wall_time"):
            t.check()
        assert t.expired

    def test_elapsed_before_start(self) -> None:
        t = _WallTimer(10.0)
        assert t.elapsed == 0.0

    def test_double_cancel_safe(self) -> None:
        t = _WallTimer(10.0)
        t.start()
        t.cancel()
        t.cancel()
        assert t._timer is None

    def test_event_based_expired(self) -> None:
        t = _WallTimer(10.0)
        assert not t.expired
        t._on_timeout()
        assert t.expired


class TestResourceLimiterCancel:
    def test_uninstall_uses_cancel(self) -> None:
        limiter = ResourceLimiter(ResourcePolicy())
        limiter.install()
        assert limiter._cpu_timer is not None
        assert limiter._wall_timer is not None
        limiter.uninstall()
        assert limiter._cpu_timer is None
        assert limiter._wall_timer is None

    def test_uninstall_try_finally_on_timers(self) -> None:
        policy = ResourcePolicy(max_cpu_seconds=0.01, wall_time_seconds=0.01)
        limiter = ResourceLimiter(policy, plugin_id="test")
        limiter.install()
        time.sleep(0.05)
        limiter.uninstall()
        assert not limiter._installed


def _burn_cpu(duration: float) -> None:
    end = time.monotonic() + duration
    total = 0.0
    while time.monotonic() < end:
        total += 1.0
