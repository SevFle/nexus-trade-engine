"""Exceptions raised by the plugin sandbox and related enforcement layers.

These are kept in a dedicated module (rather than inside ``sandbox``) so they
can be imported by callers and tests without pulling in the heavy sandbox
machinery — and without giving sandboxed strategy code a reason to import the
sandbox package (which is itself blocked by the restricted importer).
"""

from __future__ import annotations


class ResourceLimitExceededError(Exception):
    """Raised when a sandboxed strategy exceeds a declared resource limit.

    Most notably this is raised by :class:`engine.plugins.sandbox.CpuTimeLimiter`
    when its ``SIGXCPU`` guard fires — i.e. a strategy has consumed more
    *actual CPU time* (user + system, measured via ``RLIMIT_CPU``) than the
    manifest's ``resources.max_cpu_seconds`` budget.

    It is deliberately distinct from the wall-clock :class:`TimeoutError`
    raised by the asyncio timeout: ``RLIMIT_CPU`` counts CPU consumption, not
    elapsed time, so it catches a strategy that spins in a tight compute loop
    and never yields to the event loop (a loop the asyncio timeout cannot
    preempt because the loop itself is blocked).
    """

    def __init__(self, limit: float, *, resource: str = "CPU") -> None:
        self.limit = limit
        self.resource = resource
        super().__init__(
            f"Strategy exceeded {resource} resource limit of {limit}s "
            f"(RLIMIT_CPU / SIGXCPU)"
        )
