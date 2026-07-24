"""Security-hardening tests for the OAuth authorize-URL / state surface in
``engine/api/routes/auth.py``.

These tests pin two defence-in-depth guards added to the
:func:`authorize_provider` route:

* :func:`validate_authorize_url` -- rejects a non-HTTPS scheme AND any C0/C1
  control character (CRLF / NUL / DEL ...) in the provider-built authorize
  URL, so a hostile or misconfigured provider can never smuggle
  response-splitting / header-injection bytes into the ``authorize_url`` the
  route returns to the browser (or, transitively, into a ``Location``
  redirect).

* :func:`validate_oauth_state` -- rejects an empty or control-character-laden
  ``state`` token returned by a provider. The route mints its own state, but a
  provider MAY return an AUTHORITATIVE value that is then persisted in the
  ``oauth_state_{provider}`` session cookie; an empty value would let the
  callback's ``compare_digest`` silently pass against a missing/empty cookie
  (defeating CSRF protection), and a CRLF-laden value would let a hostile
  provider inject cookie / header content into the ``Set-Cookie``.

Critically, both guards run on EVERY URL-building branch (the
``get_authorize_url_with_state`` path, the tuple-returning
``get_authorize_url`` fallback, AND the previously-unvalidated plain-string
``else`` fallback), so the test class :class:`TestRouteBranchCoverage`
exercises each accessor to prove no branch escapes validation.

The tests cover:

* direct unit coverage of both validators (accept/reject + non-reflection);
* route-level CRLF-injection vectors through every accessor branch;
* the non-HTTPS-scheme rejection;
* the previously-unvalidated ``else`` state path (the regression that prompted
  the fix); and
* a benign regression proving valid providers are unaffected.

All tests are hermetic: a stub provider behind a mock registry drives the
route via an :class:`httpx.ASGITransport`.
"""

from __future__ import annotations

from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from engine.api.auth.dependency import get_current_user
from engine.api.routes.auth import (
    authorize_provider,
    validate_authorize_url,
    validate_oauth_state,
)
from engine.app import create_app
from engine.deps import get_db
from tests.conftest import _fake_authenticated_user


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
def _boot_app_with_provider(provider) -> MagicMock:
    """Create an app whose auth registry returns ``provider`` for every name.

    Mirrors the harness in ``test_auth_authorize_url_coverage.py``. The
    authorize route neither reads from the DB nor requires an authenticated
    user, so the ``get_db`` / ``get_current_user`` overrides are harmless
    defaults that keep the app fully bootable.
    """

    async def override_get_db():
        yield None

    app = create_app()
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = _fake_authenticated_user

    registry = MagicMock()
    registry.get.return_value = provider
    app.state.auth_registry = registry
    return app


def _state_from_url(url: str) -> str | None:
    return parse_qs(urlparse(url).query).get("state", [None])[0]


class _WithStateProvider:
    """Stub provider exposing the typed ``(url, state)`` accessor.

    Returns whatever ``url`` / ``state`` were injected at construction so a
    test can drive a hostile value through the authoritative-state path.
    """

    name = "withstate"

    def __init__(self, *, url: str, state: str) -> None:
        self._url = url
        self._state = state

    def get_authorize_url_with_state(self, state: str = "") -> tuple[str, str]:
        return self._url, self._state


class _TupleFallbackProvider:
    """Stub provider exposing ``get_authorize_url`` returning a tuple."""

    name = "tupfallback"

    def __init__(self, *, url: str, state: str) -> None:
        self._url = url
        self._state = state

    def get_authorize_url(self, state: str = "") -> tuple[str, str]:
        return self._url, self._state


class _StringFallbackProvider:
    """Stub provider exposing ``get_authorize_url`` returning a plain string.

    This is the previously-unvalidated ``else`` branch: the route used to
    accept the URL verbatim and keep the route-minted state, never validating
    the returned URL. It now flows through :func:`validate_authorize_url`.
    """

    name = "strfallback"

    def __init__(self, *, url: str) -> None:
        self._url = url

    def get_authorize_url(self, state: str = "") -> str:
        return self._url


