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

    def to_provider_class(self) -> ProviderAssetClass:  # noqa: PLR0911 - one return per case
        """Map to the data-routing :class:`AssetClass` used by providers."""
        from typing import assert_never  # noqa: PLC0415

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
            case _:
                assert_never(self)
        return P.EQUITY  # unreachable — assert_never never returns


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

# Asset classes that are US Section 1256 contracts by default (60/40 tax
# treatment). Used by the auto-detecting ``is_section_1256`` validator when
# the caller does not state an explicit value.
_SECTION_1256_CLASSES = frozenset({InstrumentAssetClass.FUTURE})

# Well-known exchange-traded futures contract sizes (units per contract).
# Used by :meth:`Instrument.future` to default ``multiplier`` from the
# symbol, mirroring how ``multiplier`` already encodes "size per contract"
# for options (e.g. 100 shares). There is deliberately NO separate
# ``contract_multiplier`` field — ``multiplier`` is the single canonical
# representation of "units per contract" for every asset class.
_FUTURE_CONTRACT_SIZES: dict[str, int] = {
    "ES": 50,  # CME E-mini S&P 500 — $50 x index
    "NQ": 20,  # CME E-mini Nasdaq-100 — $20 x index
    "YM": 5,  # CBOT E-mini Dow — $5 x index
    "RTY": 50,  # CME E-mini Russell 2000 — $50 x index
    "CL": 1000,  # NYMEX WTI crude — 1 000 barrels
    "GC": 100,  # COMEX gold — 100 troy oz
    "SI": 5000,  # COMEX silver — 5 000 troy oz
    "ZC": 5000,  # CBOT corn — 5 000 bushels
    "ZS": 5000,  # CBOT soybeans — 5 000 bushels
    "ZN": 1000,  # CBOT 10-Year Treasury — $1 000 face
}


