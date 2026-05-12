"""Tax-report API route (gh#155).

Single entrypoint that takes a list of jurisdiction-neutral disposals
and returns the per-jurisdiction summary in JSON. The actual
aggregation lives in :mod:`engine.core.tax.reports`; this route is a
thin transport layer that handles auth, validation, and serialisation.

Why a single endpoint
---------------------
The dispatcher already routes ``code`` to the right summariser. Adding
one HTTP endpoint per jurisdiction would just duplicate the switch on
the API surface without adding behaviour. Operators get the right
shape of response by inspecting the ``code`` they sent.

Scope
-----
- US / GB / DE / FR (the dispatcher's currently-supported set).
- Auth required (every other authenticated route in the engine uses
  the same dependency).
- No persistence: callers re-submit the disposals they care about.
  Operators who want to persist annual tax inputs build a separate
  model layer on top.

Out of scope (explicit follow-ups)
----------------------------------
- CSV download endpoint (``Accept: text/csv``) — JSON only here.
- Per-jurisdiction carry-over state (only US implemented today).
- Multi-currency conversion.
"""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field, field_validator

from engine.api.auth.dependency import get_current_user
from engine.core.tax.reports import (
    TaxableDisposal,
    UnsupportedJurisdictionError,
    flatten_summary_to_csv,
    report_for_jurisdiction,
)
from engine.db.models import User

router = APIRouter()
logger = structlog.get_logger()


class DisposalRequest(BaseModel):
    """One disposal in the request payload. Money values are passed as
    strings to preserve ``Decimal`` precision through JSON."""

    description: str = Field(..., min_length=1, max_length=200)
    acquired: date
    disposed: date
    proceeds: str = Field(..., description="Decimal as string")
    cost: str = Field(..., description="Decimal as string")

    @field_validator("proceeds", "cost")
    @classmethod
    def _is_decimal(cls, value: str) -> str:
        try:
            Decimal(value)
        except Exception as exc:
            raise ValueError(f"not a valid decimal: {value!r}") from exc
        return value

    def to_taxable(self) -> TaxableDisposal:
        return TaxableDisposal(
            description=self.description,
            acquired=self.acquired,
            disposed=self.disposed,
            proceeds=Decimal(self.proceeds),
            cost=Decimal(self.cost),
        )


class TaxReportRequest(BaseModel):
    disposals: list[DisposalRequest] = Field(default_factory=list)


@router.post("/report/{code}")
async def tax_report(
    code: str,
    req: TaxReportRequest,
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Return the per-jurisdiction tax summary as a JSON dict.

    ``code`` matches the dispatcher's two-letter jurisdiction slug
    (case-insensitive): US, GB, DE, FR. Anything else returns 400.
    """
    try:
        disposals = [d.to_taxable() for d in req.disposals]
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        summary = report_for_jurisdiction(code, disposals)
    except UnsupportedJurisdictionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "jurisdiction": code.upper(),
        "summary": _to_json(summary),
    }


@router.post("/report/{code}/csv")
async def tax_report_csv(
    code: str,
    req: TaxReportRequest,
    user: User = Depends(get_current_user),
) -> Response:
    """Same dispatch as :func:`tax_report` but returns the summary as
    a 2-row CSV (header + values). Useful for spreadsheet round-trips
    and CPA workflows."""
    try:
        disposals = [d.to_taxable() for d in req.disposals]
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        summary = report_for_jurisdiction(code, disposals)
    except UnsupportedJurisdictionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    body = flatten_summary_to_csv(summary)
    filename = f"tax-report-{code.upper()}.csv"
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={"content-disposition": f'attachment; filename="{filename}"'},
    )


def _to_json(obj: Any) -> Any:
    """Walk a frozen-dataclass tree and emit a JSON-safe dict.

    Dataclasses are stringified field-by-field; ``Decimal`` and
    ``date`` are turned into JSON-friendly forms; enums surface as
    their value. Anything else falls through unchanged."""
    if is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: _to_json(getattr(obj, f.name)) for f in fields(obj)}
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, list | tuple):
        return [_to_json(x) for x in obj]
    return obj
