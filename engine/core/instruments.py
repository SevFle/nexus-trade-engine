"""Abstract :class:`Instrument` model for multi-asset support.

Replaces the legacy string-based ``symbol`` plumbing with a typed
``Instrument`` that knows its asset class, exchange, currency, and
asset-class-specific fields (option strike/expiration, crypto base/quote,
forex pip size, …).

Backward compatibility: existing code that passes ``symbol="AAPL"`` keeps
working. The new ``instrument`` field on :class:`engine.core.signal.Signal`
is auto-populated as :meth:`Instrument.equity` when only a string symbol
is supplied.

This is a *separate* enum from ``engine.data.providers.base.AssetClass``
because the data-routing taxonomy (which providers can serve a query)
evolves independently from the instrument taxonomy (what the engine
*models*). Use :meth:`InstrumentAssetClass.to_provider_class` to bridge.
"""

from __future__ import annotations

from datetime import date  # noqa: TC003 - needed at runtime by pydantic
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field, field_validator, model_validator

if TYPE_CHECKING:
    from engine.data.providers.base import AssetClass as ProviderAssetClass


class InstrumentAssetClass(StrEnum):
    EQUITY = "equity"
    ETF = "etf"
    CRYPTO = "crypto"
    CRYPTO_PERP = "crypto_perp"
    CRYPTO_FUTURE = "crypto_future"
    FOREX = "forex"
    OPTION = "option"
    FUTURE = "future"

    def to_provider_class(self) -> ProviderAssetClass:
        """Map to the data-routing :class:`AssetClass` used by providers."""
        from engine.data.providers.base import AssetClass as P  # noqa: PLC0415

        match self:
            case InstrumentAssetClass.EQUITY:
                return P.EQUITY
            case InstrumentAssetClass.ETF:
                return P.ETF
            case (
                InstrumentAssetClass.CRYPTO
                | InstrumentAssetClass.CRYPTO_PERP
                | InstrumentAssetClass.CRYPTO_FUTURE
            ):
                return P.CRYPTO
            case InstrumentAssetClass.FOREX:
                return P.FOREX
            case InstrumentAssetClass.OPTION:
                return P.OPTIONS
            case InstrumentAssetClass.FUTURE:
                return P.FUTURES
        msg = f"Unmapped InstrumentAssetClass: {self!r}"
        raise ValueError(msg)


class OptionType(StrEnum):
    CALL = "call"
    PUT = "put"


_DERIVATIVE_CLASSES = frozenset(
    {
        InstrumentAssetClass.OPTION,
        InstrumentAssetClass.FUTURE,
        InstrumentAssetClass.CRYPTO_PERP,
        InstrumentAssetClass.CRYPTO_FUTURE,
    }
)


