"""Tests for sandbox security layers - adversarial strategy isolation."""

# ruff: noqa: PLC0415 (imports inside on_bar simulate adversarial strategies)

from __future__ import annotations

import asyncio
import os
import resource
import tempfile
from typing import Any

import pytest

from engine.plugins.manifest import StrategyManifest
from engine.plugins.sandbox import StrategySandbox
from engine.plugins.sandboxed_http import SandboxedHttpClient

FD_LIMIT = 64
EVAL_LIMIT = 2


def _make_manifest(**overrides: Any) -> StrategyManifest:
    defaults: dict[str, Any] = {
        "id": "test",
        "name": "test",
        "version": "1.0.0",
        "resources": {"max_cpu_seconds": EVAL_LIMIT},
    }
    defaults.update(overrides)
    return StrategyManifest(**defaults)


class _SandboxHelper:
    """Creates sandboxes and ensures resource-limit / import-hook cleanup."""

    def __init__(self) -> None:
        self._sandboxes: list[StrategySandbox] = []

    def create(self, strategy: Any, **manifest_kw: Any) -> StrategySandbox:
        manifest = _make_manifest(**manifest_kw)
        sb = StrategySandbox(strategy, manifest)
        self._sandboxes.append(sb)
        return sb

    def cleanup(self) -> None:
        for sb in self._sandboxes:
            sb.restore_limits()
        self._sandboxes.clear()


@pytest.fixture
def sandbox_helper():
    helper = _SandboxHelper()
    yield helper
    helper.cleanup()


# -- Layer 1: Import Restrictions -----------------------------------------


class TestLayer1ImportRestrictions:
    async def test_import_os_raises_import_error(self, sandbox_helper):
        class OsImportStrategy:
            name = "os_importer"
            version = "1.0.0"

            def on_bar(self, _state, _portfolio):
                import os  # noqa: F401

                return []

        sandbox = sandbox_helper.create(OsImportStrategy())
        signals = await sandbox.safe_evaluate(None, None, None)
        assert signals == []
        assert sandbox.metrics.errors == 1
        assert "blocked" in (sandbox.metrics.last_error or "").lower()

    async def test_import_subprocess_raises_import_error(self, sandbox_helper):
        class SubprocessStrategy:
            name = "subprocess_importer"
            version = "1.0.0"

            def on_bar(self, _state, _portfolio):
                import subprocess  # noqa: F401

                return []

        sandbox = sandbox_helper.create(SubprocessStrategy())
        signals = await sandbox.safe_evaluate(None, None, None)
        assert signals == []
        assert sandbox.metrics.errors == 1

    async def test_import_pathlib_raises_import_error(self, sandbox_helper):
        class PathlibStrategy:
            name = "pathlib_importer"
            version = "1.0.0"

            def on_bar(self, _state, _portfolio):
                import pathlib  # noqa: F401

                return []

        sandbox = sandbox_helper.create(PathlibStrategy())
        signals = await sandbox.safe_evaluate(None, None, None)
        assert signals == []
        assert sandbox.metrics.errors == 1

    async def test_import_socket_raises_import_error(self, sandbox_helper):
        class SocketStrategy:
            name = "socket_importer"
            version = "1.0.0"

            def on_bar(self, _state, _portfolio):
                import socket  # noqa: F401

                return []

        sandbox = sandbox_helper.create(SocketStrategy())
        signals = await sandbox.safe_evaluate(None, None, None)
        assert signals == []
        assert sandbox.metrics.errors == 1

    async def test_allowed_import_works(self, sandbox_helper):
        class JsonStrategy:
            name = "json_importer"
            version = "1.0.0"

            def on_bar(self, _state, _portfolio):
                import json

                _ = json.dumps({"ok": True})
                return []

        sandbox = sandbox_helper.create(JsonStrategy())
        signals = await sandbox.safe_evaluate(None, None, None)
        assert signals == []
        assert sandbox.metrics.errors == 0


# -- Layer 2: Network Whitelist -------------------------------------------


