"""Tests for KillSwitch metrics emission (gh#109 follow-up).

The switch emits the following metrics through the active
``MetricsBackend``:

- ``kill_switch.engaged`` — counter, exactly once per real engage
  transition (idempotent re-engages do not increment it).
- ``kill_switch.engage_noop`` — counter, on each idempotent re-engage.
- ``kill_switch.disengaged`` — counter, exactly once per real
  disengage transition.
- ``kill_switch.state`` — gauge, 1.0 while engaged, 0.0 while
  disengaged.

All counters are tagged with ``actor``.
"""

from __future__ import annotations

import pytest

from engine.core.live.kill_switch import KillSwitch
from engine.observability.metrics import RecordingBackend

_DISENGAGE_TOKEN = "I_UNDERSTAND_THE_RISK"


def _counter_total(backend: RecordingBackend, name: str) -> float:
    return sum(v for (n, _t), v in backend.counters.items() if n == name)


def _counter_with(
    backend: RecordingBackend, name: str, tags: dict[str, str]
) -> float:
    expected = tuple(sorted(tags.items()))
    return sum(
        v
        for (n, t), v in backend.counters.items()
        if n == name and all(item in t for item in expected)
    )


def _gauge_value(backend: RecordingBackend, name: str) -> float | None:
    matches = [v for (n, _t), v in backend.gauges.items() if n == name]
    return matches[-1] if matches else None


@pytest.fixture
def metrics() -> RecordingBackend:
    return RecordingBackend()


class TestEngageMetrics:
    def test_first_engage_increments_counter_and_sets_state_gauge(self, metrics):
        ks = KillSwitch(metrics=metrics)

        changed = ks.engage(reason="manual", actor="operator")

        assert changed is True
        assert (
            _counter_with(
                metrics, "kill_switch.engaged", {"actor": "operator"}
            )
            == 1
        )
        assert _counter_total(metrics, "kill_switch.engage_noop") == 0
        assert _gauge_value(metrics, "kill_switch.state") == 1.0

    def test_repeat_engage_records_engage_noop_only(self, metrics):
        ks = KillSwitch(metrics=metrics)
        ks.engage(reason="manual", actor="operator")
        ks.engage(reason="manual", actor="live_loop")  # already engaged

        # Real engagements: still exactly one.
        assert _counter_total(metrics, "kill_switch.engaged") == 1
        # Noop bumped under the second actor.
        assert (
            _counter_with(
                metrics, "kill_switch.engage_noop", {"actor": "live_loop"}
            )
            == 1
        )
        # Gauge stayed at 1.
        assert _gauge_value(metrics, "kill_switch.state") == 1.0


class TestDisengageMetrics:
    def test_real_disengage_increments_counter_and_clears_state(self, metrics):
        ks = KillSwitch(metrics=metrics)
        ks.engage(reason="manual", actor="operator")

        changed = ks.disengage(
            confirmation=_DISENGAGE_TOKEN, actor="operator"
        )

        assert changed is True
        assert (
            _counter_with(
                metrics, "kill_switch.disengaged", {"actor": "operator"}
            )
            == 1
        )
        assert _gauge_value(metrics, "kill_switch.state") == 0.0

    def test_disengage_when_already_disengaged_emits_nothing(self, metrics):
        ks = KillSwitch(metrics=metrics)

        changed = ks.disengage(
            confirmation=_DISENGAGE_TOKEN, actor="operator"
        )

        assert changed is False
        assert _counter_total(metrics, "kill_switch.disengaged") == 0
        # Never engaged → gauge was never set.
        assert _gauge_value(metrics, "kill_switch.state") is None


class TestRoundTrip:
    def test_engage_disengage_engage_records_two_engaged_two_state_writes(
        self, metrics
    ):
        ks = KillSwitch(metrics=metrics)
        ks.engage(reason="r1", actor="op")
        ks.disengage(confirmation=_DISENGAGE_TOKEN, actor="op")
        ks.engage(reason="r2", actor="op")

        assert _counter_total(metrics, "kill_switch.engaged") == 2
        assert _counter_total(metrics, "kill_switch.disengaged") == 1
        # Final state gauge reflects the engaged status.
        assert _gauge_value(metrics, "kill_switch.state") == 1.0


class TestDefaultBackend:
    def test_resolves_get_metrics_when_not_injected(self):
        from engine.observability.metrics import NullBackend, set_metrics

        recording = RecordingBackend()
        set_metrics(recording)
        try:
            ks = KillSwitch()
            ks.engage(reason="manual", actor="operator")
            assert _counter_total(recording, "kill_switch.engaged") == 1
        finally:
            set_metrics(NullBackend())
