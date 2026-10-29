"""Garmin JSON -> typed Samples.

Every field path in this module has been checked against a real ``garmin-exporter
dump`` capture, not against documentation. Garmin's real payloads are nested far
deeper than the docs suggest, and the names differ (``totalSteps`` does not
exist; steps live at ``movement.steps.value``). The comments record what the real
shape is, because that is the part that is not guessable.

Design rules:

* One function per Garmin source. No function raises on a missing field — Garmin
  omits data habitually (no sleep record that night, no VO2 max for a day) and an
  absent datapoint is not a fault.
* Metric names follow the contract in ``dashboards/README.md``. ``garmin_daily_*``
  means exactly one sample per calendar date, stamped at 00:00 UTC.
* Only fields that exist in the real response are mapped. Metrics that would need
  a per-day fan-out (e.g. sleep-stage SpO2 and sleep stress, which live only in
  ``get_sleep_data``'s full payload) are deliberately left out: one cheap range
  call per 28 days beats one call per day for two extra series.
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
    """First non-None value among several candidate paths."""
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
    """Parse Garmin's ``YYYY-MM-DD HH:MM:SS`` (UTC) or a millisecond epoch."""
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

    def note_missing(self, name: str) -> None:
        self.missing.add(name)


# --------------------------------------------------------------------------- #
# Daily summaries
#
# Real shape (get_daily_summaries -> {"data": [...]}), one item per day:
#   movement.steps.value / .goal / .distanceInMeters
#   movement.floorsAscended.value
#   calories.burnedResting / .burnedActive / .burnedTotal
#   heartRate.minValue / .maxValue / .restingValue
#   intensityMinutes.moderate / .vigorous / .goal
#   stress.avgLevel / .maxLevel
#   bodyBattery.minValue / .maxValue / .latestValue
#   respiration.avgValue
#   hydration.consumedInMl
# --------------------------------------------------------------------------- #
def summary_samples(payload: Any) -> list[Sample]:
    out = _Collector()
    for item in as_list(payload, "data", "summaries", "dailySummaries"):
        ts = _day_of(item)
        if ts is None:
            out.note_missing("summary:no_date")
            continue

        out.add(
            sample("garmin_daily_steps", _first(item, "movement.steps.value"), ts),
            "garmin_daily_steps",
        )
        out.add(
            sample("garmin_daily_steps_goal", _first(item, "movement.steps.goal"), ts),
            "garmin_daily_steps_goal",
        )
        out.add(
            sample(
                "garmin_daily_distance_meters", _first(item, "movement.steps.distanceInMeters"), ts
            ),
            "garmin_daily_distance_meters",
        )
        out.add(
            sample("garmin_daily_floors", _first(item, "movement.floorsAscended.value"), ts),
            "garmin_daily_floors",
        )
        out.add(
            sample("garmin_daily_calories", _first(item, "calories.burnedTotal"), ts),
            "garmin_daily_calories",
        )
        out.add(
            sample("garmin_daily_calories_active", _first(item, "calories.burnedActive"), ts),
            "garmin_daily_calories_active",
        )
        out.add(
            sample("garmin_daily_calories_resting", _first(item, "calories.burnedResting"), ts),
            "garmin_daily_calories_resting",
        )
        out.add(
            sample("garmin_daily_resting_heart_rate", _first(item, "heartRate.restingValue"), ts),
            "garmin_daily_resting_heart_rate",
        )
        out.add(
            sample("garmin_daily_heart_rate_min", _first(item, "heartRate.minValue"), ts),
            "garmin_daily_heart_rate_min",
        )
        out.add(
            sample("garmin_daily_heart_rate_max", _first(item, "heartRate.maxValue"), ts),
            "garmin_daily_heart_rate_max",
        )
        out.add(
            sample("garmin_daily_stress_avg", _first(item, "stress.avgLevel"), ts),
            "garmin_daily_stress_avg",
        )
        out.add(
            sample("garmin_daily_stress_max", _first(item, "stress.maxLevel"), ts),
            "garmin_daily_stress_max",
        )
        # Vigorous minutes count double toward Garmin's own goal, so we publish
        # both raw series and let the query decide. Guessing here would bake a
        # rule into the storage layer.
        out.add(
            sample(
                "garmin_daily_intensity_minutes",
                _first(item, "intensityMinutes.moderate"),
                ts,
                kind="moderate",
            ),
            "garmin_daily_intensity_minutes",
        )
        out.add(
            sample(
                "garmin_daily_intensity_minutes",
                _first(item, "intensityMinutes.vigorous"),
                ts,
                kind="vigorous",
            ),
            "garmin_daily_intensity_minutes",
        )
        out.add(
            sample(
                "garmin_daily_intensity_goal_minutes", _first(item, "intensityMinutes.goal"), ts
            ),
            "garmin_daily_intensity_goal_minutes",
        )
        out.add(
            sample("garmin_daily_body_battery_min", _first(item, "bodyBattery.minValue"), ts),
            "garmin_daily_body_battery_min",
        )
        out.add(
            sample("garmin_daily_body_battery_max", _first(item, "bodyBattery.maxValue"), ts),
            "garmin_daily_body_battery_max",
        )
        out.add(
            sample("garmin_daily_body_battery_latest", _first(item, "bodyBattery.latestValue"), ts),
            "garmin_daily_body_battery_latest",
        )
        out.add(
            sample("garmin_daily_respiration_avg", _first(item, "respiration.avgValue"), ts),
            "garmin_daily_respiration_avg",
        )
        out.add(
            sample("garmin_daily_hydration_ml", _first(item, "hydration.consumedInMl"), ts),
            "garmin_daily_hydration_ml",
        )
    _report("summaries", out)
    return out.samples


