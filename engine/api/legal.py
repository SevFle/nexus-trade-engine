"""Self-contained legal-acceptance dependency and endpoints.

This module provides a lightweight, **database-free** implementation of the
legal-document acceptance flow:

* :class:`LegalAcceptance` — pydantic record of a user's acceptance.
* :class:`InMemoryAcceptanceStore` — dict-backed store used until a real
  persistence layer is wired in. It implements :class:`AcceptanceStore` so it
  can be swapped for a SQLAlchemy-backed store later without touching the
  dependency or the routes.
* :func:`require_legal_acceptance` — FastAPI ``Depends()`` that rejects a
  request with HTTP 403 (``LEGAL_ACCEPTANCE_REQUIRED``) when the authenticated
  user has not accepted the current document version
  (``settings.legal_terms_version``).
* :data:`router` — ``POST /api/v1/legal/accept`` (record acceptance) and
  ``GET /api/v1/legal/status`` (check current acceptance).

The module is intentionally free of any DB / SQLAlchemy import so it can be
unit-tested in isolation. When the team is ready to persist acceptances, drop
in a store that satisfies :class:`AcceptanceStore` and override the
:func:`get_acceptance_store` dependency.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Protocol

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from engine.api.auth.dependency import get_current_user
from engine.config import settings
from engine.legal.disclaimers import (
    DisclaimerCategory,
    DisclaimerListResponse,
    RiskDisclosureResponse,
    build_disclaimer_list_response,
    get_risk_disclosure,
)

if TYPE_CHECKING:
    from engine.db.models import User

logger = structlog.get_logger()

# Stable error code surfaced in the 403 response body so clients can branch on
# it without parsing free-text messages.
LEGAL_ACCEPTANCE_REQUIRED = "LEGAL_ACCEPTANCE_REQUIRED"
# Submitted version does not match the current document version.
LEGAL_VERSION_MISMATCH = "LEGAL_VERSION_MISMATCH"


def _now() -> datetime:
    return datetime.now(tz=UTC)


class LegalAcceptance(BaseModel):
    """A user's recorded acceptance of a legal document version."""

    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(description="Stable identifier of the accepting user.")
    document_version: str = Field(description="Document version that was accepted.")
    accepted_at: datetime = Field(
        default_factory=_now,
        description="UTC timestamp at which the acceptance was recorded.",
    )


class AcceptRequest(BaseModel):
    """Request body for ``POST /api/v1/legal/accept``."""

    model_config = ConfigDict(extra="forbid")

    document_version: str = Field(
        ...,
        min_length=1,
        description=(
            "Version of the legal document being accepted. Must match the current version."
        ),
    )


class AcceptResponse(BaseModel):
    """Response body for ``POST /api/v1/legal/accept``."""

    accepted: bool
    acceptance: LegalAcceptance


class AcceptStatusResponse(BaseModel):
    """Response body for ``GET /api/v1/legal/status``."""

    accepted: bool = Field(description="True iff the user accepted the *current* version.")
    current_version: str
    accepted_version: str | None = Field(
        default=None,
        description="Version the user last accepted, if any (may be stale).",
    )
    needs_acceptance: bool = Field(
        default=True,
        description=(
            "True when the user must (re-)accept the current document version. "
            "Defaults to True so the model is safe to construct before the "
            "route has derived the value."
        ),
    )


class AcceptanceStore(Protocol):
    """Storage contract for legal acceptances.

    The default implementation (:class:`InMemoryAcceptanceStore`) keeps
    everything in process memory. A SQLAlchemy-backed store can implement this
    protocol and be returned from :func:`get_acceptance_store` without
    changing any route or dependency code.

    All methods are coroutines so the store can use an :class:`asyncio.Lock`
    for safe serialisation within the request-handling event loop.
    """

    async def record(self, user_id: str, document_version: str) -> LegalAcceptance: ...

    async def get(self, user_id: str) -> LegalAcceptance | None: ...

    async def clear(self, user_id: str) -> None: ...

    async def reset(self) -> None: ...


class InMemoryAcceptanceStore:
    """Async-safe dict-backed acceptance store.

    Keyed by ``user_id``; only the latest acceptance per user is retained,
    which mirrors the semantics of the dependency (only the latest version
    matters for gating). All mutations and reads are serialised through an
    :class:`asyncio.Lock` so the store is safe to use from concurrent
    coroutine request handlers sharing one event loop.
    """

    def __init__(self) -> None:
        self._data: dict[str, LegalAcceptance] = {}
        self._lock = asyncio.Lock()

    async def record(self, user_id: str, document_version: str) -> LegalAcceptance:
        acceptance = LegalAcceptance(
            user_id=user_id,
            document_version=document_version,
            accepted_at=_now(),
        )
        async with self._lock:
            self._data[user_id] = acceptance
        return acceptance

    async def get(self, user_id: str) -> LegalAcceptance | None:
        async with self._lock:
            # Return a defensive copy so callers cannot mutate internal state.
            stored = self._data.get(user_id)
            if stored is None:
                return None
            return stored.model_copy(deep=True)

    async def clear(self, user_id: str) -> None:
        async with self._lock:
            self._data.pop(user_id, None)

    async def reset(self) -> None:
        async with self._lock:
            self._data.clear()


# Process-wide default store. Override via the ``get_acceptance_store``
# dependency (e.g. in tests) to isolate state.
_default_store = InMemoryAcceptanceStore()


