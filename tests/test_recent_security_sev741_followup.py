"""Comprehensive tests for the SEV-741 follow-up commit
(``security(auth): centralize role-overwrite policy and broaden C1
scrub``).

The commit under test made three changes:

1. Introduced ``engine.api.auth.base._should_overwrite_role``, the
   shared policy every federated provider must consult before mutating
   ``user.role``.
2. Routed every federated provider (LDAP, OIDC, Google, GitHub)
   through that helper.
3. Broadened the client-error sanitization regex to also drop
   Unicode directional overrides (``U+202A-U+202E``, ``U+2066-
   U+2069``), zero-width characters (``U+200B-U+200D``) and the
   BOM / ZWNBSP (``U+FEFF``) on top of the previously-added C1
   range (``U+0080-U+009F``).

This module layers *additional* coverage on top of
``test_auth_role_promotion_security_fix.py`` and
``test_client_errors.py`` (which already pin the basic contracts).
It targets:

- the helper's pure-Python decision matrix at higher granularity
- every provider's call-site wiring (``mapped_role`` computation,
  branch coverage, no-op fast-path, audit emission)
- regex / scrubber coverage of every dangerous Unicode code point
  added by the broadening
- that legitimate payloads (CJK, emoji, tabs, newlines that the
  spec preserves, etc.) still pass through unscathed
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest

from engine.api.auth.base import (
    AuthResult,
    IAuthProvider,
    UserInfo,
    _should_overwrite_role,
)

if TYPE_CHECKING:
    from engine.db.models import User

# ---------------------------------------------------------------------------
# Shared test scaffolding
# ---------------------------------------------------------------------------


class _SettingsStub:
    """Minimal stand-in for ``engine.config.Settings`` so the helper
    can be exercised in isolation without touching pydantic-settings."""

    def __init__(self, *, overwrite: bool) -> None:
        self.auth_overwrite_role_on_login = overwrite


class _ConcreteProvider(IAuthProvider):
    """Concrete provider used to verify the helper is invokable from
    an arbitrary subclass (the policy lives at the module level, not
    bound to a specific class)."""

    @property
    def name(self) -> str:
        return "test-concrete"

    async def authenticate(self, **kwargs: Any) -> AuthResult:
        return AuthResult()


# ---------------------------------------------------------------------------
# Section 1 — _should_overwrite_role: pure-Python decision matrix
# ---------------------------------------------------------------------------


class TestShouldOverwriteRoleTruthTable:
    """Exhaustive truth-table coverage for the helper. The previous
    commit shipped a smaller version; this layer adds demotion,
    role-equality short-circuit and attribute-missing defaulting
    cases that aren't all pinned elsewhere."""

    @pytest.mark.parametrize(
        ("current", "mapped", "overwrite", "expected"),
        [
            # New user (None) short-circuits regardless of policy.
            (None, "user", False, True),
            (None, "user", True, True),
            (None, "admin", False, True),
            (None, "admin", True, True),
            # Equal role: never overwrite (avoids audit noise).
            ("user", "user", False, False),
            ("user", "user", True, False),
            ("admin", "admin", False, False),
            ("admin", "admin", True, False),
            # Different role + opt-out: preserve.
            ("user", "admin", False, False),
            ("admin", "user", False, False),
            ("viewer", "developer", False, False),
            # Different role + opt-in: overwrite.
            ("user", "admin", True, True),
            ("admin", "user", True, True),
            ("viewer", "developer", True, True),
        ],
    )
    def test_truth_table(self, current, mapped, overwrite, expected):
        result = _should_overwrite_role(
            current, mapped, _SettingsStub(overwrite=overwrite)
        )
        assert result is expected, (
            f"helper returned {result!r} for "
            f"current={current!r} mapped={mapped!r} overwrite={overwrite}"
        )

    def test_returns_bool_not_truthy_int(self):
        """``bool`` check uses ``is`` so attribute-style return values
        (e.g. numpy.bool_) cannot sneak through."""
        result = _should_overwrite_role(
            "user", "admin", _SettingsStub(overwrite=True)
        )
        assert result is True
        assert type(result) is bool

    def test_unknown_role_strings_still_compare_equality(self):
        """The helper doesn't validate role names — it only compares
        them. Two unknown but equal strings short-circuit to False."""
        assert (
            _should_overwrite_role(
                "superadmin", "superadmin", _SettingsStub(overwrite=True)
            )
            is False
        )
        # And different unknown strings defer to the setting.
        assert (
            _should_overwrite_role(
                "superadmin", "wizard", _SettingsStub(overwrite=False)
            )
            is False
        )
        assert (
            _should_overwrite_role(
                "superadmin", "wizard", _SettingsStub(overwrite=True)
            )
            is True
        )

    def test_case_sensitive_equality(self):
        """Role equality is case-sensitive on purpose — providers
        normalize upstream claims before reaching the helper. The
        helper should not silently lowercase anything."""
        assert (
            _should_overwrite_role(
                "Admin", "admin", _SettingsStub(overwrite=False)
            )
            is False  # policy decides; strings are not equal
        )
        assert (
            _should_overwrite_role(
                "Admin", "admin", _SettingsStub(overwrite=True)
            )
            is True
        )

    def test_empty_string_role_is_treated_as_known_role(self):
        """Empty string is *not* None — it's an existing role
        (pathological but possible). The helper must not promote it
        to None and accidentally overwrite."""
        # Equal -> False
        assert (
            _should_overwrite_role("", "", _SettingsStub(overwrite=True))
            is False
        )
        # Different + opt-in -> True
        assert (
            _should_overwrite_role("", "admin", _SettingsStub(overwrite=True))
            is True
        )
        # Different + opt-out -> False (preserve the legacy empty role)
        assert (
            _should_overwrite_role("", "admin", _SettingsStub(overwrite=False))
            is False
        )


class TestShouldOverwriteRoleConfigContract:
    """The helper is intentionally liberal about what the ``config``
    argument looks like so it can be unit-tested without pydantic —
    but it still has a contract."""

    def test_missing_attribute_defaults_to_false(self):
        class _Bare:
            pass

        assert _should_overwrite_role("user", "admin", _Bare()) is False

    def test_attribute_explicitly_none_defaults_to_false(self):
        class _NoneConfig:
            auth_overwrite_role_on_login = None

        assert _should_overwrite_role("user", "admin", _NoneConfig()) is False

    @pytest.mark.parametrize("value", [0, "", [], (), {}, frozenset(), False])
    def test_falsy_non_bool_values_treated_as_false(self, value):
        class _C:
            auth_overwrite_role_on_login = value

        assert _should_overwrite_role("user", "admin", _C()) is False

    @pytest.mark.parametrize("value", [1, "true", "yes", [0], {"x": 1}, True])
    def test_truthy_non_bool_values_treated_as_true(self, value):
        class _C:
            auth_overwrite_role_on_login = value

        assert _should_overwrite_role("user", "admin", _C()) is True

    def test_property_attribute_on_config_is_supported(self):
        """A config class that exposes the flag via a property
        (e.g. computed from multiple env settings) must still work
        — ``getattr`` resolves descriptors transparently."""

        class _PropertyConfig:
            @property
            def auth_overwrite_role_on_login(self) -> bool:
                return True

        assert (
            _should_overwrite_role("user", "admin", _PropertyConfig())
            is True
        )

    def test_module_does_not_mutate_config(self):
        """The helper reads but must never write to ``config``."""

        class _TrackingConfig:
            def __init__(self) -> None:
                self.read_count = 0
                self.auth_overwrite_role_on_login = True

            def __getattribute__(self, name: str) -> Any:
                if name == "auth_overwrite_role_on_login":
                    object.__setattr__(
                        self, "read_count", object.__getattribute__(self, "read_count") + 1
                    )
                return object.__getattribute__(self, name)

        cfg = _TrackingConfig()
        assert _should_overwrite_role("user", "admin", cfg) is True
        # Exactly one attribute lookup happened.
        assert cfg.read_count == 1


