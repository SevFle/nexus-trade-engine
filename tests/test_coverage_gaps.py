"""Coverage gap closures for engine.plugins and engine.reference modules.

Targets the lines reported as uncovered in the most recent coverage run:

- engine/plugins/registry.py          : 20-21, 56
- engine/plugins/sandbox.py           : 48-50, 97, 176, 184-185, 192-193,
                                        197, 231, 254, 386-387
- engine/plugins/scoring_executor.py  : 77
- engine/reference/classification.py  : 67
- engine/reference/model.py           : 146-147
- engine/reference/resolver.py        : 124-125, 143, 147
- engine/reference/search.py          : 57, 100, 117, 131, 187, 193, 227, 254
"""

from __future__ import annotations

import builtins
from datetime import date
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import httpx
import pytest

if TYPE_CHECKING:
    from engine.core.signal import Signal as EngineSignal
    from nexus_sdk.strategy import MarketState, StrategyConfig

from engine.plugins.manifest import StrategyManifest
from engine.plugins.sandbox import (
    HAS_RESOURCE_MODULE,
    StrategySandbox,
    _PlaceholderStrategy,
    _resource,
)
from engine.reference import Listing, RefInstrument, Resolver
from engine.reference.classification import is_valid_gics_path
from engine.reference.search import SearchIndex, _within_one_edit

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _manifest(network: list[str] | None = None, artifacts: list[str] | None = None) -> StrategyManifest:
    return StrategyManifest(
        id="cov",
        name="cov",
        version="1.0.0",
        resources={"max_cpu_seconds": 1},
        network={"allowed_endpoints": network or []},
        artifacts=artifacts or [],
    )


class _NoOpStrategy:
    name = "noop"
    version = "1.0.0"

    def on_bar(self, _state: Any, _portfolio: Any) -> list[Any]:
        return []


class _AllowedHttpxStrategy:
    """Strategy that tries to call an endpoint on the manifest allowlist."""

    name = "allowed_http"
    version = "1.0.0"

    def __init__(self, url: str) -> None:
        self._url = url

    async def on_bar(self, _state: Any, _portfolio: Any) -> list[Any]:
        async with httpx.AsyncClient() as client:
            await client.get(self._url)
        return []


# ---------------------------------------------------------------------------
# engine/plugins/registry.py — lines 20-21, 56
# ---------------------------------------------------------------------------


class TestRegistryCoverage:
    def test_is_scoring_strategy_returns_false_on_import_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Line 20-21: ``ImportError`` branch when nexus_sdk is unavailable."""
        import engine.plugins.registry as registry_mod

        real_import = builtins.__import__

        def _block_nexus_sdk(name: str, *args: Any, **kwargs: Any) -> Any:
            if name == "nexus_sdk.scoring" or name.startswith("nexus_sdk"):
                raise ImportError("blocked for test")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _block_nexus_sdk)
        assert registry_mod.is_scoring_strategy(object()) is False

    def test_load_strategy_class_raises_when_spec_is_none(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Line 56: spec is None → ImportError."""
        import importlib.util

        from engine.plugins.registry import load_strategy_class

        target = tmp_path / "empty.py"
        target.write_text("")

        # Force spec_from_file_location to return None by pointing at a
        # path that exists but mimics a broken loader situation.
        monkeypatch.setattr(
            importlib.util,
            "spec_from_file_location",
            lambda *a, **kw: None,
        )

        with pytest.raises(ImportError, match="Cannot load strategy"):
            load_strategy_class(str(target))


# ---------------------------------------------------------------------------
# engine/plugins/sandbox.py — multiple lines
# ---------------------------------------------------------------------------


