"""
Adversarial tests for the strategy sandbox security layers.

Layers tested:
  1. Import restrictions - blocked modules cannot be imported
  1.1 Introspection blocking - __subclasses__, __globals__ etc.
  1.2 io.open bypass closed
  2. Network whitelist - only declared endpoints are reachable
     (enforced on ALL httpx clients, not just SandboxedHttpClient)
  3. Resource limits - memory / FD limits enforced (Linux only)
  4. Filesystem isolation - no access outside sandbox working dir
  5. Process isolation - documented as production target (MVP = in-process)
"""

from __future__ import annotations

import asyncio
import builtins
import os
import sys
import unittest.mock
from typing import Any

import httpx
import pytest

from engine.core.signal import Signal
from engine.plugins.manifest import (
    NetworkConfig,
    StrategyManifest,
)
from engine.plugins.restricted_importer import (
    _INTERNAL_BYPASS_MODULES,
    BLOCKED_MODULES,
    RestrictedImporter,
)
from engine.plugins.sandbox import StrategySandbox
from engine.plugins.sandboxed_http import SandboxedHttpClient


class _GoodStrategy:
    name = "good"
    version = "1.0.0"

    def on_bar(self, _state: Any, _portfolio: Any) -> list[Signal]:
        return [Signal.buy(symbol="AAPL", strategy_id=self.name)]


class _ImportOsStrategy:
    name = "import_os"
    version = "1.0.0"

    def on_bar(self, _state: Any, _portfolio: Any) -> list[Any]:
        import os  # noqa: F401

        return []


class _ImportSubprocessStrategy:
    name = "import_subprocess"
    version = "1.0.0"

    def on_bar(self, _state: Any, _portfolio: Any) -> list[Any]:
        import subprocess  # noqa: F401

        return []


class _FromOsPathImportStrategy:
    name = "from_os_path"
    version = "1.0.0"

    def on_bar(self, _state: Any, _portfolio: Any) -> list[Any]:
        from os.path import join  # noqa: F401

        return []


class _ImportSysStrategy:
    name = "import_sys"
    version = "1.0.0"

    def on_bar(self, _state: Any, _portfolio: Any) -> list[Any]:
        import sys  # noqa: F401

        return []


class _ImportIoStrategy:
    name = "import_io"
    version = "1.0.0"

    def on_bar(self, _state: Any, _portfolio: Any) -> list[Any]:
        import io  # noqa: F401

        return []


class _FileReadStrategy:
    name = "file_read"
    version = "1.0.0"

    def __init__(self, target_path: str) -> None:
        self._target = target_path

    def on_bar(self, _state: Any, _portfolio: Any) -> list[Any]:
        with open(self._target) as f:
            f.read()
        return []


class _FileWriteStrategy:
    name = "file_write"
    version = "1.0.0"

    def on_bar(self, _state: Any, _portfolio: Any) -> list[Any]:
        with open("/tmp/sandbox_write_test", "w") as f:
            f.write("pwned")
        return []


class _FileDescriptorStrategy:
    name = "fd_access"
    version = "1.0.0"

    def on_bar(self, _state: Any, _portfolio: Any) -> list[Any]:
        builtins.open(0)  # noqa: SIM115
        return []


class _SlowStrategy:
    name = "slow"
    version = "1.0.0"

    async def on_bar(self, _state: Any, _portfolio: Any) -> list[Any]:
        await asyncio.sleep(60)
        return []


# ── Bypass vector strategies ─────────────────────────────────────────


class _SubclassTraversalStrategy:
    """Bypass 1: walk object.__subclasses__() to reach blocked modules."""

    name = "subclass_traversal"
    version = "1.0.0"

    def on_bar(self, _state: Any, _portfolio: Any) -> list[Any]:
        for _cls in object.__subclasses__():  # type: ignore[type-arg]
            pass
        return []


class _GetattrSubclassTraversalStrategy:
    """Bypass 1a: getattr-based traversal for non-object types."""

    name = "getattr_subclass_traversal"
    version = "1.0.0"

    def on_bar(self, _state: Any, _portfolio: Any) -> list[Any]:
        subs = getattr(int, "__subclasses__")()  # noqa: B009
        return [type(s).__name__ for s in subs]


class _GetattrGlobalsStrategy:
    """Bypass 1b: getattr-based access to __globals__."""

    name = "getattr_globals"
    version = "1.0.0"

    def on_bar(self, _state: Any, _portfolio: Any) -> list[Any]:
        fn = self.on_bar
        globs = getattr(fn, "__globals__")  # noqa: B009
        return [k for k in globs if "os" in k]


class _IoOpenReadStrategy:
    """Bypass 2: use io.open() to bypass builtins.open restriction."""

    name = "io_open_read"
    version = "1.0.0"

    def __init__(self, target_path: str) -> None:
        self._target = target_path

    def on_bar(self, _state: Any, _portfolio: Any) -> list[Any]:
        import io

        with io.open(self._target) as f:  # type: ignore[attr-defined]  # noqa: UP020
            f.read()
        return []


