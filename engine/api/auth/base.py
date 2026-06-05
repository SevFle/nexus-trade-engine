from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import structlog

from engine.config import settings

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Log sanitization helpers
# ---------------------------------------------------------------------------
#
# Unrecognized IdP role strings are surfaced in operator-facing warnings.
# Because they originate from an external system they must be sanitized
# before they are emitted to logs to prevent:
#
#   * log-injection via embedded newlines / carriage returns
#   * terminal escape sequence abuse (ANSI bombs)
#   * storage blow-ups from arbitrarily long DNs, JWTs or claim blobs
#     that some IdPs happily stuff into group claim strings

# Maximum length, in characters, of any single role string that we are
# willing to emit to a log line. Anything beyond this is truncated with
# a single trailing ellipsis so operators can still tell truncation
# happened.
_MAX_LOG_ROLE_LENGTH: int = 128

# Matches the C0 ASCII control block (U+0000 - U+001F) and DEL (U+007F).
# Stripped from role strings before they reach any log aggregator.
_CONTROL_CHARS_RE: re.Pattern[str] = re.compile(r"[\x00-\x1f\x7f]")


def sanitize_role_for_log(role: Any) -> str:
    """Return a log-safe, length-capped representation of *role*.

    This is the single chokepoint used by :meth:`IAuthProvider.map_roles`
    before any unrecognized external role reaches the structured warning
    payload. It guarantees:

    1. The result is a ``str`` (some IdPs emit role claims as bytes
       or numeric ids, which would otherwise blow up structlog's
       JSON serializer).
    2. All ASCII C0 control characters and DEL are stripped — defends
       against log injection and ANSI-escape attacks.
    3. The result is at most :data:`_MAX_LOG_ROLE_LENGTH` characters
       long (plus a trailing ``…`` if truncated) — defends against
       log-storage DoS from pathological IdP payloads.

    Note that this function is intentionally **pure**: it does not
    mutate its input and has no side effects, which makes it safe to
    call from exception handlers and signal paths.
    """
    if role is None:
        return ""
    cleaned = _CONTROL_CHARS_RE.sub("", str(role))
    if len(cleaned) > _MAX_LOG_ROLE_LENGTH:
        cleaned = cleaned[:_MAX_LOG_ROLE_LENGTH] + "…"
    return cleaned


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

        Security notes
        --------------
        * **No implicit promotion** — only roles explicitly listed in
          ``role_priority`` are eligible to become the user's role;
          anything else is dropped and a warning is emitted so
          operators can detect misconfigurations. Previously
          ``viewer`` was silently promoted to ``user`` and
          ``quant_dev`` to ``developer``, which constituted a silent
          privilege escalation (SEV-741).
        * **Least-privilege fallback** — when the IdP asserts no
          recognized role (either an empty claim or only unknown
          strings) the function falls back to ``viewer``, the lowest
          privilege level in the hierarchy. Previously the fallback
          was ``user`` which grants write access. This tightening
          means a misconfigured IdP cannot accidentally grant write
          access to a federated user with no mapped groups.
        * **Log sanitization** — unrecognized role strings are run
          through :func:`sanitize_role_for_log` before they reach the
          warning payload, defending against log injection and
          storage blow-ups from pathological IdP payloads.
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
                unrecognized=[sanitize_role_for_log(r) for r in unrecognized],
                recognized=recognized,
                mapped=best if best is not None else "viewer",
            )
        return best if best is not None else "viewer"

    def should_overwrite_existing_role(
        self,
        existing_role: str,
        mapped_role: str,
    ) -> bool:
        """Return True iff a federated login should overwrite an
        existing user's stored role with the freshly-mapped IdP role.

        Centralizes the SEV-741 guard:

        * If ``settings.auth_overwrite_role_on_login`` is **False**
          (the default) the existing role is preserved verbatim and a
          federated login can never silently widen or narrow a user's
          privileges — a misconfigured or compromised upstream IdP has
          no effect on previously-granted local roles.
        * If the flag is True the role is overwritten only when it
          actually differs from the current value, avoiding needless
          writes and DB churn on no-op logins.

        Subclasses (e.g. :class:`LDAPAuthProvider`,
        :class:`OIDCAuthProvider`) call this helper on every federated
        callback that touches an existing user record.
        """
        if not settings.auth_overwrite_role_on_login:
            logger.info(
                "auth.federated.preserve_role",
                provider=self.name,
                reason="auth_overwrite_role_on_login disabled",
                existing_role=existing_role,
                mapped_role=mapped_role,
            )
            return False
        return existing_role != mapped_role
