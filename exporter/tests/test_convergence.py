"""Live-OTLP and backfill paths must converge on one bare series.

The two write paths are:
  * live:   exporter -> OTLP -> Collector -> VM remote write
  * backfill: exporter -> Prometheus text -> VM /api/v1/import/prometheus

They write the same metric on the same event timestamp. If either path adds
labels (#{"job": ...}, #{"otel_scope_name": ...}) the same day lands in two
series and every dashboard halves its data. These tests pin the request each
path produces so the label sets can never drift apart again.
"""

from datetime import UTC, datetime

from garmin_exporter.models import sample
from garmin_exporter.otlp import build_request

DAY = datetime(2026, 10, 28, tzinfo=UTC)


def _datapoint_labels(request, metric_name):
    metrics = request.resource_metrics[0].scope_metrics[0].metrics
    metric = next(m for m in metrics if m.name == metric_name)
    return tuple(
        tuple(sorted((kv.key, kv.value) for kv in dp.attributes))
        for dp in metric.gauge.data_points
    )


def test_live_path_writes_bare_series() -> None:
    """No resource or scope material may reach the Collector's translation.

    `job` comes from `service.name` (a resource attribute the Collector adds a
    label for unconditionally) and `otel_scope_*` comes from the scope. Both
    must be absent so the OTLP path matches the backfill path's bare series.
    """
    request = build_request([sample("garmin_daily_steps", 13571, DAY)])
    assert dict(request.resource_metrics[0].resource.attributes) == {}
    assert _datapoint_labels(request, "garmin_daily_steps") == ((),)


def test_backfill_and_live_use_the_same_timestamp() -> None:
    """Both paths stamp the event time, not the observation time.

    The backfill writes Prom-text with the event timestamp; the OTLP datapoint
    must carry the identical `time_unix_nano` or the same day becomes two
    samples at two different instants.
    """
    request = build_request([sample("garmin_daily_steps", 13571, DAY)])
    metric = request.resource_metrics[0].scope_metrics[0].metrics[0]
    (point,) = metric.gauge.data_points
    assert point.time_unix_nano == int(DAY.timestamp() * 1e9)
