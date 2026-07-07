"""Unit tests for structlog configuration and structured log output.

Covers the three behaviours the observability package promises:

  * **Structured output format** — JSON records carry the required fields
    (``timestamp`` as ISO-8601 UTC, ``level``, ``event``, ``service`` /
    ``env`` / ``version``) and are valid single-line JSON.
  * **Correlation ID propagation** — ids bound to the request context
    surface in every emitted record, and are absent when no context is
    bound.
  * **configure_logging / log level from env** — the public entry point
    configures structlog and the level read from settings gates emission.

The stream-capture fixture mirrors ``test_log_redaction_e2e.py``: it points
the root logging handler's stream at an in-memory buffer so we can assert on
the *wire* output (post-renderer, post-redaction).
"""

from __future__ import annotations

import datetime as dt
import io
import json
import logging
from typing import TYPE_CHECKING

import pytest

from engine.config import settings
from engine.observability import context as ctx
from engine.observability.logging import configure_logging, get_logger, setup_logging

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(autouse=True)
def _isolate_context() -> Iterator[None]:
    """No correlation context leaks between tests."""
    ctx.clear_context()
    yield
    ctx.clear_context()


def _attach_buffer() -> io.StringIO:
    """Point the root handler's stream at a fresh buffer and return it."""
    buf = io.StringIO()
    root = logging.getLogger()
    assert root.handlers, "setup_logging() must have installed a root handler"
    root.handlers[0].stream = buf  # type: ignore[attr-defined]
    return buf


@pytest.fixture
def json_log(monkeypatch: pytest.MonkeyPatch) -> io.StringIO:
    """Configure JSON logging and capture the wire output."""
    monkeypatch.setattr(settings, "log_format", "json")
    monkeypatch.setattr(settings, "log_level", "DEBUG")
    monkeypatch.setattr(settings, "log_sink", "stdout")
    monkeypatch.setattr(settings, "log_sampling_info", 1.0)
    monkeypatch.setattr(settings, "log_sampling_debug", 1.0)
    setup_logging()
    return _attach_buffer()


def _records(buf: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]


class TestStructuredJsonFormat:
    def test_record_is_valid_single_line_json(self, json_log: io.StringIO):
        get_logger("fmt-test").info("hello", instrument="AAPL")
        lines = [ln for ln in json_log.getvalue().splitlines() if ln.strip()]
        assert len(lines) == 1
        # Must round-trip through json.loads without raising.
        json.loads(lines[0])

    def test_required_fields_present(self, json_log: io.StringIO):
        get_logger("fmt-test").info("hello", instrument="AAPL")
        rec = _records(json_log)[-1]
        for field in ("timestamp", "level", "event", "service", "env", "version"):
            assert field in rec, f"missing required field {field!r}: {rec}"

    def test_event_and_level_reflect_call(self, json_log: io.StringIO):
        log = get_logger("fmt-test")
        log.info("did_a_thing")
        log.error("something_broke")
        recs = _records(json_log)
        assert recs[0]["event"] == "did_a_thing"
        assert recs[0]["level"] == "info"
        assert recs[1]["event"] == "something_broke"
        assert recs[1]["level"] == "error"

    def test_timestamp_is_iso8601_utc(self, json_log: io.StringIO):
        get_logger("fmt-test").info("ts_check")
        rec = _records(json_log)[-1]
        ts = rec["timestamp"]
        # ISO-8601, parseable by fromisoformat, and carries UTC offset.
        parsed = dt.datetime.fromisoformat(ts)
        assert parsed.tzinfo is not None, f"timestamp missing tzinfo: {ts!r}"
        assert parsed.utcoffset() == dt.timedelta(0), f"timestamp not UTC: {ts!r}"

    def test_service_metadata_matches_settings(self, json_log: io.StringIO):
        get_logger("fmt-test").info("meta_check")
        rec = _records(json_log)[-1]
        assert rec["service"] == settings.app_name
        assert rec["env"] == settings.app_env
        assert rec["version"] == settings.app_version

    def test_logger_name_recorded(self, json_log: io.StringIO):
        get_logger("named-logger").info("named_check")
        rec = _records(json_log)[-1]
        # structlog.stdlib.add_logger_name records the name under ``logger``.
        assert rec["logger"] == "named-logger"