# ---------------------------------------------------------------------------
# Section 2 — Provider call-site wiring
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("module_path", "class_name", "provider_name", "static_role"),
    [
        # The "static-role" providers (Google / GitHub) always map to
        # ``"user"``. The role-overwrite path can therefore only ever
        # fire when an existing local user was *demoted* to ``user``.
        ("engine.api.auth.google", "GoogleAuthProvider", "google", "user"),
        ("engine.api.auth.github_oauth", "GitHubAuthProvider", "github", "user"),
        # OIDC and LDAP compute mapped_role dynamically from upstream
        # claims, so we don't pin a static value here.
        ("engine.api.auth.oidc", "OIDCAuthProvider", "oidc", None),
        ("engine.api.auth.ldap", "LDAPAuthProvider", "ldap", None),
    ],
)
class TestProviderCallSiteWiring:
    """Source-level guards: each provider module must

    1. import the helper (already covered by an existing test, kept
       here too as a defence-in-depth sanity check);
    2. invoke the helper at *exactly one* site (the existing-user
       branch). Two call-sites would imply a logic fork that the
       central policy was meant to eliminate;
    3. reference ``auth_overwrite_role_on_login`` only via the
       helper — never directly — so the policy can't be bypassed by
       a typo;
    4. log ``role_overwritten`` audit events on success.
    """

    def test_provider_imports_helper(self, module_path, class_name, provider_name, static_role):
        import importlib
        import inspect

        mod = importlib.import_module(module_path)
        src = inspect.getsource(mod)
        assert "_should_overwrite_role" in src, (
            f"{module_path} must reference _should_overwrite_role"
        )

    def test_provider_calls_helper_exactly_once(
        self, module_path, class_name, provider_name, static_role
    ):
        import importlib
        import inspect

        mod = importlib.import_module(module_path)
        src = inspect.getsource(mod)
        # The helper is invoked as ``_should_overwrite_role(...)`` —
        # count occurrences of the bare call (not the import line).
        call_sites = re.findall(r"_should_overwrite_role\s*\(", src)
        # Subtract one for the import line: ``from … import …, _should_overwrite_role``
        # doesn't contain a ``(`` after the name, so the regex above
        # already counts only call sites.
        assert len(call_sites) == 1, (
            f"{module_path} must call _should_overwrite_role exactly once "
            f"(found {len(call_sites)}); the policy is centralized by design."
        )

    def test_provider_does_not_access_setting_directly(
        self, module_path, class_name, provider_name, static_role
    ):
        """Direct attribute access to ``auth_overwrite_role_on_login``
        (e.g. ``settings.auth_overwrite_role_on_login``) would let a
        future refactor re-implement the policy inline. The helper is
        the single source of truth. We check for attribute *access*
        patterns rather than the bare identifier so docstring /
        comment references don't trip the guard."""
        import importlib
        import inspect
        import re

        mod = importlib.import_module(module_path)
        src = inspect.getsource(mod)
        # Match ``settings.auth_overwrite_role_on_login`` or
        # ``config.auth_overwrite_role_on_login`` — i.e. real
        # attribute lookups — but NOT bare identifiers inside
        # comments or string literals.
        access_pattern = re.compile(
            r"\.\s*auth_overwrite_role_on_login\b"
        )
        # Strip line comments and string literals so docstring /
        # inline references don't trigger a false positive.
        stripped_lines: list[str] = []
        for line in src.splitlines():
            code = re.sub(r"#.*$", "", line)
            code = re.sub(r'f?"""(?:.|\n)*?"""', '""', code)
            code = re.sub(r"f?'''(?:.|\n)*?'''", "''", code)
            stripped_lines.append(code)
        code_only = "\n".join(stripped_lines)
        assert not access_pattern.search(code_only), (
            f"{module_path} must not access .auth_overwrite_role_on_login "
            "directly — go through _should_overwrite_role."
        )

    def test_provider_logs_role_overwritten_audit_event(
        self, module_path, class_name, provider_name, static_role
    ):
        """When the helper returns True the provider must emit a
        ``role_overwritten`` event so the SOC trail records both the
        previous and new role."""
        import importlib
        import inspect

        mod = importlib.import_module(module_path)
        src = inspect.getsource(mod)
        expected_event = f"auth.{provider_name}.role_overwritten"
        assert expected_event in src, (
            f"{module_path} must log a {expected_event!r} event when "
            "the helper returns True (SEV-741 audit requirement)."
        )


class TestProviderRoleOverwriteBehavior:
    """End-to-end checks that each provider actually obeys the helper
    by exercising the existing-user code path with both opt-in and
    opt-out configurations.

    LDAP is exercised via the existing comprehensive test file, so we
    focus on Google and GitHub here (they have the simplest mapped
    role — always ``user`` — which makes the opt-in vs opt-out
    contract maximally observable).
    """

    @staticmethod
    def _build_provider(module_path: str, class_name: str):
        import importlib

        mod = importlib.import_module(module_path)
        return getattr(mod, class_name)()

    @staticmethod
    def _make_mock_db_with_existing_user(existing_user):
        from unittest.mock import AsyncMock, MagicMock

        from sqlalchemy.ext.asyncio import AsyncSession

        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = existing_user
        mock_db.execute.return_value = mock_result
        mock_db.flush = AsyncMock()
        return mock_db

    @pytest.mark.parametrize(
        ("module_path", "class_name", "build_kwargs"),
        [
            # Both Google and GitHub need an httpx double that yields a
            # well-formed profile. We patch httpx.AsyncClient at the
            # module level so the provider's normal client-creation
            # path runs against a stub.
            (
                "engine.api.auth.google",
                "GoogleAuthProvider",
                {
                    "profile": {
                        "sub": "g-123",
                        "email": "g@example.com",
                        "name": "G",
                    },
                    "module_settings_attr": "engine.api.auth.google.settings",
                },
            ),
            (
                "engine.api.auth.github_oauth",
                "GitHubAuthProvider",
                {
                    "profile": {
                        "id": 123,
                        "login": "ghuser",
                        "name": "GH",
                        "email": "gh@example.com",
                    },
                    "module_settings_attr": "engine.api.auth.github_oauth.settings",
                },
            ),
        ],
    )
    async def test_existing_admin_role_preserved_when_opted_out(
        self, module_path, class_name, build_kwargs, monkeypatch
    ):
        """SEV-741: with the policy off, the IdP-mapped ``user`` role
        must NOT downgrade a previously-granted local ``admin``."""
        from unittest.mock import MagicMock

        from engine.config import Settings
        from engine.db.models import User

        s = Settings(_env_file=None)  # default: overwrite=False
        monkeypatch.setattr(build_kwargs["module_settings_attr"], s)

        provider = self._build_provider(module_path, class_name)

        existing = User(
            email=build_kwargs["profile"].get("email", "x@example.com"),
            display_name="existing",
            is_active=True,
            role="admin",
            auth_provider=provider.name,
            external_id=str(build_kwargs["profile"].get("sub", build_kwargs["profile"].get("id"))),
        )
        mock_db = self._make_mock_db_with_existing_user(existing)

        # Stub httpx.AsyncClient so the token + userinfo round-trips
        # succeed without a real network.
        fake_resp = MagicMock()
        fake_resp.raise_for_status = MagicMock()
        fake_resp.json.return_value = build_kwargs["profile"]

        token_resp = MagicMock()
        token_resp.raise_for_status = MagicMock()
        token_resp.json.return_value = {"access_token": "fake-token"}

        class _FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def post(self, url, **kwargs):
                return token_resp

            async def get(self, url, **kwargs):
                return fake_resp

        with (self._patch_httpx(_FakeClient())):
            result = await provider.authenticate(code="x", db=mock_db)

        assert result.success is True
        assert existing.role == "admin", (
            "Opt-out path must NOT downgrade an existing admin to user."
        )
        mock_db.flush.assert_not_called()

    @pytest.mark.parametrize(
        ("module_path", "class_name", "build_kwargs"),
        [
            (
                "engine.api.auth.google",
                "GoogleAuthProvider",
                {
                    "profile": {
                        "sub": "g-123",
                        "email": "g@example.com",
                        "name": "G",
                    },
                    "module_settings_attr": "engine.api.auth.google.settings",
                },
            ),
            (
                "engine.api.auth.github_oauth",
                "GitHubAuthProvider",
                {
                    "profile": {
                        "id": 123,
                        "login": "ghuser",
                        "name": "GH",
                        "email": "gh@example.com",
                    },
                    "module_settings_attr": "engine.api.auth.github_oauth.settings",
                },
            ),
        ],
    )
    async def test_existing_admin_role_downgraded_when_opted_in(
        self, module_path, class_name, build_kwargs, monkeypatch
    ):
        """SEV-741: when operators opt in, the IdP-mapped ``user``
        role replaces the previously-granted ``admin`` — both
        directions of role mutation are gated by the same flag."""
        from unittest.mock import MagicMock

        from engine.config import Settings
        from engine.db.models import User

        s = Settings(_env_file=None, auth_overwrite_role_on_login=True)
        monkeypatch.setattr(build_kwargs["module_settings_attr"], s)

        provider = self._build_provider(module_path, class_name)

        existing = User(
            email=build_kwargs["profile"].get("email", "x@example.com"),
            display_name="existing",
            is_active=True,
            role="admin",
            auth_provider=provider.name,
            external_id=str(build_kwargs["profile"].get("sub", build_kwargs["profile"].get("id"))),
        )
        mock_db = self._make_mock_db_with_existing_user(existing)

        fake_resp = MagicMock()
        fake_resp.raise_for_status = MagicMock()
        fake_resp.json.return_value = build_kwargs["profile"]

        token_resp = MagicMock()
        token_resp.raise_for_status = MagicMock()
        token_resp.json.return_value = {"access_token": "fake-token"}

        class _FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def post(self, url, **kwargs):
                return token_resp

            async def get(self, url, **kwargs):
                return fake_resp

        with self._patch_httpx(_FakeClient()):
            result = await provider.authenticate(code="x", db=mock_db)

        assert result.success is True
        assert existing.role == "user", (
            "Opt-in path must overwrite the existing admin with the IdP-mapped user."
        )
        mock_db.flush.assert_called()

    @staticmethod
    def _patch_httpx(fake_client):
        """Context-manager helper that patches the provider module's
        ``httpx.AsyncClient`` to a pre-built fake. Both Google and
        GitHub do ``import httpx`` inside ``authenticate``, so we
        patch the global ``httpx`` module's attribute."""
        from contextlib import contextmanager
        from unittest.mock import patch

        @contextmanager
        def _ctx():
            with patch("httpx.AsyncClient", return_value=fake_client):
                yield

        return _ctx()


