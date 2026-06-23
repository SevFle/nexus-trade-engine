"""Tests for engine.observability.sentry — setup, teardown and PII scrubbing.

The ``_before_send`` / ``_before_send_transaction`` hooks are the last line of
defence against leaking PII to Sentry. These tests verify that every known
PII-bearing field (request, extra, user, tags, message and stack-frame locals)
is scrubbed before an event leaves the process.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from engine.observability.redact import REDACTED
from engine.observability.sentry import (
    _before_send,
    _before_send_transaction,
    _scrub_event,
    _scrub_exception,
    close_sentry,
    setup_sentry,
)

# Sentinel value used to assert a secret survived (i.e. a regression) in output.
_LEAK = "SUPER-SECRET-VALUE"


def _frame(vars_dict: dict | None = None) -> dict:
    frame = {"filename": "app.py", "function": "view", "lineno": 1}
    if vars_dict is not None:
        frame["vars"] = vars_dict
    return frame


def _exception(vars_dict: dict | None = None) -> dict:
    return {
        "type": "ValueError",
        "value": "boom",
        "stacktrace": {"frames": [_frame(vars_dict)]},
    }


# --------------------------------------------------------------------------- #
# setup_sentry
# --------------------------------------------------------------------------- #
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

    def test_inits_with_scrubbing_hooks_and_no_locals(self):
        with (
            patch("engine.observability.sentry.settings") as mock_settings,
            patch("sentry_sdk.init") as mock_init,
        ):
            mock_settings.sentry_dsn = "https://example@sentry.io/1"
            mock_settings.sentry_traces_sample_rate = 0.5
            setup_sentry()

        mock_init.assert_called_once_with(
            dsn="https://example@sentry.io/1",
            traces_sample_rate=0.5,
            before_send=_before_send,
            before_send_transaction=_before_send_transaction,
            include_local_variables=False,
        )

    def test_registers_callable_before_send_hooks(self):
        with (
            patch("engine.observability.sentry.settings") as mock_settings,
            patch("sentry_sdk.init") as mock_init,
        ):
            mock_settings.sentry_dsn = "https://example@sentry.io/1"
            mock_settings.sentry_traces_sample_rate = 0.0
            setup_sentry()

        kwargs = mock_init.call_args.kwargs
        assert callable(kwargs["before_send"])
        assert callable(kwargs["before_send_transaction"])

    def test_include_local_variables_is_false(self):
        with (
            patch("engine.observability.sentry.settings") as mock_settings,
            patch("sentry_sdk.init") as mock_init,
        ):
            mock_settings.sentry_dsn = "https://example@sentry.io/1"
            mock_settings.sentry_traces_sample_rate = 0.0
            setup_sentry()

        assert mock_init.call_args.kwargs["include_local_variables"] is False


# --------------------------------------------------------------------------- #
# close_sentry
# --------------------------------------------------------------------------- #
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


# --------------------------------------------------------------------------- #
# _scrub_exception helper
# --------------------------------------------------------------------------- #
class TestScrubException:
    def test_scrubs_frame_vars(self):
        exc = _exception({"password": _LEAK, "ok": "visible"})
        result = _scrub_exception(dict(exc))
        frame = result["stacktrace"]["frames"][0]
        assert frame["vars"]["password"] == REDACTED
        assert frame["vars"]["ok"] == "visible"

    def test_scrubs_vars_across_multiple_frames(self):
        exc = {
            "stacktrace": {
                "frames": [
                    _frame({"token": _LEAK}),
                    _frame({"api_key": _LEAK, "count": 3}),
                ]
            }
        }
        result = _scrub_exception(dict(exc))
        frames = result["stacktrace"]["frames"]
        assert frames[0]["vars"]["token"] == REDACTED
        assert frames[1]["vars"]["api_key"] == REDACTED
        assert frames[1]["vars"]["count"] == 3

    def test_no_stacktrace_is_passthrough(self):
        exc = {"type": "ValueError", "value": "boom"}
        assert _scrub_exception(dict(exc)) == exc

    def test_no_frames_is_passthrough(self):
        exc = {"stacktrace": {}}
        assert _scrub_exception(dict(exc)) == exc

    def test_frame_without_vars_is_left_intact(self):
        exc = {"stacktrace": {"frames": [_frame(None)]}}
        result = _scrub_exception(dict(exc))
        assert "vars" not in result["stacktrace"]["frames"][0]


# --------------------------------------------------------------------------- #
# _before_send — request vector
# --------------------------------------------------------------------------- #
class TestBeforeSendRequest:
    """The ``request`` blob (URL, headers, cookies, query_string, data)."""

    def test_scrubs_request_headers(self):
        event = {
            "request": {"headers": {"Authorization": f"Bearer {_LEAK}"}},
        }
        out = _before_send(dict(event), {})
        assert _LEAK not in str(out["request"]["headers"])

    def test_scrubs_banned_header_key(self):
        event = {"request": {"headers": {"cookie": _LEAK}}}
        out = _before_send(dict(event), {})
        assert out["request"]["headers"]["cookie"] == REDACTED

    def test_scrubs_request_cookies(self):
        # ``session_token`` is banned but bare ``session`` is not, so we use a
        # banned payload key to confirm the cookie dict is scrubbed.
        event = {"request": {"cookies": {"token": _LEAK, "theme": "dark"}}}
        out = _before_send(dict(event), {})
        assert out["request"]["cookies"]["token"] == REDACTED
        assert out["request"]["cookies"]["theme"] == "dark"

    def test_scrubs_request_data_body(self):
        event = {
            "request": {"data": {"password": _LEAK, "email": "a@b.com"}},
        }
        out = _before_send(dict(event), {})
        assert out["request"]["data"]["password"] == REDACTED
        assert out["request"]["data"]["email"] == "a@b.com"

    def test_scrubs_query_string_patterns(self):
        # JWT-shaped values are pattern-redacted even in a query string.
        jwt = "aaaaaaaaaaaaaaaa." * 3
        event = {"request": {"query_string": f"token={jwt}"}}
        out = _before_send(dict(event), {})
        assert jwt not in str(out["request"]["query_string"])

    def test_preserves_non_pii_request_fields(self):
        event = {
            "request": {
                "url": "https://api.test/users/42",
                "method": "POST",
                "headers": {"Content-Type": "application/json"},
            }
        }
        out = _before_send(dict(event), {})
        assert out["request"]["url"] == "https://api.test/users/42"
        assert out["request"]["method"] == "POST"
        assert (
            out["request"]["headers"]["Content-Type"] == "application/json"
        )


# --------------------------------------------------------------------------- #
# _before_send — extra / user / tags / message vectors
# --------------------------------------------------------------------------- #
class TestBeforeSendTopLevel:
    def test_scrubs_extra_recursively(self):
        event = {
            "extra": {"config": {"api_key": _LEAK}, "count": 7},
        }
        out = _before_send(dict(event), {})
        assert out["extra"]["config"]["api_key"] == REDACTED
        assert out["extra"]["count"] == 7

    def test_scrubs_user_fields(self):
        event = {
            "user": {
                "id": "u-1",
                "username": "alice",
                "ip_address": "10.0.0.1",
                "extra": {"access_token": _LEAK},
            }
        }
        out = _before_send(dict(event), {})
        assert out["user"]["id"] == "u-1"
        assert out["user"]["username"] == "alice"
        assert out["user"]["extra"]["access_token"] == REDACTED

    def test_scrubs_tags(self):
        event = {"tags": {"ssn": _LEAK, "env": "prod"}}
        out = _before_send(dict(event), {})
        assert out["tags"]["ssn"] == REDACTED
        assert out["tags"]["env"] == "prod"

    def test_scrubs_message_patterns(self):
        # ``message`` is a plain string -> pattern scrubbing only.
        jwt = "aaaaaaaaaaaaaaaa.bbbbbbbbbbbbbbbb.cccccccccccccccc"
        event = {"message": f"failure token={jwt}"}
        out = _before_send(dict(event), {})
        assert jwt not in out["message"]

    def test_message_without_secrets_preserved(self):
        event = {"message": "division by zero"}
        out = _before_send(dict(event), {})
        assert out["message"] == "division by zero"


# --------------------------------------------------------------------------- #
# _before_send — exception stacktrace vector
# --------------------------------------------------------------------------- #
class TestBeforeSendException:
    def test_scrubs_frame_local_variables(self):
        event = {
            "exception": {
                "values": [_exception({"password": _LEAK, "user": "ok"})]
            }
        }
        out = _before_send(dict(event), {})
        frame = out["exception"]["values"][0]["stacktrace"]["frames"][0]
        assert frame["vars"]["password"] == REDACTED
        assert frame["vars"]["user"] == "ok"

    def test_scrubs_multiple_exceptions(self):
        event = {
            "exception": {
                "values": [
                    _exception({"token": _LEAK}),
                    _exception({"secret": _LEAK, "ok": 1}),
                ]
            }
        }
        out = _before_send(dict(event), {})
        values = out["exception"]["values"]
        assert values[0]["stacktrace"]["frames"][0]["vars"]["token"] == REDACTED
        assert (
            values[1]["stacktrace"]["frames"][0]["vars"]["secret"] == REDACTED
        )

    def test_exception_without_stacktrace_intact(self):
        event = {"exception": {"values": [{"type": "Error", "value": "x"}]}}
        out = _before_send(dict(event), {})
        assert out["exception"]["values"][0]["type"] == "Error"


# --------------------------------------------------------------------------- #
# Combined / edge cases
# --------------------------------------------------------------------------- #
class TestBeforeSendCombinedAndEdge:
    def test_scrubs_all_vectors_at_once(self):
        jwt = "aaaaaaaaaaaaaaaa.bbbbbbbbbbbbbbbb.cccccccccccccccc"
        event = {
            "request": {"headers": {"authorization": f"Bearer {jwt}"}},
            "extra": {"api_key": _LEAK},
            "user": {"extra": {"password": _LEAK}},
            "tags": {"ssn": _LEAK},
            "message": f"token={jwt}",
            "exception": {
                "values": [_exception({"client_secret": _LEAK})]
            },
        }
        out = _before_send(dict(event), {})

        blob = repr(out)
        assert _LEAK not in blob
        assert jwt not in blob
        # structural integrity preserved
        assert out["request"]["headers"]["authorization"] == REDACTED
        assert out["extra"]["api_key"] == REDACTED
        assert out["user"]["extra"]["password"] == REDACTED
        assert out["tags"]["ssn"] == REDACTED
        frame = out["exception"]["values"][0]["stacktrace"]["frames"][0]
        assert frame["vars"]["client_secret"] == REDACTED

    def test_empty_event_returned_unchanged(self):
        event: dict = {"event_id": "abc"}
        out = _before_send(dict(event), {})
        assert out == {"event_id": "abc"}

    def test_returns_dict(self):
        out = _before_send({"message": "hi"}, {})
        assert isinstance(out, dict)

    def test_does_not_mutate_input_event(self):
        original = {
            "extra": {"password": _LEAK},
            "exception": {"values": [_exception({"token": _LEAK})]},
        }
        snapshot = {
            "extra": dict(original["extra"]),
            "exception": {
                "values": [
                    {
                        "type": "ValueError",
                        "value": "boom",
                        "stacktrace": {
                            "frames": [
                                {
                                    "filename": "app.py",
                                    "function": "view",
                                    "lineno": 1,
                                    "vars": {"token": _LEAK},
                                }
                            ]
                        },
                    }
                ]
            },
        }
        _before_send(original, {})
        assert original["extra"]["password"] == _LEAK
        assert (
            original["exception"]["values"][0]["stacktrace"]["frames"][0][
                "vars"
            ]["token"]
            == _LEAK
        ), "input event must not be mutated by the hook"
        assert snapshot["extra"]["password"] == _LEAK

    @pytest.mark.parametrize(
        "field",
        ["request", "extra", "user", "tags", "message", "exception"],
    )
    def test_each_pii_field_is_scrubbed(self, field: str):
        leaky = {"password": _LEAK}
        event = {field: leaky}
        if field == "exception":
            event = {"exception": {"values": [_exception(leaky)]}}
        if field == "message":
            event = {"message": "card 4242 4242 4242 4242"}

        out = _before_send(dict(event), {})
        assert _LEAK not in repr(out)

    def test_hint_argument_is_accepted_and_ignored(self):
        hint = {"exc_info": ValueError("x")}
        out = _before_send({"message": "ok"}, hint)
        assert out["message"] == "ok"


# --------------------------------------------------------------------------- #
# _before_send_transaction applies the same logic
# --------------------------------------------------------------------------- #
class TestBeforeSendTransaction:
    def test_scrubs_request_in_transaction(self):
        event = {
            "type": "transaction",
            "transaction": "GET /users",
            "request": {"headers": {"cookie": _LEAK}},
        }
        out = _before_send_transaction(dict(event), {})
        assert out["request"]["headers"]["cookie"] == REDACTED

    def test_scrubs_user_and_extra_in_transaction(self):
        event = {
            "type": "transaction",
            "user": {"extra": {"api_key": _LEAK}},
            "extra": {"token": _LEAK, "ok": 1},
        }
        out = _before_send_transaction(dict(event), {})
        assert out["user"]["extra"]["api_key"] == REDACTED
        assert out["extra"]["token"] == REDACTED
        assert out["extra"]["ok"] == 1

    def test_transaction_without_pii_preserved(self):
        event = {
            "type": "transaction",
            "transaction": "GET /health",
            "tags": {"env": "prod"},
        }
        out = _before_send_transaction(dict(event), {})
        assert out["transaction"] == "GET /health"
        assert out["tags"]["env"] == "prod"


# --------------------------------------------------------------------------- #
# _scrub_event public-ish helper parity
# --------------------------------------------------------------------------- #
class TestScrubEventParity:
    def test_scrub_event_matches_before_send(self):
        event = {"extra": {"password": _LEAK}, "tags": {"env": "dev"}}
        assert _scrub_event(dict(event)) == _before_send(dict(event), {})

    def test_scrub_event_handles_missing_all_keys(self):
        assert _scrub_event({}) == {}