class Instrument(BaseModel):
    """Canonical, typed representation of any tradable instrument."""

    model_config = {"frozen": False, "validate_assignment": True}

    # Identity
    symbol: str = Field(
        ...,
        min_length=1,
        description="Canonical symbol — e.g. 'AAPL', 'BTC/USDT'",
    )
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
    strike: float | None = Field(default=None, gt=0.0)
    expiration: date | None = Field(default=None)
    option_type: OptionType | None = Field(default=None)
    multiplier: int = Field(default=1, ge=1, description="Units per contract")

    # Tax classification (US Section 1256 — 60/40 treatment). A ``None``
    # input means "auto-detect"; the ``_auto_section_1256`` model validator
    # resolves it to a concrete bool at construction time based on the asset
    # class. Callers may override by passing an explicit ``True``/``False``.
    is_section_1256: bool | None = Field(
        default=None,
        description="US 60/40 tax-treatment flag; auto-detected when omitted",
    )

    @field_validator("symbol")
    @classmethod
    def _symbol_no_whitespace(cls, v: str) -> str:
        if not v.strip() or v.strip() != v:
            msg = "symbol must be non-empty and contain no leading/trailing whitespace"
            raise ValueError(msg)
        return v

    @model_validator(mode="before")
    @classmethod
    def _reject_expiry_date_alias(cls, data: Any) -> Any:
        """Keep ``expiration`` canonical.

        ``expiry_date`` is intentionally NOT a model field (no duplicate
        date fields). If a caller nonetheless supplies both ``expiry_date``
        and ``expiration`` with *differing* values we fail loudly rather
        than silently dropping one. Equal values are tolerated (and the
        alias is discarded) so that legacy callers stay compatible.
        """
        if isinstance(data, dict):
            expiry = data.get("expiry_date")
            expiration = data.get("expiration")
            if expiry is not None and expiration is not None and expiry != expiration:
                msg = (
                    "Both 'expiry_date' and 'expiration' were provided with "
                    "differing values; set only 'expiration' (the canonical field)."
                )
                raise ValueError(msg)
        return data

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

    @model_validator(mode="after")
    def _auto_section_1256(self) -> Instrument:
        """Auto-set ``is_section_1256`` when the caller left it unset.

        Detection keys off the field *value* (``None``) rather than whether
        the field was present in the constructor kwargs, so an explicit
        ``is_section_1256=None`` auto-detects exactly like an omitted value.
        A concrete ``True``/``False`` is always honoured.
        ``object.__setattr__`` is used because the model has
        ``validate_assignment=True`` and a plain assignment would re-run
        every model validator.
        """
        if self.is_section_1256 is None:
            object.__setattr__(
                self,
                "is_section_1256",
                self.asset_class in _SECTION_1256_CLASSES,
            )
        return self

    # ── Derived properties ───────────────────────────────────────────

    @property
    def uid(self) -> str:  # noqa: PLR0911 - one return per asset class
        """Stable identifier — distinct per (asset_class, identifying fields).

        Spot, perpetual, and dated future on the same pair MUST produce
        different uids; otherwise positions in different products silently
        collapse onto the same key.
        """
        ac = self.asset_class
        if ac == InstrumentAssetClass.OPTION:
            if self.expiration is None or self.option_type is None or self.strike is None:
                msg = "option uid requires expiration, option_type, strike"
                raise ValueError(msg)
            yyyymmdd = self.expiration.strftime("%Y%m%d")
            cp = "C" if self.option_type == OptionType.CALL else "P"
            return f"{self.underlying}_{yyyymmdd}_{cp}_{self.strike:.2f}"
        if ac == InstrumentAssetClass.FUTURE and self.expiration:
            return f"{self.symbol}_{self.expiration.strftime('%Y%m%d')}"
        if ac == InstrumentAssetClass.CRYPTO and self.base_asset and self.quote_asset:
            return f"{self.base_asset}/{self.quote_asset}"
        if ac == InstrumentAssetClass.CRYPTO_PERP and self.base_asset and self.quote_asset:
            return f"{self.base_asset}/{self.quote_asset}:PERP"
        if ac == InstrumentAssetClass.CRYPTO_FUTURE and self.base_asset and self.quote_asset:
            suffix = self.expiration.strftime("%Y%m%d") if self.expiration else "FUT"
            return f"{self.base_asset}/{self.quote_asset}:{suffix}"
        if ac == InstrumentAssetClass.FOREX and self.base_asset and self.quote_asset:
            return f"{self.base_asset}/{self.quote_asset}:FX"
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
    def future(
        cls,
        symbol: str,
        *,
        expiration: date | None = None,
        exchange: str | None = None,
        currency: str = "USD",
        multiplier: int | None = None,
        is_section_1256: bool | None = None,
    ) -> Instrument:
        """Exchange-traded future.

        ``multiplier`` defaults to the contract size for well-known symbols
        (e.g. 50 for ES). Pass ``is_section_1256=None`` (the default) to let
        the model auto-detect the 60/40 flag from the asset class; an
        explicit ``True``/``False`` overrides the default.
        """
        size = (
            multiplier
            if multiplier is not None
            else _FUTURE_CONTRACT_SIZES.get(symbol.upper(), 1)
        )
        kwargs: dict[str, Any] = {
            "symbol": symbol,
            "asset_class": InstrumentAssetClass.FUTURE,
            "expiration": expiration,
            "exchange": exchange,
            "currency": currency,
            "multiplier": size,
        }
        if is_section_1256 is not None:
            kwargs["is_section_1256"] = is_section_1256
        return cls(**kwargs)

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
        """Conservative coercion from a free-form symbol string.

        Defaults to **equity**. Symbol strings containing ``/`` are still
        treated as equity to avoid silently misclassifying forex pairs
        (``EUR/USD``), share-class notation (``BRK/B``), or non-pair
        slashes as crypto. Crypto/forex callers must use the explicit
        factories so the asset class is unambiguous.

        Raises ``ValueError`` for empty / whitespace-only strings.
        """
        if not raw or not raw.strip():
            msg = "from_string requires a non-empty symbol"
            raise ValueError(msg)
        return cls.equity(raw.strip())

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
