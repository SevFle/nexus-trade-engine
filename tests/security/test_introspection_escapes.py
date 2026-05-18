from __future__ import annotations

import builtins
import inspect
import types

import pytest

from engine.plugins.sandbox.core.policy import IntrospectionPolicy
from engine.plugins.sandbox.layers.introspection_guard import (
    _ALLOWED_CALLER_PREFIXES,
    _EXPLICITLY_BLOCKED_ATTRS,
    _SANDBOX_STRATEGY_PREFIXES,
    IntrospectionGuard,
)


@pytest.fixture
def strict_policy() -> IntrospectionPolicy:
    return IntrospectionPolicy()


@pytest.fixture
def guard(strict_policy: IntrospectionPolicy) -> IntrospectionGuard:
    return IntrospectionGuard(strict_policy, plugin_id="test_plugin")


class TestBlockedBuiltinFunctions:
    def test_eval_blocked(self, guard: IntrospectionGuard) -> None:
        guard.install()
        try:
            with pytest.raises(PermissionError, match="not accessible"):
                builtins.eval("1+1")  # noqa: S307
        finally:
            guard.uninstall()

    def test_exec_blocked(self, guard: IntrospectionGuard) -> None:
        guard.install()
        try:
            with pytest.raises(PermissionError, match="not accessible"):
                builtins.exec("x = 1")  # noqa: S102
        finally:
            guard.uninstall()

    def test_compile_blocked(self, guard: IntrospectionGuard) -> None:
        guard.install()
        try:
            with pytest.raises(PermissionError, match="not accessible"):
                builtins.compile("1+1", "<test>", "eval")
        finally:
            guard.uninstall()

    def test_breakpoint_blocked(self, guard: IntrospectionGuard) -> None:
        guard.install()
        try:
            with pytest.raises(PermissionError, match="not accessible"):
                builtins.breakpoint()
        finally:
            guard.uninstall()


class TestBlockedAttributes:
    @pytest.mark.parametrize("attr", sorted(_EXPLICITLY_BLOCKED_ATTRS))
    def test_blocked_attr_raises(self, attr: str, guard: IntrospectionGuard) -> None:
        guard.install()
        try:
            with pytest.raises(PermissionError, match="not accessible"):
                builtins.getattr(int, attr)
        finally:
            guard.uninstall()

    def test_subclasses_on_object_blocked(self, guard: IntrospectionGuard) -> None:
        guard.install()
        try:
            with pytest.raises(RuntimeError, match="not allowed"):
                builtins.object.__subclasses__()
        finally:
            guard.uninstall()

    def test_globals_on_function_blocked(self, guard: IntrospectionGuard) -> None:
        guard.install()
        try:

            def sample_fn() -> None:
                pass

            with pytest.raises(PermissionError, match="not accessible"):
                builtins.getattr(sample_fn, "__globals__")  # noqa: B009
        finally:
            guard.uninstall()

    def test_bases_on_class_blocked(self, guard: IntrospectionGuard) -> None:
        guard.install()
        try:
            with pytest.raises(PermissionError, match="not accessible"):
                builtins.getattr(int, "__bases__")  # noqa: B009
        finally:
            guard.uninstall()

    def test_class_attribute_blocked(self, guard: IntrospectionGuard) -> None:
        guard.install()
        try:
            with pytest.raises(PermissionError, match="not accessible"):
                builtins.getattr(42, "__class__")  # noqa: B009
        finally:
            guard.uninstall()


class TestFrameAccessBlocking:
    def test_tb_frame_blocked(self, guard: IntrospectionGuard) -> None:
        guard.install()
        try:
            with pytest.raises(PermissionError, match="not accessible"):
                builtins.getattr(Exception(), "tb_frame")  # noqa: B009
        finally:
            guard.uninstall()

    def test_f_back_blocked(self, guard: IntrospectionGuard) -> None:
        guard.install()
        try:
            with pytest.raises(PermissionError, match="not accessible"):
                builtins.getattr(object(), "f_back")  # noqa: B009
        finally:
            guard.uninstall()

    def test_f_globals_blocked(self, guard: IntrospectionGuard) -> None:
        guard.install()
        try:
            with pytest.raises(PermissionError, match="not accessible"):
                builtins.getattr(object(), "f_globals")  # noqa: B009
        finally:
            guard.uninstall()


class TestSafeAttributeAccess:
    def test_normal_getattr_works(self, guard: IntrospectionGuard) -> None:
        guard.install()
        try:
            result = builtins.getattr("hello", "upper")  # noqa: B009
            assert callable(result)
        finally:
            guard.uninstall()

    def test_name_attribute_allowed(self, guard: IntrospectionGuard) -> None:
        guard.install()
        try:

            def my_func() -> None:
                pass

            name = builtins.getattr(my_func, "__name__")  # noqa: B009
            assert name == "my_func"
        finally:
            guard.uninstall()

    def test_doc_attribute_allowed(self, guard: IntrospectionGuard) -> None:
        guard.install()
        try:
            doc = builtins.getattr(str, "__doc__")  # noqa: B009
            assert isinstance(doc, str)
        finally:
            guard.uninstall()

    def test_len_attribute_allowed(self, guard: IntrospectionGuard) -> None:
        guard.install()
        try:
            result = builtins.getattr([1, 2, 3], "__len__")  # noqa: B009
            assert result() == 3
        finally:
            guard.uninstall()