class Instrument(BaseModel):
    """Canonical, typed representation of any tradable instrument."""

    model_config = {"frozen": False, "validate_assignment": True}

    # Identity
    symbol: str = Field(..., description="Canonical symbol — e.g. 'AAPL', 'BTC/USDT'")
    asset_class: InstrumentAssetClass

    # Listing
    exchange: str | None = Field(default=None, description="Primary venue MIC/name")
    currency: str = Field(default="USD", description="Quote currency")

    # Crypto / forex pair fields
    base_asset: str | None = Field(default=None)
    quote_asset: str | None = Field(default=None)

    # Forex
    pip_size: float | None = Field(default=None)
    lot_size: int | None = Field(default=None)

    # Options
    underlying: str | None = Field(default=None)
    strike: float | None = Field(default=None, ge=0.0)
    expiration: date | None = Field(default=None)
    option_type: OptionType | None = Field(default=None)
    multiplier: int = Field(default=1, ge=1)

    @field_validator("strike")
    @classmethod
    def _strike_positive(cls, v: float | None) -> float | None:
        if v is not None and v <= 0:
            msg = "strike must be > 0"
            raise ValueError(msg)
        return v

    @model_validator(mode="after")
    def _enforce_class_invariants(self) -> Instrument:
        """Each asset class requires its own field set."""
        match self.asset_class:
            case InstrumentAssetClass.OPTION:
                missing = [
                    name
                    for name in ("strike", "expiration", "option_type", "underlying")
                    if getattr(self, name) is None
                ]
                if missing:
                    msg = f"Option requires {missing}"
                    raise ValueError(msg)
            case (
                InstrumentAssetClass.CRYPTO
                | InstrumentAssetClass.CRYPTO_PERP
                | InstrumentAssetClass.CRYPTO_FUTURE
            ):
                if not (self.base_asset and self.quote_asset):
                    msg = "Crypto requires base_asset and quote_asset"
                    raise ValueError(msg)
            case InstrumentAssetClass.FOREX:
                if not (self.base_asset and self.quote_asset):
                    msg = "Forex requires base_asset and quote_asset"
                    raise ValueError(msg)
            case _:
                pass
        return self

    # ── Derived properties ───────────────────────────────────────────

    @property
    def uid(self) -> str:
        """Stable identifier across all instrument types."""
        if self.asset_class == InstrumentAssetClass.OPTION:
            # The model validator guarantees these are non-None when
            # asset_class == OPTION, so this branch never reads None.
            assert self.expiration is not None
            assert self.option_type is not None
            assert self.strike is not None
            yyyymmdd = self.expiration.strftime("%Y%m%d")
            cp = "C" if self.option_type == OptionType.CALL else "P"
            return f"{self.underlying}_{yyyymmdd}_{cp}_{self.strike:.2f}"
        if self.asset_class == InstrumentAssetClass.FUTURE and self.expiration:
            return f"{self.symbol}_{self.expiration.strftime('%Y%m%d')}"
        if self.asset_class in {
            InstrumentAssetClass.CRYPTO,
            InstrumentAssetClass.CRYPTO_PERP,
            InstrumentAssetClass.CRYPTO_FUTURE,
            InstrumentAssetClass.FOREX,
        } and self.base_asset and self.quote_asset:
            return f"{self.base_asset}/{self.quote_asset}"
        return self.symbol

    @property
    def is_derivative(self) -> bool:
        return self.asset_class in _DERIVATIVE_CLASSES

    @property
    def contract_value(self) -> float | None:
        """Notional for one contract: option strike * multiplier; None otherwise."""
        if self.asset_class == InstrumentAssetClass.OPTION and self.strike is not None:
            return self.strike * self.multiplier
        return None

    # ── Factories ────────────────────────────────────────────────────

    @classmethod
    def equity(
        cls, symbol: str, *, exchange: str | None = None, currency: str = "USD"
    ) -> Instrument:
        return cls(
            symbol=symbol,
            asset_class=InstrumentAssetClass.EQUITY,
            exchange=exchange,
            currency=currency,
        )

    @classmethod
    def etf(
        cls, symbol: str, *, exchange: str | None = None, currency: str = "USD"
    ) -> Instrument:
        return cls(
            symbol=symbol,
            asset_class=InstrumentAssetClass.ETF,
            exchange=exchange,
            currency=currency,
        )

    @classmethod
    def crypto(
        cls, base: str, quote: str, *, exchange: str | None = None
    ) -> Instrument:
        return cls(
            symbol=f"{base}/{quote}",
            asset_class=InstrumentAssetClass.CRYPTO,
            base_asset=base,
            quote_asset=quote,
            exchange=exchange,
            currency=quote,
        )

    @classmethod
    def crypto_perp(
        cls, base: str, quote: str, *, exchange: str | None = None
    ) -> Instrument:
        return cls(
            symbol=f"{base}/{quote}:PERP",
            asset_class=InstrumentAssetClass.CRYPTO_PERP,
            base_asset=base,
            quote_asset=quote,
            exchange=exchange,
            currency=quote,
        )

    @classmethod
    def forex(cls, base: str, quote: str) -> Instrument:
        # JPY-quoted pairs use 2-decimal pip; everything else 4-decimal.
        pip = 0.01 if quote.upper() == "JPY" else 0.0001
        return cls(
            symbol=f"{base}/{quote}",
            asset_class=InstrumentAssetClass.FOREX,
            base_asset=base,
            quote_asset=quote,
            currency=quote,
            pip_size=pip,
            lot_size=100_000,
        )

    @classmethod
    def option(
        cls,
        underlying: str,
        strike: float,
        expiration: date,
        option_type: OptionType,
        *,
        multiplier: int = 100,
        currency: str = "USD",
    ) -> Instrument:
        cp = "C" if option_type == OptionType.CALL else "P"
        symbol = f"{underlying}_{expiration.strftime('%Y%m%d')}_{cp}_{strike:.2f}"
        return cls(
            symbol=symbol,
            asset_class=InstrumentAssetClass.OPTION,
            underlying=underlying,
            strike=strike,
            expiration=expiration,
            option_type=option_type,
            multiplier=multiplier,
            currency=currency,
        )

    @classmethod
    def from_string(cls, raw: str) -> Instrument:
        """Best-effort coercion from a free-form symbol string.

        - ``"AAPL"`` → equity
        - ``"BTC/USDT"`` → crypto (default — forex requires explicit factory)
        - Unknown shapes fall back to equity to preserve backward compat.
        """
        if "/" in raw:
            parts = raw.split("/", 1)
            if len(parts) == 2 and all(parts):  # noqa: PLR2004 - tuple width
                return cls.crypto(parts[0], parts[1])
        return cls.equity(raw)

    @classmethod
    def coerce(cls, value: Any) -> Instrument:
        """Accept Instrument or string; return Instrument."""
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            return cls.from_string(value)
        msg = f"cannot coerce {type(value).__name__} to Instrument"
        raise TypeError(msg)


__all__ = [
    "Instrument",
    "InstrumentAssetClass",
    "OptionType",
]
