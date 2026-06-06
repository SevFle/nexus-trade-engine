from __future__ import annotations

import re
import unicodedata
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import structlog

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Role sanitization primitives (SEV-741 follow-up: allowlist validation)
# ---------------------------------------------------------------------------
#
# Operators wire upstream Identity Providers (IdP) — OIDC, SAML, LDAP,
# GitHub, Google — to the engine.  The role claims surfaced by those
# providers are ultimately persisted on the local ``User.role`` column
# and used as input to every RBAC check.
#
# Earlier code only lower-cased and stripped the incoming claim, which
# left the door open to:
#
#   * **Role injection** — an attacker controlling one corner of the
#     IdP claim payload could push arbitrary strings into our DB.
#   * **Unicode spoofing** — fullwidth / compatibility code-points
#     (fullwidth ``admin``) and bidi-override code-points (``U+202E``) could
#     trick a human reviewer while comparing equal to a privileged
#     role under Unicode collation.
#   * **Denial-of-service** — multi-megabyte role strings flowing
#     through audit logs and DB indexes.
#
# The fixes below layer three independent defences:
#
#   1. ``NFKC`` normalisation collapses fullwidth / compatibility
#      code-points to their canonical ASCII form *before* the regex
#      ever sees them, so fullwidth ``admin`` is recognised as ``admin``
#      while still failing the allowlist (which only accepts ASCII
#      post-normalisation) if the original had non-ASCII characters
#      that NFKC does not collapse.
#   2. The strict allowlist ``^[A-Za-z0-9_-]{1,64}$`` replaces the
#      previous denylist approach.  Anything that is not an ASCII
#      letter, digit, underscore or hyphen — including all bidi
#      override characters, whitespace, control chars, punctuation
#      and combining marks — is rejected.
#   3. ``ALLOWED_ROLES`` is the closed set of internal roles the
#      engine will persist on a ``User``.  Any sanitised string that
#      is not in this set is collapsed to ``user`` so legacy /
#      misconfigured IdP roles cannot elevate privileges.

ALLOWED_ROLES: frozenset[str] = frozenset(
    {
        "user",
        "viewer",
        "developer",
        "portfolio_manager",
        "admin",
    }
)

# Allowlist regex: ASCII letters, digits, underscore, hyphen. 1-64 chars.
# Oversize strings are *rejected* (not truncated) so a 10 kB payload
# cannot sneak through after being silently chopped.
_ROLE_PATTERN: re.Pattern[str] = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# Priority ordering within ALLOWED_ROLES — higher wins when multiple
# valid roles are claimed. Kept as a plain ``dict`` so the same lookup
# path used by ``require_role`` (``ROLE_HIERARCHY``) keeps working for
# legacy / domain-specific roles that may exist on previously-created
# ``User`` rows.
_ROLE_PRIORITY: dict[str, int] = {
    "viewer": 0,
    "user": 1,
    "developer": 2,
    "portfolio_manager": 3,
    "admin": 4,
}


def _sanitize_role(role: Any) -> str | None:
    """Apply NFKC normalisation + allowlist validation to a raw role.

    Returns the lower-cased, validated role string ready for lookup in
    ``ALLOWED_ROLES``, or ``None`` if the input fails any of:

    * not a ``str`` (defensive — IdP claims can be ints, dicts, …)
    * post-NFKC form contains characters outside ``[A-Za-z0-9_-]``
    * empty after ``strip()``
    * longer than 64 characters (rejected, *not* truncated)
    """

    if not isinstance(role, str):
        return None
    try:
        normalised = unicodedata.normalize("NFKC", role)
    except (TypeError, ValueError):
        # Defensive: ``unicodedata.normalize`` only raises on weird
        # input types but we want to be explicit about never crashing
        # the auth path because of a malformed claim.
        return None
    stripped = normalised.strip()
    if not _ROLE_PATTERN.match(stripped):
        return None
    return stripped.lower()


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

    Importantly, a ``None`` ``current_role`` on an *existing* user
    (i.e. a legacy row whose role column was never populated) is NOT
    treated as a special "new user" case here — the helper is only
    invoked on the existing-user branch of every provider, so a
    ``None`` role at this point means "we have a User row but no
    role". Allowing the IdP to silently populate that role would be
    a privilege-escalation vector (SEV-741 follow-up: ``None`` is
    no longer a short-circuit).

    - ``current_role == mapped_role``: False (no-op write would be
      wasted work and would emit a misleading audit event).
    - Otherwise: True iff ``config.auth_overwrite_role_on_login`` is
      truthy.
    """
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

    def map_roles(self, external_roles: list[str]) -> str:
        """Map an external IdP role list to a single internal role.

        Security note: this function performs **no implicit promotion** of
        unrecognized roles. Upstream Identity-Provider (IdP) roles are
        reflected faithfully: only roles that survive the
        ``_sanitize_role`` allowlist **and** are listed in
        ``ALLOWED_ROLES`` are eligible to become the user's role;
        anything else is dropped and a warning is emitted so operators
        can detect misconfigurations. Previously ``viewer`` was silently
        promoted to ``user`` and ``quant_dev`` to ``developer``, which
        constituted a silent privilege escalation (SEV-741).

        SEV-741 follow-up: the previously-recognised domain roles
        ``retail_trader`` and ``quant_dev`` are no longer in the
        ``ALLOWED_ROLES`` closed set, so they collapse to ``user``.
        Legacy users that already carry those roles still get the
        correct RBAC treatment via ``ROLE_HIERARCHY`` in
        ``engine.api.auth.dependency``; we only restrict *new* role
        assignment here.
        """
        recognized: list[str] = []
        unrecognized: list[str] = []
        best: str | None = None
        for role in external_roles:
            sanitized = _sanitize_role(role)
            if sanitized is None:
                unrecognized.append(role if isinstance(role, str) else repr(role))
                continue
            if sanitized in ALLOWED_ROLES:
                recognized.append(sanitized)
                if best is None or _ROLE_PRIORITY[sanitized] > _ROLE_PRIORITY[best]:
                    best = sanitized
            else:
                # Sanitisation succeeded but the role is outside the
                # ALLOWED_ROLES closed set (e.g. legacy
                # ``retail_trader`` / ``quant_dev``).  Collapse to
                # ``user`` rather than persisting a non-allowlist
                # value, and surface it in the warning so operators
                # can clean up the IdP-side mapping.
                unrecognized.append(sanitized)
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
