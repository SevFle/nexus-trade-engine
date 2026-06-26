"""Regression tests for issue #735 — validate auth/backtest fixes hold.

Locks in the two fixes gated by the umbrella validation ticket (#727/#735):

* #731 — BacktestRunner no longer raises ``TypeError`` when computing realized
  PnL. The sell path guards a ``None`` average cost and a ``None`` cost
  breakdown, subtracts costs, and records a finite numeric ``realized_pnl``.
  These tests reconstruct the expected PnL from the emitted trade records so a
  regression to the old ``TypeError``/wrong-formula behaviour fails loudly.

* #732 — Every OAuth provider surfaces "Account is disabled" for inactive
  users. OIDC, LDAP and local already had coverage; these tests close the gap
  for the Google and GitHub providers, which previously had no unit test for
  the disabled-user branch.

The full suite is exercised by ``make test``; these tests exist to prevent the
specific regressions from silently returning.
"""

from __future__ import annotations

import math
from typing import Any, ClassVar
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pandas as pd
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from engine.api.auth.github_oauth import GitHubAuthProvider
from engine.api.auth.google import GoogleAuthProvider
from engine.config import Settings
from engine.core.backtest_runner import BacktestConfig, BacktestRunner
from engine.core.signal import Signal
from engine.db.models import User

# ---------------------------------------------------------------------------
# Helpers shared by the OAuth provider tests
# ---------------------------------------------------------------------------


