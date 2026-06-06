from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Role-name sanitization (defense-in-depth against Trojan-Source-style attacks)
# ---------------------------------------------------------------------------
#
# Role strings arrive from external Identity Providers and flow into our
# DB and audit log. A malicious IdP (or one with a sloppy group DN) can
# embed:
#
#   * C0 control characters (U+0000-U+001F) — terminal escapes, log
#     injection;
#   * DEL and the C1 range (U+007F-U+009F) — interpreted as terminal-
#     control bytes by 8-bit-clean terminals (notably U+009B as CSI);
#   * Unicode Bidi controls (U+200B-U+200F, U+202E) — the "Trojan
#     Source" attack vector that can make an admin role string render
#     as something harmless in a UI while being byte-distinct; and
#   * U+FEFF (BOM / zero-width no-break space) — invisible prefix that
#     can hide a malicious role name in a sidebar.
#
# All four classes are stripped from every external role string BEFORE
# it is compared to the recognized-role table — otherwise a payload like
# ``"admin\u202e"`` would sail past the ``role_priority`` lookup and
# silently be stored as the user's role, breaking RBAC invariants.
_CONTROL_CHARS_RE = re.compile(
    r"[\x00-\x1f\x7f-\x9f\u200b-\u200f\u202e\ufeff]"
)

# Maximum length of a sanitized role string. The DB column
# (``User.role``) is ``String(20)``; we cap at 64 in memory to leave
# headroom for migration tools and to fail fast on run-away IdP payloads
# (e.g. a misconfigured LDAP DN mistakenly treated as a role name) well
# before they reach the column-constraint error path.
_MAX_ROLE_LENGTH = 64


def _sanitize_role(role: str) -> str:
    """Strip control characters (C0/C1/Unicode Bidi/zero-width),
    surrounding whitespace, and lowercase the result. Truncates to
    ``_MAX_ROLE_LENGTH``.

    Called by ``map_roles`` *before* the recognized-role lookup so a
    malicious payload cannot smuggle past the priority table — see the
    SEV-741 defense-in-depth note on ``_CONTROL_CHARS_RE`` above.
    """
    if not isinstance(role, str):
        return ""
    cleaned = _CONTROL_CHARS_RE.sub("", role).strip().lower()
    return cleaned[:_MAX_ROLE_LENGTH]


def _should_overwrite_role(
    current_role: str | None,
    mapped_role: str,
    config: Any,
) -> bool:
    """Return True if an existing user's role should be replaced with the
    IdP-mapped role on this federated login.

    Centralizes the ``auth_overwrite_role_on_login`` policy so every
    provider makes the same decision (SEV-741). A misconfigured or
    compromised upstream Identity Provider must not be able to silently
    downgrade or escalate a previously-granted local role on each
    federated login — operators opt in explicitly via the setting.

    - ``current_role is None`` (new user, no prior local role): always
      True. There is nothing to preserve.
    - ``current_role == mapped_role``: False (no-op write would be
      wasted work and would emit a misleading audit event).
    - Otherwise: True iff ``config.auth_overwrite_role_on_login`` is
      truthy.
    """
    if current_role is None:
        return True
    if current_role == mapped_role:
        return False
    return bool(getattr(config, "auth_overwrite_role_on_login", False))


@dataclass
class UserInfo:
    external_id: str | None = None
    email: str = ""
    display_name: str = ""
    provider: str = "local"
    roles: list[str] = field(default_factory=lambda: ["user"])
    raw_claims: dict[str, Any] = field(default_factory=dict)


@dataclass
class AuthResult:
    success: bool = False
    user_info: UserInfo | None = None
    error: str | None = None


class IAuthProvider(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    async def authenticate(self, **kwargs: Any) -> AuthResult: ...

    async def get_user_info(self, _external_id: str) -> UserInfo | None:
        return None

    async def create_user(self, _user_info: UserInfo, **_kwargs: Any) -> AuthResult:
        return AuthResult(success=False, error=f"User creation not supported by {self.name}")

    async def apply_role_mapping(
        self,
        user: Any,
        mapped_role: str,
        config: Any,
        db: AsyncSession | None = None,
    ) -> bool:
        """Apply the IdP-mapped role to an existing ``user`` if the
        centralized policy permits it.

        Encapsulates the overwrite-or-skip logic so every federated
        provider (LDAP, OIDC, Google, GitHub) makes the same decision
        for the same inputs (SEV-741). Returns True when ``user.role``
        was actually changed, False otherwise (same role, opt-out, or
        new-user path which is handled by the caller before this is
        invoked). Emits a single, provider-tagged audit event on a
        successful overwrite so operators can correlate IdP-driven
        role changes.

        ``db`` is flushed only when an overwrite actually happens; the
        no-op cases avoid a round-trip.
        """
        if not _should_overwrite_role(user.role, mapped_role, config):
            return False
        logger.info(
            f"auth.{self.name}.role_overwritten",
            user_id=str(getattr(user, "id", "")),
            previous_role=user.role,
            new_role=mapped_role,
        )
        user.role = mapped_role
        if db is not None:
            await db.flush()
        return True

    def map_roles(self, external_roles: list[str]) -> str:
        """Map an external IdP role list to a single internal role.

        Security note: this function performs **no implicit promotion** of
        unrecognized roles. Upstream Identity-Provider (IdP) roles are
        reflected faithfully: only roles that are explicitly listed in
        ``role_priority`` are eligible to become the user's role; anything
        else is dropped and a warning is emitted so operators can detect
        misconfigurations. Previously ``viewer`` was silently promoted to
        ``user`` and ``quant_dev`` to ``developer``, which constituted a
        silent privilege escalation (SEV-741).

        Defense-in-depth: every external role string is run through
        ``_sanitize_role`` *before* the ``role_priority`` lookup. This
        prevents a malicious IdP from smuggling a Bidi-mangled payload
        like ``"admin\\u202e"`` past the recognized-role check — the
        sanitized form is compared, so byte-distinct lookalikes fall
        through to the unrecognized branch and the user is granted only
        the (safe) default role.
        """
        role_priority: dict[str, int] = {
            "viewer": 0,
            "user": 1,
            "retail_trader": 2,
            "quant_dev": 3,
            "developer": 4,
            "portfolio_manager": 5,
            "admin": 6,
        }
        recognized: list[str] = []
        unrecognized: list[str] = []
        best: str | None = None
        for role in external_roles:
            # Sanitize BEFORE comparison so a Bidi-control payload
            # cannot match a recognized key.
            normalized = _sanitize_role(role)
            if normalized in role_priority:
                recognized.append(normalized)
                if best is None or role_priority[normalized] > role_priority[best]:
                    best = normalized
            else:
                # Log a bounded, control-char-free form so a 100KB
                # attacker payload or a terminal-escape sequence
                # cannot reach the audit log. The sanitized form is
                # still useful for debugging misconfigurations.
                unrecognized.append(_sanitize_role(role))
        # Broaden the warning: fire on ANY unrecognized external role, not
        # only when the entire set is unrecognized. This surfaces partial
        # misconfigurations (e.g. one stale group name alongside valid ones).
        if unrecognized:
            logger.warning(
                "auth.map_roles.unrecognized_roles",
                provider=self.name,
                unrecognized=unrecognized,
                recognized=recognized,
                mapped=best if best is not None else "user",
            )
        return best if best is not None else "user"