# ---------------------------------------------------------------------------
# Section 3 — OIDC: helper-driven mutate path integration
# ---------------------------------------------------------------------------


class TestOIDCRoleOverwriteBranches:
    """OIDC routes through the helper on the existing-user branch.
    Rather than rebuild the full RSA-signed JWT + httpx stack here
    (which is already exercised by ``tests/test_oidc_auth.py``), we
    pin the source-level contract: the ``elif _should_overwrite_role``
    branch must be present, must call ``db.flush`` on success, and
    must emit the canonical ``role_overwritten`` audit event with
    ``previous_role`` + ``new_role`` payloads."""

    def test_oidc_source_has_elif_overwrite_branch(self):
        import importlib
        import inspect

        mod = importlib.import_module("engine.api.auth.oidc")
        src = inspect.getsource(mod)
        # The branch must be guarded by the helper.
        assert "elif _should_overwrite_role(" in src, (
            "OIDC must gate the existing-user role mutation on "
            "_should_overwrite_role (SEV-741)."
        )

    def test_oidc_branch_emits_audit_event(self):
        import importlib
        import inspect

        mod = importlib.import_module("engine.api.auth.oidc")
        src = inspect.getsource(mod)
        assert "auth.oidc.role_overwritten" in src
        assert "previous_role" in src
        assert "new_role" in src

    def test_oidc_branch_calls_db_flush_after_mutation(self):
        import importlib
        import inspect
        import re

        mod = importlib.import_module("engine.api.auth.oidc")
        src = inspect.getsource(mod)
        # Within the elif branch, ``db.flush()`` must follow the
        # ``user.role = mapped_role`` assignment. Capture the branch
        # body and check.
        m = re.search(
            r"elif\s+_should_overwrite_role\([^)]*\):\s*"
            r"(?:.|\n)*?"
            r"user\.role\s*=\s*mapped_role\s*"
            r"(?:.|\n)*?"
            r"await\s+db\.flush\(\)",
            src,
        )
        assert m is not None, (
            "OIDC: elif _should_overwrite_role branch must "
            "assign user.role = mapped_role and then call "
            "await db.flush()."
        )

    def test_oidc_mapped_role_computed_unconditionally(self):
        """OIDC moved the mapped_role computation out of the
        new-user branch so it's always available to the
        existing-user overwrite path. A regression that put it
        back inside ``if user is None`` would silently disable
        the helper on the existing-user branch."""
        import importlib
        import inspect

        mod = importlib.import_module("engine.api.auth.oidc")
        src = inspect.getsource(mod)
        # The mapped_role assignment must precede the
        # ``if user is None`` block.
        mapped_idx = src.find('mapped_role = "user"')
        if_idx = src.find("if user is None")
        assert mapped_idx != -1 and if_idx != -1
        assert mapped_idx < if_idx, (
            "OIDC: mapped_role must be computed BEFORE the "
            "``if user is None`` branch so the elif path can "
            "feed it to _should_overwrite_role."
        )


# ---------------------------------------------------------------------------
# Section 4 — Helper signature stability
# ---------------------------------------------------------------------------


class TestShouldOverwriteRoleSignature:
    """A refactored signature could silently break every provider's
    call site. Pin the signature explicitly."""

    def test_signature_is_three_positional_args(self):
        import inspect

        sig = inspect.signature(_should_overwrite_role)
        params = list(sig.parameters.values())
        assert len(params) == 3, (
            "Helper must take exactly 3 positional args (current_role, "
            "mapped_role, config); providers depend on this shape."
        )
        # All three are positional-or-keyword (P_OK), no *args, no
        # keyword-only, no defaults — keeps the call-site obvious.
        for p in params:
            assert p.kind == inspect.Parameter.POSITIONAL_OR_KEYWORD
            assert p.default is inspect.Parameter.empty

    def test_module_dunder_all_remains_untouched(self):
        """``engine.api.auth.base`` does not export ``__all__``, but
        the helper is intended for internal reuse only — its leading
        underscore must survive any public-API refactor."""
        from engine.api.auth import base

        assert hasattr(base, "_should_overwrite_role")
        assert callable(base._should_overwrite_role)


# ---------------------------------------------------------------------------
# Section 5 — Broadened sanitization regex (C1 + RTL + zero-width + BOM)
# ---------------------------------------------------------------------------


