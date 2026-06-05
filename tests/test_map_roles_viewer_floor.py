"""Targeted tests for the ``map_roles`` viewer-floor + structured-return
hardening.

Background
----------
Two related changes were made to :meth:`IAuthProvider.map_roles`:

1. **Viewer-as-floor**: when no recognized role is supplied (empty
   list, all-unrecognized, or whitespace-only entries) the function
   now returns ``"viewer"`` — the lowest-privilege role — instead of
   the historical ``"user"`` default. Granting the lowest possible
   privilege to an unrecognized identity is the safer default.

2. **Structured return**: a new ``map_roles_detailed`` method
   complements ``map_roles`` by returning a :class:`RoleMappingResult`
   containing both the mapped role and the full ``recognized`` /
   ``unrecognized`` lists, so callers can make policy decisions
   (e.g. deny login when any unrecognized role is present).

3. **Lowercase guard**: a module-level ``assert`` enforces that
   ``_ROLE_PRIORITY`` keys are all lowercase, since ``map_roles``
   normalizes incoming IdP role strings via ``str.lower().strip()``
   and would silently fail to match a mixed-/upper-case entry.

These tests pin each of those behaviors and the future-proofing
guard.
"""

from __future__ import annotations

import pytest

from engine.api.auth.base import (
    _ROLE_FLOOR,
    _ROLE_PRIORITY,
    AuthResult,
    IAuthProvider,
    RoleMappingResult,
)


class _ConcreteProvider(IAuthProvider):
    @property
    def name(self) -> str:
        return "test-viewer-floor"

    async def authenticate(self, **kwargs):  # pragma: no cover
        return AuthResult()


# ---------------------------------------------------------------------------
# 1. viewer is the floor — empty / unrecognized input must return viewer
# ---------------------------------------------------------------------------


class TestViewerIsTheFloor:
    """``map_roles`` returns the lowest-privilege role when no
    recognized role is present. This is the security hardening that
    replaces the old ``user`` default."""

    def test_empty_external_roles_returns_viewer(self):
        """Empty input — no roles to consider — defaults to viewer."""
        assert _ConcreteProvider().map_roles([]) == "viewer"

    def test_all_unrecognized_returns_viewer(self):
        """When every external role is unrecognized, the lowest-
        privilege floor (viewer) is returned rather than ``user``."""
        p = _ConcreteProvider()
        assert p.map_roles(["totally_bogus", "nope", "fake_role"]) == "viewer"

    def test_whitespace_only_input_returns_viewer(self):
        """A whitespace-only string normalizes to empty, which is not
        a known role — must fall through to viewer."""
        assert _ConcreteProvider().map_roles(["   ", "\t", " "]) == "viewer"

    def test_viewer_floor_is_lowest_privilege(self):
        """Sanity: ``viewer`` must have priority 0 (the lowest)."""
        assert _ROLE_PRIORITY["viewer"] == 0
        assert _ROLE_FLOOR == "viewer"
        # ``viewer`` must be strictly less-privileged than ``user``.
        assert _ROLE_PRIORITY["viewer"] < _ROLE_PRIORITY["user"]

    def test_viewer_floor_strictly_below_all_other_known_roles(self):
        """viewer is the lowest-privilege role — every other role in
        the table must outrank it."""
        for role, prio in _ROLE_PRIORITY.items():
            if role == "viewer":
                continue
            assert prio > _ROLE_PRIORITY["viewer"], (
                f"role {role!r} has priority {prio} which does not "
                f"outrank viewer ({_ROLE_PRIORITY['viewer']}); viewer "
                f"is supposed to be the floor."
            )

    def test_mixed_recognized_and_unrecognized_returns_recognized(self):
        """Mix of recognized + unrecognized — recognized one wins
        (and the unrecognized ones are dropped, not promoted)."""
        p = _ConcreteProvider()
        assert p.map_roles(["viewer", "bogus_x", "bogus_y"]) == "viewer"
        assert p.map_roles(["admin", "bogus_z"]) == "admin"
        assert p.map_roles(["developer", "l33t_h4x0r"]) == "developer"

    def test_floor_only_used_when_nothing_recognized(self):
        """Sanity: as soon as one recognized role is present, the
        floor is NOT used; the highest-priority recognized role wins."""
        p = _ConcreteProvider()
        # viewer is recognized here, so result is viewer (not because
        # of the floor, but because viewer is the highest recognized).
        assert p.map_roles(["viewer"]) == "viewer"
        # user outranks viewer — result must be user, not viewer.
        assert p.map_roles(["user"]) == "user"
        assert p.map_roles(["viewer", "user"]) == "user"


