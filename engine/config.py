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

    # Worker
    worker_concurrency: int = 4

    # Legal / Operator
    legal_documents_dir: str = "legal"
    operator_name: str = "Nexus Trade Engine"
    operator_email: str = "legal@example.com"
    operator_url: str = "https://example.com"
    jurisdiction: str = "United States"
    platform_fee_percent: int = 30

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def is_test(self) -> bool:
        return self.app_env == "test"


settings = Settings()
