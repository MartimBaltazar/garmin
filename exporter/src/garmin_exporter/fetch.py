"""Garmin fetch layer.

Only ``garminconnect`` calls live here. Nothing interprets the payloads — that
is ``transform``'s job — so the mapping from HTTP shape to metrics can be
swapped without touching I/O, and vice versa.

Range endpoints are preferred on purpose: ``get_daily_summaries`` and friends
return a month per call, which makes backfill a handful of requests instead of
one per day. That matters because Garmin rate-limits and archives intraday data.
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .auth import get_client
from .config import Config

log = logging.getLogger(__name__)

# (local key, Garmin method, takes a date range)
RANGE_SOURCES: tuple[tuple[str, str], ...] = (
    ("summaries", "get_daily_summaries"),
    ("sleep", "get_daily_sleep"),
    ("rhr", "get_rhr_daily"),
    ("stress", "get_daily_stress_stats"),
    ("body_battery", "get_daily_body_battery_stats"),
    ("hrv", "get_hrv_data_range"),
    ("vo2max", "get_max_metrics_range"),
)

ACTIVITY_SOURCE = "get_activities_by_date"


@dataclass
class RawPayloads:
    """Everything one run pulled, unmodified.

    Kept as raw dicts so ``dump`` can write exactly what Garmin sent — that is
    how the transform mapping gets verified against reality.
    """

    start: str
    end: str
    sources: dict[str, Any] = field(default_factory=dict)
    activities: list[Any] = field(default_factory=list)
    errors: Counter[str] = field(default_factory=Counter)

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": self.start,
            "end": self.end,
            "sources": self.sources,
            "activities": self.activities,
            "errors": dict(self.errors),
        }


class Fetcher:
    def __init__(self, cfg: Config, client: Any | None = None) -> None:
        self._cfg = cfg
        self._client = client
        self._last_call = 0.0
        self._retry = retry(
            retry=retry_if_exception_type(Exception),
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=1, min=2, max=30),
            reraise=True,
        )(self._call_once)

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = get_client(self._cfg)
        return self._client

    def _throttle(self) -> None:
        """Garmin rate-limit guard: never more than one call per N seconds."""
        elapsed = time.monotonic() - self._last_call
        remaining = self._cfg.request_pause_seconds - elapsed
        if remaining > 0:
            time.sleep(remaining)
        self._last_call = time.monotonic()

    def _call_once(self, method_name: str, *args: Any) -> Any:
        method: Callable[..., Any] = getattr(self.client, method_name)
        self._throttle()
        return method(*args)

    def call(self, method_name: str, *args: Any) -> Any:
        """Call a Garmin method. Raises on failure; caller decides what is fatal."""
        log.debug("garmin call", extra={"method": method_name, "args": args})
        return self._retry(method_name, *args)

    def fetch_range(self, start: str, end: str) -> RawPayloads:
        payloads = RawPayloads(start=start, end=end)

        for key, method_name in RANGE_SOURCES:
            if not hasattr(self.client, method_name):
                log.warning("method missing on client, skipping", extra={"method": method_name})
                payloads.errors[f"{key}:missing_method"] += 1
                continue
            try:
                payloads.sources[key] = self.call(method_name, start, end)
            except Exception as exc:  # noqa: BLE001 - one source must not sink the run
                log.warning(
                    "source failed",
                    extra={"source": key, "method": method_name, "error": str(exc)},
                )
                payloads.errors[f"{key}:error"] += 1

        try:
            payloads.activities = _as_list(self.call(ACTIVITY_SOURCE, start, end)) or []
        except Exception as exc:  # noqa: BLE001
            log.warning("activities failed", extra={"error": str(exc)})
            payloads.errors["activities:error"] += 1

        return payloads


def _as_list(payload: Any, *keys: str) -> list[Any] | None:
    """Find the first list in a Garmin payload.

    The range endpoints are not uniform: some return a bare list, some wrap it
    as ``{"summaries": [...]}``, some nest one level deeper. Rather than guess
    per endpoint, search shallowly and let ``dump`` reveal the shape.
    """
    if payload is None:
        return None
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return None
    for key in keys:
        if isinstance(payload.get(key), list):
            return payload[key]
    for value in payload.values():
        if isinstance(value, list):
            return value
    for value in payload.values():
        if isinstance(value, dict):
            found = _as_list(value, *keys)
            if found is not None:
                return found
    return None


def as_list(payload: Any, *keys: str) -> list[Any]:
    """Public alias used by the transform layer."""
    return _as_list(payload, *keys) or []
