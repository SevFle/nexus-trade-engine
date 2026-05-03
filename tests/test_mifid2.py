"""Tests for the MiFID II RTS 22 transaction-report scaffold (gh#155)."""

from __future__ import annotations

import csv as _csv
import io
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from engine.core.tax.reports import (
    RTS_22_COLUMNS,
    IdType,
    MiFID2Transaction,
    Side,
    TradingCapacity,
    transactions_to_csv,
)


def _txn(**overrides) -> MiFID2Transaction:
    base: dict = {
        "transaction_reference_number": "TRN-0001",
        "venue_transaction_id": "VTID-001",
        "executing_entity_lei": "529900XYZABC1234567A",
        "investment_firm_covered": True,
        "submitting_entity_lei": "529900XYZABC1234567A",
        "buyer_id_type": IdType.LEI,
        "buyer_id": "529900BUYR1234567X",
        "seller_id_type": IdType.LEI,
        "seller_id": "529900SELR1234567Y",
        "trading_capacity": TradingCapacity.AOTC,
        "quantity": Decimal("100"),
        "quantity_unit_or_ccy": "UNIT",
        "price": Decimal("12.50"),
        "price_currency": "EUR",
        "trading_datetime": datetime(2024, 6, 1, 14, 30, 15, tzinfo=UTC),
        "trading_venue": "XPAR",
        "instrument_isin": "FR0000131104",
        "cfi_code": "ESVUFR",
        "side": Side.BUYI,
        "branch_country": "FR",
    }
    base.update(overrides)
    return MiFID2Transaction(**base)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestValidation:
    def test_empty_reference_rejected(self):
        with pytest.raises(ValueError):
            _txn(transaction_reference_number="")

    def test_empty_executing_entity_rejected(self):
        with pytest.raises(ValueError):
            _txn(executing_entity_lei="")

    def test_empty_submitting_entity_rejected(self):
        with pytest.raises(ValueError):
            _txn(submitting_entity_lei="")

    def test_zero_quantity_rejected(self):
        with pytest.raises(ValueError):
            _txn(quantity=Decimal("0"))

    def test_negative_price_rejected(self):
        with pytest.raises(ValueError):
            _txn(price=Decimal("-1"))

    def test_naive_datetime_rejected(self):
        # RTS 22 requires UTC. Naive datetimes are ambiguous.
        with pytest.raises(ValueError):
            _txn(trading_datetime=datetime(2024, 6, 1, 14, 30, 15))


# ---------------------------------------------------------------------------
# Constants / enums
# ---------------------------------------------------------------------------


class TestEnums:
    def test_side_codes_match_esma(self):
        assert Side.BUYI.value == "BUYI"
        assert Side.SELL.value == "SELL"

    def test_trading_capacity_codes_match_esma(self):
        assert TradingCapacity.DEAL.value == "DEAL"
        assert TradingCapacity.MTCH.value == "MTCH"
        assert TradingCapacity.AOTC.value == "AOTC"

    def test_id_type_codes_match_esma(self):
        assert IdType.LEI.value == "LEI"
        assert IdType.NIDN.value == "NIDN"


class TestColumnOrder:
    def test_columns_match_rts22_field_order(self):
        # First three are reference + venue id + executing LEI;
        # quantity comes after the buyer/seller block; CFI is last.
        assert RTS_22_COLUMNS[0] == "transaction_reference_number"
        assert RTS_22_COLUMNS[1] == "venue_transaction_id"
        assert RTS_22_COLUMNS[2] == "executing_entity_lei"
        assert RTS_22_COLUMNS[-1] == "cfi_code"
        # No duplicates.
        assert len(set(RTS_22_COLUMNS)) == len(RTS_22_COLUMNS)
        # 28 fields after the decision-maker expansion (gh#155 follow-up).
        assert len(RTS_22_COLUMNS) == 28
        # Decision-maker columns are present.
        assert "buyer_decision_maker_code" in RTS_22_COLUMNS
        assert "buyer_decision_maker_id" in RTS_22_COLUMNS
        assert "seller_decision_maker_code" in RTS_22_COLUMNS
        assert "seller_decision_maker_id" in RTS_22_COLUMNS
        assert "investment_decision_algo_id" in RTS_22_COLUMNS
        assert "investment_decision_branch_country" in RTS_22_COLUMNS
        assert "execution_algo_id" in RTS_22_COLUMNS
        assert "execution_branch_country" in RTS_22_COLUMNS


# ---------------------------------------------------------------------------
# CSV serialisation
# ---------------------------------------------------------------------------


def _parse(text: str) -> tuple[list[str], list[list[str]]]:
    rows = list(_csv.reader(io.StringIO(text)))
    return rows[0], rows[1:]