# ===========================================================================
# validate_authorize_url: direct unit coverage
# ===========================================================================
class TestValidateAuthorizeUrl:
    def test_accepts_valid_https_url(self):
        url = "https://github.com/login/oauth/authorize?state=abc"
        assert validate_authorize_url(url) == url

    def test_scheme_check_is_case_insensitive(self):
        # ``HTTPS`` / ``Https`` are accepted just like ``https``.
        assert validate_authorize_url("HTTPS://idp.example.com/auth") == "HTTPS://idp.example.com/auth"
        assert validate_authorize_url("Https://idp.example.com/auth").startswith("Https://")

    def test_rejects_empty_string(self):
        with pytest.raises(HTTPException) as exc:
            validate_authorize_url("")
        assert exc.value.status_code == 500
        assert "authorize URL" in exc.value.detail

    @pytest.mark.parametrize("bad", [None, 123, 4.5, [], {}, object()])
    def test_rejects_non_string(self, bad):
        with pytest.raises(HTTPException) as exc:
            validate_authorize_url(bad)  # type: ignore[arg-type]
        assert exc.value.status_code == 500

    @pytest.mark.parametrize(
        "scheme",
        ["http://idp.example.com/auth", "ftp://idp/auth", "javascript:alert(1)", "//idp/auth"],
    )
    def test_rejects_non_https_scheme(self, scheme):
        with pytest.raises(HTTPException) as exc:
            validate_authorize_url(scheme)
        assert exc.value.status_code == 500
        assert "https" in exc.value.detail.lower()

    @pytest.mark.parametrize(
        "payload",
        [
            # Classic response-splitting / header-injection vector.
            "https://idp.example.com/auth?state=x\r\nSet-Cookie: admin=1",
            "https://idp.example.com/auth?state=x\nSet-Cookie: admin=1",
            "https://idp.example.com/auth?state=x\rSet-Cookie: admin=1",
            # NUL truncation / smuggling.
            "https://idp.example.com/auth?state=evil\x00safe",
            # C1 control set (DEL + 0x80-0x9f).
            "https://idp.example.com/auth?state=x\x7f",
            "https://idp.example.com/auth?state=x\x9f",
            # Horizontal tab / vertical tab / form feed are C0 control chars too.
            "https://idp.example.com/auth?state=x\ty",
            "https://idp.example.com/auth?state=x\x0by",
            "https://idp.example.com/auth?state=x\x0cy",
        ],
    )
    def test_rejects_control_characters(self, payload):
        with pytest.raises(HTTPException) as exc:
            validate_authorize_url(payload)
        assert exc.value.status_code == 500
        assert "control" in exc.value.detail.lower()

    def test_rejects_non_https_before_accepting_https_with_control_chars(self):
        # A non-HTTPS scheme with CRLF: scheme check fires first (ordering
        # guarantee documented in the function docstring).
        with pytest.raises(HTTPException) as exc:
            validate_authorize_url("http://idp.example.com/auth\r\nX: y")
        assert "https" in exc.value.detail.lower()

    def test_hostile_value_is_never_reflected(self):
        # The rejection detail must never echo the attacker-controlled bytes
        # back to the client.
        hostile = "https://idp.example.com/auth?state=x\r\nEvil-Header: 1"
        with pytest.raises(HTTPException) as exc:
            validate_authorize_url(hostile)
        assert "Evil-Header" not in str(exc.value.detail)


