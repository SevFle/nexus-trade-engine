from __future__ import annotations

from typing import TYPE_CHECKING, Any

from engine.plugins.sandbox.core.context import SandboxContext
from engine.plugins.sandbox.core.state import (
    clear_thread_locals,
    get_current_context,
    set_current_context,
)

if TYPE_CHECKING:
    from engine.plugins.sandbox.core.policy import SandboxPolicy


class SandboxLifecycle:
    def __init__(self, context: SandboxContext) -> None:
        self._context = context

    @classmethod
    def from_policy(cls, policy: SandboxPolicy) -> SandboxLifecycle:
        return cls(SandboxContext(policy))

    @property
    def context(self) -> SandboxContext:
        return self._context

    def enter(self) -> SandboxContext:
        set_current_context(self._context)
        self._context.activate()
        return self._context

    def exit(self) -> None:
        self._context.deactivate()
        if get_current_context() is self._context:
            set_current_context(None)

    def execute(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        self.enter()
        try:
            return fn(*args, **kwargs)
        finally:
            self.exit()

    def cleanup(self) -> None:
        self.exit()
        self._context.cleanup()
        clear_thread_locals()

    def __enter__(self) -> SandboxLifecycle:
        self.enter()
        return self

    def __exit__(self, *args: Any) -> None:
        self.exit()
