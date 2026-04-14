-- Nexus Trade Engine — Database Initialization
-- Runs automatically via docker-compose on first boot.

-- Enable TimescaleDB extension
CREATE EXTENSION IF NOT EXISTS timescaledb;

-- Create the OHLCV hypertable for time-series market data
-- (The SQLAlchemy model creates the base table; this converts it)
-- Run AFTER alembic migrations:
-- SELECT create_hypertable('ohlcv_bars', 'timestamp', if_not_exists => TRUE);

-- Create indexes for common query patterns
-- These supplement the SQLAlchemy-defined indexes

-- Performance: portfolio value history (for equity curves)
CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    id BIGSERIAL,
    portfolio_id INTEGER NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    total_value DOUBLE PRECISION NOT NULL,
    cash DOUBLE PRECISION NOT NULL,
    unrealized_pnl DOUBLE PRECISION NOT NULL DEFAULT 0,
    realized_pnl DOUBLE PRECISION NOT NULL DEFAULT 0,
    num_positions INTEGER NOT NULL DEFAULT 0
);

-- Convert to hypertable for efficient time-series queries
SELECT create_hypertable('portfolio_snapshots', 'timestamp', if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS ix_portfolio_snapshots_pid
    ON portfolio_snapshots (portfolio_id, timestamp DESC);

-- Strategy evaluation log (for audit trail)
CREATE TABLE IF NOT EXISTS evaluation_log (
    id BIGSERIAL,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    strategy_id VARCHAR(100) NOT NULL,
    portfolio_id INTEGER NOT NULL,
    signals_emitted INTEGER NOT NULL DEFAULT 0,
    evaluation_ms DOUBLE PRECISION NOT NULL DEFAULT 0,
    market_snapshot JSONB,
    signals JSONB
);

SELECT create_hypertable('evaluation_log', 'timestamp', if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS ix_eval_log_strategy
    ON evaluation_log (strategy_id, timestamp DESC);
