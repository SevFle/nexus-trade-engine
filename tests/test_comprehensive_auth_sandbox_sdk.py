"""Comprehensive tests for get_authorize_url, sandbox internals, and SDK MarketState.

Focus areas:
  1) Google/GitHub get_authorize_url with/without state param
  2) StrategySandbox _work_dir and error message patterns
  3) MarketState.get_news hours parameter and edge cases
"""

from __future__ import annotations

import os
import tempfile

import pytest

from engine.api.auth.github_oauth import GitHubAuthProvider
from engine.api.auth.google import GoogleAuthProvider
from engine.config import Settings
from engine.core.signal import Signal
from engine.plugins.manifest import StrategyManifest
from engine.plugins.sandbox import StrategySandbox
from nexus_sdk.strategy import MarketState

# ═══════════════════════════════════════════════════════════════════════════════
# 1. Google/GitHub get_authorize_url with state param
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def google_provider():
    return GoogleAuthProvider()


@pytest.fixture
def github_provider():
    return GitHubAuthProvider()


@pytest.fixture
def google_settings(monkeypatch):
    s = Settings(
        google_client_id="test-google-id",
        google_client_secret="test-google-secret",
        google_redirect_uri="https://app.example.com/google/callback",
    )
    monkeypatch.setattr("engine.api.auth.google.settings", s)
    return s


@pytest.fixture
def github_settings(monkeypatch):
    s = Settings(
        github_client_id="test-github-id",
        github_client_secret="test-github-secret",
        github_redirect_uri="https://app.example.com/github/callback",
    )
    monkeypatch.setattr("engine.api.auth.github_oauth.settings", s)
    return s


class TestGoogleAuthorizeUrl:
    def test_get_authorize_url_without_state(self, google_provider, google_settings):
        url = google_provider.get_authorize_url()
        assert "accounts.google.com/o/oauth2/v2/auth" in url
        assert "client_id=test-google-id" in url
        assert "redirect_uri=https://app.example.com/google/callback" in url
        assert "response_type=code" in url
        assert "scope=openid email profile" in url
        assert "state=" not in url

    def test_get_authorize_url_with_state(self, google_provider, google_settings):
        url = google_provider.get_authorize_url(state="random-csrf-state")
        assert "state=random-csrf-state" in url
        assert "accounts.google.com" in url

    def test_get_authorize_url_with_empty_state(
        self, google_provider, google_settings
    ):
        url = google_provider.get_authorize_url(state="")
        assert "state=" not in url

    def test_name_property(self, google_provider):
        assert google_provider.name == "google"


class TestGitHubAuthorizeUrl:
    def test_get_authorize_url_without_state(self, github_provider, github_settings):
        url = github_provider.get_authorize_url()
        assert "github.com/login/oauth/authorize" in url
        assert "client_id=test-github-id" in url
        assert "redirect_uri=https://app.example.com/github/callback" in url
        assert "scope=user:email" in url
        assert "state=" not in url

    def test_get_authorize_url_with_state(self, github_provider, github_settings):
        url = github_provider.get_authorize_url(state="csrf-token-123")
        assert "state=csrf-token-123" in url
        assert "github.com/login/oauth/authorize" in url

    def test_get_authorize_url_with_empty_state(
        self, github_provider, github_settings
    ):
        url = github_provider.get_authorize_url(state="")
        assert "state=" not in url

    def test_name_property(self, github_provider):
        assert github_provider.name == "github"


# ═══════════════════════════════════════════════════════════════════════════════
# 2. StrategySandbox _work_dir and error message patterns
# ═══════════════════════════════════════════════════════════════════════════════


class _SimpleStrategy:
    name = "simple"
    version = "1.0.0"

    def on_bar(self, _state, _portfolio):
        return [Signal.buy(symbol="AAPL", strategy_id=self.name)]


@pytest.fixture
def manifest():
    return StrategyManifest(
        id="test",
        name="test",
        version="1.0.0",
        resources={"max_cpu_seconds": 1},
    )


