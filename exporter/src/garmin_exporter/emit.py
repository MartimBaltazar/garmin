"""Emitters: OTLP (live path) and VictoriaMetrics import (backfill path).

Two paths, deliberately:

* **OTLP** goes through the OTel Collector, which is what PLAN.md asks for and
  what makes the pipeline observable. The datapoints carry the sample's own
  event timestamp, so this path is idempotent too: re-running overwrites the
  same points instead of appending new ones.
* **VM import** POSTs Prometheus exposition with an explicit timestamp to
  ``/api/v1/import/prometheus``. This is what writes *history* -- a bounded
  backfill job -- and it is the path to use when the Collector is not in the
  way.

Both write the same series: same names, same labels, same timestamps. The two
paths differ only in transport, so a backfilled day and a live day are one
series, not two.
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
    """Push samples to the OTel Collector over OTLP/gRPC.

    The SDK's gauge API cannot express this pipeline's data model (see
    ``otlp`` for why), so the OTLP request is built directly. The Collector
    stays a pass-through: it does not need to know about timestamps.
    """

    def __init__(
        self,
        endpoint: str,
        *,
        insecure: bool = True,
        timeout: float = 15.0,
    ) -> None:
        from .otlp import OtlpExporter

        self._exporter = OtlpExporter(endpoint, insecure=insecure, timeout=timeout)
        self._endpoint = endpoint

    def emit(self, samples: Sequence[Sample]) -> None:
        self._exporter.export(samples)

    def close(self) -> None:
        self._exporter.close()
        log.info("otlp export complete", extra={"endpoint": self._endpoint})


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