# --------------------------------------------------------------------------- #
# Sleep
#
# get_sleep_daily returns a list of {calendarDate, values: {...}}. It is the
# RANGE variant; get_daily_sleep takes a single date and its second parameter is
# nonSleepBufferMinutes, which is why calling it with two dates fails.
#
#   values.totalSleepTimeInSeconds / deepTime / remTime / lightTime / awakeTime
#   values.sleepScore / respiration / sleepNeed / bodyBatteryChange
# --------------------------------------------------------------------------- #
_SLEEP_FIELDS = (
    ("garmin_daily_sleep_duration_seconds", "totalSleepTimeInSeconds"),
    ("garmin_daily_deep_sleep_seconds", "deepTime"),
    ("garmin_daily_rem_sleep_seconds", "remTime"),
    ("garmin_daily_light_sleep_seconds", "lightTime"),
    ("garmin_daily_awake_sleep_seconds", "awakeTime"),
    ("garmin_daily_sleep_score", "sleepScore"),
    ("garmin_daily_sleep_respiration_avg", "respiration"),
    ("garmin_daily_sleep_need_minutes", "sleepNeed"),
    ("garmin_daily_sleep_body_battery_change", "bodyBatteryChange"),
)


def sleep_samples(payload: Any) -> list[Sample]:
    out = _Collector()
    for item in as_list(payload, "individualStats", "sleep"):
        ts = _day_of(item)
        if ts is None:
            out.note_missing("sleep:no_date")
            continue
        # The stage values are nested under "values" in the range endpoint. Be
        # tolerant of the flat shape too, so a library change that hoists them
        # does not silently produce nothing.
        node = item.get("values") if isinstance(item.get("values"), dict) else item
        for metric, field in _SLEEP_FIELDS:
            out.add(sample(metric, _get_path(node, field), ts), metric)
    _report("sleep", out)
    return out.samples


# --------------------------------------------------------------------------- #
# Resting HR, stress, body battery, HRV, VO2 max
# --------------------------------------------------------------------------- #
def rhr_samples(payload: Any) -> list[Sample]:
    """get_rhr_daily -> [{calendarDate, value}].

    Duplicates ``summary_samples``' ``heartRate.restingValue``. Kept because it
    still works on days the summary endpoint has no record for; ``_dedupe``
    collapses the overlap.
    """
    out = _Collector()
    for item in as_list(payload, "restingHeartRate", "rhr"):
        ts = _day_of(item)
        if ts is None:
            continue
        out.add(
            sample("garmin_daily_resting_heart_rate", _first(item, "value"), ts),
            "garmin_daily_resting_heart_rate",
        )
    _report("rhr", out)
    return out.samples


def stress_samples(payload: Any) -> list[Sample]:
    """get_daily_stress_stats -> [{calendarDate, values: {...}}].

    ``values.*Duration`` are seconds (the summary endpoint reports the same
    numbers as ``*DurationInMillis``, divided by 1000).
    """
    out = _Collector()
    for item in as_list(payload, "stress", "dailyStress"):
        ts = _day_of(item)
        if ts is None:
            continue
        node = item.get("values") if isinstance(item.get("values"), dict) else item
        out.add(
            sample("garmin_daily_stress_avg", _first(node, "overallStressLevel"), ts),
            "garmin_daily_stress_avg",
        )
        for level, field in (
            ("rest", "restStressDuration"),
            ("low", "lowStressDuration"),
            ("medium", "mediumStressDuration"),
            ("high", "highStressDuration"),
        ):
            out.add(
                sample(
                    "garmin_daily_stress_duration_seconds", _get_path(node, field), ts, level=level
                ),
                "garmin_daily_stress_duration_seconds",
            )
    _report("stress", out)
    return out.samples