def get_acceptance_store() -> AcceptanceStore:
    """FastAPI dependency yielding the active acceptance store.

    Override this in tests, or when swapping in a DB-backed store, via
    ``app.dependency_overrides[get_acceptance_store]``.
    """
    return _default_store


def current_legal_version() -> str:
    """Return the document version users are currently required to accept."""
    return settings.legal_terms_version


def _is_currently_accepted(latest: LegalAcceptance | None, current_version: str) -> bool:
    """True iff there is an acceptance record matching the current version."""
    return latest is not None and latest.document_version == current_version


async def require_legal_acceptance(
    user: User = Depends(get_current_user),  # noqa: B008
    store: AcceptanceStore = Depends(get_acceptance_store),  # noqa: B008
) -> LegalAcceptance:
    """Reject the request unless the user accepted the current legal version.

    Authentication is delegated to :func:`get_current_user`, which raises
    HTTP 401 itself when no valid credential is present. When authenticated
    but the user has not accepted ``settings.legal_terms_version`` (either no
    record at all, or a record for an older version), this raises HTTP 403
    with a structured body whose ``code`` is
    :data:`LEGAL_ACCEPTANCE_REQUIRED`.

    On success the latest :class:`LegalAcceptance` is returned so downstream
    routes can inspect the accepted version without re-querying the store.
    """
    current_version = current_legal_version()
    latest = await store.get(str(user.id))

    if _is_currently_accepted(latest, current_version):
        # ``_is_currently_accepted`` guarantees ``latest`` is not None here.
        assert latest is not None
        return latest

    accepted_version = latest.document_version if latest is not None else None
    logger.info(
        "legal.acceptance_required",
        user_id=str(user.id),
        current_version=current_version,
        accepted_version=accepted_version,
    )
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={
            "code": LEGAL_ACCEPTANCE_REQUIRED,
            "message": "You must accept the current version of the legal document.",
            "current_version": current_version,
            "accepted_version": accepted_version,
        },
    )


router = APIRouter()


@router.post("/api/v1/legal/accept", response_model=AcceptResponse)
async def accept_legal_document(
    body: AcceptRequest,
    user: User = Depends(get_current_user),  # noqa: B008
    store: AcceptanceStore = Depends(get_acceptance_store),  # noqa: B008
) -> AcceptResponse:
    """Record the authenticated user's acceptance of the legal document.

    The submitted ``document_version`` must match the current version exposed
    by ``settings.legal_terms_version``; otherwise HTTP 409
    (:data:`LEGAL_VERSION_MISMATCH`) is returned so clients resync before
    accepting a stale document. Re-accepting the current version is idempotent
    and refreshes ``accepted_at``.
    """
    current_version = current_legal_version()
    if body.document_version != current_version:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": LEGAL_VERSION_MISMATCH,
                "message": "Submitted version does not match the current document version.",
                "current_version": current_version,
                "submitted_version": body.document_version,
            },
        )

    acceptance = await store.record(str(user.id), body.document_version)
    logger.info(
        "legal.acceptance_recorded",
        user_id=str(user.id),
        document_version=current_version,
    )
    return AcceptResponse(accepted=True, acceptance=acceptance)


@router.get("/api/v1/legal/status", response_model=AcceptStatusResponse)
async def get_legal_status(
    user: User = Depends(get_current_user),  # noqa: B008
    store: AcceptanceStore = Depends(get_acceptance_store),  # noqa: B008
) -> AcceptStatusResponse:
    """Report the authenticated user's legal-acceptance status.

    ``accepted`` is ``True`` only when the user accepted the *current*
    version; an acceptance of an older version yields ``accepted=False`` and
    ``needs_acceptance=True``.
    """
    current_version = current_legal_version()
    latest = await store.get(str(user.id))
    accepted = _is_currently_accepted(latest, current_version)
    return AcceptStatusResponse(
        accepted=accepted,
        current_version=current_version,
        accepted_version=latest.document_version if latest is not None else None,
        needs_acceptance=not accepted,
    )


@router.get("/api/legal/disclaimers", response_model=DisclaimerListResponse)
async def list_disclaimers(
    category: Annotated[
        DisclaimerCategory | None,
        Query(
            description=(
                "Optional filter narrowing the result to a single disclaimer "
                "category (trading_risk, wash_sale, tax_implications, general)."
            ),
        ),
    ] = None,
) -> DisclaimerListResponse:
    """Return all structured legal disclaimers, optionally filtered by category.

    This endpoint is **public** (no authentication required) because legal
    disclaimers and risk notices must be displayable before a user signs in or
    accepts terms — e.g. on pre-login notice screens and during onboarding.

    Without ``category`` every disclaimer is returned. With a valid category
    only that category's disclaimers are returned; an unknown category value
    is rejected with HTTP 422 by FastAPI's enum validation. ``categories`` in
    the response reflects the categories present in the returned list.
    """
    return build_disclaimer_list_response(category=category)


@router.get("/api/legal/risk-disclosures", response_model=RiskDisclosureResponse)
async def get_risk_disclosures() -> RiskDisclosureResponse:
    """Return detailed, structured risk-disclosure information.

    Public endpoint (no authentication). Combines a plain-language overview,
    a list of discrete risk factors, and the related structured disclaimers
    that elaborate on the most loss-relevant risk areas, so a client can
    render the full disclosure surface from a single request.
    """
    return get_risk_disclosure()
