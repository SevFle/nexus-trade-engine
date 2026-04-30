"""Security response headers middleware.

Emits a defensive default for Content-Security-Policy plus the standard
hardening headers (HSTS, X-Content-Type-Options, X-Frame-Options,
Referrer-Policy, Permissions-Policy). Each header is opt-out via
:class:`SecurityHeadersConfig` so endpoints that intentionally serve
embeddable widgets / cross-origin assets can override.

CSRF protection is not enforced here — auth/session layers handle that
at the dependency level. This module is purely about response-header
hardening.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from starlette.types import ASGIApp


_CSP_DIRECTIVE_ORDER = (
    "default_src",
    "script_src",
    "style_src",
    "img_src",
    "font_src",
    "connect_src",
    "frame_src",
    "object_src",
    "base_uri",
    "form_action",
    "frame_ancestors",
    "media_src",
    "worker_src",
    "manifest_src",
)


# Tokens that defeat the point of CSP when present in script-src.
_FORBIDDEN_SCRIPT_SRC_TOKENS = frozenset({"*", "'unsafe-inline'", "'unsafe-eval'", "data:"})
# Tokens that defeat the point of CSP when present in default-src.
_FORBIDDEN_DEFAULT_SRC_TOKENS = frozenset({"*", "'unsafe-inline'", "'unsafe-eval'"})


def _validate_directive(name: str, value: tuple[str, ...]) -> None:
    """Raise ValueError when a directive carries a dangerous token.

    Catches operator misconfiguration (env-var-driven CSP that reaches
    `build_csp` with an attacker-influenced source list) at config
    construction rather than silently downgrading the policy.
    """
    if name == "script_src":
        bad = set(value) & _FORBIDDEN_SCRIPT_SRC_TOKENS
        if bad:
            msg = f"script_src must not include {sorted(bad)}"
            raise ValueError(msg)
    if name == "default_src":
        bad = set(value) & _FORBIDDEN_DEFAULT_SRC_TOKENS
        if bad:
            msg = f"default_src must not include {sorted(bad)}"
            raise ValueError(msg)


def build_csp(
    *,
    default_src: tuple[str, ...] = ("'self'",),
    script_src: tuple[str, ...] = ("'self'",),
    style_src: tuple[str, ...] = ("'self'", "'unsafe-inline'"),
    img_src: tuple[str, ...] = ("'self'", "data:"),
    font_src: tuple[str, ...] = ("'self'", "data:"),
    connect_src: tuple[str, ...] = ("'self'",),
    frame_src: tuple[str, ...] = ("'none'",),
    object_src: tuple[str, ...] = ("'none'",),
    base_uri: tuple[str, ...] = ("'self'",),
    form_action: tuple[str, ...] = ("'self'",),
    frame_ancestors: tuple[str, ...] = ("'none'",),
    media_src: tuple[str, ...] | None = None,
    worker_src: tuple[str, ...] | None = None,
    manifest_src: tuple[str, ...] | None = None,
    upgrade_insecure_requests: bool = True,
    report_uri: str | None = None,
) -> str:
    """Compose a CSP header value from per-directive source lists.

    ``style_src`` defaults allow ``'unsafe-inline'`` because component
    libraries (Swagger UI etc.) ship inline style attributes; ``script_src``
    deliberately does NOT — JS runs only from same-origin or a nonce-bound
    bundle. ``script_src`` and ``default_src`` reject dangerous tokens at
    construction time.
    """
    parts: list[str] = []
    locals_ = locals()
    for name in _CSP_DIRECTIVE_ORDER:
        value = locals_.get(name)
        if value is None:
            continue
        _validate_directive(name, value)
        directive = name.replace("_", "-")
        parts.append(f"{directive} {' '.join(value)}")
    if upgrade_insecure_requests:
        parts.append("upgrade-insecure-requests")
    if report_uri:
        parts.append(f"report-uri {report_uri}")
    return "; ".join(parts)


@dataclass(frozen=True)
class SecurityHeadersConfig:
    """Toggle individual headers.

    Defaults are tightened for an API that serves only its own SPA and
    documentation; endpoints that need looser policies should override.

    HSTS is emitted only on HTTPS responses (per RFC 6797 §8.1 browsers
    ignore HSTS over plain HTTP, but plain-HTTP emission is treated as a
    misconfiguration by audit tooling).
    """

    csp_enabled: bool = True
    csp_value: str = field(default_factory=build_csp)
    hsts_enabled: bool = True
    hsts_max_age: int = 31_536_000  # 1 year
    hsts_include_subdomains: bool = True
    hsts_preload: bool = False
    x_content_type_options: str = "nosniff"
    # X-Frame-Options retained for pre-CSP2 browsers; modern browsers
    # honor `frame-ancestors 'none'` from the CSP. Defense-in-depth.
    x_frame_options: str = "DENY"
    referrer_policy: str = "strict-origin-when-cross-origin"
    permissions_policy: str = (
        "camera=(), microphone=(), geolocation=(), payment=(), usb=(), "
        "browsing-topics=(), interest-cohort=()"
    )
    # Suppress runtime fingerprinting (uvicorn defaults to Server: uvicorn).
    suppress_server_header: bool = True

    def hsts_value(self) -> str:
        parts = [f"max-age={self.hsts_max_age}"]
        if self.hsts_include_subdomains:
            parts.append("includeSubDomains")
        if self.hsts_preload:
            parts.append("preload")
        return "; ".join(parts)


_STATIC_HEADERS: tuple[tuple[bytes, str], ...] = (
    (b"x-content-type-options", "x_content_type_options"),
    (b"x-frame-options", "x_frame_options"),
    (b"referrer-policy", "referrer_policy"),
    (b"permissions-policy", "permissions_policy"),
)


def _scheme_from_scope(scope: Any) -> str:
    """Resolve the effective request scheme, honoring X-Forwarded-Proto."""
    for raw_name, raw_value in scope.get("headers", []):
        if raw_name == b"x-forwarded-proto":
            try:
                return raw_value.decode("latin-1").split(",")[0].strip().lower()
            except UnicodeDecodeError:
                return ""
    return scope.get("scheme", "")


class SecurityHeadersMiddleware:
    """ASGI middleware that injects security response headers."""

    def __init__(self, app: ASGIApp, config: SecurityHeadersConfig) -> None:
        self.app = app
        self.config = config

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        is_https = _scheme_from_scope(scope) == "https"

        async def send_wrapper(message: Any) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                self._inject(headers, is_https=is_https)
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_wrapper)

    def _inject(
        self, headers: list[tuple[bytes, bytes]], *, is_https: bool
    ) -> None:
        existing = {name.lower() for name, _ in headers}

        def add(name: bytes, value: str) -> None:
            if name in existing:
                return
            headers.append((name, value.encode("latin-1")))
            existing.add(name)

        cfg = self.config
        for header, attr in _STATIC_HEADERS:
            value = getattr(cfg, attr)
            if value:
                add(header, value)
        if cfg.csp_enabled and cfg.csp_value:
            add(b"content-security-policy", cfg.csp_value)
        # Browsers ignore HSTS on plain HTTP; auditors flag emission as
        # misconfiguration. Gate on the effective request scheme.
        if cfg.hsts_enabled and is_https:
            add(b"strict-transport-security", cfg.hsts_value())
        if cfg.suppress_server_header:
            # Strip the upstream Server fingerprint if present.
            for i, (name, _) in enumerate(headers):
                if name.lower() == b"server":
                    headers[i] = (b"server", b"")
                    break


__all__ = [
    "SecurityHeadersConfig",
    "SecurityHeadersMiddleware",
    "build_csp",
]