class TestSandboxNoResourceModule:
    """Lines 48-50, 176, 197: HAS_RESOURCE_MODULE = False branch."""

    def test_placeholder_strategy_on_bar_returns_empty(self) -> None:
        """Line 97: ``_PlaceholderStrategy.on_bar`` returns ``[]``."""
        assert _PlaceholderStrategy().on_bar(None, None) == []

    def test_apply_resource_limits_skipped_when_no_resource_module(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Line 176: early return when ``HAS_RESOURCE_MODULE`` is False."""
        import engine.plugins.sandbox as sandbox_mod

        monkeypatch.setattr(sandbox_mod, "HAS_RESOURCE_MODULE", False)
        sandbox = StrategySandbox(_NoOpStrategy(), _manifest())
        try:
            # Should hit the early return at line 176.
            sandbox._apply_resource_limits()
            # And the early return at line 197 in _restore_resource_limits.
            sandbox._restore_resource_limits()
        finally:
            sandbox.cleanup()

    def test_import_fallback_when_resource_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Lines 48-50: ``except ImportError`` branch when ``resource`` is missing.

        Reloads :mod:`engine.plugins.sandbox` with ``resource`` hidden so the
        ``except ImportError`` arm executes and ``HAS_RESOURCE_MODULE`` is
        ``False`` on the freshly-loaded module.
        """
        import importlib
        import sys

        # Snapshot and remove the resource module so the import inside sandbox
        # fails. Restore on exit so other tests still see it.
        saved_resource = sys.modules.pop("resource", None)
        saved_sandbox = sys.modules.pop("engine.plugins.sandbox", None)

        # Block re-import by inserting a finder that raises ImportError.
        class _BlockResource:
            def find_spec(self, name, path=None, target=None):
                if name == "resource":
                    raise ImportError("blocked for test")

        blocker = _BlockResource()
        sys.meta_path.insert(0, blocker)
        try:
            fresh = importlib.import_module("engine.plugins.sandbox")
            assert fresh.HAS_RESOURCE_MODULE is False
            assert fresh._resource is None
        finally:
            sys.meta_path.remove(blocker)
            # Restore the original sandbox module so other tests see the
            # already-imported (resource-present) version.
            if saved_sandbox is not None:
                sys.modules["engine.plugins.sandbox"] = saved_sandbox
            if saved_resource is not None:
                sys.modules["resource"] = saved_resource


class TestSandboxResourceLimitExceptions:
    """Lines 184-185, 192-193: ``except`` branches in _apply_resource_limits."""

    def test_apply_resource_limits_swallows_setrlimit_errors(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:

        # Force setrlimit to raise OSError so both RLIMIT_AS and RLIMIT_NOFILE
        # try/except blocks trip their except clauses (lines 184-185, 192-193).
        real_setrlimit = _resource.setrlimit if (HAS_RESOURCE_MODULE and _resource) else None

        def _raising_setrlimit(*_args: Any, **_kwargs: Any) -> Any:
            raise OSError("simulated setrlimit failure")

        if real_setrlimit is None:
            pytest.skip("resource module not available on this platform")

        monkeypatch.setattr(_resource, "setrlimit", _raising_setrlimit)

        sandbox = StrategySandbox(_NoOpStrategy(), _manifest())
        try:
            # Both except branches should swallow the OSError and not re-raise.
            sandbox._apply_resource_limits()
            # And _restore_resource_limits should also swallow silently.
            sandbox._restore_resource_limits()
        finally:
            sandbox.cleanup()


class TestSandboxWriteModeBlocked:
    """Line 231: write mode blocked even when path is allowed."""

    def test_write_mode_in_work_dir_rejected(self, tmp_path: Any) -> None:
        sandbox = StrategySandbox(_NoOpStrategy(), _manifest())
        try:
            target = sandbox._work_dir or ""
            assert target, "sandbox work dir should be created by __init__"
            sample = f"{target}/sample.txt"
            with pytest.raises(PermissionError, match="Write access"):
                sandbox._restricted_open(sample, "w")
            with pytest.raises(PermissionError, match="Write access"):
                sandbox._restricted_open(sample, "a")
            with pytest.raises(PermissionError, match="Write access"):
                sandbox._restricted_open(sample, "r+")
        finally:
            sandbox.cleanup()


class TestSandboxAllowedHttpxSend:
    """Line 254: allowed endpoint → ``await original_send(...)`` path."""

    async def test_allowed_endpoint_passes_through_send(self) -> None:
        manifest = _manifest(network=["allowed.example.com"])

        captured: dict[str, Any] = {}

        async def _fake_send(self: httpx.AsyncClient, request: Any, *, stream: bool = False, **kw: Any) -> Any:
            captured["host"] = request.url.host
            return httpx.Response(200, request=request, text="ok")

        sandbox = StrategySandbox(_AllowedHttpxStrategy("https://allowed.example.com/ping"), manifest)
        try:
            # Patch *before* evaluation so _activate_restrictions installs our
            # patched send as the original.
            with patch("httpx.AsyncClient.send", _fake_send):
                await sandbox.safe_evaluate(None, None, None)
            # The strategy awaits original_send on the allowed host → line 254.
            assert captured.get("host") == "allowed.example.com"
        finally:
            sandbox.cleanup()


class TestSandboxCleanupRestoresOpen:
    """Lines 386-387: ``cleanup`` restores ``builtins.open`` after activate."""

    def test_cleanup_after_activate_restores_builtin_open(self) -> None:
        sandbox = StrategySandbox(_NoOpStrategy(), _manifest())
        original_open = builtins.open
        sandbox._activate_restrictions()
        try:
            assert sandbox._original_open is original_open
            assert builtins.open is not original_open  # patched
            # Run cleanup() while restrictions are active — this exercises
            # the ``if self._original_open is not None`` branch (lines 385-387)
            # which is the only restore path used by ``cleanup``.
            sandbox.cleanup()
            # Lines 386-387 should have restored builtins.open.
            assert builtins.open is original_open
            assert sandbox._original_open is None
        finally:
            # ``cleanup`` only restores ``builtins.open``; the other patched
            # builtins (object, getattr, io.open, httpx.send) must be reset
            # via ``_deactivate_restrictions`` so we don't leak state into
            # the rest of the pytest session.
            sandbox._deactivate_restrictions()


# ---------------------------------------------------------------------------
# engine/plugins/scoring_executor.py — line 77 (total_weight == 0)
# ---------------------------------------------------------------------------


class TestScoringExecutorZeroWeight:
    def test_zero_total_weight_returns_empty_scores(self) -> None:
        from engine.plugins.scoring_executor import ScoringExecutor
        from nexus_sdk.scoring import (
            FactorDirection,
            IScoringStrategy,
            ScoringFactor,
            ScoringResult,
        )

        class _ZeroWeightStrategy(IScoringStrategy):
            @property
            def id(self) -> str:
                return "zero_weight"

            @property
            def name(self) -> str:
                return "Zero Weight"

            @property
            def version(self) -> str:
                return "0.1.0"

            async def initialize(self, config: StrategyConfig) -> None:
                pass

            async def dispose(self) -> None:
                pass

            async def evaluate(self, _portfolio: Any, _market: MarketState, _costs: Any) -> list[EngineSignal]:
                return []

            def get_config_schema(self) -> dict[str, Any]:
                return {}

            def get_scoring_factors(self) -> list[ScoringFactor]:
                return [
                    ScoringFactor(
                        name="price",
                        weight=0.0,  # zero weight → total_weight == 0
                        direction=FactorDirection.HIGHER_IS_BETTER,
                    ),
                ]

            async def score_universe(
                self, _universe: list[str], _market: MarketState, _costs: Any
            ) -> ScoringResult:
                return ScoringResult(strategy_id=self.id, scores=[])

        executor = ScoringExecutor(_ZeroWeightStrategy(), min_data_points=2)
        raw_data = {
            "AAPL": {"price": 150.0},
            "MSFT": {"price": 300.0},
            "GOOG": {"price": 2800.0},
            "TSLA": {"price": 200.0},
            "NVDA": {"price": 800.0},
            "AMZN": {"price": 3500.0},
            "META": {"price": 500.0},
            "NFLX": {"price": 600.0},
            "INTC": {"price": 50.0},
            "AMD": {"price": 200.0},
        }
        result = executor.compute_scores(list(raw_data.keys()), raw_data)
        assert result.scores == []
        assert "price" in result.excluded_factors or result.excluded_factors == []


# ---------------------------------------------------------------------------
# engine/reference/classification.py — line 67 (industry missing)
# ---------------------------------------------------------------------------


class TestClassificationMissingIndustry:
    def test_industry_not_in_industry_group(self) -> None:
        """Line 67: ``subs is None`` branch returns False."""
        assert not is_valid_gics_path(
            "Information Technology",
            "Software & Services",
            "Nonexistent Industry",
            "Application Software",
        )


# ---------------------------------------------------------------------------
# engine/reference/model.py — lines 146-147 (whitespace validator body)
# ---------------------------------------------------------------------------


class TestModelWhitespaceValidatorBody:
    """Lines 146-147: ``msg = ...`` / ``raise ValueError(msg)``.

    Pydantic's ``_TICKER`` pattern rejects whitespace before the
    field-validator runs, so we call the validator directly to prove
    the body is correct (defense-in-depth for a future pattern change).
    """

    def test_whitespace_only_ticker_rejected_by_validator(self) -> None:
        with pytest.raises(ValueError, match="non-empty and trimmed"):
            RefInstrument._ticker_no_whitespace("   ")

    def test_untrimmed_ticker_rejected_by_validator(self) -> None:
        with pytest.raises(ValueError, match="non-empty and trimmed"):
            RefInstrument._ticker_no_whitespace(" AAPL")

    def test_trailing_newline_ticker_rejected_by_validator(self) -> None:
        with pytest.raises(ValueError, match="non-empty and trimmed"):
            RefInstrument._ticker_no_whitespace("AAPL\n")

    def test_properly_trimmed_ticker_accepted_by_validator(self) -> None:
        assert RefInstrument._ticker_no_whitespace("AAPL") == "AAPL"


class TestListingIsActive:
    """Line 102: ``Listing.is_active`` property."""

    def test_listing_without_active_to_is_active(self) -> None:
        listing = Listing(venue="XNAS", ticker="AAPL", currency="USD", active_from=date(2024, 1, 1))
        assert listing.is_active is True

    def test_listing_with_active_to_is_inactive(self) -> None:
        listing = Listing(
            venue="XNAS",
            ticker="AAPL",
            currency="USD",
            active_from=date(2020, 1, 1),
            active_to=date(2024, 1, 1),
        )
        assert listing.is_active is False


# ---------------------------------------------------------------------------
# engine/reference/resolver.py — lines 124-125, 143, 147
# ---------------------------------------------------------------------------


class TestResolverCoverageGaps:
    def _registered(self) -> Resolver:
        r = Resolver()
        r.register(
            RefInstrument(
                primary_ticker="AAPL",
                primary_venue="XNAS",
                asset_class="equity",
                name="Apple Inc.",
            )
        )
        return r

    def test_non_str_non_dict_query_raises_type_error(self) -> None:
        """Lines 124-125: ``raise TypeError`` for an unsupported query type."""
        r = self._registered()
        with pytest.raises(TypeError, match="must be str or dict"):
            r.resolve(42)  # type: ignore[arg-type]

    def test_dict_with_garbage_ticker_returns_none(self) -> None:
        """Line 143: dict path's ``_looks_garbage`` branch returns None."""
        r = self._registered()
        assert r.resolve({"ticker": "<script>", "venue": "XNAS"}) is None

    def test_empty_dict_returns_none(self) -> None:
        """Line 147: dict with no recognised keys returns None."""
        r = self._registered()
        assert r.resolve({}) is None

    def test_dict_with_only_venue_returns_none(self) -> None:
        """Line 147: dict missing both identifier and ticker falls through."""
        r = self._registered()
        assert r.resolve({"venue": "XNAS"}) is None


# ---------------------------------------------------------------------------
# engine/reference/search.py — lines 57, 100, 117, 131, 187, 193, 227, 254
# ---------------------------------------------------------------------------


def _searchable_index() -> SearchIndex:
    idx = SearchIndex()
    idx.add(
        RefInstrument(
            primary_ticker="AAPL",
            primary_venue="XNAS",
            asset_class="equity",
            name="Apple Inc.",
        )
    )
    idx.add(
        RefInstrument(
            primary_ticker="MSFT",
            primary_venue="XNAS",
            asset_class="equity",
            name="Microsoft Corp.",
        )
    )
    idx.add(
        RefInstrument(
            primary_ticker="BTC",
            primary_venue="XCRY",
            asset_class="crypto",
            name="Bitcoin",
        )
    )
    return idx


class TestSearchCoverageGaps:
    def test_search_returns_empty_for_whitespace_query(self) -> None:
        """Line 57: ``if not query.strip()`` branch."""
        idx = _searchable_index()
        assert idx.search("   ") == []
        assert idx.search("") == []

    def test_suggest_returns_empty_for_oversize_query(self) -> None:
        """Line 100: ``if len(query) > MAX_QUERY_LEN`` branch in suggest."""
        idx = _searchable_index()
        assert idx.suggest("a" * 200) == []

    def test_suggest_fuzzy_skips_other_asset_classes(self) -> None:
        """Line 117: ``continue`` in the fuzzy fallback loop.

        Force the primary tier to find nothing (typo query) and an
        asset_class filter that excludes some records; the fuzzy loop
        must skip the excluded records.
        """
        idx = _searchable_index()
        # "aple" is one edit from "apple" but not a prefix/substring.
        # Filter to crypto so the equity records get skipped in fuzzy.
        results = idx.suggest("aple", asset_class="crypto")
        # No crypto token is within one edit of "aple".
        assert results == []

    def test_suggest_name_exact_match_tier(self) -> None:
        """Line 131: ``if name == q`` → ``(90, rec.name)``."""
        idx = _searchable_index()
        out = idx.suggest("apple inc.")
        assert out
        # Top hit should be AAPL with the name-exact score (90).
        assert out[0].record.primary_ticker == "AAPL"
        assert out[0].score == 90

    def test_score_name_exact_tier(self) -> None:
        """Line 187: ``if name == q: return 90`` in ``_score``."""
        idx = _searchable_index()
        results = idx.search("apple inc.")
        assert results
        assert results[0].primary_ticker == "AAPL"

    def test_score_ticker_contains_tier(self) -> None:
        """Line 193: ``if q in ticker: return 60`` in ``_score``.

        ``"pl"`` is contained in ticker "AAPL" but not as a prefix.
        """
        idx = _searchable_index()
        results = idx.search("pl")
        tickers = [r.primary_ticker for r in results]
        assert "AAPL" in tickers

    def test_within_one_edit_substitution(self) -> None:
        """Line 227: equal-length substitution branch advances both pointers."""
        # "abcd" vs "abcf" → one substitution at index 3.
        assert _within_one_edit("abcd", "abcf") is True
        # Two substitutions must fail.
        assert _within_one_edit("abcd", "abxy") is False

    def test_within_one_edit_identical_strings_short_circuit(self) -> None:
        """Line 227: ``a == b`` short-circuit returns True immediately."""
        assert _within_one_edit("abc", "abc") is True
        assert _within_one_edit("", "") is True

    def test_within_one_edit_trailing_insertion(self) -> None:
        """Line 254: trailing extra char in b is one edit."""
        # "abc" vs "abcd" → one trailing insertion in b.
        assert _within_one_edit("abc", "abcd") is True
        # "abc" vs "abcde" → two trailing insertions, must fail.
        assert _within_one_edit("abc", "abcde") is False
