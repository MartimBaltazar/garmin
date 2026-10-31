"""OTLP metric export that preserves *event* timestamps.

Why this module exists instead of the OpenTelemetry Python SDK's gauges
---------------------------------------------------------------------

The SDK's synchronous gauge has two properties that break this pipeline:

1. **One value per attribute set per collection.** The aggregation is
   last-value-wins. A daily metric has one point per calendar day, so a
   2-day lookback produces two points that share an attribute set -- the SDK
   keeps only the last one and the other day is silently lost.
2. **The datapoint timestamp is the moment of the call.** So
   ``garmin_daily_steps`` would land in VictoriaMetrics 48 times a day
   (every 30-minute CronJob tick) instead of once at 00:00 UTC. Every daily
   panel in Grafana would then be wrong, because the daily series would hold
   both a midnight point and a swarm of poll-time points.

Both are properties of the *SDK's* convenience API, not of OTLP. OTLP itself
carries a timestamp per datapoint, so the exporter speaks the protocol
directly and sets ``time_unix_nano`` itself. The Collector stays a plain
pass-through: no timestamp rewriting, no magic attributes, nothing to keep in
sync between the two halves of the pipeline.

The payoff is idempotency. A daily sample is always written at the same
timestamp, so re-running the exporter overwrites the same point instead of
appending a new one -- the live path and the backfill path converge on exactly
the same series.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from urllib.parse import urlparse

import grpc
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
    ExportMetricsServiceRequest,
)
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2_grpc import (
    MetricsServiceStub,
)
from opentelemetry.proto.common.v1.common_pb2 import (
    AnyValue,
    InstrumentationScope,
    KeyValue,
)
from opentelemetry.proto.metrics.v1.metrics_pb2 import (
    Gauge,
    Metric,
    NumberDataPoint,
    ResourceMetrics,
    ScopeMetrics,
)
from opentelemetry.proto.resource.v1.resource_pb2 import Resource

from .models import Sample

log = logging.getLogger(__name__)

SCOPE_NAME = "garmin_exporter"
SCOPE_VERSION = "0.1.0"
DEFAULT_OTLP_PORT = 4317


def grpc_target(endpoint: str) -> str:
    """Turn an OTLP endpoint into a gRPC target.

    Accepts what people actually write in a config file -- ``http://host:4317``,
    ``host:4317``, ``grpc://host:4317`` -- and returns ``host:port``.
    """
    candidate = endpoint.strip()
    if "://" not in candidate:
        candidate = f"http://{candidate}"
    parsed = urlparse(candidate)
    if not parsed.hostname:
        raise ValueError(f"cannot parse OTLP endpoint {endpoint!r}")
    port = parsed.port or (443 if parsed.scheme == "https" else DEFAULT_OTLP_PORT)
    return f"{parsed.hostname}:{port}"


def build_request(
    samples: Sequence[Sample],
) -> ExportMetricsServiceRequest:
    """Build an OTLP export request, one gauge per metric name.

    All points for a metric share one Gauge, which is what the OTLP data model
    requires: a Gauge is a collection of time series distinguished by their
    attributes, and each series carries its own timestamp.

    The resource is intentionally empty -- see the comment below.
    """
    grouped: dict[str, list[Sample]] = {}
    seen: set[tuple[str, tuple[tuple[str, str], ...], int]] = set()
    for item in samples:
        # A Gauge may not contain two points with the same attributes and
        # timestamp -- that is one time series, not two. The transform layer
        # already dedupes, but the emitter must not depend on its caller.
        series_key = (item.name, tuple(sorted(item.labels.items())), item.timestamp_ms)
        if series_key in seen:
            continue
        seen.add(series_key)
        grouped.setdefault(item.name, []).append(item)

    metrics: list[Metric] = []
    for name, items in grouped.items():
        points: list[NumberDataPoint] = []
        for item in items:
            point = NumberDataPoint()
            # Nanoseconds, as OTLP requires. The source of truth is the
            # sample's own timestamp, never "now".
            point.time_unix_nano = item.timestamp_ms * 1_000_000
            point.as_double = item.value
            for key, value in item.labels.items():
                point.attributes.append(KeyValue(key=key, value=AnyValue(string_value=value)))
            points.append(point)
        metrics.append(Metric(name=name, gauge=Gauge(data_points=points)))

    # No resource attributes, deliberately -- in particular no `service.name`.
    #
    # The Collector turns `service.name` into a `job` label and
    # `service.instance.id` into an `instance` label, unconditionally: the
    # Prometheus translation in
    # pkg/translator/prometheusremotewrite/helper.go adds them whenever the
    # resource carries them, and no collector setting suppresses it.
    #
    # The backfill path writes straight to VictoriaMetrics and has no such
    # labels, so a `job` label here would split every metric into two series
    # -- one from the live path and one from the backfill -- and every
    # dashboard would show half the data, with nothing in the logs to say why.
    #
    # The metric names already carry the namespace (`garmin_*`), so nothing is
    # lost. `test_the_resource_carries_no_labels` guards this.
    resource = Resource()

    scope = InstrumentationScope(name=SCOPE_NAME, version=SCOPE_VERSION)
    return ExportMetricsServiceRequest(
        resource_metrics=[
            ResourceMetrics(
                resource=resource,
                scope_metrics=[ScopeMetrics(scope=scope, metrics=metrics)],
            )
        ]
    )


class OtlpExporter:
    """Push metrics to an OTLP endpoint over gRPC."""

    def __init__(
        self,
        endpoint: str,
        *,
        insecure: bool = True,
        timeout: float = 15.0,
    ) -> None:
        self._target = grpc_target(endpoint)
        self._timeout = timeout
        if insecure:
            self._channel = grpc.insecure_channel(self._target)
        else:
            self._channel = grpc.secure_channel(self._target, grpc.ssl_channel_credentials())
        self._stub = MetricsServiceStub(self._channel)

    @property
    def target(self) -> str:
        return self._target

    def export(self, samples: Sequence[Sample]) -> None:
        if not samples:
            return
        request = build_request(samples)
        self._stub.Export(request, timeout=self._timeout)
        metrics = len(request.resource_metrics[0].scope_metrics[0].metrics)
        log.info(
            "exported over otlp",
            extra={"samples": len(samples), "metrics": metrics},
        )

    def close(self) -> None:
        self._channel.close()
