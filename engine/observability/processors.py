"""structlog processors: service metadata, correlation merge, sampling."""

from __future__ import annotations

import random
from typing import TYPE_CHECKING, Any

from structlog import DropEvent

from engine.config import settings
from engine.observability import context as ctx

if TYPE_CHECKING:
    from structlog.typing import EventDict, WrappedLogger

# structlog normalizes `logger.exception(...)` to method_name="error",
# so "exception" never reaches the filter — keep just the actual names
# that arrive.
_ALWAYS_KEEP = frozenset({"warning", "warn", "error", "critical"})


def add_service_metadata(
    _logger: WrappedLogger, _name: str, event_dict: EventDict
) -> EventDict:
    """Attach service / env / version to every record without overwriting."""
    event_dict.setdefault("service", settings.app_name)
    event_dict.setdefault("env", settings.app_env)
    event_dict.setdefault("version", settings.app_version)
    return event_dict


def add_correlation_context(
    _logger: WrappedLogger, _name: str, event_dict: EventDict
) -> EventDict:
    """Merge correlation/request/span/user/domain ids into the record."""
    snap = ctx.snapshot()
    for key, value in snap.items():
        event_dict.setdefault(key, value)
    return event_dict


def sampling_filter(
    _logger: WrappedLogger, method_name: str, event_dict: EventDict
) -> EventDict:
    """Drop a configurable fraction of info/debug records.

    warn/error/critical always pass. info/debug pass according to
    `settings.log_sampling_info` and `settings.log_sampling_debug`.
    1.0 = keep all, 0.0 = drop all.
    """
    level = method_name.lower()
    if level in _ALWAYS_KEEP:
        return event_dict
    if level == "info":
        rate = settings.log_sampling_info
    elif level == "debug":
        rate = settings.log_sampling_debug
    else:
        return event_dict
    if rate >= 1.0:
        return event_dict
    if rate <= 0.0 or random.random() >= rate:  # noqa: S311 - non-crypto sampling
        raise DropEvent
    return event_dict


# Hint to type-checkers in callers that imports work without runtime deps.
_ = Any

__all__ = [
    "add_correlation_context",
    "add_service_metadata",
    "sampling_filter",
]
