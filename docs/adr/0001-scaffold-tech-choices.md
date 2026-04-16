# ADR-0001: Scaffold Technology Choices

**Status:** Accepted
**Date:** 2026-04-15

## Context

We need a high-performance backtesting and trading engine with async I/O, time-series storage, background task processing, and a plugin system for user strategies.

## Decision

- **Python 3.12 + uv**: Latest stable Python with fastest package manager. uv provides deterministic lockfiles and fast installs.
- **FastAPI + Pydantic 2.x**: Async-first web framework with native Pydantic validation. OpenAPI docs out of the box.
- **SQLAlchemy 2.0 async + asyncpg**: Modern declarative ORM with native async support. asyncpg is the fastest PostgreSQL driver for Python.
- **TimescaleDB (PG16)**: PostgreSQL extension optimized for time-series data (OHLCV bars). Hypertables, compression, and continuous aggregates.
- **Alembic**: Industry-standard migration tool, configured for async engine.
- **TaskIQ + Valkey**: Async task queue backed by Valkey (Redis fork). Decouples long-running backtests from request cycle.
- **Polars**: Columnar DataFrame library for fast numerical computation in metrics and backtest loops.
- **structlog + OpenTelemetry + Sentry**: Structured logging (JSON in prod), distributed tracing (OTLP), and error tracking.
- **Ruff + basedpyright**: Fast linter/formatter and strict type checker.
- **Docker multi-stage (distroless)**: Minimal attack surface, small image size.
- **GitHub Actions with astral-sh/setup-uv**: CI with lint/typecheck/test parallel matrix.

## Consequences

- All async: no blocking I/O in the request path.
- Plugin system via filesystem discovery: strategies are isolated Python packages.
- TimescaleDB requires PostgreSQL — no SQLite for local dev (use docker compose).
- Polars chosen over pandas for performance; team needs to learn Polars API.
