"""Unit tests for the live-trading kill-switch (gh#109)."""

from __future__ import annotations

import pytest

from engine.core.live import KillSwitch, KillSwitchState, get_kill_switch
from engine.core.live.kill_switch import (
    _DISENGAGE_TOKEN,
    KillSwitchError,
    _reset_for_tests,
)


@pytest.fixture(autouse=True)
def _reset_singleton():
    _reset_for_tests()
    yield
    _reset_for_tests()


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestInitial:
    def test_starts_disengaged(self):
        ks = KillSwitch()
        assert ks.state == KillSwitchState.DISENGAGED
        assert ks.is_engaged() is False
        snap = ks.snapshot()
        assert snap.engaged_at is None
        assert snap.reason is None
        assert snap.actor is None


# ---------------------------------------------------------------------------
# Engage
# ---------------------------------------------------------------------------


class TestEngage:
    def test_engage_changes_state(self):
        ks = KillSwitch()
        changed = ks.engage(reason="test", actor="sysop")
        assert changed is True
        assert ks.is_engaged()
        snap = ks.snapshot()
        assert snap.reason == "test"
        assert snap.actor == "sysop"
        assert snap.engaged_at is not None

    def test_engage_is_idempotent(self):
        ks = KillSwitch()
        ks.engage(reason="first")
        first_snap = ks.snapshot()
        changed = ks.engage(reason="second")
        assert changed is False
        # State unchanged: original reason + timestamp preserved.
        snap = ks.snapshot()
        assert snap.reason == "first"
        assert snap.engaged_at == first_snap.engaged_at

    def test_engage_requires_reason(self):
        ks = KillSwitch()
        with pytest.raises(ValueError):
            ks.engage(reason="")
        with pytest.raises(ValueError):
            ks.engage(reason="   ")


# ---------------------------------------------------------------------------
# Disengage
# ---------------------------------------------------------------------------


class TestDisengage:
    def test_disengage_with_token(self):
        ks = KillSwitch()
        ks.engage(reason="test")
        changed = ks.disengage(confirmation=_DISENGAGE_TOKEN)
        assert changed is True
        assert ks.is_engaged() is False
        snap = ks.snapshot()
        assert snap.reason is None
        assert snap.engaged_at is None

    def test_disengage_without_token_raises(self):
        ks = KillSwitch()
        ks.engage(reason="test")
        with pytest.raises(KillSwitchError):
            ks.disengage(confirmation="please")
        # Switch still engaged.
        assert ks.is_engaged()

    def test_disengage_when_already_disengaged_is_noop(self):
        ks = KillSwitch()
        changed = ks.disengage(confirmation=_DISENGAGE_TOKEN)
        assert changed is False


# ---------------------------------------------------------------------------
# Observers
# ---------------------------------------------------------------------------


class TestObservers:
    def test_observer_called_on_engage_and_disengage(self):
        ks = KillSwitch()
        events: list[KillSwitchState] = []

        def cb(snap):
            events.append(snap.state)

        ks.add_observer(cb)
        ks.engage(reason="test")
        ks.disengage(confirmation=_DISENGAGE_TOKEN)
        assert events == [KillSwitchState.ENGAGED, KillSwitchState.DISENGAGED]

    def test_observer_failure_does_not_block_transition(self):
        ks = KillSwitch()

        def angry(snap):
            raise RuntimeError("nope")

        ks.add_observer(angry)
        # The transition still completes despite the observer raising.
        assert ks.engage(reason="test") is True
        assert ks.is_engaged()

    def test_remove_observer(self):
        ks = KillSwitch()
        seen: list = []

        def cb(snap):
            seen.append(snap)

        ks.add_observer(cb)
        ks.remove_observer(cb)
        ks.engage(reason="test")
        assert seen == []

    def test_idempotent_engage_does_not_notify(self):
        ks = KillSwitch()
        seen: list = []
        ks.add_observer(seen.append)
        ks.engage(reason="first")
        ks.engage(reason="second")  # no-op
        assert len(seen) == 1


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------


class TestSingleton:
    def test_get_kill_switch_idempotent(self):
        a = get_kill_switch()
        b = get_kill_switch()
        assert a is b

    def test_singleton_state_persists_across_calls(self):
        a = get_kill_switch()
        a.engage(reason="boom")
        b = get_kill_switch()
        assert b.is_engaged()