class TestLayer2NetworkWhitelist:
    async def test_sandboxed_http_blocks_unauthorized_host(self):
        client = SandboxedHttpClient(["api.anthropic.com"])
        with pytest.raises(PermissionError, match=r"evil\.com"):
            await client.get("https://evil.com/data")

    async def test_sandboxed_http_allows_whitelisted_host(self):
        client = SandboxedHttpClient(["api.anthropic.com"])
        assert client.is_host_allowed("api.anthropic.com")
        assert client.is_host_allowed("sub.api.anthropic.com")
        assert not client.is_host_allowed("evil.com")

    async def test_strategy_raw_httpx_blocked(self, sandbox_helper):
        class RawHttpxStrategy:
            name = "raw_httpx"
            version = "1.0.0"

            async def on_bar(self, _state, _portfolio):
                import httpx

                async with httpx.AsyncClient() as client:
                    await client.get("https://evil.com")
                return []

        sandbox = sandbox_helper.create(RawHttpxStrategy())
        signals = await sandbox.safe_evaluate(None, None, None)
        assert signals == []
        assert sandbox.metrics.errors == 1

    async def test_allowed_endpoint_configured(self, sandbox_helper):
        class NoopStrategy:
            name = "noop"
            version = "1.0.0"

            def on_bar(self, _state, _portfolio):
                return []

        sandbox = sandbox_helper.create(
            NoopStrategy(),
            network={"allowed_endpoints": ["api.anthropic.com"]},
        )
        assert sandbox._http_client is not None  # noqa: SLF001
        assert "api.anthropic.com" in sandbox._allowed_endpoints  # noqa: SLF001


# -- Layer 3: Resource Limits --------------------------------------------


class TestLayer3ResourceLimits:
    async def test_file_descriptor_limit_set(self, sandbox_helper):
        class NoopStrategy:
            name = "fd_test"
            version = "1.0.0"

            def on_bar(self, _state, _portfolio):
                return []

        sandbox = sandbox_helper.create(NoopStrategy())
        try:
            soft, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
            assert soft <= FD_LIMIT
        finally:
            sandbox.restore_limits()

    async def test_memory_limit_set(self, sandbox_helper):
        class NoopStrategy:
            name = "mem_test"
            version = "1.0.0"

            def on_bar(self, _state, _portfolio):
                return []

        sandbox = sandbox_helper.create(NoopStrategy())
        try:
            soft, _hard = resource.getrlimit(resource.RLIMIT_AS)
            assert soft > 0
        finally:
            sandbox.restore_limits()


# -- Layer 4: Filesystem Isolation ----------------------------------------


class TestLayer4FilesystemIsolation:
    async def test_read_etc_passwd_blocked(self, sandbox_helper):
        class ReadEtcStrategy:
            name = "read_etc"
            version = "1.0.0"

            def on_bar(self, _state, _portfolio):
                with open("/etc/passwd") as f:
                    f.read()
                return []

        sandbox = sandbox_helper.create(ReadEtcStrategy())
        signals = await sandbox.safe_evaluate(None, None, None)
        assert signals == []
        assert sandbox.metrics.errors == 1

    async def test_sandbox_working_dir_is_temp(self, sandbox_helper):
        class NoopStrategy:
            name = "dir_test"
            version = "1.0.0"

            def on_bar(self, _state, _portfolio):
                return []

        sandbox = sandbox_helper.create(NoopStrategy())
        try:
            assert sandbox._work_dir is not None  # noqa: SLF001
            assert sandbox._work_dir.startswith(tempfile.gettempdir())  # noqa: SLF001
        finally:
            sandbox.restore_limits()

    async def test_sandbox_work_dir_cleaned_after_evaluation(self, sandbox_helper):
        class NoopStrategy:
            name = "write_test"
            version = "1.0.0"

            def on_bar(self, _state, _portfolio):
                return []

        sandbox = sandbox_helper.create(NoopStrategy())
        work_dir = sandbox._work_dir  # noqa: SLF001
        await sandbox.safe_evaluate(None, None, None)
        assert not os.path.exists(work_dir)


# -- Layer 5: Process Isolation (documentation) --------------------------


class TestLayer5ProcessIsolation:
    def test_process_isolation_documented(self):
        from engine.plugins import sandbox

        doc = sandbox.StrategySandbox.__doc__ or ""
        lower = doc.lower()
        assert "process" in lower or "container" in lower or "production" in lower


# -- Integration ----------------------------------------------------------


class TestSandboxIntegration:
    async def test_timeout_returns_empty_signals(self, sandbox_helper):
        class InfiniteLoopStrategy:
            name = "infinite"
            version = "1.0.0"

            async def on_bar(self, _state, _portfolio):
                while True:
                    await asyncio.sleep(0.01)

        sandbox = sandbox_helper.create(
            InfiniteLoopStrategy(),
            resources={"max_cpu_seconds": 1},
        )
        signals = await sandbox.safe_evaluate(None, None, None)
        assert signals == []
        assert sandbox.metrics.errors == 1
        assert "Timeout" in (sandbox.metrics.last_error or "")

    async def test_multiple_evaluations_isolated(self, sandbox_helper):
        class CounterStrategy:
            name = "counter"
            version = "1.0.0"
            count = 0

            def on_bar(self, _state, _portfolio):
                CounterStrategy.count += 1
                return []

        sandbox = sandbox_helper.create(CounterStrategy())
        await sandbox.safe_evaluate(None, None, None)
        await sandbox.safe_evaluate(None, None, None)
        assert sandbox.metrics.total_evaluations == EVAL_LIMIT
