from __future__ import annotations

import threading
import time

from engine.plugins.sandbox.core.context import SandboxContext
from engine.plugins.sandbox.core.policy import SandboxPolicy
from engine.plugins.sandbox.core.state import (
    SandboxTLS,
    get_active_plugin_id,
    get_active_trust_level,
    get_current_context,
    get_default_tls,
    is_sandbox_active,
    set_current_context,
)


class TestModuleLevelFunctions:
    def test_initial_state_is_none(self) -> None:
        assert get_current_context() is None
        assert is_sandbox_active() is False
        assert get_active_plugin_id() is None
        assert get_active_trust_level() is None

    def test_set_and_clear_context(self) -> None:
        set_current_context("mock")
        assert get_current_context() == "mock"
        set_current_context(None)
        assert get_current_context() is None


class TestSandboxTLS:
    def test_initial_state(self) -> None:
        tls = SandboxTLS()
        assert tls.context is None
        assert tls.plugin_id is None
        assert tls.trust_level is None
        assert tls.is_active is False

    def test_bind_and_unbind(self) -> None:
        tls = SandboxTLS()
        tls.bind("mock_ctx")
        assert tls.context == "mock_ctx"
        tls.unbind()
        assert tls.context is None

    def test_snapshot_empty(self) -> None:
        tls = SandboxTLS()
        snap = tls.snapshot()
        assert snap["plugin_id"] is None
        assert snap["trust_level"] is None
        assert snap["is_active"] is False

    def test_get_default_tls_returns_same_instance(self) -> None:
        tls1 = get_default_tls()
        tls2 = get_default_tls()
        assert tls1 is tls2

    def test_concurrent_tls_isolation(self) -> None:
        tls = SandboxTLS()
        results: dict[str, str | None] = {}
        policy_a = SandboxPolicy(plugin_id="plugin_a")
        policy_b = SandboxPolicy(plugin_id="plugin_b")
        ctx_a = SandboxContext(policy_a)
        ctx_b = SandboxContext(policy_b)

        def thread_fn(ctx: SandboxContext, name: str) -> None:
            tls.bind(ctx)
            time.sleep(0.01)
            results[name] = tls.plugin_id
            tls.unbind()

        t1 = threading.Thread(target=thread_fn, args=(ctx_a, "plugin_a"))
        t2 = threading.Thread(target=thread_fn, args=(ctx_b, "plugin_b"))
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        ctx_a.cleanup()
        ctx_b.cleanup()
        assert results["plugin_a"] == "plugin_a"
        assert results["plugin_b"] == "plugin_b"
