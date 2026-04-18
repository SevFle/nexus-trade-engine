"""Tests for observability setup functions."""

from unittest.mock import patch

from engine.observability.logging import setup_logging
from engine.observability.tracing import setup_tracing


class TestLoggingSetup:
    def test_setup_logging_does_not_crash(self):
        setup_logging()

    def test_setup_logging_idempotent(self):
        setup_logging()
        setup_logging()


class TestTracingSetup:
    def test_setup_tracing_does_not_crash(self):
        setup_tracing()

    @patch("engine.observability.tracing.settings")
    def test_setup_tracing_with_otlp_endpoint(self, mock_settings):
        mock_settings.otlp_endpoint = "http://localhost:4317"
        setup_tracing()
