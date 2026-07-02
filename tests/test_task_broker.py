"""Unit tests for :func:`engine.tasks.broker.build_broker` URL handling.

Covers the scheme-normalisation contract:

* ``redis://``   -> ``redis://``   (plain Redis, identity)
* ``rediss://``  -> ``rediss://``  (TLS Redis, identity)
* ``valkey://``  -> ``redis://``   (Valkey plain -> Redis plain)
* ``valkeys://`` -> ``rediss://``  (Valkey TLS  -> Redis TLS)
* an unrecognised scheme -> ``ValueError`` raised *before* the broker is built
* the default ``url=None`` resolves :attr:`settings.valkey_url` inside the
  body (so the import-time construction is decoupled from settings resolution)

The ``taskiq_redis`` builders are mocked out so no socket is opened and we
can assert on the URL that actually reaches ``ListQueueBroker``.
"""

from __future__ import annotations

import importlib
import warnings
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    # ``importlib.import_module`` is used to grab the module object: the
    # ``from engine.tasks import broker`` form resolves to the re-exported
    # broker *instance* (the package facade binds that name), not the module.
    broker_module = importlib.import_module("engine.tasks.broker")
    build_broker = broker_module.build_broker
    _normalize_broker_url = broker_module._normalize_broker_url
    _sanitize_url = broker_module._sanitize_url


@pytest.fixture
def captured_broker():
    """Patch the taskiq_redis builders and capture the resolved broker URL.

    ``ListQueueBroker`` returns a self-chaining mock so the fluent
    ``with_result_backend`` / ``with_middlewares`` calls resolve back to the
    same instance, mirroring how :func:`build_broker` wires the real broker.
    """
    captured: dict[str, object] = {}

    instance = MagicMock(name="broker-instance")
    instance.with_result_backend.return_value = instance
    instance.with_middlewares.return_value = instance

    def _fake_list_queue_broker(url=None, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        return instance

    with (
        patch("engine.tasks.broker.ListQueueBroker", side_effect=_fake_list_queue_broker),
        patch("engine.tasks.broker.RedisAsyncResultBackend"),
        patch("engine.tasks.broker.CorrelationMiddleware"),
    ):
        yield captured, instance


class TestBuildBrokerSchemeMapping:
    @pytest.mark.parametrize(
        ("input_url", "expected"),
        [
            ("redis://host:6379/0", "redis://host:6379/0"),
            ("rediss://host:6379/0", "rediss://host:6379/0"),
            ("valkey://host:6379/0", "redis://host:6379/0"),
            ("valkeys://host:6379/0", "rediss://host:6379/0"),
            # The scheme should be matched case-insensitively.
            ("VALKEY://host:6379/0", "redis://host:6379/0"),
            ("Redis://host:6379/0", "redis://host:6379/0"),
        ],
    )
    def test_scheme_is_normalised(self, captured_broker, input_url, expected):
        captured, instance = captured_broker

        result = build_broker(input_url)

        assert captured["url"] == expected
        # The result backend + correlation middleware are wired onto the same
        # broker instance and the normalised URL is forwarded to both.
        assert result is instance
        instance.with_result_backend.assert_called_once()
        instance.with_middlewares.assert_called_once()

    def test_host_port_path_are_preserved(self, captured_broker):
        captured, _ = captured_broker

        build_broker("valkeys://redis-cluster.internal:6380/2")

        assert captured["url"] == "rediss://redis-cluster.internal:6380/2"

    def test_unknown_scheme_is_rejected_before_construction(self, captured_broker):
        captured, instance = captured_broker

        with pytest.raises(ValueError, match="Unsupported broker URL scheme"):
            build_broker("amqp://host:5672//")

        # Normalisation runs before the (mocked) ListQueueBroker is touched,
        # so no URL was ever captured and no wiring occurred.
        assert "url" not in captured
        instance.with_result_backend.assert_not_called()
        instance.with_middlewares.assert_not_called()

    def test_default_none_resolves_settings_valkey_url_in_body(self, captured_broker, monkeypatch):
        captured, _ = captured_broker

        # Patch the module object directly: the dotted name
        # ``engine.tasks.broker`` also resolves to the re-exported broker
        # *instance* via the package facade, so a string-based monkeypatch
        # would target the wrong object.
        monkeypatch.setattr(
            broker_module,
            "settings",
            SimpleNamespace(valkey_url="valkeys://settings-host:6420/3"),
        )

        build_broker()

        assert captured["url"] == "rediss://settings-host:6420/3"


class TestSanitizeUrl:
    """Direct unit coverage for the :func:`_sanitize_url` helper.

    The helper is the single chokepoint for keeping credentials out of
    plaintext surfaces, so it gets its own focused suite: userinfo is
    stripped, the host/port/path survive, and the helper never raises
    (it is meant to be called from error paths).
    """

    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            # No credentials — the URL passes through untouched.
            (
                "redis://host:6379/0",
                "redis://host:6379/0",
            ),
            # Username + password, default port omitted from netloc.
            (
                "redis://user:pass@host:6379/0",
                "redis://host:6379/0",
            ),
            # Password-only form (``:secret@``).
            (
                "valkey://:secret@host:6379/0",
                "valkey://host:6379/0",
            ),
            # TLS scheme, credentials stripped, scheme + db path retained.
            (
                "rediss://admin:hunter2@broker.internal:6380/2",
                "rediss://broker.internal:6380/2",
            ),
            # URL-encoded password is fully removed too; scheme survives.
            (
                "valkeys://svc:p%40ss%21@cluster:6390/1",
                "valkeys://cluster:6390/1",
            ),
        ],
    )
    def test_userinfo_is_stripped(self, url, expected):
        assert _sanitize_url(url) == expected

    def test_host_port_and_path_are_preserved(self):
        sanitized = _sanitize_url("redis://u:p@redis-cluster.internal:6380/2")

        assert sanitized == "redis://redis-cluster.internal:6380/2"

    def test_scheme_is_preserved(self):
        # The scheme is left intact — sanitisation only touches the netloc.
        assert _sanitize_url("valkey://u:p@host:6379/0").startswith("valkey://")

    def test_password_is_absent_from_output(self):
        secret = "do-not-leak-me-98765"
        sanitized = _sanitize_url(f"redis://user:{secret}@host:6379/0")

        assert secret not in sanitized
        assert "user" not in sanitized

    def test_no_port(self):
        # ``parsed.port`` is ``None`` when the port is absent, so the netloc
        # must not acquire a trailing ``:None``.
        assert _sanitize_url("redis://u:p@host/0") == "redis://host/0"

    def test_non_credentialed_url_is_unchanged(self):
        url = "redis://localhost:6379/0"
        assert _sanitize_url(url) == url

    def test_does_not_raise_on_unparseable_input(self):
        # The helper is designed to be called from error paths, so a bogus
        # input must fall through to returning the value verbatim rather
        # than raising a second exception.
        assert _sanitize_url(None) is None  # type: ignore[arg-type]

    def test_empty_string(self):
        assert _sanitize_url("") == ""


