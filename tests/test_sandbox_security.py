"""
Adversarial tests for the strategy sandbox security layers.

Layers tested:
  1. Import restrictions - blocked modules cannot be imported
  2. Network whitelist - only declared endpoints are reachable
  3. Resource limits - memory / FD limits enforced (Linux only)
  4. Filesystem isolation - no access outside sandbox working dir
  5. Process isolation - documented as production target (MVP = in-process)
"""

from __future__ import annotations

import asyncio
import builtins
import os
import sys
from typing import Any

import httpx
import pytest

from engine.core.signal import Signal
from engine.plugins.manifest import StrategyManifest
from engine.plugins.restricted_importer import BLOCKED_MODULES, RestrictedImporter
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
        import os  # noqa: F401, PLC0415

        return []


class _ImportSubprocessStrategy:
    name = "import_subprocess"
    version = "1.0.0"

    def on_bar(self, _state: Any, _portfolio: Any) -> list[Any]:
        import subprocess  # noqa: F401, PLC0415

        return []


class _FromOsPathImportStrategy:
    name = "from_os_path"
    version = "1.0.0"

    def on_bar(self, _state: Any, _portfolio: Any) -> list[Any]:
        from os.path import join  # noqa: F401, PLC0415

        return []


class _ImportSysStrategy:
    name = "import_sys"
    version = "1.0.0"

    def on_bar(self, _state: Any, _portfolio: Any) -> list[Any]:
        import sys  # noqa: F401, PLC0415

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
        with open("/tmp/sandbox_write_test", "w") as f:  # noqa: S108
            f.write("pwned")
        return []


class _FileDescriptorStrategy:
    name = "fd_access"
    version = "1.0.0"

    def on_bar(self, _state: Any, _portfolio: Any) -> list[Any]:
        builtins.open(0)  # noqa: SIM115
        return []


class _SubclassTraversalStrategy:
    name = "subclass_traversal"
    version = "1.0.0"

    def on_bar(self, _state: Any, _portfolio: Any) -> list[Any]:
        getattr((), "__class__").__bases__[0].__subclasses__()  # noqa: B009
        return []


class _IoOpenStrategy:
    name = "io_open"
    version = "1.0.0"

    def on_bar(self, _state: Any, _portfolio: Any) -> list[Any]:
        import io  # noqa: PLC0415

        io.open("/etc/passwd").read()  # noqa: SIM115, UP020
        return []


class _ImportHttpxStrategy:
    name = "import_httpx"
    version = "1.0.0"

    def on_bar(self, _state: Any, _portfolio: Any) -> list[Any]:
        import httpx  # noqa: PLC0415

        httpx.get("https://evil.com")
        return []


class _TypeIntrospectionStrategy:
    name = "type_introspection"
    version = "1.0.0"

    def on_bar(self, _state: Any, _portfolio: Any) -> list[Any]:
        type("X", (), {})
        return []


class _GetattrBypassStrategy:
    name = "getattr_bypass"
    version = "1.0.0"

    def __init__(self) -> None:
        self._result: str | None = None

    def on_bar(self, _state: Any, _portfolio: Any) -> list[Any]:
        self._result = getattr(self, "name")  # noqa: B009
        return []


class _DirBypassStrategy:
    name = "dir_bypass"
    version = "1.0.0"

    def on_bar(self, _state: Any, _portfolio: Any) -> list[Any]:
        dir(builtins)
        return []


class _VarsBypassStrategy:
    name = "vars_bypass"
    version = "1.0.0"

    def on_bar(self, _state: Any, _portfolio: Any) -> list[Any]:
        vars()
        return []


class _ExecBypassStrategy:
    name = "exec_bypass"
    version = "1.0.0"

    def on_bar(self, _state: Any, _portfolio: Any) -> list[Any]:
        exec("import os")  # noqa: S102
        return []


class _EvalBypassStrategy:
    name = "eval_bypass"
    version = "1.0.0"

    def on_bar(self, _state: Any, _portfolio: Any) -> list[Any]:
        eval("1+1")  # noqa: S307
        return []


class _ImportThreadingStrategy:
    name = "import_threading"
    version = "1.0.0"

    def on_bar(self, _state: Any, _portfolio: Any) -> list[Any]:
        import threading  # noqa: F401, PLC0415

        return []


class _ImportPickleStrategy:
    name = "import_pickle"
    version = "1.0.0"

    def on_bar(self, _state: Any, _portfolio: Any) -> list[Any]:
        import pickle  # noqa: F401, PLC0415

        return []


class _ImportInspectStrategy:
    name = "import_inspect"
    version = "1.0.0"

    def on_bar(self, _state: Any, _portfolio: Any) -> list[Any]:
        import inspect  # noqa: F401, PLC0415

        return []


class _SlowStrategy:
    name = "slow"
    version = "1.0.0"

    async def on_bar(self, _state: Any, _portfolio: Any) -> list[Any]:
        await asyncio.sleep(60)
        return []


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


