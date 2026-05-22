from __future__ import annotations

import builtins
import contextlib

import pytest

from engine.plugins.sandbox.core.policy import IntrospectionPolicy
from engine.plugins.sandbox.core.violation import IntrospectionViolation
from engine.plugins.sandbox.layers.introspection_guard import (
    _BLOCKED_ATTRS,
    _EXPLICITLY_BLOCKED_ATTRS,
    _FRAME_ATTRS,
    IntrospectionGuard,
    _RestrictedObject,
)


def _make_policy(**overrides: object) -> IntrospectionPolicy:
    defaults = {
        "blocked_builtins": {"eval", "exec", "compile"},
        "blocked_attributes": {"__subclasses__", "__globals__"},
        "blocked_dunder_access": True,
        "block_gc": True,
        "block_inspect": True,
        "block_frame_access": True,
    }
    defaults.update(overrides)
    return IntrospectionPolicy(**defaults)  # type: ignore[arg-type]


# ── Constants coverage ────────────────────────────────────────────────


class TestBlockedAttrConstants:
    def test_blocked_attrs_is_frozenset(self) -> None:
        assert isinstance(_BLOCKED_ATTRS, frozenset)

    def test_blocked_attrs_contains_dunder_globals(self) -> None:
        assert "__globals__" in _BLOCKED_ATTRS

    def test_blocked_attrs_contains_dunder_subclasses(self) -> None:
        assert "__subclasses__" in _BLOCKED_ATTRS

    def test_blocked_attrs_contains_dunder_bases(self) -> None:
        assert "__bases__" in _BLOCKED_ATTRS

    def test_blocked_attrs_contains_dunder_mro(self) -> None:
        assert "__mro__" in _BLOCKED_ATTRS

    def test_blocked_attrs_contains_dunder_closure(self) -> None:
        assert "__closure__" in _BLOCKED_ATTRS

    def test_blocked_attrs_contains_dunder_code(self) -> None:
        assert "__code__" in _BLOCKED_ATTRS

    def test_blocked_attrs_contains_dunder_dict(self) -> None:
        assert "__dict__" in _BLOCKED_ATTRS

    def test_explicitly_blocked_attrs_is_frozenset(self) -> None:
        assert isinstance(_EXPLICITLY_BLOCKED_ATTRS, frozenset)

    def test_explicitly_blocked_contains_init_subclass(self) -> None:
        assert "__init_subclass__" in _EXPLICITLY_BLOCKED_ATTRS

    def test_explicitly_blocked_contains_instancecheck(self) -> None:
        assert "__instancecheck__" in _EXPLICITLY_BLOCKED_ATTRS

    def test_explicitly_blocked_contains_subclasscheck(self) -> None:
        assert "__subclasscheck__" in _EXPLICITLY_BLOCKED_ATTRS

    def test_explicitly_blocked_contains_reduce(self) -> None:
        assert "__reduce__" in _EXPLICITLY_BLOCKED_ATTRS

    def test_explicitly_blocked_contains_reduce_ex(self) -> None:
        assert "__reduce_ex__" in _EXPLICITLY_BLOCKED_ATTRS

    def test_explicitly_blocked_contains_getstate(self) -> None:
        assert "__getstate__" in _EXPLICITLY_BLOCKED_ATTRS

    def test_explicitly_blocked_contains_setstate(self) -> None:
        assert "__setstate__" in _EXPLICITLY_BLOCKED_ATTRS

    def test_frame_attrs_is_frozenset(self) -> None:
        assert isinstance(_FRAME_ATTRS, frozenset)

    def test_frame_attrs_contains_tb_frame(self) -> None:
        assert "tb_frame" in _FRAME_ATTRS

    def test_frame_attrs_contains_f_back(self) -> None:
        assert "f_back" in _FRAME_ATTRS

    def test_frame_attrs_contains_f_builtins(self) -> None:
        assert "f_builtins" in _FRAME_ATTRS

    def test_frame_attrs_contains_f_code(self) -> None:
        assert "f_code" in _FRAME_ATTRS

    def test_frame_attrs_contains_f_globals(self) -> None:
        assert "f_globals" in _FRAME_ATTRS

    def test_frame_attrs_contains_f_locals(self) -> None:
        assert "f_locals" in _FRAME_ATTRS

    def test_frame_attrs_contains_dunder_traceback(self) -> None:
        assert "__traceback__" in _FRAME_ATTRS

    def test_frame_attrs_contains_dunder_context(self) -> None:
        assert "__context__" in _FRAME_ATTRS

    def test_frame_attrs_contains_dunder_cause(self) -> None:
        assert "__cause__" in _FRAME_ATTRS

    def test_no_overlap_between_blocked_and_explicitly_blocked(self) -> None:
        assert _BLOCKED_ATTRS.isdisjoint(_EXPLICITLY_BLOCKED_ATTRS)

    def test_no_overlap_between_blocked_and_frame(self) -> None:
        assert _BLOCKED_ATTRS.isdisjoint(_FRAME_ATTRS)

    def test_no_overlap_between_explicitly_blocked_and_frame(self) -> None:
        assert _EXPLICITLY_BLOCKED_ATTRS.isdisjoint(_FRAME_ATTRS)


