"""Live trading primitives (gh#109).

Today this exposes the kill-switch — the safety floor every live
trading deployment must check before submitting an order. Live-loop
orchestration, reconciliation, and recovery are tracked as follow-ups
under the same issue and consume this primitive.
"""

from engine.core.live.kill_switch import (
    KillSwitch,
    KillSwitchState,
    get_kill_switch,
)

__all__ = ["KillSwitch", "KillSwitchState", "get_kill_switch"]