class _DirectHttpxStrategy:
    """Bypass 3: create own httpx.AsyncClient bypassing SandboxedHttpClient."""

    name = "direct_httpx"
    version = "1.0.0"

    async def on_bar(self, _state: Any, _portfolio: Any) -> list[Any]:
        import httpx as hx

        transport = hx.MockTransport(lambda _r: hx.Response(200))
        async with hx.AsyncClient(transport=transport) as client:
            await client.get("https://evil.com/api")
        return []


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def manifest() -> StrategyManifest:
    return StrategyManifest(
        id="test",
        name="test",
        version="1.0.0",
        resources={"max_cpu_seconds": 1},
    )


@pytest.fixture
def networked_manifest() -> StrategyManifest:
    return StrategyManifest(
        id="test",
        name="test",
        version="1.0.0",
        resources={"max_cpu_seconds": 5},
        network={"allowed_endpoints": ["api.anthropic.com"]},
    )


# ── Layer 1: Import restrictions ─────────────────────────────────────


class TestRestrictedImporter:
    def test_install_and_uninstall(self) -> None:
        importer = RestrictedImporter()
        importer.install()
        assert importer in sys.meta_path
        assert importer._installed is True
        importer.uninstall()
        assert importer not in sys.meta_path
        assert importer._installed is False

    def test_double_install_is_noop(self) -> None:
        importer = RestrictedImporter()
        importer.install()
        idx = sys.meta_path.index(importer)
        importer.install()
        assert sys.meta_path.index(importer) == idx
        importer.uninstall()

    def test_double_uninstall_is_safe(self) -> None:
        importer = RestrictedImporter()
        importer.install()
        importer.uninstall()
        importer.uninstall()

    @pytest.mark.parametrize(
        "module_name",
        # ``_INTERNAL_BYPASS_MODULES`` are host/test-harness infra that the hook
        # deliberately never intercepts (see focus: don't break test collection),
        # so they are excluded from the denylist regression matrix.
        [m for m in sorted(BLOCKED_MODULES) if m not in _INTERNAL_BYPASS_MODULES],
    )
    def test_find_spec_blocks_all_listed_modules(self, module_name: str) -> None:
        importer = RestrictedImporter()
        with pytest.raises(ImportError, match="blocked"):
            importer.find_spec(module_name)

    def test_find_spec_allows_safe_module(self) -> None:
        importer = RestrictedImporter()
        result = importer.find_spec("json")
        assert result is None

    def test_find_spec_blocks_submodule(self) -> None:
        importer = RestrictedImporter()
        with pytest.raises(ImportError, match=r"os\.path"):
            importer.find_spec("os.path")

    def test_custom_blocked_set(self) -> None:
        importer = RestrictedImporter(blocked={"custom_danger"})
        with pytest.raises(ImportError, match="custom_danger"):
            importer.find_spec("custom_danger")

    def test_original_import_not_captured_until_install(self) -> None:
        """Construction must be side-effect free: ``_original_import`` is
        captured lazily at ``install()`` time, not in ``__init__``."""
        importer = RestrictedImporter()
        assert importer._original_import is None
        assert importer._installed is False

    def test_restricted_import_raises_when_not_installed(self) -> None:
        """``_restricted_import`` must raise a clear ``ImportError`` (never a
        ``TypeError`` from calling ``None``) when the importer has not been
        installed and therefore has no original ``__import__`` to delegate to."""
        importer = RestrictedImporter()
        # ``json`` is allowlisted, so the policy check passes and execution
        # reaches the delegation guard.
        assert importer._is_allowed("json")
        with pytest.raises(ImportError, match="install\\(\\) was never called"):
            importer._restricted_import("json")

    @pytest.fixture(autouse=True)
    def _restore_import_state(self) -> Any:
        """Snapshot and restore ``sys.meta_path`` and ``builtins.__import__``
        around every test in this class.

        Several tests install a :class:`RestrictedImporter` (which mutates
        both) and only ``uninstall()`` it at the very end.  If an assertion
        fails before ``uninstall()`` the importer would otherwise leak onto
        ``sys.meta_path`` and clobber ``builtins.__import__`` for the rest of
        the session — most catastrophically blocking ``hypothesis``'s
        ``sortedcontainers`` import during the pytest terminal-summary.  This
        fixture guarantees no leak regardless of whether a test passes.
        """
        meta_snapshot = list(sys.meta_path)
        import_snapshot = builtins.__import__
        try:
            yield
        finally:
            builtins.__import__ = import_snapshot
            sys.meta_path[:] = meta_snapshot

    def test_uninstall_does_not_clobber_unowned_builtin(self) -> None:
        """``uninstall()`` must only restore ``builtins.__import__`` when it
        still points at *this* importer's hook.  If something else (a later
        importer, or test scaffolding) has replaced it, uninstall must not
        blindly overwrite — otherwise it corrupts the import system."""
        importer = RestrictedImporter()
        importer.install()
        # ``install()`` stores the bound method once in ``_import_hook`` (a
        # fresh ``importer._restricted_import`` access is a *different* object),
        # so identity is checked against the stored hook.
        assert builtins.__import__ is importer._import_hook

        # Simulate an unrelated party resetting the builtin to the real import
        # (e.g. an outer test harness or a nested importer that already tore
        # down).  After this, ``importer`` no longer owns the builtin.
        real_import = importer._original_import
        assert real_import is not None
        builtins.__import__ = real_import

        importer.uninstall()
        # The real import is still in place (uninstall did NOT overwrite it
        # with a stale value), and the importer is marked uninstalled.
        assert builtins.__import__ is real_import
        assert importer._installed is False
        assert importer not in sys.meta_path

    def test_uninstall_restores_when_it_owns_the_builtin(self) -> None:
        """Normal in-order teardown: uninstall restores the original import."""
        real_import = builtins.__import__
        importer = RestrictedImporter()
        importer.install()
        assert builtins.__import__ is importer._import_hook
        importer.uninstall()
        assert builtins.__import__ is real_import
        assert importer._installed is False

    def test_install_captures_original_import(self) -> None:
        """After ``install()`` the importer has a valid delegation target and
        delegates allowlisted imports to the real ``__import__``."""
        real_import = builtins.__import__
        importer = RestrictedImporter()
        importer.install()
        assert importer._original_import is real_import
        # ``json`` is allowlisted → delegated straight through.
        import json as _json  # noqa: F401

        assert importer._original_import is real_import  # unchanged by delegation
        importer.uninstall()


