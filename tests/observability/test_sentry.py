"""Tests for engine.observability.sentry — setup, teardown and PII scrubbing."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from engine.observability.sentry import (
    REDACTED,
    close_sentry,
    scrub_event,
    setup_sentry,
)


class TestSetupSentry:
    """``setup_sentry`` must call ``sentry_sdk.init`` only when a DSN is set."""

    def test_noop_when_dsn_empty(self):
        with (
            patch("engine.observability.sentry.settings") as mock_settings,
            patch("sentry_sdk.init") as mock_init,
        ):
            mock_settings.sentry_dsn = ""
            setup_sentry()

        mock_init.assert_not_called()

    def test_inits_when_dsn_configured(self):
        with (
            patch("engine.observability.sentry.settings") as mock_settings,
            patch("sentry_sdk.init") as mock_init,
        ):
            mock_settings.sentry_dsn = "https://example@sentry.io/1"
            mock_settings.sentry_traces_sample_rate = 0.5
            setup_sentry()

        mock_init.assert_called_once()
        kwargs = mock_init.call_args.kwargs
        assert kwargs["dsn"] == "https://example@sentry.io/1"
        assert kwargs["traces_sample_rate"] == 0.5
        # Local frame variables must never be shipped to Sentry.
        assert kwargs["include_local_variables"] is False
        # Both error and transaction events are scrubbed.
        assert callable(kwargs["before_send"])
        assert callable(kwargs["before_send_transaction"])


class TestCloseSentry:
    """``close_sentry`` must flush + close the client only when initialised."""

    def test_noop_when_not_initialised(self):
        with (
            patch("sentry_sdk.is_initialized", return_value=False),
            patch("sentry_sdk.flush") as mock_flush,
        ):
            close_sentry()

        mock_flush.assert_not_called()

    def test_flush_and_close_when_initialised(self):
        mock_client = MagicMock()
        with (
            patch("sentry_sdk.is_initialized", return_value=True),
            patch("sentry_sdk.flush") as mock_flush,
            patch("sentry_sdk.get_client", return_value=mock_client),
        ):
            close_sentry()

        mock_flush.assert_called_once_with(timeout=2)
        mock_client.close.assert_called_once()

    def test_flush_timeout_is_2_seconds(self):
        mock_client = MagicMock()
        with (
            patch("sentry_sdk.is_initialized", return_value=True),
            patch("sentry_sdk.flush") as mock_flush,
            patch("sentry_sdk.get_client", return_value=mock_client),
        ):
            close_sentry()

        assert mock_flush.call_args.kwargs["timeout"] == 2


# ---------------------------------------------------------------------------
# Sensitive-key scrubbing
# ---------------------------------------------------------------------------

SENTITIVE_KEYS = pytest.mark.parametrize(
    "key",
    [
        "authorization",
        "Authorization",
        "password",
        "PASSWORD",
        "token",
        "access_token",
        "refresh-token",
        "id_token",
        "secret",
        "client_secret",
        "api_key",
        "apikey",
        "x-api-key",
        "X-Api-Key",
        "cookie",
        "set-cookie",
        "email",
        "user_email",
        "Email-Address",
    ],
    ids=lambda v: v.replace(" ", "_"),
)


class TestScrubEventKeys:
    """Sensitive keys are redacted wherever they appear in the event."""

    @SENTITIVE_KEYS
    def test_top_level_key_is_redacted(self, key):
        assert scrub_event({key: "super-secret-value"}) == {key: REDACTED}

    @SENTITIVE_KEYS
    def test_input_event_is_not_mutated(self, key):
        event = {key: "super-secret-value"}
        scrub_event(event)
        assert event == {key: "super-secret-value"}

    def test_innocuous_keys_are_preserved(self):
        event = {"message": "hello", "level": "error", "count": 3}
        assert scrub_event(event) == event

    def test_nested_dict_keys_are_redacted(self):
        event = {
            "request": {
                "headers": {
                    "Authorization": "Bearer abc.def.ghi1234567890",
                    "X-Api-Key": "sk_1234567890abcdef",
                    "Content-Type": "application/json",
                },
                "data": {"password": "hunter2", "username": "alice"},
            }
        }
        result = scrub_event(event)
        assert result["request"]["headers"]["Authorization"] == REDACTED
        assert result["request"]["headers"]["X-Api-Key"] == REDACTED
        assert result["request"]["headers"]["Content-Type"] == "application/json"
        assert result["request"]["data"]["password"] == REDACTED
        assert result["request"]["data"]["username"] == "alice"

    def test_lists_inside_event_are_walked(self):
        event = {
            "breadcrumbs": [
                {"data": {"token": "t0", "ok": "fine"}},
                {"data": {"cookie": "c1"}},
            ]
        }
        result = scrub_event(event)
        assert result["breadcrumbs"][0]["data"]["token"] == REDACTED
        assert result["breadcrumbs"][0]["data"]["ok"] == "fine"
        assert result["breadcrumbs"][1]["data"]["cookie"] == REDACTED

    def test_tuples_inside_event_are_walked(self):
        event = {"frames": ({"secret": "s"}, {"ok": True})}
        result = scrub_event(event)
        assert result["frames"][0]["secret"] == REDACTED
        assert result["frames"][1]["ok"] is True

    def test_deeply_nested_structure(self):
        event = {"contexts": {"trace": {"extra": {"meta": {"auth": {"token": "deep"}}}}}}
        assert (
            scrub_event(event)["contexts"]["trace"]["extra"]["meta"]["auth"]["token"] == REDACTED
        )


# ---------------------------------------------------------------------------
# Sensitive value patterns (free-form string scrubbing)
# ---------------------------------------------------------------------------


class TestScrubEventValues:
    """Secret-shaped *values* are masked even under innocuous keys."""

    def test_bearer_token_in_message(self):
        event = {"message": "fail: Authorization: Bearer eyJhbGci.eyJzdWIiS16chars.e30"}
        result = scrub_event(event)
        assert "Bearer eyJhbGci" not in result["message"]
        assert REDACTED in result["message"]

    def test_jwt_shaped_value(self):
        jwt = "aaaaaaaaaaaaaaaa.bbbbbbbbbbbbbbbb.cccccccccccccccc"
        event = {"extra": {"detail": f"token was {jwt}"}}
        result = scrub_event(event)
        assert jwt not in result["extra"]["detail"]
        assert REDACTED in result["extra"]["detail"]

    def test_prefixed_provider_secret(self):
        event = {"tags": {"note": "key=sk_live_1234567890abcdef"}}
        result = scrub_event(event)
        assert "sk_live_1234567890abcdef" not in result["tags"]["note"]
        assert REDACTED in result["tags"]["note"]

    def test_pem_block_is_redacted(self):
        pem = "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANB\n-----END PRIVATE KEY-----"
        event = {"extra": {"dump": pem}}
        result = scrub_event(event)
        assert "BEGIN PRIVATE KEY" not in result["extra"]["dump"]
        assert REDACTED in result["extra"]["dump"]

    def test_bytes_value_is_decoded_and_scrubbed(self):
        event = {
            "extra": {
                "blob": b"Authorization: Bearer aaaaaaaaaaaaaaaa.bbbbbbbbbbbbbbbb.cccccccccccccccc"
            }
        }
        result = scrub_event(event)
        assert result["extra"]["blob"] == f"Authorization: {REDACTED}"

    def test_non_dict_event_returned_unchanged(self):
        assert scrub_event(None) is None
        assert scrub_event("plain") == "plain"


# ---------------------------------------------------------------------------
# before_send / before_send_transaction wiring
# ---------------------------------------------------------------------------


class TestBeforeSendHooks:
    """The init hooks scrub events of every category."""

    def test_before_send_scrubs_request_headers(self):
        from engine.observability.sentry import _before_send

        event = {
            "request": {"headers": {"Authorization": "Bearer xyz"}},
            "extra": {"api_key": "abc"},
        }
        result = _before_send(event, {})
        assert result["request"]["headers"]["Authorization"] == REDACTED
        assert result["extra"]["api_key"] == REDACTED

    def test_before_send_transaction_scrubs_user_email(self):
        from engine.observability.sentry import _before_send_transaction

        event = {
            "transaction": "GET /api",
            "user": {"email": "alice@example.com", "id": "u1"},
        }
        result = _before_send_transaction(event, {})
        assert result["user"]["email"] == REDACTED
        assert result["user"]["id"] == "u1"
        assert result["transaction"] == "GET /api"

    def test_hooks_do_not_mutate_original(self):
        from engine.observability.sentry import _before_send

        event = {"extra": {"password": "p"}}
        _before_send(event, {})
        assert event["extra"]["password"] == "p"