# ── _RestrictedObject ─────────────────────────────────────────────────


class TestRestrictedObject:
    def test_subclasses_raises_runtime_error(self) -> None:
        with pytest.raises(RuntimeError, match="not allowed"):
            _RestrictedObject.__subclasses__()

    def test_is_class(self) -> None:
        assert isinstance(_RestrictedObject, type)


# ── IntrospectionGuard._is_blocked_attr ───────────────────────────────


class TestIsBlockedAttr:
    def test_blocked_attr_from_constant_set(self) -> None:
        guard = IntrospectionGuard(_make_policy())
        assert guard._is_blocked_attr("__globals__") is True

    def test_explicitly_blocked_attr(self) -> None:
        guard = IntrospectionGuard(_make_policy())
        assert guard._is_blocked_attr("__reduce__") is True

    def test_frame_attr_when_frame_access_blocked(self) -> None:
        guard = IntrospectionGuard(_make_policy(block_frame_access=True))
        assert guard._is_blocked_attr("tb_frame") is True

    def test_frame_attr_when_frame_access_allowed(self) -> None:
        guard = IntrospectionGuard(_make_policy(block_frame_access=False))
        assert guard._is_blocked_attr("tb_frame") is False

    def test_custom_blocked_attribute(self) -> None:
        guard = IntrospectionGuard(
            _make_policy(blocked_attributes={"__subclasses__", "__my_secret__"})
        )
        assert guard._is_blocked_attr("__my_secret__") is True

    def test_non_blocked_attr_passes(self) -> None:
        guard = IntrospectionGuard(_make_policy())
        assert guard._is_blocked_attr("name") is False
        assert guard._is_blocked_attr("value") is False
        assert guard._is_blocked_attr("__len__") is False
        assert guard._is_blocked_attr("__str__") is False

    def test_all_blocked_attrs_constant_set_blocked(self) -> None:
        guard = IntrospectionGuard(_make_policy())
        for attr in _BLOCKED_ATTRS:
            assert guard._is_blocked_attr(attr) is True, f"{attr} should be blocked"

    def test_all_explicitly_blocked_attrs_blocked(self) -> None:
        guard = IntrospectionGuard(_make_policy())
        for attr in _EXPLICITLY_BLOCKED_ATTRS:
            assert guard._is_blocked_attr(attr) is True, f"{attr} should be blocked"

    def test_all_frame_attrs_blocked_when_enabled(self) -> None:
        guard = IntrospectionGuard(_make_policy(block_frame_access=True))
        for attr in _FRAME_ATTRS:
            assert guard._is_blocked_attr(attr) is True, f"{attr} should be blocked"


# ── IntrospectionGuard.__init__ ───────────────────────────────────────


class TestInit:
    def test_default_state(self) -> None:
        policy = _make_policy()
        guard = IntrospectionGuard(policy)
        assert guard._policy is policy
        assert guard._plugin_id is None
        assert guard._installed is False
        assert guard._violation_log == []

    def test_with_plugin_id(self) -> None:
        guard = IntrospectionGuard(_make_policy(), plugin_id="my_plugin")
        assert guard._plugin_id == "my_plugin"


# ── IntrospectionGuard._restricted_getattr ────────────────────────────


