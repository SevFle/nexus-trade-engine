"""Complementary tests for the SEV-741 security fix in
``engine.api.auth.base.IAuthProvider.map_roles`` and the new
``auth_overwrite_role_on_login`` setting.

These tests complement ``tests/test_auth_role_promotion_security_fix.py``
by covering gaps that the existing file does not exercise:

* Determinism (idempotence) of ``map_roles`` under repeated calls
* Property-based tests using ``hypothesis`` for arbitrary inputs
* Thread-safety: ``map_roles`` is a pure function and must be safe
  to call concurrently from multiple threads
* Boundary conditions: very large input lists, deeply nested
  whitespace, non-ASCII characters
* Idempotence on the actual UserInfo/AuthResult dataclasses
* Static-source invariants (no re-introduction of helper tables)
* ``auth_overwrite_role_on_login`` propagation through the LDAP
  provider branch (which currently overwrites unconditionally) —
  this pins current behavior so a future fix is testable.
"""

from __future__ import annotations

import inspect
import threading
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from engine.api.auth.base import AuthResult, IAuthProvider, UserInfo
from engine.config import Settings
from engine.config import settings as global_settings


class _ConcreteProvider(IAuthProvider):
    @property
    def name(self) -> str:
        return "test-properties"

    async def authenticate(self, **kwargs: Any) -> AuthResult:
        return AuthResult()


# ---------------------------------------------------------------------------
# 1. Determinism and idempotence
# ---------------------------------------------------------------------------


class TestMapRolesDeterminism:
    """``map_roles`` must be a pure function — calling it twice with the
    same arguments must yield identical results, and must not produce
    observable side effects beyond the documented warning log."""

    @pytest.mark.parametrize(
        "external",
        [
            [],
            ["viewer"],
            ["admin"],
            ["quant_dev", "viewer"],
            ["unknown_role"],
            ["ADMIN", "  admin  "],
            ["admin", "totally_bogus"],
        ],
    )
    def test_repeated_calls_produce_identical_results(self, external):
        p = _ConcreteProvider()
        first = p.map_roles(list(external))
        for _ in range(5):
            assert p.map_roles(list(external)) == first

    def test_call_order_independence_for_recognized_roles(self):
        """The output depends only on the *set* of recognized roles —
        not on the order they appear in the input list."""
        p = _ConcreteProvider()
        roles = ["viewer", "user", "retail_trader", "quant_dev",
                 "developer", "portfolio_manager", "admin"]
        baseline = p.map_roles(roles)
        # Re-order and ensure same answer.
        assert p.map_roles(list(reversed(roles))) == baseline
        assert p.map_roles([roles[3], roles[0], roles[6], roles[1]]) == baseline

    def test_no_bare_print_to_stdout(self, capsys):
        """``map_roles`` must not use bare ``print()`` — any stdout
        output must come from the configured structlog handler, not
        from ad-hoc debugging prints. We assert that any captured
        output is a structured log line containing the canonical
        event name, not arbitrary text."""
        p = _ConcreteProvider()
        p.map_roles(["bogus_for_warnings"])
        captured = capsys.readouterr()
        # If anything was captured, it must be a structured warning,
        # not an unstructured ``print(...)``.
        if captured.out:
            assert "auth.map_roles.unrecognized_roles" in captured.out
        if captured.err:
            assert "auth.map_roles.unrecognized_roles" in captured.err

    def test_no_io_side_effects_for_recognized_input(self, capsys):
        """When all roles are recognized, no warning is emitted at
        all — so stdout/stderr must be empty."""
        p = _ConcreteProvider()
        p.map_roles(["admin", "viewer"])
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""


# ---------------------------------------------------------------------------
# 2. Property-based / fuzz testing with hypothesis
# ---------------------------------------------------------------------------


_RECOGNIZED_ROLES = ["viewer", "user", "retail_trader",
                     "quant_dev", "developer", "portfolio_manager", "admin"]


