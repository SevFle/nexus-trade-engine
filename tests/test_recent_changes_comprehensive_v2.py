"""Comprehensive tests for recently changed code — loop-break cycle.

Targets:
- engine/plugins/manifest.py (StrategyManifest, ResourceLimits, NetworkConfig)
- engine/data/providers/_resilience.py (TokenBucket edge cases, call_with_retry)
- engine/legal/service.py (_version_gte, _version_lt)
- engine/privacy/export.py (_jsonify edge cases: UUID, Decimal, nested)
- engine/events/webhook_dispatcher.py (_backoff_delay, canonical_payload edge cases)
- engine/core/live/kill_switch.py (edge cases: empty reason, observers, re-engage)
"""
from __future__ import annotations

import uuid
from decimal import Decimal
from typing import ClassVar
from unittest.mock import MagicMock

import pytest

from engine.data.providers.base import FatalProviderError, RateLimit, TransientProviderError
from engine.events.webhook_dispatcher import (
    _backoff_delay,
    canonical_payload,
    render_template,
    sign_payload,
)
from engine.plugins.manifest import NetworkConfig, ResourceLimits, StrategyManifest
from engine.privacy.export import _jsonify, _row_to_dict

# ---------- engine/plugins/manifest.py ----------


class TestStrategyManifest:
    def test_minimal_valid_manifest(self):
        m = StrategyManifest(id="sma", name="SMA Cross", version="1.0.0")
        assert m.id == "sma"
        assert m.author == "unknown"
        assert m.runtime == "python:3.11"

    def test_full_manifest_round_trip(self):
        m = StrategyManifest(
            id="momentum",
            name="Momentum Alpha",
            version="2.3.1",
            author="alice",
            description="Momentum-based strategy",
            dependencies=["numpy>=2.0", "pandas"],
            resources=ResourceLimits(max_memory="1GB", gpu="required", max_cpu_seconds=60),
            network=NetworkConfig(allowed_endpoints=["https://api.example.com"]),
            data_feeds=["ohlcv", "news"],
            watchlist=["AAPL", "GOOG"],
        )
        assert m.requires_network() is True
        assert m.requires_gpu() is True
        assert len(m.dependencies) == 2
        assert m.watchlist == ["AAPL", "GOOG"]

    def test_requires_network_false_when_no_endpoints(self):
        m = StrategyManifest(id="x", name="X", version="0.1.0")
        assert m.requires_network() is False

    def test_requires_gpu_false_when_none(self):
        m = StrategyManifest(id="x", name="X", version="0.1.0")
        assert m.requires_gpu() is False

    def test_requires_gpu_optional_not_required(self):
        m = StrategyManifest(
            id="x", name="X", version="0.1.0",
            resources=ResourceLimits(gpu="optional"),
        )
        assert m.requires_gpu() is False

    def test_default_data_feeds_is_ohlcv(self):
        m = StrategyManifest(id="x", name="X", version="0.1.0")
        assert m.data_feeds == ["ohlcv"]

    def test_default_min_history_bars(self):
        m = StrategyManifest(id="x", name="X", version="0.1.0")
        assert m.min_history_bars == 50

    def test_marketplace_default_none(self):
        m = StrategyManifest(id="x", name="X", version="0.1.0")
        assert m.marketplace is None

    def test_config_schema_default(self):
        m = StrategyManifest(id="x", name="X", version="0.1.0")
        assert m.config_schema["type"] == "object"

    def test_artifacts_default_empty(self):
        m = StrategyManifest(id="x", name="X", version="0.1.0")
        assert m.artifacts == []

    def test_serialization_round_trip(self):
        m = StrategyManifest(
            id="rsi", name="RSI", version="1.0.0",
            resources=ResourceLimits(max_memory="2GB"),
        )
        data = m.model_dump()
        m2 = StrategyManifest.model_validate(data)
        assert m2.id == m.id
        assert m2.resources.max_memory == "2GB"


class TestResourceLimits:
    def test_defaults(self):
        r = ResourceLimits()
        assert r.max_memory == "512MB"
        assert r.gpu == "none"
        assert r.max_cpu_seconds == 30


class TestNetworkConfig:
    def test_default_empty_endpoints(self):
        n = NetworkConfig()
        assert n.allowed_endpoints == []