class TestCsv:
    def test_header_matches_column_constant(self):
        out = transactions_to_csv([_txn()])
        header, _ = _parse(out)
        assert header == list(RTS_22_COLUMNS)

    def test_single_transaction_row_shape(self):
        out = transactions_to_csv([_txn()])
        header, body = _parse(out)
        assert len(body) == 1
        idx = {col: i for i, col in enumerate(header)}
        row = body[0]

        assert row[idx["transaction_reference_number"]] == "TRN-0001"
        assert row[idx["executing_entity_lei"]] == "529900XYZABC1234567A"
        assert row[idx["investment_firm_covered"]] == "true"
        assert row[idx["buyer_id_type"]] == "LEI"
        assert row[idx["seller_id"]] == "529900SELR1234567Y"
        assert row[idx["trading_capacity"]] == "AOTC"
        assert row[idx["quantity"]] == "100.00"
        assert row[idx["price"]] == "12.50"
        assert row[idx["side"]] == "BUYI"
        assert row[idx["instrument_isin"]] == "FR0000131104"
        assert row[idx["cfi_code"]] == "ESVUFR"

    def test_datetime_serialises_utc_with_z_suffix(self):
        out = transactions_to_csv([_txn()])
        _, body = _parse(out)
        # Datetime column should end with "Z", not "+00:00".
        assert "2024-06-01T14:30:15Z" in body[0]
        for cell in body[0]:
            if cell.startswith("2024-06-01"):
                assert cell.endswith("Z")
                assert "+00:00" not in cell

    def test_non_utc_datetime_normalised_to_utc(self):
        # +02:00 timezone — should convert to 12:30:15Z.
        cest = timezone(timedelta(hours=2))
        out = transactions_to_csv(
            [
                _txn(
                    trading_datetime=datetime(
                        2024, 6, 1, 14, 30, 15, tzinfo=cest
                    )
                )
            ]
        )
        _, body = _parse(out)
        assert any(
            cell == "2024-06-01T12:30:15Z"
            for cell in body[0]
        )

    def test_empty_optional_venue_id_renders_blank(self):
        out = transactions_to_csv([_txn(venue_transaction_id=None)])
        header, body = _parse(out)
        idx = {col: i for i, col in enumerate(header)}
        assert body[0][idx["venue_transaction_id"]] == ""

    def test_investment_firm_false_renders_lowercase_false(self):
        out = transactions_to_csv([_txn(investment_firm_covered=False)])
        header, body = _parse(out)
        idx = {col: i for i, col in enumerate(header)}
        assert body[0][idx["investment_firm_covered"]] == "false"

    def test_decision_maker_defaults_render_blank(self):
        # The 8 decision-maker fields default to empty strings so
        # callers that don't yet populate them keep producing valid
        # CSVs.
        out = transactions_to_csv([_txn()])
        header, body = _parse(out)
        idx = {col: i for i, col in enumerate(header)}
        for col in (
            "buyer_decision_maker_code",
            "buyer_decision_maker_id",
            "seller_decision_maker_code",
            "seller_decision_maker_id",
            "investment_decision_algo_id",
            "investment_decision_branch_country",
            "execution_algo_id",
            "execution_branch_country",
        ):
            assert body[0][idx[col]] == ""

    def test_decision_maker_populated_round_trips_to_csv(self):
        out = transactions_to_csv(
            [
                _txn(
                    buyer_decision_maker_code="LEI",
                    buyer_decision_maker_id="529900BUYR1234567X",
                    seller_decision_maker_code="NORE",
                    seller_decision_maker_id="NORE",
                    investment_decision_algo_id="ALGO-INV-42",
                    investment_decision_branch_country="DE",
                    execution_algo_id="ALGO-EXEC-7",
                    execution_branch_country="FR",
                )
            ]
        )
        header, body = _parse(out)
        idx = {col: i for i, col in enumerate(header)}
        assert body[0][idx["buyer_decision_maker_code"]] == "LEI"
        assert body[0][idx["buyer_decision_maker_id"]] == "529900BUYR1234567X"
        assert body[0][idx["seller_decision_maker_code"]] == "NORE"
        assert body[0][idx["investment_decision_algo_id"]] == "ALGO-INV-42"
        assert body[0][idx["investment_decision_branch_country"]] == "DE"
        assert body[0][idx["execution_algo_id"]] == "ALGO-EXEC-7"
        assert body[0][idx["execution_branch_country"]] == "FR"

    def test_decision_maker_columns_precede_trading_datetime(self):
        # Field-order constraint: ESMA expects the decision-maker
        # block (8/9, 17/18, 24-27) before the trading datetime (28).
        out = transactions_to_csv([_txn()])
        header, _ = _parse(out)
        idx = {col: i for i, col in enumerate(header)}
        assert idx["buyer_decision_maker_code"] < idx["trading_datetime"]
        assert idx["seller_decision_maker_id"] < idx["trading_datetime"]
        assert idx["execution_branch_country"] < idx["trading_datetime"]


class TestEmpty:
    def test_empty_list_yields_header_only(self):
        out = transactions_to_csv([])
        header, body = _parse(out)
        assert header == list(RTS_22_COLUMNS)
        assert body == []
