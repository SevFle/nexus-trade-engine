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
    """The userinfo-stripping helper must never leak credentials."""

    def test_url_without_userinfo_is_returned_unchanged(self):
        url = "redis://cluster.internal:6379/0?ssl=true"
        assert broker_module._sanitize_url(url) == url

    def test_user_and_password_are_stripped(self):
        sanitized = broker_module._sanitize_url("redis://hunter2:s3cret@cluster.internal:6379/0")
        assert sanitized == "redis://cluster.internal:6379/0"
        assert "hunter2" not in sanitized
        assert "s3cret" not in sanitized

    def test_password_only_is_stripped(self):
        # ``urlparse`` reports an empty-string username (not None) when only
        # a password is present; the helper must still treat this as
        # userinfo and drop it.
        sanitized = broker_module._sanitize_url("rediss://:s3cret@host:6380/1")
        assert sanitized == "rediss://host:6380/1"
        assert "s3cret" not in sanitized

    def test_username_only_is_stripped(self):
        sanitized = broker_module._sanitize_url("valkey://svc@host/2")
        assert sanitized == "valkey://host/2"
        assert "svc" not in sanitized

    def test_port_and_path_are_preserved(self):
        sanitized = broker_module._sanitize_url("redis://u:p@db:6390/3")
        assert sanitized == "redis://db:6390/3"

    def test_ipv6_host_with_userinfo_is_handled(self):
        sanitized = broker_module._sanitize_url("rediss://u:p@[::1]:6391/0")
        assert sanitized == "rediss://[::1]:6391/0"
        assert "u" not in sanitized.replace("[::1]", "")
        assert "p" not in sanitized.replace("[::1]", "")

    def test_query_and_fragment_survive(self):
        sanitized = broker_module._sanitize_url("redis://u:p@host:6379/0?ssl=true#frag")
        assert sanitized == "redis://host:6379/0?ssl=true#frag"


class TestNormalizeUrlErrorSanitization:
    """The ``ValueError`` message must not leak embedded credentials."""

    def test_invalid_scheme_error_strips_userinfo(self):
        url = "amqp://hunter2:s3cret@broker.internal:5672/0"
        with pytest.raises(ValueError, match="Unsupported broker URL scheme") as exc_info:
            broker_module._normalize_broker_url(url)

        message = str(exc_info.value)
        # Host is preserved for operator triage ...
        assert "broker.internal" in message
        assert "amqp" in message
        # ... but the credentials are not.
        assert "hunter2" not in message
        assert "s3cret" not in message


class TestModuleBrokerFatalLog:
    """A misconfigured URL must surface as a clear fatal log, not a bare
    import-time traceback, and must never leak credentials."""

    def test_invalid_scheme_logs_fatal_with_sanitised_url_then_reraises(self, monkeypatch):
        # Drive the helper with an invalid scheme that *also* carries
        # userinfo, so we can assert on both the fatal event and the
        # credential stripping in one shot.
        monkeypatch.setattr(
            broker_module,
            "settings",
            SimpleNamespace(valkey_url="amqp://hunter2:s3cret@broker.internal:5672/0"),
        )
        mock_logger = MagicMock()
        monkeypatch.setattr(broker_module, "logger", mock_logger)

        with pytest.raises(ValueError, match="Unsupported broker URL scheme"):
            broker_module._build_module_broker()

        # Exactly one fatal event, carrying the structured context an
        # operator needs to fix the misconfiguration.
        mock_logger.fatal.assert_called_once_with(
            "tasks.broker.config_invalid",
            error=mock_logger.fatal.call_args.kwargs["error"],
            error_type="ValueError",
            scheme="amqp",
            url="amqp://broker.internal:5672/0",
        )
        call = mock_logger.fatal.call_args
        assert call.args == ("tasks.broker.config_invalid",)
        assert call.kwargs["scheme"] == "amqp"
        assert call.kwargs["url"] == "amqp://broker.internal:5672/0"
        assert "s3cret" not in call.kwargs["url"]
        assert "hunter2" not in call.kwargs["url"]
        assert "Unsupported broker URL scheme" in call.kwargs["error"]

    def test_valid_scheme_builds_broker_without_fatal_log(self, monkeypatch):
        # A correctly configured URL must build cleanly and emit no fatal
        # event — guards against the fatal branch firing spuriously.
        monkeypatch.setattr(
            broker_module,
            "settings",
            SimpleNamespace(valkey_url="valkeys://broker.internal:6420/3"),
        )
        mock_logger = MagicMock()
        monkeypatch.setattr(broker_module, "logger", mock_logger)

        instance = MagicMock(name="broker")
        instance.with_result_backend.return_value = instance
        instance.with_middlewares.return_value = instance
        with (
            patch("engine.tasks.broker.ListQueueBroker", return_value=instance),
            patch("engine.tasks.broker.RedisAsyncResultBackend"),
            patch("engine.tasks.broker.CorrelationMiddleware"),
        ):
            url, broker = broker_module._build_module_broker()

        assert url == "rediss://broker.internal:6420/3"
        assert broker is instance
        mock_logger.fatal.assert_not_called()
