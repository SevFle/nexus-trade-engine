"""MiFID II RTS 22 transaction-report scaffold (gh#155).

Renders the most-essential ~20 of the 65 fields ESMA's Regulatory
Technical Standard 22 (Commission Delegated Regulation 2017/590)
requires for post-trade transaction reports. The intent is to give
operators a serialiser they can wire into their own reporting
pipelines without owning the full RTS 22 schema themselves.

Operators of an EU MiFID II investment firm are required to send the
T+1 transaction report to their National Competent Authority via an
Approved Reporting Mechanism (ARM). This module produces a CSV that
matches the ARM's expected layout for the *core* fields; downstream
tooling enriches the file with the remaining fields (notification
codes, decision-maker IDs, waiver indicators, commodity-derivative
flags, etc.).

Field coverage
--------------
Implemented (ESMA RTS 22 Annex Field number in parentheses):

- 1  Transaction reference number
- 2  Trading venue transaction identification code
- 3  Executing entity LEI
- 4  Investment-firm-covered indicator
- 5  Submitting entity LEI
- 6  Buyer identification type
- 7  Buyer LEI / national ID
- 8  Buyer decision-maker code (NATL when natural person, LEI for entity)
- 9  Buyer decision-maker identifier (LEI / national ID / "NORE")
- 15 Seller identification type
- 16 Seller LEI / national ID
- 17 Seller decision-maker code
- 18 Seller decision-maker identifier
- 24 Investment-decision-within-firm algorithmic ID
- 25 Country of branch supervising the investment decision
- 26 Execution-within-firm algorithmic ID
- 27 Country of branch supervising execution
- 28 Trading date and time (UTC)
- 29 Trading capacity
- 30 Quantity
- 31 Quantity currency / unit (XBT/USD/etc.)
- 32 Side (buy/sell)
- 33 Price
- 34 Price currency
- 35 Country of branch (where applicable)
- 36 Trading venue (MIC code)
- 41 Instrument identification code (ISIN)
- 43 CFI code
- 62 Waiver indicator
- 63 Short-sale indicator (SESH / SSEX / SELL / UNDI per ESMA)
- 64 OTC post-trade indicator
- 65 Commodity-derivative indicator

Deferred (~33 fields) — explicit follow-ups:

- Intermediary fields (10-14 buyer transmission, 19-23 seller
  transmission, plus party-A / party-B routing).
- Commodity-derivative metadata block (fields 53-55: notional schedule,
  delivery type, contract type details).
- Securities-financing-transaction flags (fields 56-61).
- Schema validation against the official ESMA XSD.

What's NOT here
---------------
- ARM-specific transport (FIX, SFTP, MQ). Operators wire that.
- Reference-data resolution (ISIN ↔ CFI lookups).
- Position reporting under MiFIR Article 26(2) — different schema.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum

_TWOPLACES = Decimal("0.01")


class Side(str, Enum):
    BUYI = "BUYI"  # ESMA RTS 22 buy code
    SELL = "SELL"  # ESMA RTS 22 sell code


class TradingCapacity(str, Enum):
    """ESMA RTS 22 trading-capacity codes."""

    DEAL = "DEAL"  # dealing on own account
    MTCH = "MTCH"  # matched principal
    AOTC = "AOTC"  # any other trading capacity (agency)


class IdType(str, Enum):
    """Identification type for buyer/seller. The full RTS 22 set is
    larger; this scaffold covers LEI for entities and NIDN (national
    identifier) for natural persons."""

    LEI = "LEI"
    NIDN = "NIDN"


class ShortSaleIndicator(str, Enum):
    """ESMA RTS 22 field 63 short-sale codes."""

    SESH = "SESH"  # short sale with no exemption
    SSEX = "SSEX"  # short sale with exemption
    SELL = "SELL"  # any other (i.e. not a short sale)
    UNDI = "UNDI"  # information not available

    @classmethod
    def empty(cls) -> str:
        """Sentinel returned when the operator has not classified the
        trade. ARMs treat the missing field as ``UNDI`` but we keep
        empty strings out of the enum so the dataclass default stays
        opt-in."""
        return ""


@dataclass(frozen=True)
class MiFID2Transaction:
    """Minimum-viable RTS 22 transaction record."""

    transaction_reference_number: str
    venue_transaction_id: str | None
    executing_entity_lei: str
    investment_firm_covered: bool
    submitting_entity_lei: str
    buyer_id_type: IdType
    buyer_id: str
    seller_id_type: IdType
    seller_id: str
    trading_capacity: TradingCapacity
    quantity: Decimal
    quantity_unit_or_ccy: str
    price: Decimal
    price_currency: str
    trading_datetime: datetime
    trading_venue: str
    instrument_isin: str
    cfi_code: str
    side: Side
    branch_country: str = ""
    # Decision-maker fields. Defaults preserve backward compatibility
    # with callers that don't yet populate them; ESMA accepts the
    # ARM's standard sentinels (e.g. "NORE" for "no decision maker
    # other than the investment firm itself").
    # Field 8 / 17 — decision-maker code: "NATL" (natural person),
    # "LEI" (entity), or "NORE" (none beyond the firm).
    buyer_decision_maker_code: str = ""
    buyer_decision_maker_id: str = ""  # field 9
    seller_decision_maker_code: str = ""  # field 17
    seller_decision_maker_id: str = ""  # field 18
    # Algorithmic-decision identifiers. Empty string when the trade
    # was operator-initiated (no algo).
    investment_decision_algo_id: str = ""  # field 24
    investment_decision_branch_country: str = ""  # field 25
    execution_algo_id: str = ""  # field 26
    execution_branch_country: str = ""  # field 27
    # Indicator fields 62-65. Booleans render as "true" / "false" to
    # match the existing investment_firm_covered convention; the
    # short-sale field is a discrete ESMA code (or empty when not
    # classified — see :class:`ShortSaleIndicator`).
    waiver_indicator: bool = False  # field 62
    short_sale_indicator: str = ""  # field 63 (ShortSaleIndicator code)
    otc_post_trade_indicator: bool = False  # field 64
    commodity_derivative_indicator: bool = False  # field 65

    def __post_init__(self) -> None:
        if not self.transaction_reference_number:
            raise ValueError("transaction_reference_number must be non-empty")
        if not self.executing_entity_lei:
            raise ValueError("executing_entity_lei must be non-empty")
        if not self.submitting_entity_lei:
            raise ValueError("submitting_entity_lei must be non-empty")
        if self.quantity <= 0:
            raise ValueError("quantity must be positive")
        if self.price < 0:
            raise ValueError("price must be non-negative")
        if (
            self.trading_datetime.tzinfo is None
            or self.trading_datetime.utcoffset() is None
        ):
            raise ValueError(
                "trading_datetime must be timezone-aware (UTC required by RTS 22)"
            )
        if self.short_sale_indicator and self.short_sale_indicator not in {
            ShortSaleIndicator.SESH.value,
            ShortSaleIndicator.SSEX.value,
            ShortSaleIndicator.SELL.value,
            ShortSaleIndicator.UNDI.value,
        }:
            raise ValueError(
                f"short_sale_indicator must be one of "
                f"{{SESH, SSEX, SELL, UNDI}} or empty, got "
                f"{self.short_sale_indicator!r}"
            )


# ESMA expects an exact column order for ARM submission.
RTS_22_COLUMNS: tuple[str, ...] = (
    "transaction_reference_number",  # 1
    "venue_transaction_id",  # 2
    "executing_entity_lei",  # 3
    "investment_firm_covered",  # 4
    "submitting_entity_lei",  # 5
    "buyer_id_type",  # 6
    "buyer_id",  # 7
    "buyer_decision_maker_code",  # 8
    "buyer_decision_maker_id",  # 9
    "seller_id_type",  # 15
    "seller_id",  # 16
    "seller_decision_maker_code",  # 17
    "seller_decision_maker_id",  # 18
    "investment_decision_algo_id",  # 24
    "investment_decision_branch_country",  # 25
    "execution_algo_id",  # 26
    "execution_branch_country",  # 27
    "trading_datetime",  # 28
    "trading_capacity",  # 29
    "quantity",  # 30
    "quantity_unit_or_ccy",  # 31
    "side",  # 32
    "price",  # 33
    "price_currency",  # 34
    "branch_country",  # 35
    "trading_venue",  # 36
    "instrument_isin",  # 41
    "cfi_code",  # 43
    "waiver_indicator",  # 62
    "short_sale_indicator",  # 63
    "otc_post_trade_indicator",  # 64
    "commodity_derivative_indicator",  # 65
)


def transactions_to_csv(transactions: list[MiFID2Transaction]) -> str:
    """Render ``transactions`` to RTS-22-shaped CSV.

    Datetimes serialise as ``YYYY-MM-DDTHH:MM:SS.ffffffZ`` (UTC),
    booleans render as ``true`` / ``false``, decimals quantise to two
    places. Empty optional values become empty strings.
    """
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(RTS_22_COLUMNS)
    for t in transactions:
        writer.writerow(
            [
                t.transaction_reference_number,
                t.venue_transaction_id or "",
                t.executing_entity_lei,
                "true" if t.investment_firm_covered else "false",
                t.submitting_entity_lei,
                t.buyer_id_type.value,
                t.buyer_id,
                t.buyer_decision_maker_code,
                t.buyer_decision_maker_id,
                t.seller_id_type.value,
                t.seller_id,
                t.seller_decision_maker_code,
                t.seller_decision_maker_id,
                t.investment_decision_algo_id,
                t.investment_decision_branch_country,
                t.execution_algo_id,
                t.execution_branch_country,
                _fmt_dt(t.trading_datetime),
                t.trading_capacity.value,
                _fmt_money(t.quantity),
                t.quantity_unit_or_ccy,
                t.side.value,
                _fmt_money(t.price),
                t.price_currency,
                t.branch_country,
                t.trading_venue,
                t.instrument_isin,
                t.cfi_code,
                "true" if t.waiver_indicator else "false",
                t.short_sale_indicator,
                "true" if t.otc_post_trade_indicator else "false",
                "true" if t.commodity_derivative_indicator else "false",
            ]
        )
    return buf.getvalue()


def _fmt_money(value: Decimal) -> str:
    return f"{value.quantize(_TWOPLACES)}"


def _fmt_dt(value: datetime) -> str:
    """ESMA expects UTC datetimes in the extended ISO 8601 form ending
    in ``Z``. Convert to UTC first so a non-UTC timezone-aware input
    is normalised, then patch ``+00:00`` to ``Z``."""
    iso = value.astimezone(UTC).isoformat()
    if iso.endswith("+00:00"):
        return iso[:-6] + "Z"
    return iso


__all__ = [
    "RTS_22_COLUMNS",
    "IdType",
    "MiFID2Transaction",
    "ShortSaleIndicator",
    "Side",
    "TradingCapacity",
    "transactions_to_csv",
]
