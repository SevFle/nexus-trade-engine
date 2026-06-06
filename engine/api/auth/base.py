from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import structlog

logger = structlog.get_logger()


# Maximum length for any single role string included in log payloads.
# External Identity-Provider (IdP) group DN strings can be arbitrarily
# long; capping the logged value prevents log-injection / disk-exhaustion
# attacks where a malicious IdP feeds oversized payloads.
_MAX_LOGGED_ROLE_LEN = 256

# Pattern that matches ASCII control characters (0x00-0x1F and 0x7F)
# excluding the horizontal tab (0x09) which is generally benign inside
# log payloads. Used to scrub CR/LF and similar control characters that
# could be used for log injection / terminal escape attacks.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")


def _sanitize_role_for_log(role: str) -> str:
    """Sanitize a single role string before including it in log records.

    - Strip ASCII control characters (CR/LF, NUL, etc.) so that a
      malicious upstream IdP cannot perform log injection by embedding
      line breaks or terminal escape sequences in a role/group name.
    - Cap the length to ``_MAX_LOGGED_ROLE_LEN`` to bound log volume and
      protect downstream aggregators from pathologically long inputs.

    Non-string inputs are coerced to ``str`` and then sanitized.
    """
    text = role if isinstance(role, str) else str(role)
    text = _CONTROL_CHARS_RE.sub("", text)
    if len(text) > _MAX_LOGGED_ROLE_LEN:
        text = text[:_MAX_LOGGED_ROLE_LEN] + "…"
    return text


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

        Least-privilege fallback: when the input list is empty or every
        supplied role is unrecognized, the function returns ``viewer``
        (the lowest-privilege role in the hierarchy). Returning ``user``
        here would have granted write privileges to an attacker who
        controls upstream role assertions without ever having a
        recognized role claim validated.
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
            # Sanitize each raw role string before logging to prevent log
            # injection (CR/LF, terminal escapes) and bound log volume.
            sanitized_unrecognized = [_sanitize_role_for_log(r) for r in unrecognized]
            logger.warning(
                "auth.map_roles.unrecognized_roles",
                provider=self.name,
                unrecognized=sanitized_unrecognized,
                recognized=recognized,
                mapped=best if best is not None else "viewer",
            )
        return best if best is not None else "viewer"
