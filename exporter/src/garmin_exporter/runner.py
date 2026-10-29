"""Run orchestration: fetch -> transform -> emit, plus self-observability.

The exporter must be able to report on *itself* while failing, otherwise the
"no successful run in 6h" alert is blind to the case that matters most. So a
failed run still emits ``garmin_exporter_run_success 0`` and the previous
``last_success_timestamp_seconds``, read from a small state file on the token PVC
(CronJob pods are otherwise stateless).
"""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .auth import AuthError
from .config import Config
from .emit import Emitter, VmImportEmitter, build_emitter
from .fetch import Fetcher, RawPayloads
from .models import Sample, sample
from .transform import transform_all

log = logging.getLogger(__name__)

METRIC_LAST_SUCCESS = "garmin_exporter_last_success_timestamp_seconds"
METRIC_RUN_SUCCESS = "garmin_exporter_run_success"
METRIC_RUN_DURATION = "garmin_exporter_run_duration_seconds"
METRIC_API_ERRORS = "garmin_exporter_api_errors_total"
METRIC_SAMPLES = "garmin_exporter_samples_emitted"


def run_live(cfg: Config) -> int:
    """One CronJob tick: refetch a short trailing window and push it live."""
    started = time.monotonic()
    today = datetime.now(UTC).date()
    start = today - timedelta(days=cfg.lookback_days)
    emitter = build_emitter(cfg)
    try:
        payloads = Fetcher(cfg).fetch_range(start.isoformat(), today.isoformat())
        samples = transform_all(payloads)
        if not samples:
            log.warning("no samples produced", extra={"errors": dict(payloads.errors)})
        emitter.emit(samples)
        _record_success(cfg)
        _emit_self(
            emitter,
            success=True,
            duration=time.monotonic() - started,
            samples=len(samples),
            errors=payloads.errors,
            cfg=cfg,
        )
        return 0
    except AuthError as exc:
        # Expected in two situations: first deploy, and a revoked refresh token.
        # A traceback here would bury the one thing the operator must fix.
        log.error("authentication failed", extra={"error": str(exc)})
        _emit_self(
            emitter,
            success=False,
            duration=time.monotonic() - started,
            samples=0,
            errors={"auth_error": 1},
            cfg=cfg,
            error=str(exc),
        )
        return 1
    except Exception as exc:  # noqa: BLE001 - the process exit code is the contract
        log.exception("run failed")
        _emit_self(
            emitter,
            success=False,
            duration=time.monotonic() - started,
            samples=0,
            errors={type(exc).__name__: 1},
            cfg=cfg,
            error=str(exc),
        )
        return 1
    finally:
        emitter.close()


def run_backfill(cfg: Config, start: str, end: str, chunk_days: int = 30) -> int:
    """Replay history with explicit timestamps.

    Always writes to VictoriaMetrics directly: the OTel SDK stamps at
    observation time, so OTLP cannot express the past. ``GARMIN_EMIT`` is
    ignored here on purpose.
    """
    if not cfg.vm_url:
        log.error("backfill requires GARMIN_VM_URL (OTLP cannot carry event timestamps)")
        return 2

    started = time.monotonic()
    emitter = VmImportEmitter(cfg.vm_url)
    total = 0
    errors: dict[str, int] = {}
    try:
        cursor = datetime.strptime(start, "%Y-%m-%d").date()
        finish = datetime.strptime(end, "%Y-%m-%d").date()
        if cursor > finish:
            log.error("--from must not be after --to")
            return 2

        while cursor <= finish:
            chunk_end = min(cursor + timedelta(days=chunk_days - 1), finish)
            log.info(
                "backfill chunk", extra={"from": cursor.isoformat(), "to": chunk_end.isoformat()}
            )
            payloads = Fetcher(cfg).fetch_range(cursor.isoformat(), chunk_end.isoformat())
            samples = transform_all(payloads)
            emitter.emit(samples)
            total += len(samples)
            for key, count in payloads.errors.items():
                errors[key] = errors.get(key, 0) + count
            cursor = chunk_end + timedelta(days=1)

        _record_success(cfg)
        log.info(
            "backfill complete",
            extra={"samples": total, "duration_s": round(time.monotonic() - started, 2)},
        )
        return 0
    except Exception:  # noqa: BLE001
        log.exception("backfill failed", extra={"samples_before_failure": total})
        return 1
    finally:
        emitter.close()