# ---------- engine/data/providers/_resilience.py ----------


class TestTokenBucketEdgeCases:
    @pytest.mark.asyncio
    async def test_zero_rpm_disables_limiting(self):
        from engine.data.providers._resilience import TokenBucket

        tb = TokenBucket(RateLimit(requests_per_minute=0))
        for _ in range(100):
            await tb.acquire()

    @pytest.mark.asyncio
    async def test_burst_equal_to_capacity(self):
        from engine.data.providers._resilience import TokenBucket

        tb = TokenBucket(RateLimit(requests_per_minute=60, burst=3))
        for _ in range(3):
            await tb.acquire()

    @pytest.mark.asyncio
    async def test_negative_rpm_treated_as_zero(self):
        from engine.data.providers._resilience import TokenBucket

        tb = TokenBucket(RateLimit(requests_per_minute=-5))
        for _ in range(50):
            await tb.acquire()


class TestCallWithRetryEdgeCases:
    @pytest.mark.asyncio
    async def test_fatal_propagates_immediately(self):
        from engine.data.providers._resilience import call_with_retry

        async def boom():
            raise FatalProviderError("unrecoverable")

        with pytest.raises(FatalProviderError, match="unrecoverable"):
            await call_with_retry(boom, provider="test", max_attempts=5)

    @pytest.mark.asyncio
    async def test_transient_retried_then_raises(self):
        from engine.data.providers._resilience import call_with_retry

        call_count = 0

        async def flaky():
            nonlocal call_count
            call_count += 1
            raise TransientProviderError("temp")

        with pytest.raises(TransientProviderError):
            await call_with_retry(
                flaky,
                provider="test",
                max_attempts=2,
                base_delay_s=0.01,
                max_delay_s=0.01,
            )
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_timeout_error_retried(self):
        from engine.data.providers._resilience import call_with_retry

        attempts = 0

        async def slow():
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise TimeoutError("too slow")
            return "ok"

        result = await call_with_retry(
            slow,
            provider="test",
            max_attempts=3,
            base_delay_s=0.01,
            max_delay_s=0.01,
        )
        assert result == "ok"
        assert attempts == 3

    @pytest.mark.asyncio
    async def test_success_on_first_attempt(self):
        from engine.data.providers._resilience import call_with_retry

        async def ok():
            return 42

        result = await call_with_retry(ok, provider="test")
        assert result == 42


# ---------- engine/legal/service.py ----------


class TestVersionComparison:
    def test_version_gte_equal(self):
        from engine.legal.service import _version_gte

        assert _version_gte("1.0.0", "1.0.0") is True

    def test_version_gte_greater(self):
        from engine.legal.service import _version_gte

        assert _version_gte("2.0.0", "1.0.0") is True

    def test_version_gte_less(self):
        from engine.legal.service import _version_gte

        assert _version_gte("0.9.0", "1.0.0") is False

    def test_version_gte_patch_difference(self):
        from engine.legal.service import _version_gte

        assert _version_gte("1.0.1", "1.0.0") is True
        assert _version_gte("1.0.0", "1.0.1") is False

    def test_version_lt_equal(self):
        from engine.legal.service import _version_lt

        assert _version_lt("1.0.0", "1.0.0") is False

    def test_version_lt_less(self):
        from engine.legal.service import _version_lt

        assert _version_lt("0.9.0", "1.0.0") is True

    def test_version_lt_greater(self):
        from engine.legal.service import _version_lt

        assert _version_lt("2.0.0", "1.0.0") is False


# ---------- engine/privacy/export.py ----------


class TestJsonifyEdgeCases:
    def test_uuid_stringified(self):
        val = uuid.UUID("12345678-1234-5678-1234-567812345678")
        result = _jsonify(val)
        assert isinstance(result, str)
        assert "12345678" in result

    def test_decimal_stringified(self):
        result = _jsonify(Decimal("3.14"))
        assert isinstance(result, str)
        assert "3.14" in result

    def test_nested_list_with_mixed(self):
        result = _jsonify([1, [2, 3], "four"])
        assert result == [1, [2, 3], "four"]

    def test_nested_dict(self):
        result = _jsonify({"a": {"b": 1}})
        assert result == {"a": {"b": 1}}

    def test_bool_not_confused_with_int(self):
        assert _jsonify(True) is True
        assert _jsonify(False) is False

    def test_empty_list(self):
        assert _jsonify([]) == []

    def test_empty_dict(self):
        assert _jsonify({}) == {}


