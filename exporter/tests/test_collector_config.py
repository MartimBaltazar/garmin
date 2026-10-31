"""The Collector config must stay a pure pass-through for labels.

Live series reach VictoriaMetrics through the Collector's Prometheus
remote-write translation. Two config knobs decide whether extra labels appear:

  * ``disable_scope_info: true`` -- without it every series gains
    ``otel_scope_name`` / ``otel_scope_version``.
  * no ``service.name`` resource attribute from the exporter side, and no
    ``resource_constant_labels`` adding ``job``/``instance`` server-side.

The backfill path writes directly to VM with none of those labels. If the
Collector config regresses, the same metric day lands in two series and no
log line explains it. These tests read the checked-in config so the drift is
caught at test time, not on a Grafana panel.
"""

import pathlib
from functools import cache

import yaml

CONFIG = (
    pathlib.Path(__file__).parents[2] / "deploy" / "config" / "collector.yaml"
)


@cache
def _config() -> dict:
    return yaml.safe_load(CONFIG.read_text())


def _remote_write_exporters() -> list[dict]:
    return [
        v for k, v in _config()["exporters"].items() if "prometheusremotewrite" in k
    ]


def test_the_config_file_exists() -> None:
    assert CONFIG.is_file(), CONFIG


def test_scope_info_is_disabled() -> None:
    for exporter in _remote_write_exporters():
        assert exporter.get("disable_scope_info") is True


def test_no_job_or_instance_labels_are_injected() -> None:
    for exporter in _remote_write_exporters():
        constant = exporter.get("resource_constant_labels") or {}
        assert not constant, constant


def test_the_exporter_side_stays_empty() -> None:
    """This is the unit-level half: the request must carry no resource attrs."""
    from datetime import UTC, datetime

    from garmin_exporter.models import sample
    from garmin_exporter.otlp import build_request

    request = build_request(
        [sample("garmin_daily_steps", 1, datetime(2026, 10, 28, tzinfo=UTC))]
    )
    assert dict(request.resource_metrics[0].resource.attributes) == {}
