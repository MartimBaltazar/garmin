"""End to end: exporter -> real OTel Collector (the checked-in config) -> real VictoriaMetrics.

Skipped unless both endpoints are given; CI's `e2e` job starts the two
containers and sets them:

    GARMIN_E2E_OTLP=localhost:4317 GARMIN_E2E_VM=http://localhost:8428 pytest -m e2e

The unit tests pin what the exporter *sends* and what the Collector config
*says*. Only this proves what ends up *stored*: the series identity (no
`job`, `instance` or `otel_scope_*` label), the event timestamps, that the
OTLP path and the VM-import path converge on one series, that the filter
drops non-Garmin metrics, and that the count connector and self-scrape
produce their metrics.
"""

from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from garmin_exporter.fetch import RawPayloads
from garmin_exporter.models import sample
from garmin_exporter.transform import transform_all

OTLP = os.environ.get("GARMIN_E2E_OTLP")
VM = os.environ.get("GARMIN_E2E_VM", "").rstrip("/")
FIXTURE = Path(__file__).parent / "fixtures" / "raw-2026-10-28.json"
FORBIDDEN_LABELS = {"job", "instance", "otel_scope_name", "otel_scope_version", "service_name"}

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(not (OTLP and VM), reason="set GARMIN_E2E_OTLP and GARMIN_E2E_VM"),
]


def _export(match: str) -> list[dict[str, Any]]:
    query = urllib.parse.urlencode({"match[]": match, "start": "0"})
    with urllib.request.urlopen(f"{VM}/api/v1/export?{query}", timeout=10) as resp:
        return [json.loads(line) for line in resp.read().decode().splitlines() if line]


def _eventually(fetch: Callable[[], list[dict[str, Any]]], timeout: float = 90.0) -> list:
    """Poll until data appears: batch (5s), remote write, VM's flush, scrape (30s)."""
    deadline = time.monotonic() + timeout
    while True:
        result = fetch()
        if result or time.monotonic() > deadline:
            return result
        time.sleep(2)


@pytest.fixture(scope="module")
def otlp_exporter():
    from garmin_exporter.otlp import OtlpExporter

    exporter = OtlpExporter(OTLP or "", insecure=True, max_points_per_request=500)
    yield exporter
    exporter.close()


@pytest.fixture(scope="module")
def shipped(otlp_exporter) -> list:
    """Send the real fixture's samples once, through the Collector."""
    data = json.loads(FIXTURE.read_text())
    payloads = RawPayloads(
        start=data["start"], end=data["end"], sources=data["sources"], activities=data["activities"]
    )
    samples = transform_all(payloads)
    assert samples
    otlp_exporter.export(samples)
    return samples


def test_daily_metric_is_stored_as_one_bare_series_at_midnight(shipped) -> None:
    series = _eventually(lambda: _export("garmin_daily_steps"))
    assert len(series) == 1, series
    labels = set(series[0]["metric"]) - {"__name__"}
    assert not labels & FORBIDDEN_LABELS, labels
    assert series[0]["timestamps"] == [int(datetime(2026, 10, 28, tzinfo=UTC).timestamp() * 1000)]


def test_no_stored_garmin_series_carries_collector_labels(shipped) -> None:
    series = _eventually(
        lambda: _export('{__name__=~"garmin_(daily|activity)_.*|garmin_heart_rate"}')
    )
    assert series
    for item in series:
        assert not set(item["metric"]) & FORBIDDEN_LABELS, item["metric"]


def test_every_sent_metric_name_arrives(shipped) -> None:
    sent = {s.name for s in shipped}
    deadline = time.monotonic() + 90
    while True:
        stored = {item["metric"]["__name__"] for item in _export('{__name__=~"garmin_.*"}')}
        missing = sent - stored
        if not missing or time.monotonic() > deadline:
            break
        time.sleep(2)
    assert not missing, missing


def test_non_garmin_metrics_are_dropped_by_the_collector(otlp_exporter) -> None:
    stray = sample("e2e_not_garmin", 1, datetime.now(UTC))
    marker = sample("garmin_e2e_filter_marker", 1, datetime.now(UTC))
    assert stray and marker
    otlp_exporter.export([stray, marker])
    # Wait for the marker (sent in the same request) so absence of the stray
    # metric means "filtered", not "not flushed yet".
    assert _eventually(lambda: _export("garmin_e2e_filter_marker"))
    assert _export("e2e_not_garmin") == []


def test_otlp_and_vm_import_converge_on_one_series(otlp_exporter) -> None:
    from garmin_exporter.emit import VmImportEmitter

    day = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=3)
    item = sample("garmin_e2e_convergence", 42, day)
    assert item
    otlp_exporter.export([item])
    vm = VmImportEmitter(VM)
    vm.emit([item])
    vm.close()

    assert _eventually(lambda: _export("garmin_e2e_convergence"))
    # The VM write lands at once; the OTLP one after the Collector's batch
    # timeout. Give the slower path time before judging the series count.
    time.sleep(15)
    series = _export("garmin_e2e_convergence")
    assert len(series) == 1, f"two paths produced {len(series)} series: {series}"
    assert set(series[0]["timestamps"]) == {int(day.timestamp() * 1000)}


def test_backfill_timestamps_from_the_past_are_kept(otlp_exporter) -> None:
    """History flows through the Collector too; the event time must survive."""
    old = datetime(2026, 1, 15, tzinfo=UTC)
    item = sample("garmin_e2e_history", 7, old)
    assert item
    otlp_exporter.export([item])
    series = _eventually(lambda: _export("garmin_e2e_history"))
    assert series and series[0]["timestamps"] == [int(old.timestamp() * 1000)]


def test_count_connector_reports_flow_per_metric(shipped) -> None:
    series = _eventually(lambda: _export('{__name__=~"garmin_pipeline_datapoints(_total)?"}'))
    names = {item["metric"].get("metric_name") for item in series}
    assert "garmin_daily_steps" in names, names


def test_collector_self_telemetry_is_stored(shipped) -> None:
    uptime = '{__name__=~"otelcol_process_uptime(_total)?"}'
    series = _eventually(lambda: _export(uptime), timeout=120)
    assert series, "prometheus/self receiver produced nothing"
    # OtelCollectorDown depends on this name; the job label is not relied on.
    assert all(not s["metric"]["__name__"].startswith("garmin_") for s in series)
    accepted = _export('{__name__=~"otelcol_receiver_accepted_metric_points(_total)?"}')
    assert accepted, "internal telemetry is not at level normal, or names changed"
