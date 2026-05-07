"""Targeted tests for uncovered modules — SEV-264 coverage cycle.

Covers:
- engine/tasks/worker.py (run_backtest_task success + failure paths)
- engine/data/providers/oanda.py (get_latest_price, get_multiple_prices, get_orderbook)
- engine/data/providers/polygon.py (get_multiple_prices, get_options_chain)
- engine/observability/logging.py (_resolve_log_path, _build_handler, setup_logging)
- engine/legal/dependencies.py (require_legal_acceptance enforcement)
- engine/db/session.py (init_db migration runner)
"""
from __future__ import annotations

import logging
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from engine.data.providers._cache import ProviderCache
from engine.data.providers.base import FatalProviderError


def _mock_transport(handler) -> httpx.AsyncClient:
    transport = httpx.MockTransport(handler)
    return httpx.AsyncClient(base_url="http://mock", transport=transport)


def _make_cache() -> ProviderCache:
    ProviderCache.reset_for_tests()
    return ProviderCache(url=None)


# ---------- engine/tasks/worker.py ----------


class TestRunBacktestTask:
    @pytest.fixture(autouse=True)
    def _mock_redis_deps(self):
        mock_broker_inst = MagicMock()
        mock_broker_inst.with_result_backend.return_value = mock_broker_inst
        mock_broker_inst.with_middlewares.return_value = mock_broker_inst
        mock_broker_inst.task = lambda: (lambda f: f)

        mods_to_remove = [k for k in sys.modules if k.startswith("engine.tasks")]
        saved = {m: sys.modules.pop(m) for m in mods_to_remove}

        with (
            patch("engine.tasks.worker.ListQueueBroker", return_value=mock_broker_inst),
            patch("engine.tasks.worker.RedisAsyncResultBackend"),
            patch("engine.tasks.worker.CorrelationMiddleware"),
            patch("engine.tasks.worker.TaskiqScheduler"),
        ):
            yield

        for m in mods_to_remove:
            sys.modules.pop(m, None)
        sys.modules.update(saved)

    async def test_run_backtest_task_success(self):
        mock_result = MagicMock()
        mock_result.trades = [{"action": "BUY", "qty": 10}]
        mock_result.total_return_pct = 15.3
        mock_result.final_capital = 115_300.0
        mock_result.metrics = {"sharpe": 1.2}
        mock_result.equity_curve = [100_000, 115_300]

        mock_runner = AsyncMock()
        mock_runner.run.return_value = mock_result

        mock_strategy = MagicMock()

        mock_registry = MagicMock()
        mock_registry.load_strategy.return_value = mock_strategy

        mock_provider = MagicMock()

        with (
            patch("engine.core.backtest_runner.BacktestRunner", return_value=mock_runner),
            patch("engine.core.backtest_runner.BacktestConfig"),
            patch("engine.data.feeds.get_data_provider", return_value=mock_provider),
            patch("engine.plugins.registry.PluginRegistry", return_value=mock_registry),
        ):
            from engine.tasks.worker import run_backtest_task

            result = await run_backtest_task(
                strategy_name="sma_crossover",
                symbol="AAPL",
                start_date="2025-01-01",
                end_date="2025-12-31",
                initial_capital=100_000.0,
            )

        assert result["status"] == "completed"
        assert result["strategy_name"] == "sma_crossover"
        assert result["symbol"] == "AAPL"
        assert result["total_trades"] == 1
        assert result["total_return_pct"] == 15.3
        assert result["final_capital"] == 115_300.0

    async def test_run_backtest_task_strategy_not_found(self):
        mock_registry = MagicMock()
        mock_registry.load_strategy.return_value = None

        mock_provider = MagicMock()

        with (
            patch("engine.data.feeds.get_data_provider", return_value=mock_provider),
            patch("engine.plugins.registry.PluginRegistry", return_value=mock_registry),
        ):
            from engine.tasks.worker import run_backtest_task

            result = await run_backtest_task(
                strategy_name="nonexistent",
                symbol="AAPL",
                start_date="2025-01-01",
                end_date="2025-12-31",
            )

        assert result["status"] == "failed"
        assert result["error_type"] == "ValueError"
        assert "not found" in result["error"]

    async def test_run_backtest_task_generic_exception(self):
        mock_registry = MagicMock()
        mock_registry.load_strategy.side_effect = RuntimeError("boom")

        mock_provider = MagicMock()

        with (
            patch("engine.data.feeds.get_data_provider", return_value=mock_provider),
            patch("engine.plugins.registry.PluginRegistry", return_value=mock_registry),
        ):
            from engine.tasks.worker import run_backtest_task

            result = await run_backtest_task(
                strategy_name="sma",
                symbol="AAPL",
                start_date="2025-01-01",
                end_date="2025-12-31",
            )

        assert result["status"] == "failed"
        assert result["error"] == "boom"
        assert result["error_type"] == "RuntimeError"