# ===========================================================================
# validate_oauth_state: direct unit coverage
# ===========================================================================
class TestValidateOauthState:
    def test_accepts_valid_state(self):
        state = "csrf-token-abc123"
        assert validate_oauth_state(state) == state

    def test_rejects_empty_string(self):
        with pytest.raises(HTTPException) as exc:
            validate_oauth_state("")
        assert exc.value.status_code == 500
        assert "state" in exc.value.detail.lower()

    @pytest.mark.parametrize("bad", [None, 123, 4.5, [], {}, object()])
    def test_rejects_non_string(self, bad):
        with pytest.raises(HTTPException) as exc:
            validate_oauth_state(bad)  # type: ignore[arg-type]
        assert exc.value.status_code == 500

    @pytest.mark.parametrize(
        "payload",
        [
            "state\r\nSet-Cookie: admin=1",
            "state\nSet-Cookie: admin=1",
            "state\rEvil",
            "state\x00trunc",
            "state\x7f",
            "state\x9f",
        ],
    )
    def test_rejects_control_characters(self, payload):
        with pytest.raises(HTTPException) as exc:
            validate_oauth_state(payload)
        assert exc.value.status_code == 500
        assert "control" in exc.value.detail.lower()

    def test_hostile_value_is_never_reflected(self):
        hostile = "state\r\nSet-Cookie: admin=1"
        with pytest.raises(HTTPException) as exc:
            validate_oauth_state(hostile)
        assert "Set-Cookie" not in str(exc.value.detail)


# ===========================================================================
# Route-level CRLF injection vectors (every accessor branch)
# ===========================================================================
class TestRouteCrlfInjection:
    """A hostile provider must never get CRLF bytes into the JSON response
    body or the session cookie, regardless of which accessor it implements."""

    @pytest.mark.asyncio
    async def test_crlf_in_url_through_with_state_accessor_is_rejected(self):
        provider = _WithStateProvider(
            url="https://idp.example.com/auth?state=x\r\nSet-Cookie: admin=1",
            state="clean-state",
        )
        app = _boot_app_with_provider(provider)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/v1/auth/withstate/authorize")

        assert resp.status_code == 500
        # The smuggled header / cookie bytes never reach the client.
        assert "Set-Cookie: admin=1" not in resp.text
        assert "\r\n" not in resp.text

    @pytest.mark.asyncio
    async def test_non_https_url_through_with_state_accessor_is_rejected(self):
        provider = _WithStateProvider(
            url="http://idp.example.com/auth?state=x",
            state="clean-state",
        )
        app = _boot_app_with_provider(provider)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/v1/auth/withstate/authorize")

        assert resp.status_code == 500
        assert "https" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_crlf_in_state_through_with_state_accessor_is_rejected(self):
        hostile_state = "csrf\r\nSet-Cookie: admin=1"
        provider = _WithStateProvider(
            url="https://idp.example.com/auth",
            state=hostile_state,
        )
        app = _boot_app_with_provider(provider)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/v1/auth/withstate/authorize")

        assert resp.status_code == 500
        # The hostile state never reaches the body nor a Set-Cookie header.
        assert "Set-Cookie: admin=1" not in resp.text
        assert "oauth_state_withstate" not in resp.headers.get_list("set-cookie")

    @pytest.mark.asyncio
    async def test_empty_state_through_with_state_accessor_is_rejected(self):
        provider = _WithStateProvider(url="https://idp.example.com/auth", state="")
        app = _boot_app_with_provider(provider)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/v1/auth/withstate/authorize")

        assert resp.status_code == 500
        assert "state" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_crlf_in_url_through_tuple_fallback_is_rejected(self):
        provider = _TupleFallbackProvider(
            url="https://idp.example.com/auth\r\nX-Smuggled: yes",
            state="clean-state",
        )
        app = _boot_app_with_provider(provider)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/v1/auth/tupfallback/authorize")

        assert resp.status_code == 500
        assert "X-Smuggled" not in resp.text

    @pytest.mark.asyncio
    async def test_crlf_in_state_through_tuple_fallback_is_rejected(self):
        provider = _TupleFallbackProvider(
            url="https://idp.example.com/auth",
            state="evil\r\nSet-Cookie: admin=1",
        )
        app = _boot_app_with_provider(provider)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/v1/auth/tupfallback/authorize")

        assert resp.status_code == 500
        assert "Set-Cookie: admin=1" not in resp.text