class TestRestrictedImporter:
    def test_install_and_uninstall(self) -> None:
        importer = RestrictedImporter()
        importer.install()
        assert importer in sys.meta_path
        assert importer._installed is True  # noqa: SLF001
        importer.uninstall()
        assert importer not in sys.meta_path
        assert importer._installed is False  # noqa: SLF001

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

    @pytest.mark.parametrize("module_name", sorted(BLOCKED_MODULES))
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

    async def test_safe_import_still_works(self, manifest: StrategyManifest) -> None:
        sandbox = StrategySandbox(_GoodStrategy(), manifest)
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert len(signals) == 1
            assert signals[0].symbol == "AAPL"
        finally:
            sandbox.cleanup()


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
            assert response.status_code == 200  # noqa: PLR2004

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
            assert response.status_code == 200  # noqa: PLR2004

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

    async def test_sandbox_creates_http_client_when_manifest_has_endpoints(
        self, networked_manifest: StrategyManifest
    ) -> None:
        sandbox = StrategySandbox(_GoodStrategy(), networked_manifest)
        try:
            assert sandbox._http_client is not None  # noqa: SLF001
            assert isinstance(sandbox._http_client, SandboxedHttpClient)  # noqa: SLF001
        finally:
            sandbox.cleanup()

    async def test_sandbox_skips_http_client_when_no_endpoints(
        self, manifest: StrategyManifest
    ) -> None:
        sandbox = StrategySandbox(_GoodStrategy(), manifest)
        try:
            assert sandbox._http_client is None  # noqa: SLF001
        finally:
            sandbox.cleanup()


class TestResourceLimits:
    def test_parse_memory_mb(self) -> None:
        assert StrategySandbox._parse_memory("512MB") == 512 * 1024**2  # noqa: SLF001

    def test_parse_memory_gb(self) -> None:
        assert StrategySandbox._parse_memory("2GB") == 2 * 1024**3  # noqa: SLF001

    def test_parse_memory_kb(self) -> None:
        assert StrategySandbox._parse_memory("256KB") == 256 * 1024  # noqa: SLF001

    def test_parse_memory_b(self) -> None:
        assert StrategySandbox._parse_memory("1024B") == 1024  # noqa: SLF001, PLR2004

    def test_parse_memory_plain_number(self) -> None:
        assert StrategySandbox._parse_memory("1048576") == 1_048_576  # noqa: SLF001, PLR2004

    def test_parse_memory_case_insensitive(self) -> None:
        assert StrategySandbox._parse_memory("512mb") == 512 * 1024**2  # noqa: SLF001

    def test_parse_memory_with_spaces(self) -> None:
        assert StrategySandbox._parse_memory("  512MB  ") == 512 * 1024**2  # noqa: SLF001


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
            assert sandbox._work_dir is not None  # noqa: SLF001
            assert os.path.isdir(sandbox._work_dir)  # noqa: SLF001
        finally:
            sandbox.cleanup()

    async def test_cleanup_removes_work_dir(self, manifest: StrategyManifest) -> None:
        sandbox = StrategySandbox(_GoodStrategy(), manifest)
        work_dir = sandbox._work_dir  # noqa: SLF001
        assert work_dir is not None
        sandbox.cleanup()
        assert not os.path.isdir(work_dir)


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

        sandbox = StrategySandbox(_GoodStrategy(), manifest)
        try:
            await sandbox.safe_evaluate(None, None, None)
            assert builtins.__import__ is original_import
            assert builtins.open is original_open
        finally:
            sandbox.cleanup()

    async def test_restrictions_removed_after_error(self, manifest: StrategyManifest) -> None:
        original_import = builtins.__import__
        original_open = builtins.open

        sandbox = StrategySandbox(_ImportOsStrategy(), manifest)
        try:
            await sandbox.safe_evaluate(None, None, None)
            assert builtins.__import__ is original_import
            assert builtins.open is original_open
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


