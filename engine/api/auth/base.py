from __future__ import annotations

import re
import unicodedata
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger()


# Frozen set of recognized internal roles. Used as the source of truth
# by both ``_sanitize_role`` (incoming IdP claim validation) and
# ``IAuthProvider.map_roles``. Defining it once here prevents the two
# surfaces from drifting out of sync (SEV-741 follow-up).
ALLOWED_ROLES: frozenset[str] = frozenset(
    {
        "viewer",
        "user",
        "retail_trader",
        "quant_dev",
        "developer",
        "portfolio_manager",
        "admin",
    }
)


# Suspicious Unicode to strip from incoming role claims:
#   * \u202a-\u202e — BiDi override / embedding controls (Trojan Source).
#   * \u2066-\u2069 — BiDi directional isolates.
#   * \u200b-\u200f — zero-width characters + LRE/RLE/PDF.
#   * \u2028/\u2029 — line/paragraph separators (log injection).
_ROLE_BIDI_RE = re.compile(r"[\u202a-\u202e\u2066-\u2069\u200b-\u200f\u2028\u2029]")


# Hard ceiling on the raw role string length accepted from an IdP claim.
# Real roles are <= 17 chars ("portfolio_manager"); the cap is generous so
# legitimate trailing/leading whitespace never trips it, while pathological
# multi-kilobyte payloads (NFKC DoS / log-flooding) are rejected before any
# normalization work happens. The persisted column is String(20).
_MAX_ROLE_LENGTH = 64


def _sanitize_role(role: Any) -> str:
    """Return a safe, normalized role string.

    Pipeline (order matters):

    0. Length guard — inputs longer than ``_MAX_ROLE_LENGTH`` collapse to
       ``"user"`` immediately, before any normalization work.
    1. ``unicodedata.normalize('NFKC')`` — collapses visually-equivalent
       code points (e.g. fullwidth U+FF41..U+FF4E spelling "admin" collapse
       to plain ASCII) so a homoglyph attack cannot bypass the allow-list.
    2. Strip BiDi overrides, directional isolates, zero-width, and
       line/paragraph separators via ``_ROLE_BIDI_RE`` so an attacker
       can't smuggle an "admin" role past a casual visual inspection
       of logs (Trojan Source-style attack).
    3. Lowercase + strip whitespace.
    4. Validate against ``ALLOWED_ROLES``; if not present, collapse to
       the safe default ``"user"`` and emit a warning so operators
       can detect misconfigured / hostile upstream IdPs.

    Non-string inputs (None, list, dict) collapse to ``"user"`` rather
    than raising — the caller (``_apply_role_mapping``) treats this as
    a soft-fail and authentication proceeds with the default role.
    """
    if not isinstance(role, str):
        logger.warning(
            "auth.sanitize_role.rejected",
            raw=role,
            reason="not_string",
        )
        return "user"

    # 0. Early length rejection — reject absurdly long role strings before
    #    doing any NFKC / regex work (DoS + log-flooding hardening). A real
    #    role is short; anything beyond the cap collapses to the safe default.
    if len(role) > _MAX_ROLE_LENGTH:
        logger.warning(
            "auth.sanitize_role.rejected",
            raw=role,
            reason="too_long",
            length=len(role),
        )
        return "user"

    # 1. NFKC normalize first so visually-equivalent unicode collapses.
    normalized = unicodedata.normalize("NFKC", role)
    # 2. Strip BiDi / zero-width / line-separator characters.
    stripped = _ROLE_BIDI_RE.sub("", normalized)
    # 3. Lowercase + strip whitespace.
    cleaned = stripped.lower().strip()
    # 4. Validate against ALLOWED_ROLES.
    if cleaned not in ALLOWED_ROLES:
        logger.warning(
            "auth.sanitize_role.rejected",
            raw=role,
            normalized=cleaned,
            reason="not_in_allowed_roles",
        )
        return "user"
    return cleaned


def _should_overwrite_role(
    current_role: str | None,
    mapped_role: str,
    config: Any,
    *,
    is_new_user: bool = False,
) -> bool:
    """Return True if an existing user's role should be replaced with the
    IdP-mapped role on this federated login.

    Centralizes the ``auth_overwrite_role_on_login`` policy so every
    provider makes the same decision (SEV-741). A misconfigured or
    compromised upstream Identity Provider must not be able to silently
    downgrade or escalate a previously-granted local role on each
    federated login — operators opt in explicitly via the setting.

    - ``current_role is None`` AND ``is_new_user=True`` (caller
      confirms this is a fresh insert): always True. There is nothing
      to preserve.
    - ``current_role is None`` AND ``is_new_user=False`` (an existing
      user row that, anomalously, has no local role yet): True iff
      ``config.auth_overwrite_role_on_login`` is truthy. Defends
      against an attacker-controlled upstream row wipe masquerading
      as a "fresh insert".
    - ``current_role == mapped_role``: False (no-op write would be
      wasted work and would emit a misleading audit event).
    - Otherwise: True iff ``config.auth_overwrite_role_on_login`` is
      truthy.
    """
    if current_role is None:
        if is_new_user:
            return True
        # Existing user with no prior role: still require explicit
        # operator opt-in. Only the ``is_new_user=True`` path
        # short-circuits the policy.
        return bool(getattr(config, "auth_overwrite_role_on_login", False))
    if current_role == mapped_role:
        return False
    return bool(getattr(config, "auth_overwrite_role_on_login", False))


async def _apply_role_mapping(
    user: Any,
    mapped_role: str,
    config: Any,
    *,
    is_new_user: bool = False,
    provider_name: str = "auth",
    db: AsyncSession | None = None,
) -> None:
    """Sanitize the IdP-mapped role and, subject to the
    ``_should_overwrite_role`` policy, persist it on the user row.

    Every federated provider must funnel through this helper so that
    sanitization, the opt-in overwrite decision, the audit-log event,
    and the database flush all happen in lock-step. Skipping it
    reintroduces the SEV-741 silent-escalation surface.

    The caller is responsible for the ``is_active`` gate — disabled
    users must be rejected **before** this helper runs so we never
    mutate the role of a disabled account (which would be both
    pointless and a quiet privilege escalation the moment the account
    is reactivated).
    """
    sanitized = _sanitize_role(mapped_role)
    if _should_overwrite_role(
        user.role, sanitized, config, is_new_user=is_new_user
    ):
        logger.info(
            f"auth.{provider_name}.role_overwritten",
            user_id=str(getattr(user, "id", "")),
            previous_role=user.role,
            new_role=sanitized,
            is_new_user=is_new_user,
        )
        user.role = sanitized
        if db is not None:
            await db.flush()


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
