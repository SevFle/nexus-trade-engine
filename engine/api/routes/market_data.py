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

import math
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
from engine.data.providers.registry import (
    CapabilityNotSupportedError,
    DataProviderRegistry,
)
from engine.db.models import User

if TYPE_CHECKING:
    import pandas as pd

logger = structlog.get_logger()

router = APIRouter()

_SYMBOL_RE = re.compile(SYMBOL_PATTERN)

# Heuristics for asset-class detection based on the symbol shape we see in
# the wild. Order matters — first match wins.
_FOREX_SUFFIX = re.compile(r"=X$", re.IGNORECASE)
# Conservative fiat allowlist for the slash-pair forex form. Anything outside
# this set with a slash is treated as crypto when the quote currency matches
# _CRYPTO_QUOTES — keeps `BTC/USD` and `ETH/USDT` out of the forex bucket.
_FIAT_CCY = frozenset({"USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD"})
_CRYPTO_QUOTES = frozenset({"USD", "USDT", "USDC", "BUSD", "BTC", "ETH", "EUR", "GBP"})
_CRYPTO_DASH_QUOTES = _CRYPTO_QUOTES  # dash form: BTC-USD, ETH-USDT


def detect_asset_class(symbol: str) -> AssetClass:
    """Best-effort asset class inference from the symbol string alone.

    Used only when the caller hasn't pinned ``asset_class`` via query param.
    Conservative: equities are the default since they cover the long tail.
    Crypto is checked before forex because the fiat/crypto quote sets
    overlap (``BTC/USD`` would otherwise misclassify as forex).
    """
    upper = symbol.upper()

    # Yahoo-style forex first — unambiguous shape.
    if _FOREX_SUFFIX.search(upper):
        return AssetClass.FOREX

    if "-" in upper:
        parts = upper.split("-", 1)
        if len(parts) == 2 and parts[1] in _CRYPTO_DASH_QUOTES:
            return AssetClass.CRYPTO
        # Fall through to equity for things like BRK-B.
        return AssetClass.EQUITY

    if "/" in upper:
        parts = upper.split("/", 1)
        if len(parts) == 2:
            base, quote = parts
            if quote in _CRYPTO_QUOTES and base not in _FIAT_CCY:
                return AssetClass.CRYPTO
            if base in _FIAT_CCY and quote in _FIAT_CCY:
                return AssetClass.FOREX

    return AssetClass.EQUITY


def _validate_symbol(symbol: str) -> str:
    """Normalise + validate a symbol from a path parameter.

    Uses ``fullmatch`` (not ``match``) so trailing newlines or stray bytes
    can't satisfy the trailing ``$`` anchor and slip an unsanitised value
    into logs or response echoes. Mirrors the provider-layer ``..`` reject.
    """
    cleaned = symbol.strip()
    if not cleaned or ".." in cleaned or not _SYMBOL_RE.fullmatch(cleaned):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid symbol format",
        )
    return cleaned


def _resolve_asset_class(value: str | None, symbol: str) -> AssetClass:
    if not value:
        return detect_asset_class(symbol)
    try:
        return AssetClass(value.strip().lower())
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown asset_class: {value!r}",
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


def _safe_float(value: object) -> float:
    """Coerce a provider value to a finite float or raise.

    Real provider feeds emit NaN for low-volume bars or `null` for delisted
    fields; serialising NaN produces invalid JSON, so callers skip rows
    where any field can't be made finite.
    """
    if value is None:
        raise ValueError("None is not a finite float")
    f = float(value)
    if not math.isfinite(f):
        raise ValueError("non-finite float")
    return f


def _df_to_bars(df: pd.DataFrame) -> list[Bar]:
    if df is None or df.empty:
        return []
    out: list[Bar] = []
    for ts, row in df.iterrows():
        try:
            bar = Bar(
                timestamp=ts.isoformat(),
                open=_safe_float(row.get("open")),
                high=_safe_float(row.get("high")),
                low=_safe_float(row.get("low")),
                close=_safe_float(row.get("close")),
                volume=_safe_float(row.get("volume")),
            )
        except (ValueError, TypeError, KeyError):
            # Provider returned NaN/None or missing columns — drop the bar
            # rather than serialise invalid JSON.
            continue
        out.append(bar)
    return out


def _resolve_pinned_provider(name: str, registry: DataProviderRegistry):
    adapter = registry.get(name)
    if adapter is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Provider not registered: {name}",
        )
    return adapter


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
        adapter = _resolve_pinned_provider(provider, registry)
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
        served_provider = provider
    else:
        try:
            df, served_provider = await registry.get_ohlcv_traced(
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
            logger.warning(
                "market_data.fatal_provider_error",
                symbol=symbol,
                error=type(exc).__name__,
            )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            ) from None

    return BarsResponse(
        symbol=symbol,
        interval=interval,
        period=period,
        asset_class=resolved_class.value,
        provider=served_provider,
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
        adapter = _resolve_pinned_provider(provider, registry)
        try:
            price = await adapter.get_latest_price(symbol)
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
        except ProviderError as exc:
            logger.warning(
                "market_data.provider_error",
                provider=provider,
                symbol=symbol,
                error=type(exc).__name__,
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)
            ) from None
        served_provider = provider
    else:
        try:
            price, served_provider = await registry.get_latest_price_traced(
                symbol, asset_class=resolved_class
            )
        except CapabilityNotSupportedError as exc:
            raise HTTPException(
                status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=str(exc)
            ) from None
        except NoProviderAvailableError as exc:
            # Distinguish "no providers configured" from "no price for symbol":
            # NoProviderAvailableError raised here means every candidate
            # adapter failed (or none registered); 503 is the honest signal.
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
            ) from None
        except FatalProviderError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            ) from None

    if price is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No price available for {symbol}",
        )

    return QuoteResponse(
        symbol=symbol,
        asset_class=resolved_class.value,
        provider=served_provider,
        price=_safe_float(price),
    )
