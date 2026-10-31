"""Garmin fetch layer.

Only ``garminconnect`` calls live here. Nothing interprets the payloads — that
is ``transform``'s job — so the mapping from HTTP shape to metrics can be
swapped without touching I/O, and vice versa.

Two facts about the real API, both learned the hard way, are encoded here:

1. **Not every "range" endpoint takes a range.** ``get_daily_sleep`` sounds like
   the range variant and is not: its second parameter is
   ``non_sleep_buffer_minutes``. Passing a date there raises
   ``non_sleep_buffer_minutes must be an integer``. The range variant is
   ``get_sleep_daily(start, end)``.

2. **Some endpoints have a hard server-side 28-day limit.** ``get_daily_stress_stats``
   and ``get_daily_body_battery_stats`` return HTTP 400 beyond 28 days and the
   library does *not* chunk them for you (unlike ``get_sleep_daily``). So the
   fetcher chunks every source, and callers do not have to know.
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import date, timedelta
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

# The tightest server-side limit of any source we call. Garmin counts the
# *difference* in days, so a 28-day window (27 days apart) is inside the limit.
MAX_SPAN_DAYS = 28


@dataclass(frozen=True, slots=True)
class Source:
    """One Garmin endpoint and how to call it."""

    key: str
    method: str
    #: Days per request. Lower than MAX_SPAN_DAYS only where Garmin is stricter.
    span_days: int = MAX_SPAN_DAYS


#: Every endpoint a run pulls. ``sleep`` is ``get_sleep_daily``, not
#: ``get_daily_sleep`` — see the module docstring.
SOURCES: tuple[Source, ...] = (
    Source("summaries", "get_daily_summaries"),
    Source("sleep", "get_sleep_daily"),
    Source("rhr", "get_rhr_daily"),
    Source("stress", "get_daily_stress_stats"),
    Source("body_battery", "get_daily_body_battery_stats"),
    Source("hrv", "get_hrv_data_range"),
    Source("vo2max", "get_max_metrics_range"),
)

#: Intraday endpoints. Garmin serves these **one day per call** (span_days=1),
#: with millisecond-epoch arrays inside. Compared with the daily sources above
#: these produce ~100x the samples, which is the granularity the dashboards'
#: minute-resolution panels need. Intraday history older than ~6 months is in
#: Garmin's cold storage and unavailable to these endpoints -- the daily
#: summaries above remain available for any age.
INTRADAY_SOURCES: tuple[Source, ...] = (
    Source("intraday_hr", "get_heart_rates", span_days=1),
    Source("intraday_stress", "get_stress_data", span_days=1),
    Source("intraday_respiration", "get_respiration_data", span_days=1),
    Source("intraday_steps", "get_steps_data", span_days=1),
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

        for source in (*SOURCES, *INTRADAY_SOURCES):
            if not hasattr(self.client, source.method):
                log.warning("method missing on client, skipping", extra={"method": source.method})
                payloads.errors[f"{source.key}:missing_method"] += 1
                continue
            try:
                chunks = [
                    self.call(
                        source.method,
                        *(window_start,) if source.span_days == 1 else (window_start, window_end),
                    )
                    for window_start, window_end in iter_windows(start, end, source.span_days)
                ]
                payloads.sources[source.key] = merge_chunks(chunks)
            except Exception as exc:  # noqa: BLE001 - one source must not sink the run
                log.warning(
                    "source failed",
                    extra={"source": source.key, "method": source.method, "error": str(exc)},
                )
                payloads.errors[f"{source.key}:error"] += 1

        try:
            payloads.activities = as_list(self.call(ACTIVITY_SOURCE, start, end))
        except Exception as exc:  # noqa: BLE001
            log.warning("activities failed", extra={"error": str(exc)})
            payloads.errors["activities:error"] += 1

        return payloads


def iter_windows(start: str, end: str, span_days: int) -> Iterator[tuple[str, str]]:
    """Split an inclusive date range into windows of at most ``span_days``."""
    if span_days < 1:
        raise ValueError(f"span_days must be >= 1, got {span_days}")
    first = date.fromisoformat(start)
    last = date.fromisoformat(end)
    if first > last:
        raise ValueError(f"start {start} is after end {end}")
    cursor = first
    while cursor <= last:
        window_end = min(cursor + timedelta(days=span_days - 1), last)
        yield cursor.isoformat(), window_end.isoformat()
        cursor = window_end + timedelta(days=1)


def merge_chunks(chunks: list[Any]) -> Any:
    """Concatenate the per-window responses of one source.

    The endpoints are not uniform: most return a bare list, ``get_daily_summaries``
    returns ``{"data": [...]}``, and ``get_hrv_data_range`` returns
    ``{"hrvSummaries": [...], "userProfilePk": N}``. Merging per key handles all
    three without special-casing any of them, and keeps ``dump`` output shaped
    like a single response so it stays readable.
    """
    present = [chunk for chunk in chunks if chunk is not None]
    if not present:
        return None
    if len(present) == 1:
        return present[0]
    if all(isinstance(chunk, list) for chunk in present):
        return [item for chunk in present for item in chunk]
    if all(isinstance(chunk, dict) for chunk in present):
        merged: dict[str, Any] = dict(present[0])
        for chunk in present[1:]:
            for key, value in chunk.items():
                existing = merged.get(key)
                if isinstance(value, list) and isinstance(existing, list):
                    merged[key] = [*existing, *value]
        return merged
    # Mixed shapes across windows: keep the first rather than inventing a merge.
    log.warning(
        "could not merge chunks of different shapes", extra={"shape": str(type(present[0]))}
    )
    return present[0]


def _as_list(payload: Any, *keys: str) -> list[Any] | None:
    """Find the first list in a Garmin payload.

    The endpoints are not uniform: some return a bare list, some wrap it as
    ``{"data": [...]}``, some nest one level deeper. Rather than guess per
    endpoint, search shallowly and let ``dump`` reveal the shape.
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
    """Public alias used by the transform layer; always returns a list."""
    return _as_list(payload, *keys) or []