class TestRowToDictEdgeCases:
    def test_uuid_column_stringified(self):
        class _FakeTable:
            columns: ClassVar[list] = [type("C", (), {"name": "id"})]

        class _Row:
            __table__ = _FakeTable()
            id = uuid.UUID("00000000-0000-0000-0000-000000000001")

        out = _row_to_dict(_Row())
        assert isinstance(out["id"], str)

    def test_decimal_column_stringified(self):
        class _FakeTable:
            columns: ClassVar[list] = [type("C", (), {"name": "amount"})]

        class _Row:
            __table__ = _FakeTable()
            amount = Decimal("99.99")

        out = _row_to_dict(_Row())
        assert isinstance(out["amount"], str)

    def test_none_value_preserved(self):
        class _FakeTable:
            columns: ClassVar[list] = [type("C", (), {"name": "optional_field"})]

        class _Row:
            __table__ = _FakeTable()
            optional_field = None

        out = _row_to_dict(_Row())
        assert out["optional_field"] is None

    def test_multiple_deny_columns(self):
        class _FakeTable:
            columns: ClassVar[list] = [
                type("C", (), {"name": "a"}),
                type("C", (), {"name": "b"}),
                type("C", (), {"name": "c"}),
            ]

        class _Row:
            __table__ = _FakeTable()
            a = 1
            b = 2
            c = 3

        out = _row_to_dict(_Row(), deny=frozenset({"a", "c"}))
        assert out == {"b": 2}


# ---------- engine/events/webhook_dispatcher.py ----------


class TestBackoffDelay:
    def test_first_attempt_is_one(self):
        assert _backoff_delay(1) == 1.0

    def test_second_attempt_is_two(self):
        assert _backoff_delay(2) == 2.0

    def test_third_attempt_is_four(self):
        assert _backoff_delay(3) == 4.0

    def test_capped_at_sixty(self):
        assert _backoff_delay(10) == 60.0
        assert _backoff_delay(100) == 60.0


class TestCanonicalPayloadEdgeCases:
    def test_empty_data(self):
        p = canonical_payload("test", {})
        assert p["data"] == {}
        assert "timestamp" in p

    def test_nested_data(self):
        p = canonical_payload("test", {"nested": {"deep": [1, 2]}})
        assert p["data"]["nested"]["deep"] == [1, 2]


class TestRenderTemplateEdgeCases:
    def _base_payload(self):
        return canonical_payload("evt", {"key": "val"})

    def test_discord_embeds_have_timestamp(self):
        out = render_template("discord", self._base_payload())
        assert out["embeds"][0]["timestamp"] is not None

    def test_slack_blocks_structure(self):
        out = render_template("slack", self._base_payload())
        assert len(out["blocks"]) == 2
        assert out["blocks"][0]["type"] == "header"
        assert out["blocks"][1]["type"] == "section"

    def test_telegram_has_parse_mode(self):
        out = render_template("telegram", self._base_payload())
        assert out["parse_mode"] == "Markdown"
        assert "*evt*" in out["text"]

    def test_empty_string_template_falls_back(self):
        p = self._base_payload()
        assert render_template("", p) == p


class TestSignPayloadEdgeCases:
    def test_empty_body(self):
        sig = sign_payload("secret", b"")
        assert sig.startswith("sha256=")

    def test_unicode_secret(self):
        sig = sign_payload("sëcrët", b"data")
        assert sig.startswith("sha256=")

    def test_large_body(self):
        body = b"x" * 100_000
        sig = sign_payload("key", body)
        assert len(sig) > 10


# ---------- engine/core/live/kill_switch.py ----------