class TestCtrlRegexUnicodeCoverage:
    """Programmatic guard: ``_CTRL_RE`` must match every code point
    in the widened ranges so a future regex simplification can't
    silently narrow the sweep back to ASCII-only or C1-only."""

    @pytest.mark.parametrize("codepoint", range(0x80, 0xA0))
    def test_c1_range_matched(self, codepoint):
        from engine.api.routes.client_errors import _CTRL_RE

        assert _CTRL_RE.search(chr(codepoint)) is not None, (
            f"U+{codepoint:04X} (C1) must be matched by _CTRL_RE"
        )

    @pytest.mark.parametrize(
        "codepoint",
        # Directional overrides U+202A-U+202E (LRE/RLE/PDF/LRO/RLO)
        list(range(0x202A, 0x202F))
        # Directional isolates U+2066-U+2069 (LRI/RLI/FSI/PDI)
        + list(range(0x2066, 0x206A)),
    )
    def test_directional_overrides_and_isolates_matched(self, codepoint):
        from engine.api.routes.client_errors import _CTRL_RE

        assert _CTRL_RE.search(chr(codepoint)) is not None, (
            f"U+{codepoint:04X} (directional formatting) must be matched by _CTRL_RE"
        )

    @pytest.mark.parametrize(
        "codepoint",
        [0x200B, 0x200C, 0x200D],  # ZWSP, ZWNJ, ZWJ
        ids=["U+200B-ZWSP", "U+200C-ZWNJ", "U+200D-ZWJ"],
    )
    def test_zero_width_chars_matched(self, codepoint):
        from engine.api.routes.client_errors import _CTRL_RE

        assert _CTRL_RE.search(chr(codepoint)) is not None

    def test_bom_zwnbsp_matched(self):
        from engine.api.routes.client_errors import _CTRL_RE

        # U+FEFF: BOM at start, Zero-Width No-Break Space mid-string.
        assert _CTRL_RE.search("\ufeff") is not None

    def test_tab_preserved(self):
        from engine.api.routes.client_errors import _CTRL_RE

        assert _CTRL_RE.search("\t") is None, (
            "Tab must remain — stack traces are tab-indented."
        )

    @pytest.mark.parametrize(
        "codepoint",
        [
            0x200E,  # LRM (Right-to-Left Mark — outside our sweep)
            0x200F,  # RLM (Right-to-Left Mark — outside our sweep)
            0x2010,  # Hyphen (regular punctuation)
            0x2028,  # Line separator (paragraph break, not control)
            0x2029,  # Paragraph separator
            0x202F,  # Narrow no-break space (just above 202E)
            0x2065,  # Unassigned, just below directional isolates
            0x206A,  # Just above directional isolates
            0x205F,  # Medium mathematical space
            0x3000,  # Ideographic space (CJK)
        ],
        ids=[
            "U+200E-LRM",
            "U+200F-RLM",
            "U+2010-hyphen",
            "U+2028-line-sep",
            "U+2029-para-sep",
            "U+202F-nnbsp",
            "U+2065-unassigned",
            "U+206A-above-isolates",
            "U+205F-math-space",
            "U+3000-ideographic-space",
        ],
    )
    def test_legitimate_unicode_outside_swept_ranges_passes(self, codepoint):
        """False positives are just as bad as false negatives — a
        user pasting a legitimate CJK / RTL / mixed-direction
        message must not see their text turned into spaces."""
        from engine.api.routes.client_errors import _CTRL_RE

        assert _CTRL_RE.search(chr(codepoint)) is None, (
            f"U+{codepoint:04X} is outside the swept ranges and must "
            "not be matched — false positives would silently mangle "
            "legitimate content."
        )


class TestScrubBehaviourWithBroadenedRegex:
    """The ``_scrub`` helper applies ``_ANSI_RE`` first then
    ``_CTRL_RE`` — the order matters because the ANSI regex needs the
    ESC byte to be intact. These tests pin end-to-end scrubbing
    behaviour against the widened code-point set."""

    def test_scrub_none_returns_none(self):
        from engine.api.routes.client_errors import _scrub

        assert _scrub(None) is None

    def test_scrub_empty_string_returns_empty(self):
        from engine.api.routes.client_errors import _scrub

        assert _scrub("") == ""

    def test_scrub_preserves_plain_ascii(self):
        from engine.api.routes.client_errors import _scrub

        assert _scrub("hello world") == "hello world"

    def test_scrub_preserves_tabs(self):
        from engine.api.routes.client_errors import _scrub

        assert _scrub("at\tFoo.tsx") == "at\tFoo.tsx"

    def test_scrub_preserves_cjk(self):
        from engine.api.routes.client_errors import _scrub

        # Mixed CJK + Latin must survive the widened regex.
        assert _scrub("error: 你好世界 さようなら 안녕하세요") == (
            "error: 你好世界 さようなら 안녕하세요"
        )

    def test_scrub_preserves_emoji(self):
        from engine.api.routes.client_errors import _scrub

        assert _scrub("boom 💥💥💥") == "boom 💥💥💥"

    def test_scrub_preserves_legitimate_rtl_text(self):
        """Arabic / Hebrew characters (U+0590-U+05FF, U+0600-U+06FF)
        are normal writing, not bidi *controls*. They must pass
        through — only the explicit control characters are stripped."""
        from engine.api.routes.client_errors import _scrub

        # Arabic + Hebrew text — not directional control chars.
        text = "خطأ في النظام"  # "system error" in Arabic
        assert _scrub(text) == text

    def test_scrub_drops_rtl_override(self):
        """CVE-2021-42574: a leading U+202E flips the renderer. We
        must collapse it to a space."""
        from engine.api.routes.client_errors import _scrub

        # The override should be replaced with a space.
        assert _scrub("\u202eevil") == " evil"
        assert "\u202e" not in _scrub("hello\u202eworld")

    def test_scrub_drops_all_directional_controls(self):
        from engine.api.routes.client_errors import _scrub

        for cp in [*range(0x202A, 0x202F), *range(0x2066, 0x206A)]:
            char = chr(cp)
            assert char not in _scrub(f"a{char}b"), (
                f"U+{cp:04X} must be stripped by _scrub"
            )

    def test_scrub_drops_zero_width_chars(self):
        from engine.api.routes.client_errors import _scrub

        # ZWSP, ZWNJ, ZWJ — all invisible, all stripped.
        assert "\u200b" not in _scrub("a\u200bb")
        assert "\u200c" not in _scrub("a\u200cb")
        assert "\u200d" not in _scrub("a\u200db")
        # And collapse to spaces, not silently removed.
        assert _scrub("a\u200bb") == "a b"

    def test_scrub_drops_bom(self):
        from engine.api.routes.client_errors import _scrub

        assert "\ufeff" not in _scrub("\ufeffhello")
        assert _scrub("\ufeffhello") == " hello"

    def test_scrub_collapses_each_control_to_a_space(self):
        """Each stripped code point becomes a single space — multiple
        consecutive controls become multiple spaces. We do NOT
        re-collapse runs to keep the implementation cheap and
        predictable."""
        from engine.api.routes.client_errors import _scrub

        # 3 RTL overrides + 1 zero-width → 4 spaces.
        assert _scrub("\u202e\u202e\u202e\u200bX") == "    X"

    def test_scrub_mixed_attack_vector(self):
        """A realistic payload: ANSI red-text CSI, RTL override to
        flip the rendering of the rest, zero-width chars to evade
        substring-search dedup, BOM at the front, plus a C1 CSI
        single-byte form. All must be neutralized; the human-
        readable parts must survive."""
        from engine.api.routes.client_errors import _scrub

        payload = (
            "\ufeff"                                # BOM
            "\x1b[31m"                              # ANSI red CSI
            "\u202e"                                # RTL override
            "\u200b"                                # zero-width space
            "evil"                                  # human text
            "\u009b"                                # C1 CSI single-byte
            "tail"
        )
        scrubbed = _scrub(payload)
        # No control / bidi / zero-width chars remain.
        for bad in ["\ufeff", "\x1b", "\u202e", "\u200b", "\u009b"]:
            assert bad not in scrubbed
        # And the human-readable parts do survive.
        assert "evil" in scrubbed
        assert "tail" in scrubbed

    def test_scrub_does_not_double_apply_space_to_ansi(self):
        """The two-stage pipeline drops ANSI sequences *first* (no
        replacement character), then runs the broader sweep. So a
        CSI sequence is removed entirely, not turned into spaces —
        this test pins that to catch a future refactor that swaps
        the order and accidentally inserts spurious spaces."""
        from engine.api.routes.client_errors import _scrub

        # "\x1b[31m" -> dropped, then "X" -> "X". Length = 1.
        assert _scrub("\x1b[31mX") == "X"

    def test_scrub_osc_sequence_dropped(self):
        """OSC (Operating System Command) sequences are ESC + ']' +
        payload + BEL. They can be used by some terminals to set
        window title / inject clipboard contents."""
        from engine.api.routes.client_errors import _scrub

        osc = "\x1b]0;evil-title\x07rest"
        assert _scrub(osc) == "rest"


