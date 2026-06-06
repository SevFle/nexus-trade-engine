from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import structlog

logger = structlog.get_logger()

# Maximum length of any role string we will emit to logs.  External IdP
# role names are operator-controlled strings; an attacker who can craft
# arbitrary role values could otherwise flood log pipelines with megabyte-
# long payloads.  128 chars is well above every legitimate role identifier
# in this codebase while still bounding log line size.
_LOG_ROLE_MAX_LENGTH = 128

# Match every C0 control character (0x00-0x1F) and DEL (0x7F), plus the
# Unicode "Control" category (Cc).  Tab (0x09) and LF/CR are intentionally
# stripped: log-injection via embedded newlines has historically been used
# to forge fake log lines in downstream aggregators.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")


def sanitize_role_for_log(role: Any) -> str:
    """Return a safe representation of *role* suitable for log payloads.

    Defends against three classes of log-injection / log-flooding:

    * **Control characters** (ASCII 0x00-0x1F, 0x7F) are stripped, which
      prevents newline-injection attacks against downstream log shippers
      that may split events on raw ``\\n``.
    * **Length** is capped at ``_LOG_ROLE_MAX_LENGTH`` (128 chars) so a
      malicious IdP cannot exhaust log storage with megabyte-scale role
      names.
    * **Non-string** inputs are coerced to ``str`` (the empty string when
      ``None``) — the helper is called from defensive code paths that
      must never raise on bad input.
    """
    if role is None:
        return ""
    text = role if isinstance(role, str) else str(role)
    text = _CONTROL_CHARS_RE.sub("", text)
    if len(text) > _LOG_ROLE_MAX_LENGTH:
        text = text[:_LOG_ROLE_MAX_LENGTH]
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

        Fallback note: when no recognized role is present the function
        falls back to ``"viewer"`` — the lowest-privilege role — and emits
        a dedicated ``auth.map_roles.fallback_role`` warning.  The previous
        fallback of ``"user"`` granted write access to anyone whose IdP
        happened to send an unrecognized role, which is itself a privilege
        escalation vector.
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
        # Sanitize every external role value before it touches the logger.
        # A malicious or misconfigured IdP can otherwise write arbitrary
        # bytes (newlines, megabyte-scale names) into our log pipeline.
        safe_unrecognized = [sanitize_role_for_log(r) for r in unrecognized]
        safe_recognized = [sanitize_role_for_log(r) for r in recognized]
        # Broaden the warning: fire on ANY unrecognized external role, not
        # only when the entire set is unrecognized. This surfaces partial
        # misconfigurations (e.g. one stale group name alongside valid ones).
        if unrecognized:
            mapped_for_log = (
                sanitize_role_for_log(best) if best is not None else "viewer"
            )
            logger.warning(
                "auth.map_roles.unrecognized_roles",
                provider=self.name,
                unrecognized=safe_unrecognized,
                recognized=safe_recognized,
                mapped=mapped_for_log,
            )
        if best is None:
            # Dedicated warning when the fallback fires — this is the
            # signal an operator uses to detect "no mapping configured
            # at all" vs. "partial mapping".  Distinct event name keeps
            # alerting rules clean.
            logger.warning(
                "auth.map_roles.fallback_role",
                provider=self.name,
                mapped="viewer",
                external_count=len(external_roles),
                unrecognized=safe_unrecognized,
            )
            return "viewer"
        return best