class TestKillSwitchEdgeCases:
    def test_engage_requires_non_empty_reason(self):
        from engine.core.live.kill_switch import KillSwitch

        ks = KillSwitch(metrics=MagicMock())
        with pytest.raises(ValueError, match="non-empty reason"):
            ks.engage(reason="")

    def test_engage_requires_non_whitespace_reason(self):
        from engine.core.live.kill_switch import KillSwitch

        ks = KillSwitch(metrics=MagicMock())
        with pytest.raises(ValueError, match="non-empty reason"):
            ks.engage(reason="   ")

    def test_disengage_requires_confirmation_token(self):
        from engine.core.live.kill_switch import KillSwitch, KillSwitchError

        ks = KillSwitch(metrics=MagicMock())
        with pytest.raises(KillSwitchError):
            ks.disengage(confirmation="wrong_token")

    def test_disengage_when_already_disengaged_returns_false(self):
        from engine.core.live.kill_switch import KillSwitch

        ks = KillSwitch(metrics=MagicMock())
        result = ks.disengage(confirmation="I_UNDERSTAND_THE_RISK")
        assert result is False

    def test_engage_then_disengage_round_trip(self):
        from engine.core.live.kill_switch import KillSwitch, KillSwitchState

        ks = KillSwitch(metrics=MagicMock())
        assert ks.engage(reason="test") is True
        assert ks.state == KillSwitchState.ENGAGED
        assert ks.disengage(confirmation="I_UNDERSTAND_THE_RISK") is True
        assert ks.state == KillSwitchState.DISENGAGED

    def test_engage_idempotent(self):
        from engine.core.live.kill_switch import KillSwitch

        ks = KillSwitch(metrics=MagicMock())
        assert ks.engage(reason="first") is True
        assert ks.engage(reason="second") is False

    def test_snapshot_reflects_state(self):
        from engine.core.live.kill_switch import KillSwitch, KillSwitchState

        ks = KillSwitch(metrics=MagicMock())
        snap = ks.snapshot()
        assert snap.state == KillSwitchState.DISENGAGED
        assert snap.reason is None

        ks.engage(reason="emergency")
        snap = ks.snapshot()
        assert snap.state == KillSwitchState.ENGAGED
        assert snap.reason == "emergency"

    def test_observer_notified_on_engage(self):
        from engine.core.live.kill_switch import KillSwitch, KillSwitchState

        ks = KillSwitch(metrics=MagicMock())
        observed: list = []

        def _capture(snap):
            observed.append(snap)

        ks.add_observer(_capture)
        ks.engage(reason="test")
        assert len(observed) == 1
        assert observed[0].state == KillSwitchState.ENGAGED

    def test_observer_notified_on_disengage(self):
        from engine.core.live.kill_switch import KillSwitch, KillSwitchState

        ks = KillSwitch(metrics=MagicMock())
        observed: list = []

        def _capture(snap):
            observed.append(snap)

        ks.add_observer(_capture)
        ks.engage(reason="test")
        ks.disengage(confirmation="I_UNDERSTAND_THE_RISK")
        assert len(observed) == 2
        assert observed[1].state == KillSwitchState.DISENGAGED

    def test_failing_observer_does_not_block(self):
        from engine.core.live.kill_switch import KillSwitch

        ks = KillSwitch(metrics=MagicMock())

        def bad_observer(snap):
            raise RuntimeError("observer crashed")

        ks.add_observer(bad_observer)
        ks.engage(reason="test")
        assert ks.is_engaged() is True

    def test_remove_observer(self):
        from engine.core.live.kill_switch import KillSwitch

        ks = KillSwitch(metrics=MagicMock())
        observed = []

        def obs(snap):
            observed.append(1)

        ks.add_observer(obs)
        ks.engage(reason="first")
        assert len(observed) == 1

        ks.remove_observer(obs)
        ks.disengage(confirmation="I_UNDERSTAND_THE_RISK")
        ks.engage(reason="second")
        assert len(observed) == 1

    def test_remove_nonexistent_observer_no_error(self):
        from engine.core.live.kill_switch import KillSwitch

        ks = KillSwitch(metrics=MagicMock())
        ks.remove_observer(lambda: None)

    def test_is_engaged(self):
        from engine.core.live.kill_switch import KillSwitch

        ks = KillSwitch(metrics=MagicMock())
        assert ks.is_engaged() is False
        ks.engage(reason="test")
        assert ks.is_engaged() is True