class TestGetaddrinfoGuard:
    """DNS-resolution guard: ``install()`` wraps ``socket.getaddrinfo`` so a
    non-allowlisted hostname is rejected with ``ConnectionError`` *before* any
    resolution occurs, and ``uninstall()`` restores the original.

    This is defence-in-depth beneath the httpx ``send`` hook: ``socket`` is
    blocked by the import allowlist, but allowlisted networking libraries
    resolve hostnames through ``socket.getaddrinfo``, so the guard closes that
    path.
    """

    @pytest.fixture(autouse=True)
    def _restore_socket_and_import(self) -> Any:
        """Snapshot/restore ``sys.meta_path``, ``builtins.__import__`` and
        ``socket.getaddrinfo`` so a failing assertion cannot leak the guard
        onto the process-global socket function for the rest of the session."""
        import socket

        meta_snapshot = list(sys.meta_path)
        import_snapshot = builtins.__import__
        gai_snapshot = socket.getaddrinfo
        try:
            yield
        finally:
            builtins.__import__ = import_snapshot
            sys.meta_path[:] = meta_snapshot
            socket.getaddrinfo = gai_snapshot

    def test_non_allowlisted_host_blocked_at_resolution(self) -> None:
        import socket

        original_gai = socket.getaddrinfo
        importer = RestrictedImporter(allowed_hosts=["api.anthropic.com"])
        importer.install()

        # The guard is in place.
        assert socket.getaddrinfo is importer._getaddrinfo_hook
        # A non-allowlisted host is rejected *before* any DNS lookup happens.
        with pytest.raises(ConnectionError, match=r"evil\.example\.com"):
            socket.getaddrinfo("evil.example.com", 80)

        importer.uninstall()
        # Original ``getaddrinfo`` restored after teardown.
        assert socket.getaddrinfo is original_gai
        assert importer._original_getaddrinfo is None

    def test_allowlisted_host_delegates_to_original(self) -> None:
        import socket

        real_original = socket.getaddrinfo
        delegated: list[Any] = []

        def fake_getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
            delegated.append(host)
            return [("resolved", host)]

        importer = RestrictedImporter(allowed_hosts=["api.anthropic.com"])
        importer.install()
        # ``install()`` captured the real ``getaddrinfo``; swap in a stub so
        # the test makes no real DNS/network call.  An allowlisted host must
        # reach the original (here, the stub).
        real_captured = importer._original_getaddrinfo
        importer._original_getaddrinfo = fake_getaddrinfo

        result = socket.getaddrinfo("api.anthropic.com", 443)
        assert delegated == ["api.anthropic.com"]
        assert result == [("resolved", "api.anthropic.com")]

        # Restore the real original so ``uninstall()`` reinstates it cleanly.
        importer._original_getaddrinfo = real_captured
        importer.uninstall()
        assert socket.getaddrinfo is real_original

    def test_is_host_allowed_rejects_when_allowlist_empty(self) -> None:
        """An empty allowlist rejects every host (covers the empty-list branch)."""
        importer = RestrictedImporter()  # no allowed_hosts -> empty allowlist
        assert importer.allowed_hosts == []
        assert importer._is_host_allowed("api.anthropic.com") is False

    def test_is_host_allowed_rejects_none_host(self) -> None:
        """``None`` host is rejected before any string comparison (None branch)."""
        importer = RestrictedImporter(allowed_hosts=["api.anthropic.com"])
        assert importer._is_host_allowed(None) is False

    def test_is_host_allowed_rejects_empty_host(self) -> None:
        """An empty-string host is rejected (empty-name branch)."""
        importer = RestrictedImporter(allowed_hosts=["api.anthropic.com"])
        assert importer._is_host_allowed("") is False

    def test_is_host_allowed_is_case_insensitive(self) -> None:
        """Mixed-case request hosts match a canonical lowercase entry."""
        importer = RestrictedImporter(allowed_hosts=["api.anthropic.com"])
        # Exact match, differing only by case.
        assert importer._is_host_allowed("API.ANTHROPIC.COM") is True
        # Subdomain match, differing only by case.
        assert importer._is_host_allowed("V2.Api.Anthropic.Com") is True
        # A mixed-case *entry* still matches a lowercase host (defence for
        # callers that bypass the manifest validator).
        mixed = RestrictedImporter(allowed_hosts=["API.Anthropic.Com"])
        assert mixed._is_host_allowed("api.anthropic.com") is True

    def test_getaddrinfo_raises_when_original_not_captured(self) -> None:
        """``_restricted_getaddrinfo`` raises if ``_original_getaddrinfo`` is None.

        This branch is unreachable in normal operation (``install()`` captures
        the original first) but is guarded defensively.  Exercise it directly
        by clearing the captured original while still passing an allowlisted
        host so the hostname check succeeds and the delegation guard runs.
        """
        importer = RestrictedImporter(allowed_hosts=["api.anthropic.com"])
        # Force the delegation-guard branch: host is allowed, but there is no
        # captured original to delegate to.
        importer._original_getaddrinfo = None
        with pytest.raises(
            ConnectionError,
            match=r"no original getaddrinfo to delegate to",
        ):
            importer._restricted_getaddrinfo("api.anthropic.com", 443)


