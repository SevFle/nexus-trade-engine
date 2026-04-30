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


def build_csp(
    *,
    default_src: tuple[str, ...] = ("'self'",),
    script_src: tuple[str, ...] = ("'self'",),
    style_src: tuple[str, ...] = ("'self'", "'unsafe-inline'"),
    img_src: tuple[str, ...] = ("'self'", "data:", "https:"),
    font_src: tuple[str, ...] = ("'self'", "https:", "data:"),
    connect_src: tuple[str, ...] = ("'self'",),
    frame_src: tuple[str, ...] = ("'none'",),
    object_src: tuple[str, ...] = ("'none'",),
    base_uri: tuple[str, ...] = ("'self'",),
    form_action: tuple[str, ...] = ("'self'",),
    frame_ancestors: tuple[str, ...] = ("'none'",),
    media_src: tuple[str, ...] | None = None,
    worker_src: tuple[str, ...] | None = None,
    manifest_src: tuple[str, ...] | None = None,
) -> str:
    """Compose a CSP header value from per-directive source lists.

    ``style_src`` defaults allow ``'unsafe-inline'`` because most
    component libraries (FastAPI's Swagger UI included) ship inline
    style attributes; ``script_src`` deliberately does NOT — JS should
    run only from the same origin or a nonce-bound bundle.
    """
    parts: list[str] = []
    locals_ = locals()
    for name in _CSP_DIRECTIVE_ORDER:
        value = locals_.get(name)
        if value is None:
            continue
        directive = name.replace("_", "-")
        parts.append(f"{directive} {' '.join(value)}")
    return "; ".join(parts)


@dataclass(frozen=True)
class SecurityHeadersConfig:
    """Toggle individual headers.

    Defaults are tightened for an API that serves only its own SPA and
    documentation; endpoints that need looser policies should override.
    """

    csp_enabled: bool = True
    csp_value: str = field(default_factory=build_csp)
    hsts_enabled: bool = True
    hsts_max_age: int = 31_536_000  # 1 year
    hsts_include_subdomains: bool = True
    hsts_preload: bool = False
    x_content_type_options: str = "nosniff"
    x_frame_options: str = "DENY"
    referrer_policy: str = "strict-origin-when-cross-origin"
    permissions_policy: str = (
        "camera=(), microphone=(), geolocation=(), payment=(), usb=(), "
        "interest-cohort=()"
    )

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


class SecurityHeadersMiddleware:
    """ASGI middleware that injects security response headers."""

    def __init__(self, app: ASGIApp, config: SecurityHeadersConfig) -> None:
        self.app = app
        self.config = config

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message: Any) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                self._inject(headers)
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_wrapper)

    def _inject(self, headers: list[tuple[bytes, bytes]]) -> None:
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
        if cfg.hsts_enabled:
            add(b"strict-transport-security", cfg.hsts_value())


__all__ = [
    "SecurityHeadersConfig",
    "SecurityHeadersMiddleware",
    "build_csp",
]
