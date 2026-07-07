"""Focused unit tests for the self-contained legal-acceptance module.

Covers the three core scenarios required by the spec, plus auth and store
behavior. Each test builds an isolated FastAPI app with a fresh in-memory
store so there is zero cross-test state and no DB dependency.
"""

from __future__ import annotations

import asyncio
import copy
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from fastapi import Depends, FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from engine.api.auth.dependency import get_current_user
from engine.api.legal import (
    LEGAL_ACCEPTANCE_REQUIRED,
    LEGAL_VERSION_MISMATCH,
    AcceptStatusResponse,
    InMemoryAcceptanceStore,
    LegalAcceptance,
    get_acceptance_store,
    require_legal_acceptance,
)
from engine.api.legal import router as legal_router
from engine.config import settings
from engine.db.models import User

DEFAULT_VERSION = "1.0.0"
BUMPED_VERSION = "2.0.0"


def _make_user() -> User:
    return User(
        id=uuid.uuid4(),
        email="legal-test@example.com",
        display_name="Legal Tester",
        is_active=True,
        role="user",
        auth_provider="local",
    )


@pytest.fixture
def store() -> InMemoryAcceptanceStore:
    return InMemoryAcceptanceStore()


@pytest.fixture
def user() -> User:
    """A fresh, isolated authenticated user for each test."""
    return _make_user()


@pytest.fixture(autouse=True)
def current_version(monkeypatch: pytest.MonkeyPatch) -> str:
    """Pin the configured document version for deterministic tests."""
    monkeypatch.setattr(settings, "legal_terms_version", DEFAULT_VERSION, raising=False)
    return DEFAULT_VERSION


def _build_app(store: InMemoryAcceptanceStore, user: User | None) -> FastAPI:
    """Construct an isolated app mounting the legal router plus a dummy
    route gated by :func:`require_legal_acceptance`.

    ``user=None`` simulates an unauthenticated request (``get_current_user``
    raises 401, mirroring real behaviour when no credential is present).
    """
    app = FastAPI()
    app.include_router(legal_router)

    @app.get("/api/v1/_protected")
    async def _protected(
        acceptance: LegalAcceptance = Depends(require_legal_acceptance),
    ) -> dict[str, str]:
        return {"ok": "true", "version": acceptance.document_version}

    app.dependency_overrides[get_acceptance_store] = lambda: store
    if user is None:

        async def _no_auth() -> User:
            raise HTTPException(status_code=401, detail="Authentication required")

        app.dependency_overrides[get_current_user] = _no_auth
    else:
        app.dependency_overrides[get_current_user] = lambda: user
    return app


@pytest.fixture
async def client_factory(store: InMemoryAcceptanceStore):
    """Returns a factory that builds an AsyncClient bound to an app for a
    given user (``None`` => unauthenticated)."""

    async def _make(user: User | None) -> AsyncIterator[AsyncClient]:
        app = _build_app(store, user)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            yield ac

    return _make