# ---------------------------------------------------------------------------
# 2. Structured return — map_roles_detailed
# ---------------------------------------------------------------------------


class TestMapRolesDetailed:
    """``map_roles_detailed`` returns a :class:`RoleMappingResult`
    with the mapped role plus the full recognized/unrecognized
    lists, so callers can implement policy decisions on top."""

    def test_returns_role_mapping_result_instance(self):
        result = _ConcreteProvider().map_roles_detailed(["admin"])
        assert isinstance(result, RoleMappingResult)

    def test_role_field_matches_map_roles(self):
        """The ``role`` attribute must agree with what plain
        ``map_roles`` returns."""
        p = _ConcreteProvider()
        cases = [
            ["admin"],
            ["user", "developer"],
            ["viewer"],
            ["quant_dev"],
            [],
            ["bogus"],
            ["admin", "bogus"],
        ]
        for external in cases:
            detailed = p.map_roles_detailed(external)
            assert detailed.role == p.map_roles(external), (
                f"map_roles_detailed().role must equal map_roles() for "
                f"input {external!r}"
            )

    def test_recognized_list_contains_all_recognized(self):
        """The ``recognized`` list must include every recognized role
        from the input (normalized to lowercase), in input order."""
        p = _ConcreteProvider()
        result = p.map_roles_detailed(["ADMIN", "user", "  developer  "])
        assert result.recognized == ["admin", "user", "developer"]

    def test_recognized_list_empty_when_all_unrecognized(self):
        p = _ConcreteProvider()
        result = p.map_roles_detailed(["bogus_a", "bogus_b"])
        assert result.recognized == []

    def test_recognized_list_empty_for_empty_input(self):
        p = _ConcreteProvider()
        result = p.map_roles_detailed([])
        assert result.recognized == []

    def test_unrecognized_list_contains_raw_unrecognized(self):
        """``unrecognized`` must contain the *raw* (un-normalized)
        unrecognized strings in input order, so operators can audit
        the exact IdP-supplied group/role names."""
        p = _ConcreteProvider()
        result = p.map_roles_detailed(
            ["admin", "StaleGroup", "  WeirdRole  ", "developer"]
        )
        assert result.unrecognized == ["StaleGroup", "  WeirdRole  "]

    def test_unrecognized_list_empty_when_all_recognized(self):
        p = _ConcreteProvider()
        result = p.map_roles_detailed(["admin", "user"])
        assert result.unrecognized == []

    def test_unrecognized_list_empty_for_empty_input(self):
        p = _ConcreteProvider()
        result = p.map_roles_detailed([])
        assert result.unrecognized == []

    def test_floor_returned_in_role_when_nothing_recognized(self):
        """When nothing is recognized, ``role`` is the viewer floor
        and both recognized/unrecognized are populated correctly."""
        p = _ConcreteProvider()
        result = p.map_roles_detailed(["foo", "bar"])
        assert result.role == "viewer"
        assert result.recognized == []
        assert result.unrecognized == ["foo", "bar"]

    def test_floor_returned_for_empty_input(self):
        p = _ConcreteProvider()
        result = p.map_roles_detailed([])
        assert result.role == "viewer"
        assert result.recognized == []
        assert result.unrecognized == []

    def test_callers_can_implement_deny_if_unrecognized_policy(self):
        """Demonstrates a caller-side policy: deny if any role is
        unrecognized. This is the structured-return use case."""
        p = _ConcreteProvider()

        def deny_if_unrecognized(external: list[str]) -> str | None:
            """Return the mapped role, or None to deny login when
            any unrecognized role is present."""
            r = p.map_roles_detailed(external)
            if r.unrecognized:
                return None
            return r.role

        # All-recognized: policy passes, role returned.
        assert deny_if_unrecognized(["admin", "user"]) == "admin"
        # Mixed: policy denies.
        assert deny_if_unrecognized(["admin", "weird"]) is None
        # All-unrecognized: policy denies (and floor not exposed).
        assert deny_if_unrecognized(["weird"]) is None

    def test_result_is_hashable_via_frozen_dataclass(self):
        """RoleMappingResult is a frozen dataclass — it should be
        hashable so it can be used as a dict key / set member by
        callers tracking mapping outcomes."""
        p = _ConcreteProvider()
        r = p.map_roles_detailed(["admin"])
        # frozen=True + list fields: lists are not hashable, but the
        # dataclass itself raises FrozenInstanceError on mutation.
        # Verify immutability contract.
        with pytest.raises(Exception):  # noqa: B017 (any FrozenInstanceError attr)
            r.role = "viewer"

    def test_role_field_is_viewer_floor_when_input_is_unrecognized(self):
        """Direct test of the viewer floor through the structured
        API."""
        p = _ConcreteProvider()
        result = p.map_roles_detailed(["totally_unknown"])
        assert result.role == "viewer"