class TestIntrospectionGuardLifecycle:
    def test_install_restores_on_uninstall(self) -> None:
        policy = IntrospectionPolicy()
        guard = IntrospectionGuard(policy)
        original_eval = builtins.eval
        original_getattr = builtins.getattr
        original_object = builtins.object

        guard.install()
        assert builtins.eval is not original_eval
        assert builtins.getattr is not original_getattr
        assert builtins.object is not original_object

        guard.uninstall()
        assert builtins.eval is original_eval
        assert builtins.getattr is original_getattr
        assert builtins.object is original_object

    def test_double_install_safe(self, guard: IntrospectionGuard) -> None:
        guard.install()
        guard.install()
        guard.uninstall()
        guard.uninstall()

    def test_violation_logging(self, guard: IntrospectionGuard) -> None:
        guard.install()
        try:
            with pytest.raises(PermissionError):
                builtins.eval("1")  # noqa: S307
        finally:
            guard.uninstall()
        violations = guard.get_violations()
        assert len(violations) >= 1
        guard.clear_violations()
        assert len(guard.get_violations()) == 0


class TestEscapeVectors:
    def test_class_chain_via_getattr_blocked(self, guard: IntrospectionGuard) -> None:
        guard.install()
        try:
            with pytest.raises(PermissionError):
                builtins.getattr(int, "__class__")  # noqa: B009
        finally:
            guard.uninstall()

    def test_subclasses_chain_blocked(self, guard: IntrospectionGuard) -> None:
        guard.install()
        try:
            with pytest.raises((PermissionError, RuntimeError)):
                builtins.getattr(builtins.object, "__subclasses__")()  # noqa: B009
        finally:
            guard.uninstall()

    def test_mro_chain_blocked(self, guard: IntrospectionGuard) -> None:
        guard.install()
        try:
            with pytest.raises(PermissionError):
                builtins.getattr(int, "__mro__")  # noqa: B009
        finally:
            guard.uninstall()


class TestTrustedLibraryBypass:
    def test_inspect_getfullargspec_allowed(self, guard: IntrospectionGuard) -> None:
        guard.install()
        try:

            def sample_fn(x: int, y: str = "hello") -> None:
                pass

            spec = inspect.getfullargspec(sample_fn)
            assert spec.args == ["x", "y"]
        finally:
            guard.uninstall()

    def test_inspect_get_annotations_allowed(self, guard: IntrospectionGuard) -> None:
        guard.install()
        try:

            def sample_fn(x: int) -> str:
                pass

            annotations = inspect.get_annotations(sample_fn)
            assert annotations == {"x": int, "return": str}
        finally:
            guard.uninstall()

    def test_inspect_signature_allowed(self, guard: IntrospectionGuard) -> None:
        guard.install()
        try:

            def sample_fn(a: int, b: int = 0) -> int:
                return a + b

            sig = inspect.signature(sample_fn)
            assert "a" in sig.parameters
        finally:
            guard.uninstall()

    def test_direct_getattr_still_blocked(self, guard: IntrospectionGuard) -> None:
        guard.install()
        try:

            def sample_fn() -> None:
                pass

            with pytest.raises(PermissionError, match="not accessible"):
                builtins.getattr(sample_fn, "__globals__")  # noqa: B009
        finally:
            guard.uninstall()


class TestSandboxStrategyContext:
    def test_sandbox_strategy_frame_blocks_even_through_inspect(
        self, guard: IntrospectionGuard
    ) -> None:
        mod = types.ModuleType("engine.plugins.sandbox.strategy.test_plugin")
        code = compile(
            "import builtins\n"
            "result = builtins.getattr(lambda: None, '__globals__')\n",
            "<sandbox_strategy>",
            "exec",
        )
        guard.install()
        try:
            with pytest.raises(PermissionError, match="not accessible"):
                exec(code, {"__builtins__": builtins, "__name__": mod.__name__})  # noqa: S102
        finally:
            guard.uninstall()


class TestAtexitBypass:
    def test_atexit_module_caller_allowed(self, guard: IntrospectionGuard) -> None:
        atexit_ns = {"__name__": "atexit", "__builtins__": builtins}
        code = compile(
            "import builtins\n"
            "result = builtins.getattr(lambda: None, '__globals__')\n",
            "<atexit>",
            "exec",
        )
        guard.install()
        try:
            exec(code, atexit_ns)  # noqa: S102
            assert "result" in atexit_ns
        finally:
            guard.uninstall()


class TestBypassConfiguration:
    def test_allowed_caller_prefixes_includes_sqlalchemy(self) -> None:
        assert "sqlalchemy" in _ALLOWED_CALLER_PREFIXES

    def test_allowed_caller_prefixes_includes_opentelemetry(self) -> None:
        assert "opentelemetry" in _ALLOWED_CALLER_PREFIXES

    def test_allowed_caller_prefixes_includes_inspect(self) -> None:
        assert "inspect" in _ALLOWED_CALLER_PREFIXES

    def test_allowed_caller_prefixes_includes_pytest(self) -> None:
        assert any(p in _ALLOWED_CALLER_PREFIXES for p in ("pytest", "_pytest"))

    def test_sandbox_strategy_prefixes_defined(self) -> None:
        assert len(_SANDBOX_STRATEGY_PREFIXES) > 0
        for prefix in _SANDBOX_STRATEGY_PREFIXES:
            assert prefix.startswith("engine.plugins.sandbox.")
