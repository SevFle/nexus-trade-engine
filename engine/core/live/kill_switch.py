"""Live trading kill-switch (gh#109).

The kill-switch is the safety floor: any code path that submits an
order to a broker must check it first. When engaged, the engine must
stop generating new orders, attempt to cancel resting orders, and
emit a structured event that surfaces in the runbooks.

Design choices
--------------
- One process-singleton :func:`get_kill_switch` so domain code and
  routes get the same view without threading the instance through
  the DI graph.
- :meth:`engage` is *idempotent* — calling it twice with the same
  reason is a no-op. This matches operator behaviour ("smash the
  red button" should not race with itself).
- :meth:`disengage` requires an explicit ``confirmation`` token so
  that an accidental call doesn't restart trading. The token is
  documented in the runbook.
- Observers are notified on every transition. Failures inside an
  observer are logged but do not block the transition — the switch
  must always work, even if downstream notification breaks.

What's *not* here (explicit follow-ups):
- Persistence — the switch does not survive a restart. The live
  trading loop should re-read its state from a known-good source
  on boot (config, DB, or a heartbeat row).
- Auto-engage triggers (max-loss, drawdown, broker-disconnect).
  Those policies belong in the live-trading orchestration loop and
  call :meth:`engage` when their thresholds trip.
- Per-strategy / per-symbol gates. Today's switch is global; future
  work can add a hierarchical version backed by the same primitive.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum

import structlog

logger = structlog.get_logger()


# Documented disengage token. Operators are expected to type this
# explicitly so a stray script can't toggle the switch off.
_DISENGAGE_TOKEN: str = "I_UNDERSTAND_THE_RISK"


class KillSwitchState(str, Enum):
    DISENGAGED = "disengaged"  # trading allowed
    ENGAGED = "engaged"        # trading blocked


@dataclass(frozen=True)
class KillSwitchSnapshot:
    """Read-only view of the switch — useful for logs / API responses."""

    state: KillSwitchState
    engaged_at: datetime | None
    reason: str | None
    actor: str | None


Observer = Callable[[KillSwitchSnapshot], None]


class KillSwitchError(Exception):
    """Raised on disengage attempts without the proper confirmation."""


class KillSwitch:
    """Process-wide on/off switch for live order submission."""

    def __init__(self) -> None:
        self._state = KillSwitchState.DISENGAGED
        self._engaged_at: datetime | None = None
        self._reason: str | None = None
        self._actor: str | None = None
        self._lock = threading.Lock()
        self._observers: list[Observer] = []

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    @property
    def state(self) -> KillSwitchState:
        return self._state

    def is_engaged(self) -> bool:
        return self._state == KillSwitchState.ENGAGED

    def snapshot(self) -> KillSwitchSnapshot:
        with self._lock:
            return KillSwitchSnapshot(
                state=self._state,
                engaged_at=self._engaged_at,
                reason=self._reason,
                actor=self._actor,
            )

    # ------------------------------------------------------------------
    # Transitions
    # ------------------------------------------------------------------

    def engage(self, *, reason: str, actor: str = "system") -> bool:
        """Engage the switch. Returns True if state changed.

        Idempotent: a second call with no state change returns False
        and does not bump the engaged_at timestamp.
        """
        if not reason or not reason.strip():
            raise ValueError("kill-switch engage requires a non-empty reason")
        with self._lock:
            if self._state == KillSwitchState.ENGAGED:
                logger.warning(
                    "kill_switch.engage_noop",
                    existing_reason=self._reason,
                    new_reason=reason,
                    actor=actor,
                )
                return False
            self._state = KillSwitchState.ENGAGED
            self._engaged_at = datetime.now(tz=UTC)
            self._reason = reason
            self._actor = actor
            snap = KillSwitchSnapshot(
                state=self._state,
                engaged_at=self._engaged_at,
                reason=self._reason,
                actor=self._actor,
            )
        logger.error(
            "kill_switch.engaged",
            reason=reason,
            actor=actor,
            engaged_at=snap.engaged_at.isoformat() if snap.engaged_at else None,
        )
        self._notify(snap)
        return True

    def disengage(self, *, confirmation: str, actor: str = "operator") -> bool:
        """Disengage the switch. Returns True if state changed.

        Requires ``confirmation == _DISENGAGE_TOKEN``. The token is
        deliberately wordy and documented in the runbook so an
        accidental call cannot restart trading.
        """
        if confirmation != _DISENGAGE_TOKEN:
            raise KillSwitchError(
                "disengage requires confirmation token "
                f"{_DISENGAGE_TOKEN!r} (see runbook)"
            )
        with self._lock:
            if self._state == KillSwitchState.DISENGAGED:
                return False
            prior_reason = self._reason
            self._state = KillSwitchState.DISENGAGED
            self._engaged_at = None
            self._reason = None
            self._actor = actor
            snap = KillSwitchSnapshot(
                state=self._state,
                engaged_at=self._engaged_at,
                reason=self._reason,
                actor=self._actor,
            )
        logger.warning(
            "kill_switch.disengaged",
            actor=actor,
            prior_reason=prior_reason,
        )
        self._notify(snap)
        return True

    # ------------------------------------------------------------------
    # Observers
    # ------------------------------------------------------------------

    def add_observer(self, observer: Observer) -> None:
        with self._lock:
            self._observers.append(observer)

    def remove_observer(self, observer: Observer) -> None:
        with self._lock:
            try:
                self._observers.remove(observer)
            except ValueError:
                pass

    def _notify(self, snap: KillSwitchSnapshot) -> None:
        with self._lock:
            observers = list(self._observers)
        for obs in observers:
            try:
                obs(snap)
            except Exception as exc:  # noqa: BLE001 - observer is untrusted
                logger.warning(
                    "kill_switch.observer_failed",
                    observer=getattr(obs, "__name__", repr(obs)),
                    error_type=type(exc).__name__,
                    error_message=str(exc)[:200],
                )


# ---------------------------------------------------------------------------
# Process singleton
# ---------------------------------------------------------------------------


_INSTANCE: KillSwitch | None = None
_INSTANCE_LOCK = threading.Lock()


def get_kill_switch() -> KillSwitch:
    global _INSTANCE  # noqa: PLW0603 - process-wide singleton
    if _INSTANCE is None:
        with _INSTANCE_LOCK:
            if _INSTANCE is None:
                _INSTANCE = KillSwitch()
    return _INSTANCE


def _reset_for_tests() -> None:
    """Test-only: clear the singleton so each test starts fresh."""
    global _INSTANCE  # noqa: PLW0603
    with _INSTANCE_LOCK:
        _INSTANCE = None
