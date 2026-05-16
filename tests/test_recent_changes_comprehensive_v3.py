"""Comprehensive tests for the most recently changed code.

Targets coverage gaps in:
  - engine/plugins/sandbox/core/policy.py  (_parse_memory, ImportPolicy.is_allowed,
    NetworkPolicy.is_host_allowed, SandboxPolicy.from_manifest edge cases)
  - engine/plugins/sandbox/core/violation.py  (all violation to_dict methods)
  - engine/plugins/sandbox/monitoring/violation_report.py  (ViolationReport)
  - engine/plugins/sandbox/monitoring/event_logger.py  (SecurityEventLogger.log_event)
  - engine/plugins/sandbox/monitoring/metrics.py  (SandboxMetricsCollector.get_or_create)
  - engine/plugins/sandbox/layers/resource_limiter.py  (_CPUTimer, cpu_elapsed,
    check_cpu_timer)
  - engine/core/backtest_runner.py  (BacktestRunner error paths, _apply_strategy_params,
    BacktestResult/BacktestConfig defaults)
  - engine/plugins/trust_levels.py  (get_trust_policy fallback)
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from engine.core.backtest_runner import (
    BacktestConfig,
    BacktestResult,
    BacktestRunner,
    build_timeline,
)
from engine.plugins.sandbox.core.policy import (
    FilesystemPolicy,
    ImportPolicy,
    IntrospectionPolicy,
    NetworkPolicy,
    ResourcePolicy,
    SandboxPolicy,
    _parse_memory,
)
from engine.plugins.sandbox.core.violation import (
    FilesystemViolation,
    ImportViolation,
    IntrospectionViolation,
    NetworkViolation,
    ResourceExhausted,
    SandboxViolation,
    SandboxViolationCategory,
)
from engine.plugins.sandbox.layers.resource_limiter import (
    ResourceLimiter,
    _CPUTimer,
)
from engine.plugins.sandbox.monitoring.event_logger import SecurityEventLogger
from engine.plugins.sandbox.monitoring.metrics import (
    PluginMetrics,
    SandboxMetricsCollector,
)
from engine.plugins.sandbox.monitoring.violation_report import ViolationReport
from engine.plugins.trust_levels import TrustLevel, get_trust_policy

# ─── _parse_memory edge cases ──────────────────────────────────────────


class TestParseMemoryEdgeCases:
    def test_gb_uppercase(self) -> None:
        assert _parse_memory("1GB") == 1024**3

    def test_gb_lowercase(self) -> None:
        assert _parse_memory("1gb") == 1024**3

    def test_mb_with_whitespace(self) -> None:
        assert _parse_memory("  512MB  ") == 512 * 1024**2

    def test_kb(self) -> None:
        assert _parse_memory("1024KB") == 1024 * 1024

    def test_bytes_unit(self) -> None:
        assert _parse_memory("4096B") == 4096

    def test_plain_integer(self) -> None:
        assert _parse_memory("2097152") == 2097152

    def test_float_gb(self) -> None:
        assert _parse_memory("0.5GB") == int(0.5 * 1024**3)

    def test_float_mb(self) -> None:
        assert _parse_memory("1.5MB") == int(1.5 * 1024**2)

    def test_empty_string_raises(self) -> None:
        with pytest.raises(ValueError):
            _parse_memory("")

    def test_whitespace_only_raises(self) -> None:
        with pytest.raises(ValueError):
            _parse_memory("   ")

    def test_gb_before_mb_in_parsing_order(self) -> None:
        assert _parse_memory("1GB") != _parse_memory("1MB")


# ─── ImportPolicy.is_allowed ───────────────────────────────────────────


class TestImportPolicyIsAllowed:
    def test_empty_policy_allows_all(self) -> None:
        policy = ImportPolicy()
        assert policy.is_allowed("json") is True
        assert policy.is_allowed("os") is True

    def test_blocked_module_rejected(self) -> None:
        policy = ImportPolicy(blocked_modules={"os"})
        assert policy.is_allowed("os") is False
        assert policy.is_allowed("os.path") is False

    def test_allowlist_only_blocks_unlisted(self) -> None:
        policy = ImportPolicy(allowed_modules={"json"})
        assert policy.is_allowed("json") is True
        assert policy.is_allowed("math") is False

    def test_blocked_overrides_allowed(self) -> None:
        policy = ImportPolicy(blocked_modules={"os"}, allowed_modules={"os", "json"})
        assert policy.is_allowed("os") is False
        assert policy.is_allowed("json") is True

    def test_submodule_resolved_to_root(self) -> None:
        policy = ImportPolicy(blocked_modules={"os"})
        assert policy.is_allowed("os.path") is False
        assert policy.is_allowed("os.environ") is False

    def test_empty_allowlist_is_permissive(self) -> None:
        policy = ImportPolicy(allowed_modules=set(), blocked_modules=set())
        assert policy.is_allowed("anything") is True


# ─── NetworkPolicy.is_host_allowed ─────────────────────────────────────


class TestNetworkPolicyIsHostAllowed:
    def test_empty_endpoints_blocks_all(self) -> None:
        policy = NetworkPolicy()
        assert policy.is_host_allowed("any.host") is False

    def test_exact_match(self) -> None:
        policy = NetworkPolicy(allowed_endpoints=["api.example.com"])
        assert policy.is_host_allowed("api.example.com") is True

    def test_subdomain_match(self) -> None:
        policy = NetworkPolicy(allowed_endpoints=["example.com"])
        assert policy.is_host_allowed("sub.example.com") is True

    def test_deep_subdomain(self) -> None:
        policy = NetworkPolicy(allowed_endpoints=["example.com"])
        assert policy.is_host_allowed("a.b.c.example.com") is True

    def test_partial_suffix_no_match(self) -> None:
        policy = NetworkPolicy(allowed_endpoints=["example.com"])
        assert policy.is_host_allowed("notexample.com") is False

    def test_dot_prefixed_evil_domain(self) -> None:
        policy = NetworkPolicy(allowed_endpoints=["example.com"])
        assert policy.is_host_allowed("example.com.evil.org") is False


# ─── SandboxPolicy.from_manifest edge cases ────────────────────────────


class TestSandboxPolicyFromManifestEdgeCases:
    def test_minimal_manifest(self) -> None:
        manifest = SimpleNamespace()
        policy = SandboxPolicy.from_manifest(manifest)
        assert policy.plugin_id == "unknown"
        assert policy.trust_level == "untrusted"

    def test_manifest_with_id(self) -> None:
        manifest = SimpleNamespace(id="my_plugin")
        policy = SandboxPolicy.from_manifest(manifest)
        assert policy.plugin_id == "my_plugin"

    def test_manifest_with_resources(self) -> None:
        manifest = SimpleNamespace(
            id="res_plugin",
            resources=SimpleNamespace(max_cpu_seconds=60, max_memory="1GB"),
        )
        policy = SandboxPolicy.from_manifest(manifest)
        assert policy.resource_policy.max_cpu_seconds == 60.0
        assert policy.resource_policy.max_memory_bytes == 1024**3

    def test_manifest_with_network(self) -> None:
        manifest = SimpleNamespace(
            id="net_plugin",
            trust_level="trusted_full",
            requires_network=lambda: True,
            network=SimpleNamespace(allowed_endpoints=["api.data.com"]),
        )
        policy = SandboxPolicy.from_manifest(manifest)
        assert policy.network_policy.allowed_endpoints == ["api.data.com"]

    def test_manifest_network_not_required(self) -> None:
        manifest = SimpleNamespace(
            id="no_net",
            trust_level="trusted_full",
            requires_network=lambda: False,
            network=SimpleNamespace(allowed_endpoints=["api.data.com"]),
        )
        policy = SandboxPolicy.from_manifest(manifest)
        assert policy.network_policy.allowed_endpoints == []

    def test_manifest_without_network_method(self) -> None:
        manifest = SimpleNamespace(id="no_net_method", trust_level="trusted_full")
        policy = SandboxPolicy.from_manifest(manifest)
        assert policy.network_policy.allowed_endpoints == []

    def test_manifest_filesystem_rw_for_trusted(self) -> None:
        manifest = SimpleNamespace(
            id="fs_plugin",
            trust_level="trusted_full",
            artifacts=["/data/output"],
            permissions=["filesystem_write"],
            has_permission=lambda p: p == "filesystem_write",
        )
        policy = SandboxPolicy.from_manifest(manifest)
        assert policy.filesystem_policy.read_write_paths == ["/data/output"]

    def test_manifest_no_filesystem_write_for_untrusted(self) -> None:
        manifest = SimpleNamespace(
            id="untrusted_fs",
            trust_level="untrusted",
            artifacts=["/data/output"],
            has_permission=lambda p: True,
            permissions=["filesystem_write"],
        )
        policy = SandboxPolicy.from_manifest(manifest)
        assert policy.filesystem_policy.read_write_paths == []

    def test_manifest_artifacts_as_read_only_paths(self) -> None:
        manifest = SimpleNamespace(
            id="art_plugin",
            artifacts=["/data/input", "/data/config"],
        )
        policy = SandboxPolicy.from_manifest(manifest)
        assert "/data/input" in policy.filesystem_policy.read_only_paths
        assert "/data/config" in policy.filesystem_policy.read_only_paths


# ─── SandboxPolicy.from_trust_level ────────────────────────────────────


class TestSandboxPolicyFromTrustLevel:
    def test_trusted_full_multiplier(self) -> None:
        policy = SandboxPolicy.from_trust_level(
            TrustLevel.TRUSTED_FULL, "p1", max_cpu_seconds=10
        )
        assert policy.resource_policy.max_cpu_seconds == 10 * 4.0

    def test_trusted_limited_multiplier(self) -> None:
        policy = SandboxPolicy.from_trust_level(
            TrustLevel.TRUSTED_LIMITED, "p1", max_cpu_seconds=10
        )
        assert policy.resource_policy.max_cpu_seconds == 10 * 2.0

    def test_untrusted_multiplier(self) -> None:
        policy = SandboxPolicy.from_trust_level(
            TrustLevel.UNTRUSTED, "p1", max_cpu_seconds=10
        )
        assert policy.resource_policy.max_cpu_seconds == 10 * 1.0

    def test_network_endpoints_passed_through(self) -> None:
        policy = SandboxPolicy.from_trust_level(
            TrustLevel.UNTRUSTED,
            "p1",
            network_endpoints=["api.example.com"],
        )
        assert policy.network_policy.allowed_endpoints == ["api.example.com"]

    def test_read_only_paths_passed_through(self) -> None:
        policy = SandboxPolicy.from_trust_level(
            TrustLevel.UNTRUSTED,
            "p1",
            read_only_paths=["/data"],
        )
        assert policy.filesystem_policy.read_only_paths == ["/data"]

    def test_defaults(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED)
        assert policy.plugin_id == "unknown"
        assert policy.trust_level == "untrusted"


# ─── Violation to_dict ─────────────────────────────────────────────────


class TestViolationToDict:
    def test_import_violation_to_dict(self) -> None:
        v = ImportViolation("os", plugin_id="p1")
        d = v.to_dict()
        assert d["category"] == "import"
        assert "os" in d["detail"]
        assert d["attempted_action"] == "import os"
        assert d["plugin_id"] == "p1"

    def test_network_violation_to_dict(self) -> None:
        v = NetworkViolation("evil.com", port=443, plugin_id="p1")
        d = v.to_dict()
        assert d["category"] == "network"
        assert "evil.com" in d["detail"]
        assert d["attempted_action"] == "connect:evil.com:443"

    def test_network_violation_no_port(self) -> None:
        v = NetworkViolation("evil.com", plugin_id="p2")
        d = v.to_dict()
        assert "connect:evil.com:None" in d["attempted_action"]

    def test_filesystem_violation_to_dict(self) -> None:
        v = FilesystemViolation("/etc/passwd", "read", plugin_id="p1")
        d = v.to_dict()
        assert d["category"] == "filesystem"
        assert "/etc/passwd" in d["detail"]
        assert d["attempted_action"] == "read:/etc/passwd"

    def test_introspection_violation_to_dict(self) -> None:
        v = IntrospectionViolation("__globals__", plugin_id="p1")
        d = v.to_dict()
        assert d["category"] == "introspection"
        assert "__globals__" in d["detail"]
        assert d["attempted_action"] == "access:__globals__"

    def test_resource_exhausted_to_dict(self) -> None:
        v = ResourceExhausted("memory", 512, 600, plugin_id="p1")
        d = v.to_dict()
        assert d["category"] == "resource"
        assert "memory" in d["detail"]
        assert d["attempted_action"] == "allocate:memory"

    def test_sandbox_violation_base_no_attempted_action(self) -> None:
        v = SandboxViolation(
            "test error",
            category=SandboxViolationCategory.IMPORT,
        )
        d = v.to_dict()
        assert d["detail"] == "test error"
        assert d["attempted_action"] is None
        assert d["plugin_id"] is None

    def test_all_categories_covered(self) -> None:
        for cat in SandboxViolationCategory:
            v = SandboxViolation("msg", category=cat)
            assert v.to_dict()["category"] == cat.value


# ─── ViolationReport ───────────────────────────────────────────────────


class TestViolationReportComprehensive:
    def test_empty_report(self) -> None:
        report = ViolationReport(plugin_id="empty")
        assert report.total_violations == 0
        assert report.by_category == {}
        assert report.plugin_id == "empty"

    def test_default_layers_initialized(self) -> None:
        report = ViolationReport()
        assert "import" in report.by_layer
        assert "network" in report.by_layer
        assert "resource" in report.by_layer
        assert "filesystem" in report.by_layer
        assert "introspection" in report.by_layer

    def test_to_dict_structure(self) -> None:
        report = ViolationReport(plugin_id="test")
        d = report.to_dict()
        assert "plugin_id" in d
        assert "generated_at" in d
        assert "total_violations" in d
        assert "by_category" in d
        assert "by_layer" in d
        assert d["plugin_id"] == "test"

    def test_to_json_roundtrip(self) -> None:
        import json

        report = ViolationReport(plugin_id="json_test")
        report.by_category = {"import": 2, "network": 1}
        report.total_violations = 3
        j = report.to_json()
        parsed = json.loads(j)
        assert parsed["plugin_id"] == "json_test"
        assert parsed["total_violations"] == 3
        assert parsed["by_category"]["import"] == 2

    def test_to_json_custom_indent(self) -> None:
        report = ViolationReport(plugin_id="indent")
        j = report.to_json(indent=4)
        assert "    " in j

    def test_summary_format(self) -> None:
        report = ViolationReport(plugin_id="summary_test")
        report.by_category = {"import": 5}
        report.total_violations = 5
        s = report.summary()
        assert "summary_test" in s
        assert "5" in s
        assert "import" in s

    def test_summary_empty_category(self) -> None:
        report = ViolationReport(plugin_id="no_cats")
        s = report.summary()
        assert "no_cats" in s
        assert "By category" not in s

    def test_from_events_with_mixed_categories(self) -> None:
        events = []
        logger = SecurityEventLogger(plugin_id="rp")
        logger.log_violation(ImportViolation("os", plugin_id="rp"))
        logger.log_violation(NetworkViolation("evil.com", plugin_id="rp"))
        logger.log_violation(FilesystemViolation("/etc/passwd", "read", plugin_id="rp"))
        events = logger.get_events(limit=100)

        report = ViolationReport.from_events(events, plugin_id="rp")
        assert report.total_violations == 3
        assert report.by_category.get("import", 0) >= 1
        assert report.by_category.get("network", 0) >= 1
        assert report.by_category.get("filesystem", 0) >= 1
        assert len(report.by_layer["import"]) >= 1
        assert len(report.by_layer["network"]) >= 1

    def test_from_events_empty_list(self) -> None:
        report = ViolationReport.from_events([], plugin_id="empty")
        assert report.total_violations == 0
        assert report.by_category == {}


# ─── SecurityEventLogger.log_event ─────────────────────────────────────


class TestSecurityEventLoggerLogEvent:
    def test_log_event_creates_entry(self) -> None:
        logger = SecurityEventLogger(plugin_id="p1")
        logger.log_event(
            SandboxViolationCategory.IMPORT,
            "custom event",
            attempted_action="test_action",
        )
        assert logger.event_count == 1
        events = logger.get_events()
        assert events[0].detail == "custom event"
        assert events[0].attempted_action == "test_action"

    def test_log_event_has_timestamp(self) -> None:
        logger = SecurityEventLogger()
        before = time.time()
        logger.log_event(SandboxViolationCategory.NETWORK, "test")
        after = time.time()
        events = logger.get_events()
        assert before <= events[0].timestamp <= after

    def test_log_event_stack_trace_present(self) -> None:
        logger = SecurityEventLogger()
        logger.log_event(SandboxViolationCategory.RESOURCE, "oom")
        events = logger.get_events()
        assert events[0].stack_trace is not None

    def test_log_event_default_no_action(self) -> None:
        logger = SecurityEventLogger()
        logger.log_event(SandboxViolationCategory.FILESYSTEM, "msg")
        events = logger.get_events()
        assert events[0].attempted_action is None

    def test_log_violation_inherits_plugin_id(self) -> None:
        logger = SecurityEventLogger(plugin_id="inherited")
        v = ImportViolation("os")
        logger.log_violation(v)
        events = logger.get_events()
        assert events[0].plugin_id == "inherited"

    def test_log_violation_uses_violation_plugin_id(self) -> None:
        logger = SecurityEventLogger(plugin_id="logger_pid")
        v = ImportViolation("os", plugin_id="violation_pid")
        logger.log_violation(v)
        events = logger.get_events()
        assert events[0].plugin_id == "violation_pid"


# ─── SandboxMetricsCollector.get_or_create ─────────────────────────────


class TestSandboxMetricsCollectorGetOrCreate:
    def test_creates_new_entry(self) -> None:
        collector = SandboxMetricsCollector()
        m = collector.get_or_create("p1")
        assert m.plugin_id == "p1"
        assert m.total_evaluations == 0

    def test_returns_existing(self) -> None:
        collector = SandboxMetricsCollector()
        m1 = collector.get_or_create("p1")
        m1.total_evaluations = 5
        m2 = collector.get_or_create("p1")
        assert m2.total_evaluations == 5
        assert m1 is m2

    def test_different_plugins_independent(self) -> None:
        collector = SandboxMetricsCollector()
        m1 = collector.get_or_create("p1")
        m2 = collector.get_or_create("p2")
        m1.total_evaluations = 10
        assert m2.total_evaluations == 0


# ─── PluginMetrics dataclass ───────────────────────────────────────────


class TestPluginMetricsDefaults:
    def test_all_defaults(self) -> None:
        m = PluginMetrics(plugin_id="test")
        assert m.total_evaluations == 0
        assert m.total_signals_emitted == 0
        assert m.total_cpu_time_ms == 0.0
        assert m.avg_evaluation_ms == 0.0
        assert m.peak_memory_bytes == 0
        assert m.current_memory_bytes == 0
        assert m.api_calls == 0
        assert m.errors == 0
        assert m.last_error is None
        assert m.security_violations == 0
        assert m.file_operations == 0
        assert m.network_requests == 0

    def test_to_dict_keys(self) -> None:
        m = PluginMetrics(plugin_id="test")
        d = m.to_dict()
        expected_keys = {
            "plugin_id", "total_evaluations", "total_signals_emitted",
            "total_cpu_time_ms", "avg_evaluation_ms", "peak_memory_bytes",
            "current_memory_bytes", "api_calls", "errors", "last_error",
            "security_violations", "file_operations", "network_requests",
        }
        assert set(d.keys()) == expected_keys


# ─── _CPUTimer ─────────────────────────────────────────────────────────


class TestCPUTimer:
    def test_not_expired_initially(self) -> None:
        timer = _CPUTimer(10.0)
        assert timer.expired is False

    def test_elapsed_before_start(self) -> None:
        timer = _CPUTimer(10.0)
        elapsed = timer.elapsed
        assert elapsed >= 0

    def test_start_and_check_within_limit(self) -> None:
        timer = _CPUTimer(10.0)
        timer.start()
        try:
            timer.check()
        finally:
            timer.stop()

    def test_expired_after_timeout(self) -> None:
        timer = _CPUTimer(0.01)
        timer.start()
        _burn_cpu(0.05)
        assert timer.expired is True
        timer.stop()

    def test_check_raises_when_expired(self) -> None:
        timer = _CPUTimer(0.01, plugin_id="test")
        timer.start()
        _burn_cpu(0.05)
        with pytest.raises(ResourceExhausted, match="cpu_time"):
            timer.check()
        timer.stop()

    def test_stop_cancels_timer(self) -> None:
        timer = _CPUTimer(10.0)
        timer.start()
        timer.stop()
        assert timer._timer is None

    def test_elapsed_after_start(self) -> None:
        timer = _CPUTimer(10.0)
        timer.start()
        time.sleep(0.02)
        elapsed = timer.elapsed
        assert elapsed >= 0.01
        timer.stop()

    def test_check_raises_on_elapsed_exceeding_limit(self) -> None:
        timer = _CPUTimer(0.01, plugin_id="test")
        timer.start()
        time.sleep(0.05)
        with pytest.raises(ResourceExhausted) as exc_info:
            timer.check()
        assert exc_info.value.resource_type == "cpu_time"
        assert exc_info.value.plugin_id == "test"
        timer.stop()


# ─── ResourceLimiter cpu_elapsed and check_cpu_timer ───────────────────


class TestResourceLimiterCPUTimer:
    def test_cpu_elapsed_zero_without_install(self) -> None:
        limiter = ResourceLimiter(ResourcePolicy())
        assert limiter.cpu_elapsed == 0.0

    def test_check_cpu_timer_noop_without_install(self) -> None:
        limiter = ResourceLimiter(ResourcePolicy())
        limiter.check_cpu_timer()

    def test_cpu_elapsed_after_install(self) -> None:
        limiter = ResourceLimiter(ResourcePolicy(max_cpu_seconds=30))
        limiter.install()
        try:
            time.sleep(0.02)
            elapsed = limiter.cpu_elapsed
            assert elapsed >= 0.01
        finally:
            limiter.uninstall()


# ─── BacktestRunner error paths ────────────────────────────────────────


class _SynthProvider:
    def __init__(self, data: dict[str, pd.DataFrame]):
        self._data = data

    async def get_latest_price(self, symbol: str) -> float | None:
        df = self._data.get(symbol)
        if df is None or df.empty:
            return None
        return float(df["close"].iloc[-1])

    async def get_ohlcv(
        self, symbol: str, period: str = "1y", interval: str = "1d"
    ) -> pd.DataFrame:
        return self._data.get(symbol, pd.DataFrame())

    async def get_multiple_prices(self, symbols: list[str]) -> dict[str, float]:
        result = {}
        for sym in symbols:
            price = await self.get_latest_price(sym)
            if price is not None:
                result[sym] = price
        return result


class TestBacktestRunnerErrorPaths:
    @pytest.mark.asyncio
    async def test_no_provider_raises(self) -> None:
        config = BacktestConfig(
            strategy_name="test", symbol="AAPL",
            start_date="2024-01-01", end_date="2024-12-31",
        )
        runner = BacktestRunner(config=config)

        class DummyStrat:
            name = "test"
            version = "1.0"
            def on_bar(self, s, p): return []

        runner.strategy = DummyStrat()
        with pytest.raises(RuntimeError, match="No data provider"):
            await runner.run()

    @pytest.mark.asyncio
    async def test_no_strategy_raises(self) -> None:
        config = BacktestConfig(
            strategy_name="test", symbol="AAPL",
            start_date="2024-01-01", end_date="2024-12-31",
        )
        provider = _SynthProvider({"AAPL": pd.DataFrame({"open": [], "high": [], "low": [], "close": [], "volume": []})})
        runner = BacktestRunner(config=config, provider=provider)
        with pytest.raises(RuntimeError, match="No strategy"):
            await runner.run()

    @pytest.mark.asyncio
    async def test_empty_ohlcv_single_symbol_raises(self) -> None:
        config = BacktestConfig(
            strategy_name="test", symbol="AAPL",
            start_date="2024-01-01", end_date="2024-12-31",
        )
        provider = _SynthProvider({"AAPL": pd.DataFrame()})
        runner = BacktestRunner(config=config, strategy=type("S", (), {"name": "s", "version": "1", "on_bar": lambda self, s, p: []})(), provider=provider)
        with pytest.raises(RuntimeError, match="No OHLCV data"):
            await runner.run()

    @pytest.mark.asyncio
    async def test_multi_symbol_skips_empty(self) -> None:
        config = BacktestConfig(
            strategy_name="test", symbol="AAPL",
            start_date="2024-01-01", end_date="2024-12-31",
            symbols=["AAPL", "MSFT"],
            min_bars=1,
        )
        dates = pd.bdate_range("2024-01-01", periods=5)
        df_aapl = pd.DataFrame(
            {"open": [100]*5, "high": [101]*5, "low": [99]*5, "close": [100]*5, "volume": [1000]*5},
            index=dates,
        )
        provider = _SynthProvider({"AAPL": df_aapl, "MSFT": pd.DataFrame()})
        runner = BacktestRunner(
            config=config,
            strategy=type("S", (), {"name": "s", "version": "1", "on_bar": lambda self, s, p: []})(),
            provider=provider,
        )
        result = await runner.run()
        assert result.final_capital == pytest.approx(100_000.0, abs=0.01)

    @pytest.mark.asyncio
    async def test_all_symbols_no_data_in_range(self) -> None:
        config = BacktestConfig(
            strategy_name="test", symbol="AAPL",
            start_date="2099-01-01", end_date="2099-12-31",
            min_bars=1,
        )
        dates = pd.bdate_range("2024-01-01", periods=5)
        df = pd.DataFrame(
            {"open": [100]*5, "high": [101]*5, "low": [99]*5, "close": [100]*5, "volume": [1000]*5},
            index=dates,
        )
        provider = _SynthProvider({"AAPL": df})
        runner = BacktestRunner(
            config=config,
            strategy=type("S", (), {"name": "s", "version": "1", "on_bar": lambda self, s, p: []})(),
            provider=provider,
        )
        with pytest.raises(RuntimeError, match="No data in range"):
            await runner.run()


# ─── BacktestRunner._apply_strategy_params ─────────────────────────────


class TestApplyStrategyParams:
    @pytest.mark.asyncio
    async def test_nonexistent_attr_not_set(self) -> None:
        config = BacktestConfig(
            strategy_name="test", symbol="AAPL",
            start_date="2024-01-01", end_date="2024-12-31",
            strategy_params={"nonexistent_param": 42},
        )

        class Strat:
            name = "test"
            version = "1.0"
            def on_bar(self, s, p): return []

        strat = Strat()
        dates = pd.bdate_range("2024-01-01", periods=60)
        df = pd.DataFrame(
            {"open": [100]*60, "high": [101]*60, "low": [99]*60, "close": [100]*60, "volume": [1000]*60},
            index=dates,
        )
        provider = _SynthProvider({"AAPL": df})
        runner = BacktestRunner(config=config, strategy=strat, provider=provider)
        await runner.run()
        assert not hasattr(strat, "nonexistent_param")

    @pytest.mark.asyncio
    async def test_callable_not_overwritten(self) -> None:
        config = BacktestConfig(
            strategy_name="test", symbol="AAPL",
            start_date="2024-01-01", end_date="2024-12-31",
            strategy_params={"on_bar": "should_not_set"},
        )

        class Strat:
            name = "test"
            version = "1.0"
            def on_bar(self, s, p): return []

        strat = Strat()
        dates = pd.bdate_range("2024-01-01", periods=60)
        df = pd.DataFrame(
            {"open": [100]*60, "high": [101]*60, "low": [99]*60, "close": [100]*60, "volume": [1000]*60},
            index=dates,
        )
        provider = _SynthProvider({"AAPL": df})
        runner = BacktestRunner(config=config, strategy=strat, provider=provider)
        await runner.run()
        assert callable(strat.on_bar)

    @pytest.mark.asyncio
    async def test_no_params_no_change(self) -> None:
        config = BacktestConfig(
            strategy_name="test", symbol="AAPL",
            start_date="2024-01-01", end_date="2024-12-31",
        )

        class Strat:
            window: int = 20
            name = "test"
            version = "1.0"
            def on_bar(self, s, p): return []

        strat = Strat()
        dates = pd.bdate_range("2024-01-01", periods=60)
        df = pd.DataFrame(
            {"open": [100]*60, "high": [101]*60, "low": [99]*60, "close": [100]*60, "volume": [1000]*60},
            index=dates,
        )
        provider = _SynthProvider({"AAPL": df})
        runner = BacktestRunner(config=config, strategy=strat, provider=provider)
        await runner.run()
        assert strat.window == 20


# ─── BacktestResult and BacktestConfig defaults ────────────────────────


class TestBacktestResultDefaults:
    def test_default_values(self) -> None:
        result = BacktestResult()
        assert result.portfolio_id is None
        assert result.equity_curve == []
        assert result.trades == []
        assert result.metrics == {}
        assert result.final_capital == 0.0
        assert result.total_return_pct == 0.0


class TestBacktestConfigDefaults:
    def test_default_values(self) -> None:
        config = BacktestConfig(
            strategy_name="test", symbol="AAPL",
            start_date="2024-01-01", end_date="2024-12-31",
        )
        assert config.initial_capital == 100_000.0
        assert config.min_bars == 50
        assert config.debug is False
        assert config.random_seed == 42
        assert config.symbols is None
        assert config.strategy_params == {}
        assert config.cost_config == {}
        assert config.interval == "1d"


# ─── build_timeline additional edge cases ──────────────────────────────


class TestBuildTimelineEdgeCases:
    def test_single_bar(self) -> None:
        dates = pd.DatetimeIndex([pd.Timestamp("2024-01-01")])
        df = pd.DataFrame(
            {"open": [100], "high": [101], "low": [99], "close": [100], "volume": [1000]},
            index=dates,
        )
        timeline = build_timeline({"AAPL": df})
        assert len(timeline) == 1
        _ts, bars = timeline[0]
        assert "AAPL" in bars
        assert bars["AAPL"]["close"] == 100.0

    def test_volume_is_int(self) -> None:
        dates = pd.DatetimeIndex([pd.Timestamp("2024-01-01")])
        df = pd.DataFrame(
            {"open": [100], "high": [101], "low": [99], "close": [100], "volume": [1000]},
            index=dates,
        )
        timeline = build_timeline({"AAPL": df})
        assert isinstance(timeline[0][1]["AAPL"]["volume"], int)

    def test_three_symbols_partial_overlap(self) -> None:
        dates_a = pd.bdate_range("2024-01-01", periods=5)
        dates_b = pd.bdate_range("2024-01-01", periods=3)
        dates_c = pd.bdate_range("2024-01-01", periods=4)

        df_a = pd.DataFrame({"open": [100]*5, "high": [101]*5, "low": [99]*5, "close": [100]*5, "volume": [1000]*5}, index=dates_a)
        df_b = pd.DataFrame({"open": [200]*3, "high": [201]*3, "low": [199]*3, "close": [200]*3, "volume": [2000]*3}, index=dates_b)
        df_c = pd.DataFrame({"open": [300]*4, "high": [301]*4, "low": [299]*4, "close": [300]*4, "volume": [3000]*4}, index=dates_c)

        timeline = build_timeline({"AAPL": df_a, "MSFT": df_b, "GOOG": df_c})
        assert len(timeline) == 5

        has_all_three = sum(1 for _, bars in timeline if len(bars) == 3)
        assert has_all_three == 3

    def test_preserves_float_precision(self) -> None:
        dates = pd.DatetimeIndex([pd.Timestamp("2024-01-01")])
        df = pd.DataFrame(
            {"open": [100.123], "high": [101.456], "low": [99.789], "close": [100.001], "volume": [1000]},
            index=dates,
        )
        timeline = build_timeline({"AAPL": df})
        bar = timeline[0][1]["AAPL"]
        assert bar["open"] == pytest.approx(100.123)
        assert bar["close"] == pytest.approx(100.001)


# ─── get_trust_policy fallback ─────────────────────────────────────────


class TestGetTrustPolicyFallback:
    def test_valid_trusted_full(self) -> None:
        policy = get_trust_policy(TrustLevel.TRUSTED_FULL)
        assert policy["resource_multiplier"] == 4.0

    def test_valid_trusted_limited(self) -> None:
        policy = get_trust_policy(TrustLevel.TRUSTED_LIMITED)
        assert policy["resource_multiplier"] == 2.0

    def test_valid_untrusted(self) -> None:
        policy = get_trust_policy(TrustLevel.UNTRUSTED)
        assert policy["resource_multiplier"] == 1.0

    def test_all_policies_have_required_keys(self) -> None:
        for level in TrustLevel:
            policy = get_trust_policy(level)
            assert "import_restriction" in policy
            assert "network" in policy
            assert "resource_multiplier" in policy
            assert "filesystem" in policy
            assert "introspection" in policy


# ─── TrustLevel enum ──────────────────────────────────────────────────


class TestTrustLevelEnum:
    def test_values(self) -> None:
        assert TrustLevel.TRUSTED_FULL.value == "trusted_full"
        assert TrustLevel.TRUSTED_LIMITED.value == "trusted_limited"
        assert TrustLevel.UNTRUSTED.value == "untrusted"

    def test_from_value(self) -> None:
        assert TrustLevel("trusted_full") == TrustLevel.TRUSTED_FULL
        assert TrustLevel("trusted_limited") == TrustLevel.TRUSTED_LIMITED
        assert TrustLevel("untrusted") == TrustLevel.UNTRUSTED

    def test_invalid_value_raises(self) -> None:
        with pytest.raises(ValueError):
            TrustLevel("super_admin")


# ─── ResourcePolicy defaults ───────────────────────────────────────────


class TestResourcePolicyDefaults:
    def test_default_values(self) -> None:
        policy = ResourcePolicy()
        assert policy.max_cpu_seconds == 30.0
        assert policy.max_memory_bytes == 512 * 1024 * 1024
        assert policy.max_file_descriptors == 64
        assert policy.max_threads == 1
        assert policy.wall_time_seconds == 60.0


# ─── FilesystemPolicy defaults ─────────────────────────────────────────


class TestFilesystemPolicyDefaults:
    def test_default_values(self) -> None:
        policy = FilesystemPolicy()
        assert policy.read_only_paths == []
        assert policy.read_write_paths == []
        assert policy.virtual_root is None
        assert policy.block_symlinks is True
        assert policy.block_absolute_paths is True


# ─── IntrospectionPolicy defaults ──────────────────────────────────────


class TestIntrospectionPolicyDefaults:
    def test_default_blocked_builtins(self) -> None:
        policy = IntrospectionPolicy()
        assert "eval" in policy.blocked_builtins
        assert "exec" in policy.blocked_builtins
        assert "compile" in policy.blocked_builtins
        assert "breakpoint" in policy.blocked_builtins

    def test_default_blocked_attributes(self) -> None:
        policy = IntrospectionPolicy()
        assert "__subclasses__" in policy.blocked_attributes
        assert "__globals__" in policy.blocked_attributes
        assert "__bases__" in policy.blocked_attributes
        assert "__dict__" in policy.blocked_attributes

    def test_dunder_access_blocked(self) -> None:
        policy = IntrospectionPolicy()
        assert policy.blocked_dunder_access is True

    def test_gc_blocked(self) -> None:
        policy = IntrospectionPolicy()
        assert policy.block_gc is True

    def test_inspect_blocked(self) -> None:
        policy = IntrospectionPolicy()
        assert policy.block_inspect is True

    def test_frame_access_blocked(self) -> None:
        policy = IntrospectionPolicy()
        assert policy.block_frame_access is True


# ─── BacktestRunner with Buy/Sell producing realized PnL ──────────────


class TestBacktestRunnerRealizedPnL:
    @pytest.mark.asyncio
    async def test_sell_trade_has_realized_pnl(self) -> None:
        from engine.core.signal import Side, Signal

        class BuySellStrat:
            name = "pnl_test"
            version = "1.0"

            def __init__(self):
                self._bar = 0

            def on_bar(self, state, portfolio):
                self._bar += 1
                symbol = next(iter(state.prices.keys())) if state.prices else "AAPL"
                if self._bar == 55:
                    return [Signal(symbol=symbol, side=Side.BUY, quantity=100, strategy_id="pnl_test")]
                if self._bar == 75:
                    return [Signal(symbol=symbol, side=Side.SELL, quantity=100, strategy_id="pnl_test")]
                return []

        dates = pd.bdate_range("2024-01-01", periods=100)
        rng = np.random.default_rng(42)
        prices = 100 * np.cumprod(1 + rng.normal(0.0005, 0.015, 100))
        df = pd.DataFrame(
            {
                "open": prices * (1 + rng.normal(0, 0.002, 100)),
                "high": prices * (1 + np.abs(rng.normal(0, 0.003, 100))),
                "low": prices * (1 - np.abs(rng.normal(0, 0.003, 100))),
                "close": prices,
                "volume": rng.integers(500_000, 5_000_000, 100),
            },
            index=dates,
        )
        config = BacktestConfig(
            strategy_name="pnl_test", symbol="AAPL",
            start_date="2024-01-01", end_date="2024-12-31",
            min_bars=5,
        )
        provider = _SynthProvider({"AAPL": df})
        runner = BacktestRunner(config=config, strategy=BuySellStrat(), provider=provider)
        result = await runner.run()

        sells = [t for t in result.trades if t["side"] == "sell"]
        if sells:
            for sell in sells:
                assert "realized_pnl" in sell
                assert isinstance(sell["realized_pnl"], float)

    @pytest.mark.asyncio
    async def test_buy_trade_realized_pnl_zero(self) -> None:
        from engine.core.signal import Side, Signal

        class BuyOnlyStrat:
            name = "buy_only"
            version = "1.0"

            def __init__(self):
                self._bought = False

            def on_bar(self, state, portfolio):
                if not self._bought and portfolio.cash > 50000:
                    self._bought = True
                    symbol = next(iter(state.prices.keys())) if state.prices else "AAPL"
                    return [Signal(symbol=symbol, side=Side.BUY, quantity=10, strategy_id="buy_only")]
                return []

        dates = pd.bdate_range("2024-01-01", periods=100)
        df = pd.DataFrame(
            {"open": [100]*100, "high": [101]*100, "low": [99]*100, "close": [100]*100, "volume": [1000]*100},
            index=dates,
        )
        config = BacktestConfig(
            strategy_name="buy_only", symbol="AAPL",
            start_date="2024-01-01", end_date="2024-12-31",
            min_bars=5,
        )
        provider = _SynthProvider({"AAPL": df})
        runner = BacktestRunner(config=config, strategy=BuyOnlyStrat(), provider=provider)
        result = await runner.run()

        buys = [t for t in result.trades if t["side"] == "buy"]
        for buy in buys:
            assert buy["realized_pnl"] == 0.0


def _burn_cpu(duration: float) -> None:
    end = time.monotonic() + duration
    total = 0.0
    while time.monotonic() < end:
        total += 1.0
