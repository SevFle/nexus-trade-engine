from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from engine.config import Settings
    from engine.db.models import User

logger = structlog.get_logger()


# Control characters and invisible Unicode that must never appear in a
# normalized role string.  Goes beyond ASCII C0 controls (U+0000-U+001F)
# and DEL (U+007F) to also cover additional dangerous classes:
#   - C1 control range          : U+0080 - U+009F
#   - Right-to-Left Override    : U+202E  (visual-spoofing vector)
#   - Zero-width chars          : U+200B - U+200D  (invisible joiners / ZWSP)
#   - BOM / Zero-Width No-Break : U+FEFF
# Any of these embedded in an IdP-asserted role can either spoof role
# names visually (the role 'admin' followed by U+200B looks like
# 'admin' but compares unequal) or trigger display / parsing bugs in
# downstream admin tools.
_CONTROL_CHARS_RE = re.compile(
    r"[\u0000-\u001F\u007F"  # C0 controls + DEL
    r"\u0080-\u009F"          # C1 controls
    r"\u202E"                 # Right-to-Left Override
    r"\u200B-\u200D"          # Zero-width Space / Joiner / Non-Joiner
    r"\uFEFF]"                # BOM / Zero-Width No-Break Space
)


def _sanitize_role(role: str) -> str:
    """Strip control characters and invisible Unicode from a role string.

    Applied to the OUTPUT of :meth:`IAuthProvider.map_roles` (i.e. the
    single internal role string we are about to persist to
    ``User.role``) **before** it is handed to either (a) the ``User``
    constructor at first-seen creation time, or (b)
    :func:`_apply_role_mapping` for an existing-user overwrite.

    Sanitization happens at the boundary between the external IdP role
    vocabulary and our internal single-role column, so that no invisible
    or visually-spoofing characters reach the database, audit log, or
    downstream authorization decisions.

    If the result is empty after stripping (e.g. the role was comprised
    solely of control chars), callers should treat that as a mapping
    failure and fall back to the default ``"user"`` role.
    """
    return _CONTROL_CHARS_RE.sub("", role)


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
        mapped = best if best is not None else "user"
        # Sanitize at the IdP->internal boundary.  An empty post-strip
        # result is treated as a mapping failure and falls back to the
        # default ``user`` role, so we never persist an empty role.
        sanitized = _sanitize_role(mapped)
        return sanitized if sanitized else "user"


def _apply_role_mapping(
    user: User,
    mapped_role: str,
    config: Settings,
) -> bool:
    """Conditionally overwrite an existing user's role with the IdP-mapped role.

    Centralizes the overwrite-or-skip logic for federated login.  The
    ``user.role`` attribute is overwritten with ``mapped_role`` **only**
    when ``config.auth_overwrite_role_on_login`` is ``True``.  When
    ``False`` (the default — see SEV-741), the existing locally-granted
    role is preserved and only an info-level audit event is emitted.

    This helper is intended to be called for EXISTING users after
    provider lookup; first-time user creation assigns the role directly
    on the ``User`` constructor and does not need the overwrite guard.

    Returns ``True`` if the role was actually changed, ``False``
    otherwise (caller may use the return value to decide whether to
    ``await db.flush()``).
    """
    if user.role == mapped_role:
        return False
    if not config.auth_overwrite_role_on_login:
        logger.info(
            "auth.role_overwrite_skipped",
            provider=user.auth_provider,
            external_id=user.external_id,
            current_role=user.role,
            mapped_role=mapped_role,
        )
        return False
    previous = user.role
    user.role = mapped_role
    logger.info(
        "auth.role_overwritten",
        provider=user.auth_provider,
        external_id=user.external_id,
        previous_role=previous,
        new_role=mapped_role,
    )
    return True