class TestSubclassTraversalBypass:
    async def test_getattr_blocked_prevents_subclass_traversal(
        self, manifest: StrategyManifest
    ) -> None:
        sandbox = StrategySandbox(_SubclassTraversalStrategy(), manifest)
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert signals == []
            assert sandbox.metrics.errors == 1
            assert "getattr" in (sandbox.metrics.last_error or "").lower()
        finally:
            sandbox.cleanup()

    async def test_type_blocked_prevents_type_introspection(
        self, manifest: StrategyManifest
    ) -> None:
        sandbox = StrategySandbox(_TypeIntrospectionStrategy(), manifest)
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert signals == []
            assert sandbox.metrics.errors == 1
            assert "type" in (sandbox.metrics.last_error or "").lower()
        finally:
            sandbox.cleanup()

    async def test_dir_blocked(self, manifest: StrategyManifest) -> None:
        sandbox = StrategySandbox(_DirBypassStrategy(), manifest)
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert signals == []
            assert sandbox.metrics.errors == 1
            assert "dir" in (sandbox.metrics.last_error or "").lower()
        finally:
            sandbox.cleanup()

    async def test_vars_blocked(self, manifest: StrategyManifest) -> None:
        sandbox = StrategySandbox(_VarsBypassStrategy(), manifest)
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert signals == []
            assert sandbox.metrics.errors == 1
            assert "vars" in (sandbox.metrics.last_error or "").lower()
        finally:
            sandbox.cleanup()

    async def test_exec_blocked(self, manifest: StrategyManifest) -> None:
        sandbox = StrategySandbox(_ExecBypassStrategy(), manifest)
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert signals == []
            assert sandbox.metrics.errors == 1
            assert "exec" in (sandbox.metrics.last_error or "").lower()
        finally:
            sandbox.cleanup()

    async def test_eval_blocked(self, manifest: StrategyManifest) -> None:
        sandbox = StrategySandbox(_EvalBypassStrategy(), manifest)
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert signals == []
            assert sandbox.metrics.errors == 1
            assert "eval" in (sandbox.metrics.last_error or "").lower()
        finally:
            sandbox.cleanup()

    async def test_getattr_not_available_as_builtin_during_execution(
        self, manifest: StrategyManifest
    ) -> None:
        sandbox = StrategySandbox(_GetattrBypassStrategy(), manifest)
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert signals == []
            assert sandbox.metrics.errors == 1
        finally:
            sandbox.cleanup()

    async def test_builtins_restored_after_evaluation(self, manifest: StrategyManifest) -> None:
        original_getattr = builtins.getattr
        original_type = builtins.type
        original_dir = builtins.dir

        sandbox = StrategySandbox(_GoodStrategy(), manifest)
        try:
            await sandbox.safe_evaluate(None, None, None)
            assert builtins.getattr is original_getattr
            assert builtins.type is original_type
            assert builtins.dir is original_dir
        finally:
            sandbox.cleanup()

    async def test_builtins_restored_after_error(self, manifest: StrategyManifest) -> None:
        original_getattr = builtins.getattr
        original_type = builtins.type

        sandbox = StrategySandbox(_SubclassTraversalStrategy(), manifest)
        try:
            await sandbox.safe_evaluate(None, None, None)
            assert builtins.getattr is original_getattr
            assert builtins.type is original_type
        finally:
            sandbox.cleanup()


class TestIoOpenBypass:
    async def test_io_import_blocked(self, manifest: StrategyManifest) -> None:
        sandbox = StrategySandbox(_IoOpenStrategy(), manifest)
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert signals == []
            assert sandbox.metrics.errors == 1
            assert "blocked" in (sandbox.metrics.last_error or "").lower()
        finally:
            sandbox.cleanup()

    async def test_io_in_blocked_modules_list(self) -> None:
        assert "io" in BLOCKED_MODULES
        assert "_io" in BLOCKED_MODULES

    async def test_codecs_in_blocked_modules_list(self) -> None:
        assert "codecs" in BLOCKED_MODULES


class TestHttpxBypass:
    async def test_httpx_import_blocked(self, manifest: StrategyManifest) -> None:
        sandbox = StrategySandbox(_ImportHttpxStrategy(), manifest)
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert signals == []
            assert sandbox.metrics.errors == 1
            assert "blocked" in (sandbox.metrics.last_error or "").lower()
        finally:
            sandbox.cleanup()

    async def test_httpx_in_blocked_modules_list(self) -> None:
        assert "httpx" in BLOCKED_MODULES

    async def test_requests_in_blocked_modules_list(self) -> None:
        assert "requests" in BLOCKED_MODULES

    async def test_aiohttp_in_blocked_modules_list(self) -> None:
        assert "aiohttp" in BLOCKED_MODULES

    async def test_urllib3_in_blocked_modules_list(self) -> None:
        assert "urllib3" in BLOCKED_MODULES


class TestAdditionalModuleBlocks:
    async def test_threading_import_blocked(self, manifest: StrategyManifest) -> None:
        sandbox = StrategySandbox(_ImportThreadingStrategy(), manifest)
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert signals == []
            assert "blocked" in (sandbox.metrics.last_error or "").lower()
        finally:
            sandbox.cleanup()

    async def test_pickle_import_blocked(self, manifest: StrategyManifest) -> None:
        sandbox = StrategySandbox(_ImportPickleStrategy(), manifest)
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert signals == []
            assert "blocked" in (sandbox.metrics.last_error or "").lower()
        finally:
            sandbox.cleanup()

    async def test_inspect_import_blocked(self, manifest: StrategyManifest) -> None:
        sandbox = StrategySandbox(_ImportInspectStrategy(), manifest)
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert signals == []
            assert "blocked" in (sandbox.metrics.last_error or "").lower()
        finally:
            sandbox.cleanup()


class TestCleanupResourceRestore:
    async def test_cleanup_restores_resource_limits(self, manifest: StrategyManifest) -> None:
        sandbox = StrategySandbox(_GoodStrategy(), manifest)
        try:
            await sandbox.safe_evaluate(None, None, None)
            sandbox.cleanup()
            assert sandbox._saved_resource_limits == {}  # noqa: SLF001
        finally:
            sandbox.cleanup()
