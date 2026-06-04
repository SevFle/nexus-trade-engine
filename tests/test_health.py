from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from httpx import AsyncClient


@pytest.mark.asyncio
async def test_health_returns_ok(client: AsyncClient) -> None:
    response = await client.get("/health")
    assert response.status_code == HTTPStatus.OK
    data = response.json()
    assert data["status"] == "ok"


@pytest.mark.asyncio
async def test_backtest_run_stub_returns_accepted(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/backtest/run",
        json={
            "strategy_name": "mean_reversion_basic",
            "symbol": "AAPL",
            "start_date": "2024-01-01",
            "end_date": "2024-12-31",
        },
    )
    assert response.status_code == HTTPStatus.OK
    data = response.json()
    assert data["status"] == "accepted"


@pytest.mark.asyncio
async def test_legal_acceptance_dependency_is_overridden_in_tests(
    client: AsyncClient,
) -> None:
    """Routes guarded by ``require_legal_acceptance`` must remain reachable
    in tests even when no ``legal_documents`` table exists — the dependency
    is overridden in ``tests/conftest.py``. This is a regression guard for
    the SEV-501 follow-up where CI was hitting ``UndefinedTableError``."""
    from engine.api.router import api_router

    guarded_paths = ["/api/v1/backtest/run", "/api/v1/scoring/strategies"]
    for path in guarded_paths:
        # The fact that the request gets past the dependency (regardless of
        # the eventual response status) proves the override is wired. We
        # only assert the response is not a 451 (legal re-acceptance) or a
        # 500 from a missing-table query.
        response = await client.post(
            path,
            json={"strategy_name": "x", "symbol": "AAPL", "start_date": "2024-01-01", "end_date": "2024-12-31"},
        )
        assert response.status_code != 451
        assert response.status_code != 500
    # Confirm the dependency is registered against the router somewhere.
    dep_targets = [
        getattr(d, "dependency", None)
        for r in api_router.routes
        for d in getattr(r, "dependants", []) or []
    ]
    from engine.legal.dependencies import require_legal_acceptance

    assert require_legal_acceptance in dep_targets or any(
        getattr(d, "dependency", None) is require_legal_acceptance
        for r in api_router.routes
        for d in getattr(r, "dependencies", []) or []
    )
