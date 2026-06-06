from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import structlog

logger = structlog.get_logger()


# Maximum length of a role string that we will persist onto ``user.role``
# from an IdP-asserted claim. The longest legitimate internal role is
# ``portfolio_manager`` (17 characters); the cap exists to refuse
# absurdly long strings that a misconfigured or hostile IdP might push
# in lieu of a real role name. Note that this is an application-layer
# sanity cap on the *role identifier*, **not** a reflection of the
# underlying database column width (the column is sized independently
# in the ORM migration).
_MAX_ROLE_LENGTH: int = 64

# Matches characters that must not appear inside an IdP-asserted role
# string: ASCII C0 controls (NUL..US), DEL + C1 controls (DEL..APC),
# plus the most common Unicode invisible / bidi-override characters
# (zero-width spaces ZWSP..ZWJ, LRM/RLM, LRE/RLO/PDI, BOM). Any of
# these would be invisible to operators tailing audit logs yet could
# affect string equality or terminal rendering — they are stripped
# before the role is considered for persistence.
_CONTROL_CHARS_RE: re.Pattern[str] = re.compile(
    r"[\x00-\x1f\x7f-\x9f\u200b-\u200f\u202e\ufeff]"
)


def _sanitize_role(mapped_role: Any) -> str:
    """Return a cleaned role string suitable for assignment to
    ``user.role``.

    - Non-string inputs collapse to the default ``"user"`` role.
    - ASCII C0 / DEL + C1 controls and Unicode invisible / bidi-override
      characters are removed.
    - The result is truncated to :data:`_MAX_ROLE_LENGTH`.
    - A whitespace-only result collapses to ``"user"``.
    """
    if not isinstance(mapped_role, str):
        return "user"
    cleaned = _CONTROL_CHARS_RE.sub("", mapped_role).strip()
    if len(cleaned) > _MAX_ROLE_LENGTH:
        cleaned = cleaned[:_MAX_ROLE_LENGTH]
    return cleaned or "user"


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


def _apply_role_mapping(user: Any, mapped_role: str, config: Any) -> bool:
    """Apply an IdP-mapped role to an existing user, honoring the
    ``auth_overwrite_role_on_login`` opt-in policy.

    This helper centralizes the overwrite-or-skip decision so every
    federated provider (LDAP, OIDC, Google, GitHub) makes the same
    choice and emits the same audit-event shape (SEV-741). Providers
    must not mutate ``user.role`` directly on the federated-login
    path — they call this helper instead.

    The supplied ``mapped_role`` is sanitized (control characters
    stripped, length capped to :data:`_MAX_ROLE_LENGTH`) before the
    overwrite decision is made, so a hostile IdP cannot smuggle hidden
    bytes or log-bomb payloads into the audit trail.

    Args:
        user: An existing ORM ``User`` instance. ``user.role`` is read
            and (when policy allows) replaced in place.
        mapped_role: The candidate role string produced by
            :meth:`IAuthProvider.map_roles` (or its default).
        config: Settings-like object exposing
            ``auth_overwrite_role_on_login``.

    Returns:
        ``True`` if ``user.role`` was changed; the caller is then
        responsible for persisting the change (e.g. ``await
        db.flush()``). ``False`` when the policy blocked the write
        (no-op, role unchanged, no audit event emitted).
    """
    sanitized = _sanitize_role(mapped_role)
    if not _should_overwrite_role(user.role, sanitized, config):
        return False
    logger.info(
        "auth.role_overwritten",
        provider=getattr(user, "auth_provider", None),
        user_id=str(getattr(user, "id", "")),
        previous_role=user.role,
        new_role=sanitized,
    )
    user.role = sanitized
    return True


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
            normalized = role.lower().strip()
            if normalized in role_priority:
                recognized.append(normalized)
                if best is None or role_priority[normalized] > role_priority[best]:
                    best = normalized
            else:
                unrecognized.append(role)
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
