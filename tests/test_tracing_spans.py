"""Tests for tracing span error handling in instrumented modules.

Each test verifies that:
1. Tracing spans are created with correct names
2. Exceptions are recorded on the span (set_status + record_exception)
3. Exceptions are re-raised after recording
4. Previously uncovered lines are now exercised
"""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

import pytest
from opentelemetry import trace

from engine.reference.classification import is_valid_gics_path
from engine.reference.model import InstrumentIds, Listing, RefInstrument
from engine.reference.resolver import Resolver
from engine.reference.search import SearchIndex


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


def _make_span_mock():
    span = MagicMock()
    span.__enter__ = MagicMock(return_value=span)
    span.__exit__ = MagicMock(return_value=False)
    return span


class TestRegistryTracing:
    @patch("engine.plugins.registry._tracer")
    def test_is_scoring_strategy_creates_span(self, mock_tracer):
        from engine.plugins.registry import is_scoring_strategy

        mock_span = _make_span_mock()
        mock_tracer.start_as_current_span.return_value = mock_span

        is_scoring_strategy(object())
        mock_tracer.start_as_current_span.assert_called_once_with("registry.is_scoring_strategy")

    @patch("engine.plugins.registry._tracer")
    def test_load_strategy_class_creates_span(self, mock_tracer, tmp_path):
        from engine.plugins.registry import load_strategy_class

        mock_span = _make_span_mock()
        mock_tracer.start_as_current_span.return_value = mock_span

        module = tmp_path / "strategy.py"
        module.write_text("class Strategy:\n    pass\n")
        load_strategy_class(str(module))
        mock_tracer.start_as_current_span.assert_called_once_with("registry.load_strategy_class")
        mock_span.set_attribute.assert_called()

    @patch("engine.plugins.registry._tracer")
    def test_load_strategy_class_error_records_on_span(self, mock_tracer):
        from engine.plugins.registry import load_strategy_class

        mock_span = _make_span_mock()
        mock_tracer.start_as_current_span.return_value = mock_span

        with pytest.raises(ImportError):
            load_strategy_class("/nonexistent/path/strategy.py")
        mock_span.set_status.assert_called_once()
        assert mock_span.set_status.call_args[0][0] == trace.StatusCode.ERROR
        mock_span.record_exception.assert_called_once()

    @patch("engine.plugins.registry._tracer")
    def test_load_strategy_class_missing_class_records_error(self, mock_tracer, tmp_path):
        from engine.plugins.registry import load_strategy_class

        mock_span = _make_span_mock()
        mock_tracer.start_as_current_span.return_value = mock_span

        module = tmp_path / "strategy.py"
        module.write_text("x = 1\n")
        with pytest.raises(AttributeError):
            load_strategy_class(str(module))
        mock_span.set_status.assert_called_once()
        assert mock_span.set_status.call_args[0][0] == trace.StatusCode.ERROR
        mock_span.record_exception.assert_called_once()

    @patch("engine.plugins.registry._tracer")
    def test_is_scoring_strategy_true_records_span(self, mock_tracer):
        from engine.plugins.registry import is_scoring_strategy
        from nexus_sdk.scoring import IScoringStrategy

        mock_span = _make_span_mock()
        mock_tracer.start_as_current_span.return_value = mock_span

        class _Impl(IScoringStrategy):
            @property
            def id(self):
                return "t"

            @property
            def name(self):
                return "t"

            @property
            def version(self):
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
        mock_span.set_status.assert_not_called()


class TestScoringExecutorTracing:
    @patch("engine.plugins.scoring_executor._tracer")
    def test_compute_scores_creates_span(self, mock_tracer):
        from engine.plugins.scoring_executor import ScoringExecutor
        from nexus_sdk.scoring import FactorDirection, ScoringFactor

        mock_span = _make_span_mock()
        mock_tracer.start_as_current_span.return_value = mock_span

        class _Strat:
            id = "t"
            name = "t"
            version = "0.0.1"

            def get_scoring_factors(self):
                return [ScoringFactor(name="f", weight=0.0, direction=FactorDirection.HIGHER_IS_BETTER)]

        executor = ScoringExecutor(_Strat(), min_data_points=1)
        executor.compute_scores(universe=["A"], raw_data={"A": {"f": 1.0}})
        mock_tracer.start_as_current_span.assert_called_once_with("scoring_executor.compute_scores")

    @patch("engine.plugins.scoring_executor._tracer")
    def test_compute_scores_zero_weight_span_ok(self, mock_tracer):
        from engine.plugins.scoring_executor import ScoringExecutor
        from nexus_sdk.scoring import FactorDirection, ScoringFactor

        mock_span = _make_span_mock()
        mock_tracer.start_as_current_span.return_value = mock_span

        class _Strat:
            id = "t"
            name = "t"
            version = "0.0.1"

            def get_scoring_factors(self):
                return [ScoringFactor(name="f", weight=0.0, direction=FactorDirection.HIGHER_IS_BETTER)]

        executor = ScoringExecutor(_Strat(), min_data_points=1)
        result = executor.compute_scores(universe=["A"], raw_data={"A": {"f": 1.0}})
        assert result.scores == []
        mock_span.set_status.assert_not_called()


