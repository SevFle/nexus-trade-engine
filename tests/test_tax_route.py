"""Tests for the tax-report API route (gh#155)."""

from __future__ import annotations

from datetime import date

import pytest
from httpx import AsyncClient


def _disposal(
    *,
    description: str = "100 ABC",
    acquired: date | str = "2023-06-01",
    disposed: date | str = "2024-06-01",
    proceeds: str = "0",
    cost: str = "0",
) -> dict:
    return {
        "description": description,
        "acquired": str(acquired),
        "disposed": str(disposed),
        "proceeds": proceeds,
        "cost": cost,
    }


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


class TestUsRouting:
    async def test_us_returns_schedule_d_summary_json(
        self, client: AsyncClient
    ):
        # Long-term lot (>1 year) with a +5,000 gain.
        resp = await client.post(
            "/api/v1/tax/report/US",
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
        body = resp.json()
        assert body["jurisdiction"] == "US"
        summary = body["summary"]
        assert summary["long_term"]["row_count"] == 1
        assert summary["long_term"]["gain_loss"] == "5000.00"
        assert summary["short_term"]["row_count"] == 0


class TestGbRouting:
    async def test_gb_returns_cgt_summary_with_aea_applied(
        self, client: AsyncClient
    ):
        resp = await client.post(
            "/api/v1/tax/report/GB",
            json={
                "disposals": [
                    _disposal(proceeds="15000", cost="10000"),
                ]
            },
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["jurisdiction"] == "GB"
        s = body["summary"]
        assert s["net_gain"] == "5000.00"
        assert s["annual_exempt_amount_used"] == "3000.00"
        assert s["taxable_gain"] == "2000.00"


class TestDeRouting:
    async def test_de_returns_kest_summary_with_solz_breakdown(
        self, client: AsyncClient
    ):
        resp = await client.post(
            "/api/v1/tax/report/DE",
            json={
                "disposals": [
                    _disposal(proceeds="6000", cost="1000"),
                ]
            },
        )

        assert resp.status_code == 200
        s = resp.json()["summary"]
        assert s["taxable_income"] == "4000.00"
        assert s["kest"] == "1000.00"
        assert s["solidarity_surcharge"] == "55.00"
        assert s["total_tax"] == "1055.00"


class TestFrRouting:
    async def test_fr_returns_pfu_summary_with_30_percent_breakdown(
        self, client: AsyncClient
    ):
        resp = await client.post(
            "/api/v1/tax/report/FR",
            json={
                "disposals": [
                    _disposal(proceeds="6000", cost="5000"),
                ]
            },
        )

        assert resp.status_code == 200
        s = resp.json()["summary"]
        assert s["net_gain"] == "1000.00"
        assert s["income_tax"] == "128.00"
        assert s["social_charges"] == "172.00"
        assert s["total_tax"] == "300.00"


# ---------------------------------------------------------------------------
# Lowercase / casing
# ---------------------------------------------------------------------------


class TestCaseInsensitive:
    async def test_lowercase_code_normalises_in_response(
        self, client: AsyncClient
    ):
        resp = await client.post(
            "/api/v1/tax/report/us",
            json={"disposals": []},
        )

        assert resp.status_code == 200
        # The route uppercases the slug for the response so callers can
        # echo it back to the user without re-normalising.
        assert resp.json()["jurisdiction"] == "US"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class TestUnknownJurisdiction:
    async def test_unknown_code_returns_400_with_supported_list(
        self, client: AsyncClient
    ):
        resp = await client.post(
            "/api/v1/tax/report/ZZ",
            json={"disposals": []},
        )

        assert resp.status_code == 400
        body = resp.json()
        # Detail surfaces the dispatcher error so the client can repair.
        assert "ZZ" in body["detail"]
        for code in ("US", "GB", "DE", "FR"):
            assert code in body["detail"]


class TestInvalidPayload:
    async def test_non_decimal_proceeds_rejected_by_pydantic(
        self, client: AsyncClient
    ):
        resp = await client.post(
            "/api/v1/tax/report/US",
            json={
                "disposals": [_disposal(proceeds="abc", cost="100")],
            },
        )

        # Pydantic returns 422 for validator failures.
        assert resp.status_code == 422

    async def test_acquired_after_disposed_rejected_at_taxable_layer(
        self, client: AsyncClient
    ):
        resp = await client.post(
            "/api/v1/tax/report/US",
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


# ---------------------------------------------------------------------------
# Empty
# ---------------------------------------------------------------------------


class TestEmpty:
    @pytest.mark.parametrize("code", ["US", "GB", "DE", "FR"])
    async def test_empty_disposals_returns_200_with_zero_summary(
        self, client: AsyncClient, code: str
    ):
        resp = await client.post(
            f"/api/v1/tax/report/{code}",
            json={"disposals": []},
        )

        assert resp.status_code == 200
        assert resp.json()["jurisdiction"] == code
