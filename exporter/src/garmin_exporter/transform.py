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
            # Garmin uses both "YYYY-MM-DD HH:MM:SS" and "YYYY-MM-DDTHH:MM:SS".
            try:
                return datetime.strptime(raw[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S").replace(
                    tzinfo=UTC
                )
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


_STAGES = {0: "deep", 1: "light", 2: "rem", 3: "awake"}


def sleep_stage_samples(payload: Any) -> list[Sample]:
    """get_sleep_data -> {'sleepLevels': [{'startGMT','endGMT','activityLevel'}, ...]}.

    Each sleep-stage segment becomes `garmin_sleep_stage` stamped at the
    segment start with the stage label: this is what a state-timeline panel
    plots. The last segment is duplicated at its end time, so a stage does not
    visually truncate if the next one starts after it (the same trick the
    upstream project applies for the identical reason).
    """
    out = _Collector()
    for item in as_list(payload, "sleepLevels"):
        if not isinstance(item, dict):
            continue
        level = item.get("activityLevel")
        start = _instant(item, "startGMT", "startTimeGMT", "startTimestampGMT")
        end = _instant(item, "endGMT", "endTimeGMT", "endTimestampGMT")
        if level is None or start is None:
            out.note_missing("sleep:no_segment")
            continue
        stage = _STAGES.get(int(level))
        if stage is None:
            continue
        out.add(
            sample("garmin_sleep_stage", float(level), start, stage=stage), "garmin_sleep_stage"
        )
        if end is not None and end > start:
            out.add(
                sample("garmin_sleep_stage", float(level), end, stage=stage), "garmin_sleep_stage"
            )
    _report("sleepStage", out)
    return out.samples


def spo2_samples(payload: Any) -> list[Sample]:
    """get_sleep_data -> {'wellnessEpochSPO2DataDTOList': [{'startGMT','spo2Reading'}, ...]}.

    Intrapulmonary oxygen saturation readings spanning usually the whole day,
    taken at epoch resolution; the widget panel shows the dip during sleep.
    """
    out = _Collector()
    for item in as_list(payload, "wellnessEpochSPO2DataDTOList"):
        if not isinstance(item, dict):
            continue
        ts = _instant(item, "startGMT", "startTimestampGMT", "startTimeGMT")
        if ts is None:
            out.note_missing("spo2:no_timestamp")
            continue
        out.add(sample("garmin_spo2", _first(item, "spo2Reading"), ts), "garmin_spo2")
    _report("spo2", out)
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
# Intraday series: heart rate, stress, body battery, respiration, steps
#
# These come from the wellness endpoints (``get_heart_rates`` et al.), one day
# per request, as ``[[epoch_ms, value], ...]`` arrays or, for steps, a list of
# ``{"startGMT": ..., "steps": n}`` 15-minute buckets. The arrays are already
# event-time; no date attribution is needed. Garmin omits readings it has no
# signal for (``None`` in the second slot) — those become absent datapoints,
# which is the sample model's contract.
# --------------------------------------------------------------------------- #
def _epoch_ms_of(entry: Any) -> datetime | None:
    """Parse ``[[epoch_ms, value], ...]`` array timestamps."""
    if (
        isinstance(entry, (list, tuple))
        and entry
        and isinstance(entry[0], (int, float))
        and entry[0] > 0
    ):
        return datetime.fromtimestamp(entry[0] / 1000, tz=UTC)
    return None


def _array_samples(payload: Any, key: str, name: str, slot: int = 1) -> list[Sample]:
    out = _Collector()
    for entry in as_list(payload, key):
        ts = _epoch_ms_of(entry)
        if ts is None:
            continue
        value = entry[slot] if isinstance(entry, (list, tuple)) and len(entry) > slot else None
        out.add(sample(name, value, ts), name)
    _report(f"intraday:{key}", out)
    return out.samples


def intraday_heart_rate_samples(payload: Any) -> list[Sample]:
    """get_heart_rates -> {'heartRateValues': [[epoch_ms, bpm], ...]}."""
    return _array_samples(payload, "heartRateValues", "garmin_heart_rate")


def intraday_stress_samples(payload: Any) -> list[Sample]:
    """get_stress_data -> {'stressValuesArray': [...], 'bodyBatteryValuesArray': [...]}.

    One endpoint feeds two intraday series: stress levels and body battery.
    Slot layout: stress ``[epoch_ms, level]``, body battery
    ``[epoch_ms, feed_delta, level]`` (slot 2 is what the Garmin UI plots).
    """
    return [
        *_array_samples(payload, "stressValuesArray", "garmin_stress"),
        *_array_samples(payload, "bodyBatteryValuesArray", "garmin_body_battery", slot=2),
    ]


def intraday_respiration_samples(payload: Any) -> list[Sample]:
    """get_respiration_data -> {'respirationValuesArray': [[epoch_ms, brpm], ...]}."""
    return _array_samples(payload, "respirationValuesArray", "garmin_respiration")


def intraday_steps_samples(payload: Any) -> list[Sample]:
    """get_steps_data -> [{'startGMT': ts, 'steps': n}, ...] 15-minute buckets.

    Emits a **cumulative** ramp per calendar day (``garmin_steps_cumulative``):
    the Apple-Watch-style stair curve, and the form where the 15-minute buckets
    say the most -- how today is tracking versus yesterday. Interval counts are
    derivable downstream (``non_negative_derivative``) if anyone wants them, but
    a per-bucket absolute series would make multi-day charts unreadable.
    """
    out = _Collector()
    entries: list[tuple[datetime, float]] = []
    for item in as_list(payload):
        if not isinstance(item, dict):
            continue
        ts = _instant(item, "startGMT", "startTimestampGMT")
        steps = _first(item, "steps")
        if ts is None or steps is None:
            continue
        entries.append((ts, float(steps)))
    entries.sort(key=lambda pair: pair[0])

    by_day: dict[str, float] = {}
    for ts, steps in entries:
        day = ts.date().isoformat()
        by_day[day] = by_day.get(day, 0.0) + steps
        out.add(sample("garmin_steps_cumulative", by_day[day], ts), "garmin_steps_cumulative")
    _report("intraday:steps", out)
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
def sleep_data_samples(payload: Any) -> list[Sample]:
    """get_sleep_data -> sleep segments + SpO2 readings.

    One day per call; only days with a recorded sleep record produce data.
    Stages stitch into a state-timeline panel; SpO2 shows overnight dips.
    """
    return [*sleep_stage_samples(payload), *spo2_samples(payload)]


_HANDLERS = {
    "summaries": summary_samples,
    "sleep": sleep_samples,
    "rhr": rhr_samples,
    "stress": stress_samples,
    "body_battery": body_battery_samples,
    "hrv": hrv_samples,
    "vo2max": vo2max_samples,
    "intraday_hr": intraday_heart_rate_samples,
    "intraday_stress": intraday_stress_samples,
    "intraday_respiration": intraday_respiration_samples,
    "intraday_steps": intraday_steps_samples,
    "sleep_data": sleep_data_samples,
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