class TestClassificationTracing:
    @patch("engine.reference.classification._tracer")
    def test_valid_gics_creates_span(self, mock_tracer):
        mock_span = _make_span_mock()
        mock_tracer.start_as_current_span.return_value = mock_span

        assert is_valid_gics_path(
            "Information Technology",
            "Software & Services",
            "Software",
            "Application Software",
        )
        mock_tracer.start_as_current_span.assert_called_once_with("classification.is_valid_gics_path")

    @patch("engine.reference.classification._tracer")
    def test_invalid_gics_creates_span_ok(self, mock_tracer):
        mock_span = _make_span_mock()
        mock_tracer.start_as_current_span.return_value = mock_span

        assert not is_valid_gics_path(
            "Information Technology",
            "Software & Services",
            "Software",
            "Nonexistent Sub",
        )
        mock_span.set_status.assert_not_called()


class TestModelTracing:
    @patch("engine.reference.model._tracer")
    def test_valid_ticker_creates_span(self, mock_tracer):
        mock_span = _make_span_mock()
        mock_tracer.start_as_current_span.return_value = mock_span

        RefInstrument(
            primary_ticker="AAPL",
            primary_venue="XNAS",
            asset_class="equity",
            name="Apple",
        )
        mock_tracer.start_as_current_span.assert_called_with("model.ticker_no_whitespace")

    def test_whitespace_ticker_rejected_by_pattern(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            RefInstrument(
                primary_ticker=" AAPL ",
                primary_venue="XNAS",
                asset_class="equity",
                name="Apple",
            )

    def test_empty_ticker_rejected_by_pattern(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            RefInstrument(
                primary_ticker=" ",
                primary_venue="XNAS",
                asset_class="equity",
                name="Apple",
            )

    @patch("engine.reference.model._tracer")
    def test_validator_error_path_direct_call(self, mock_tracer):
        mock_span = _make_span_mock()
        mock_tracer.start_as_current_span.return_value = mock_span

        with pytest.raises(ValueError, match="primary_ticker must be non-empty"):
            RefInstrument._ticker_no_whitespace("trimmed_but_invalid  ")

        mock_span.set_status.assert_called_once()
        assert mock_span.set_status.call_args[0][0] == trace.StatusCode.ERROR
        mock_span.record_exception.assert_called_once()


class TestResolverTracing:
    @patch("engine.reference.resolver._tracer")
    def test_resolve_creates_span(self, mock_tracer):
        mock_span = _make_span_mock()
        mock_tracer.start_as_current_span.return_value = mock_span

        r = Resolver()
        r.register(_aapl())
        r.resolve("AAPL")
        mock_tracer.start_as_current_span.assert_any_call("resolver.resolve")

    @patch("engine.reference.resolver._tracer")
    def test_resolve_type_error_records_on_span(self, mock_tracer):
        mock_span = _make_span_mock()
        mock_tracer.start_as_current_span.return_value = mock_span

        r = Resolver()
        with pytest.raises(TypeError):
            r.resolve(12345)
        mock_span.set_status.assert_called_once()
        assert mock_span.set_status.call_args[0][0] == trace.StatusCode.ERROR
        mock_span.record_exception.assert_called_once()

    @patch("engine.reference.resolver._tracer")
    def test_resolve_dict_creates_span(self, mock_tracer):
        mock_span = _make_span_mock()
        mock_tracer.start_as_current_span.return_value = mock_span

        r = Resolver()
        r.register(_aapl())
        r.resolve({"ticker": "AAPL", "venue": "XNAS"})
        mock_tracer.start_as_current_span.assert_any_call("resolver.resolve_dict")

    @patch("engine.reference.resolver._tracer")
    def test_resolve_dict_garbage_ticker_creates_span(self, mock_tracer):
        mock_span = _make_span_mock()
        mock_tracer.start_as_current_span.return_value = mock_span

        r = Resolver()
        r.register(_aapl())
        result = r.resolve({"ticker": "<script>", "venue": "XNAS"})
        assert result is None
        mock_tracer.start_as_current_span.assert_any_call("resolver.resolve_dict")

    @patch("engine.reference.resolver._tracer")
    def test_resolve_dict_no_keys_creates_span(self, mock_tracer):
        mock_span = _make_span_mock()
        mock_tracer.start_as_current_span.return_value = mock_span

        r = Resolver()
        result = r.resolve({"unknown_key": "value"})
        assert result is None
        mock_tracer.start_as_current_span.assert_any_call("resolver.resolve_dict")

    @patch("engine.reference.resolver._tracer")
    def test_resolve_dict_ticker_only_creates_span(self, mock_tracer):
        mock_span = _make_span_mock()
        mock_tracer.start_as_current_span.return_value = mock_span

        r = Resolver()
        r.register(_aapl())
        result = r.resolve({"ticker": "AAPL"})
        assert result is not None
        mock_tracer.start_as_current_span.assert_any_call("resolver.resolve_dict")

    def test_resolve_dict_miss_returns_none(self):
        r = Resolver()
        assert r.resolve({"isin": "MISSING12345"}) is None
        assert r.resolve({"cusip": "MISSING12"}) is None
        assert r.resolve({"figi": "MISSINGFIGI12"}) is None
        assert r.resolve({"cik": "9999999999"}) is None


class TestSearchTracing:
    @patch("engine.reference.search._tracer")
    def test_search_creates_span(self, mock_tracer):
        mock_span = _make_span_mock()
        mock_tracer.start_as_current_span.return_value = mock_span

        idx = SearchIndex()
        idx.add(_aapl())
        idx.search("AAPL")
        mock_tracer.start_as_current_span.assert_called_once_with("search.index_search")

    @patch("engine.reference.search._tracer")
    def test_search_empty_query_span_ok(self, mock_tracer):
        mock_span = _make_span_mock()
        mock_tracer.start_as_current_span.return_value = mock_span

        idx = SearchIndex()
        idx.add(_aapl())
        assert idx.search("") == []
        mock_span.set_status.assert_not_called()

    @patch("engine.reference.search._tracer")
    def test_search_too_long_query_span_ok(self, mock_tracer):
        mock_span = _make_span_mock()
        mock_tracer.start_as_current_span.return_value = mock_span

        idx = SearchIndex()
        idx.add(_aapl())
        assert idx.search("a" * 100) == []
        mock_span.set_status.assert_not_called()

    @patch("engine.reference.search._tracer")
    def test_suggest_creates_span(self, mock_tracer):
        mock_span = _make_span_mock()
        mock_tracer.start_as_current_span.return_value = mock_span

        idx = SearchIndex()
        idx.add(_aapl())
        idx.suggest("AAPL")
        mock_tracer.start_as_current_span.assert_called_once_with("search.index_suggest")

    @patch("engine.reference.search._tracer")
    def test_suggest_empty_query_span_ok(self, mock_tracer):
        mock_span = _make_span_mock()
        mock_tracer.start_as_current_span.return_value = mock_span

        idx = SearchIndex()
        idx.add(_aapl())
        assert idx.suggest("") == []
        mock_span.set_status.assert_not_called()

    @patch("engine.reference.search._tracer")
    def test_suggest_too_long_query_span_ok(self, mock_tracer):
        mock_span = _make_span_mock()
        mock_tracer.start_as_current_span.return_value = mock_span

        idx = SearchIndex()
        idx.add(_aapl())
        assert idx.suggest("a" * 100) == []
        mock_span.set_status.assert_not_called()

    @patch("engine.reference.search._tracer")
    def test_suggest_with_asset_class_filter_span(self, mock_tracer):
        mock_span = _make_span_mock()
        mock_tracer.start_as_current_span.return_value = mock_span

        idx = SearchIndex()
        idx.add(_aapl())
        result = idx.suggest("Aple", asset_class="crypto")
        assert result == []
        mock_span.set_status.assert_not_called()

    def test_search_name_exact_match(self):
        idx = SearchIndex()
        idx.add(_aapl())
        results = idx.search("apple inc.")
        assert len(results) > 0

    def test_search_ticker_contains(self):
        idx = SearchIndex()
        idx.add(_aapl())
        results = idx.search("ppl")
        assert any(r.primary_ticker == "AAPL" for r in results)

    def test_search_name_prefix(self):
        idx = SearchIndex()
        idx.add(_aapl())
        results = idx.search("apple")
        assert len(results) > 0

    def test_suggest_name_exact_match(self):
        idx = SearchIndex()
        idx.add(_aapl())
        out = idx.suggest("apple inc.")
        assert len(out) > 0
        assert out[0].score == 90


class TestSandboxLegacyTracing:
    @patch("engine.plugins.sandbox._sandbox_legacy._tracer")
    def test_apply_resource_limits_creates_span(self, mock_tracer):
        from engine.plugins.sandbox._sandbox_legacy import StrategySandbox

        mock_span = _make_span_mock()
        mock_tracer.start_as_current_span.return_value = mock_span

        class _Strat:
            name = "test"
            version = "1.0.0"

        from engine.plugins.manifest import StrategyManifest

        manifest = StrategyManifest(
            id="test", name="test", version="1.0.0",
            resources={"max_cpu_seconds": 1, "max_memory": "512MB"},
        )
        sandbox = StrategySandbox(_Strat(), manifest)
        sandbox._apply_resource_limits()
        mock_tracer.start_as_current_span.assert_called_once_with("sandbox_legacy.apply_resource_limits")

    @patch("engine.plugins.sandbox._sandbox_legacy._tracer")
    def test_restore_resource_limits_creates_span(self, mock_tracer):
        from engine.plugins.sandbox._sandbox_legacy import StrategySandbox

        mock_span = _make_span_mock()
        mock_tracer.start_as_current_span.return_value = mock_span

        class _Strat:
            name = "test"
            version = "1.0.0"

        from engine.plugins.manifest import StrategyManifest

        manifest = StrategyManifest(
            id="test", name="test", version="1.0.0",
            resources={"max_cpu_seconds": 1, "max_memory": "512MB"},
        )
        sandbox = StrategySandbox(_Strat(), manifest)
        sandbox._restore_resource_limits()
        mock_tracer.start_as_current_span.assert_called_once_with("sandbox_legacy.restore_resource_limits")

    @patch("engine.plugins.sandbox._sandbox_legacy._tracer")
    def test_restricted_open_file_descriptor_raises(self, mock_tracer):
        from engine.plugins.sandbox._sandbox_legacy import StrategySandbox

        mock_span = _make_span_mock()
        mock_tracer.start_as_current_span.return_value = mock_span

        class _Strat:
            name = "test"
            version = "1.0.0"

        from engine.plugins.manifest import StrategyManifest

        manifest = StrategyManifest(
            id="test", name="test", version="1.0.0",
            resources={"max_cpu_seconds": 1},
        )
        sandbox = StrategySandbox(_Strat(), manifest)
        with pytest.raises(PermissionError, match="File descriptor"):
            sandbox._restricted_open(0)
        mock_span.set_status.assert_called_once()
        assert mock_span.set_status.call_args[0][0] == trace.StatusCode.ERROR
        mock_span.record_exception.assert_called_once()

    @patch("engine.plugins.sandbox._sandbox_legacy._tracer")
    def test_restricted_open_outside_workdir_raises(self, mock_tracer):
        from engine.plugins.sandbox._sandbox_legacy import StrategySandbox

        mock_span = _make_span_mock()
        mock_tracer.start_as_current_span.return_value = mock_span

        class _Strat:
            name = "test"
            version = "1.0.0"

        from engine.plugins.manifest import StrategyManifest

        manifest = StrategyManifest(
            id="test", name="test", version="1.0.0",
            resources={"max_cpu_seconds": 1},
        )
        sandbox = StrategySandbox(_Strat(), manifest)
        sandbox._original_open = open
        with pytest.raises(PermissionError, match="File access"):
            sandbox._restricted_open("/etc/passwd")
        mock_span.set_status.assert_called_once()
        assert mock_span.set_status.call_args[0][0] == trace.StatusCode.ERROR

    @patch("engine.plugins.sandbox._sandbox_legacy._tracer")
    def test_cleanup_creates_span(self, mock_tracer):
        from engine.plugins.sandbox._sandbox_legacy import StrategySandbox

        mock_span = _make_span_mock()
        mock_tracer.start_as_current_span.return_value = mock_span

        class _Strat:
            name = "test"
            version = "1.0.0"

        from engine.plugins.manifest import StrategyManifest

        manifest = StrategyManifest(
            id="test", name="test", version="1.0.0",
            resources={"max_cpu_seconds": 1},
        )
        sandbox = StrategySandbox(_Strat(), manifest)
        sandbox.cleanup()
        mock_tracer.start_as_current_span.assert_called_once_with("sandbox_legacy.cleanup")

    @patch("engine.plugins.sandbox._sandbox_legacy._tracer")
    def test_from_factory_creates_span(self, mock_tracer):
        from engine.plugins.sandbox._sandbox_legacy import StrategySandbox

        mock_span = _make_span_mock()
        mock_tracer.start_as_current_span.return_value = mock_span

        class _Strat:
            name = "factory_test"
            version = "1.0.0"

        from engine.plugins.manifest import StrategyManifest

        manifest = StrategyManifest(
            id="test", name="test", version="1.0.0",
            resources={"max_cpu_seconds": 1},
        )
        sandbox = StrategySandbox.from_factory(_Strat, manifest)
        assert sandbox.strategy.name == "factory_test"
        mock_tracer.start_as_current_span.assert_any_call("sandbox_legacy.from_factory")

    @patch("engine.plugins.sandbox._sandbox_legacy._tracer")
    async def test_safe_evaluate_creates_span(self, mock_tracer):
        from engine.plugins.sandbox._sandbox_legacy import StrategySandbox

        mock_span = _make_span_mock()
        mock_tracer.start_as_current_span.return_value = mock_span

        class _Strat:
            name = "eval_test"
            version = "1.0.0"

            def on_bar(self, state, portfolio):
                return []

        from engine.plugins.manifest import StrategyManifest

        manifest = StrategyManifest(
            id="test", name="test", version="1.0.0",
            resources={"max_cpu_seconds": 30},
        )
        sandbox = StrategySandbox.from_factory(_Strat, manifest)
        result = await sandbox.safe_evaluate(None, None, None)
        assert result == []
        mock_tracer.start_as_current_span.assert_any_call("sandbox_legacy.safe_evaluate")

    @patch("engine.plugins.sandbox._sandbox_legacy._tracer")
    def test_make_restricted_send_creates_span(self, mock_tracer):
        from engine.plugins.sandbox._sandbox_legacy import StrategySandbox

        mock_span = _make_span_mock()
        mock_tracer.start_as_current_span.return_value = mock_span

        class _Strat:
            name = "test"
            version = "1.0.0"

        from engine.plugins.manifest import StrategyManifest

        manifest = StrategyManifest(
            id="test", name="test", version="1.0.0",
            resources={"max_cpu_seconds": 1},
            network={"allowed_endpoints": ["api.example.com"]},
        )
        sandbox = StrategySandbox(_Strat(), manifest)
        sandbox._original_httpx_send = MagicMock()
        result = sandbox._make_restricted_send()
        assert result is not None
        mock_tracer.start_as_current_span.assert_any_call("sandbox_legacy.make_restricted_send")


class TestSearchCoverageEdgeCases:
    def test_search_whitespace_query(self):
        idx = SearchIndex()
        idx.add(_aapl())
        assert idx.search("   ") == []

    def test_suggest_whitespace_query(self):
        idx = SearchIndex()
        idx.add(_aapl())
        assert idx.suggest("   ") == []

    def test_suggest_fuzzy_match(self):
        idx = SearchIndex()
        idx.add(_aapl())
        result = idx.suggest("Aple")
        assert len(result) > 0
        assert result[0].score == 15

    def test_suggest_ticker_prefix(self):
        idx = SearchIndex()
        idx.add(_aapl())
        result = idx.suggest("AA")
        assert len(result) > 0

    def test_suggest_ticker_exact(self):
        idx = SearchIndex()
        idx.add(_aapl())
        result = idx.suggest("aapl")
        assert len(result) > 0
        assert result[0].score == 100

    def test_suggest_name_token_match(self):
        idx = SearchIndex()
        idx.add(_aapl())
        result = idx.suggest("inc")
        assert len(result) > 0

    def test_search_word_token_prefix(self):
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

    def test_search_name_contains(self):
        idx = SearchIndex()
        idx.add(_aapl())
        results = idx.search("pple")
        assert any(r.primary_ticker == "AAPL" for r in results)

    def test_search_with_asset_class_filter(self):
        idx = SearchIndex()
        idx.add(_aapl())
        idx.add(
            RefInstrument(
                primary_ticker="ETH",
                primary_venue="XCRY",
                asset_class="crypto",
                name="Ethereum",
            )
        )
        results = idx.search("e", asset_class="crypto")
        assert all(r.asset_class == "crypto" for r in results)

    def test_within_one_edit_edge_cases(self):
        from engine.reference.search import _within_one_edit

        assert _within_one_edit("abc", "abc") is True
        assert _within_one_edit("ab", "abc") is True
        assert _within_one_edit("abc", "axc") is True
        assert _within_one_edit("ac", "abc") is True
        assert _within_one_edit("a", "abc") is False
        assert _within_one_edit("ab", "cde") is False
