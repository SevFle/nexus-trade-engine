from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import structlog

logger = structlog.get_logger()


# Characters we strip from unrecognized-role strings before logging.
# This covers the entire C0 + C1 control plane (U+0000-U+001F, U+007F-U+009F)
# including NUL, BEL, BS, TAB, LF, CR, ESC, DEL and friends. These are the
# characters that can break log parsers, hide subsequent text in terminals,
# or smuggle content into structured-log downstream consumers.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")

# Maximum number of characters of an unrecognized role we are willing to
# emit to logs. Anything longer is almost certainly garbage (e.g. a token
# accidentally routed through the role claim) and would just bloat log
# aggregators.
_MAX_LOG_ROLE_LENGTH = 128


def sanitize_role_for_log(role: str) -> str:
    """Return a log-safe representation of an external role string.

    External (IdP-supplied) role strings are user-controlled and must not
    be emitted verbatim into structured logs — they may contain C0/C1
    control characters that break terminal rendering, log parsers, or
    downstream SIEM ingestion, or they may be arbitrarily long payloads
    that bloat log aggregators.

    This helper:

    * Strips every character in the Unicode control planes (``\\x00``-
      ``\\x1f``, ``\\x7f``-``\\x9f``). Newlines, tabs and carriage
      returns are removed as well — they are the most dangerous in a
      logging context (log forging / terminal escape injection).
    * Truncates the result to :data:`_MAX_LOG_ROLE_LENGTH` characters
      and appends an ellipsis indicator when truncation occurs.
    * Preserves printable ASCII and printable Unicode (letters,
      punctuation, emoji, etc.) as-is so the value remains useful for
      operator triage.

    The empty string is returned unchanged.
    """
    if not role:
        return role
    cleaned = _CONTROL_CHARS_RE.sub("", role)
    if len(cleaned) > _MAX_LOG_ROLE_LENGTH:
        cleaned = cleaned[:_MAX_LOG_ROLE_LENGTH] + "..."
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
                # Sanitize IdP-supplied strings before they reach the log
                # pipeline — they may contain control chars or be very
                # long, both of which break log parsers / terminals.
                unrecognized=[sanitize_role_for_log(r) for r in unrecognized],
                recognized=recognized,
                mapped=best if best is not None else "user",
            )
        return best if best is not None else "user"