class TestKillSwitchSingleton:
    def test_reset_for_tests_clears_singleton(self):
        from engine.core.live.kill_switch import _reset_for_tests, get_kill_switch

        ks1 = get_kill_switch()
        _reset_for_tests()
        ks2 = get_kill_switch()
        assert ks1 is not ks2


# ---------- engine/plugins/registry.py ----------


class TestPluginRegistryHelpers:
    def test_discover_strategies_empty_dir(self, tmp_path):
        from engine.plugins.registry import discover_strategies

        result = discover_strategies(tmp_path)
        assert result == {}

    def test_discover_strategies_missing_module(self, tmp_path):
        from engine.plugins.registry import discover_strategies

        strat_dir = tmp_path / "test_strat"
        strat_dir.mkdir()
        (strat_dir / "manifest.yaml").write_text("id: test\nname: Test\nversion: 1.0\n")
        result = discover_strategies(tmp_path)
        assert "test_strat" not in result

    def test_discover_strategies_with_valid_files(self, tmp_path):
        from engine.plugins.registry import discover_strategies

        strat_dir = tmp_path / "valid_strat"
        strat_dir.mkdir()
        (strat_dir / "manifest.yaml").write_text(
            "id: valid\nname: Valid Strategy\nversion: 1.0.0\n"
        )
        (strat_dir / "strategy.py").write_text(
            "class Strategy: pass\n"
        )
        result = discover_strategies(tmp_path)
        assert "valid_strat" in result
        assert result["valid_strat"]["manifest"]["id"] == "valid"

    def test_discover_strategies_nonexistent_dir(self, tmp_path):
        from engine.plugins.registry import discover_strategies

        result = discover_strategies(tmp_path / "does_not_exist")
        assert result == {}

    def test_load_strategy_class_from_file(self, tmp_path):
        from engine.plugins.registry import load_strategy_class

        strategy_file = tmp_path / "strategy.py"
        strategy_file.write_text(
            "class Strategy:\n    name = 'test'\n"
        )
        cls = load_strategy_class(str(strategy_file))
        instance = cls()
        assert instance.name == "test"

    def test_load_strategy_class_missing_file_raises(self, tmp_path):
        from engine.plugins.registry import load_strategy_class

        with pytest.raises((ImportError, FileNotFoundError)):
            load_strategy_class(str(tmp_path / "nonexistent.py"))

    def test_is_scoring_strategy_non_matching(self):
        from engine.plugins.registry import is_scoring_strategy

        assert is_scoring_strategy("not_a_strategy") is False

    def test_is_scoring_strategy_none(self):
        from engine.plugins.registry import is_scoring_strategy

        assert is_scoring_strategy(None) is False


# ---------- engine/observability/metrics.py ----------


class TestNullBackend:
    def test_null_backend_no_error(self):
        from engine.observability.metrics import NullBackend

        backend = NullBackend()
        backend.counter("test.counter")
        backend.gauge("test.gauge", 1.0)
        backend.histogram("test.hist", 0.5)
        with backend.timer("test.timer"):
            pass

    def test_null_backend_rejects_empty_name(self):
        from engine.observability.metrics import NullBackend

        backend = NullBackend()
        with pytest.raises(ValueError, match="non-empty"):
            backend.counter("")
        with pytest.raises(ValueError, match="non-empty"):
            backend.gauge("  ", 1.0)
        with pytest.raises(ValueError, match="non-empty"):
            backend.histogram("", 0.5)


class TestRecordingBackend:
    def test_counter_records(self):
        from engine.observability.metrics import RecordingBackend

        backend = RecordingBackend()
        backend.counter("req", tags={"path": "/api"})
        assert len(backend.counters) == 1

    def test_gauge_records(self):
        from engine.observability.metrics import RecordingBackend

        backend = RecordingBackend()
        backend.gauge("cpu", 0.75)
        assert len(backend.gauges) == 1

    def test_histogram_records(self):
        from engine.observability.metrics import RecordingBackend

        backend = RecordingBackend()
        backend.histogram("latency", 42.5)
        assert len(backend.histograms) == 1

    def test_tag_normalisation(self):
        from engine.observability.metrics import _canonical_tags

        assert _canonical_tags({"b": "2", "a": "1"}) == (("a", "1"), ("b", "2"))
        assert _canonical_tags(None) == ()
        assert _canonical_tags({}) == ()