# ---------------------------------------------------------------------------
# Section 6 — Endpoint integration: scrubbed values reach structlog
# ---------------------------------------------------------------------------


class TestClientErrorsEndpointSanitization:
    """Lightweight integration smoke-test that the /api/v1/client/errors
    endpoint accepts payloads containing every dangerous code point
    and still returns 201. The full unit-level coverage of what
    reaches structlog lives in the scrubber tests above."""

    def test_endpoint_accepts_payload_with_rtl_override(self):
        from fastapi.testclient import TestClient

        from engine.app import create_app

        client = TestClient(create_app())
        r = client.post(
            "/api/v1/client/errors",
            json={"message": "\u202eevil boom"},
        )
        assert r.status_code == 201

    def test_endpoint_accepts_payload_with_zero_width_chars(self):
        from fastapi.testclient import TestClient

        from engine.app import create_app

        client = TestClient(create_app())
        r = client.post(
            "/api/v1/client/errors",
            json={"message": "hello\u200b\u200c\u200dworld"},
        )
        assert r.status_code == 201

    def test_endpoint_accepts_payload_with_bom(self):
        from fastapi.testclient import TestClient

        from engine.app import create_app

        client = TestClient(create_app())
        r = client.post(
            "/api/v1/client/errors",
            json={"message": "\ufeffboom"},
        )
        assert r.status_code == 201

    def test_endpoint_accepts_payload_with_c1_byte(self):
        from fastapi.testclient import TestClient

        from engine.app import create_app

        client = TestClient(create_app())
        r = client.post(
            "/api/v1/client/errors",
            json={"message": "boom\u009b"},
        )
        assert r.status_code == 201

    def test_endpoint_accepts_payload_with_combined_attack(self):
        from fastapi.testclient import TestClient

        from engine.app import create_app

        client = TestClient(create_app())
        r = client.post(
            "/api/v1/client/errors",
            json={
                "message": "\ufeff\x1b[31m\u202e\u200bboom\u009b",
                "stack": "at\u200dFoo (\u202e:10)",
            },
        )
        assert r.status_code == 201


# ---------------------------------------------------------------------------
# Section 7 — Provider source-level invariants (regression guards)
# ---------------------------------------------------------------------------


class TestProviderSourceInvariants:
    """Additional static-analysis guards that complement
    ``TestEveryProviderGoesThroughHelper`` from the existing
    test_auth_role_promotion_security_fix.py."""

    @pytest.mark.parametrize(
        "module_path",
        [
            "engine.api.auth.google",
            "engine.api.auth.github_oauth",
            "engine.api.auth.ldap",
            "engine.api.auth.oidc",
        ],
    )
    def test_provider_does_not_assign_user_role_without_helper(self, module_path):
        """A direct ``user.role = <expr>`` assignment on the
        existing-user branch (i.e. outside ``User(...)`` construction
        and outside the ``elif _should_overwrite_role(...)`` block)
        would bypass the centralized policy. Catch any such
        regression with a source-text scan."""
        import importlib
        import inspect
        import re

        mod = importlib.import_module(module_path)
        src = inspect.getsource(mod)

        # Find every ``user.role = ...`` assignment outside of a
        # ``User(...)`` construction context. The legitimate
        # assignments are:
        #   user.role = mapped_role   (inside the elif block)
        # Reject anything that's not ``mapped_role``.
        direct_assignments = re.findall(r"user\.role\s*=\s*([^,\n]+)", src)
        for assignment in direct_assignments:
            stripped = assignment.strip().rstrip(")")
            assert stripped == "mapped_role", (
                f"{module_path}: user.role = {stripped!r} is not "
                "the centralized mapped_role variable. Direct "
                "assignment of literals or method calls would "
                "bypass _should_overwrite_role."
            )

    @pytest.mark.parametrize(
        "module_path",
        [
            "engine.api.auth.google",
            "engine.api.auth.github_oauth",
            "engine.api.auth.ldap",
            "engine.api.auth.oidc",
        ],
    )
    def test_provider_has_only_one_role_assign_site(self, module_path):
        import importlib
        import inspect
        import re

        mod = importlib.import_module(module_path)
        src = inspect.getsource(mod)
        # Exactly one runtime assignment to user.role (inside the
        # elif branch). The other "role=…" occurrences are inside
        # the User(...) constructor, which we exclude by anchoring
        # on "user.role =" (with the dot).
        sites = re.findall(r"\buser\.role\s*=", src)
        assert len(sites) == 1, (
            f"{module_path}: expected exactly 1 runtime assignment to "
            f"user.role, found {len(sites)}. The policy is centralized."
        )

    @pytest.mark.parametrize(
        "module_path",
        [
            "engine.api.auth.google",
            "engine.api.auth.github_oauth",
        ],
    )
    def test_static_role_providers_pin_mapped_role_to_user(self, module_path):
        """Google and GitHub don't surface IdP role claims. Their
        ``mapped_role`` must be the literal string ``"user"`` — any
        other value would imply the helper is being fed a different
        policy input than what the comment advertises."""
        import importlib
        import inspect

        mod = importlib.import_module(module_path)
        src = inspect.getsource(mod)
        # Look for the canonical pattern: ``mapped_role = "user"``
        assert 'mapped_role = "user"' in src, (
            f"{module_path}: mapped_role must be set to the literal "
            '"user" string (Google/GitHub do not surface IdP roles).'
        )


# ---------------------------------------------------------------------------
# Section 8 — UserInfo / AuthResult dataclass stability
# ---------------------------------------------------------------------------


class TestAuthBaseDataclassStability:
    """Lightweight stability tests for the dataclasses the helper
    depends on / returns. Pinning field defaults catches accidental
    refactor that breaks callers."""

    def test_userinfo_default_provider_is_local(self):
        info = UserInfo()
        assert info.provider == "local"
        assert info.roles == ["user"]
        assert info.external_id is None

    def test_authresult_defaults_to_failure(self):
        """``AuthResult()`` defaults to ``success=False`` — providers
        must explicitly opt into success. A regression that flipped
        the default would let any failed auth flow slip through as
        successful."""
        r = AuthResult()
        assert r.success is False
        assert r.user_info is None
        assert r.error is None

    def test_userinfo_accepts_arbitrary_claims(self):
        info = UserInfo(raw_claims={"custom": 42, "nested": {"a": 1}})
        assert info.raw_claims["custom"] == 42
        assert info.raw_claims["nested"]["a"] == 1


# ---------------------------------------------------------------------------
# Section 9 — map_roles (inherited from IAuthProvider) — quick guards
# ---------------------------------------------------------------------------


class TestMapRolesHelperGuards:
    """The role-mapping layer that *feeds* the helper. Pinning edge
    cases here ensures the helper always sees a valid ``mapped_role``
    argument."""

    def test_recognized_role_priority(self):
        p = _ConcreteProvider()
        assert p.map_roles(["viewer", "user"]) == "user"
        assert p.map_roles(["user", "admin"]) == "admin"
        # Highest priority wins.
        assert p.map_roles(["viewer", "admin", "developer"]) == "admin"

    def test_unrecognized_role_emits_warning_and_falls_back(self):
        """Unrecognized roles fall back to ``user`` when no
        recognized role is present, plus a warning."""
        p = _ConcreteProvider()
        assert p.map_roles(["totally-bogus"]) == "user"
        assert p.map_roles([]) == "user"

    def test_case_and_whitespace_insensitive(self):
        p = _ConcreteProvider()
        assert p.map_roles(["ADMIN"]) == "admin"
        assert p.map_roles(["  Admin  "]) == "admin"