def run_dump(cfg: Config, day: str, out_dir: Path) -> int:
    """Write the raw Garmin payloads for one day.

    This is the Phase 1 task from PLAN.md, and the mechanism that verifies the
    field paths in ``transform`` against reality. Deliberately dumps raw bytes
    rather than samples, so the transform can be rewritten without refetching.
    """
    target = out_dir / day
    payloads = Fetcher(cfg).fetch_range(day, day)
    # Directory is created only after a successful fetch: a failed dump should
    # not leave an empty directory that looks like it worked.
    target.mkdir(parents=True, exist_ok=True)
    _write(target / "raw.json", payloads.to_dict())
    log.info(
        "dumped raw payloads",
        extra={
            "path": str(target / "raw.json"),
            "sources": sorted(payloads.sources),
            "activities": len(payloads.activities),
            "errors": dict(payloads.errors),
        },
    )

    # A human-readable inventory, so the mapping can be corrected by reading one
    # file instead of scrolling a 40k-line JSON blob.
    _write(target / "inventory.json", _inventory(payloads))
    return 0


def _inventory(payloads: RawPayloads) -> dict[str, object]:
    def keys_of(value: object) -> list[str]:
        if isinstance(value, dict):
            return sorted(value.keys())
        if isinstance(value, list) and value and isinstance(value[0], dict):
            return sorted(value[0].keys())
        return []

    return {
        "sources": {key: keys_of(value) for key, value in payloads.sources.items()},
        "activities": keys_of(payloads.activities),
        "errors": dict(payloads.errors),
    }


def _write(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, indent=2, default=str, sort_keys=False) + "\n")


def _record_success(cfg: Config) -> None:
    try:
        cfg.tokenstore.mkdir(parents=True, exist_ok=True)
        state = _read_state(cfg)
        state["last_success"] = int(datetime.now(UTC).timestamp())
        state.pop("last_error", None)
        cfg.state_file().write_text(json.dumps(state, indent=2) + "\n")
    except OSError as exc:
        # Losing the state file degrades the alert to "always stale", which is
        # wrong but not dangerous. Never fail a good run over it.
        log.warning("could not persist state", extra={"error": str(exc)})


def _read_state(cfg: Config) -> dict[str, object]:
    try:
        raw = cfg.state_file().read_text()
    except OSError:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _emit_self(
    emitter: Emitter,
    *,
    success: bool,
    duration: float,
    samples: int,
    errors: dict[str, int],
    cfg: Config,
    error: str | None = None,
) -> None:
    now = datetime.now(UTC)
    last_success = _read_state(cfg).get("last_success")

    self_samples: list[Sample] = [
        item
        for item in (
            sample(METRIC_RUN_SUCCESS, 1.0 if success else 0.0, now),
            sample(METRIC_RUN_DURATION, duration, now),
            sample(METRIC_SAMPLES, float(samples), now),
            sample(METRIC_LAST_SUCCESS, last_success, now),
        )
        if item is not None
    ]
    for kind, count in errors.items():
        item = sample(METRIC_API_ERRORS, float(count), now, kind=kind)
        if item is not None:
            self_samples.append(item)

    try:
        emitter.emit(self_samples)
    except Exception:  # noqa: BLE001 - never mask the original failure
        log.exception("could not emit self-observability metrics")

    if not success:
        log.error(
            "run outcome",
            extra={"success": False, "duration_s": round(duration, 2), "error": error},
        )
