from __future__ import annotations

import enum
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

from engine.plugins.sandbox.core.state import SandboxTLS, get_default_tls

if TYPE_CHECKING:
    from engine.plugins.sandbox.core.context import SandboxContext

logger = structlog.get_logger()


class SandboxPhase(enum.Enum):
    CREATED = "created"
    ACTIVATING = "activating"
    ACTIVE = "active"
    DEACTIVATING = "deactivating"
    DEACTIVATED = "deactivated"
    FAILED = "failed"
    CLEANED_UP = "cleaned_up"


@dataclass
class LifecycleEvent:
    timestamp: float
    phase: SandboxPhase
    previous_phase: SandboxPhase | None
    plugin_id: str
    detail: str | None = None


@dataclass
class SandboxLifecycle:
    plugin_id: str
    phase: SandboxPhase = SandboxPhase.CREATED
    _events: list[LifecycleEvent] = field(default_factory=list)
    _tls: SandboxTLS = field(default_factory=get_default_tls)
    _context: SandboxContext | None = field(default=None, repr=False)
    _created_at: float = field(default_factory=time.monotonic)
    _activated_at: float | None = field(default=None)
    _deactivated_at: float | None = field(default=None)

    def bind(self, context: SandboxContext) -> None:
        self._context = context
        self._record(SandboxPhase.CREATED, None)

    def activate(self) -> None:
        if self.phase in (SandboxPhase.ACTIVE, SandboxPhase.ACTIVATING):
            return
        previous = self.phase
        self._record(SandboxPhase.ACTIVATING, previous)
        try:
            if self._context is not None:
                self._tls.bind(self._context)
                self._context.activate()
            self._activated_at = time.monotonic()
            self._record(SandboxPhase.ACTIVE, SandboxPhase.ACTIVATING)
        except Exception:
            self._record(SandboxPhase.FAILED, SandboxPhase.ACTIVATING)
            self._tls.unbind()
            raise

    def deactivate(self) -> None:
        if self.phase in (
            SandboxPhase.DEACTIVATED,
            SandboxPhase.DEACTIVATING,
            SandboxPhase.CREATED,
        ):
            return
        previous = self.phase
        self._record(SandboxPhase.DEACTIVATING, previous)
        try:
            if self._context is not None:
                self._context.deactivate()
            self._deactivated_at = time.monotonic()
            self._tls.unbind()
            self._record(SandboxPhase.DEACTIVATED, SandboxPhase.DEACTIVATING)
        except Exception:
            self._record(SandboxPhase.FAILED, SandboxPhase.DEACTIVATING)
            raise

    def cleanup(self) -> None:
        if self._context is not None:
            self._context.cleanup()
        self._tls.unbind()
        self._record(SandboxPhase.CLEANED_UP, self.phase)

    @property
    def active_duration(self) -> float | None:
        if self._activated_at is None:
            return None
        end = self._deactivated_at or time.monotonic()
        return end - self._activated_at

    @property
    def total_duration(self) -> float:
        return time.monotonic() - self._created_at

    @property
    def events(self) -> list[LifecycleEvent]:
        return list(self._events)

    def _record(
        self,
        phase: SandboxPhase,
        previous: SandboxPhase | None,
        detail: str | None = None,
    ) -> None:
        self.phase = phase
        event = LifecycleEvent(
            timestamp=time.monotonic(),
            phase=phase,
            previous_phase=previous,
            plugin_id=self.plugin_id,
            detail=detail,
        )
        self._events.append(event)
        logger.debug(
            "sandbox.lifecycle",
            plugin_id=self.plugin_id,
            phase=phase.value,
            previous=previous.value if previous else None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "plugin_id": self.plugin_id,
            "phase": self.phase.value,
            "active_duration": self.active_duration,
            "total_duration": self.total_duration,
            "event_count": len(self._events),
            "created_at": self._created_at,
            "activated_at": self._activated_at,
            "deactivated_at": self._deactivated_at,
        }


class LifecycleManager:
    def __init__(self) -> None:
        self._lifecycles: dict[str, SandboxLifecycle] = {}

    def create(self, context: SandboxContext) -> SandboxLifecycle:
        plugin_id = context.policy.plugin_id
        lc = SandboxLifecycle(plugin_id=plugin_id)
        lc.bind(context)
        self._lifecycles[plugin_id] = lc
        return lc

    def get(self, plugin_id: str) -> SandboxLifecycle | None:
        return self._lifecycles.get(plugin_id)

    def remove(self, plugin_id: str) -> SandboxLifecycle | None:
        return self._lifecycles.pop(plugin_id, None)

    def activate(self, plugin_id: str) -> None:
        lc = self._lifecycles.get(plugin_id)
        if lc is not None:
            lc.activate()

    def deactivate(self, plugin_id: str) -> None:
        lc = self._lifecycles.get(plugin_id)
        if lc is not None:
            lc.deactivate()

    def cleanup(self, plugin_id: str) -> None:
        lc = self._lifecycles.get(plugin_id)
        if lc is not None:
            lc.cleanup()

    def cleanup_all(self) -> None:
        for lc in self._lifecycles.values():
            lc.cleanup()
        self._lifecycles.clear()

    def get_active(self) -> list[str]:
        return [
            lc.plugin_id
            for lc in self._lifecycles.values()
            if lc.phase == SandboxPhase.ACTIVE
        ]

    def get_all_states(self) -> dict[str, dict[str, Any]]:
        return {pid: lc.to_dict() for pid, lc in self._lifecycles.items()}