def body_battery_samples(payload: Any) -> list[Sample]:
    """get_daily_body_battery_stats -> [{calendarDate, values: {...}}]."""
    out = _Collector()
    for item in as_list(payload, "bodyBattery", "dailyBodyBattery"):
        ts = _day_of(item)
        if ts is None:
            continue
        node = item.get("values") if isinstance(item.get("values"), dict) else item
        out.add(
            sample("garmin_daily_body_battery_min", _first(node, "lowBodyBattery"), ts),
            "garmin_daily_body_battery_min",
        )
        out.add(
            sample("garmin_daily_body_battery_max", _first(node, "highBodyBattery"), ts),
            "garmin_daily_body_battery_max",
        )
    _report("body_battery", out)
    return out.samples


def hrv_samples(payload: Any) -> list[Sample]:
    """get_hrv_data_range -> {"hrvSummaries": [{calendarDate, ...}], "userProfilePk"}.

    ``baseline`` is null until Garmin has enough history, so the baseline series
    legitimately starts later than the others.
    """
    out = _Collector()
    for item in as_list(payload, "hrvSummaries", "hrv"):
        ts = _day_of(item)
        if ts is None:
            continue
        out.add(
            sample("garmin_daily_hrv_last_night_ms", _first(item, "lastNightAvg"), ts),
            "garmin_daily_hrv_last_night_ms",
        )
        out.add(
            sample("garmin_daily_hrv_weekly_avg_ms", _first(item, "weeklyAvg"), ts),
            "garmin_daily_hrv_weekly_avg_ms",
        )
        out.add(
            sample("garmin_daily_hrv_baseline_ms", _first(item, "baseline"), ts),
            "garmin_daily_hrv_baseline_ms",
        )
    _report("hrv", out)
    return out.samples


def vo2max_samples(payload: Any) -> list[Sample]:
    """get_max_metrics_range -> [{generic: {...}, cycling: null, ...}].

    The date lives inside each sport block, not on the wrapper, and entries only
    exist on days with a qualifying activity — VO2 max is sparse by nature, not
    every day.
    """
    out = _Collector()
    for item in as_list(payload, "maxMetrics", "metrics"):
        if not isinstance(item, dict):
            continue
        for sport in ("generic", "cycling"):
            block = item.get(sport)
            if not isinstance(block, dict):
                continue
            ts = _day_of(block)
            if ts is None:
                out.note_missing("vo2max:no_date")
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
#
# Real shape has no ``trainingEffect``: there are two separate effects, aerobic
# and anaerobic. ``beginTimestamp`` is a millisecond epoch; ``startTimeGMT`` is
# the same instant as ``YYYY-MM-DD HH:MM:SS``.
# --------------------------------------------------------------------------- #
def activity_samples(activities: list[Any]) -> list[Sample]:
    out = _Collector()
    for item in activities:
        if not isinstance(item, dict):
            continue
        activity_id = _first(item, "activityId")
        if activity_id is None:
            continue
        ts = _instant(item, "startTimeGMT", "beginTimestamp", "startTimeLocal")
        if ts is None:
            out.note_missing("activity:no_start_time")
            continue
        labels = {
            "activity_id": str(int(activity_id)),
            "activity_type": str(
                _first(item, "activityType.typeKey", "activityType.typeId") or "unknown"
            ),
        }
        for metric, field in (
            ("garmin_activity_duration_seconds", "duration"),
            ("garmin_activity_moving_seconds", "movingDuration"),
            ("garmin_activity_distance_meters", "distance"),
            ("garmin_activity_avg_heart_rate", "averageHR"),
            ("garmin_activity_max_heart_rate", "maxHR"),
            ("garmin_activity_calories", "calories"),
            ("garmin_activity_aerobic_training_effect", "aerobicTrainingEffect"),
            ("garmin_activity_anaerobic_training_effect", "anaerobicTrainingEffect"),
            ("garmin_activity_elevation_gain_meters", "elevationGain"),
        ):
            out.add(sample(metric, _first(item, field), ts, **labels), metric)
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

    Several endpoints legitimately report the same daily value (summaries and the
    RHR endpoint both give resting heart rate; summaries and the body-battery
    endpoint both give min/max). The TSDB would overwrite them anyway; doing it
    here keeps the wire payload small and makes the precedence deterministic.
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