class TestSandboxWorkDir:
    def test_work_dir_created_on_init(self, manifest):
        sandbox = StrategySandbox(_SimpleStrategy(), manifest)
        try:
            assert sandbox._work_dir is not None
            assert os.path.isdir(sandbox._work_dir)
            assert "strategy_sandbox_" in sandbox._work_dir
        finally:
            sandbox.cleanup()

    def test_work_dir_is_temp_directory(self, manifest):
        sandbox = StrategySandbox(_SimpleStrategy(), manifest)
        try:
            assert sandbox._work_dir is not None
            tmp_root = tempfile.gettempdir()
            assert sandbox._work_dir.startswith(tmp_root)
        finally:
            sandbox.cleanup()

    def test_cleanup_removes_work_dir(self, manifest):
        sandbox = StrategySandbox(_SimpleStrategy(), manifest)
        work_dir = sandbox._work_dir
        assert work_dir is not None
        assert os.path.isdir(work_dir)
        sandbox.cleanup()
        assert not os.path.isdir(work_dir)

    def test_cleanup_sets_work_dir_to_none(self, manifest):
        sandbox = StrategySandbox(_SimpleStrategy(), manifest)
        assert sandbox._work_dir is not None
        sandbox.cleanup()
        assert sandbox._work_dir is None

    def test_cleanup_idempotent(self, manifest):
        sandbox = StrategySandbox(_SimpleStrategy(), manifest)
        sandbox.cleanup()
        sandbox.cleanup()
        assert sandbox._work_dir is None

    @pytest.mark.asyncio
    async def test_work_dir_survives_evaluation(self, manifest):
        sandbox = StrategySandbox(_SimpleStrategy(), manifest)
        try:
            work_dir = sandbox._work_dir
            await sandbox.safe_evaluate(None, None, None)
            assert sandbox._work_dir == work_dir
            assert os.path.isdir(sandbox._work_dir)
        finally:
            sandbox.cleanup()


class TestSandboxErrorPatterns:
    @pytest.mark.asyncio
    async def test_import_error_contains_blocked(self, manifest):
        class _ImportOs:
            name = "import_os_err"
            version = "1.0.0"

            def on_bar(self, _s, _p):
                import os  # noqa: F401
                return []

        sandbox = StrategySandbox(_ImportOs(), manifest)
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert signals == []
            assert sandbox.metrics.errors == 1
            assert "blocked" in (sandbox.metrics.last_error or "").lower()
        finally:
            sandbox.cleanup()

    @pytest.mark.asyncio
    async def test_runtime_error_in_strategy(self, manifest):
        class _Crash:
            name = "crash"
            version = "1.0.0"

            def on_bar(self, _s, _p):
                raise RuntimeError("strategy blew up")

        sandbox = StrategySandbox(_Crash(), manifest)
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert signals == []
            assert sandbox.metrics.errors == 1
            assert "blew up" in (sandbox.metrics.last_error or "")
        finally:
            sandbox.cleanup()

    @pytest.mark.asyncio
    async def test_file_permission_error_message(self, manifest, tmp_path):
        secret = tmp_path / "secret.txt"
        secret.write_text("sensitive")

        class _FileRead:
            name = "file_read_err"
            version = "1.0.0"

            def __init__(self, path):
                self._path = path

            def on_bar(self, _s, _p):
                with open(self._path) as f:
                    f.read()
                return []

        sandbox = StrategySandbox(_FileRead(str(secret)), manifest)
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert signals == []
            assert sandbox.metrics.errors == 1
            assert "not allowed" in (sandbox.metrics.last_error or "").lower()
        finally:
            sandbox.cleanup()

    @pytest.mark.asyncio
    async def test_write_permission_error_message(self, manifest):
        class _FileWrite:
            name = "file_write_err"
            version = "1.0.0"

            def on_bar(self, _s, _p):
                with open("/tmp/sandbox_write_test", "w") as f:
                    f.write("pwned")
                return []

        sandbox = StrategySandbox(_FileWrite(), manifest)
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert signals == []
            assert sandbox.metrics.errors == 1
            assert "not allowed" in (sandbox.metrics.last_error or "").lower()
        finally:
            sandbox.cleanup()

    @pytest.mark.asyncio
    async def test_file_descriptor_error_message(self, manifest):
        import builtins

        class _FdAccess:
            name = "fd_access_err"
            version = "1.0.0"

            def on_bar(self, _s, _p):
                with builtins.open(0):
                    pass
                return []

        sandbox = StrategySandbox(_FdAccess(), manifest)
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert signals == []
            assert sandbox.metrics.errors == 1
            assert (
                "file descriptor" in (sandbox.metrics.last_error or "").lower()
                or "not allowed" in (sandbox.metrics.last_error or "").lower()
            )
        finally:
            sandbox.cleanup()


class TestSandboxMetricsOnError:
    @pytest.mark.asyncio
    async def test_error_count_increments(self, manifest):
        class _Bad:
            name = "bad_metrics"
            version = "1.0.0"

            def on_bar(self, _s, _p):
                raise ValueError("boom")

        sandbox = StrategySandbox(_Bad(), manifest)
        try:
            await sandbox.safe_evaluate(None, None, None)
            assert sandbox.metrics.errors == 1
            assert sandbox.metrics.last_error == "boom"

            await sandbox.safe_evaluate(None, None, None)
            assert sandbox.metrics.errors == 2
        finally:
            sandbox.cleanup()

    @pytest.mark.asyncio
    async def test_metrics_no_error_on_success(self, manifest):
        sandbox = StrategySandbox(_SimpleStrategy(), manifest)
        try:
            await sandbox.safe_evaluate(None, None, None)
            assert sandbox.metrics.errors == 0
            assert sandbox.metrics.last_error is None
            assert sandbox.metrics.total_evaluations == 1
            assert sandbox.metrics.total_signals_emitted == 1
        finally:
            sandbox.cleanup()