# ---------- engine/data/providers/oanda.py ----------


class TestOandaCoverageGaps:
    @pytest.mark.asyncio
    async def test_get_latest_price_with_data(self):
        from engine.data.providers.oanda import OandaDataProvider

        payload = {
            "candles": [
                {
                    "time": "2026-01-01T00:00:00.000000000Z",
                    "complete": True,
                    "volume": 100,
                    "mid": {"o": "1.10", "h": "1.11", "l": "1.09", "c": "1.105"},
                },
                {
                    "time": "2026-01-02T00:00:00.000000000Z",
                    "complete": True,
                    "volume": 200,
                    "mid": {"o": "1.105", "h": "1.12", "l": "1.10", "c": "1.115"},
                },
            ]
        }

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=payload)

        cache = _make_cache()
        async with _mock_transport(handler) as client:
            provider = OandaDataProvider(api_key="t", client=client, cache=cache)
            price = await provider.get_latest_price("EUR_USD")
        assert price == 1.115

    @pytest.mark.asyncio
    async def test_get_latest_price_empty_fallback_then_none(self):
        from engine.data.providers.oanda import OandaDataProvider

        call_count = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            return httpx.Response(200, json={"candles": []})

        cache = _make_cache()
        async with _mock_transport(handler) as client:
            provider = OandaDataProvider(api_key="t", client=client, cache=cache)
            price = await provider.get_latest_price("EUR_USD")
        assert price is None
        assert call_count["n"] == 2

    @pytest.mark.asyncio
    async def test_get_multiple_prices_normal(self):
        from engine.data.providers.oanda import OandaDataProvider

        payload = {
            "candles": [
                {
                    "time": "2026-01-01T00:00:00.000000000Z",
                    "complete": True,
                    "volume": 50,
                    "mid": {"o": "1.10", "h": "1.11", "l": "1.09", "c": "1.10"},
                },
            ]
        }

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=payload)

        cache = _make_cache()
        async with _mock_transport(handler) as client:
            provider = OandaDataProvider(api_key="t", client=client, cache=cache)
            result = await provider.get_multiple_prices(["EUR_USD", "GBP_USD"])
        assert "EUR_USD" in result
        assert "GBP_USD" in result

    @pytest.mark.asyncio
    async def test_get_multiple_prices_skips_errors(self):
        from engine.data.providers.oanda import OandaDataProvider

        call_count = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            if "EUR" in request.url.path:
                return httpx.Response(
                    200,
                    json={
                        "candles": [
                            {
                                "time": "2026-01-01T00:00:00Z",
                                "complete": True,
                                "volume": 10,
                                "mid": {"o": "1.1", "h": "1.2", "l": "1.0", "c": "1.15"},
                            }
                        ]
                    },
                )
            return httpx.Response(401, text="bad")

        cache = _make_cache()
        async with _mock_transport(handler) as client:
            provider = OandaDataProvider(api_key="t", client=client, cache=cache)
            result = await provider.get_multiple_prices(["EUR_USD", "GBP_USD"])
        assert "EUR_USD" in result
        assert "GBP_USD" not in result

    @pytest.mark.asyncio
    async def test_get_orderbook_with_data(self):
        from engine.data.providers.oanda import OandaDataProvider

        payload = {
            "orderBook": {
                "buckets": [
                    {"price": "1.10", "longCountPercent": 40.0, "shortCountPercent": 60.0},
                    {"price": "1.11", "longCountPercent": 55.0, "shortCountPercent": 45.0},
                ]
            }
        }

        def handler(request: httpx.Request) -> httpx.Response:
            assert "orderBook" in request.url.path
            return httpx.Response(200, json=payload)

        cache = _make_cache()
        async with _mock_transport(handler) as client:
            provider = OandaDataProvider(api_key="t", client=client, cache=cache)
            df = await provider.get_orderbook("EUR_USD", depth=10)
        assert len(df) == 2
        assert list(df.columns) == ["price", "long_pct", "short_pct"]
        assert df["price"].iloc[0] == 1.10

    @pytest.mark.asyncio
    async def test_get_orderbook_empty(self):
        from engine.data.providers.oanda import OandaDataProvider

        payload = {}

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=payload)

        cache = _make_cache()
        async with _mock_transport(handler) as client:
            provider = OandaDataProvider(api_key="t", client=client, cache=cache)
            df = await provider.get_orderbook("EUR_USD")
        assert len(df) == 0

    @pytest.mark.asyncio
    async def test_get_orderbook_respects_depth(self):
        from engine.data.providers.oanda import OandaDataProvider

        buckets = [
            {"price": str(1.10 + i * 0.01), "longCountPercent": 50.0, "shortCountPercent": 50.0}
            for i in range(30)
        ]
        payload = {"orderBook": {"buckets": buckets}}

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=payload)

        cache = _make_cache()
        async with _mock_transport(handler) as client:
            provider = OandaDataProvider(api_key="t", client=client, cache=cache)
            df = await provider.get_orderbook("EUR_USD", depth=5)
        assert len(df) == 5

    @pytest.mark.asyncio
    async def test_parse_candles_empty_payload(self):
        from engine.data.providers.oanda import OandaDataProvider

        df = OandaDataProvider._parse_candles({})
        assert len(df) == 0

    @pytest.mark.asyncio
    async def test_parse_candles_none_payload(self):
        from engine.data.providers.oanda import OandaDataProvider

        df = OandaDataProvider._parse_candles(None)
        assert len(df) == 0

    @pytest.mark.asyncio
    async def test_get_ohlcv_invalid_interval(self):
        from engine.data.providers.oanda import OandaDataProvider

        cache = _make_cache()
        async with _mock_transport(lambda r: httpx.Response(200, json={})) as client:
            provider = OandaDataProvider(api_key="t", client=client, cache=cache)
            with pytest.raises(FatalProviderError, match="invalid interval"):
                await provider.get_ohlcv("EUR_USD", interval="1tick")

    @pytest.mark.asyncio
    async def test_get_ohlcv_invalid_period(self):
        from engine.data.providers.oanda import OandaDataProvider

        cache = _make_cache()
        async with _mock_transport(lambda r: httpx.Response(200, json={})) as client:
            provider = OandaDataProvider(api_key="t", client=client, cache=cache)
            with pytest.raises(FatalProviderError, match="invalid period"):
                await provider.get_ohlcv("EUR_USD", period="99y")

    @pytest.mark.asyncio
    async def test_health_check(self):
        from engine.data.providers.oanda import OandaDataProvider

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"accounts": []})

        cache = _make_cache()
        async with _mock_transport(handler) as client:
            provider = OandaDataProvider(api_key="t", client=client, cache=cache)
            result = await provider.health_check()
        assert result.status.value == "up"

    @pytest.mark.asyncio
    async def test_live_environment(self):
        from engine.data.providers.oanda import OandaDataProvider

        cache = _make_cache()
        async with _mock_transport(lambda r: httpx.Response(200, json={})) as client:
            provider = OandaDataProvider(api_key="k", environment="live", client=client, cache=cache)
            assert "fxtrade" in provider._base_url


