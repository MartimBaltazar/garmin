"""The Collector config is part of the contract, so it is tested like code.

Live series reach VictoriaMetrics through the Collector's Prometheus
remote-write translation. These tests read the checked-in config and pin the
properties that, if they regress, fail silently -- the same metric day lands
in two series, or data is dropped, and no log line explains it:

* the Garmin pipeline adds no labels (no scope info, no constant labels, no
  processor that can touch attributes or resources);
* every pipeline starts with ``memory_limiter``;
* the flow-counting branch is wired end to end (a connector must appear as an
  exporter of one pipeline and a receiver of another, or the Collector refuses
  to start);
* the count connector's delta output is converted to cumulative before remote
  write, which drops deltas.

``helm/config/collector.yaml`` must be byte-identical: one file, two packagings.
"""

import pathlib
from functools import cache

import pytest
import yaml

ROOT = pathlib.Path(__file__).parents[2]
CONFIG = ROOT / "deploy" / "config" / "collector.yaml"
HELM_CONFIG = ROOT / "helm" / "config" / "collector.yaml"

#: Processors that may run on the Garmin pipeline: none of them can add,
#: remove or rename labels.
LABEL_SAFE_PROCESSORS = ("memory_limiter", "filter", "batch")


@cache
def _config() -> dict:
    return yaml.safe_load(CONFIG.read_text())


def _pipelines() -> dict[str, dict]:
    return _config()["service"]["pipelines"]


def _remote_write_exporters() -> dict[str, dict]:
    return {
        key: value or {}
        for key, value in _config()["exporters"].items()
        if key.split("/")[0] in ("prometheus_remote_write", "prometheusremotewrite")
    }


def _type(component_id: str) -> str:
    return component_id.split("/")[0]


def test_the_config_file_exists() -> None:
    assert CONFIG.is_file(), CONFIG


def test_there_is_a_remote_write_exporter() -> None:
    """Guards the guards: the label tests below are vacuous without one."""
    assert _remote_write_exporters()


def test_scope_info_is_disabled() -> None:
    for exporter in _remote_write_exporters().values():
        assert exporter.get("disable_scope_info") is True


def test_no_job_or_instance_labels_are_injected() -> None:
    for exporter in _remote_write_exporters().values():
        assert not exporter.get("resource_constant_labels")
        assert not exporter.get("external_labels")
        conversion = exporter.get("resource_to_telemetry_conversion") or {}
        assert not conversion.get("enabled")


def test_the_remote_write_endpoint_comes_from_the_environment() -> None:
    """kustomize and Helm name the VM Service differently; neither is baked in."""
    for exporter in _remote_write_exporters().values():
        endpoint = (exporter.get("http") or {}).get("endpoint") or exporter.get("endpoint")
        assert endpoint == "${env:VM_REMOTE_WRITE_ENDPOINT}"


def test_the_garmin_pipeline_cannot_touch_labels() -> None:
    pipeline = _pipelines()["metrics/garmin"]
    assert pipeline["receivers"] == ["otlp"]
    for processor in pipeline["processors"]:
        assert _type(processor) in LABEL_SAFE_PROCESSORS, processor
    assert all(_type(e) in ("prometheus_remote_write", "debug") for e in pipeline["exporters"])


@pytest.mark.parametrize("name", ["metrics/garmin", "metrics/flow", "metrics/pipeline"])
def test_memory_limiter_runs_first(name: str) -> None:
    assert _pipelines()[name]["processors"][0] == "memory_limiter"


def test_batch_runs_last_where_present() -> None:
    for name, pipeline in _pipelines().items():
        processors = pipeline.get("processors", [])
        if "batch" in processors:
            assert processors[-1] == "batch", name


def test_every_connector_is_wired_on_both_sides() -> None:
    for connector in _config().get("connectors", {}):
        as_exporter = [n for n, p in _pipelines().items() if connector in p.get("exporters", [])]
        as_receiver = [n for n, p in _pipelines().items() if connector in p.get("receivers", [])]
        assert as_exporter and as_receiver, connector


def test_count_output_is_made_cumulative_before_remote_write() -> None:
    """countconnector emits monotonic deltas; remote write drops those."""
    for name, pipeline in _pipelines().items():
        if "count" in pipeline["receivers"]:
            assert "delta_to_cumulative" in pipeline["processors"], name


def test_only_garmin_metrics_are_accepted_over_otlp() -> None:
    for name, pipeline in _pipelines().items():
        if "otlp" in pipeline["receivers"]:
            assert "filter/garmin_only" in pipeline["processors"], name


def test_every_declared_component_is_used() -> None:
    """An unused component is either dead config or a pipeline someone forgot."""
    used: set[str] = set(_config()["service"].get("extensions", []))
    for pipeline in _pipelines().values():
        for section in ("receivers", "processors", "exporters"):
            used.update(pipeline.get(section, []))
    # `debug` is documented as an opt-in exporter for live troubleshooting.
    declared = {
        key
        for section in ("receivers", "processors", "exporters", "connectors", "extensions")
        for key in _config().get(section, {})
    } - {"debug"}
    assert declared <= used, declared - used


def test_helm_ships_the_same_file() -> None:
    assert HELM_CONFIG.read_bytes() == CONFIG.read_bytes(), "run `make chart-sync`"


def test_the_exporter_side_stays_empty() -> None:
    """This is the unit-level half: the request must carry no resource attrs."""
    from datetime import UTC, datetime

    pytest.importorskip("grpc")
    from garmin_exporter.models import sample
    from garmin_exporter.otlp import build_request

    request = build_request([sample("garmin_daily_steps", 1, datetime(2026, 10, 28, tzinfo=UTC))])
    assert dict(request.resource_metrics[0].resource.attributes) == {}
