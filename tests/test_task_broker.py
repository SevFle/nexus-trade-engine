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

    def test_default_none_resolves_settings_valkey_url_in_body(
        self, captured_broker, monkeypatch
    ):
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
    """``_sanitize_url`` must remove ``user:password@`` userinfo from a URL.

    Credentials are routinely embedded in broker/cache URLs (e.g.
    ``redis://alice:s3cr3t@host:6379/0``). Surfacing the raw URL in an
    error message or log line would leak the password into tracebacks,
    structured logs and Sentry, so the helper reduces the netloc to just
    ``host[:port]`` while leaving every other component intact.
    """

    def test_strips_username_and_password(self):
        assert (
            broker_module._sanitize_url("redis://alice:s3cr3t@host:6379/0")
            == "redis://host:6379/0"
        )

    def test_strips_username_only(self):
        # A username with no password (no colon) is still userinfo and must
        # be removed along with the trailing ``@``.
        assert (
            broker_module._sanitize_url("redis://alice@host:6379/0")
            == "redis://host:6379/0"
        )

    def test_without_credentials_is_unchanged(self):
        url = "redis://host:6379/0"
        assert broker_module._sanitize_url(url) == url

    def test_preserves_query_and_fragment(self):
        assert (
            broker_module._sanitize_url("rediss://u:p@host:6380/1?ssl=true#frag")
            == "rediss://host:6380/1?ssl=true#frag"
        )

    def test_preserves_scheme_and_path(self):
        assert (
            broker_module._sanitize_url("valkeys://user:pass@cluster.internal:6420/3")
            == "valkeys://cluster.internal:6420/3"
        )

    def test_preserves_ipv6_brackets(self):
        assert (
            broker_module._sanitize_url("redis://u:p@[::1]:6379/0")
            == "redis://[::1]:6379/0"
        )

    def test_handles_password_containing_at_sign(self):
        # ``rsplit("@", 1)`` keeps everything after the *last* ``@`` as the
        # hostinfo, so an ``@`` inside the password is treated as userinfo.
        assert (
            broker_module._sanitize_url("redis://u:p@ss@host:6379/0")
            == "redis://host:6379/0"
        )

    def test_malformed_or_plain_string_is_returned_unchanged(self):
        # No netloc / no ``@`` => nothing to strip; the input round-trips.
        assert broker_module._sanitize_url("not-a-url") == "not-a-url"
        assert broker_module._sanitize_url("") == ""

    @pytest.mark.parametrize(
        "url",
        [
            "redis://host:6379/0",
            "valkeys://cluster.internal:6420/3",
            "amqp://guest:guest@broker:5672//",
            "rediss://u:p@[::1]:6380/2?ssl=true#f",
        ],
    )
    def test_sanitize_never_contains_at_for_non_ipv6(self, url):
        # A sanitised URL (ignoring IPv6 bracket content) must not carry an
        # ``@``: any ``@`` would imply leftover userinfo.
        sanitized = broker_module._sanitize_url(url)
        if "[" not in sanitized:
            assert "@" not in sanitized


class TestCredentialsDoNotLeakIntoExceptions:
    """Passwords embedded in a broker URL must never appear in a raised error.

    The headline security contract: when ``_normalize_broker_url`` rejects a
    URL that also carries inline credentials, neither the password nor the
    username may surface in the ``ValueError`` message (which ends up in
    tracebacks, structured logs and Sentry).
    """

    def test_password_absent_from_normalize_exception(self):
        secret = "super-secret-password-DO-NOT-LEAK-12345"
        username = "trader-user"
        url = f"amqp://{username}:{secret}@broker.internal:5672//"

        with pytest.raises(ValueError) as exc_info:
            broker_module._normalize_broker_url(url)

        message = str(exc_info.value)
        assert secret not in message
        assert username not in message
        # The host survives sanitisation so the error stays actionable.
        assert "broker.internal" in message
        assert "amqp" in message  # the rejected scheme is still reported

    def test_password_absent_from_build_broker_exception(self, captured_broker):
        secret = "hunter2-hunter2-leak"
        with pytest.raises(ValueError) as exc_info:
            build_broker(f"postgres://dbuser:{secret}@db.internal:5432/0")

        message = str(exc_info.value)
        assert secret not in message
        assert "dbuser" not in message
        assert "db.internal" in message

    def test_no_credentials_in_exception_repr(self):
        # ``str(exc)`` and the raw exception object (e.g. when logged via
        # ``repr``) must both be free of the secret.
        secret = "repr-secret-value"
        url = f"ftp://anon:{secret}@files.internal:21/"

        with pytest.raises(ValueError) as exc_info:
            broker_module._normalize_broker_url(url)

        assert secret not in str(exc_info.value)
        assert secret not in repr(exc_info.value)

    def test_valid_scheme_with_credentials_is_not_logged(
        self, captured_broker, caplog
    ):
        # On the happy path the broker *needs* the credentials to connect, so
        # the URL handed to ``ListQueueBroker`` legitimately retains them —
        # sanitisation applies to *error messages*, not to the live
        # connection URL. The security contract here is narrower: nothing
        # must be *logged* with the secret in it (caplog captures any
        # structlog/stdlib record emitted during construction).
        secret = "happy-path-secret"
        captured, _ = captured_broker

        with caplog.at_level("DEBUG"):
            build_broker(f"valkey://app:{secret}@redis.internal:6379/1")

        # Scheme normalisation still applies; credentials survive (by design).
        assert captured["url"] == "redis://app:" + secret + "@redis.internal:6379/1"
        for record in caplog.records:
            assert secret not in record.getMessage()
