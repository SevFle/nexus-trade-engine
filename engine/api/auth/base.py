from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import structlog

logger = structlog.get_logger()


# Maximum length of a persisted role string. Matches the ``String(20)``
# constraint on :class:`engine.db.models.User.role`. Roles longer than
# this are treated as unrecognized by :meth:`IAuthProvider.map_roles`
# so they cannot reach the persistence layer and trigger a database
# error or be used for log-bombing.
_MAX_ROLE_LENGTH = 20

# Characters that must never appear in a persisted or logged role
# string. Covers:
#
# - C0 control characters (``\\x00``-``\\x1f``): NUL injection, log
#   splitting via CR/LF, terminal bells, etc.
# - DEL (``\\x7f``) and the C1 control block (``\\u0080``-``\\u009f``):
#   interpreted as terminal-control bytes by 8-bit-clean terminals
#   (notably ``\\u009b`` acting as a CSI lead byte).
# - Zero-width and bidi formatting characters (``\\u200b``-``\\u200f``):
#   ZWSP / ZWNJ / ZWJ / LRM / RLM - visual spoofing vectors that let
#   a hostile IdP impersonate a privileged role in dashboards.
# - Right-to-Left Override (``\\u202e``): the canonical Trojan-source
#   bidi attack.
# - BOM / Zero-Width No-Break Space (``\\ufeff``): hides a payload at
#   the start of a string.
_CONTROL_CHARS_RE = re.compile(
    r"[\x00-\x1f\x7f-\x9f\u200b-\u200f\u202e\ufeff]"
)


def _sanitize_role(role: str) -> str:
    """Strip control / bidi / zero-width characters from a role string.

    Used by :meth:`IAuthProvider.map_roles` to sanitize the
    ``unrecognized`` payload of its warning event so that a hostile
    upstream Identity Provider cannot perform log injection via CR/LF
    or terminal-control sequences in role names.

    The function is intentionally simple and idempotent: a clean input
    is returned unchanged, and applying the function a second time
    never alters the output. Callers that need to additionally cap
    length (e.g. for log lines) should do so after this function.
    """
    if not isinstance(role, str):
        return ""
    return _CONTROL_CHARS_RE.sub("", role)


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

        Sanitization boundary: control / bidi / zero-width characters
        (see :data:`_CONTROL_CHARS_RE`) are stripped from the
        ``unrecognized`` payload of the warning event **before**
        logging, so a hostile IdP cannot perform log injection via
        CR/LF or terminal-control sequences. The match against the
        recognized-role set is deliberately performed on the raw
        normalized input — a role like ``"admin\\u200B"`` is **not**
        silently coerced to ``"admin"``; it falls through to
        ``unrecognized`` and the user receives the safe default
        ``"user"``. Roles longer than :data:`_MAX_ROLE_LENGTH` are
        treated as unrecognized so they never reach the persistence
        layer (which constrains ``User.role`` to ``String(20)``).
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
            # Skip silently when the input is not a string (defence
            # against malformed IdP claims). A non-string sentinel
            # never matches a recognized role and would clutter the
            # unrecognized payload if logged verbatim.
            if not isinstance(role, str):
                continue
            normalized = role.lower().strip()
            # After whitespace stripping, an empty string or a string
            # composed entirely of control characters carries no
            # signal — silently skip it rather than poisoning the
            # unrecognized-role warning.
            if not normalized or not _sanitize_role(normalized):
                continue
            # Reject overlong inputs up front: they cannot fit the
            # ``User.role`` column and would never match a recognized
            # role (longest valid role, ``portfolio_manager``, is 17
            # chars). Logging them verbatim would also let an attacker
            # use the IdP's role claim as a log-bomb.
            if len(normalized) > _MAX_ROLE_LENGTH:
                unrecognized.append(_sanitize_role(role))
                continue
            if normalized in role_priority:
                recognized.append(normalized)
                if best is None or role_priority[normalized] > role_priority[best]:
                    best = normalized
            else:
                # Sanitize before logging so a CR/LF- or ANSI-laden
                # role name cannot split the log line or hijack a
                # downstream terminal. The original ``role`` is what
                # we emit (after sanitization), not the lowercased
                # form, so operators can spot typos in casing.
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