# ═══════════════════════════════════════════════════════════════════════════════
# 3. MarketState.get_news hours parameter and edge cases
# ═══════════════════════════════════════════════════════════════════════════════


class TestMarketStateGetNews:
    def test_get_news_returns_all_news_default(self):
        news = [
            {"headline": "Market rallies", "source": "reuters"},
            {"headline": "Fed holds rates", "source": "bloomberg"},
        ]
        state = MarketState(news=news)
        result = state.get_news()
        assert result == news
        assert len(result) == 2

    def test_get_news_with_hours_returns_all_news(self):
        news = [{"headline": "A"}]
        state = MarketState(news=news)
        result = state.get_news(hours=48)
        assert result == news

    def test_get_news_with_hours_zero(self):
        news = [{"headline": "A"}]
        state = MarketState(news=news)
        result = state.get_news(hours=0)
        assert result == news

    def test_get_news_with_negative_hours(self):
        news = [{"headline": "A"}]
        state = MarketState(news=news)
        result = state.get_news(hours=-1)
        assert result == news

    def test_get_news_with_large_hours(self):
        news = [{"headline": "A"}]
        state = MarketState(news=news)
        result = state.get_news(hours=8760)
        assert result == news

    def test_get_news_empty_returns_empty_list(self):
        state = MarketState()
        assert state.get_news() == []

    def test_get_news_preserves_order(self):
        news = [
            {"headline": "First"},
            {"headline": "Second"},
            {"headline": "Third"},
        ]
        state = MarketState(news=news)
        result = state.get_news()
        assert [n["headline"] for n in result] == ["First", "Second", "Third"]

    def test_get_news_preserves_all_fields(self):
        news = [
            {
                "headline": "Breaking",
                "sentiment": 0.8,
                "source": "reuters",
                "url": "https://example.com",
                "timestamp": "2025-01-01T00:00:00Z",
            }
        ]
        state = MarketState(news=news)
        result = state.get_news()
        assert result[0]["headline"] == "Breaking"
        assert result[0]["sentiment"] == 0.8
        assert result[0]["source"] == "reuters"
        assert result[0]["url"] == "https://example.com"
        assert result[0]["timestamp"] == "2025-01-01T00:00:00Z"


class TestMarketStateAdditionalIndicators:
    def test_sma_with_single_bar(self):
        bars = [{"close": 42.0}]
        state = MarketState(ohlcv={"AAPL": bars})
        result = state.sma("AAPL", period=1)
        assert result == 42.0

    def test_std_with_single_bar(self):
        bars = [{"close": 42.0}]
        state = MarketState(ohlcv={"AAPL": bars})
        result = state.std("AAPL", period=1)
        assert result == 0.0

    def test_latest_returns_none_for_empty_prices(self):
        state = MarketState()
        assert state.latest("AAPL") is None

    def test_get_macro_indicators_returns_copy(self):
        macro = {"gdp": 2.5, "inflation": 3.1}
        state = MarketState(macro=macro)
        result = state.get_macro_indicators()
        assert result == macro
        assert result is state.macro

    def test_market_state_all_default_fields(self):
        state = MarketState()
        assert state.timestamp is None
        assert state.prices == {}
        assert state.volumes == {}
        assert state.ohlcv == {}
        assert state.news == []
        assert state.sentiment == {}
        assert state.macro == {}
        assert state.order_book == {}

    def test_market_state_with_all_fields(self):
        state = MarketState(
            timestamp="2025-01-01T00:00:00Z",
            prices={"AAPL": 150.0},
            volumes={"AAPL": 1000000},
            ohlcv={"AAPL": [{"close": 150.0}]},
            news=[{"headline": "Test"}],
            sentiment={"AAPL": 0.75},
            macro={"gdp": 2.5},
            order_book={"AAPL": {"bids": [], "asks": []}},
        )
        assert state.timestamp == "2025-01-01T00:00:00Z"
        assert state.prices["AAPL"] == 150.0
        assert state.volumes["AAPL"] == 1000000
        assert len(state.ohlcv["AAPL"]) == 1
        assert len(state.news) == 1
        assert state.sentiment["AAPL"] == 0.75
        assert state.macro["gdp"] == 2.5
        assert "bids" in state.order_book["AAPL"]
