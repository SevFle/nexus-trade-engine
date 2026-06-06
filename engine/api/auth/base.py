from __future__ import annotations

import re
import unicodedata
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import structlog

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# SEV-741 follow-up: hard role allow-list + sanitization helper
# ---------------------------------------------------------------------------
#
# ``ALLOWED_ROLES`` is the single source of truth for the set of internal
# role names a federated login is permitted to assert. It mirrors the
# keys of ``IAuthProvider.map_roles``'s ``role_priority`` table; keeping
# both in sync is enforced by the ``test_allowed_roles_matches_priority``
# test in ``tests/test_auth_role_promotion_security_fix.py``.
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

# Hard upper bound on role-string length. The longest legitimate role
# is ``"portfolio_manager"`` (17 chars); 32 leaves headroom while still
# rejecting any oversized payload before the regex/NFKC work below.
_MAX_ROLE_LEN = 32

# Control characters (C0 + DEL + C1). Any role containing one of these
# is rejected outright — we do *not* silently strip, because a payload
# that needed stripping was almost certainly crafted.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _sanitize_role(raw: Any) -> str:
    """Return a canonical role name for an IdP-asserted string, or
    ``"user"`` if the input fails any safety check.

    Pipeline (short-circuit, cheapest checks first):

    1. **Type guard** — non-string input collapses immediately.
    2. **Oversize guard** — anything longer than ``_MAX_ROLE_LEN`` is
       rejected *before* the control-char regex or NFKC runs, so a
       multi-megabyte payload cannot DoS the normalization step.
    3. **Control-character guard** — any C0/DEL/C1 byte causes
       rejection (we do not silently strip — see SEV-741).
    4. **NFKC stability guard** — if ``unicodedata.normalize("NFKC",
       raw) != raw`` the input contained compatibility characters
       (fullwidth Latin, ligatures, superscripts, …). These are a
       well-known homoglyph attack vector; the normalized form is
       *not* accepted.
    5. **Allow-list match** — case-folded, whitespace-trimmed form
       must be a member of ``ALLOWED_ROLES``.

    The fallback to ``"user"`` (rather than ``None`` or an exception)
    keeps the calling code simple: a misconfigured or hostile IdP
    cannot break authentication, only downgrade the assertion.
    """
    if not isinstance(raw, str):
        return "user"
    # 2. Reject oversized input immediately, before regex processing.
    if len(raw) > _MAX_ROLE_LEN:
        return "user"
    # 3. Reject any control characters — do not silently strip.
    if _CONTROL_CHARS_RE.search(raw):
        return "user"
    # 4. Reject if NFKC would change the string (fullwidth / ligatures
    #    / compatibility characters are a homoglyph attack vector).
    if unicodedata.normalize("NFKC", raw) != raw:
        return "user"
    # 5. Validate against the allow-list (case-insensitive).
    lowered = raw.lower().strip()
    if lowered in ALLOWED_ROLES:
        return lowered
    return "user"


def _should_overwrite_role(
    current_role: str | None,
    mapped_role: str,
    config: Any,
    *,
    is_new_user: bool = True,
) -> bool:
    """Return True if an existing user's role should be replaced with the
    IdP-mapped role on this federated login.

    Centralizes the ``auth_overwrite_role_on_login`` policy so every
    provider makes the same decision (SEV-741). A misconfigured or
    compromised upstream Identity Provider must not be able to silently
    downgrade or escalate a previously-granted local role on each
    federated login — operators opt in explicitly via the setting.

    - ``current_role is None`` *and* the caller signals a brand-new
      user (``is_new_user=True``, the default): always True — there
      is nothing to preserve.
    - ``current_role is None`` *and* the caller signals an existing
      user (``is_new_user=False``): treated as a data anomaly (a row
      in ``users`` whose ``role`` column is NULL). The IdP-mapped
      role is **not** allowed to silently fill the gap — operators
      must opt in via ``auth_overwrite_role_on_login=True``.
    - ``current_role == mapped_role``: False (no-op write would be
      wasted work and would emit a misleading audit event).
    - Otherwise: True iff ``config.auth_overwrite_role_on_login`` is
      truthy.
    """
    if current_role is None:
        if is_new_user:
            return True
        # Existing user with a NULL role is an anomaly — do not let
        # federated login silently repair it without operator opt-in.
        return bool(getattr(config, "auth_overwrite_role_on_login", False))
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
        reflected faithfully: only roles that are explicitly listed in
        ``role_priority`` are eligible to become the user's role; anything
        else is dropped and a warning is emitted so operators can detect
        misconfigurations. Previously ``viewer`` was silently promoted to
        ``user`` and ``quant_dev`` to ``developer``, which constituted a
        silent privilege escalation (SEV-741).

        SEV-741 follow-up: every external role string is routed through
        ``_sanitize_role`` *before* the priority comparison, so that
        oversized payloads, control-character injection, and Unicode
        homoglyph attacks (fullwidth Latin, ligatures, …) all collapse
        safely to ``"user"`` rather than reaching the allow-list.
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
            sanitized = _sanitize_role(role)
            # ``_sanitize_role`` collapses any invalid input to
            # ``"user"`` — but ``"user"`` is itself a legitimate role,
            # so to distinguish "fell back" from "really user" we
            # require the sanitized canonical name to match a plain
            # lowercased+stripped form of the raw input. Any input
            # that needed NFKC / control-char / size handling fails
            # this equality and is recorded as unrecognized.
            normalized_input = (
                role.lower().strip() if isinstance(role, str) else ""
            )
            if sanitized in role_priority and sanitized == normalized_input:
                recognized.append(sanitized)
                if best is None or role_priority[sanitized] > role_priority[best]:
                    best = sanitized
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
