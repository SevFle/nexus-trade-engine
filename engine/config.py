from __future__ import annotations

import json

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from engine.api.cors import normalize_origin_allowlist


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NEXUS_", env_file=".env", extra="ignore")

    # App
    app_name: str = "nexus-trade-engine"
    app_env: str = "development"
    app_debug: bool = False
    app_host: str = "0.0.0.0"  # noqa: S104
    app_port: int = 8000
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])

    @field_validator("cors_origins", mode="after")
    @classmethod
    def _normalize_cors_origins(cls, value: list[str]) -> list[str]:
        """Pre-normalise the CORS allowlist at config load.

        Each entry is canonicalised via
        :func:`~engine.api.cors.normalize_origin` (scheme + host lower-cased,
        trailing slash / path stripped, duplicates removed) so that the
        Starlette ``CORSMiddleware`` — which compares the browser ``Origin``
        header by exact equality — receives a canonical list.  This closes
        the trailing-slash / upper-case-scheme / mixed-case-host bypass and
        fail-closed-misconfiguration vectors at the source rather than at
        every match site.
        """
        return normalize_origin_allowlist(value)

    # Database
    database_url: str = "postgresql+asyncpg://nexus:nexus@localhost:5432/nexus"
    database_pool_size: int = 5
    database_max_overflow: int = 10

    # Valkey
    valkey_url: str = "valkey://localhost:6379/0"

    # Observability
    log_level: str = "INFO"
    log_format: str = "console"
    otlp_endpoint: str = ""
    sentry_dsn: str = ""
    sentry_traces_sample_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    app_version: str = "0.1.0"
    log_sampling_info: float = 1.0
    log_sampling_debug: float = 0.01
    log_sink: str = "stdout"  # stdout | file | otlp
    log_file_path: str = "logs/engine.log"

    # Worker
    worker_concurrency: int = 4

    # Rate limit (global default; per-route overrides live in code)
    rate_limit_per_minute: int = 600
    rate_limit_burst: int = 60
    rate_limit_exempt_paths: str = "/health,/metrics"
    # When True, the API uses a Valkey-backed token-bucket backend so
    # the configured limits are enforced globally across every pod
    # sharing the same Valkey. When False (default), each pod keeps a
    # private in-memory bucket — fine for single-pod deployments and
    # tests, but the effective limit becomes ``per_minute * pod_count``.
    rate_limit_valkey_enabled: bool = False
    # Per-role rate-limit overrides. JSON-encoded mapping
    # ``{role: [per_minute, burst]}``. Applied when the request can be
    # authenticated inline by the middleware (i.e. a Bearer JWT is
    # present). Unknown roles fall back to the default tier.
    #
    # Example: ``NEXUS_RATE_LIMIT_ROLE_TIERS='{"viewer":[120,30],"admin":[6000,200]}'``
    rate_limit_role_tiers: str = ""
    # TTL (seconds) for per-key state stored in Valkey. After this many
    # seconds of inactivity the key is reaped, bounding memory growth
    # on the distributed backend even under a hostile key-space.
    rate_limit_valkey_key_ttl_sec: int = 3600

    # Data providers
    data_providers_config: str = ""
    data_providers_default: str = "yahoo"

    # Legal / Operator
    legal_documents_dir: str = "legal"
    # Current version of the legal document (e.g. terms of service) that
    # users must accept. Bump this to force re-acceptance on next request.
    # Used by the self-contained acceptance dependency in engine.api.legal.
    legal_terms_version: str = "1.0.0"
    # Legal scoring gate. Comma-separated strategy ids that are under a
    # compliance hold: their surfaced scores are suppressed (dropped) by
    # :class:`engine.legal.scoring_gate.LegalScoreValidator` before any
    # marketplace / backtest / scoring surface exposes them. Empty (default)
    # means no strategy is flagged.
    legal_score_flagged_strategies: str = ""
    # Hard legal cap applied to a strategy's composite score (0-100). Any
    # score above this ceiling is clamped down before exposure. Defaults to
    # 100.0 (the technical max) so the cap is a no-op unless an operator
    # configures a tighter compliance ceiling.
    legal_score_max_composite: float = 100.0
    operator_name: str = "Nexus Trade Engine"
    operator_email: str = "legal@example.com"
    operator_url: str = "https://example.com"
    jurisdiction: str = "United States"
    platform_fee_percent: int = 30
    # Comma-separated list of trusted reverse-proxy addresses / CIDR ranges
    # (e.g. "10.0.0.0/8,127.0.0.1"). Used by client-IP resolution to decide
    # whether to trust the X-Forwarded-For header. Empty (default) means no
    # peer is trusted and the raw TCP peer is always used as the client IP.
    trusted_proxies: str = ""

    # Auth
    secret_key: str = ""
    secret_key_previous: str = ""
    jwt_access_token_expire_minutes: int = 60
    jwt_refresh_token_expire_days: int = 7
    auth_providers: str = "local"
    auth_local_allow_registration: bool = True

    # MFA — Fernet key (url-safe base64, 32 bytes decoded) used to
    # encrypt TOTP secrets at rest. Empty disables MFA enrollment.
    mfa_encryption_key: str = ""
    mfa_challenge_ttl_seconds: int = 300
    mfa_backup_codes_count: int = 10

    # Google OAuth2
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = ""

    # GitHub OAuth2
    github_client_id: str = ""
    github_client_secret: str = ""
    github_redirect_uri: str = ""

    # OIDC
    oidc_discovery_url: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    oidc_redirect_uri: str = ""
    oidc_role_claim: str = "roles"

    # LDAP
    ldap_server_url: str = ""
    ldap_bind_dn: str = ""
    ldap_bind_password: str = ""
    ldap_search_base: str = ""
    ldap_role_mapping: str = "{}"

    ws_max_connections: int = 5000
    ws_auth_timeout_seconds: float = 5.0
    ws_heartbeat_interval_seconds: float = 30.0
    ws_idle_timeout_seconds: float = 300.0
    ws_send_queue_size: int = 256
    ws_max_subscriptions_per_connection: int = 50
    ws_event_bridge_concurrency: int = 32
    ws_auth_rate_limit_per_minute: int = 10

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def is_test(self) -> bool:
        return self.app_env == "test"

    @property
    def enabled_providers(self) -> list[str]:
        return [p.strip() for p in self.auth_providers.split(",") if p.strip()]

    @property
    def trusted_proxies_set(self) -> set[str]:
        """Parse ``trusted_proxies`` into a de-duplicated set of entries.

        Blank entries are dropped so the empty default yields an empty set
        (=> no peer is trusted, X-Forwarded-For is never consulted). Each
        entry may be a bare IP or a CIDR range; parsing/containment is done
        downstream in :mod:`engine.api.ip_utils`.
        """
        return {p.strip() for p in self.trusted_proxies.split(",") if p.strip()}

    @property
    def rate_limit_role_tiers_map(self) -> dict[str, tuple[int, int]]:
        """Parse ``rate_limit_role_tiers`` into a typed mapping.

        Format: ``{role: [per_minute, burst]}``. Malformed entries are
        silently skipped so a bad operator env var cannot prevent the
        process from starting — instead, the affected role falls back
        to the default tier.
        """
        raw = (self.rate_limit_role_tiers or "").strip()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {}
        if not isinstance(parsed, dict):
            return {}
        out: dict[str, tuple[int, int]] = {}
        # Expected shape: [per_minute, burst].
        _expected_limits_len = 2
        for role, limits in parsed.items():
            if not isinstance(role, str) or not isinstance(limits, (list, tuple)):
                continue
            if len(limits) != _expected_limits_len:
                continue
            try:
                per_min = int(limits[0])
                burst = int(limits[1])
            except (TypeError, ValueError):
                continue
            if per_min <= 0 or burst <= 0:
                continue
            out[role] = (per_min, burst)
        return out


settings = Settings()