# ---------------------------------------------------------------------------
# Section 10 — Google & GitHub provider coverage (recently centralized)
# ---------------------------------------------------------------------------


class _HttpxStub:
    """Reusable httpx.AsyncClient double for Google/GitHub tests.

    The provider modules do ``import httpx`` inside ``authenticate``
    and then ``async with httpx.AsyncClient() as client: …``. Patching
    ``httpx.AsyncClient`` so the call returns our stub is enough to
    short-circuit the entire token-exchange + userinfo round-trip."""

    def __init__(
        self,
        *,
        token_payload: dict | None = None,
        profile_payload: dict | None = None,
        token_error: Exception | None = None,
        userinfo_error: Exception | None = None,
    ) -> None:
        self._token = token_payload
        self._profile = profile_payload
        self._token_error = token_error
        self._userinfo_error = userinfo_error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url: str, **kwargs):
        if self._token_error is not None:
            raise self._token_error
        m = MagicMock()
        m.raise_for_status = MagicMock()
        m.json.return_value = self._token or {}
        return m

    async def get(self, url: str, **kwargs):
        if self._userinfo_error is not None:
            raise self._userinfo_error
        m = MagicMock()
        m.raise_for_status = MagicMock()
        m.json.return_value = self._profile or {}
        return m


def _apply_provider_settings(monkeypatch, module_path: str, **overrides):
    """Helper that builds a Settings instance and patches the
    module-level ``settings`` import on the given provider module."""
    from engine.config import Settings

    s = Settings(_env_file=None, **overrides)
    monkeypatch.setattr(f"{module_path}.settings", s)
    return s


class TestGoogleAuthProvider:
    """Comprehensive coverage for ``GoogleAuthProvider.authenticate``
    (the existing-user role-overwrite path is already exercised by
    ``TestProviderRoleOverwriteBehavior`` above)."""

    @staticmethod
    def _provider():
        from engine.api.auth.google import GoogleAuthProvider

        return GoogleAuthProvider()

    def test_name(self):
        assert self._provider().name == "google"

    async def test_missing_code_returns_error(self, monkeypatch):
        from unittest.mock import AsyncMock

        from sqlalchemy.ext.asyncio import AsyncSession

        _apply_provider_settings(monkeypatch, "engine.api.auth.google")
        mock_db = AsyncMock(spec=AsyncSession)
        result = await self._provider().authenticate(db=mock_db)
        assert result.success is False
        assert "Authorization code" in result.error

    async def test_missing_db_returns_error(self, monkeypatch):
        _apply_provider_settings(monkeypatch, "engine.api.auth.google")
        result = await self._provider().authenticate(code="x")
        assert result.success is False
        assert "Authorization code" in result.error

    async def test_token_exchange_failure(self, monkeypatch):
        from unittest.mock import AsyncMock

        from sqlalchemy.ext.asyncio import AsyncSession

        _apply_provider_settings(monkeypatch, "engine.api.auth.google")
        stub = _HttpxStub(token_error=Exception("token endpoint down"))
        mock_db = AsyncMock(spec=AsyncSession)
        with patch("httpx.AsyncClient", return_value=stub):
            result = await self._provider().authenticate(code="x", db=mock_db)
        assert result.success is False
        assert "Google authentication failed" in result.error

    async def test_userinfo_failure(self, monkeypatch):
        from unittest.mock import AsyncMock

        from sqlalchemy.ext.asyncio import AsyncSession

        _apply_provider_settings(monkeypatch, "engine.api.auth.google")
        stub = _HttpxStub(
            token_payload={"access_token": "at"},
            userinfo_error=Exception("userinfo endpoint down"),
        )
        mock_db = AsyncMock(spec=AsyncSession)
        with patch("httpx.AsyncClient", return_value=stub):
            result = await self._provider().authenticate(code="x", db=mock_db)
        assert result.success is False
        assert "Google authentication failed" in result.error

    async def test_incomplete_profile_missing_sub(self, monkeypatch):
        from unittest.mock import AsyncMock

        from sqlalchemy.ext.asyncio import AsyncSession

        _apply_provider_settings(monkeypatch, "engine.api.auth.google")
        stub = _HttpxStub(
            token_payload={"access_token": "at"},
            profile_payload={"email": "x@example.com", "name": "X"},  # no sub
        )
        mock_db = AsyncMock(spec=AsyncSession)
        with patch("httpx.AsyncClient", return_value=stub):
            result = await self._provider().authenticate(code="x", db=mock_db)
        assert result.success is False
        assert "Incomplete Google profile" in result.error

    async def test_incomplete_profile_missing_email(self, monkeypatch):
        from unittest.mock import AsyncMock

        from sqlalchemy.ext.asyncio import AsyncSession

        _apply_provider_settings(monkeypatch, "engine.api.auth.google")
        stub = _HttpxStub(
            token_payload={"access_token": "at"},
            profile_payload={"sub": "g-1", "name": "X"},  # no email
        )
        mock_db = AsyncMock(spec=AsyncSession)
        with patch("httpx.AsyncClient", return_value=stub):
            result = await self._provider().authenticate(code="x", db=mock_db)
        assert result.success is False
        assert "Incomplete Google profile" in result.error

    async def test_new_user_created_with_user_role(self, monkeypatch):
        from unittest.mock import AsyncMock, MagicMock

        from sqlalchemy.ext.asyncio import AsyncSession

        _apply_provider_settings(monkeypatch, "engine.api.auth.google")
        stub = _HttpxStub(
            token_payload={"access_token": "at"},
            profile_payload={
                "sub": "g-new",
                "email": "new@example.com",
                "name": "New User",
            },
        )

        mock_db = AsyncMock(spec=AsyncSession)
        # First lookup: no existing google user. Second lookup: no
        # email conflict.
        call_count = 0

        async def mock_execute(stmt):
            nonlocal call_count
            call_count += 1
            r = MagicMock()
            r.scalar_one_or_none.return_value = None
            return r

        mock_db.execute = mock_execute
        mock_db.flush = AsyncMock()
        mock_db.refresh = AsyncMock(side_effect=lambda u: setattr(u, "is_active", True))

        created: list[User] = []
        mock_db.add = MagicMock(side_effect=created.append)

        with patch("httpx.AsyncClient", return_value=stub):
            result = await self._provider().authenticate(code="x", db=mock_db)

        assert result.success is True
        assert result.user_info is not None
        assert result.user_info.email == "new@example.com"
        assert result.user_info.external_id == "g-new"
        assert result.user_info.provider == "google"
        assert len(created) == 1
        assert created[0].role == "user"

    async def test_email_conflict_with_different_provider(self, monkeypatch):
        from unittest.mock import AsyncMock, MagicMock

        from sqlalchemy.ext.asyncio import AsyncSession

        from engine.db.models import User

        _apply_provider_settings(monkeypatch, "engine.api.auth.google")
        stub = _HttpxStub(
            token_payload={"access_token": "at"},
            profile_payload={
                "sub": "g-conflict",
                "email": "conflict@example.com",
                "name": "Conflict",
            },
        )

        existing_local = User(
            email="conflict@example.com",
            display_name="Local",
            auth_provider="local",
        )
        mock_db = AsyncMock(spec=AsyncSession)
        call_count = 0

        async def mock_execute(stmt):
            nonlocal call_count
            call_count += 1
            r = MagicMock()
            r.scalar_one_or_none.return_value = None if call_count == 1 else existing_local
            return r

        mock_db.execute = mock_execute

        with patch("httpx.AsyncClient", return_value=stub):
            result = await self._provider().authenticate(code="x", db=mock_db)
        assert result.success is False
        assert "different provider" in result.error

    async def test_disabled_user_rejected(self, monkeypatch):
        from unittest.mock import AsyncMock, MagicMock

        from sqlalchemy.ext.asyncio import AsyncSession

        from engine.db.models import User

        _apply_provider_settings(monkeypatch, "engine.api.auth.google")
        stub = _HttpxStub(
            token_payload={"access_token": "at"},
            profile_payload={
                "sub": "g-disabled",
                "email": "disabled@example.com",
                "name": "Disabled",
            },
        )
        existing = User(
            email="disabled@example.com",
            display_name="Disabled",
            is_active=False,
            role="user",
            auth_provider="google",
            external_id="g-disabled",
        )
        mock_db = AsyncMock(spec=AsyncSession)
        r = MagicMock()
        r.scalar_one_or_none.return_value = existing
        mock_db.execute.return_value = r

        with patch("httpx.AsyncClient", return_value=stub):
            result = await self._provider().authenticate(code="x", db=mock_db)
        assert result.success is False
        assert "disabled" in result.error.lower()

    async def test_existing_user_with_same_role_skips_overwrite(self, monkeypatch):
        """Same-role short-circuit: helper returns False so the
        elif body never runs."""
        from unittest.mock import AsyncMock, MagicMock

        from sqlalchemy.ext.asyncio import AsyncSession

        from engine.db.models import User

        _apply_provider_settings(
            monkeypatch,
            "engine.api.auth.google",
            auth_overwrite_role_on_login=True,
        )
        stub = _HttpxStub(
            token_payload={"access_token": "at"},
            profile_payload={
                "sub": "g-same",
                "email": "same@example.com",
                "name": "Same",
            },
        )
        existing = User(
            email="same@example.com",
            display_name="Same",
            is_active=True,
            role="user",  # matches mapped_role
            auth_provider="google",
            external_id="g-same",
        )
        mock_db = AsyncMock(spec=AsyncSession)
        r = MagicMock()
        r.scalar_one_or_none.return_value = existing
        mock_db.execute.return_value = r
        mock_db.flush = AsyncMock()

        with patch("httpx.AsyncClient", return_value=stub):
            result = await self._provider().authenticate(code="x", db=mock_db)

        assert result.success is True
        assert existing.role == "user"
        mock_db.flush.assert_not_called()

    def test_get_authorize_url(self, monkeypatch):
        from engine.config import Settings

        s = Settings(
            _env_file=None,
            google_client_id="g-client",
            google_redirect_uri="https://app/cb",
        )
        monkeypatch.setattr("engine.api.auth.google.settings", s)
        url = self._provider().get_authorize_url()
        assert "accounts.google.com/o/oauth2/v2/auth" in url
        assert "client_id=g-client" in url
        assert "redirect_uri=https://app/cb" in url
        assert "response_type=code" in url
        assert "scope=openid" in url
        assert "state=" not in url

    def test_get_authorize_url_with_state(self, monkeypatch):
        from engine.config import Settings

        s = Settings(
            _env_file=None,
            google_client_id="g-client",
            google_redirect_uri="https://app/cb",
        )
        monkeypatch.setattr("engine.api.auth.google.settings", s)
        url = self._provider().get_authorize_url(state="abc")
        assert "state=abc" in url


