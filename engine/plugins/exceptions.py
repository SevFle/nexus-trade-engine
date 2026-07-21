"""Exceptions raised by the plugin sandbox and related enforcement layers.

These are kept in a dedicated module (rather than inside ``sandbox``) so they
can be imported by callers and tests without pulling in the heavy sandbox
machinery — and without giving sandboxed strategy code a reason to import the
sandbox package (which is itself blocked by the restricted importer).
"""

from __future__ import annotations

from typing import Any


class SandboxSecurityError(ImportError):
    """Raised when a sandboxed plugin violates the Layer-1 import policy.

    This is the **typed** security exception surfaced by
    :class:`engine.plugins.sandbox.import_guard.ImportGuard` (Layer 1 of the
    five-layer sandbox) whenever strategy plugin code attempts to import a
    module that is not on the allowlist (or is explicitly on the denylist).

    It deliberately subclasses :class:`ImportError` so that existing call-sites
    and the stdlib import machinery — which expect an ``ImportError`` (or its
    subclass) when a finder rejects a name — continue to behave correctly,
    while giving application code a *specific* exception type to catch and
    report distinctly from ordinary missing-module errors::

        try:
            import_guard.check_import(name)
        except SandboxSecurityError as e:
            audit.log("sandbox.import_violation", module=e.module)

    Attributes
    ----------
    module:
        The fully-qualified module name whose import was rejected.
    reason:
        A short human-readable explanation of *why* it was rejected (e.g.
        ``"not in allowlist"`` or ``"explicitly denylisted"``).
    """

    def __init__(self, module: str, reason: str = "not in allowlist") -> None:
        self.module: str = module
        self.reason: str = reason
        super().__init__(
            f"Import of module {module!r} blocked by strategy sandbox: {reason}"
        )

    def __reduce__(self) -> Any:
        # Pickle support for the typed exception (tests / structured logging
        # may round-trip it).  ``__reduce__`` itself is *not* reachable from
        # sandboxed strategy code — this exception class lives outside the
        # sandboxed execution context.
        return (self.__class__, (self.module, self.reason))


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