class TestRestrictedGetattr:
    def test_blocked_attr_raises_permission_error(self) -> None:
        guard = IntrospectionGuard(_make_policy())
        guard._original_getattr = getattr
        with pytest.raises(PermissionError, match="not accessible"):
            guard._restricted_getattr(str, "__globals__")

    def test_blocked_attr_logs_violation(self) -> None:
        guard = IntrospectionGuard(_make_policy())
        guard._original_getattr = getattr
        with pytest.raises(PermissionError):
            guard._restricted_getattr(str, "__subclasses__")
        assert len(guard._violation_log) == 1
        assert guard._violation_log[0].attribute == "__subclasses__"

    def test_allowed_attr_passes_through(self) -> None:
        guard = IntrospectionGuard(_make_policy())
        guard._original_getattr = getattr
        result = guard._restricted_getattr(str, "__len__")
        assert result is str.__len__

    def test_violation_includes_plugin_id(self) -> None:
        guard = IntrospectionGuard(_make_policy(), plugin_id="p42")
        guard._original_getattr = getattr
        with pytest.raises(PermissionError):
            guard._restricted_getattr(str, "__bases__")
        assert guard._violation_log[0].plugin_id == "p42"

    def test_default_parameter_passed_through(self) -> None:
        guard = IntrospectionGuard(_make_policy())
        guard._original_getattr = getattr
        result = guard._restricted_getattr(object(), "nonexistent", "fallback")
        assert result == "fallback"


# ── IntrospectionGuard._restricted_setattr ────────────────────────────


class TestRestrictedSetattr:
    def test_blocked_attr_raises_permission_error(self) -> None:
        guard = IntrospectionGuard(_make_policy())
        guard._original_setattr = setattr

        class Dummy:
            pass

        obj = Dummy()
        with pytest.raises(PermissionError, match="not accessible"):
            guard._restricted_setattr(obj, "__globals__", "evil")

    def test_blocked_attr_logs_violation(self) -> None:
        guard = IntrospectionGuard(_make_policy())
        guard._original_setattr = setattr

        class Dummy:
            pass

        with pytest.raises(PermissionError):
            guard._restricted_setattr(Dummy(), "__code__", None)
        assert len(guard._violation_log) == 1

    def test_allowed_setattr_passes_through(self) -> None:
        guard = IntrospectionGuard(_make_policy())
        guard._original_setattr = setattr

        class Dummy:
            x = 0

        obj = Dummy()
        guard._restricted_setattr(obj, "x", 42)
        assert obj.x == 42


# ── IntrospectionGuard._restricted_dir ────────────────────────────────


class TestRestrictedDir:
    def test_filters_blocked_attrs_from_dir(self) -> None:
        guard = IntrospectionGuard(_make_policy())
        guard._original_dir = dir
        result = guard._restricted_dir(str)
        for attr in _BLOCKED_ATTRS:
            assert attr not in result, f"{attr} should be filtered from dir()"
        for attr in _EXPLICITLY_BLOCKED_ATTRS:
            assert attr not in result, f"{attr} should be filtered from dir()"

    def test_filters_frame_attrs(self) -> None:
        guard = IntrospectionGuard(_make_policy(block_frame_access=True))
        guard._original_dir = dir
        result = guard._restricted_dir(str)
        for attr in _FRAME_ATTRS:
            assert attr not in result, f"{attr} should be filtered from dir()"

    def test_no_args_returns_globals(self) -> None:
        guard = IntrospectionGuard(_make_policy())
        guard._original_dir = dir
        result = guard._restricted_dir()
        assert isinstance(result, list)
        for attr in _BLOCKED_ATTRS:
            assert attr not in result

    def test_custom_blocked_attrs_filtered(self) -> None:
        guard = IntrospectionGuard(
            _make_policy(blocked_attributes={"__subclasses__", "__custom_attr__"})
        )
        guard._original_dir = dir
        result = guard._restricted_dir(str)
        assert "__custom_attr__" not in result

    def test_normal_attrs_preserved(self) -> None:
        guard = IntrospectionGuard(_make_policy())
        guard._original_dir = dir
        result = guard._restricted_dir(str)
        assert "upper" in result
        assert "lower" in result
        assert "strip" in result


# ── IntrospectionGuard._make_restricted_builtin ───────────────────────


class TestMakeRestrictedBuiltin:
    def test_returns_callable(self) -> None:
        guard = IntrospectionGuard(_make_policy())
        blocked = guard._make_restricted_builtin("eval")
        assert callable(blocked)

    def test_callable_raises_permission_error(self) -> None:
        guard = IntrospectionGuard(_make_policy())
        blocked = guard._make_restricted_builtin("eval")
        with pytest.raises(PermissionError, match="not available"):
            blocked()

    def test_callable_logs_violation(self) -> None:
        guard = IntrospectionGuard(_make_policy())
        blocked = guard._make_restricted_builtin("exec")
        with pytest.raises(PermissionError):
            blocked("print(1)")
        assert len(guard._violation_log) == 1
        assert guard._violation_log[0].attribute == "exec"

    def test_callable_with_kwargs_raises(self) -> None:
        guard = IntrospectionGuard(_make_policy())
        blocked = guard._make_restricted_builtin("compile")
        with pytest.raises(PermissionError):
            blocked("code", filename="test", mode="exec")

    def test_violation_includes_plugin_id(self) -> None:
        guard = IntrospectionGuard(_make_policy(), plugin_id="z99")
        blocked = guard._make_restricted_builtin("eval")
        with pytest.raises(PermissionError):
            blocked("1+1")
        assert guard._violation_log[0].plugin_id == "z99"