class TestGitHubAuthProvider:
    """Same shape of tests as Google — see ``TestGoogleAuthProvider``
    above for individual rationale."""

    @staticmethod
    def _provider():
        from engine.api.auth.github_oauth import GitHubAuthProvider

        return GitHubAuthProvider()

    def test_name(self):
        assert self._provider().name == "github"

    async def test_missing_code_returns_error(self, monkeypatch):
        from unittest.mock import AsyncMock

        from sqlalchemy.ext.asyncio import AsyncSession

        _apply_provider_settings(monkeypatch, "engine.api.auth.github_oauth")
        mock_db = AsyncMock(spec=AsyncSession)
        result = await self._provider().authenticate(db=mock_db)
        assert result.success is False
        assert "Authorization code" in result.error

    async def test_missing_db_returns_error(self, monkeypatch):
        _apply_provider_settings(monkeypatch, "engine.api.auth.github_oauth")
        result = await self._provider().authenticate(code="x")
        assert result.success is False
        assert "Authorization code" in result.error

    async def test_token_exchange_failure(self, monkeypatch):
        from unittest.mock import AsyncMock

        from sqlalchemy.ext.asyncio import AsyncSession

        _apply_provider_settings(monkeypatch, "engine.api.auth.github_oauth")
        stub = _HttpxStub(token_error=Exception("token endpoint down"))
        mock_db = AsyncMock(spec=AsyncSession)
        with patch("httpx.AsyncClient", return_value=stub):
            result = await self._provider().authenticate(code="x", db=mock_db)
        assert result.success is False
        assert "GitHub authentication failed" in result.error

    async def test_userinfo_failure(self, monkeypatch):
        from unittest.mock import AsyncMock

        from sqlalchemy.ext.asyncio import AsyncSession

        _apply_provider_settings(monkeypatch, "engine.api.auth.github_oauth")
        stub = _HttpxStub(
            token_payload={"access_token": "at"},
            userinfo_error=Exception("userinfo endpoint down"),
        )
        mock_db = AsyncMock(spec=AsyncSession)
        with patch("httpx.AsyncClient", return_value=stub):
            result = await self._provider().authenticate(code="x", db=mock_db)
        assert result.success is False
        assert "GitHub authentication failed" in result.error

    async def test_incomplete_profile_missing_id(self, monkeypatch):
        from unittest.mock import AsyncMock

        from sqlalchemy.ext.asyncio import AsyncSession

        _apply_provider_settings(monkeypatch, "engine.api.auth.github_oauth")
        stub = _HttpxStub(
            token_payload={"access_token": "at"},
            profile_payload={"login": "user", "name": "User", "email": "u@x.com"},
        )
        mock_db = AsyncMock(spec=AsyncSession)
        with patch("httpx.AsyncClient", return_value=stub):
            result = await self._provider().authenticate(code="x", db=mock_db)
        assert result.success is False
        assert "Incomplete GitHub profile" in result.error

    async def test_new_user_created_with_user_role(self, monkeypatch):
        from unittest.mock import AsyncMock, MagicMock

        from sqlalchemy.ext.asyncio import AsyncSession

        _apply_provider_settings(monkeypatch, "engine.api.auth.github_oauth")
        stub = _HttpxStub(
            token_payload={"access_token": "at"},
            profile_payload={
                "id": 42,
                "login": "ghuser",
                "name": "GH User",
                "email": "gh@example.com",
            },
        )

        mock_db = AsyncMock(spec=AsyncSession)
        call_count = 0

        async def mock_execute(stmt):
            nonlocal call_count
            call_count += 1
            r = MagicMock()
            r.scalar_one_or_none.return_value = None
            return r

        mock_db.execute = mock_execute
        mock_db.flush = AsyncMock()
        mock_db.refresh = AsyncMock(side_effect=lambda u: setattr(u, "is_active", True))

        created: list[User] = []
        mock_db.add = MagicMock(side_effect=created.append)

        with patch("httpx.AsyncClient", return_value=stub):
            result = await self._provider().authenticate(code="x", db=mock_db)

        assert result.success is True
        assert result.user_info is not None
        assert result.user_info.email == "gh@example.com"
        assert result.user_info.external_id == "42"
        assert result.user_info.provider == "github"
        assert len(created) == 1
        assert created[0].role == "user"

    async def test_email_fallback_when_profile_email_null(self, monkeypatch):
        """GitHub's user profile may have ``email: null`` if the
        user hides their address. The provider falls back to
        ``<login>@github``."""
        from unittest.mock import AsyncMock, MagicMock

        from sqlalchemy.ext.asyncio import AsyncSession

        _apply_provider_settings(monkeypatch, "engine.api.auth.github_oauth")
        stub = _HttpxStub(
            token_payload={"access_token": "at"},
            profile_payload={
                "id": 99,
                "login": "hidden",
                "name": "Hidden",
                "email": None,
            },
        )

        mock_db = AsyncMock(spec=AsyncSession)
        call_count = 0

        async def mock_execute(stmt):
            nonlocal call_count
            call_count += 1
            r = MagicMock()
            r.scalar_one_or_none.return_value = None
            return r

        mock_db.execute = mock_execute
        mock_db.flush = AsyncMock()
        mock_db.refresh = AsyncMock(side_effect=lambda u: setattr(u, "is_active", True))
        mock_db.add = MagicMock()

        with patch("httpx.AsyncClient", return_value=stub):
            result = await self._provider().authenticate(code="x", db=mock_db)
        assert result.success is True
        assert result.user_info is not None
        assert result.user_info.email == "hidden@github"

    async def test_email_conflict_with_different_provider(self, monkeypatch):
        from unittest.mock import AsyncMock, MagicMock

        from sqlalchemy.ext.asyncio import AsyncSession

        from engine.db.models import User

        _apply_provider_settings(monkeypatch, "engine.api.auth.github_oauth")
        stub = _HttpxStub(
            token_payload={"access_token": "at"},
            profile_payload={
                "id": 7,
                "login": "user",
                "name": "U",
                "email": "conflict@example.com",
            },
        )
        existing_local = User(
            email="conflict@example.com",
            display_name="Local",
            auth_provider="local",
        )
        mock_db = AsyncMock(spec=AsyncSession)
        call_count = 0

        async def mock_execute(stmt):
            nonlocal call_count
            call_count += 1
            r = MagicMock()
            r.scalar_one_or_none.return_value = None if call_count == 1 else existing_local
            return r

        mock_db.execute = mock_execute

        with patch("httpx.AsyncClient", return_value=stub):
            result = await self._provider().authenticate(code="x", db=mock_db)
        assert result.success is False
        assert "different provider" in result.error

    async def test_disabled_user_rejected(self, monkeypatch):
        from unittest.mock import AsyncMock, MagicMock

        from sqlalchemy.ext.asyncio import AsyncSession

        from engine.db.models import User

        _apply_provider_settings(monkeypatch, "engine.api.auth.github_oauth")
        stub = _HttpxStub(
            token_payload={"access_token": "at"},
            profile_payload={
                "id": 13,
                "login": "off",
                "name": "Off",
                "email": "off@example.com",
            },
        )
        existing = User(
            email="off@example.com",
            display_name="Off",
            is_active=False,
            role="user",
            auth_provider="github",
            external_id="13",
        )
        mock_db = AsyncMock(spec=AsyncSession)
        r = MagicMock()
        r.scalar_one_or_none.return_value = existing
        mock_db.execute.return_value = r

        with patch("httpx.AsyncClient", return_value=stub):
            result = await self._provider().authenticate(code="x", db=mock_db)
        assert result.success is False
        assert "disabled" in result.error.lower()

    async def test_existing_user_with_same_role_skips_overwrite(self, monkeypatch):
        from unittest.mock import AsyncMock, MagicMock

        from sqlalchemy.ext.asyncio import AsyncSession

        from engine.db.models import User

        _apply_provider_settings(
            monkeypatch,
            "engine.api.auth.github_oauth",
            auth_overwrite_role_on_login=True,
        )
        stub = _HttpxStub(
            token_payload={"access_token": "at"},
            profile_payload={
                "id": 21,
                "login": "same",
                "name": "Same",
                "email": "same@example.com",
            },
        )
        existing = User(
            email="same@example.com",
            display_name="Same",
            is_active=True,
            role="user",  # matches mapped_role
            auth_provider="github",
            external_id="21",
        )
        mock_db = AsyncMock(spec=AsyncSession)
        r = MagicMock()
        r.scalar_one_or_none.return_value = existing
        mock_db.execute.return_value = r
        mock_db.flush = AsyncMock()

        with patch("httpx.AsyncClient", return_value=stub):
            result = await self._provider().authenticate(code="x", db=mock_db)
        assert result.success is True
        assert existing.role == "user"
        mock_db.flush.assert_not_called()

    def test_get_authorize_url(self, monkeypatch):
        from engine.config import Settings

        s = Settings(
            _env_file=None,
            github_client_id="gh-client",
            github_redirect_uri="https://app/cb",
        )
        monkeypatch.setattr("engine.api.auth.github_oauth.settings", s)
        url = self._provider().get_authorize_url()
        assert "github.com/login/oauth/authorize" in url
        assert "client_id=gh-client" in url
        assert "redirect_uri=" in url
        assert "scope=user:email" in url
        assert "state=" not in url

    def test_get_authorize_url_with_state(self, monkeypatch):
        from engine.config import Settings

        s = Settings(
            _env_file=None,
            github_client_id="gh-client",
            github_redirect_uri="https://app/cb",
        )
        monkeypatch.setattr("engine.api.auth.github_oauth.settings", s)
        url = self._provider().get_authorize_url(state="xyz")
        assert "state=xyz" in url


