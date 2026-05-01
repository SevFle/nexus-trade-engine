from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NEXUS_", env_file=".env", extra="ignore")

    # App
    app_name: str = "nexus-trade-engine"
    app_env: str = "development"
    app_debug: bool = False
    app_host: str = "0.0.0.0"  # noqa: S104
    app_port: int = 8000
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])

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

    # Data providers
    data_providers_config: str = ""
    data_providers_default: str = "yahoo"

    # Legal / Operator
    legal_documents_dir: str = "legal"
    operator_name: str = "Nexus Trade Engine"
    operator_email: str = "legal@example.com"
    operator_url: str = "https://example.com"
    jurisdiction: str = "United States"
    platform_fee_percent: int = 30

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

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def is_test(self) -> bool:
        return self.app_env == "test"

    @property
    def enabled_providers(self) -> list[str]:
        return [p.strip() for p in self.auth_providers.split(",") if p.strip()]


settings = Settings()