# ── IntrospectionGuard.install / uninstall ────────────────────────────


class TestInstallUninstall:
    def test_install_sets_installed_flag(self) -> None:
        guard = IntrospectionGuard(_make_policy())
        guard.install()
        try:
            assert guard._installed is True
        finally:
            guard.uninstall()

    def test_install_replaces_builtins_getattr(self) -> None:
        original = builtins.getattr
        guard = IntrospectionGuard(_make_policy())
        guard.install()
        try:
            assert builtins.getattr is not original
            assert builtins.getattr.__func__ is guard._restricted_getattr.__func__
        finally:
            guard.uninstall()

    def test_install_replaces_builtins_setattr(self) -> None:
        original = builtins.setattr
        guard = IntrospectionGuard(_make_policy())
        guard.install()
        try:
            assert builtins.setattr is not original
            assert builtins.setattr.__func__ is guard._restricted_setattr.__func__
        finally:
            guard.uninstall()

    def test_install_replaces_builtins_dir(self) -> None:
        original = builtins.dir
        guard = IntrospectionGuard(_make_policy())
        guard.install()
        try:
            assert builtins.dir is not original
            assert builtins.dir.__func__ is guard._restricted_dir.__func__
        finally:
            guard.uninstall()

    def test_install_replaces_builtins_object(self) -> None:
        original = builtins.object
        guard = IntrospectionGuard(_make_policy())
        guard.install()
        try:
            assert builtins.object is _RestrictedObject
        finally:
            guard.uninstall()

    def test_install_blocks_declared_builtins(self) -> None:
        guard = IntrospectionGuard(
            _make_policy(blocked_builtins={"eval", "exec", "compile"})
        )
        guard.install()
        try:
            with pytest.raises(PermissionError, match="not available"):
                builtins.eval("1+1")
            with pytest.raises(PermissionError, match="not available"):
                builtins.exec("x=1")
            with pytest.raises(PermissionError, match="not available"):
                builtins.compile("1", "test", "eval")
        finally:
            guard.uninstall()

    def test_uninstall_restores_originals(self) -> None:
        orig_getattr = builtins.getattr
        orig_setattr = builtins.setattr
        orig_dir = builtins.dir
        orig_object = builtins.object
        orig_eval = builtins.eval

        guard = IntrospectionGuard(
            _make_policy(blocked_builtins={"eval"})
        )
        guard.install()
        guard.uninstall()

        assert builtins.getattr is orig_getattr
        assert builtins.setattr is orig_setattr
        assert builtins.dir is orig_dir
        assert builtins.object is orig_object
        assert builtins.eval is orig_eval

    def test_uninstall_clears_installed_flag(self) -> None:
        guard = IntrospectionGuard(_make_policy())
        guard.install()
        guard.uninstall()
        assert guard._installed is False

    def test_uninstall_clears_original_refs(self) -> None:
        guard = IntrospectionGuard(_make_policy())
        guard.install()
        guard.uninstall()
        assert guard._original_getattr is None
        assert guard._original_setattr is None
        assert guard._original_dir is None
        assert guard._original_object is None
        assert guard._original_builtins == {}

    def test_double_install_is_noop(self) -> None:
        guard = IntrospectionGuard(_make_policy())
        guard.install()
        first_getattr = builtins.getattr
        guard.install()
        try:
            assert builtins.getattr is first_getattr
        finally:
            guard.uninstall()

    def test_double_uninstall_is_safe(self) -> None:
        guard = IntrospectionGuard(_make_policy())
        guard.install()
        guard.uninstall()
        guard.uninstall()
        assert guard._installed is False

    def test_uninstall_without_install_is_safe(self) -> None:
        guard = IntrospectionGuard(_make_policy())
        guard.uninstall()
        assert guard._installed is False

    def test_install_with_no_blocked_builtins(self) -> None:
        guard = IntrospectionGuard(_make_policy(blocked_builtins=set()))
        guard.install()
        try:
            result = builtins.eval("2+2")  # noqa: S307
            assert result == 4
        finally:
            guard.uninstall()