class TestRequireLegalAcceptance:
    """The dependency is the security-critical surface — cover it densely."""

    async def test_rejects_when_not_accepted(
        self, store: InMemoryAcceptanceStore, client_factory, user: User
    ) -> None:
        async for ac in client_factory(user):
            resp = await ac.get("/api/v1/_protected")
        assert resp.status_code == 403
        detail = resp.json()["detail"]
        assert detail["code"] == LEGAL_ACCEPTANCE_REQUIRED
        assert detail["current_version"] == DEFAULT_VERSION
        assert detail["accepted_version"] is None

    async def test_passes_after_acceptance(
        self, store: InMemoryAcceptanceStore, client_factory, user: User
    ) -> None:
        async for ac in client_factory(user):
            accept = await ac.post(
                "/api/v1/legal/accept", json={"document_version": DEFAULT_VERSION}
            )
            assert accept.status_code == 200
            protected = await ac.get("/api/v1/_protected")
        assert protected.status_code == 200
        body = protected.json()
        assert body == {"ok": "true", "version": DEFAULT_VERSION}

    async def test_version_mismatch_requires_re_acceptance(
        self,
        store: InMemoryAcceptanceStore,
        client_factory,
        user: User,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 1. Accept the current (1.0.0) document.
        async for ac in client_factory(user):
            await ac.post("/api/v1/legal/accept", json={"document_version": DEFAULT_VERSION})
            assert (await ac.get("/api/v1/_protected")).status_code == 200

        # 2. Operator publishes a new version -> re-acceptance required.
        monkeypatch.setattr(settings, "legal_terms_version", BUMPED_VERSION, raising=False)

        async for ac in client_factory(user):
            blocked = await ac.get("/api/v1/_protected")
            assert blocked.status_code == 403
            detail = blocked.json()["detail"]
            assert detail["code"] == LEGAL_ACCEPTANCE_REQUIRED
            assert detail["current_version"] == BUMPED_VERSION
            assert detail["accepted_version"] == DEFAULT_VERSION

            # Status reflects the stale acceptance.
            status_resp = await ac.get("/api/v1/legal/status")
            assert status_resp.status_code == 200
            status_body = status_resp.json()
            assert status_body["accepted"] is False
            assert status_body["needs_acceptance"] is True
            assert status_body["current_version"] == BUMPED_VERSION
            assert status_body["accepted_version"] == DEFAULT_VERSION

            # Submitting the now-stale version is rejected with a clear code.
            stale = await ac.post(
                "/api/v1/legal/accept", json={"document_version": DEFAULT_VERSION}
            )
            assert stale.status_code == 409
            assert stale.json()["detail"]["code"] == LEGAL_VERSION_MISMATCH

            # 3. Accepting the new version unblocks the protected route.
            reaccept = await ac.post(
                "/api/v1/legal/accept", json={"document_version": BUMPED_VERSION}
            )
            assert reaccept.status_code == 200
            assert (await ac.get("/api/v1/_protected")).status_code == 200

    async def test_rejects_unauthenticated_requests(
        self, store: InMemoryAcceptanceStore, client_factory
    ) -> None:
        # No authenticated user -> auth fails before acceptance is checked.
        async for ac in client_factory(None):
            assert (await ac.get("/api/v1/_protected")).status_code == 401
            assert (
                await ac.post("/api/v1/legal/accept", json={"document_version": DEFAULT_VERSION})
            ).status_code == 401
            assert (await ac.get("/api/v1/legal/status")).status_code == 401


class TestAcceptEndpoint:
    async def test_records_and_returns_acceptance(
        self, store: InMemoryAcceptanceStore, client_factory, user: User
    ) -> None:
        async for ac in client_factory(user):
            resp = await ac.post(
                "/api/v1/legal/accept", json={"document_version": DEFAULT_VERSION}
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["accepted"] is True
        acceptance = body["acceptance"]
        assert acceptance["user_id"] == str(user.id)
        assert acceptance["document_version"] == DEFAULT_VERSION
        # accepted_at is a parseable, timezone-aware UTC timestamp.
        parsed = datetime.fromisoformat(acceptance["accepted_at"])
        assert parsed.tzinfo is not None
        # The store actually holds the record for this user.
        stored = await store.get(str(user.id))
        assert stored is not None
        assert stored.document_version == DEFAULT_VERSION

    async def test_rejects_mismatched_version(
        self, store: InMemoryAcceptanceStore, client_factory, user: User
    ) -> None:
        async for ac in client_factory(user):
            resp = await ac.post("/api/v1/legal/accept", json={"document_version": "9.9.9"})
        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert detail["code"] == LEGAL_VERSION_MISMATCH
        assert detail["current_version"] == DEFAULT_VERSION
        assert detail["submitted_version"] == "9.9.9"

    async def test_rejects_empty_version(
        self, store: InMemoryAcceptanceStore, client_factory, user: User
    ) -> None:
        async for ac in client_factory(user):
            resp = await ac.post("/api/v1/legal/accept", json={"document_version": ""})
        assert resp.status_code == 422

    async def test_rejects_unknown_fields(
        self, store: InMemoryAcceptanceStore, client_factory, user: User
    ) -> None:
        async for ac in client_factory(user):
            resp = await ac.post(
                "/api/v1/legal/accept",
                json={"document_version": DEFAULT_VERSION, "extra": "nope"},
            )
        assert resp.status_code == 422

    async def test_is_idempotent_for_current_version(
        self, store: InMemoryAcceptanceStore, client_factory, user: User
    ) -> None:
        async for ac in client_factory(user):
            first = await ac.post(
                "/api/v1/legal/accept", json={"document_version": DEFAULT_VERSION}
            )
            second = await ac.post(
                "/api/v1/legal/accept", json={"document_version": DEFAULT_VERSION}
            )
        assert first.status_code == 200
        assert second.status_code == 200
        # Re-accepting refreshes accepted_at to a later (>=) timestamp.
        first_ts = datetime.fromisoformat(first.json()["acceptance"]["accepted_at"])
        second_ts = datetime.fromisoformat(second.json()["acceptance"]["accepted_at"])
        assert second_ts >= first_ts


class TestStatusEndpoint:
    async def test_status_with_no_acceptance(
        self, store: InMemoryAcceptanceStore, client_factory, user: User
    ) -> None:
        async for ac in client_factory(user):
            resp = await ac.get("/api/v1/legal/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body == {
            "accepted": False,
            "current_version": DEFAULT_VERSION,
            "accepted_version": None,
            "needs_acceptance": True,
        }

    async def test_status_after_acceptance(
        self, store: InMemoryAcceptanceStore, client_factory, user: User
    ) -> None:
        async for ac in client_factory(user):
            await ac.post("/api/v1/legal/accept", json={"document_version": DEFAULT_VERSION})
            resp = await ac.get("/api/v1/legal/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["accepted"] is True
        assert body["needs_acceptance"] is False
        assert body["accepted_version"] == DEFAULT_VERSION

    async def test_status_reflects_version_bump(
        self,
        store: InMemoryAcceptanceStore,
        client_factory,
        user: User,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async for ac in client_factory(user):
            await ac.post("/api/v1/legal/accept", json={"document_version": DEFAULT_VERSION})
        monkeypatch.setattr(settings, "legal_terms_version", BUMPED_VERSION, raising=False)
        async for ac in client_factory(user):
            resp = await ac.get("/api/v1/legal/status")
        body = resp.json()
        assert body["accepted"] is False
        assert body["needs_acceptance"] is True
        assert body["current_version"] == BUMPED_VERSION
        assert body["accepted_version"] == DEFAULT_VERSION


class TestInMemoryAcceptanceStore:
    async def test_record_and_get(self, store: InMemoryAcceptanceStore) -> None:
        acceptance = await store.record("user-1", "1.0.0")
        assert acceptance.user_id == "user-1"
        assert acceptance.document_version == "1.0.0"
        got = await store.get("user-1")
        assert got is not None
        assert got.document_version == "1.0.0"

    async def test_get_missing_returns_none(self, store: InMemoryAcceptanceStore) -> None:
        assert await store.get("nobody") is None

    async def test_get_returns_defensive_copy(self, store: InMemoryAcceptanceStore) -> None:
        await store.record("user-1", "1.0.0")
        got = await store.get("user-1")
        assert got is not None
        got.document_version = "tampered"
        # Internal state is untouched.
        again = await store.get("user-1")
        assert again is not None
        assert again.document_version == "1.0.0"

    async def test_record_overwrites_latest(self, store: InMemoryAcceptanceStore) -> None:
        await store.record("user-1", "1.0.0")
        await store.record("user-1", "2.0.0")
        got = await store.get("user-1")
        assert got is not None
        assert got.document_version == "2.0.0"

    async def test_clear(self, store: InMemoryAcceptanceStore) -> None:
        await store.record("user-1", "1.0.0")
        await store.clear("user-1")
        assert await store.get("user-1") is None
        # Clearing a missing user is a no-op.
        await store.clear("never-existed")

    async def test_reset(self, store: InMemoryAcceptanceStore) -> None:
        await store.record("user-1", "1.0.0")
        await store.record("user-2", "1.0.0")
        await store.reset()
        assert await store.get("user-1") is None
        assert await store.get("user-2") is None

    async def test_concurrent_records_are_serialised(
        self, store: InMemoryAcceptanceStore
    ) -> None:
        # The store is guarded by an ``asyncio.Lock``; many concurrent
        # coroutines recording distinct users must all land safely without
        # dropping or corrupting entries.
        async def _write(uid: int) -> None:
            await store.record(f"user-{uid}", "1.0.0")

        await asyncio.gather(*(_write(i) for i in range(50)))
        for i in range(50):
            assert await store.get(f"user-{i}") is not None


class TestLegalAcceptanceModel:
    def test_defaults_accepted_at_to_utc_now(self) -> None:
        before = datetime.now(tz=UTC)
        acceptance = LegalAcceptance(user_id="u", document_version="1.0.0")
        after = datetime.now(tz=UTC)
        assert acceptance.accepted_at >= before
        assert acceptance.accepted_at <= after
        assert acceptance.accepted_at.tzinfo is not None

    def test_extra_fields_forbidden(self) -> None:
        import pydantic

        with pytest.raises(pydantic.ValidationError):
            LegalAcceptance(user_id="u", document_version="1.0.0", surprise="bad")  # type: ignore[call-arg]

    def test_round_trip_is_independent_of_caller_mutation(self) -> None:
        original = LegalAcceptance(user_id="u", document_version="1.0.0")
        snapshot = copy.deepcopy(original)
        original.user_id = "changed"
        # Demonstrates model is mutable but the store returns copies, so this
        # pattern is safe only against store-provided instances (see store tests).
        assert snapshot.user_id == "u"


class TestStatusResponseModel:
    def test_accept_status_response_defaults(self) -> None:
        resp = AcceptStatusResponse(accepted=False, current_version="1.0.0", accepted_version=None)
        # needs_acceptance is derived by the route, not the model — but the
        # field must accept the value the route produces.
        resp = AcceptStatusResponse(
            accepted=False, current_version="1.0.0", accepted_version=None, needs_acceptance=True
        )
        assert resp.needs_acceptance is True
