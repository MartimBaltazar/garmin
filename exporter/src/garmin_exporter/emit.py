"""Emitters: OTLP (live path) and VictoriaMetrics import (backfill path).

Two paths, deliberately:

* **OTLP** goes through the OTel Collector, which is what PLAN.md asks for and
  what makes the pipeline observable. Its limitation is that the Python SDK
  stamps metrics at *observation* time, so it cannot backfill.
* **VM import** POSTs Prometheus exposition with an explicit timestamp to
  ``/api/v1/import/prometheus``. That is the only way to write real history, and
  it is idempotent because the timestamp is part of the series identity.

The two must agree on names and labels or the backfilled history and the live
data will form separate series. Both read from the same ``Sample`` list.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Protocol

import httpx

from .config import Config
from .models import Sample

log = logging.getLogger(__name__)

VM_IMPORT_PATH = "/api/v1/import/prometheus"


class Emitter(Protocol):
    def emit(self, samples: Sequence[Sample]) -> None: ...

    def close(self) -> None: ...


class NullEmitter:
    """Dry-run sink. Prints a summary so ``--dry-run`` is actually useful."""

    def __init__(self, limit: int = 10) -> None:
        self._limit = limit
        self.total = 0

    def emit(self, samples: Sequence[Sample]) -> None:
        self.total += len(samples)
        for item in samples[: self._limit]:
            labels = ",".join(f"{k}={v}" for k, v in sorted(item.labels.items()))
            log.info(
                "sample",
                extra={
                    "metric": item.name,
                    "value": item.value,
                    "ts": item.timestamp.isoformat(),
                    "labels": labels,
                },
            )
        if len(samples) > self._limit:
            log.info(
                "sample output truncated", extra={"emitted": len(samples), "shown": self._limit}
            )

    def close(self) -> None:
        log.info("dry run complete", extra={"samples": self.total})


class VmImportEmitter:
    """Write samples straight to VictoriaMetrics with explicit timestamps."""

    def __init__(self, base_url: str, timeout: float = 15.0) -> None:
        self._url = base_url.rstrip("/") + VM_IMPORT_PATH
        self._client = httpx.Client(timeout=timeout)

    def render(self, samples: Sequence[Sample]) -> str:
        return render_prometheus(samples)

    def emit(self, samples: Sequence[Sample]) -> None:
        if not samples:
            return
        body = self.render(samples)
        response = self._client.post(
            self._url, content=body, headers={"Content-Type": "text/plain"}
        )
        response.raise_for_status()
        log.info("imported to victoriametrics", extra={"samples": len(samples), "url": self._url})

    def close(self) -> None:
        self._client.close()


class OtlpEmitter:
    """Push gauges to the OTel Collector over OTLP/gRPC.

    A Gauge is created lazily per metric name and reused, because the SDK keys
    instruments by name and creating one per batch would leak.
    """

    def __init__(
        self, endpoint: str, *, insecure: bool = True, service_name: str = "garmin-exporter"
    ) -> None:
        from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource

        exporter = OTLPMetricExporter(endpoint=endpoint, insecure=insecure)
        reader = PeriodicExportingMetricReader(
            exporter,
            # A CronJob's whole life is a few seconds; the explicit force_flush in
            # close() is what actually ships the data. The interval just stops the
            # reader firing mid-run.
            export_interval_millis=60_000,
        )
        self._provider = MeterProvider(
            resource=Resource.create({"service.name": service_name}),
            metric_readers=[reader],
        )
        self._meter = self._provider.get_meter("garmin_exporter")
        self._gauges: dict[str, object] = {}
        self._endpoint = endpoint

    def _gauge(self, name: str) -> object:
        gauge = self._gauges.get(name)
        if gauge is None:
            gauge = self._meter.create_gauge(name)
            self._gauges[name] = gauge
        return gauge

    def emit(self, samples: Sequence[Sample]) -> None:
        for item in samples:
            gauge = self._gauge(item.name)
            gauge.set(item.value, dict(item.labels))  # type: ignore[attr-defined]

    def close(self) -> None:
        self._provider.force_flush()
        self._provider.shutdown()
        log.info("otlp flush complete", extra={"endpoint": self._endpoint})


def build_emitter(cfg: Config) -> Emitter:
    if cfg.dry_run or cfg.emit == "none":
        return NullEmitter()
    if cfg.emit == "vm":
        return VmImportEmitter(_require_vm(cfg))
    if cfg.emit == "otlp":
        return OtlpEmitter(cfg.otlp_endpoint, insecure=cfg.otlp_insecure)
    return _FanOut(
        [
            OtlpEmitter(cfg.otlp_endpoint, insecure=cfg.otlp_insecure),
            VmImportEmitter(_require_vm(cfg)),
        ]
    )


class _FanOut:
    def __init__(self, emitters: list[Emitter]) -> None:
        self._emitters = emitters

    def emit(self, samples: Sequence[Sample]) -> None:
        for emitter in self._emitters:
            emitter.emit(samples)

    def close(self) -> None:
        for emitter in self._emitters:
            emitter.close()


def _require_vm(cfg: Config) -> str:
    if not cfg.vm_url:
        raise ValueError("GARMIN_EMIT=vm requires GARMIN_VM_URL, e.g. http://victoriametrics:8428")
    return cfg.vm_url


def render_prometheus(samples: Sequence[Sample]) -> str:
    """Prometheus exposition with an explicit millisecond timestamp per sample.

    The timestamp is the whole point of this path: it is what lets history be
    written, and what makes re-running a backfill idempotent.
    """
    if not samples:
        return ""
    return "\n".join(_render_line(item) for item in samples) + "\n"


def _render_line(item: Sample) -> str:
    if not item.labels:
        return f"{item.name} {_fmt_value(item.value)} {item.timestamp_ms}"
    labels = ",".join(f'{key}="{_escape(value)}"' for key, value in sorted(item.labels.items()))
    return f"{item.name}{{{labels}}} {_fmt_value(item.value)} {item.timestamp_ms}"


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _fmt_value(value: float) -> str:
    """Render floats without an exponent or trailing ``.0``.

    Prometheus exposition accepts both, but ``1e+06`` style output trips older
    parsers, and integer-valued gauges read better without the decimal.
    """
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return repr(float(value))
