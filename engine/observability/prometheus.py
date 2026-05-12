"""Prometheus exposition-format renderer (gh#34 follow-up).

Renders a :class:`RecordingBackend` snapshot to the Prometheus text
exposition format (Content-Type ``text/plain; version=0.0.4``) so
operators can wire ``/metrics`` to their pull-mode scrape target
without adding the optional ``prometheus_client`` dependency.

The formatter does *not* re-bucket histograms. Each histogram surfaces
as ``<name>_count`` (number of observations so far) and ``<name>_sum``
(sum of observations) — which is the strict subset of the histogram
contract Prometheus accepts. Operators who need bucketed quantiles
should swap in a backend that pre-buckets at observation time
(``prometheus_client.Histogram`` is the typical choice; this scaffold
keeps the door open for that without taking the dep here).

Naming
------
Engine code emits dot-separated metric names (``webhook.delivered``)
to match the rest of the codebase. Prometheus requires
``[a-zA-Z_:][a-zA-Z0-9_:]*`` so dots are converted to underscores
when rendering. Tag (label) values are escaped per the Prometheus
spec: ``\\``, ``\\n``, and ``"`` are the only escapes.

What this is *not*
------------------
- A push-gateway client. Operators choose pull vs push.
- A registry of metric metadata (``# HELP`` / ``# TYPE`` lines are
  emitted with placeholder doc-strings — feed-through descriptions
  are a future enhancement once we have a metric catalog).
- A multi-process collector. The :class:`PrometheusBackend` is a
  single-process recorder; running multiple workers requires
  shared-memory coordination (also deferred).
"""

from __future__ import annotations

from io import StringIO

from engine.observability.metrics import RecordingBackend

_LABEL_ESCAPES = str.maketrans(
    {
        "\\": "\\\\",
        "\n": "\\n",
        '"': '\\"',
    }
)


def _safe_metric_name(name: str) -> str:
    """Convert dot-separated engine metric names into Prometheus-safe
    identifiers. Replace any character outside ``[a-zA-Z0-9_:]`` with
    ``_``. The first character must be ``[a-zA-Z_:]`` — if the input
    starts with a digit, prefix ``_``."""
    if not name:
        return "_"
    out = []
    for ch in name:
        if ch.isalnum() or ch in "_:":
            out.append(ch)
        else:
            out.append("_")
    if out[0].isdigit():
        out.insert(0, "_")
    return "".join(out)


def _format_labels(tags: tuple[tuple[str, str], ...]) -> str:
    """Render label set as ``{k1="v1",k2="v2"}``. Empty tags render as
    an empty string (no braces) per the Prometheus convention for
    label-less series."""
    if not tags:
        return ""
    parts = []
    for k, v in tags:
        safe_k = _safe_metric_name(k)
        safe_v = v.translate(_LABEL_ESCAPES)
        parts.append(f'{safe_k}="{safe_v}"')
    return "{" + ",".join(parts) + "}"


def _format_value(value: float) -> str:
    """Render a numeric value. Integers are emitted without a decimal
    point so Prometheus tooling reads them cleanly; floats use Python's
    ``repr`` which is round-trip safe."""
    if value == int(value) and not isinstance(value, bool):
        return str(int(value))
    return repr(float(value))


def render_prometheus(backend: RecordingBackend) -> str:
    """Return the exposition-format text for ``backend``'s current
    snapshot. The output is sorted by metric name + label set so two
    snapshots taken with the same observations diff cleanly.
    """
    out = StringIO()

    # Counters
    counter_groups: dict[
        str, list[tuple[tuple[tuple[str, str], ...], float]]
    ] = {}
    for (name, tags), value in backend.counters.items():
        counter_groups.setdefault(name, []).append((tags, value))
    for name in sorted(counter_groups):
        prom_name = _safe_metric_name(name)
        out.write(f"# HELP {prom_name} engine counter (gh#34)\n")
        out.write(f"# TYPE {prom_name} counter\n")
        for tags, value in sorted(counter_groups[name], key=lambda x: x[0]):
            out.write(
                f"{prom_name}{_format_labels(tags)} {_format_value(value)}\n"
            )

    # Gauges
    gauge_groups: dict[
        str, list[tuple[tuple[tuple[str, str], ...], float]]
    ] = {}
    for (name, tags), value in backend.gauges.items():
        gauge_groups.setdefault(name, []).append((tags, value))
    for name in sorted(gauge_groups):
        prom_name = _safe_metric_name(name)
        out.write(f"# HELP {prom_name} engine gauge (gh#34)\n")
        out.write(f"# TYPE {prom_name} gauge\n")
        for tags, value in sorted(gauge_groups[name], key=lambda x: x[0]):
            out.write(
                f"{prom_name}{_format_labels(tags)} {_format_value(value)}\n"
            )

    # Histograms — emit *_count and *_sum series only. No buckets.
    histogram_groups: dict[
        str, list[tuple[tuple[tuple[str, str], ...], list[float]]]
    ] = {}
    for (name, tags), values in backend.histograms.items():
        histogram_groups.setdefault(name, []).append((tags, values))
    for name in sorted(histogram_groups):
        prom_name = _safe_metric_name(name)
        out.write(
            f"# HELP {prom_name} engine histogram (count + sum only) (gh#34)\n"
        )
        out.write(f"# TYPE {prom_name} summary\n")
        for tags, observations in sorted(
            histogram_groups[name], key=lambda x: x[0]
        ):
            label_text = _format_labels(tags)
            out.write(
                f"{prom_name}_count{label_text} {len(observations)}\n"
            )
            out.write(
                f"{prom_name}_sum{label_text} "
                f"{_format_value(sum(observations))}\n"
            )

    return out.getvalue()


class PrometheusBackend(RecordingBackend):
    """RecordingBackend with a :meth:`render` shortcut.

    Wire this as the global metrics backend at startup, then expose
    ``render()`` (or :func:`render_prometheus`) from a ``/metrics``
    handler. The backend continues to record everything in memory —
    operators are responsible for ensuring a single process owns the
    metrics state (see module docstring for caveats)."""

    def render(self) -> str:
        return render_prometheus(self)


__all__ = [
    "PrometheusBackend",
    "render_prometheus",
]