class TestPurgeNonAllowlisted:
    """Cover ``RestrictedImporter.purge_non_allowlisted`` (lines 201-208).

    Verifies that non-allowlisted, non-essential modules are evicted from
    ``sys.modules`` while essentials and allowlisted modules survive.

    A snapshot/restore fixture is essential because ``purge_non_allowlisted``
    mutates the *real* ``sys.modules`` dict — without restoration, subsequent
    tests (and their autouse fixtures) would crash trying to re-import purged
    third-party packages like ``fastapi`` / ``typing_extensions``.
    """

    @pytest.fixture(autouse=True)
    def _restore_sys_modules(self) -> None:
        snapshot = dict(sys.modules)
        yield
        for name, mod in snapshot.items():
            if name not in sys.modules:
                sys.modules[name] = mod

    def test_purge_removes_non_allowlisted_module(self) -> None:
        importer = RestrictedImporter()
        sentinel = "_test_purge_sentinel_module"
        sys.modules[sentinel] = object()
        try:
            assert sentinel in sys.modules
            importer.purge_non_allowlisted()
            assert sentinel not in sys.modules
        finally:
            sys.modules.pop(sentinel, None)

    def test_purge_retains_essential_cpython_modules(self) -> None:
        importer = RestrictedImporter()
        importer.purge_non_allowlisted()
        assert "sys" in sys.modules
        assert "builtins" in sys.modules

    def test_purge_retains_allowlisted_modules(self) -> None:
        importer = RestrictedImporter()
        if "json" not in sys.modules:
            import json  # noqa: F401
        importer.purge_non_allowlisted()
        assert "json" in sys.modules

    def test_purge_removes_submodule_of_blocked_root(self) -> None:
        importer = RestrictedImporter()
        sentinel = "_test_purge_pkg.sub"
        sys.modules[sentinel] = object()
        try:
            importer.purge_non_allowlisted()
            assert sentinel not in sys.modules
        finally:
            sys.modules.pop(sentinel, None)

    def test_purge_is_idempotent(self) -> None:
        importer = RestrictedImporter()
        importer.purge_non_allowlisted()
        importer.purge_non_allowlisted()
        assert "sys" in sys.modules


class TestResourceImportFallback:
    """Cover the ``except ImportError`` branch in ``StrategySandbox.__init__``
    (sandbox.py lines 227-228) where ``import resource`` fails at construction
    time even though ``HAS_RESOURCE_MODULE`` was ``True`` at module load.
    """

    def test_resource_import_failure_sets_none(self, manifest: StrategyManifest) -> None:
        original_import = builtins.__import__

        def failing_import(name: str, *args: Any, **kwargs: Any) -> Any:
            if name == "resource":
                raise ImportError("simulated unavailable")
            return original_import(name, *args, **kwargs)

        with unittest.mock.patch("builtins.__import__", failing_import):
            sandbox = StrategySandbox(_GoodStrategy(), manifest)
            try:
                assert sandbox._resource_module is None
            finally:
                sandbox.cleanup()


