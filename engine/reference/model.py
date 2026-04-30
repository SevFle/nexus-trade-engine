"""Reference-data domain model.

Pure pydantic types — no DB session, no ORM coupling. The DB tables and
ingestion adapters serialize *to* and *from* these models.

Design notes:
- ``RefInstrument.id`` is a UUID4 generated at construction time. Once
  assigned, it is the **stable** internal handle that survives symbol
  changes; downstream tables (positions, orders) should foreign-key on
  this id, not on the ticker string.
- Cross-listed equities are stored as separate ``RefInstrument`` rows
  that share an ISIN. Use ``Resolver.resolve({"isin": ...})`` to fetch
  any candidate, or filter by venue.
- ``InstrumentIds`` validates the *shape* (ISIN 12 / CUSIP 9 / FIGI 12
  chars, CIK numeric). Checksum validation is an ingestion concern.
"""

from __future__ import annotations

import uuid
from datetime import date  # noqa: TC003 - pydantic needs runtime
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, StringConstraints, field_validator

AssetClassLiteral = Literal[
    "equity",
    "etf",
    "crypto",
    "crypto_perp",
    "crypto_future",
    "forex",
    "option",
    "future",
]


_ISIN = Annotated[str, StringConstraints(min_length=12, max_length=12, pattern=r"^[A-Z0-9]{12}$")]
_CUSIP = Annotated[str, StringConstraints(min_length=9, max_length=9, pattern=r"^[A-Z0-9]{9}$")]
_FIGI = Annotated[str, StringConstraints(min_length=12, max_length=12, pattern=r"^[A-Z0-9]{12}$")]
_SEDOL = Annotated[str, StringConstraints(min_length=7, max_length=7)]
_CIK = Annotated[str, StringConstraints(min_length=1, max_length=10, pattern=r"^[0-9]+$")]
_MIC = Annotated[str, StringConstraints(min_length=4, max_length=4, pattern=r"^[A-Z0-9]{4}$")]
_CCY = Annotated[str, StringConstraints(min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")]
# Ticker allowlist — covers all real-world formats (BRK.B, BTC-USD, ES_F,
# AAPL.L, ALV1, AAPL:US) without admitting path-traversal or injection
# characters. 1-32 chars. Slash is intentionally excluded so listing
# tickers cannot smuggle `../../etc/passwd`-shaped strings into the
# resolver indexes.
_TICKER = Annotated[
    str,
    StringConstraints(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9.\-_:]+$"),
]
_CLASSIFICATION_FIELD = Annotated[str, StringConstraints(max_length=128)]


class InstrumentIds(BaseModel):
    """Cross-vendor identifier bundle for a single instrument."""

    isin: _ISIN | None = None
    cusip: _CUSIP | None = None
    figi: _FIGI | None = None
    sedol: _SEDOL | None = None
    cik: _CIK | None = None


class Classification(BaseModel):
    """Sector / industry classification.

    GICS for equities; SIC/NAICS for US filings; crypto and FX have
    their own taxonomies (see :mod:`engine.reference.classification`).
    """

    gics_sector: _CLASSIFICATION_FIELD | None = None
    gics_industry_group: _CLASSIFICATION_FIELD | None = None
    gics_industry: _CLASSIFICATION_FIELD | None = None
    gics_sub_industry: _CLASSIFICATION_FIELD | None = None
    sic: _CLASSIFICATION_FIELD | None = None
    naics: _CLASSIFICATION_FIELD | None = None
    crypto_class: _CLASSIFICATION_FIELD | None = None
    forex_class: _CLASSIFICATION_FIELD | None = None


class Listing(BaseModel):
    """A single venue listing for an instrument.

    An instrument may have multiple listings over time (symbol changes,
    cross-listing). The resolver searches across all listings to handle
    historical ticker queries (FB → META).
    """

    venue: _MIC
    ticker: _TICKER
    currency: _CCY
    active_from: date
    active_to: date | None = None

    @property
    def is_active(self) -> bool:
        return self.active_to is None


class GICSNode(BaseModel):
    """One node in the GICS taxonomy tree."""

    code: str
    name: str
    level: Literal["sector", "industry_group", "industry", "sub_industry"]
    parent_code: str | None = None


class Venue(BaseModel):
    """ISO 10383 MIC venue metadata."""

    mic: _MIC
    name: str
    country: str = Field(..., min_length=2, max_length=2)
    timezone: str = Field(..., min_length=1)


class RefInstrument(BaseModel):
    """Canonical reference record for any tradable instrument."""

    model_config = {"validate_assignment": True}

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    primary_ticker: _TICKER
    primary_venue: _MIC
    asset_class: AssetClassLiteral
    name: str = Field(..., min_length=1)
    active: bool = True
    lot_size: Decimal = Field(default=Decimal("1"))
    tick_size: Decimal = Field(default=Decimal("0.01"))
    currency: _CCY = "USD"
    ids: InstrumentIds = Field(default_factory=InstrumentIds)
    classification: Classification = Field(default_factory=Classification)
    listings: list[Listing] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("primary_ticker")
    @classmethod
    def _ticker_no_whitespace(cls, v: str) -> str:
        if not v.strip() or v.strip() != v:
            msg = "primary_ticker must be non-empty and trimmed"
            raise ValueError(msg)
        return v


__all__ = [
    "AssetClassLiteral",
    "Classification",
    "GICSNode",
    "InstrumentIds",
    "Listing",
    "RefInstrument",
    "Venue",
]
