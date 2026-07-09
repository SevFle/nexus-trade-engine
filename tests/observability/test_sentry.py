"""Tests for engine.observability.sentry — setup and teardown of the Sentry SDK."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest

from engine.observability.redact import REDACTED
from engine.observability.sentry import (
    _before_send,
    _redact_query_string,
    _scrub_request,
    close_sentry,
    init_sentry,
    setup_sentry,
)


class TestInitSentry:
    """``init_sentry`` is the canonical Sentry bootstrap entry point.

    It reads ``sentry_dsn`` / ``sentry_traces_sample_rate`` / ``app_env``
    from the pydantic-settings instance and calls ``sentry_sdk.init``.
    The legacy ``setup_sentry`` name is kept as a backward-compatible
    alias, so these cases are parametrized over both callables to prove
    there is no behaviour drift. ``sentry_sdk.init`` is mocked throughout
    so no real network call is ever made.
    """

    @pytest.mark.parametrize("init_fn", [init_sentry, setup_sentry], ids=["init_sentry", "setup_sentry_alias"])
    def test_skipped_when_dsn_empty(self, init_fn):
        """No ``sentry_sdk.init`` call when the DSN is empty (graceful no-op)."""
        with (
            patch("engine.observability.sentry.settings") as mock_settings,
            patch("sentry_sdk.init") as mock_init,
        ):
            mock_settings.sentry_dsn = ""
            mock_settings.sentry_traces_sample_rate = 0.5
            init_fn()

        mock_init.assert_not_called()

    @pytest.mark.parametrize("init_fn", [init_sentry, setup_sentry], ids=["init_sentry", "setup_sentry_alias"])
    def test_calls_sentry_init_with_correct_params_when_dsn_set(self, init_fn):
        """``sentry_sdk.init`` receives the configured DSN, sample rate,
        environment, release, PII flag and ``before_send`` hook."""
        with (
            patch("engine.observability.sentry.settings") as mock_settings,
            patch("sentry_sdk.init") as mock_init,
        ):
            mock_settings.sentry_dsn = "https://example@sentry.io/1"
            mock_settings.sentry_traces_sample_rate = 0.25
            mock_settings.app_version = "2.0.0"
            mock_settings.app_env = "production"
            init_fn()

        mock_init.assert_called_once_with(
            dsn="https://example@sentry.io/1",
            release="2.0.0",
            environment="production",
            traces_sample_rate=0.25,
            send_default_pii=False,
            before_send=_before_send,
        )

    def test_dsn_read_from_settings(self):
        """The DSN passed to ``sentry_sdk.init`` comes straight from settings."""
        with (
            patch("engine.observability.sentry.settings") as mock_settings,
            patch("sentry_sdk.init") as mock_init,
        ):
            mock_settings.sentry_dsn = "https://key@sentry.example/42"
            mock_settings.sentry_traces_sample_rate = 1.0
            mock_settings.app_version = "0.0.0"
            mock_settings.app_env = "test"
            init_sentry()

        assert mock_init.call_args.kwargs["dsn"] == "https://key@sentry.example/42"

    def test_traces_sample_rate_read_from_settings(self):
        """``traces_sample_rate`` flows from settings into ``sentry_sdk.init``."""
        with (
            patch("engine.observability.sentry.settings") as mock_settings,
            patch("sentry_sdk.init") as mock_init,
        ):
            mock_settings.sentry_dsn = "https://example@sentry.io/1"
            mock_settings.sentry_traces_sample_rate = 0.3
            mock_settings.app_version = "1.0.0"
            mock_settings.app_env = "staging"
            init_sentry()

        assert mock_init.call_args.kwargs["traces_sample_rate"] == 0.3

    def test_environment_read_from_settings(self):
        """``environment`` is read from ``settings.app_env``."""
        with (
            patch("engine.observability.sentry.settings") as mock_settings,
            patch("sentry_sdk.init") as mock_init,
        ):
            mock_settings.sentry_dsn = "https://example@sentry.io/1"
            mock_settings.sentry_traces_sample_rate = 0.0
            mock_settings.app_version = "1.0.0"
            mock_settings.app_env = "production"
            init_sentry()

        assert mock_init.call_args.kwargs["environment"] == "production"

    def test_setup_sentry_delegates_to_init_sentry(self):
        """Invoking the legacy alias must trigger exactly one init call too."""
        with (
            patch("engine.observability.sentry.settings") as mock_settings,
            patch("sentry_sdk.init") as mock_init,
        ):
            mock_settings.sentry_dsn = "https://example@sentry.io/1"
            mock_settings.sentry_traces_sample_rate = 0.1
            mock_settings.app_version = "1.0.0"
            mock_settings.app_env = "test"
            setup_sentry()

        mock_init.assert_called_once_with(
            dsn="https://example@sentry.io/1",
            release="1.0.0",
            environment="test",
            traces_sample_rate=0.1,
            send_default_pii=False,
            before_send=_before_send,
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
            mock_settings.app_version = "1.2.3"
            mock_settings.app_env = "production"
            setup_sentry()

        mock_init.assert_called_once_with(
            dsn="https://example@sentry.io/1",
            release="1.2.3",
            environment="production",
            traces_sample_rate=0.5,
            send_default_pii=False,
            before_send=_before_send,
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
            "sentry.flush_timeout" in rec.message or rec.message == "sentry.flush_timeout"
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

        assert not any("flush_timeout" in rec.message for rec in caplog.records)


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


class TestRedactQueryString:
    r"""``_redact_query_string`` parses a query string parameter by parameter.

    The generic ``_scrub_string`` treats the whole string as one value and
    its inline ``key=value`` rule uses a greedy ``\S+`` that lets one
    parameter swallow its siblings across ``&`` (e.g.
    ``token=secret&page=1`` -> ``token=***REDACTED***``, losing
    ``page=1``). The dedicated helper avoids that by splitting first.
    """

    def test_banned_key_value_redacted(self):
        assert _redact_query_string("token=topsecret") == f"token={REDACTED}"

    def test_password_value_redacted(self):
        assert _redact_query_string("password=hunter2") == f"password={REDACTED}"

    def test_non_sensitive_param_preserved(self):
        # `page` and `keep` are not banned key names, so they survive.
        result = _redact_query_string("page=2&keep=ok")
        assert result == "page=2&keep=ok"

    def test_mixed_sensitive_and_non_sensitive(self):
        qs = "token=topsecret&page=2&refresh_token=rt1&keep=ok"
        result = _redact_query_string(qs)
        assert "topsecret" not in result
        assert "rt1" not in result
        # Non-sensitive params survive intact (not absorbed/mangled).
        assert "page=2" in result
        assert "keep=ok" in result
        assert result.count(REDACTED) == 2

    @pytest.mark.parametrize(
        "key",
        [
            "access_token",
            "refresh_token",
            "api_key",
            "apikey",
            "authorization",
            "client_secret",
            "session_id",
            "ssn",
        ],
    )
    def test_each_banned_query_param_redacted(self, key: str):
        assert _redact_query_string(f"{key}=leak") == f"{key}={REDACTED}"

    def test_hyphenated_banned_key_redacted(self):
        # Header-style hyphenated names are normalized (``-`` -> ``_``).
        assert _redact_query_string("set-cookie=leak") == f"set-cookie={REDACTED}"
        assert _redact_query_string("x-api-key=leak") == f"x-api-key={REDACTED}"
        assert (
            _redact_query_string("x-auth-token=leak") == f"x-auth-token={REDACTED}"
        )

    def test_url_encoded_banned_key_redacted(self):
        # ``access%5Ftoken`` URL-decodes to ``access_token`` (banned).
        result = _redact_query_string("access%5Ftoken=leak&page=1")
        assert "leak" not in result
        assert "page=1" in result

    def test_value_level_pattern_redacted_on_non_banned_key(self):
        # The key is benign, but the value carries a prefixed secret.
        result = _redact_query_string("note=sk_live_abcdefghijklmnop&page=1")
        assert "sk_live_abcdefghijklmnop" not in result
        assert "page=1" in result

    def test_value_level_credit_card_redacted(self):
        result = _redact_query_string("card=4242 4242 4242 4242")
        assert "4242 4242 4242 4242" not in result

    def test_empty_string(self):
        assert _redact_query_string("") == ""

    def test_flag_style_parameter_preserved(self):
        # A param without ``=`` (a bare flag) is left alone.
        assert _redact_query_string("enabled&page=1") == "enabled&page=1"

    def test_trailing_amp_preserved(self):
        assert _redact_query_string("token=leak&") == f"token={REDACTED}&"

    def test_does_not_mutate_input(self):
        qs = "token=leak&page=1"
        _redact_query_string(qs)
        assert qs == "token=leak&page=1"

    def test_more_precise_than_scrub_string(self):
        # Regression guard: ``_scrub_string`` would eat ``page=1`` here
        # (greedy value), the dedicated helper must not.
        from engine.observability.redact import _scrub_string

        assert "page=1" not in _scrub_string("token=secret&page=1")
        assert "page=1" in _redact_query_string("token=secret&page=1")


class TestScrubRequest:
    """``_scrub_request`` strips secrets from a Sentry request payload.

    Covers the three sensitive fields (``query_string``, ``headers``,
    ``data``) and proves the remaining fields (``url``, ``method``,
    ``env`` ...) are preserved untouched.
    """

    def test_other_request_fields_preserved(self):
        """Non-sensitive request fields pass through verbatim.

        Note: ``query_string`` is *redacted* by ``_scrub_request`` -- this
        test deliberately uses a query string with NO secrets so the field
        survives unchanged, and never asserts that a secret-bearing query
        string would leak through (that would be a bug).
        """
        request = {
            "url": "https://app.example/dash",
            "method": "POST",
            "query_string": "page=2",
            "headers": {"content-type": "application/json"},
            "data": {"message": "ok"},
            "env": {"SERVER_NAME": "app.example"},
            "fragment": "section",
        }
        result = _scrub_request(request)
        assert result["url"] == "https://app.example/dash"
        assert result["method"] == "POST"
        assert result["env"]["SERVER_NAME"] == "app.example"
        assert result["fragment"] == "section"
        # Benign query / header / data are preserved (nothing wrongly censored).
        assert result["query_string"] == "page=2"
        assert result["headers"]["content-type"] == "application/json"
        assert result["data"]["message"] == "ok"
        # Input is not mutated.
        assert request["query_string"] == "page=2"
        assert request["headers"]["content-type"] == "application/json"

    def test_query_string_redacted(self):
        request = {"query_string": "token=topsecret&page=2&refresh_token=rt1&keep=ok"}
        result = _scrub_request(request)
        assert "topsecret" not in result["query_string"]
        assert "rt1" not in result["query_string"]
        assert "page=2" in result["query_string"]
        assert "keep=ok" in result["query_string"]
        # The raw secret-bearing query string must NOT leak verbatim.
        assert result["query_string"] != request["query_string"]

    def test_query_string_non_string_left_alone(self):
        # Sentry may attach a non-string query_string in some integrations.
        request = {"query_string": None}
        result = _scrub_request(request)
        assert result["query_string"] is None

    @pytest.mark.parametrize(
        ("header", "value"),
        [
            ("Authorization", "Bearer abc123def456"),
            ("authorization", "Basic dXNlcjpwYXNz"),
            ("Cookie", "session=xyz"),
            ("Set-Cookie", "csrf=abc; Path=/"),
            ("X-API-Key", "leak"),
            ("x-api-key", "leak"),
            ("X-Auth-Token", "leak2"),
            ("Proxy-Authorization", "Basic dXNlcjpwYXNz"),
            ("proxy-authorization", "Bearer xyz"),
        ],
    )
    def test_headers_scrubbed(self, header: str, value: str):
        result = _scrub_request({"headers": {header: value}})
        assert result["headers"][header] == REDACTED

    def test_headers_non_sensitive_preserved(self):
        result = _scrub_request(
            {
                "headers": {
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "User-Agent": "pytest/1.0",
                }
            }
        )
        assert result["headers"]["Content-Type"] == "application/json"
        assert result["headers"]["Accept"] == "application/json"
        assert result["headers"]["User-Agent"] == "pytest/1.0"

    def test_headers_non_dict_left_alone(self):
        result = _scrub_request({"headers": "not-a-dict"})
        assert result["headers"] == "not-a-dict"

    def test_headers_input_not_mutated(self):
        request = {"headers": {"Authorization": "Bearer abc123def456"}}
        _scrub_request(request)
        assert request["headers"]["Authorization"] == "Bearer abc123def456"

    def test_request_data_scrubbed_dict(self):
        request = {
            "data": {
                "username": "alice",
                "password": "hunter2",
                "nested": {"api_key": "leak"},
                "items": [{"token": "t1"}, {"name": "n"}],
                "note": "card 4242 4242 4242 4242",
            }
        }
        result = _scrub_request(request)
        data = result["data"]
        assert data["username"] == "alice"
        assert data["password"] == REDACTED
        assert data["nested"]["api_key"] == REDACTED
        assert data["items"][0]["token"] == REDACTED
        assert data["items"][1]["name"] == "n"
        assert "4242 4242 4242 4242" not in str(data["note"])

    def test_request_data_scrubbed_string(self):
        result = _scrub_request({"data": "password=hunter2&keep=ok"})
        assert "hunter2" not in result["data"]
        assert "keep=ok" in result["data"]

    def test_request_data_scrubbed_list(self):
        result = _scrub_request({"data": [{"token": "t"}, {"id": 1}]})
        assert result["data"][0]["token"] == REDACTED
        assert result["data"][1]["id"] == 1

    def test_request_data_scrubbed_bytes(self):
        result = _scrub_request({"data": b"password=hunter2"})
        assert b"hunter2" not in result["data"]

    def test_request_data_none_preserved(self):
        # ``data`` absent -> key not added; ``data: None`` -> stays None.
        r1 = _scrub_request({"url": "https://x"})
        assert "data" not in r1
        r2 = _scrub_request({"url": "https://x", "data": None})
        assert r2["data"] is None

    def test_request_data_input_not_mutated(self):
        body = {"password": "hunter2", "name": "alice"}
        request = {"data": body}
        _scrub_request(request)
        assert request["data"]["password"] == "hunter2"
        assert request["data"] is body

    def test_full_request_payload_scrubbed_together(self):
        request = {
            "url": "https://app.example/api",
            "method": "POST",
            "query_string": "token=secret&page=1",
            "headers": {
                "Authorization": "Bearer abc123def456",
                "Content-Type": "application/json",
            },
            "data": {"password": "p", "user": "alice"},
        }
        result = _scrub_request(request)
        assert result["url"] == "https://app.example/api"
        assert result["method"] == "POST"
        assert "secret" not in result["query_string"]
        assert result["headers"]["Authorization"] == REDACTED
        assert result["headers"]["Content-Type"] == "application/json"
        assert result["data"]["password"] == REDACTED
        assert result["data"]["user"] == "alice"


class TestBeforeSendRequestScrubbing:
    """``_before_send`` must scrub the ``request`` payload it attaches.

    Integration layer on top of :class:`TestScrubRequest`: proves the
    wiring inside the ``before_send`` hook catches the request payload
    Sentry SDK attaches to outbound events.
    """

    def test_before_send_scrubs_request(self):
        event = {
            "request": {
                "url": "https://app.example/api",
                "query_string": "token=secret&page=1",
                "headers": {"Authorization": "Bearer xyz"},
                "data": {"password": "p"},
            }
        }
        result = _before_send(event, {})
        req = result["request"]
        assert req["url"] == "https://app.example/api"
        assert "secret" not in req["query_string"]
        assert req["query_string"] != "token=secret&page=1"
        assert req["headers"]["Authorization"] == REDACTED
        assert req["data"]["password"] == REDACTED

    def test_before_send_no_request_key(self):
        event = {"message": "x"}
        result = _before_send(event, {})
        assert "request" not in result

    def test_before_send_request_non_dict_passthrough(self):
        event = {"request": "not-a-dict"}
        result = _before_send(event, {})
        assert result["request"] == "not-a-dict"

    def test_before_send_scrubs_contexts_and_request_together(self):
        """contexts scrubbing and the new request scrubbing coexist."""
        event = {
            "contexts": {"user": {"token": "ctx-leak"}},
            "request": {"headers": {"Authorization": "Bearer abc123def456"}},
        }
        result = _before_send(event, {})
        assert result["contexts"]["user"]["token"] == REDACTED
        assert result["request"]["headers"]["Authorization"] == REDACTED
