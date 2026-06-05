from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import structlog

logger = structlog.get_logger()

# Maximum length of an unrecognized role string that we will retain
# for logging. Anything longer is truncated to avoid bloating log
# payloads (and to defang log-injection attempts that rely on
# arbitrary-length payloads). Mirrors typical field-length guards
# in audit pipelines.
_UNRECOGNIZED_ROLE_MAX_LEN = 128

# Match ASCII control characters (0x00-0x1F and 0x7F), including
# newlines, tabs, and the DEL character. Used to sanitize raw
# role strings before they are persisted to logs.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")


def _sanitize_unrecognized_role(role: str) -> str:
    """Strip ASCII control characters and cap length on an arbitrary
    external role string before it is logged or otherwise reflected
    back to operators.

    This is a **defensive** sanitization layer — unrecognized role
    strings never reach the database (they are dropped before
    being mapped to an internal role), but they are emitted in
    warning log payloads so that operators can detect IdP
    misconfigurations. Without sanitization, a malicious or
    misconfigured upstream could inject newline / ANSI-escape
    sequences into log streams in order to forge log lines or
    tamper with terminal output. Capping the length bounds the
    size of the log payload (defending against log-flooding).
    """
    if role is None:
        return ""
    cleaned = _CONTROL_CHARS_RE.sub("", str(role))
    if len(cleaned) > _UNRECOGNIZED_ROLE_MAX_LEN:
        cleaned = cleaned[:_UNRECOGNIZED_ROLE_MAX_LEN]
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

        Defense-in-depth note: when *no* recognized role is present in
        the upstream claim (either because the claim is empty or because
        every entry is unrecognized), the function falls back to
        ``viewer`` — the **least-privileged** role in the priority table.
        Previously this fallback was ``user``, which silently granted
        a higher privilege than necessary to anyone whose IdP supplied
        an empty or wholly-unknown roles claim. (Follow-up to SEV-741.)

        Sanitization: unrecognized role strings are stripped of ASCII
        control characters and capped at 128 characters before being
        appended to the ``unrecognized`` list and emitted in the
        warning payload. This prevents a misconfigured or hostile IdP
        from injecting newlines / escape sequences into operator log
        streams or ballooning log record sizes.
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
                # Defensive sanitization before the string is reflected
                # back to operators via the warning payload. The raw
                # external claim is never persisted to the database —
                # only ``best`` (an entry from ``role_priority``) is —
                # but log injection is still a concern.
                sanitized = _sanitize_unrecognized_role(role)
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
                mapped=best if best is not None else "viewer",
            )
        # Fall back to ``viewer`` (the lowest-privilege recognized role)
        # rather than ``user``. This ensures that a user whose IdP
        # supplies an empty or wholly-unrecognized roles claim receives
        # only the minimum privilege required to authenticate, rather
        # than an implicit upgrade to ``user``.
        return best if best is not None else "viewer"
