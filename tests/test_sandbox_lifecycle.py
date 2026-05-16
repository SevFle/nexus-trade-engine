from __future__ import annotations

import time

import pytest

from engine.plugins.sandbox.core.context import SandboxContext
from engine.plugins.sandbox.core.lifecycle import (
    LifecycleEvent,
    LifecycleManager,
    SandboxLifecycle,
    SandboxPhase,
)
from engine.plugins.sandbox.core.policy import SandboxPolicy


class TestSandboxPhase:
    def test_all_phases(self) -> None:
        assert SandboxPhase.CREATED.value == "created"
        assert SandboxPhase.ACTIVATING.value == "activating"
        assert SandboxPhase.ACTIVE.value == "active"
        assert SandboxPhase.DEACTIVATING.value == "deactivating"
        assert SandboxPhase.DEACTIVATED.value == "deactivated"
        assert SandboxPhase.FAILED.value == "failed"
        assert SandboxPhase.CLEANED_UP.value == "cleaned_up"


class TestLifecycleEvent:
    def test_fields(self) -> None:
        event = LifecycleEvent(
            timestamp=time.monotonic(),
            phase=SandboxPhase.CREATED,
            previous_phase=None,
            plugin_id="test",
            detail="created",
        )
        assert event.phase is SandboxPhase.CREATED
        assert event.previous_phase is None
        assert event.plugin_id == "test"
        assert event.detail == "created"


class TestSandboxLifecycle:
    def test_initial_state(self) -> None:
        lc = SandboxLifecycle(plugin_id="test")
        assert lc.phase is SandboxPhase.CREATED
        assert lc.active_duration is None
        assert lc.total_duration > 0
        assert len(lc.events) == 0

    def test_bind_records_event(self) -> None:
        lc = SandboxLifecycle(plugin_id="test")
        policy = SandboxPolicy(plugin_id="test")
        ctx = SandboxContext(policy)
        try:
            lc.bind(ctx)
            assert len(lc.events) == 1
            assert lc.events[0].phase is SandboxPhase.CREATED
        finally:
            ctx.cleanup()

    def test_activate_transitions(self) -> None:
        lc = SandboxLifecycle(plugin_id="test")
        policy = SandboxPolicy(plugin_id="test")
        ctx = SandboxContext(policy)
        try:
            lc.bind(ctx)
            lc.activate()
            assert lc.phase is SandboxPhase.ACTIVE
            assert lc._activated_at is not None
            assert lc.active_duration is not None
        finally:
            lc.deactivate()
            ctx.cleanup()

    def test_deactivate_transitions(self) -> None:
        lc = SandboxLifecycle(plugin_id="test")
        policy = SandboxPolicy(plugin_id="test")
        ctx = SandboxContext(policy)
        try:
            lc.bind(ctx)
            lc.activate()
            lc.deactivate()
            assert lc.phase is SandboxPhase.DEACTIVATED
            assert lc._deactivated_at is not None
        finally:
            ctx.cleanup()

    def test_cleanup_transitions(self) -> None:
        lc = SandboxLifecycle(plugin_id="test")
        policy = SandboxPolicy(plugin_id="test")
        ctx = SandboxContext(policy)
        try:
            lc.bind(ctx)
            lc.activate()
            lc.deactivate()
        finally:
            lc.cleanup()
            ctx.cleanup()
        assert lc.phase is SandboxPhase.CLEANED_UP

    def test_activate_idempotent(self) -> None:
        lc = SandboxLifecycle(plugin_id="test")
        policy = SandboxPolicy(plugin_id="test")
        ctx = SandboxContext(policy)
        try:
            lc.bind(ctx)
            lc.activate()
            lc.activate()
            assert lc.phase is SandboxPhase.ACTIVE
        finally:
            lc.deactivate()
            ctx.cleanup()

    def test_deactivate_idempotent(self) -> None:
        lc = SandboxLifecycle(plugin_id="test")
        assert lc.phase is SandboxPhase.CREATED
        lc.deactivate()
        assert lc.phase is SandboxPhase.CREATED

    def test_to_dict(self) -> None:
        lc = SandboxLifecycle(plugin_id="test_plugin")
        d = lc.to_dict()
        assert d["plugin_id"] == "test_plugin"
        assert d["phase"] == "created"
        assert d["active_duration"] is None
        assert d["total_duration"] > 0
        assert d["event_count"] == 0

    def test_event_history(self) -> None:
        lc = SandboxLifecycle(plugin_id="test")
        policy = SandboxPolicy(plugin_id="test")
        ctx = SandboxContext(policy)
        try:
            lc.bind(ctx)
            lc.activate()
            lc.deactivate()
            phases = [e.phase for e in lc.events]
            assert SandboxPhase.CREATED in phases
            assert SandboxPhase.ACTIVATING in phases
            assert SandboxPhase.ACTIVE in phases
            assert SandboxPhase.DEACTIVATING in phases
            assert SandboxPhase.DEACTIVATED in phases
        finally:
            ctx.cleanup()


