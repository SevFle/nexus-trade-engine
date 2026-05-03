"""Tests for the /tax/report/{code}.csv endpoint (gh#155 follow-up)."""

from __future__ import annotations

import csv as _csv
import io

import pytest
from httpx import AsyncClient

_CSV_PREFIX = "text/csv"


def _disposal(
    *,
    description: str = "100 ABC",
    acquired: str = "2023-06-01",
    disposed: str = "2024-06-01",
    proceeds: str = "0",
    cost: str = "0",
) -> dict:
    return {
        "description": description,
        "acquired": acquired,
        "disposed": disposed,
        "proceeds": proceeds,
        "cost": cost,
    }


def _parse_csv(text: str) -> tuple[list[str], list[str]]:
    rows = list(_csv.reader(io.StringIO(text)))
    assert len(rows) == 2  # header + values
    return rows[0], rows[1]


class TestSuccess:
    async def test_us_csv_includes_long_term_columns(self, client: AsyncClient):
        resp = await client.post(
            "/api/v1/tax/report/US/csv",
            json={
                "disposals": [
                    _disposal(
                        acquired="2022-01-01",
                        disposed="2024-06-01",
                        proceeds="9000",
                        cost="4000",
                    )
                ]
            },
        )

        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith(_CSV_PREFIX)
        # Browser-friendly download; filename echoes the jurisdiction.
        assert (
            resp.headers["content-disposition"]
            == 'attachment; filename="tax-report-US.csv"'
        )

        header, values = _parse_csv(resp.text)
        idx = {col: i for i, col in enumerate(header)}
        assert values[idx["long_term.row_count"]] == "1"
        assert values[idx["long_term.gain_loss"]] == "5000.00"

    async def test_gb_csv_includes_aea_columns(self, client: AsyncClient):
        resp = await client.post(
            "/api/v1/tax/report/GB/csv",
            json={"disposals": [_disposal(proceeds="15000", cost="10000")]},
        )

        assert resp.status_code == 200
        header, values = _parse_csv(resp.text)
        idx = {col: i for i, col in enumerate(header)}
        assert values[idx["taxable_gain"]] == "2000.00"

    async def test_lowercase_code_normalises_filename(self, client: AsyncClient):
        resp = await client.post(
            "/api/v1/tax/report/de/csv",
            json={"disposals": [_disposal(proceeds="6000", cost="1000")]},
        )

        assert resp.status_code == 200
        assert (
            resp.headers["content-disposition"]
            == 'attachment; filename="tax-report-DE.csv"'
        )


class TestErrors:
    async def test_unknown_code_returns_400(self, client: AsyncClient):
        resp = await client.post(
            "/api/v1/tax/report/ZZ/csv",
            json={"disposals": []},
        )

        assert resp.status_code == 400
        assert "ZZ" in resp.json()["detail"]

    async def test_invalid_decimal_rejected_at_pydantic(self, client: AsyncClient):
        resp = await client.post(
            "/api/v1/tax/report/US/csv",
            json={"disposals": [_disposal(proceeds="abc", cost="100")]},
        )

        assert resp.status_code == 422

    async def test_acquired_after_disposed_rejected_at_taxable(
        self, client: AsyncClient
    ):
        resp = await client.post(
            "/api/v1/tax/report/US/csv",
            json={
                "disposals": [
                    _disposal(
                        acquired="2024-12-31",
                        disposed="2024-01-01",
                        proceeds="100",
                        cost="50",
                    )
                ]
            },
        )

        assert resp.status_code == 400


class TestEmpty:
    @pytest.mark.parametrize("code", ["US", "GB", "DE", "FR"])
    async def test_empty_disposals_returns_csv_with_zero_values(
        self, client: AsyncClient, code: str
    ):
        resp = await client.post(
            f"/api/v1/tax/report/{code}/csv",
            json={"disposals": []},
        )

        assert resp.status_code == 200
        # Even on empty input we emit a 2-row CSV (header + zero values).
        header, values = _parse_csv(resp.text)
        assert len(header) > 0
        assert len(values) == len(header)