class TestNormalizeBrokerUrlRedactsCredentials:
    """The security-critical contract: credentials must never reach the
    ``ValueError`` message raised for an unsupported scheme.

    A misconfigured deployment commonly points ``valkey_url`` (and thus the
    broker URL) at the wrong scheme *with* credentials embedded — e.g.
    ``postgres://user:pass@host``. Before :func:`_sanitize_url` the whole
    credentialed URL was interpolated into the exception, leaking the
    shared cache/broker password into logs and tracebacks. These tests pin
    the redaction so a regression is caught immediately.
    """

    def test_credentialled_valkey_url_password_absent_from_error(self):
        """The headline test: a credentialed ``valkey_url`` whose scheme is
        rejected must not echo the password (or username) back."""
        secret = "super-secret-pw-12345"
        url = f"postgres://admin:{secret}@broker.internal:6379/0"

        with pytest.raises(ValueError, match="Unsupported broker URL scheme") as exc_info:
            _normalize_broker_url(url)

        message = str(exc_info.value)
        # Neither the password nor the username may survive into the message.
        assert secret not in message
        assert "admin" not in message
        # The host stays so the message remains diagnostically useful.
        assert "broker.internal" in message
        assert "6379" in message

    def test_credentialled_url_password_absent_from_error_via_build_broker(self, captured_broker):
        """The redaction must also hold on the public :func:`build_broker`
        entry point, not only the private normaliser."""
        captured, _ = captured_broker
        secret = "another-s3cret-via-build-broker"
        url = f"amqp://operator:{secret}@mq.internal:5672//"

        with pytest.raises(ValueError, match="Unsupported broker URL scheme") as exc_info:
            build_broker(url)

        message = str(exc_info.value)
        assert secret not in message
        assert "operator" not in message
        # Normalisation runs before construction, so nothing was captured.
        assert "url" not in captured

    def test_password_only_form_is_redacted(self):
        """The ``:secret@`` (no-username) form must also be stripped."""
        secret = "pw-only-secret"
        url = f"http://:{secret}@host:6379/0"

        with pytest.raises(ValueError, match="Unsupported broker URL scheme") as exc_info:
            _normalize_broker_url(url)

        assert secret not in str(exc_info.value)

    def test_url_encoded_password_is_redacted(self):
        """A URL-encoded password must not leak either."""
        secret = "p%40ss%21word"  # decoded: p@ss!word
        url = f"postgres://user:{secret}@host:6379/0"

        with pytest.raises(ValueError, match="Unsupported broker URL scheme") as exc_info:
            _normalize_broker_url(url)

        message = str(exc_info.value)
        assert secret not in message
        assert "p@ss" not in message

    def test_credentialled_supported_scheme_still_passes_credentials_through(self):
        """Sanitisation is *only* for diagnostics. A *supported* scheme must
        keep its credentials so the broker can actually authenticate —
        i.e. we redact error messages, never the live connection URL."""
        secret = "live-connection-secret"
        url = f"valkeys://user:{secret}@broker.internal:6380/3"

        normalized = _normalize_broker_url(url)

        # Scheme rewritten to what taskiq_redis expects, credentials intact.
        assert normalized == f"rediss://user:{secret}@broker.internal:6380/3"


class TestBrokerUrlExport:
    """The module-level ``broker_url`` is part of ``__all__`` and is derived
    from settings at import time. It necessarily retains credentials (the
    broker needs them to connect), so these tests pin that contract and the
    ``__all__`` surface rather than the (intentional) credential presence.
    """

    def test_broker_url_is_in_all(self):
        assert "broker_url" in broker_module.__all__

    def test_broker_url_is_a_string(self):
        assert isinstance(broker_module.broker_url, str)

    def test_broker_url_is_normalised_to_redis_scheme(self):
        # Whatever the configured ``valkey_url`` scheme is, the exported
        # ``broker_url`` is rewritten to ``redis://`` / ``rediss://``.
        assert broker_module.broker_url.split("://")[0] in {"redis", "rediss"}

    def test_sanitize_url_is_available_for_redacting_broker_url(self):
        """Anything importing ``broker_url`` for display must be able to
        redact it via the public-enough :func:`_sanitize_url` helper."""
        sanitized = _sanitize_url("rediss://user:pass@broker.internal:6380/3")
        assert sanitized == "rediss://broker.internal:6380/3"
