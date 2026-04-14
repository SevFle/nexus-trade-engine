"""
Application configuration loaded from environment variables.
"""

from functools import lru_cache
from pydantic_settings import BaseSettings
from pydantic import Field


class Settings(BaseSettings):
    """Global application settings. All values can be overridden via env vars."""

    # ── App ──
    app_name: str = "Nexus Trade Engine"
    environment: str = Field(default="development", description="development | staging | production")
    log_level: str = "INFO"
    debug: bool = False

    # ── Database ──
    database_url: str = "postgresql+asyncpg://nexus:nexus_dev_password@localhost:5432/nexus_trade"
    db_pool_size: int = 20
    db_max_overflow: int = 10

    # ── Redis ──
    redis_url: str = "redis://localhost:6379/0"

    # ── Auth ──
    secret_key: str = "CHANGE-ME-IN-PRODUCTION-use-openssl-rand-hex-32"
    access_token_expire_minutes: int = 60
    algorithm: str = "HS256"

    # ── Trading Engine ──
    default_execution_mode: str = "paper"  # backtest | paper | live
    max_open_positions: int = 50
    max_portfolio_risk_pct: float = 0.25
    circuit_breaker_drawdown_pct: float = 0.10

    # ── Cost Model Defaults ──
    default_commission_per_trade: float = 0.0  # USD (many brokers are zero)
    default_spread_bps: float = 5.0  # basis points
    default_slippage_bps: float = 10.0  # basis points
    short_term_tax_rate: float = 0.37  # US federal max
    long_term_tax_rate: float = 0.20
    long_term_holding_days: int = 365
    enable_wash_sale_detection: bool = True

    # ── Plugin System ──
    plugin_dir: str = "./strategies"
    plugin_max_memory_mb: int = 2048
    plugin_max_cpu_seconds: int = 30
    plugin_sandbox_enabled: bool = True

    # ── Market Data ──
    market_data_provider: str = "yahoo"  # yahoo | alpaca | polygon | custom
    market_data_cache_ttl_seconds: int = 60

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


@lru_cache()
def get_settings() -> Settings:
    return Settings()