class TestImportRestrictionIntegration:
    async def test_import_os_blocked_in_sandbox(self, manifest: StrategyManifest) -> None:
        sandbox = StrategySandbox(_ImportOsStrategy(), manifest)
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert signals == []
            assert sandbox.metrics.errors == 1
            assert "blocked" in (sandbox.metrics.last_error or "").lower()
        finally:
            sandbox.cleanup()

    async def test_import_subprocess_blocked(self, manifest: StrategyManifest) -> None:
        sandbox = StrategySandbox(_ImportSubprocessStrategy(), manifest)
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert signals == []
            assert "blocked" in (sandbox.metrics.last_error or "").lower()
        finally:
            sandbox.cleanup()

    async def test_from_os_path_import_blocked(self, manifest: StrategyManifest) -> None:
        sandbox = StrategySandbox(_FromOsPathImportStrategy(), manifest)
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert signals == []
            assert "blocked" in (sandbox.metrics.last_error or "").lower()
        finally:
            sandbox.cleanup()

    async def test_import_sys_blocked(self, manifest: StrategyManifest) -> None:
        sandbox = StrategySandbox(_ImportSysStrategy(), manifest)
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert signals == []
            assert "blocked" in (sandbox.metrics.last_error or "").lower()
        finally:
            sandbox.cleanup()

    async def test_import_io_blocked(self, manifest: StrategyManifest) -> None:
        sandbox = StrategySandbox(_ImportIoStrategy(), manifest)
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert signals == []
            assert "blocked" in (sandbox.metrics.last_error or "").lower()
        finally:
            sandbox.cleanup()

    async def test_safe_import_still_works(self, manifest: StrategyManifest) -> None:
        sandbox = StrategySandbox(_GoodStrategy(), manifest)
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert len(signals) == 1
            assert signals[0].symbol == "AAPL"
        finally:
            sandbox.cleanup()


# ── Layer 1.1: Introspection blocking ────────────────────────────────


class TestBypassSubclassTraversal:
    async def test_object_subclasses_blocked(self, manifest: StrategyManifest) -> None:
        sandbox = StrategySandbox(_SubclassTraversalStrategy(), manifest)
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert signals == []
            assert sandbox.metrics.errors == 1
            assert "__subclasses__" in (sandbox.metrics.last_error or "")
        finally:
            sandbox.cleanup()

    async def test_getattr_subclasses_blocked(self, manifest: StrategyManifest) -> None:
        sandbox = StrategySandbox(_GetattrSubclassTraversalStrategy(), manifest)
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert signals == []
            assert sandbox.metrics.errors == 1
            assert "not accessible" in (sandbox.metrics.last_error or "")
        finally:
            sandbox.cleanup()

    async def test_getattr_globals_blocked(self, manifest: StrategyManifest) -> None:
        sandbox = StrategySandbox(_GetattrGlobalsStrategy(), manifest)
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert signals == []
            assert sandbox.metrics.errors == 1
            assert "not accessible" in (sandbox.metrics.last_error or "")
        finally:
            sandbox.cleanup()


# ── Layer 1.2 / Bypass 2: io.open ───────────────────────────────────


class TestBypassIoOpen:
    async def test_io_import_blocked(self, manifest: StrategyManifest) -> None:
        sandbox = StrategySandbox(_IoOpenReadStrategy("/etc/passwd"), manifest)
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert signals == []
            assert sandbox.metrics.errors == 1
            assert "blocked" in (sandbox.metrics.last_error or "").lower()
        finally:
            sandbox.cleanup()


# ── Layer 2 / Bypass 3: httpx direct construction ───────────────────


class TestSandboxedHttpClient:
    async def test_allowed_host_passes(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"status": "ok"})

        client = SandboxedHttpClient(
            allowed_endpoints=["api.anthropic.com"],
            transport=httpx.MockTransport(handler),
        )
        async with client:
            response = await client.get("https://api.anthropic.com/v1/models")
            assert response.status_code == 200

    async def test_blocked_host_raises_permission_error(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200)

        client = SandboxedHttpClient(
            allowed_endpoints=["api.anthropic.com"],
            transport=httpx.MockTransport(handler),
        )
        async with client:
            request = httpx.Request("GET", "https://evil.com/api")
            with pytest.raises(PermissionError, match="not allowed"):
                await client.send(request)

    async def test_subdomain_of_allowed_host_passes(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"data": "ok"})

        client = SandboxedHttpClient(
            allowed_endpoints=["anthropic.com"],
            transport=httpx.MockTransport(handler),
        )
        async with client:
            response = await client.get("https://api.anthropic.com/v1/models")
            assert response.status_code == 200

    async def test_empty_whitelist_blocks_everything(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200)

        client = SandboxedHttpClient(
            allowed_endpoints=[],
            transport=httpx.MockTransport(handler),
        )
        async with client:
            request = httpx.Request("GET", "https://api.anthropic.com/v1/models")
            with pytest.raises(PermissionError):
                await client.send(request)

    async def test_mixed_case_host_matches_at_runtime(self) -> None:
        """A manifest-canonicalised (lowercase) entry matches an uppercase host.

        ``NetworkConfig`` normalises entries to lowercase hostnames at load
        time; the runtime matcher must therefore be case-insensitive so a
        request whose URL host happens to be upper-case still matches.
        """
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            return httpx.Response(200, json={"status": "ok"})

        config = NetworkConfig(allowed_endpoints=["API.Anthropic.COM"])
        client = SandboxedHttpClient(
            allowed_endpoints=config.allowed_endpoints,
            transport=httpx.MockTransport(handler),
        )
        async with client:
            # Entry canonicalised to lowercase at load time.
            assert config.allowed_endpoints == ["api.anthropic.com"]
            # Upper-case host still matches at runtime.
            response = await client.get("https://API.ANTHROPIC.COM/v1/models")
            assert response.status_code == 200
        assert seen, "request should have reached the handler"

    async def test_sandbox_creates_http_client_when_manifest_has_endpoints(
        self, networked_manifest: StrategyManifest
    ) -> None:
        sandbox = StrategySandbox(_GoodStrategy(), networked_manifest)
        try:
            assert sandbox._http_client is not None
            assert isinstance(sandbox._http_client, SandboxedHttpClient)
        finally:
            sandbox.cleanup()

    async def test_sandbox_skips_http_client_when_no_endpoints(
        self, manifest: StrategyManifest
    ) -> None:
        sandbox = StrategySandbox(_GoodStrategy(), manifest)
        try:
            assert sandbox._http_client is None
        finally:
            sandbox.cleanup()