# ===========================================================================
# Route: the previously-unvalidated plain-string ``else`` branch
# ===========================================================================
class TestRouteStringFallbackValidation:
    """The plain-string ``get_authorize_url`` fallback used to bypass URL
    validation entirely (the route kept its own minted state and surfaced the
    returned URL verbatim). It now flows through
    :func:`validate_authorize_url`, closing that path to CRLF / non-HTTPS
    injection."""

    @pytest.mark.asyncio
    async def test_crlf_in_url_through_string_fallback_is_rejected(self):
        provider = _StringFallbackProvider(
            url="https://idp.example.com/auth?state=x\r\nSet-Cookie: admin=1",
        )
        app = _boot_app_with_provider(provider)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/v1/auth/strfallback/authorize")

        assert resp.status_code == 500
        assert "Set-Cookie: admin=1" not in resp.text

    @pytest.mark.asyncio
    async def test_non_https_through_string_fallback_is_rejected(self):
        provider = _StringFallbackProvider(url="http://idp.example.com/auth")
        app = _boot_app_with_provider(provider)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/v1/auth/strfallback/authorize")

        assert resp.status_code == 500
        assert "https" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_nul_in_url_through_string_fallback_is_rejected(self):
        provider = _StringFallbackProvider(
            url="https://idp.example.com/auth?state=evil\x00safe",
        )
        app = _boot_app_with_provider(provider)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/v1/auth/strfallback/authorize")

        assert resp.status_code == 500
        assert "evil" not in resp.text


# ===========================================================================
# Route: benign regression -- valid providers keep working on every branch
# ===========================================================================
class TestRouteBenignRegression:
    """The hardening must not break the legitimate flow on any branch."""

    @pytest.mark.asyncio
    async def test_valid_with_state_accessor_succeeds(self):
        provider = _WithStateProvider(
            url="https://idp.example.com/auth?state=provider-state",
            state="provider-state",
        )
        app = _boot_app_with_provider(provider)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/v1/auth/withstate/authorize")

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["state"] == "provider-state"
        assert data["authorize_url"].startswith("https://")
        # The authoritative state is persisted for callback validation.
        assert resp.cookies.get("oauth_state_withstate") == "provider-state"

    @pytest.mark.asyncio
    async def test_valid_tuple_fallback_succeeds(self):
        provider = _TupleFallbackProvider(
            url="https://idp.example.com/auth?state=tup-state",
            state="tup-state",
        )
        app = _boot_app_with_provider(provider)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/v1/auth/tupfallback/authorize")

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["state"] == "tup-state"
        assert data["authorize_url"].startswith("https://")
        assert resp.cookies.get("oauth_state_tupfallback") == "tup-state"

    @pytest.mark.asyncio
    async def test_valid_string_fallback_succeeds(self):
        provider = _StringFallbackProvider(url="https://idp.example.com/auth")
        app = _boot_app_with_provider(provider)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/v1/auth/strfallback/authorize")

        assert resp.status_code == 200, resp.text
        data = resp.json()
        # String fallback keeps the route-minted state and embeds it in the URL
        # only when the provider did so itself; the key invariant is that some
        # non-empty state is persisted for the callback.
        assert data["state"]
        assert data["authorize_url"].startswith("https://")
        assert resp.cookies.get("oauth_state_strfallback") == data["state"]


# ===========================================================================
# Sanity: the route handler is the exported coroutine
# ===========================================================================
class TestAuthorizeProviderCallable:
    def test_authorize_provider_is_async(self):
        import inspect

        assert inspect.iscoroutinefunction(authorize_provider)
