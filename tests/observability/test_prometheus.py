"""Tests for the Prometheus exposition-format renderer (gh#34 follow-up)."""

from __future__ import annotations

from engine.observability.prometheus import (
    PrometheusBackend,
    _format_labels,
    _safe_metric_name,
    render_prometheus,
)

# ---------------------------------------------------------------------------
# Helpers (private — exercise their invariants here)
# ---------------------------------------------------------------------------


class TestSafeMetricName:
    def test_dots_become_underscores(self):
        assert _safe_metric_name("webhook.delivered") == "webhook_delivered"

    def test_keeps_underscores_and_colons(self):
        assert _safe_metric_name("ns:metric_name") == "ns:metric_name"

    def test_replaces_arbitrary_chars(self):
        assert _safe_metric_name("foo-bar/baz") == "foo_bar_baz"

    def test_prefixes_underscore_for_leading_digit(self):
        assert _safe_metric_name("404_count").startswith("_4")

    def test_empty_returns_underscore(self):
        assert _safe_metric_name("") == "_"


class TestFormatLabels:
    def test_no_tags_renders_empty(self):
        assert _format_labels(()) == ""

    def test_basic_labels(self):
        out = _format_labels((("a", "1"), ("b", "two")))
        assert out == '{a="1",b="two"}'

    def test_escapes_quote_backslash_and_newline(self):
        out = _format_labels(
            (("k", 'has "quote" and \\backslash and\nnewline'),)
        )
        assert out == '{k="has \\"quote\\" and \\\\backslash and\\nnewline"}'

    def test_label_key_is_sanitised(self):
        # A label key with a dot should become an underscored ident.
        out = _format_labels((("event.type", "x"),))
        assert out == '{event_type="x"}'


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------


class TestRenderCounters:
    def test_single_counter_no_tags(self):
        b = PrometheusBackend()
        b.counter("webhook.delivered")

        out = render_prometheus(b)

        assert "# TYPE webhook_delivered counter" in out
        assert "webhook_delivered 1" in out

    def test_counter_with_tags_sorted_for_diffability(self):
        b = PrometheusBackend()
        b.counter("oms.submit.outcome", tags={"outcome": "submitted"})
        b.counter("oms.submit.outcome", tags={"outcome": "rejected"})
        b.counter("oms.submit.outcome", tags={"outcome": "submitted"})

        out = render_prometheus(b)

        # Two distinct label sets; the rejected line precedes submitted
        # because the renderer sorts label tuples for stable output.
        rejected_idx = out.index('outcome="rejected"')
        submitted_idx = out.index('outcome="submitted"')
        assert rejected_idx < submitted_idx
        # Aggregated counter values rendered as integers (no .0).
        assert 'oms_submit_outcome{outcome="submitted"} 2' in out
        assert 'oms_submit_outcome{outcome="rejected"} 1' in out


class TestRenderGauges:
    def test_gauge_renders_as_float_when_fractional(self):
        b = PrometheusBackend()
        b.gauge("oms.open_orders", 3.5)

        out = render_prometheus(b)

        assert "# TYPE oms_open_orders gauge" in out
        assert "oms_open_orders 3.5" in out

    def test_gauge_integral_value_renders_without_decimal(self):
        b = PrometheusBackend()
        b.gauge("kill_switch.state", 1.0)

        out = render_prometheus(b)

        assert "kill_switch_state 1\n" in out

    def test_gauge_last_write_wins(self):
        b = PrometheusBackend()
        b.gauge("oms.open_orders", 1.0)
        b.gauge("oms.open_orders", 2.0)
        b.gauge("oms.open_orders", 3.0)

        out = render_prometheus(b)

        # Only the most recent value should appear in the rendered text.
        assert "oms_open_orders 3" in out
        assert "oms_open_orders 1\n" not in out
        assert "oms_open_orders 2\n" not in out


class TestRenderHistograms:
    def test_histogram_emits_count_and_sum_lines(self):
        b = PrometheusBackend()
        b.histogram("webhook.duration_ms", 100.0)
        b.histogram("webhook.duration_ms", 250.0)
        b.histogram("webhook.duration_ms", 50.0)

        out = render_prometheus(b)

        assert "# TYPE webhook_duration_ms summary" in out
        assert "webhook_duration_ms_count 3" in out
        # 100 + 250 + 50 = 400 (integral) → rendered without decimal.
        assert "webhook_duration_ms_sum 400\n" in out

    def test_histogram_with_tags(self):
        b = PrometheusBackend()
        b.histogram(
            "webhook.duration_ms",
            10.0,
            tags={"event_type": "order.filled"},
        )

        out = render_prometheus(b)

        assert (
            'webhook_duration_ms_count{event_type="order.filled"} 1'
            in out
        )
        # Sum of a single 10.0 observation is integral → no decimal.
        assert (
            'webhook_duration_ms_sum{event_type="order.filled"} 10\n'
            in out
        )


class TestPrometheusBackend:
    def test_render_method_matches_module_function(self):
        b = PrometheusBackend()
        b.counter("a.b")
        b.gauge("c.d", 2.0)
        b.histogram("e.f", 1.5)

        assert b.render() == render_prometheus(b)

    def test_subclass_is_runtime_protocol_compatible(self):
        from engine.observability.metrics import MetricsBackend

        b = PrometheusBackend()
        assert isinstance(b, MetricsBackend)


class TestIntegration:
    def test_realistic_snapshot_round_trips_to_text(self):
        # Simulate a small slice of a real run: webhook delivery + OMS
        # lifecycle, then render.
        b = PrometheusBackend()
        b.counter(
            "webhook.delivered",
            tags={"event_type": "order.filled", "template": "discord"},
        )
        b.counter(
            "webhook.attempts",
            tags={"event_type": "order.filled", "template": "discord"},
        )
        b.histogram(
            "webhook.duration_ms",
            42.0,
            tags={"event_type": "order.filled", "status": "200"},
        )
        b.gauge("oms.open_orders", 0.0)
        b.gauge("kill_switch.state", 0.0)

        out = b.render()

        # Each section starts with its TYPE line and a stable order.
        assert out.index("# TYPE kill_switch_state gauge") < out.index(
            "# TYPE webhook_duration_ms summary"
        )
        # Help/type lines are exactly one per metric — no duplicates.
        assert out.count("# TYPE webhook_delivered counter") == 1


# ---------------------------------------------------------------------------
# Pytest collection sanity
# ---------------------------------------------------------------------------


def test_renderer_is_pure_does_not_mutate_backend():
    b = PrometheusBackend()
    b.counter("webhook.delivered")

    before = dict(b.counters)
    render_prometheus(b)
    after = dict(b.counters)

    assert before == after