class TestBypassDirectHttpx:
    async def test_direct_httpx_client_blocked(self, networked_manifest: StrategyManifest) -> None:
        sandbox = StrategySandbox(_DirectHttpxStrategy(), networked_manifest)
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert signals == []
            assert sandbox.metrics.errors == 1
            assert "not allowed" in (sandbox.metrics.last_error or "").lower()
        finally:
            sandbox.cleanup()


# ── Layer 3: Resource limits ─────────────────────────────────────────


class TestResourceLimits:
    def test_parse_memory_mb(self) -> None:
        assert StrategySandbox._parse_memory("512MB") == 512 * 1024**2

    def test_parse_memory_gb(self) -> None:
        assert StrategySandbox._parse_memory("2GB") == 2 * 1024**3

    def test_parse_memory_kb(self) -> None:
        assert StrategySandbox._parse_memory("256KB") == 256 * 1024

    def test_parse_memory_b(self) -> None:
        assert StrategySandbox._parse_memory("1024B") == 1024

    def test_parse_memory_plain_number(self) -> None:
        assert StrategySandbox._parse_memory("1048576") == 1_048_576

    def test_parse_memory_case_insensitive(self) -> None:
        assert StrategySandbox._parse_memory("512mb") == 512 * 1024**2

    def test_parse_memory_with_spaces(self) -> None:
        assert StrategySandbox._parse_memory("  512MB  ") == 512 * 1024**2


# ── Layer 4: Filesystem isolation ────────────────────────────────────


class TestFilesystemIsolation:
    async def test_read_outside_sandbox_blocked(
        self, manifest: StrategyManifest, tmp_path: Any
    ) -> None:
        secret = tmp_path / "secret.txt"
        secret.write_text("sensitive data")

        sandbox = StrategySandbox(_FileReadStrategy(str(secret)), manifest)
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert signals == []
            assert sandbox.metrics.errors == 1
            assert "not allowed" in (sandbox.metrics.last_error or "").lower()
        finally:
            sandbox.cleanup()

    async def test_write_blocked(self, manifest: StrategyManifest) -> None:
        sandbox = StrategySandbox(_FileWriteStrategy(), manifest)
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert signals == []
            assert sandbox.metrics.errors == 1
        finally:
            sandbox.cleanup()

    async def test_file_descriptor_blocked(self, manifest: StrategyManifest) -> None:
        sandbox = StrategySandbox(_FileDescriptorStrategy(), manifest)
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert signals == []
            assert sandbox.metrics.errors == 1
        finally:
            sandbox.cleanup()

    async def test_sandbox_work_dir_created(self, manifest: StrategyManifest) -> None:
        sandbox = StrategySandbox(_GoodStrategy(), manifest)
        try:
            assert sandbox._work_dir is not None
            assert os.path.isdir(sandbox._work_dir)
        finally:
            sandbox.cleanup()

    async def test_cleanup_removes_work_dir(self, manifest: StrategyManifest) -> None:
        sandbox = StrategySandbox(_GoodStrategy(), manifest)
        work_dir = sandbox._work_dir
        assert work_dir is not None
        sandbox.cleanup()
        assert not os.path.isdir(work_dir)


# ── C-2: Pre-loaded module reference ─────────────────────────────────


class _InitStashStrategy:
    """C-2: stash os in __init__ before sandbox activates."""

    name = "init_stash"
    version = "1.0.0"
    _os: Any = None

    def __init__(self) -> None:
        import os

        self._os = os

    def on_bar(self, _state: Any, _portfolio: Any) -> list[Any]:
        self._os.system("echo pwned")
        return []


class _ClassBodyStashStrategy:
    """C-2: stash os at class-body time."""

    name = "class_body_stash"
    version = "1.0.0"

    def on_bar(self, _state: Any, _portfolio: Any) -> list[Any]:
        return []


class TestBypassInitStash:
    def test_from_factory_blocks_init_stash(self, manifest: StrategyManifest) -> None:
        with pytest.raises(ImportError, match="blocked"):
            StrategySandbox.from_factory(_InitStashStrategy, manifest)

    async def test_from_factory_produces_working_sandbox(self, manifest: StrategyManifest) -> None:
        sandbox = StrategySandbox.from_factory(_GoodStrategy, manifest)
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert len(signals) == 1
            assert signals[0].symbol == "AAPL"
        finally:
            sandbox.cleanup()


# ── H-2: _ctypes bypass ──────────────────────────────────────────────


class _ImportCtypesStrategy:
    name = "import_ctypes"
    version = "1.0.0"

    def on_bar(self, _state: Any, _portfolio: Any) -> list[Any]:
        import _ctypes  # noqa: F401

        return []


