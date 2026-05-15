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


def get_current_plugin_id() -> str | None:
    ctx = get_current_context()
    if ctx is not None:
        return ctx.policy.plugin_id
    return None


def get_thread_local(key: str, default: Any = None) -> Any:
    return getattr(_tls, key, default)


def set_thread_local(key: str, value: Any) -> None:
    setattr(_tls, key, value)


def clear_thread_locals() -> None:
    for attr in list(vars(_tls)):
        delattr(_tls, attr)
