"""Tests for service/env/version + correlation merge + sampling processors."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from engine.observability import context as ctx
from engine.observability.processors import (
    add_correlation_context,
    add_service_metadata,
    sampling_filter,
)


@pytest.fixture(autouse=True)
def _clear_context():
    ctx.clear_context()
    yield
    ctx.clear_context()


class TestAddServiceMetadata:
    @patch("engine.observability.processors.settings")
    def test_adds_required_static_fields(self, mock_settings):
        mock_settings.app_name = "engine"
        mock_settings.app_env = "test"
        mock_settings.app_version = "1.2.3"

        out = add_service_metadata(None, "info", {"event": "hello"})

        assert out["service"] == "engine"
        assert out["env"] == "test"
        assert out["version"] == "1.2.3"

    @patch("engine.observability.processors.settings")
    def test_does_not_overwrite_explicit_value(self, mock_settings):
        mock_settings.app_name = "engine"
        mock_settings.app_env = "test"
        mock_settings.app_version = "1.0.0"

        out = add_service_metadata(None, "info", {"event": "x", "service": "worker"})
        assert out["service"] == "worker"


class TestAddCorrelationContext:
    def test_adds_correlation_id_when_present(self):
        ctx.bind_correlation_id("c-1")
        out = add_correlation_context(None, "info", {"event": "x"})
        assert out["correlation_id"] == "c-1"

    def test_omits_correlation_id_when_absent(self):
        out = add_correlation_context(None, "info", {"event": "x"})
        assert "correlation_id" not in out

    def test_includes_request_and_span_ids(self):
        ctx.bind_correlation_id("c-1")
        ctx.bind_request_id("r-1")
        ctx.new_span_id("s-1")
        out = add_correlation_context(None, "info", {"event": "x"})
        assert out["correlation_id"] == "c-1"
        assert out["request_id"] == "r-1"
        assert out["span_id"] == "s-1"


class TestSamplingFilter:
    @patch("engine.observability.processors.settings")
    def test_warn_and_error_always_pass(self, mock_settings):
        mock_settings.log_sampling_info = 0.0
        mock_settings.log_sampling_debug = 0.0
        for level in ("warning", "warn", "error", "critical"):
            assert sampling_filter(None, level, {"event": "x"}) is not None

    @patch("engine.observability.processors.settings")
    def test_info_dropped_when_sampling_zero(self, mock_settings):
        from structlog import DropEvent

        mock_settings.log_sampling_info = 0.0
        mock_settings.log_sampling_debug = 0.0
        with pytest.raises(DropEvent):
            sampling_filter(None, "info", {"event": "x"})

    @patch("engine.observability.processors.settings")
    def test_info_passes_when_sampling_one(self, mock_settings):
        mock_settings.log_sampling_info = 1.0
        mock_settings.log_sampling_debug = 1.0
        out = sampling_filter(None, "info", {"event": "x"})
        assert out["event"] == "x"

    @patch("engine.observability.processors.settings")
    def test_debug_dropped_when_sampling_zero(self, mock_settings):
        from structlog import DropEvent

        mock_settings.log_sampling_info = 1.0
        mock_settings.log_sampling_debug = 0.0
        with pytest.raises(DropEvent):
            sampling_filter(None, "debug", {"event": "x"})