class _ImportThreadStrategy:
    name = "import_thread"
    version = "1.0.0"

    def on_bar(self, _state: Any, _portfolio: Any) -> list[Any]:
        import threading  # noqa: F401

        return []


class _ImportGcStrategy:
    name = "import_gc"
    version = "1.0.0"

    def on_bar(self, _state: Any, _portfolio: Any) -> list[Any]:
        import gc  # noqa: F401

        return []


class _ImportPickleStrategy:
    name = "import_pickle"
    version = "1.0.0"

    def on_bar(self, _state: Any, _portfolio: Any) -> list[Any]:
        import pickle  # noqa: F401

        return []


class TestExpandedBlocklist:
    async def test_ctypes_blocked(self, manifest: StrategyManifest) -> None:
        sandbox = StrategySandbox(_ImportCtypesStrategy(), manifest)
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert signals == []
            assert "blocked" in (sandbox.metrics.last_error or "").lower()
        finally:
            sandbox.cleanup()

    async def test_threading_blocked(self, manifest: StrategyManifest) -> None:
        sandbox = StrategySandbox(_ImportThreadStrategy(), manifest)
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert signals == []
            assert "blocked" in (sandbox.metrics.last_error or "").lower()
        finally:
            sandbox.cleanup()

    async def test_gc_blocked(self, manifest: StrategyManifest) -> None:
        sandbox = StrategySandbox(_ImportGcStrategy(), manifest)
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert signals == []
            assert "blocked" in (sandbox.metrics.last_error or "").lower()
        finally:
            sandbox.cleanup()

    async def test_pickle_blocked(self, manifest: StrategyManifest) -> None:
        sandbox = StrategySandbox(_ImportPickleStrategy(), manifest)
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert signals == []
            assert "blocked" in (sandbox.metrics.last_error or "").lower()
        finally:
            sandbox.cleanup()


# ── M-1: Path prefix matching ────────────────────────────────────────


class _FileReadStrategyWithPath:
    name = "file_read_path"
    version = "1.0.0"

    def __init__(self, target_path: str) -> None:
        self._target = target_path

    def on_bar(self, _state: Any, _portfolio: Any) -> list[Any]:
        with open(self._target) as f:
            f.read()
        return []


class TestPathPrefixMatching:
    async def test_path_prefix_does_not_match_partial_directory(self, tmp_path: Any) -> None:
        artifact_dir = tmp_path / "data"
        artifact_dir.mkdir()
        (artifact_dir / "safe.txt").write_text("safe")

        sibling = tmp_path / "database"
        sibling.mkdir()
        (sibling / "secret.txt").write_text("secret")

        sandbox = StrategySandbox(
            _FileReadStrategyWithPath(str(sibling / "secret.txt")),
            StrategyManifest(
                id="test",
                name="test",
                version="1.0.0",
                resources={"max_cpu_seconds": 1},
                artifacts=[str(artifact_dir)],
            ),
        )
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert signals == []
            assert sandbox.metrics.errors == 1
            assert "not allowed" in (sandbox.metrics.last_error or "").lower()
        finally:
            sandbox.cleanup()

    async def test_exact_artifact_path_allowed(self, tmp_path: Any) -> None:
        artifact = tmp_path / "safe.txt"
        artifact.write_text("safe content")

        sandbox = StrategySandbox(
            _FileReadStrategyWithPath(str(artifact)),
            StrategyManifest(
                id="test",
                name="test",
                version="1.0.0",
                resources={"max_cpu_seconds": 1},
                artifacts=[str(artifact)],
            ),
        )
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert len(signals) == 0
            assert sandbox.metrics.errors == 0
        finally:
            sandbox.cleanup()


# ── Integration ──────────────────────────────────────────────────────


class _WriteToAllowedPathStrategy:
    name = "write_allowed_path"
    version = "1.0.0"

    def __init__(self, target_path: str) -> None:
        self._target = target_path

    def on_bar(self, _state: Any, _portfolio: Any) -> list[Any]:
        with open(self._target, "w") as f:
            f.write("test")
        return []


class _AllowedHttpStrategy:
    name = "allowed_http"
    version = "1.0.0"

    async def on_bar(self, _state: Any, _portfolio: Any) -> list[Any]:
        import httpx as hx

        transport = hx.MockTransport(lambda r: hx.Response(200, json={"ok": True}))
        async with hx.AsyncClient(transport=transport) as client:
            await client.get("https://api.anthropic.com/v1/models")
        return []


class TestPlaceholderStrategy:
    def test_on_bar_returns_empty_list(self) -> None:
        from engine.plugins.sandbox import _PlaceholderStrategy

        p = _PlaceholderStrategy()
        assert p.on_bar(None, None) == []