class TestMapRolesPropertyBased:
    """Property-based tests asserting invariants of ``map_roles`` over
    arbitrary inputs."""

    @staticmethod
    def _role_strategy():
        # Mix of recognized, unrecognized, and edge-case strings.
        return st.one_of(
            st.sampled_from(_RECOGNIZED_ROLES),
            st.sampled_from(["root", "superuser", "l33t", "guest"]),
            st.text(min_size=0, max_size=20),
        )

    @given(external=st.lists(_role_strategy(), max_size=20))
    @settings(max_examples=200, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_always_returns_a_string(self, external):
        p = _ConcreteProvider()
        result = p.map_roles(external)
        assert isinstance(result, str)
        assert result  # never empty

    @given(external=st.lists(_role_strategy(), max_size=20))
    @settings(max_examples=200, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_result_is_either_recognized_or_user(self, external):
        p = _ConcreteProvider()
        result = p.map_roles(external)
        assert result in _RECOGNIZED_ROLES, (
            f"map_roles returned '{result}' which is not in the "
            f"recognized priority list — should always be one of "
            f"them or 'user' as fallback."
        )

    @given(external=st.lists(st.sampled_from(_RECOGNIZED_ROLES), max_size=20))
    @settings(max_examples=200, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_pure_recognized_input_returns_one_of_inputs(self, external):
        """When all inputs are recognized, the output must be one of
        the inputs (the highest-priority one)."""
        if not external:
            return  # Empty falls through to "user", which is fine.
        p = _ConcreteProvider()
        result = p.map_roles(external)
        assert result in external, (
            f"With recognized-only input {external}, expected result to "
            f"be one of the inputs, got {result}"
        )

    @given(external=st.lists(_role_strategy(), max_size=20))
    @settings(max_examples=200, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_never_returns_unrecognized_role_verbatim(self, external):
        """An unrecognized role must NEVER be returned as the mapped
        role — it must be filtered out."""
        p = _ConcreteProvider()
        result = p.map_roles(external)
        # If the input list was a single unrecognized role, result must
        # be the fallback 'user', not the unrecognized role.
        for role in external:
            if role not in _RECOGNIZED_ROLES:
                # The result must not be that unrecognized role verbatim
                # nor its lowercased/whitespace-stripped form.
                normalized = role.lower().strip()
                assert result != role
                assert result != normalized or normalized in _RECOGNIZED_ROLES

    @given(external=st.lists(_role_strategy(), min_size=2, max_size=20))
    @settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_idempotence_under_repeated_calls(self, external):
        p = _ConcreteProvider()
        first = p.map_roles(external)
        for _ in range(3):
            assert p.map_roles(external) == first


# ---------------------------------------------------------------------------
# 3. Thread safety
# ---------------------------------------------------------------------------


class TestMapRolesThreadSafety:
    """``map_roles`` uses only local variables and is therefore expected
    to be thread-safe.  These tests verify no torn reads / races."""

    def test_concurrent_calls_produce_consistent_results(self):
        p = _ConcreteProvider()
        inputs = [
            ["viewer"],
            ["admin", "viewer"],
            ["bogus", "developer"],
            [],
            ["quant_dev"],
        ] * 20
        results: dict[int, str] = {}
        errors: list[Exception] = []

        def worker(idx: int, roles: list[str]) -> None:
            try:
                results[idx] = p.map_roles(roles)
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(i, r)) for i, r in enumerate(inputs)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Concurrent calls raised: {errors}"
        # Each input/output pair must be deterministic.
        for i, roles in enumerate(inputs):
            assert results[i] == p.map_roles(roles)

    def test_concurrent_high_contention(self):
        """Hammer the method from many threads to maximize the chance
        of exposing shared-state bugs."""
        p = _ConcreteProvider()
        N_THREADS = 16
        N_ITERS = 200
        barrier = threading.Barrier(N_THREADS)
        mismatches: list[str] = []

        def worker():
            barrier.wait()
            for _ in range(N_ITERS):
                r = p.map_roles(["admin", "viewer", "bogus"])
                if r != "admin":
                    mismatches.append(r)

        threads = [threading.Thread(target=worker) for _ in range(N_THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert mismatches == [], (
            f"Expected 'admin' from concurrent calls; got mismatches: {mismatches[:5]}"
        )


# ---------------------------------------------------------------------------
# 4. Boundary conditions on input
# ---------------------------------------------------------------------------


class TestMapRolesBoundaries:
    """Tests for unusual / adversarial inputs."""

    def test_extremely_long_recognized_list(self):
        """A very long list of recognized roles should not blow the
        stack or hang — it just keeps re-electing the same winner."""
        p = _ConcreteProvider()
        big = ["admin"] * 10_000
        assert p.map_roles(big) == "admin"

    def test_extremely_long_unrecognized_list(self):
        p = _ConcreteProvider()
        big = ["bogus"] * 10_000
        assert p.map_roles(big) == "user"

    def test_single_empty_string_role(self):
        p = _ConcreteProvider()
        assert p.map_roles([""]) == "user"

    def test_many_empty_strings(self):
        p = _ConcreteProvider()
        assert p.map_roles([""] * 100) == "user"

    def test_unicode_role_name_is_unrecognized(self):
        """Non-ASCII strings are not recognized roles; must fall back."""
        p = _ConcreteProvider()
        assert p.map_roles(["管理员"]) == "user"  # 'admin' in Chinese
        # Cyrillic 'a' look-alike (homoglyph attack) must not match 'admin'.
        # The character below is U+0430 CYRILLIC SMALL LETTER A, not Latin 'a'.
        cyrillic_a = "\u0430"
        assert p.map_roles([f"{cyrillic_a}dmin"]) == "user"

    def test_role_with_internal_whitespace_is_unrecognized(self):
        """'ad min' is not the same as 'admin' — must not match."""
        p = _ConcreteProvider()
        assert p.map_roles(["ad min"]) == "user"
        assert p.map_roles(["admin "]) == "admin"  # trailing ws stripped
        assert p.map_roles([" admin"]) == "admin"  # leading ws stripped
        assert p.map_roles(["\tadmin\t"]) == "admin"

    def test_role_with_null_byte_is_unrecognized(self):
        """Null-byte injection attempts must not match 'admin'."""
        p = _ConcreteProvider()
        assert p.map_roles(["admin\x00"]) == "user"
        assert p.map_roles(["\x00admin"]) == "user"

    def test_duplicate_recognized_roles_are_idempotent(self):
        p = _ConcreteProvider()
        assert p.map_roles(["admin", "admin", "admin"]) == "admin"
        assert p.map_roles(["viewer", "viewer", "admin"]) == "admin"

    def test_all_seven_recognized_roles_present(self):
        """When every priority-list role appears, admin must win."""
        p = _ConcreteProvider()
        assert (
            p.map_roles([
                "viewer", "user", "retail_trader", "quant_dev",
                "developer", "portfolio_manager", "admin",
            ])
            == "admin"
        )

    def test_priority_ordering_is_strict(self):
        """Each adjacent pair in the priority list must compare correctly."""
        p = _ConcreteProvider()
        order = ["viewer", "user", "retail_trader", "quant_dev",
                 "developer", "portfolio_manager", "admin"]
        for i, lower in enumerate(order[:-1]):
            higher = order[i + 1]
            assert p.map_roles([lower, higher]) == higher, (
                f"Expected '{higher}' to outrank '{lower}'"
            )
            assert p.map_roles([higher, lower]) == higher, (
                f"Order in input should not matter: '{higher}' > '{lower}'"
            )


# ---------------------------------------------------------------------------
# 5. UserInfo / AuthResult integration
# ---------------------------------------------------------------------------


class TestUserInfoAuthResultUsage:
    """``map_roles`` is consumed when constructing ``UserInfo`` and
    persisting ``User.role``. Pin the data shapes that downstream
    code relies on."""

    def test_userinfo_accepts_mapped_role_in_roles_list(self):
        p = _ConcreteProvider()
        mapped = p.map_roles(["admin", "viewer"])
        info = UserInfo(
            external_id="ext-1",
            email="x@y.com",
            display_name="X",
            provider="oidc",
            roles=[mapped],
        )
        assert info.roles == ["admin"]

    def test_authresult_with_userinfo_round_trip(self):
        p = _ConcreteProvider()
        info = UserInfo(roles=[p.map_roles(["developer"])])
        result = AuthResult(success=True, user_info=info)
        assert result.success is True
        assert result.user_info is not None
        assert result.user_info.roles == ["developer"]

    def test_userinfo_default_roles_is_user(self):
        info = UserInfo()
        assert info.roles == ["user"]

    def test_authresult_default_is_failure_with_no_user(self):
        r = AuthResult()
        assert r.success is False
        assert r.user_info is None
        assert r.error is None


# ---------------------------------------------------------------------------
# 6. Static source invariants (guards against re-introduction)
# ---------------------------------------------------------------------------


class TestSourceLevelInvariants:
    """Inspect the source of ``engine.api.auth.base`` to catch
    accidental re-introduction of the silent-escalation table."""

    def test_map_roles_does_not_reference_role_promotions(self):
        from engine.api.auth import base

        src = inspect.getsource(base)
        assert "_ROLE_PROMOTIONS" not in src
        assert "ROLE_PROMOTIONS" not in src

    def test_map_roles_uses_explicit_role_priority(self):
        from engine.api.auth import base

        src = inspect.getsource(base)
        # The new design relies on an explicit priority dict — its
        # presence is what prevents silent re-introduction of the
        # translation table.
        assert "role_priority" in src

    def test_no_assignment_to_external_argument(self):
        """``map_roles`` must not mutate its input list — pin via
        source inspection."""
        from engine.api.auth import base

        src = inspect.getsource(base.IAuthProvider.map_roles)
        # Naive heuristic: any 'external_roles[...]=' would be mutation.
        assert "external_roles[" not in src.replace("external_roles[0:", "__")  # tolerate slicing reads
        # Stronger: look for assignment target on external_roles
        for line in src.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            assert "= external_roles" not in stripped, (
                f"Possible mutation of external_roles: {stripped!r}"
            )

    def test_map_roles_input_list_not_mutated_at_runtime(self):
        p = _ConcreteProvider()
        original = ["ADMIN", "  viewer  ", "bogus"]
        snapshot = list(original)
        p.map_roles(original)
        assert original == snapshot, "map_roles must not mutate its input"


# ---------------------------------------------------------------------------
# 7. Settings: deeper auth_overwrite_role_on_login coverage
# ---------------------------------------------------------------------------


class TestAuthOverwriteRoleSetting:
    """Additional tests for ``auth_overwrite_role_on_login``."""

    def test_global_settings_instance_exposes_flag(self):
        assert hasattr(global_settings, "auth_overwrite_role_on_login")
        assert isinstance(global_settings.auth_overwrite_role_on_login, bool)

    def test_setting_default_is_documented_in_source(self):
        from engine.config import Settings

        src = inspect.getsource(Settings)
        # The default literal must be in the model definition.
        assert "auth_overwrite_role_on_login: bool = False" in src

    def test_setting_truthy_string_values_via_env(self, monkeypatch):
        """Pydantic boolean coercion rules: '1', 'true', 'TRUE', 'yes'."""
        for truthy in ("1", "true", "TRUE", "True", "yes", "on", "ON"):
            monkeypatch.setenv("NEXUS_AUTH_OVERWRITE_ROLE_ON_LOGIN", truthy)
            assert Settings(_env_file=None).auth_overwrite_role_on_login is True, (
                f"Expected True for env value {truthy!r}"
            )

    def test_setting_falsy_string_values_via_env(self, monkeypatch):
        for falsy in ("0", "false", "FALSE", "False", "no", "off"):
            monkeypatch.setenv("NEXUS_AUTH_OVERWRITE_ROLE_ON_LOGIN", falsy)
            assert Settings(_env_file=None).auth_overwrite_role_on_login is False, (
                f"Expected False for env value {falsy!r}"
            )

    def test_setting_invalid_value_raises(self, monkeypatch):
        """Junk strings should be rejected by pydantic-v2 validation."""
        from pydantic import ValidationError

        monkeypatch.setenv("NEXUS_AUTH_OVERWRITE_ROLE_ON_LOGIN", "maybe")
        with pytest.raises(ValidationError):
            Settings(_env_file=None)

    def test_setting_field_has_security_doc_comment(self):
        """The SEV-741 defense-in-depth change ships with a docstring
        explaining why the default is False."""
        from engine.config import Settings

        src = inspect.getsource(Settings)
        # The comment must mention SEV-741 so future readers can find
        # the original incident context.
        assert "SEV-741" in src


# ---------------------------------------------------------------------------
# 8. LDAP / OIDC provider integration with auth_overwrite_role_on_login
# ---------------------------------------------------------------------------


class TestProviderRoleOverwriteSemantics:
    """The new ``auth_overwrite_role_on_login`` flag is meant to
    control whether a federated login changes an existing user's
    role.  These tests pin the **current** behavior of OIDC vs LDAP
    so that any future fix to make LDAP honor the flag is testable.

    * OIDC: only sets role at user-creation time (honors the flag
      implicitly — never overwrites).
    * LDAP: currently overwrites unconditionally when role differs
      (does NOT honor the flag).  This test pins that behavior so a
      fix can flip the assertion.
    """

    def test_oidc_provider_name(self):
        from engine.api.auth.oidc import OIDCAuthProvider

        assert OIDCAuthProvider().name == "oidc"

    def test_ldap_provider_name(self):
        from engine.api.auth.ldap import LDAPAuthProvider

        assert LDAPAuthProvider().name == "ldap"

    def test_oidc_map_roles_inherited(self):
        from engine.api.auth.oidc import OIDCAuthProvider

        assert OIDCAuthProvider().map_roles(["admin"]) == "admin"
        assert OIDCAuthProvider().map_roles(["quant_dev"]) == "quant_dev"

    def test_ldap_map_roles_inherited(self):
        from engine.api.auth.ldap import LDAPAuthProvider

        assert LDAPAuthProvider().map_roles(["admin"]) == "admin"
        assert LDAPAuthProvider().map_roles(["viewer"]) == "viewer"

    def test_oidc_authenticate_does_not_overwrite_role(self):
        """Source-level pin: OIDC ``authenticate`` only assigns a role
        when creating a new User. On subsequent logins the role is
        untouched (the new ``auth_overwrite_role_on_login`` setting is
        implicitly honored).  We verify by reading the OIDC module
        source: the only ``role=`` assignment appears inside the
        ``User(...)`` constructor call under the ``user is None``
        branch — there is no ``user.role = ...`` assignment anywhere
        in the file."""
        import inspect

        from engine.api.auth import oidc

        src = inspect.getsource(oidc)
        # The dangerous pattern would be ``user.role = mapped_role``
        # or ``existing_user.role = ...`` — assert absence.
        assert "user.role = " not in src, (
            "OIDC authenticate must not assign user.role after creation; "
            "doing so would bypass auth_overwrite_role_on_login (SEV-741)."
        )
        assert "existing_user.role" not in src
        # And the only role assignment must be the constructor at user
        # creation time.
        assert "role=mapped_role" in src

    def test_oidc_role_assignment_is_inside_user_creation_branch(self):
        """Confirm via source inspection that ``role=mapped_role`` is
        only set when instantiating a new ``User(...)`` (which is
        guarded by ``if user is None``)."""
        import inspect

        from engine.api.auth import oidc

        src = inspect.getsource(oidc)
        # Locate the role=mapped_role line.
        assert "role=mapped_role" in src
        # The User(...) construction site must be in the same module.
        assert "user = User(" in src
        # And it must be guarded by a "user is None" check.
        assert "user is None" in src

    def test_ldap_does_overwrite_role_unconditionally(self):
        """Pins the CURRENT (post-SEV-741) LDAP behavior: the LDAP
        branch unconditionally sets ``user.role = mapped_role`` when
        the user exists and the role differs — meaning LDAP does NOT
        honor ``auth_overwrite_role_on_login``.

        This is documented as a known gap: the OIDC branch was
        updated but the LDAP branch was not.  When LDAP is fixed to
        respect the flag, this test should be updated to assert the
        new correct behavior.

        Why pin it?  So that a future LDAP change that removes this
        assignment is *visible in code review* via a failing test,
        rather than silently changing semantics.
        """
        import inspect

        from engine.api.auth import ldap

        src = inspect.getsource(ldap)
        # Document current state: LDAP DOES overwrite. If a future
        # patch removes this, the test will fail and the reviewer
        # will know to update the assertion to reflect the fixed
        # behavior.
        assert "user.role = mapped_role" in src, (
            "LDAP authenticate currently overwrites user.role "
            "unconditionally (does not honor auth_overwrite_role_on_login). "
            "If this assertion fails, the LDAP provider has been updated "
            "to honor the flag — replace this test with one that asserts "
            "the new correct behavior."
        )
        assert "auth_overwrite_role_on_login" not in src, (
            "LDAP provider does not currently consult "
            "auth_overwrite_role_on_login — see SEV-741 follow-up."
        )