class _FakeResponse:
    """Minimal stand-in for an ``httpx.Response``."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeAsyncClient:
    """Async-context-manager fake for ``httpx.AsyncClient``.

    The Google/GitHub providers do ``POST`` (token exchange) then ``GET``
    (userinfo); this fake returns deterministic payloads for each.
    """

    def __init__(self, token_payload: dict[str, Any], profile_payload: dict[str, Any]) -> None:
        self._token = token_payload
        self._profile = profile_payload

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(self, *_args: object) -> bool:
        return False

    async def post(self, _url: str, **_kwargs: Any) -> _FakeResponse:
        return _FakeResponse(self._token)

    async def get(self, _url: str, **_kwargs: Any) -> _FakeResponse:
        return _FakeResponse(self._profile)


@pytest.fixture
def google_settings(monkeypatch) -> Settings:
    s = Settings(
        google_client_id="google-client-id",
        google_client_secret="google-client-secret",
        google_redirect_uri="https://app.example.com/google/callback",
        github_client_id="github-client-id",
        github_client_secret="github-client-secret",
        github_redirect_uri="https://app.example.com/github/callback",
    )
    monkeypatch.setattr("engine.api.auth.google.settings", s)
    monkeypatch.setattr("engine.api.auth.github_oauth.settings", s)
    return s


def _mock_db_returning(user: User | None) -> AsyncMock:
    """Build an ``AsyncSession`` mock whose first ``execute`` returns ``user``."""
    mock_db = AsyncMock(spec=AsyncSession)
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = user
    mock_db.execute.return_value = mock_result
    return mock_db


# ---------------------------------------------------------------------------
# #732 — Google OAuth: disabled vs. active user
# ---------------------------------------------------------------------------


class TestGoogleOAuthDisabledUser:
    GOOGLE_PROFILE: ClassVar[dict[str, Any]] = {
        "sub": "google-disabled-1",
        "email": "disabled@example.com",
        "name": "Disabled User",
    }

    async def test_disabled_user_rejected(self, google_settings: Settings) -> None:
        disabled_user = User(
            email="disabled@example.com",
            display_name="Disabled User",
            is_active=False,
            role="user",
            auth_provider="google",
            external_id="google-disabled-1",
        )
        fake_client = _FakeAsyncClient(
            token_payload={"access_token": "token-abc"},
            profile_payload=self.GOOGLE_PROFILE,
        )
        mock_db = _mock_db_returning(disabled_user)

        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await GoogleAuthProvider().authenticate(code="auth-code", db=mock_db)

        assert result.success is False
        assert result.error == "Account is disabled"
        # The provider short-circuits before creating/refreshing the user.
        assert not mock_db.add.called

    async def test_active_user_authenticates(self, google_settings: Settings) -> None:
        active_user = User(
            email="disabled@example.com",
            display_name="Active User",
            is_active=True,
            role="user",
            auth_provider="google",
            external_id="google-disabled-1",
        )
        fake_client = _FakeAsyncClient(
            token_payload={"access_token": "token-abc"},
            profile_payload=self.GOOGLE_PROFILE,
        )
        mock_db = _mock_db_returning(active_user)

        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await GoogleAuthProvider().authenticate(code="auth-code", db=mock_db)

        assert result.success is True
        assert result.user_info is not None
        assert result.user_info.provider == "google"


# ---------------------------------------------------------------------------
# #732 — GitHub OAuth: disabled vs. active user
# ---------------------------------------------------------------------------


class TestGitHubOAuthDisabledUser:
    GITHUB_PROFILE: ClassVar[dict[str, Any]] = {
        "id": 12345,
        "login": "disabled",
        "email": "disabled@github",
        "name": "Disabled GH",
    }

    async def test_disabled_user_rejected(self, google_settings: Settings) -> None:
        disabled_user = User(
            email="disabled@github",
            display_name="Disabled GH",
            is_active=False,
            role="user",
            auth_provider="github",
            external_id="12345",
        )
        fake_client = _FakeAsyncClient(
            token_payload={"access_token": "token-abc"},
            profile_payload=self.GITHUB_PROFILE,
        )
        mock_db = _mock_db_returning(disabled_user)

        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await GitHubAuthProvider().authenticate(code="auth-code", db=mock_db)

        assert result.success is False
        assert result.error == "Account is disabled"
        assert not mock_db.add.called

    async def test_active_user_authenticates(self, google_settings: Settings) -> None:
        active_user = User(
            email="disabled@github",
            display_name="Disabled GH",
            is_active=True,
            role="user",
            auth_provider="github",
            external_id="12345",
        )
        fake_client = _FakeAsyncClient(
            token_payload={"access_token": "token-abc"},
            profile_payload=self.GITHUB_PROFILE,
        )
        mock_db = _mock_db_returning(active_user)

        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await GitHubAuthProvider().authenticate(code="auth-code", db=mock_db)

        assert result.success is True
        assert result.user_info is not None
        assert result.user_info.provider == "github"


# ---------------------------------------------------------------------------
# #731 — BacktestRunner realized PnL (no TypeError, correct formula)
# ---------------------------------------------------------------------------


def _flat_price_df(n_days: int = 80, price: float = 100.0) -> pd.DataFrame:
    """OHLCV frame with a constant close so buy/sell fills are deterministic."""
    rng = np.random.default_rng(0)
    start = pd.Timestamp("2024-01-01")
    idx = pd.DatetimeIndex([start + pd.Timedelta(days=i) for i in range(n_days)], name="timestamp")
    return pd.DataFrame(
        {
            "open": np.full(n_days, price),
            "high": np.full(n_days, price * 1.01),
            "low": np.full(n_days, price * 0.99),
            "close": np.full(n_days, price),
            "volume": rng.integers(500_000, 5_000_000, n_days),
        },
        index=idx,
    )


class _FlatProvider:
    def __init__(self, df: pd.DataFrame) -> None:
        self._df = df

    async def get_latest_price(self, symbol: str) -> float | None:
        if self._df.empty:
            return None
        return float(self._df["close"].iloc[-1])

    async def get_ohlcv(
        self, symbol: str, period: str = "1y", interval: str = "1d"
    ) -> pd.DataFrame:
        return self._df

    async def get_multiple_prices(self, symbols: list[str]) -> dict[str, float]:
        if self._df.empty:
            return {}
        return {symbols[0]: float(self._df["close"].iloc[-1])}


class _BuyThenSellStrategy:
    """Buy 10 shares on the first bar, sell 10 on the next, then hold."""

    name = "buy_then_sell"
    version = "1.0.0"

    def __init__(self) -> None:
        self._bought = False
        self._sold = False

    def on_bar(self, state: Any, portfolio: Any) -> list[Signal]:
        if not self._bought:
            self._bought = True
            return [Signal.buy(symbol="AAPL", strategy_id=self.name, quantity=10)]
        if not self._sold:
            self._sold = True
            return [Signal.sell(symbol="AAPL", strategy_id=self.name, quantity=10)]
        return []


class TestBacktestRealizedPnl:
    @pytest.fixture
    def runner(self) -> BacktestRunner:
        config = BacktestConfig(
            strategy_name="buy_then_sell",
            symbol="AAPL",
            start_date="2024-01-01",
            end_date="2024-12-31",
        )
        return BacktestRunner(
            config=config,
            strategy=_BuyThenSellStrategy(),
            provider=_FlatProvider(_flat_price_df()),
        )

    async def test_sell_realized_pnl_is_finite_float(self, runner: BacktestRunner) -> None:
        """Regression for #731: computing PnL must not raise TypeError."""
        result = await runner.run()

        sell_trades = [t for t in result.trades if t["side"] == "sell"]
        assert sell_trades, "expected at least one sell trade"
        pnl = sell_trades[0]["realized_pnl"]
        # The pre-fix bug raised TypeError before reaching here, or stored None.
        assert isinstance(pnl, float)
        assert math.isfinite(pnl)

    async def test_sell_realized_pnl_matches_formula(self, runner: BacktestRunner) -> None:
        """realized_pnl == (sell_fill - avg_cost) * qty - sell_costs.

        ``avg_cost`` at sell time equals the single buy fill price (no prior
        sells ⇒ no wash-sale basis adjustment), so it can be reconstructed
        purely from the emitted trade records.
        """
        result = await runner.run()

        buy_trade = next(t for t in result.trades if t["side"] == "buy")
        sell_trade = next(t for t in result.trades if t["side"] == "sell")

        buy_fill = buy_trade["fill_price"]
        sell_fill = sell_trade["fill_price"]
        qty = sell_trade["quantity"]
        sell_costs = (sell_trade.get("cost_breakdown") or {}).get("total", 0.0)

        expected = (sell_fill - buy_fill) * qty - sell_costs
        assert sell_trade["realized_pnl"] == pytest.approx(expected)

    async def test_buy_realized_pnl_is_zero(self, runner: BacktestRunner) -> None:
        """Buys never realize PnL — the default branch of the #731 fix."""
        result = await runner.run()
        buy_trade = next(t for t in result.trades if t["side"] == "buy")
        assert buy_trade["realized_pnl"] == 0.0
