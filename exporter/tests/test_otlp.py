"""OTLP export tests.

The live path's correctness rests on two things that are invisible in the
output unless you look at the wire format:

1. every datapoint carries the **event** timestamp, not the time of the call;
2. two points of the same metric with the same labels are both present.

Both were broken by the SDK's gauge API, which is why the OTLP request is built
by hand. These tests assert the request, not the exporter's behaviour around
it.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from garmin_exporter.models import Sample, sample
from garmin_exporter.otlp import (
    DEFAULT_SERVICE_NAME,
    build_request,
    grpc_target,
)

DAY = datetime(2026, 10, 28, tzinfo=UTC)


def _points(request, name: str):
    """All datapoints of one metric in a built request."""
    metrics = request.resource_metrics[0].scope_metrics[0].metrics
    for metric in metrics:
        if metric.name == name:
            return list(metric.gauge.data_points)
    raise AssertionError(f"metric {name} not in request")


def _labels(point) -> dict[str, str]:
    """Datapoint attributes are a repeated KeyValue, not a map."""
    return {item.key: item.value.string_value for item in point.attributes}


def _resource_attributes(request) -> dict[str, str]:
    """Resource attributes are a repeated KeyValue, not a map."""
    return {
        item.key: item.value.string_value
        for item in request.resource_metrics[0].resource.attributes
    }


# --------------------------------------------------------------------------- #
# Endpoint parsing
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        ("http://otel-collector:4317", "otel-collector:4317"),
        ("https://otel-collector:4317", "otel-collector:4317"),
        ("otel-collector:4317", "otel-collector:4317"),
        ("grpc://otel-collector:4317", "otel-collector:4317"),
        (
            "http://otel-collector.garmin.svc.cluster.local:4317",
            "otel-collector.garmin.svc.cluster.local:4317",
        ),
        ("http://otel-collector:4317/", "otel-collector:4317"),
    ],
)
def test_grpc_target_accepts_the_shapes_people_write(endpoint: str, expected: str) -> None:
    assert grpc_target(endpoint) == expected


def test_grpc_target_defaults_to_the_otlp_port() -> None:
    assert grpc_target("http://collector") == "collector:4317"


def test_grpc_target_rejects_nonsense() -> None:
    with pytest.raises(ValueError, match="cannot parse"):
        grpc_target("http://")


# --------------------------------------------------------------------------- #
# Timestamps
# --------------------------------------------------------------------------- #
def test_datapoint_carries_the_event_timestamp_not_now() -> None:
    request = build_request([Sample(name="garmin_daily_steps", value=1, timestamp=DAY)])
    point = _points(request, "garmin_daily_steps")[0]
    assert point.time_unix_nano == int(DAY.timestamp() * 1_000_000_000)


def test_two_days_of_the_same_metric_are_both_present() -> None:
    """The SDK's gauge kept only the last one. This must not happen."""
    older = sample("garmin_daily_steps", 100, datetime(2026, 10, 27, tzinfo=UTC))
    newer = sample("garmin_daily_steps", 200, DAY)
    assert older is not None and newer is not None

    points = _points(build_request([older, newer]), "garmin_daily_steps")
    assert len(points) == 2
    assert sorted(point.as_double for point in points) == [100.0, 200.0]


def test_same_series_same_timestamp_is_not_duplicated() -> None:
    """Re-running the exporter must overwrite, not append."""
    item = sample("garmin_daily_steps", 100, DAY)
    assert item is not None
    points = _points(build_request([item, item]), "garmin_daily_steps")
    assert len(points) == 1


# --------------------------------------------------------------------------- #
# Shape
# --------------------------------------------------------------------------- #
def test_one_gauge_per_metric_name() -> None:
    request = build_request(
        [
            sample("garmin_daily_steps", 1, datetime(2026, 10, 27, tzinfo=UTC)),
            sample("garmin_daily_steps", 2, DAY),
            sample("garmin_daily_resting_heart_rate", 3, DAY),
        ]
    )
    metrics = request.resource_metrics[0].scope_metrics[0].metrics
    assert [metric.name for metric in metrics] == [
        "garmin_daily_steps",
        "garmin_daily_resting_heart_rate",
    ]
    assert len(_points(request, "garmin_daily_steps")) == 2


def test_labels_become_datapoint_attributes() -> None:
    request = build_request([sample("garmin_daily_intensity_minutes", 9, DAY, kind="moderate")])
    point = _points(request, "garmin_daily_intensity_minutes")[0]
    assert _labels(point) == {"kind": "moderate"}


def test_activity_labels_survive() -> None:
    request = build_request(
        [
            sample(
                "garmin_activity_duration_seconds",
                60,
                DAY,
                activity_id="123",
                activity_type="running",
            )
        ]
    )
    point = _points(request, "garmin_activity_duration_seconds")[0]
    assert _labels(point) == {"activity_id": "123", "activity_type": "running"}


def test_resource_carries_the_service_name() -> None:
    request = build_request([sample("garmin_daily_steps", 1, DAY)])
    assert _resource_attributes(request)["service.name"] == DEFAULT_SERVICE_NAME


def test_service_name_can_be_overridden() -> None:
    request = build_request([sample("garmin_daily_steps", 1, DAY)], service_name="other")
    assert _resource_attributes(request)["service.name"] == "other"


def test_an_empty_request_has_no_metrics() -> None:
    request = build_request([])
    assert not request.resource_metrics[0].scope_metrics[0].metrics


def test_values_are_exported_as_doubles() -> None:
    request = build_request([sample("garmin_daily_steps", 11234, DAY)])
    point = _points(request, "garmin_daily_steps")[0]
    assert point.as_double == 11234.0