# ── IntrospectionGuard.get_violations / clear_violations ───────────────


class TestViolationTracking:
    def test_get_violations_returns_empty_initially(self) -> None:
        guard = IntrospectionGuard(_make_policy())
        assert guard.get_violations() == []

    def test_get_violations_returns_copy(self) -> None:
        guard = IntrospectionGuard(_make_policy())
        guard._violation_log.append(
            IntrospectionViolation("__globals__", plugin_id="t")
        )
        v1 = guard.get_violations()
        v2 = guard.get_violations()
        assert v1 is not v2
        assert v1 == v2

    def test_clear_violations(self) -> None:
        guard = IntrospectionGuard(_make_policy())
        guard._violation_log.append(
            IntrospectionViolation("__globals__", plugin_id="t")
        )
        guard.clear_violations()
        assert guard.get_violations() == []

    def test_violations_accumulate(self) -> None:
        guard = IntrospectionGuard(_make_policy())
        guard._original_getattr = getattr
        for attr in ("__globals__", "__subclasses__", "__bases__"):
            with contextlib.suppress(PermissionError):
                guard._restricted_getattr(str, attr)
        assert len(guard.get_violations()) == 3

    def test_violations_record_correct_attributes(self) -> None:
        guard = IntrospectionGuard(_make_policy())
        guard._original_getattr = getattr
        with pytest.raises(PermissionError):
            guard._restricted_getattr(str, "__globals__")
        assert guard._violation_log[0].attribute == "__globals__"


# ── Integration: install → use → uninstall round-trip ─────────────────


class TestInstallIntegration:
    def test_getattr_blocked_after_install(self) -> None:
        guard = IntrospectionGuard(_make_policy())
        guard.install()
        try:
            with pytest.raises(PermissionError, match="not accessible"):
                _ = str.__globals__
        finally:
            guard.uninstall()

    def test_setattr_blocked_after_install(self) -> None:
        guard = IntrospectionGuard(_make_policy())
        guard.install()
        try:

            class Dummy:
                pass

            obj = Dummy()
            with pytest.raises(PermissionError, match="not accessible"):
                obj.__code__ = None
        finally:
            guard.uninstall()

    def test_dir_filters_after_install(self) -> None:
        guard = IntrospectionGuard(_make_policy())
        guard.install()
        try:
            result = builtins.dir(str)
            assert "__globals__" not in result
            assert "__subclasses__" not in result
            assert "upper" in result
        finally:
            guard.uninstall()

    def test_object_subclasses_blocked_after_install(self) -> None:
        guard = IntrospectionGuard(_make_policy())
        guard.install()
        try:
            with pytest.raises(RuntimeError, match="not allowed"):
                object.__subclasses__()
        finally:
            guard.uninstall()

    def test_normal_getattr_works_after_install(self) -> None:
        guard = IntrospectionGuard(_make_policy())
        guard.install()
        try:
            result = str.upper
            assert result is str.upper
        finally:
            guard.uninstall()

    def test_normal_dir_works_after_install(self) -> None:
        guard = IntrospectionGuard(_make_policy())
        guard.install()
        try:
            result = builtins.dir(str)
            assert "upper" in result
            assert isinstance(result, list)
        finally:
            guard.uninstall()

    def test_install_uninstall_preserves_builtins(self) -> None:
        orig_eval = builtins.eval
        orig_getattr = builtins.getattr
        guard = IntrospectionGuard(
            _make_policy(blocked_builtins={"eval"})
        )
        guard.install()
        guard.uninstall()
        assert builtins.eval is orig_eval
        assert builtins.getattr is orig_getattr
        assert builtins.eval("2+3") == 5  # noqa: S307

    def test_violations_recorded_during_install(self) -> None:
        guard = IntrospectionGuard(_make_policy())
        guard.install()
        try:
            with pytest.raises(PermissionError):
                _ = str.__globals__
            assert len(guard.get_violations()) == 1
        finally:
            guard.uninstall()

    def test_blocked_builtin_not_overwritten_if_not_present(self) -> None:
        guard = IntrospectionGuard(
            _make_policy(blocked_builtins={"nonexistent_builtin_xyz"})
        )
        guard.install()
        guard.uninstall()
        assert not hasattr(builtins, "nonexistent_builtin_xyz") or callable(
            builtins.nonexistent_builtin_xyz
        )