# ---------- engine/data/providers/polygon.py ----------


class TestPolygonCoverageGaps:
    @pytest.mark.asyncio
    async def test_get_multiple_prices_normal(self):
        from engine.data.providers.polygon import PolygonDataProvider

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"results": {"p": 150.0}})

        cache = _make_cache()
        async with _mock_transport(handler) as client:
            provider = PolygonDataProvider(api_key="x", client=client, cache=cache)
            result = await provider.get_multiple_prices(["AAPL", "MSFT"])
        assert result == {"AAPL": 150.0, "MSFT": 150.0}

    @pytest.mark.asyncio
    async def test_get_multiple_prices_skips_errors(self):
        from engine.data.providers.polygon import PolygonDataProvider

        def handler(request: httpx.Request) -> httpx.Response:
            if "AAPL" in request.url.path:
                return httpx.Response(200, json={"results": {"p": 150.0}})
            return httpx.Response(401, text="bad")

        cache = _make_cache()
        async with _mock_transport(handler) as client:
            provider = PolygonDataProvider(api_key="x", client=client, cache=cache)
            result = await provider.get_multiple_prices(["AAPL", "MSFT"])
        assert "AAPL" in result
        assert "MSFT" not in result

    @pytest.mark.asyncio
    async def test_get_options_chain_with_data(self):
        from engine.data.providers.polygon import PolygonDataProvider

        payload = {
            "results": [
                {"ticker": "O:AAPL260117C00150000", "strike": 150.0, "expiry": "2026-01-17"},
                {"ticker": "O:AAPL260117P00150000", "strike": 150.0, "expiry": "2026-01-17"},
            ]
        }

        def handler(request: httpx.Request) -> httpx.Response:
            assert "options" in request.url.path
            return httpx.Response(200, json=payload)

        cache = _make_cache()
        async with _mock_transport(handler) as client:
            provider = PolygonDataProvider(api_key="x", client=client, cache=cache)
            df = await provider.get_options_chain("AAPL")
        assert len(df) == 2

    @pytest.mark.asyncio
    async def test_get_options_chain_with_expiry(self):
        from engine.data.providers.polygon import PolygonDataProvider

        payload = {"results": [{"ticker": "O:AAPL260117C00150000"}]}

        def handler(request: httpx.Request) -> httpx.Response:
            assert "expiration_date" in str(request.url)
            return httpx.Response(200, json=payload)

        cache = _make_cache()
        async with _mock_transport(handler) as client:
            provider = PolygonDataProvider(api_key="x", client=client, cache=cache)
            df = await provider.get_options_chain("AAPL", expiry="2026-01-17")
        assert len(df) == 1

    @pytest.mark.asyncio
    async def test_get_options_chain_empty(self):
        from engine.data.providers.polygon import PolygonDataProvider

        payload = {"results": []}

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=payload)

        cache = _make_cache()
        async with _mock_transport(handler) as client:
            provider = PolygonDataProvider(api_key="x", client=client, cache=cache)
            df = await provider.get_options_chain("AAPL")
        assert len(df) == 0

    @pytest.mark.asyncio
    async def test_get_ohlcv_invalid_interval(self):
        from engine.data.providers.polygon import PolygonDataProvider

        cache = _make_cache()
        async with _mock_transport(lambda r: httpx.Response(200, json={})) as client:
            provider = PolygonDataProvider(api_key="x", client=client, cache=cache)
            with pytest.raises(FatalProviderError, match="invalid interval"):
                await provider.get_ohlcv("AAPL", interval="2h")

    @pytest.mark.asyncio
    async def test_get_ohlcv_invalid_period(self):
        from engine.data.providers.polygon import PolygonDataProvider

        cache = _make_cache()
        async with _mock_transport(lambda r: httpx.Response(200, json={})) as client:
            provider = PolygonDataProvider(api_key="x", client=client, cache=cache)
            with pytest.raises(FatalProviderError, match="invalid period"):
                await provider.get_ohlcv("AAPL", period="10y")

    @pytest.mark.asyncio
    async def test_get_latest_price_none_result(self):
        from engine.data.providers.polygon import PolygonDataProvider

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"results": None})

        cache = _make_cache()
        async with _mock_transport(handler) as client:
            provider = PolygonDataProvider(api_key="x", client=client, cache=cache)
            price = await provider.get_latest_price("AAPL")
        assert price is None

    @pytest.mark.asyncio
    async def test_get_latest_price_string_price(self):
        from engine.data.providers.polygon import PolygonDataProvider

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"results": {"p": "not_a_number"}})

        cache = _make_cache()
        async with _mock_transport(handler) as client:
            provider = PolygonDataProvider(api_key="x", client=client, cache=cache)
            price = await provider.get_latest_price("AAPL")
        assert price is None

    @pytest.mark.asyncio
    async def test_parse_aggs_empty(self):
        from engine.data.providers.polygon import PolygonDataProvider

        df = PolygonDataProvider._parse_aggs({"results": []})
        assert len(df) == 0

    @pytest.mark.asyncio
    async def test_parse_aggs_none_results(self):
        from engine.data.providers.polygon import PolygonDataProvider

        df = PolygonDataProvider._parse_aggs({})
        assert len(df) == 0

    @pytest.mark.asyncio
    async def test_health_check(self):
        from engine.data.providers.polygon import PolygonDataProvider

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[])

        cache = _make_cache()
        async with _mock_transport(handler) as client:
            provider = PolygonDataProvider(api_key="x", client=client, cache=cache)
            result = await provider.health_check()
        assert result.status.value == "up"


