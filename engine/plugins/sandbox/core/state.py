from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from engine.plugins.sandbox.core.context import SandboxContext

_tls = threading.local()


def get_current_context() -> SandboxContext | None:
    return getattr(_tls, "active_context", None)


def set_current_context(ctx: SandboxContext | None) -> None:
    _tls.active_context = ctx


def is_sandbox_active() -> bool:
    ctx = get_current_context()
    return ctx is not None and ctx.is_active


def get_active_plugin_id() -> str | None:
    ctx = get_current_context()
    if ctx is not None:
        return ctx.policy.plugin_id
    return None


def get_active_trust_level() -> str | None:
    ctx = get_current_context()
    if ctx is not None:
        return ctx.policy.trust_level
    return None


class SandboxTLS:
    def __init__(self) -> None:
        self._local = threading.local()

    def bind(self, ctx: SandboxContext) -> None:
        self._local.active_context = ctx

    def unbind(self) -> None:
        self._local.active_context = None

    @property
    def context(self) -> SandboxContext | None:
        return getattr(self._local, "active_context", None)

    @property
    def plugin_id(self) -> str | None:
        ctx = self.context
        return ctx.policy.plugin_id if ctx else None

    @property
    def trust_level(self) -> str | None:
        ctx = self.context
        return ctx.policy.trust_level if ctx else None

    @property
    def is_active(self) -> bool:
        ctx = self.context
        return ctx is not None and ctx.is_active

    def snapshot(self) -> dict[str, Any]:
        return {
            "plugin_id": self.plugin_id,
            "trust_level": self.trust_level,
            "is_active": self.is_active,
        }


_default_tls = SandboxTLS()


def get_default_tls() -> SandboxTLS:
    return _default_tls
