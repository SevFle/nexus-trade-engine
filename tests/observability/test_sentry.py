"""Tests for engine.observability.sentry — setup and teardown of the Sentry SDK."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest
from sentry_sdk.integrations.fastapi import FastApiIntegration

from engine.observability.redact import REDACTED
from engine.observability.sentry import _before_send, close_sentry, setup_sentry


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
            mock_settings.app_version = "1.2.3"
            mock_settings.app_env = "production"
            setup_sentry()

        # Assert per-kwarg rather than with ``assert_called_once_with``:
        # the ``integrations`` list holds ``FastApiIntegration`` instances
        # which compare by identity, so an exact-equality match would be
        # brittle across construction sites.
        mock_init.assert_called_once()
        kwargs = mock_init.call_args.kwargs
        assert kwargs["dsn"] == "https://example@sentry.io/1"
        assert kwargs["release"] == "1.2.3"
        assert kwargs["environment"] == "production"
        assert kwargs["traces_sample_rate"] == 0.5
        assert kwargs["send_default_pii"] is False
        assert kwargs["before_send"] is _before_send
        assert isinstance(kwargs["integrations"], list)
        assert any(
            isinstance(integration, FastApiIntegration)
            for integration in kwargs["integrations"]
        )

    def test_send_default_pii_disabled(self):
        """``send_default_pii`` must be False so Sentry never scrapes PII."""
        with (
            patch("engine.observability.sentry.settings") as mock_settings,
            patch("sentry_sdk.init") as mock_init,
        ):
            mock_settings.sentry_dsn = "https://example@sentry.io/1"
            mock_settings.sentry_traces_sample_rate = 0.0
            mock_settings.app_version = "1.0.0"
            mock_settings.app_env = "test"
            setup_sentry()

        assert mock_init.call_args.kwargs["send_default_pii"] is False

    def test_release_and_environment_passed(self):
        with (
            patch("engine.observability.sentry.settings") as mock_settings,
            patch("sentry_sdk.init") as mock_init,
        ):
            mock_settings.sentry_dsn = "https://example@sentry.io/1"
            mock_settings.sentry_traces_sample_rate = 0.0
            mock_settings.app_version = "9.9.9"
            mock_settings.app_env = "staging"
            setup_sentry()

        assert mock_init.call_args.kwargs["release"] == "9.9.9"
        assert mock_init.call_args.kwargs["environment"] == "staging"


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
            patch("sentry_sdk.flush", return_value=True) as mock_flush,
            patch("sentry_sdk.get_client", return_value=mock_client),
        ):
            close_sentry()

        mock_flush.assert_called_once_with(timeout=2)
        mock_client.close.assert_called_once()

    def test_flush_timeout_is_2_seconds(self):
        mock_client = MagicMock()
        with (
            patch("sentry_sdk.is_initialized", return_value=True),
            patch("sentry_sdk.flush", return_value=True) as mock_flush,
            patch("sentry_sdk.get_client", return_value=mock_client),
        ):
            close_sentry()

        assert mock_flush.call_args.kwargs["timeout"] == 2

    def test_close_still_called_when_flush_times_out(self):
        """Even when flush reports a timeout the client must still be closed."""
        mock_client = MagicMock()
        with (
            patch("sentry_sdk.is_initialized", return_value=True),
            patch("sentry_sdk.flush", return_value=False),
            patch("sentry_sdk.get_client", return_value=mock_client),
        ):
            close_sentry()

        mock_client.close.assert_called_once()

    def test_flush_timeout_logs_warning(self, caplog):
        mock_client = MagicMock()
        with (
            patch("sentry_sdk.is_initialized", return_value=True),
            patch("sentry_sdk.flush", return_value=False),
            patch("sentry_sdk.get_client", return_value=mock_client),
            caplog.at_level(logging.WARNING, logger="engine.observability.sentry"),
        ):
            close_sentry()

        assert any(
            "sentry.flush_timeout" in rec.message
            or rec.message == "sentry.flush_timeout"
            for rec in caplog.records
        )

    def test_flush_success_does_not_log_warning(self, caplog):
        mock_client = MagicMock()
        with (
            patch("sentry_sdk.is_initialized", return_value=True),
            patch("sentry_sdk.flush", return_value=True),
            patch("sentry_sdk.get_client", return_value=mock_client),
            caplog.at_level(logging.WARNING, logger="engine.observability.sentry"),
        ):
            close_sentry()

        assert not any(
            "flush_timeout" in rec.message for rec in caplog.records
        )


class TestBeforeSend:
    """``_before_send`` must strip PII from contexts and breadcrumbs."""

    def test_returns_event_unchanged_when_no_pii(self):
        event = {
            "event_id": "abc",
            "message": "all good",
            "contexts": {"app": {"version": "1.0.0"}},
        }
        result = _before_send(dict(event), {})
        assert result["event_id"] == "abc"
        assert result["contexts"]["app"]["version"] == "1.0.0"

    def test_accepts_hint_argument(self):
        """Sentry passes a hint dict as the second positional argument."""
        event = {"contexts": {}}
        result = _before_send(event, {"exc_info": ValueError("x")})
        assert result is event

    def test_scrubs_banned_keys_in_contexts(self):
        event = {
            "contexts": {
                "app": {"version": "1.0.0"},
                "user": {"token": "leak-me", "password": "secret"},
            }
        }
        result = _before_send(event, {})
        assert result["contexts"]["user"]["token"] == REDACTED
        assert result["contexts"]["user"]["password"] == REDACTED
        assert result["contexts"]["app"]["version"] == "1.0.0"

    def test_scrubs_pii_patterns_in_context_values(self):
        event = {
            "contexts": {
                "request": {"header": "Bearer eyJhbGciOiJIUzI1.supersecret.sig"},
            }
        }
        result = _before_send(event, {})
        assert "supersecret" not in str(result["contexts"]["request"]["header"])

    def test_scrubs_credit_card_in_context(self):
        event = {"contexts": {"billing": {"note": "card 4242 4242 4242 4242"}}}
        result = _before_send(event, {})
        assert "4242 4242 4242 4242" not in str(result["contexts"]["billing"]["note"])

    def test_scrubs_breadcrumb_data_dicts(self):
        event = {
            "breadcrumbs": {
                "values": [
                    {
                        "type": "http",
                        "message": "request",
                        "data": {"authorization": "Bearer abc", "ok": "keep"},
                    },
                ]
            }
        }
        result = _before_send(event, {})
        crumb = result["breadcrumbs"]["values"][0]
        assert crumb["data"]["authorization"] == REDACTED
        assert crumb["data"]["ok"] == "keep"

    def test_scrubs_pii_in_breadcrumb_messages(self):
        event = {
            "breadcrumbs": {
                "values": [
                    {"message": "auth header Bearer eyJhbGciOiJIUzI1.secret.sig"},
                ]
            }
        }
        result = _before_send(event, {})
        assert "secret" not in str(result["breadcrumbs"]["values"][0]["message"])

    def test_scrubs_breadcrumbs_when_list_form(self):
        event = {
            "breadcrumbs": [
                {"message": "ok", "data": {"token": "leak"}},
            ]
        }
        result = _before_send(event, {})
        assert result["breadcrumbs"][0]["data"]["token"] == REDACTED

    def test_handles_missing_contexts_and_breadcrumbs(self):
        event = {"event_id": "x", "message": "no contexts"}
        result = _before_send(dict(event), {})
        assert result == event

    def test_handles_none_contexts(self):
        event = {"contexts": None, "breadcrumbs": None}
        result = _before_send(event, {})
        assert result["contexts"] is None
        assert result["breadcrumbs"] is None

    def test_scrubbed_breadcrumbs_use_new_dict_not_input(self):
        """``_scrub_dict`` returns fresh structures; the original nested
        secret survives untouched rather than being replaced in place."""
        event = {
            "breadcrumbs": {
                "values": [{"data": {"token": "leak"}}],
            }
        }
        original_data = event["breadcrumbs"]["values"][0]["data"]
        result = _before_send(event, {})
        assert result["breadcrumbs"]["values"][0]["data"]["token"] == REDACTED
        assert result["breadcrumbs"]["values"][0]["data"] is not original_data
        assert original_data["token"] == "leak"


class TestBeforeSendIntegrationWithScrubDict:
    """``_before_send`` must reuse ``_scrub_dict`` from the redact module."""

    def test_contexts_match_scrub_dict_output(self):
        from engine.observability.redact import _scrub_dict

        contexts = {"user": {"password": "p", "name": "alice"}}
        event = {"contexts": dict(contexts)}
        result = _before_send(event, {})
        assert result["contexts"] == _scrub_dict(contexts)

    @pytest.mark.parametrize(
        "key",
        ["password", "token", "api_key", "authorization", "secret", "ssn"],
    )
    def test_each_banned_key_redacted_in_contexts(self, key: str):
        event = {"contexts": {"block": {key: "leak"}}}
        result = _before_send(event, {})
        assert result["contexts"]["block"][key] == REDACTED
