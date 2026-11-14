"""Generate deploy/config/dashboard-pipeline.json.

The dashboard is checked in (Grafana provisions the JSON file); this script is
how it is produced, so a panel change is a reviewable Python diff instead of a
2000-line JSON diff. Run: python3 hack/gen_pipeline_dashboard.py
"""

from __future__ import annotations

import json
import pathlib

OUT = pathlib.Path(__file__).parents[1] / "deploy" / "config" / "dashboard-pipeline.json"
DS = {"type": "prometheus", "uid": "victoriametrics"}


def m(name: str) -> str:
    """Collector metric selector tolerant of the `_total` suffix varying by version."""
    return f'{{__name__=~"{name}(_total)?"'


_ids = iter(range(1, 1000))


def target(expr: str, legend: str = "", ref: str = "A") -> dict:
    return {"refId": ref, "datasource": DS, "expr": expr, "legendFormat": legend}


def panel(title, kind, pos, targets, *, unit=None, desc="", thresholds=None, options=None, custom=None):
    defaults: dict = {}
    if unit:
        defaults["unit"] = unit
    if thresholds:
        defaults["color"] = {"mode": "thresholds"}
        defaults["thresholds"] = {
            "mode": "absolute",
            "steps": [{"color": c, "value": v} for c, v in thresholds],
        }
    if custom:
        defaults["custom"] = custom
    return {
        "id": next(_ids),
        "title": title,
        "type": kind,
        "datasource": DS,
        "description": desc,
        "gridPos": dict(zip(("x", "y", "w", "h"), pos, strict=True)),
        "targets": [target(*t) if isinstance(t, tuple) else target(t) for t in targets],
        "fieldConfig": {"defaults": defaults, "overrides": []},
        "options": options or {},
    }


def row(title: str, y: int) -> dict:
    return {
        "id": next(_ids),
        "type": "row",
        "title": title,
        "collapsed": False,
        "gridPos": {"x": 0, "y": y, "w": 24, "h": 1},
        "panels": [],
    }


STAT = {
    "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
    "colorMode": "background",
    "graphMode": "none",
    "textMode": "value",
}
LINES = {"drawStyle": "line", "lineWidth": 2, "fillOpacity": 10, "spanNulls": 3_600_000}
BARS = {"drawStyle": "bars", "lineWidth": 1, "fillOpacity": 80}

