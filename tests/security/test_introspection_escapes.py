from __future__ import annotations

import builtins

import pytest

from engine.plugins.sandbox.core.policy import IntrospectionPolicy
from engine.plugins.sandbox.layers.introspection_guard import (
    _EXPLICITLY_BLOCKED_ATTRS,
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


class TestPassThroughAfterUninstall:
    def test_getattr_passes_through_after_uninstall(self) -> None:
        policy = IntrospectionPolicy()
        guard = IntrospectionGuard(policy)
        guard.install()
        patched_fn = guard._restricted_getattr
        guard.uninstall()
        result = patched_fn("hello", "upper")
        assert callable(result)

    def test_setattr_passes_through_after_uninstall(self) -> None:
        policy = IntrospectionPolicy()
        guard = IntrospectionGuard(policy)
        guard.install()
        patched_fn = guard._restricted_setattr
        guard.uninstall()
        obj = type("Obj", (), {"x": 0})()
        patched_fn(obj, "x", 42)
        assert obj.x == 42

    def test_blocked_builtin_passes_through_after_uninstall(self) -> None:
        policy = IntrospectionPolicy()
        guard = IntrospectionGuard(policy)
        guard.install()
        blocked_eval = builtins.eval
        guard.uninstall()
        assert blocked_eval("1+1") == 2

    def test_getattr_globals_allowed_after_uninstall(self) -> None:
        policy = IntrospectionPolicy()
        guard = IntrospectionGuard(policy)
        guard.install()

        def sample_fn() -> None:
            pass

        guard.uninstall()
        result = guard._restricted_getattr(sample_fn, "__globals__")
        assert isinstance(result, dict)


class TestAtexitSafetyNet:
    def test_atexit_cleanup_uninstalls_guard(self) -> None:
        policy = IntrospectionPolicy()
        guard = IntrospectionGuard(policy)
        original_getattr = builtins.getattr
        original_object = builtins.object
        guard.install()
        assert guard._installed
        guard._atexit_cleanup()
        assert not guard._installed
        assert builtins.getattr is original_getattr
        assert builtins.object is original_object

    def test_atexit_cleanup_noop_if_not_installed(self) -> None:
        policy = IntrospectionPolicy()
        guard = IntrospectionGuard(policy)
        guard._atexit_cleanup()
        assert not guard._installed

    def test_atexit_cleanup_suppresses_exceptions(self) -> None:
        policy = IntrospectionPolicy()
        guard = IntrospectionGuard(policy)
        original_getattr = builtins.getattr
        guard.install()
        builtins.getattr = original_getattr
        guard._atexit_cleanup()
        assert not guard._installed


class TestClosureBasedUninstallFlag:
    def test_uninstall_flag_is_thread_local(self) -> None:
        import threading

        from engine.plugins.sandbox.layers.introspection_guard import (
            _is_uninstalling,
            _set_uninstalling,
        )

        results = {}

        def worker(name: str) -> None:
            _set_uninstalling(True)
            results[name] = _is_uninstalling()

        t1 = threading.Thread(target=worker, args=("t1",))
        t1.start()
        t1.join()
        assert results["t1"] is True
        assert not _is_uninstalling()
        _set_uninstalling(False)

    def test_uninstall_flag_default_is_false(self) -> None:
        from engine.plugins.sandbox.layers.introspection_guard import _is_uninstalling

        assert not _is_uninstalling()
