import importlib

from engine.plugins.sandbox.core.context import SandboxContext
from engine.plugins.sandbox.core.policy import SandboxPolicy
from engine.plugins.sandbox.core.violation import (
    ResourceExhausted,
    SandboxViolation,
    SandboxViolationCategory,
)

__all__ = [
    "ResourceExhausted",
    "SandboxContext",
    "SandboxLifecycle",
    "SandboxPolicy",
    "SandboxViolation",
    "SandboxViolationCategory",
]


def __getattr__(name: str):
    lazy = {
        "SandboxLifecycle": "engine.plugins.sandbox.core.lifecycle",
    }
    if name in lazy:
        mod = importlib.import_module(lazy[name])
        value = getattr(mod, name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