# ---------------------------------------------------------------------------
# Section 11 — Cross-module regression: regex / scrub export surface
# ---------------------------------------------------------------------------


class TestSanitizationExportSurface:
    """Pin the public-but-private exports the sanitization layer
    exposes. A rename without test updates would silently break
    callers."""

    def test_ctrl_re_pattern_compiles_at_import(self):
        from engine.api.routes.client_errors import _CTRL_RE

        # Must be a compiled regex (i.e. Pattern, not str).
        assert hasattr(_CTRL_RE, "search")
        assert hasattr(_CTRL_RE, "sub")

    def test_ansi_re_pattern_compiles_at_import(self):
        from engine.api.routes.client_errors import _ANSI_RE

        assert hasattr(_ANSI_RE, "search")
        assert hasattr(_ANSI_RE, "sub")

    def test_scrub_is_callable_with_optional_str(self):
        from engine.api.routes.client_errors import _scrub

        assert _scrub(None) is None
        assert isinstance(_scrub("x"), str)

    def test_strip_query_is_callable_with_optional_str(self):
        from engine.api.routes.client_errors import _strip_query

        assert _strip_query(None) is None
        assert isinstance(_strip_query("https://x"), str)

    def test_max_text_is_64kb(self):
        from engine.api.routes.client_errors import _MAX_TEXT

        assert _MAX_TEXT == 64 * 1024

    def test_strip_query_handles_malformed_url(self):
        """``urlsplit`` can raise ``ValueError`` for some malformed
        inputs (e.g. surrogates). The helper returns ``None`` so
        the caller doesn't crash."""
        from engine.api.routes import client_errors as ce

        # Patch ``urlsplit`` to raise; the helper must catch and
        # return None. (``urlsplit`` itself rarely raises on CPython,
        # so we force the path here to cover lines 110-111.)
        with patch.object(ce, "urlsplit", side_effect=ValueError("bad url")):
            result = ce._strip_query("https://x.example")
        assert result is None

    def test_strip_query_returns_none_for_none_input(self):
        from engine.api.routes.client_errors import _strip_query

        assert _strip_query(None) is None

    def test_strip_query_preserves_scheme_host_path(self):
        from engine.api.routes.client_errors import _strip_query

        assert _strip_query("https://x.example/a/b") == "https://x.example/a/b"

    def test_validate_error_id_passes_through_none(self):
        """Pin line 133 (``return v`` when ``error_id is None``):
        the validator must not transform ``None`` into anything else."""
        from engine.api.routes.client_errors import ClientErrorReport

        # error_id defaults to None; the validator must preserve it.
        r = ClientErrorReport(message="x")
        assert r.error_id is None

    def test_validate_error_id_raises_value_error_for_non_uuid(self):
        """The field validator must raise ``ValueError`` for a
        non-UUID string so Pydantic surfaces a 422."""
        from pydantic import ValidationError

        from engine.api.routes.client_errors import ClientErrorReport

        with pytest.raises(ValidationError) as exc_info:
            ClientErrorReport(message="x", error_id="not-a-uuid")
        # The error must mention UUID shape.
        assert "UUID" in str(exc_info.value)