class TestLifecycleManager:
    def test_create_and_get(self) -> None:
        mgr = LifecycleManager()
        policy = SandboxPolicy(plugin_id="test")
        ctx = SandboxContext(policy)
        try:
            lc = mgr.create(ctx)
            assert lc is not None
            assert mgr.get("test") is lc
        finally:
            ctx.cleanup()

    def test_get_nonexistent(self) -> None:
        mgr = LifecycleManager()
        assert mgr.get("nonexistent") is None

    def test_remove(self) -> None:
        mgr = LifecycleManager()
        policy = SandboxPolicy(plugin_id="test")
        ctx = SandboxContext(policy)
        try:
            mgr.create(ctx)
            removed = mgr.remove("test")
            assert removed is not None
            assert mgr.get("test") is None
        finally:
            ctx.cleanup()

    def test_remove_nonexistent(self) -> None:
        mgr = LifecycleManager()
        assert mgr.remove("nonexistent") is None

    def test_activate_deactivate(self) -> None:
        mgr = LifecycleManager()
        policy = SandboxPolicy(plugin_id="test")
        ctx = SandboxContext(policy)
        try:
            mgr.create(ctx)
            mgr.activate("test")
            assert mgr.get("test").phase is SandboxPhase.ACTIVE
            mgr.deactivate("test")
            assert mgr.get("test").phase is SandboxPhase.DEACTIVATED
        finally:
            ctx.cleanup()

    def test_get_active(self) -> None:
        mgr = LifecycleManager()
        policy1 = SandboxPolicy(plugin_id="p1")
        policy2 = SandboxPolicy(plugin_id="p2")
        ctx1 = SandboxContext(policy1)
        ctx2 = SandboxContext(policy2)
        try:
            mgr.create(ctx1)
            mgr.create(ctx2)
            mgr.activate("p1")
            active = mgr.get_active()
            assert "p1" in active
            assert "p2" not in active
            mgr.deactivate("p1")
        finally:
            mgr.cleanup_all()
            ctx1.cleanup()
            ctx2.cleanup()

    def test_cleanup_all(self) -> None:
        mgr = LifecycleManager()
        for pid in ("p1", "p2", "p3"):
            policy = SandboxPolicy(plugin_id=pid)
            ctx = SandboxContext(policy)
            mgr.create(ctx)
        mgr.cleanup_all()
        assert mgr.get_active() == []

    def test_get_all_states(self) -> None:
        mgr = LifecycleManager()
        policy = SandboxPolicy(plugin_id="p1")
        ctx = SandboxContext(policy)
        try:
            mgr.create(ctx)
            states = mgr.get_all_states()
            assert "p1" in states
            assert states["p1"]["phase"] == "created"
        finally:
            mgr.cleanup_all()
            ctx.cleanup()

    def test_activate_nonexistent(self) -> None:
        mgr = LifecycleManager()
        mgr.activate("nonexistent")

    def test_deactivate_nonexistent(self) -> None:
        mgr = LifecycleManager()
        mgr.deactivate("nonexistent")

    def test_cleanup_nonexistent(self) -> None:
        mgr = LifecycleManager()
        mgr.cleanup("nonexistent")