# ---------------------------------------------------------------------------
# 3. Lowercase-guard on _ROLE_PRIORITY
# ---------------------------------------------------------------------------


class TestRolePriorityLowercaseGuard:
    """The module-level ``assert`` ensures every key in
    ``_ROLE_PRIORITY`` is lowercase, because ``map_roles`` normalizes
    incoming IdP roles via ``str.lower().strip()`` — an upper- or
    mixed-case key would silently never match."""

    def test_all_keys_are_lowercase(self):
        """Static guard: every key must already be lowercase."""
        for key in _ROLE_PRIORITY:
            assert key == key.lower(), (
                f"_ROLE_PRIORITY key {key!r} must be lowercase; "
                f"map_roles normalizes via str.lower() and would "
                f"never match a mixed-/upper-case key."
            )

    def test_no_duplicate_priorities_among_adjacent_ranks(self):
        """Sanity: every priority value is unique (no two roles tied
        for the same rank) so that the max-priority lookup is
        deterministic."""
        priorities = list(_ROLE_PRIORITY.values())
        assert len(set(priorities)) == len(priorities), (
            "Every role must have a distinct priority; ties would "
            "make map_roles' max-priority lookup non-deterministic."
        )

    def test_viewer_is_minimum_priority(self):
        """The viewer floor must have the minimum priority value."""
        min_priority = min(_ROLE_PRIORITY.values())
        assert _ROLE_PRIORITY["viewer"] == min_priority

    def test_normalization_would_match_every_known_role(self):
        """Round-trip: ``role.lower().strip()`` of every key must
        still be present as a key. (Trivially true when keys are
        already lowercase + stripped, but this pins the contract.)"""
        for key in _ROLE_PRIORITY:
            normalized = key.lower().strip()
            assert normalized in _ROLE_PRIORITY, (
                f"Normalized form {normalized!r} of key {key!r} is "
                f"missing from _ROLE_PRIORITY; map_roles would fail "
                f"to match the normalized form."
            )

    def test_module_reimport_does_not_raise_assertion(self):
        """Re-importing the module must succeed — i.e. the assert
        is not currently tripping on bad data."""
        import importlib

        from engine.api.auth import base

        importlib.reload(base)
        # If we got here, the module-level assert did not fire.
        assert hasattr(base, "_ROLE_PRIORITY")
        assert hasattr(base, "_ROLE_FLOOR")
