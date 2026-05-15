from __future__ import annotations

from typing import TYPE_CHECKING, Any

from engine.plugins.sandbox.core.context import SandboxContext
from engine.plugins.sandbox.core.policy import SandboxPolicy
from engine.plugins.trust_levels import TrustLevel

if TYPE_CHECKING:
    from collections.abc import Callable


def create_sandbox_context(
    policy: SandboxPolicy,
) -> SandboxContext:
    return SandboxContext(policy)


def create_default_policy(
    plugin_id: str = "unknown",
    trust_level: str = "untrusted",
) -> SandboxPolicy:
    try:
        tl = TrustLevel(trust_level)
    except ValueError:
        tl = TrustLevel.UNTRUSTED
    return SandboxPolicy.from_trust_level(tl, plugin_id=plugin_id)


def execute_in_sandbox(
    fn: Callable[..., Any],
    policy: SandboxPolicy,
    *args: Any,
    **kwargs: Any,
) -> Any:
    ctx = create_sandbox_context(policy)
    ctx.activate()
    try:
        return fn(*args, **kwargs)
    finally:
        ctx.deactivate()
        ctx.cleanup()


def get_violation_summary(
    context: SandboxContext,
) -> dict[str, Any]:
    events = context.event_logger.get_events()
    by_category: dict[str, int] = {}
    for event in events:
        cat = event.category.value
        by_category[cat] = by_category.get(cat, 0) + 1
    return {
        "plugin_id": context.policy.plugin_id,
        "total_violations": len(events),
        "by_category": by_category,
    }
