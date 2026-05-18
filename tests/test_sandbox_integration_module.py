from __future__ import annotations

from engine.plugins.sandbox.core.context import SandboxContext
from engine.plugins.sandbox.core.integration import SandboxIntegration
from engine.plugins.sandbox.core.policy import ImportPolicy, SandboxPolicy
from engine.plugins.sandbox.core.state import SandboxTLS
from engine.plugins.sandbox.monitoring.metrics import SandboxMetricsCollector


class TestSandboxIntegration:
    def test_create_policy_untrusted(self) -> None:
        policy = SandboxIntegration.create_policy(
            plugin_id="test", trust_level="untrusted"
        )
        assert policy.plugin_id == "test"
        assert policy.trust_level == "untrusted"

    def test_create_policy_trusted(self) -> None:
        policy = SandboxIntegration.create_policy(
            plugin_id="trusted_p", trust_level="trusted_full"
        )
        assert policy.trust_level == "trusted_full"
        assert policy.resource_policy.max_cpu_seconds > 30

    def test_create_policy_with_overrides(self) -> None:
        policy = SandboxIntegration.create_policy(
            plugin_id="test",
            trust_level="untrusted",
            allowed_endpoints=["api.example.com"],
            max_cpu_seconds=120,
            max_memory_bytes=1024 * 1024 * 1024,
            blocked_modules={"os", "subprocess"},
        )
        assert policy.network_policy.allowed_endpoints == ["api.example.com"]
        assert policy.resource_policy.max_cpu_seconds == 120
        assert "os" in policy.import_policy.blocked_modules

    def test_create_policy_invalid_trust_defaults_untrusted(self) -> None:
        policy = SandboxIntegration.create_policy(
            plugin_id="test", trust_level="invalid_level"
        )
        assert policy.trust_level == "untrusted"

    def test_register_and_unregister(self) -> None:
        integ = SandboxIntegration()
        policy = SandboxPolicy(plugin_id="test_reg")
        ctx = SandboxContext(policy)
        try:
            lc = integ.register(ctx)
            assert lc is not None
            assert integ.get_context("test_reg") is ctx
            assert integ.get_lifecycle("test_reg") is lc
            integ.unregister("test_reg")
            assert integ.get_context("test_reg") is None
        finally:
            ctx.cleanup()

    def test_activate_and_deactivate(self) -> None:
        integ = SandboxIntegration()
        policy = SandboxPolicy(
            plugin_id="test_act",
            import_policy=ImportPolicy(blocked_modules={f"mod_{i}" for i in range(15)}),
        )
        ctx = SandboxContext(policy)
        try:
            integ.register(ctx)
            integ.activate("test_act")
            assert "test_act" in integ.get_active_plugins()
            integ.deactivate("test_act")
            assert "test_act" not in integ.get_active_plugins()
        finally:
            integ.unregister("test_act")
            ctx.cleanup()

    def test_get_metrics(self) -> None:
        metrics = SandboxMetricsCollector()
        integ = SandboxIntegration(metrics_collector=metrics)
        policy = SandboxPolicy(
            plugin_id="test_met",
            import_policy=ImportPolicy(blocked_modules={f"mod_{i}" for i in range(15)}),
        )
        ctx = SandboxContext(policy)
        try:
            integ.register(ctx)
            integ.activate("test_met")
            integ.deactivate("test_met")
            m = integ.get_metrics("test_met")
            assert m is not None
            assert m["total_evaluations"] == 1
        finally:
            integ.unregister("test_met")
            ctx.cleanup()

    def test_get_all_metrics(self) -> None:
        metrics = SandboxMetricsCollector()
        integ = SandboxIntegration(metrics_collector=metrics)
        all_m = integ.get_all_metrics()
        assert isinstance(all_m, dict)

    def test_get_all_states(self) -> None:
        integ = SandboxIntegration()
        policy = SandboxPolicy(plugin_id="test_st")
        ctx = SandboxContext(policy)
        try:
            integ.register(ctx)
            states = integ.get_all_states()
            assert "test_st" in states
        finally:
            integ.unregister("test_st")
            ctx.cleanup()

    def test_shutdown(self) -> None:
        integ = SandboxIntegration()
        for pid in ("p1", "p2"):
            policy = SandboxPolicy(plugin_id=pid)
            ctx = SandboxContext(policy)
            integ.register(ctx)
        integ.shutdown()
        assert integ.get_active_plugins() == []

    def test_activate_nonexistent(self) -> None:
        integ = SandboxIntegration()
        result = integ.activate("nonexistent")
        assert result is None

    def test_custom_tls(self) -> None:
        tls = SandboxTLS()
        integ = SandboxIntegration(tls=tls)
        assert integ._tls is tls

    def test_get_context_nonexistent(self) -> None:
        integ = SandboxIntegration()
        assert integ.get_context("nonexistent") is None
