"""Market data REST endpoints.

Surface for the pluggable provider system (engine.data.providers): given a
symbol the registry resolves an adapter (Yahoo, Polygon, Alpaca, Binance,
CoinGecko, OANDA, ...), fetches OHLCV bars or a quote, and returns it
normalised as JSON.

The asset class is inferred from symbol shape and can be overridden via the
``asset_class`` query param. The provider can also be pinned via ``provider``
to bypass registry routing — useful for parity testing across adapters.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from engine.api.auth.dependency import get_current_user
from engine.data.providers import (
    AssetClass,
    FatalProviderError,
    NoProviderAvailableError,
    ProviderError,
    TransientProviderError,
    get_registry,
)
from engine.data.providers.base import SYMBOL_PATTERN
from engine.data.providers.registry import CapabilityNotSupportedError
from engine.db.models import User

if TYPE_CHECKING:
    import pandas as pd

logger = structlog.get_logger()

router = APIRouter()

_SYMBOL_RE = re.compile(SYMBOL_PATTERN)

# Heuristics for asset-class detection based on the symbol shape we see in
# the wild. Order matters — first match wins.
_FOREX_SUFFIX = re.compile(r"=X$", re.IGNORECASE)
_FOREX_PAIR = re.compile(r"^[A-Z]{3}/[A-Z]{3}$")
_CRYPTO_QUOTES = ("USD", "USDT", "USDC", "BUSD", "BTC", "ETH", "EUR", "GBP")


def detect_asset_class(symbol: str) -> AssetClass:
    """Best-effort asset class inference from the symbol string alone.

    Used only when the caller hasn't pinned ``asset_class`` via query param.
    Conservative: equities are the default since they cover the long tail.
    """
    upper = symbol.upper()
    if _FOREX_SUFFIX.search(upper) or _FOREX_PAIR.match(upper):
        return AssetClass.FOREX
    if "-" in upper or "/" in upper:
        # Treat hyphen/slash pairs as crypto when the quote currency looks
        # like a crypto/stable. Otherwise leave as equity (e.g. BRK-B).
        sep = "-" if "-" in upper else "/"
        parts = upper.split(sep)
        if len(parts) == 2 and parts[1] in _CRYPTO_QUOTES:
            return AssetClass.CRYPTO
    return AssetClass.EQUITY


def _validate_symbol(symbol: str) -> str:
    if not _SYMBOL_RE.match(symbol):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid symbol format",
        )
    return symbol


def _resolve_asset_class(value: str | None, symbol: str) -> AssetClass:
    if not value:
        return detect_asset_class(symbol)
    try:
        return AssetClass(value.lower())
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown asset_class: {value}",
        ) from None


class Bar(BaseModel):
    timestamp: str = Field(..., description="ISO-8601 UTC timestamp")
    open: float
    high: float
    low: float
    close: float
    volume: float


class BarsResponse(BaseModel):
    symbol: str
    interval: str
    period: str
    asset_class: str
    provider: str
    bars: list[Bar]


class QuoteResponse(BaseModel):
    symbol: str
    asset_class: str
    provider: str
    price: float


def _df_to_bars(df: pd.DataFrame) -> list[Bar]:
    if df is None or df.empty:
        return []
    out: list[Bar] = []
    # Provider contract: index is ascending UTC DatetimeIndex with the
    # canonical lowercase OHLCV columns.
    for ts, row in df.iterrows():
        out.append(
            Bar(
                timestamp=ts.isoformat(),
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row["volume"]),
            )
        )
    return out


@router.get("/{symbol}/bars", response_model=BarsResponse)
async def get_bars(
    symbol: str,
    interval: str = Query("1d", min_length=1, max_length=8),
    period: str = Query("1y", min_length=1, max_length=8),
    provider: str | None = Query(None, max_length=32),
    asset_class: str | None = Query(None, max_length=16),
    _: User = Depends(get_current_user),
) -> BarsResponse:
    symbol = _validate_symbol(symbol)
    resolved_class = _resolve_asset_class(asset_class, symbol)
    registry = get_registry()

    if provider:
        adapter = registry.get(provider)
        if adapter is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Provider not registered: {provider}",
            )
        try:
            df = await adapter.get_ohlcv(symbol, period=period, interval=interval)
        except FatalProviderError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            ) from None
        except (TransientProviderError, TimeoutError) as exc:
            logger.warning(
                "market_data.provider_transient",
                provider=provider,
                symbol=symbol,
                error=type(exc).__name__,
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Upstream provider unavailable",
            ) from None
        provider_name = provider
    else:
        try:
            df = await registry.get_ohlcv(
                symbol,
                period=period,
                interval=interval,
                asset_class=resolved_class,
            )
        except CapabilityNotSupportedError as exc:
            raise HTTPException(
                status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=str(exc)
            ) from None
        except NoProviderAvailableError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
            ) from None
        except FatalProviderError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            ) from None
        provider_name = _last_provider_for(registry, resolved_class)

    return BarsResponse(
        symbol=symbol,
        interval=interval,
        period=period,
        asset_class=resolved_class.value,
        provider=provider_name,
        bars=_df_to_bars(df),
    )


@router.get("/{symbol}/quote", response_model=QuoteResponse)
async def get_quote(
    symbol: str,
    provider: str | None = Query(None, max_length=32),
    asset_class: str | None = Query(None, max_length=16),
    _: User = Depends(get_current_user),
) -> QuoteResponse:
    symbol = _validate_symbol(symbol)
    resolved_class = _resolve_asset_class(asset_class, symbol)
    registry = get_registry()

    price: float | None
    if provider:
        adapter = registry.get(provider)
        if adapter is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Provider not registered: {provider}",
            )
        try:
            price = await adapter.get_latest_price(symbol)
        except ProviderError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)
            ) from None
        provider_name = provider
    else:
        price = await registry.get_latest_price(symbol, asset_class=resolved_class)
        provider_name = _last_provider_for(registry, resolved_class)

    if price is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No price available for {symbol}",
        )

    return QuoteResponse(
        symbol=symbol,
        asset_class=resolved_class.value,
        provider=provider_name,
        price=float(price),
    )


def _last_provider_for(registry, asset_class: AssetClass) -> str:
    """Best-effort name of the registry's first candidate for an asset class.

    The registry doesn't expose which provider actually served a request, but
    surfacing the *intended* primary in the response header is still useful
    for the client. If nothing matches, returns an empty string.
    """
    candidates = registry.candidates_for(asset_class)
    return candidates[0].name if candidates else ""