class TestResourceLimitsNoModule:
    def test_apply_returns_early_without_resource_module(self, manifest: StrategyManifest) -> None:
        import engine.plugins.sandbox as mod

        sandbox = StrategySandbox(_GoodStrategy(), manifest)
        original = mod.HAS_RESOURCE_MODULE
        mod.HAS_RESOURCE_MODULE = False
        try:
            sandbox._apply_resource_limits()
        finally:
            mod.HAS_RESOURCE_MODULE = original
            sandbox.cleanup()

    def test_restore_returns_early_without_resource_module(
        self, manifest: StrategyManifest
    ) -> None:
        import engine.plugins.sandbox as mod

        sandbox = StrategySandbox(_GoodStrategy(), manifest)
        original = mod.HAS_RESOURCE_MODULE
        mod.HAS_RESOURCE_MODULE = False
        try:
            sandbox._restore_resource_limits()
        finally:
            mod.HAS_RESOURCE_MODULE = original
            sandbox.cleanup()


class TestResourceLimitsErrors:
    @staticmethod
    def _make_mock_resource() -> unittest.mock.MagicMock:
        mock = unittest.mock.MagicMock()
        mock.RLIMIT_AS = 9
        mock.RLIMIT_NOFILE = 7
        return mock

    def test_handles_rlimit_as_error(self, manifest: StrategyManifest) -> None:
        sandbox = StrategySandbox(_GoodStrategy(), manifest)
        mock_resource = self._make_mock_resource()
        mock_resource.getrlimit.side_effect = ValueError
        # After patch #908 the resource module lives on the instance as
        # ``_resource_module`` (no longer a module-level ``_resource``).
        sandbox._resource_module = mock_resource
        sandbox._apply_resource_limits()
        sandbox.cleanup()

    def test_handles_rlimit_nofile_error(self, manifest: StrategyManifest) -> None:
        sandbox = StrategySandbox(_GoodStrategy(), manifest)
        mock_resource = self._make_mock_resource()
        call_count = 0

        def _flaky_getrlimit(_res):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return (1024, 4096)
            raise OSError("mocked")

        mock_resource.getrlimit.side_effect = _flaky_getrlimit
        sandbox._resource_module = mock_resource
        sandbox._apply_resource_limits()
        sandbox._restore_resource_limits()
        sandbox.cleanup()


class TestFilesystemIsolationWrite:
    async def test_write_to_allowed_path_blocked(
        self, manifest: StrategyManifest, tmp_path: Any
    ) -> None:
        artifact_dir = tmp_path / "artifacts"
        artifact_dir.mkdir()
        target = artifact_dir / "data.txt"

        write_manifest = StrategyManifest(
            id="test",
            name="test",
            version="1.0.0",
            resources={"max_cpu_seconds": 1},
            artifacts=[str(artifact_dir)],
        )
        sandbox = StrategySandbox(_WriteToAllowedPathStrategy(str(target)), write_manifest)
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert signals == []
            assert sandbox.metrics.errors == 1
            assert "Write access" in (sandbox.metrics.last_error or "")
        finally:
            sandbox.cleanup()


class TestRestrictedNetworkSend:
    async def test_allowed_endpoint_passes(self, networked_manifest: StrategyManifest) -> None:
        sandbox = StrategySandbox(_AllowedHttpStrategy(), networked_manifest)
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert signals == []
            assert sandbox.metrics.errors == 0
        finally:
            sandbox.cleanup()


class TestCleanupWhileActive:
    def test_cleanup_restores_builtins_when_restrictions_active(
        self, manifest: StrategyManifest
    ) -> None:
        import builtins as bi

        sandbox = StrategySandbox(_GoodStrategy(), manifest)
        original_open = bi.open
        sandbox._activate_restrictions()
        assert sandbox._original_open is not None
        sandbox.cleanup()
        assert bi.open is original_open
        assert sandbox._original_open is None


class TestSandboxSecurityIntegration:
    async def test_timeout_returns_empty_signals(self, manifest: StrategyManifest) -> None:
        sandbox = StrategySandbox(_SlowStrategy(), manifest)
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert signals == []
            assert sandbox.metrics.errors == 1
            assert "Timeout" in (sandbox.metrics.last_error or "")
        finally:
            sandbox.cleanup()

    async def test_restrictions_removed_after_evaluation(self, manifest: StrategyManifest) -> None:
        original_import = builtins.__import__
        original_open = builtins.open
        original_object = builtins.object

        sandbox = StrategySandbox(_GoodStrategy(), manifest)
        try:
            await sandbox.safe_evaluate(None, None, None)
            assert builtins.__import__ is original_import
            assert builtins.open is original_open
            assert builtins.object is original_object
        finally:
            sandbox.cleanup()

    async def test_restrictions_removed_after_error(self, manifest: StrategyManifest) -> None:
        original_import = builtins.__import__
        original_open = builtins.open
        original_object = builtins.object

        sandbox = StrategySandbox(_ImportOsStrategy(), manifest)
        try:
            await sandbox.safe_evaluate(None, None, None)
            assert builtins.__import__ is original_import
            assert builtins.open is original_open
            assert builtins.object is original_object
        finally:
            sandbox.cleanup()

    async def test_good_strategy_passes_all_layers(self, manifest: StrategyManifest) -> None:
        sandbox = StrategySandbox(_GoodStrategy(), manifest)
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert len(signals) == 1
            assert signals[0].symbol == "AAPL"
            assert sandbox.metrics.errors == 0
        finally:
            sandbox.cleanup()
