"""WebSocket metrics (SEV-275).

Centralizes all WS metric emissions through the engine's MetricsBackend.
"""

from __future__ import annotations

from engine.observability.metrics import get_metrics


class _WSMetrics:
    """Thin wrapper that resolves the global metrics backend lazily."""

    @property
    def metrics(self):
        return get_metrics()


ws_metrics = _WSMetrics()