class TestCorrelationPropagation:
    def test_bound_correlation_id_appears_in_record(self, json_log: io.StringIO):
        ctx.bind_correlation_id("cid-abc-123")
        get_logger("prop-test").info("correlated_event")
        rec = _records(json_log)[-1]
        assert rec["correlation_id"] == "cid-abc-123"

    def test_request_and_span_ids_propagate(self, json_log: io.StringIO):
        ctx.bind_correlation_id("cid-1")
        ctx.bind_request_id("req-1")
        ctx.new_span_id("span-1")
        get_logger("prop-test").info("multi_id_event")
        rec = _records(json_log)[-1]
        assert rec["correlation_id"] == "cid-1"
        assert rec["request_id"] == "req-1"
        assert rec["span_id"] == "span-1"

    def test_correlation_absent_when_context_empty(self, json_log: io.StringIO):
        get_logger("prop-test").info("no_context_event")
        rec = _records(json_log)[-1]
        assert "correlation_id" not in rec
        assert "request_id" not in rec

    def test_bind_request_scope_scopes_ids_to_block(self, json_log: io.StringIO):
        tokens = ctx.bind_request_scope(
            correlation_id="scoped-cid",
            request_id="scoped-req",
            span_id="scoped-span",
        )
        try:
            get_logger("prop-test").info("inside_block")
        finally:
            ctx.reset_tokens(tokens)

        get_logger("prop-test").info("outside_block")
        recs = _records(json_log)
        assert recs[-2]["correlation_id"] == "scoped-cid"
        assert recs[-2]["request_id"] == "scoped-req"
        # After reset, no correlation fields leak into the next record.
        assert "correlation_id" not in recs[-1]

    def test_ensure_correlation_id_generates_and_binds(self, json_log: io.StringIO):
        cid = ctx.ensure_correlation_id()
        assert cid  # generated
        assert ctx.get_correlation_id() == cid
        get_logger("prop-test").info("after_ensure")
        rec = _records(json_log)[-1]
        assert rec["correlation_id"] == cid


class TestConfigureLoggingEntryPoints:
    def test_configure_logging_is_idempotent(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(settings, "log_format", "json")
        monkeypatch.setattr(settings, "log_level", "DEBUG")
        monkeypatch.setattr(settings, "log_sink", "stdout")
        configure_logging()  # must not raise
        configure_logging()  # and again
        buf = _attach_buffer()
        get_logger("alias-test").info("post_configure")
        rec = _records(buf)[-1]
        assert rec["event"] == "post_configure"

    def test_get_logger_returns_logger_with_bind(self):
        log = get_logger("bind-test")
        # structlog bound loggers expose the standard level methods + bind.
        assert hasattr(log, "info")
        assert hasattr(log, "bind")
        bound = log.bind(extra="value")
        assert hasattr(bound, "info")


class TestLogLevelFromSettings:
    def test_warning_level_drops_info_records(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(settings, "log_format", "json")
        monkeypatch.setattr(settings, "log_level", "WARNING")
        monkeypatch.setattr(settings, "log_sink", "stdout")
        monkeypatch.setattr(settings, "log_sampling_info", 1.0)
        monkeypatch.setattr(settings, "log_sampling_debug", 1.0)
        setup_logging()
        buf = _attach_buffer()

        get_logger("level-test").info("dropped_info")
        get_logger("level-test").warning("kept_warning")

        recs = _records(buf)
        events = [r["event"] for r in recs]
        assert "kept_warning" in events
        assert "dropped_info" not in events

    def test_root_logger_level_matches_settings(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(settings, "log_format", "json")
        monkeypatch.setattr(settings, "log_level", "ERROR")
        monkeypatch.setattr(settings, "log_sink", "stdout")
        setup_logging()
        assert logging.getLogger().level == logging.ERROR


class TestConsoleRenderer:
    def test_console_format_when_not_json(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # Non-production + log_format != "json" selects ConsoleRenderer.
        monkeypatch.setattr(settings, "log_format", "console")
        monkeypatch.setattr(settings, "app_env", "development")
        monkeypatch.setattr(settings, "log_level", "DEBUG")
        monkeypatch.setattr(settings, "log_sink", "stdout")
        monkeypatch.setattr(settings, "log_sampling_debug", 1.0)
        setup_logging()
        buf = _attach_buffer()

        ctx.bind_correlation_id("console-cid")
        get_logger("console-test").info("human_readable_event")

        wire = buf.getvalue()
        # ConsoleRenderer output is NOT valid JSON and is human-oriented.
        with pytest.raises(json.JSONDecodeError):
            json.loads(wire.strip())
        assert "human_readable_event" in wire
        assert "console-cid" in wire
