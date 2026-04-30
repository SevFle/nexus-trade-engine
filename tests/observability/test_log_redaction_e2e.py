"""CI gate test: drive structlog with the full chain, then ensure no banned
patterns appear in raw JSON output."""

from __future__ import annotations

import io
import json
import logging
import re

import pytest
import structlog

from engine.observability.logging import setup_logging


@pytest.fixture
def stream_buf(monkeypatch) -> io.StringIO:
    from engine.config import settings as _settings

    monkeypatch.setattr(_settings, "log_format", "json")
    setup_logging()
    buf = io.StringIO()
    root = logging.getLogger()
    if root.handlers:
        root.handlers[0].stream = buf  # type: ignore[attr-defined]
    return buf


class TestRedactionAtWire:
    def test_password_is_not_in_wire_output(self, stream_buf: io.StringIO):
        log = structlog.get_logger("redaction-test")
        log.info("login_attempted", user="u-1", password="hunter2")

        wire = stream_buf.getvalue()
        assert "hunter2" not in wire
        assert "REDACTED" in wire

    def test_token_is_not_in_wire_output(self, stream_buf: io.StringIO):
        log = structlog.get_logger("redaction-test")
        log.info("api_call", token="sk-very-secret-1234567890abcdef")
        wire = stream_buf.getvalue()
        assert "sk-very-secret-1234567890abcdef" not in wire

    def test_authorization_header_is_redacted(self, stream_buf: io.StringIO):
        log = structlog.get_logger("redaction-test")
        log.info("call", authorization="Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig")
        wire = stream_buf.getvalue()
        assert "eyJhbGciOiJIUzI1NiJ9" not in wire

    def test_required_fields_present_in_json_record(self, stream_buf: io.StringIO):
        log = structlog.get_logger("redaction-test")
        log.info("hello", user_id="u-1")
        lines = [ln for ln in stream_buf.getvalue().splitlines() if ln.strip()]
        rec = json.loads(lines[-1])
        for f in ("timestamp", "level", "service", "env", "version"):
            assert f in rec, f"missing field {f} in {rec}"

    def test_no_double_redaction_artifact(self, stream_buf: io.StringIO):
        log = structlog.get_logger("redaction-test")
        log.info("hi", user_id="u-1")
        wire = stream_buf.getvalue()
        assert "u-1" in wire
        assert not re.search(r'"password"\s*:\s*"[^*]', wire)
