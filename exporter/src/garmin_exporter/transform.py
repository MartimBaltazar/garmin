"""Garmin JSON -> typed Samples.

Design rules:

* One function per Garmin source. No function raises on a missing field — Garmin
  omits data habitually (no sleep record that night, no VO2 max for a day) and an
  absent datapoint is not a fault.
* Metric names follow the contract in ``dashboards/README.md``. ``garmin_daily_*``
  means exactly one sample per calendar date.
* Field paths are **documented guesses**, not verified against a live response.
  ``garmin-exporter dump`` is the mechanism that verifies them; every function
  logs which metric it could not extract so the fix is obvious.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from .fetch import RawPayloads, as_list
from .models import Sample, midnight_utc, sample

log = logging.getLogger(__name__)

_DATE_KEYS = ("calendarDate", "date", "startDate", "calendar_date", "summaryDate")


def _get_path(payload: Any, path: str) -> Any:
    """Look up ``a.b.c`` in nested dicts, returning None if any hop is missing."""
    current = payload
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
        if current is None:
            return None
    return current


def _first(payload: Any, *paths: str) -> Any:
    for path in paths:
        value = _get_path(payload, path)
        if value is not None:
            return value
    return None


def _day_of(payload: Any) -> datetime | None:
    for key in _DATE_KEYS:
        raw = _get_path(payload, key) if isinstance(payload, dict) else None
        if isinstance(raw, str) and len(raw) >= 10:
            try:
                return midnight_utc(raw[:10])
            except ValueError:
                continue
    return None


def _instant(payload: Any, *paths: str) -> datetime | None:
    """Parse a Garmin ``YYYY-MM-DD HH:MM:SS`` UTC string."""
    for path in paths:
        raw = _get_path(payload, path)
        if isinstance(raw, str) and len(raw) >= 19:
            try:
                return datetime.strptime(raw[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
            except ValueError:
                continue
        if isinstance(raw, (int, float)) and raw > 0:
            return datetime.fromtimestamp(raw / 1000, tz=UTC)
    return None


class _Collector:
    """Accumulates samples and remembers which metrics produced nothing."""

    def __init__(self) -> None:
        self.samples: list[Sample] = []
        self.missing: set[str] = set()

    def add(self, result: Sample | None, name: str) -> None:
        if result is None:
            self.missing.add(name)
        else:
            self.samples.append(result)

    def extend(self, results: list[Sample]) -> None:
        self.samples.extend(results)

    def note_missing(self, name: str) -> None:
        self.missing.add(name)


# --------------------------------------------------------------------------- #
# Daily summaries: steps, resting HR, calories, stress, intensity minutes
# --------------------------------------------------------------------------- #
def summary_samples(payload: Any) -> list[Sample]:
    out = _Collector()
    for item in as_list(payload, "summaries", "dailySummaries"):
        ts = _day_of(item)
        if ts is None:
            out.note_missing("summary:no_date")
            continue
        out.add(sample("garmin_daily_steps", _first(item, "totalSteps"), ts), "garmin_daily_steps")
        out.add(
            sample("garmin_daily_resting_heart_rate", _first(item, "restingHeartRate"), ts),
            "garmin_daily_resting_heart_rate",
        )
        out.add(
            sample("garmin_daily_calories", _first(item, "totalKilocalories"), ts),
            "garmin_daily_calories",
        )
        out.add(
            sample("garmin_daily_stress_avg", _first(item, "averageStressLevel"), ts),
            "garmin_daily_stress_avg",
        )
        out.add(
            sample("garmin_daily_floors", _first(item, "floorsAscended"), ts),
            "garmin_daily_floors",
        )
        # Vigorous minutes count double toward Garmin's own goal, so we publish
        # both raw series and let the query decide. Guessing here would bake a
        # rule into the storage layer.
        out.add(
            sample(
                "garmin_daily_intensity_minutes",
                _first(item, "moderateIntensityMinutes"),
                ts,
                kind="moderate",
            ),
            "garmin_daily_intensity_minutes",
        )
        out.add(
            sample(
                "garmin_daily_intensity_minutes",
                _first(item, "vigorousIntensityMinutes"),
                ts,
                kind="vigorous",
            ),
            "garmin_daily_intensity_minutes",
        )
    _report("summaries", out)
    return out.samples


# --------------------------------------------------------------------------- #
# Sleep
# --------------------------------------------------------------------------- #
def sleep_samples(payload: Any) -> list[Sample]:
    out = _Collector()
    for item in as_list(payload, "dailySleepDTOList", "sleep"):
        dto = item.get("dailySleepDTO") if isinstance(item, dict) else None
        node = dto if isinstance(dto, dict) else item
        ts = _day_of(node) or _day_of(item)
        if ts is None:
            out.note_missing("sleep:no_date")
            continue
        # sleepTimeSeconds is asleep time; the three stages must not be confused
        # with it, and awake time is deliberately separate.
        out.add(
            sample("garmin_daily_sleep_duration_seconds", _first(node, "sleepTimeSeconds"), ts),
            "garmin_daily_sleep_duration_seconds",
        )
        out.add(
            sample("garmin_daily_deep_sleep_seconds", _first(node, "deepSleepSeconds"), ts),
            "garmin_daily_deep_sleep_seconds",
        )
        out.add(
            sample("garmin_daily_rem_sleep_seconds", _first(node, "remSleepSeconds"), ts),
            "garmin_daily_rem_sleep_seconds",
        )
        out.add(
            sample("garmin_daily_light_sleep_seconds", _first(node, "lightSleepSeconds"), ts),
            "garmin_daily_light_sleep_seconds",
        )
        out.add(
            sample("garmin_daily_awake_sleep_seconds", _first(node, "awakeSleepSeconds"), ts),
            "garmin_daily_awake_sleep_seconds",
        )
        out.add(
            sample(
                "garmin_daily_sleep_score",
                _first(node, "sleepScores.overall.value", "sleepScore"),
                ts,
            ),
            "garmin_daily_sleep_score",
        )
        out.add(
            sample("garmin_daily_sleep_stress_avg", _first(node, "avgSleepStress"), ts),
            "garmin_daily_sleep_stress_avg",
        )
        out.add(
            sample("garmin_daily_spo2_percent", _first(node, "averageSpO2Value"), ts),
            "garmin_daily_spo2_percent",
        )
        out.add(
            sample("garmin_daily_respiration_avg", _first(node, "averageRespirationValue"), ts),
            "garmin_daily_respiration_avg",
        )
    _report("sleep", out)
    return out.samples


# --------------------------------------------------------------------------- #
# Resting HR, stress, body battery, HRV, VO2 max
# --------------------------------------------------------------------------- #
def rhr_samples(payload: Any) -> list[Sample]:
    out = _Collector()
    for item in as_list(payload, "restingHeartRate", "rhr"):
        ts = _day_of(item)
        if ts is None:
            continue
        # Fallback path because get_rhr_daily nests values under allMetrics.
        value = _first(
            item,
            "restingHeartRate",
            "value",
            "allMetrics.metricsMap.WELLNESS_RESTING_HEART_RATE.0.value",
        )
        out.add(
            sample("garmin_daily_resting_heart_rate", value, ts), "garmin_daily_resting_heart_rate"
        )
    _report("rhr", out)
    return out.samples


def stress_samples(payload: Any) -> list[Sample]:
    out = _Collector()
    for item in as_list(payload, "stress", "dailyStress"):
        ts = _day_of(item)
        if ts is None:
            continue
        out.add(
            sample(
                "garmin_daily_stress_avg",
                _first(item, "overallStressLevel", "averageStressLevel", "value"),
                ts,
            ),
            "garmin_daily_stress_avg",
        )
    _report("stress", out)
    return out.samples


def body_battery_samples(payload: Any) -> list[Sample]:
    out = _Collector()
    for item in as_list(payload, "bodyBattery", "dailyBodyBattery"):
        ts = _day_of(item)
        if ts is None:
            continue
        out.add(
            sample(
                "garmin_daily_body_battery_max",
                _first(item, "stats.max", "charged", "valueMax"),
                ts,
            ),
            "garmin_daily_body_battery_max",
        )
        out.add(
            sample(
                "garmin_daily_body_battery_min",
                _first(item, "stats.min", "drained", "valueMin"),
                ts,
            ),
            "garmin_daily_body_battery_min",
        )
    _report("body_battery", out)
    return out.samples


def hrv_samples(payload: Any) -> list[Sample]:
    out = _Collector()
    for item in as_list(payload, "hrv", "hrvData"):
        ts = _day_of(item) or _day_of(item.get("hrvSummary", {}) if isinstance(item, dict) else {})
        if ts is None:
            continue
        out.add(
            sample(
                "garmin_daily_hrv_ms",
                _first(item, "hrvSummary.lastNightAvg", "lastNightAvg", "weeklyAvg"),
                ts,
            ),
            "garmin_daily_hrv_ms",
        )
    _report("hrv", out)
    return out.samples


def vo2max_samples(payload: Any) -> list[Sample]:
    out = _Collector()
    for item in as_list(payload, "maxMetrics", "metrics"):
        ts = _day_of(item)
        if ts is None:
            continue
        for sport in ("generic", "cycling"):
            block = item.get(sport) if isinstance(item, dict) else None
            if not isinstance(block, dict):
                continue
            out.add(
                sample(
                    "garmin_daily_vo2max",
                    _first(block, "vo2MaxPreciseValue", "vo2MaxValue"),
                    ts,
                    sport=sport,
                ),
                "garmin_daily_vo2max",
            )
    _report("vo2max", out)
    return out.samples


# --------------------------------------------------------------------------- #
# Activities
# --------------------------------------------------------------------------- #
def activity_samples(activities: list[Any]) -> list[Sample]:
    out = _Collector()
    for item in activities:
        if not isinstance(item, dict):
            continue
        activity_id = _first(item, "activityId")
        if activity_id is None:
            continue
        ts = _instant(item, "startTimeGMT", "startTimeLocal", "beginTimestamp")
        if ts is None:
            out.note_missing("activity:no_start_time")
            continue
        labels = {
            "activity_id": str(int(activity_id)),
            "activity_type": str(
                _first(item, "activityType.typeKey", "activityType.typeId") or "unknown"
            ),
        }
        out.add(
            sample("garmin_activity_duration_seconds", _first(item, "duration"), ts, **labels),
            "garmin_activity_duration_seconds",
        )
        out.add(
            sample("garmin_activity_distance_meters", _first(item, "distance"), ts, **labels),
            "garmin_activity_distance_meters",
        )
        out.add(
            sample("garmin_activity_avg_heart_rate", _first(item, "averageHR"), ts, **labels),
            "garmin_activity_avg_heart_rate",
        )
        out.add(
            sample("garmin_activity_max_heart_rate", _first(item, "maxHR"), ts, **labels),
            "garmin_activity_max_heart_rate",
        )
        out.add(
            sample("garmin_activity_calories", _first(item, "calories"), ts, **labels),
            "garmin_activity_calories",
        )
        out.add(
            sample("garmin_activity_training_effect", _first(item, "trainingEffect"), ts, **labels),
            "garmin_activity_training_effect",
        )
        out.add(
            sample(
                "garmin_activity_elevation_gain_meters",
                _first(item, "elevationGain"),
                ts,
                **labels,
            ),
            "garmin_activity_elevation_gain_meters",
        )
    _report("activities", out)
    return out.samples


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
_HANDLERS = {
    "summaries": summary_samples,
    "sleep": sleep_samples,
    "rhr": rhr_samples,
    "stress": stress_samples,
    "body_battery": body_battery_samples,
    "hrv": hrv_samples,
    "vo2max": vo2max_samples,
}


def transform_all(payloads: RawPayloads) -> list[Sample]:
    samples: list[Sample] = []
    for key, handler in _HANDLERS.items():
        if key not in payloads.sources:
            continue
        samples.extend(handler(payloads.sources[key]))
    samples.extend(activity_samples(payloads.activities))
    return _dedupe(samples)


def _dedupe(samples: list[Sample]) -> list[Sample]:
    """Later sources win on an identical (name, labels, timestamp).

    Two endpoints can legitimately report the same daily value (summary and
    RHR). The TSDB would overwrite them anyway; doing it here keeps the wire
    payload small and makes the precedence deterministic.
    """
    by_key: dict[tuple[str, tuple[tuple[str, str], ...], int], Sample] = {}
    for item in samples:
        key = (item.name, tuple(sorted(item.labels.items())), item.timestamp_ms)
        by_key[key] = item
    return list(by_key.values())


def _report(source: str, collector: _Collector) -> None:
    if collector.missing:
        log.debug(
            "fields not present in payload",
            extra={"source": source, "missing": sorted(collector.missing)},
        )