panels = [
    row("Exporter (garmin_exporter_* self-metrics, one point per CronJob run)", 0),
    panel(
        "Last run", "stat", (0, 1, 4, 5),
        [("max(max_over_time(garmin_exporter_run_success[1h]))", "success")],
        desc="1 = the last run succeeded. Runs every 30 minutes.",
        thresholds=[("red", None), ("green", 1)],
        options={**STAT, "textMode": "value"},
    ),
    panel(
        "Since last success", "stat", (4, 1, 4, 5),
        [("time() - max(max_over_time(garmin_exporter_last_success_timestamp_seconds[1h]))", "age")],
        unit="s",
        desc="GarminExporterStale fires above 6h.",
        thresholds=[("green", None), ("yellow", 3600), ("red", 21600)],
        options=STAT,
    ),
    panel(
        "Sources failing", "stat", (8, 1, 4, 5),
        [("max(max_over_time(garmin_exporter_sources_failed[1h]))", "sources")],
        desc="Garmin endpoints that failed on the last run. GarminSourcesFailing fires if non-zero for 2h.",
        thresholds=[("green", None), ("yellow", 1), ("red", 6)],
        options=STAT,
    ),
    panel(
        "Run duration", "timeseries", (12, 1, 6, 5),
        ["garmin_exporter_run_duration_seconds"],
        unit="s", custom=LINES,
        desc="The Job is killed at activeDeadlineSeconds (600s).",
    ),
    panel(
        "Samples emitted per run", "timeseries", (18, 1, 6, 5),
        ["garmin_exporter_samples_emitted"],
        unit="short", custom=LINES,
    ),
    panel(
        "API errors by kind", "timeseries", (0, 6, 24, 6),
        [("sum by (kind) (garmin_exporter_api_errors)", "{{kind}}")],
        unit="short", custom=BARS,
        desc="Per-run error counts. `auth_error` means Garmin rejected the session: run `make login`.",
    ),
    row("Collector: receive (otlp receiver, count connector)", 12),
    panel(
        "Points received over OTLP", "timeseries", (0, 13, 12, 7),
        [
            (f'sum(rate({m("otelcol_receiver_accepted_metric_points")}, receiver="otlp"}}[5m]))', "accepted"),
            (f'sum(rate({m("otelcol_receiver_refused_metric_points")}, receiver="otlp"}}[5m]))', "refused", "B"),
        ],
        unit="pps", custom=LINES,
        desc="Bursts every 30 minutes, one per exporter run. Refused points are almost always memory_limiter.",
    ),
    panel(
        "Datapoints per metric, last 24h", "barchart", (12, 13, 12, 7),
        [(f'sort_desc(sum by (metric_name) (increase({m("garmin_pipeline_datapoints")}}}[1d])))', "{{metric_name}}")],
        unit="short",
        desc="From the count connector: what actually flowed through the Collector, by metric. A metric missing here was not sent, whatever storage says.",
        options={"orientation": "horizontal", "xTickLabelRotation": 0, "showValue": "auto",
                 "legend": {"showLegend": False}, "reduceOptions": {"calcs": ["lastNotNull"]}},
    ),
    row("Collector: export (prometheus_remote_write to VictoriaMetrics)", 20),
    panel(
        "Points written to VictoriaMetrics", "timeseries", (0, 21, 12, 7),
        [
            (f'sum by (exporter) (rate({m("otelcol_exporter_sent_metric_points")}}}[5m]))', "sent {{exporter}}"),
            (f'sum by (exporter) (rate({m("otelcol_exporter_send_failed_metric_points")}}}[5m]))', "failed {{exporter}}", "B"),
            (f'sum by (exporter) (rate({m("otelcol_exporter_enqueue_failed_metric_points")}}}[5m]))', "dropped (queue full) {{exporter}}", "C"),
        ],
        unit="pps", custom=LINES,
        desc="Anything in `failed` or `dropped` is lost data and fires OtelCollectorExportFailing.",
    ),
    panel(
        "Remote-write queue", "timeseries", (12, 21, 6, 7),
        [
            ('max by (exporter) ({__name__=~"otelcol_exporter_queue_size"})', "size {{exporter}}"),
            ('max by (exporter) ({__name__=~"otelcol_exporter_queue_capacity"})', "capacity", "B"),
        ],
        unit="short", custom=LINES,
        desc="Requests waiting to be written. Sustained growth means VictoriaMetrics is slower than ingest (e.g. during a backfill).",
    ),
    panel(
        "Collector memory", "timeseries", (18, 21, 6, 7),
        [
            ('max({__name__=~"otelcol_process_memory_rss(_bytes)?"})', "rss"),
            ('max({__name__=~"otelcol_process_runtime_heap_alloc_bytes(_bytes)?"})', "heap", "B"),
        ],
        unit="bytes", custom=LINES,
        desc="memory_limiter starts refusing at 80% of the 512Mi limit (~410Mi).",
    ),
]

dashboard = {
    "annotations": {"list": [{
        "builtIn": 1, "datasource": {"type": "grafana", "uid": "-- Grafana --"},
        "enable": True, "hide": True, "iconColor": "rgba(0, 211, 255, 1)",
        "name": "Annotations & Alerts", "type": "dashboard",
    }]},
    "editable": True,
    "graphTooltip": 1,
    "refresh": "1m",
    "schemaVersion": 39,
    "tags": ["garmin", "pipeline", "otel"],
    "templating": {"list": []},
    "time": {"from": "now-24h", "to": "now"},
    "timezone": "utc",
    "title": "Garmin Pipeline",
    "uid": "garmin-pipeline",
    "version": 1,
    "description": "Exporter -> OTel Collector -> VictoriaMetrics. See docs/otel-collector.md.",
    "panels": panels,
}

OUT.write_text(json.dumps(dashboard, indent=2) + "\n")
print(f"wrote {OUT}")