# ---------- engine/observability/logging.py ----------


class TestLoggingCoverage:
    def test_resolve_log_path_relative(self):
        from engine.observability.logging import _resolve_log_path

        result = _resolve_log_path("logs/app.log")
        assert result.is_absolute()
        assert str(result).endswith("logs/app.log")

    def test_resolve_log_path_rejects_path_traversal(self):
        from engine.observability.logging import _resolve_log_path

        with pytest.raises(ValueError, match="must resolve under"):
            _resolve_log_path("../../etc/cron.d/malicious")

    def test_resolve_log_path_rejects_absolute_outside_cwd(self):
        from engine.observability.logging import _resolve_log_path

        with pytest.raises(ValueError, match="must resolve under"):
            _resolve_log_path("/etc/passwd")

    def test_build_handler_stdout_sink(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        from engine.config import settings

        monkeypatch.setattr(settings, "log_sink", "stdout")
        monkeypatch.setattr(settings, "log_file_path", "logs/app.log")

        from engine.observability.logging import _build_handler

        handler = _build_handler()
        assert isinstance(handler, logging.StreamHandler)

    def test_build_handler_file_sink(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        from engine.config import settings

        monkeypatch.setattr(settings, "log_sink", "file")
        monkeypatch.setattr(settings, "log_file_path", "logs/test.log")

        from engine.observability.logging import _build_handler

        handler = _build_handler()
        assert isinstance(handler, logging.handlers.WatchedFileHandler)
        assert (tmp_path / "logs").is_dir()

    def test_build_handler_otlp_falls_back_to_stdout(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        from engine.config import settings

        monkeypatch.setattr(settings, "log_sink", "otlp")
        monkeypatch.setattr(settings, "log_file_path", "logs/app.log")

        from engine.observability.logging import _build_handler

        handler = _build_handler()
        assert isinstance(handler, logging.StreamHandler)

    def test_setup_logging_json_format(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)

        from engine.config import settings

        monkeypatch.setattr(settings, "log_sink", "stdout")
        monkeypatch.setattr(settings, "log_file_path", "logs/app.log")
        monkeypatch.setattr(settings, "log_format", "json")
        monkeypatch.setattr(settings, "log_level", "INFO")
        monkeypatch.setattr(settings, "app_env", "development")

        from engine.observability.logging import setup_logging

        setup_logging()

        root = logging.getLogger()
        assert root.level == logging.INFO
        assert len(root.handlers) == 1

    def test_setup_logging_console_format(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)

        from engine.config import settings

        monkeypatch.setattr(settings, "log_sink", "stdout")
        monkeypatch.setattr(settings, "log_file_path", "logs/app.log")
        monkeypatch.setattr(settings, "log_format", "console")
        monkeypatch.setattr(settings, "log_level", "DEBUG")
        monkeypatch.setattr(settings, "app_env", "development")

        from engine.observability.logging import setup_logging

        setup_logging()

        root = logging.getLogger()
        assert root.level == logging.DEBUG

    def test_setup_logging_production_forces_json(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)

        from engine.config import settings

        monkeypatch.setattr(settings, "log_sink", "stdout")
        monkeypatch.setattr(settings, "log_file_path", "logs/app.log")
        monkeypatch.setattr(settings, "log_format", "console")
        monkeypatch.setattr(settings, "log_level", "WARNING")
        monkeypatch.setattr(settings, "app_env", "production")

        from engine.observability.logging import setup_logging

        setup_logging()

        root = logging.getLogger()
        assert root.level == logging.WARNING


# ---------- engine/legal/dependencies.py ----------


class TestLegalDependencies:
    @pytest.mark.asyncio
    async def test_require_legal_acceptance_no_user_passes(self):
        from engine.legal.dependencies import require_legal_acceptance

        result = await require_legal_acceptance(db=MagicMock())
        assert result is None

    @pytest.mark.asyncio
    async def test_require_legal_acceptance_with_pending(self, monkeypatch):
        from engine.legal import dependencies

        monkeypatch.setattr(dependencies, "_placeholder_user_id", "12345678-1234-5678-1234-567812345678")

        mock_doc = MagicMock()
        mock_doc.slug = "terms-of-service"

        with patch("engine.legal.dependencies.legal_service") as mock_svc:
            mock_svc.get_pending_acceptances = AsyncMock(return_value=[mock_doc])

            with pytest.raises(Exception) as exc_info:
                await dependencies.require_legal_acceptance(db=MagicMock())

            assert exc_info.value.status_code == 451
            assert "legal_re_acceptance_required" in str(exc_info.value.detail)

        monkeypatch.setattr(dependencies, "_placeholder_user_id", None)

    @pytest.mark.asyncio
    async def test_require_legal_acceptance_no_pending(self, monkeypatch):
        from engine.legal import dependencies

        monkeypatch.setattr(dependencies, "_placeholder_user_id", "12345678-1234-5678-1234-567812345678")

        with patch("engine.legal.dependencies.legal_service") as mock_svc:
            mock_svc.get_pending_acceptances = AsyncMock(return_value=[])

            result = await dependencies.require_legal_acceptance(db=MagicMock())
            assert result is None

        monkeypatch.setattr(dependencies, "_placeholder_user_id", None)


# ---------- engine/db/session.py ----------


class TestDbSession:
    async def test_init_db_runs_migrations(self, monkeypatch):
        mock_upgrade = MagicMock()
        mock_config_inst = MagicMock()

        def fake_config(path):
            assert path == "alembic.ini"
            return mock_config_inst

        monkeypatch.setattr("alembic.config.Config", fake_config)
        monkeypatch.setattr("alembic.command.upgrade", mock_upgrade)

        from engine.db.session import init_db

        await init_db()

        mock_upgrade.assert_called_once_with(mock_config_inst, "head")

    def test_get_engine_creates_engine(self, monkeypatch):
        from engine.db import session as session_mod

        monkeypatch.setattr(session_mod, "_engine", None)

        from engine.config import settings

        monkeypatch.setattr(settings, "database_url", "sqlite+aiosqlite:///test.db")
        monkeypatch.setattr(settings, "database_pool_size", 5)
        monkeypatch.setattr(settings, "database_max_overflow", 10)
        monkeypatch.setattr(settings, "app_debug", False)

        engine = session_mod.get_engine()
        assert engine is not None

        monkeypatch.setattr(session_mod, "_engine", None)

    def test_get_session_factory_creates_factory(self, monkeypatch):
        from engine.db import session as session_mod

        monkeypatch.setattr(session_mod, "_engine", None)
        monkeypatch.setattr(session_mod, "_session_factory", None)

        from engine.config import settings

        monkeypatch.setattr(settings, "database_url", "sqlite+aiosqlite:///test.db")
        monkeypatch.setattr(settings, "database_pool_size", 5)
        monkeypatch.setattr(settings, "database_max_overflow", 10)
        monkeypatch.setattr(settings, "app_debug", False)

        factory = session_mod.get_session_factory()
        assert factory is not None

        monkeypatch.setattr(session_mod, "_engine", None)
        monkeypatch.setattr(session_mod, "_session_factory", None)

    async def test_dispose_engine_cleans_up(self, monkeypatch):
        from engine.db import session as session_mod

        mock_engine = AsyncMock()
        monkeypatch.setattr(session_mod, "_engine", mock_engine)
        monkeypatch.setattr(session_mod, "_session_factory", MagicMock())

        await session_mod.dispose_engine()

        mock_engine.dispose.assert_called_once()
        assert session_mod._engine is None
        assert session_mod._session_factory is None
