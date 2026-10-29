"""Transform tests.

These run against a saved payload, never the network.

**The fixture is structurally real.** It was derived from an actual
``garmin-exporter dump`` capture: every key, every nesting level and every null is
what Garmin really returns. Only the values and the identifiers were replaced, so
no health data is committed.

That distinction matters. An earlier version of this file asserted against a
fixture I had written from the library's *documented* response shapes, and it
passed while the real API returned something completely different
(``totalSteps`` does not exist; steps are at ``movement.steps.value``). Tests that
validate a guess only validate the guess.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from garmin_exporter.emit import _fmt_value, render_prometheus
from garmin_exporter.fetch import RawPayloads, as_list
from garmin_exporter.models import Sample, midnight_utc, sample
from garmin_exporter.transform import (
    activity_samples,
    body_battery_samples,
    hrv_samples,
    rhr_samples,
    sleep_samples,
    stress_samples,
    summary_samples,
    transform_all,
    vo2max_samples,
)

FIXTURE = Path(__file__).parent / "fixtures" / "raw-2026-10-28.json"
DAY = datetime(2026, 10, 28, tzinfo=UTC)


@pytest.fixture()
def payloads() -> RawPayloads:
    data = json.loads(FIXTURE.read_text())
    return RawPayloads(
        start=data["start"],
        end=data["end"],
        sources=data["sources"],
        activities=data["activities"],
    )


@pytest.fixture()
def samples(payloads: RawPayloads) -> list[Sample]:
    return transform_all(payloads)


def by_name(items: list[Sample], name: str) -> list[Sample]:
    return [item for item in items if item.name == name]


def value_of(items: list[Sample], name: str) -> float:
    found = by_name(items, name)
    assert len(found) == 1, f"expected exactly one {name}, got {len(found)}"
    return found[0].value


# --------------------------------------------------------------------------- #
# Contract
# --------------------------------------------------------------------------- #
def test_every_metric_uses_the_garmin_namespace(samples: list[Sample]) -> None:
    for item in samples:
        assert item.name.startswith("garmin_"), item.name


def test_daily_metrics_land_on_midnight_utc(samples: list[Sample]) -> None:
    for item in samples:
        if item.name.startswith("garmin_daily_"):
            assert item.timestamp == DAY, item.name


def test_activity_metrics_keep_their_real_event_time(samples: list[Sample]) -> None:
    """Activities are instants, not calendar days — they must not be midnighted."""
    item = by_name(samples, "garmin_activity_duration_seconds")[0]
    assert item.timestamp == datetime(2026, 10, 28, 7, 15, 30, tzinfo=UTC)


def test_no_daily_metric_produces_a_duplicate_sample(samples: list[Sample]) -> None:
    """One sample per calendar date is the whole point of the prefix."""
    seen: set[tuple[str, tuple[tuple[str, str], ...]]] = set()
    for item in samples:
        if not item.name.startswith("garmin_daily_"):
            continue
        key = (item.name, tuple(sorted(item.labels.items())))
        assert key not in seen, f"duplicate series {item.name} {item.labels}"
        seen.add(key)


# --------------------------------------------------------------------------- #
# Summaries — the nesting is the hard part: nothing lives at the top level
# --------------------------------------------------------------------------- #
def test_steps_come_from_the_nested_movement_block(samples: list[Sample]) -> None:
    assert value_of(samples, "garmin_daily_steps") == 11234
    assert value_of(samples, "garmin_daily_steps_goal") == 10000
    assert value_of(samples, "garmin_daily_distance_meters") == 8500


def test_calories_are_broken_out_not_just_total(samples: list[Sample]) -> None:
    assert value_of(samples, "garmin_daily_calories") == 2500
    assert value_of(samples, "garmin_daily_calories_active") == 500
    assert value_of(samples, "garmin_daily_calories_resting") == 2000


def test_resting_heart_rate_appears_once_despite_two_sources(
    samples: list[Sample],
) -> None:
    """The summary block and the RHR endpoint both report it; one series must win."""
    assert value_of(samples, "garmin_daily_resting_heart_rate") == 52


def test_stress_avg_appears_once_despite_two_sources(samples: list[Sample]) -> None:
    assert value_of(samples, "garmin_daily_stress_avg") == 30


def test_body_battery_bounds_appear_once_despite_two_sources(samples: list[Sample]) -> None:
    assert value_of(samples, "garmin_daily_body_battery_min") == 25
    assert value_of(samples, "garmin_daily_body_battery_max") == 95


def test_intensity_minutes_are_split_not_summed(samples: list[Sample]) -> None:
    """Garmin counts vigorous minutes double. Summing here would bake a policy
    into storage; the query should decide."""
    kinds = {
        item.labels["kind"]: item.value
        for item in by_name(samples, "garmin_daily_intensity_minutes")
    }
    assert kinds == {"moderate": 20, "vigorous": 15}
    assert value_of(samples, "garmin_daily_intensity_goal_minutes") == 150


def test_stress_durations_are_labelled_by_level(samples: list[Sample]) -> None:
    by_level = {
        item.labels["level"]: item.value
        for item in by_name(samples, "garmin_daily_stress_duration_seconds")
    }
    assert by_level == {"rest": 50000, "low": 6000, "medium": 2400, "high": 3000}


# --------------------------------------------------------------------------- #
# Sleep — the range endpoint nests everything under "values"
# --------------------------------------------------------------------------- #
def test_sleep_stages_are_separate_series(samples: list[Sample]) -> None:
    expected = {
        "garmin_daily_sleep_duration_seconds": 26400,
        "garmin_daily_deep_sleep_seconds": 5100,
        "garmin_daily_rem_sleep_seconds": 4800,
        "garmin_daily_light_sleep_seconds": 15900,
        "garmin_daily_awake_sleep_seconds": 600,
    }
    for name, value in expected.items():
        assert value_of(samples, name) == value, name


def test_sleep_score_and_respiration(samples: list[Sample]) -> None:
    assert value_of(samples, "garmin_daily_sleep_score") == 78
    assert value_of(samples, "garmin_daily_sleep_respiration_avg") == pytest.approx(14.1)
    assert value_of(samples, "garmin_daily_sleep_need_minutes") == 460


def test_sleep_tolerates_an_unwrapped_payload() -> None:
    """If the library ever hoists the values out of "values", keep working."""
    flat = [{"calendarDate": "2026-10-28", "totalSleepTimeInSeconds": 1000}]
    assert sleep_samples(flat)[0].value == 1000


# --------------------------------------------------------------------------- #
# HRV and VO2 max — both sparse by nature
# --------------------------------------------------------------------------- #
def test_hrv_reads_the_summary_wrapper(samples: list[Sample]) -> None:
    assert value_of(samples, "garmin_daily_hrv_last_night_ms") == 48
    assert value_of(samples, "garmin_daily_hrv_weekly_avg_ms") == 46
    assert value_of(samples, "garmin_daily_hrv_baseline_ms") == 45


def test_missing_hrv_baseline_is_dropped_not_zeroed() -> None:
    """Garmin returns baseline: null until enough history exists."""
    payload = {
        "hrvSummaries": [{"calendarDate": "2026-10-28", "lastNightAvg": 48, "baseline": None}]
    }
    names = [item.name for item in hrv_samples(payload)]
    assert names == ["garmin_daily_hrv_last_night_ms"]


def test_vo2max_is_labelled_by_sport(samples: list[Sample]) -> None:
    by_sport = {
        item.labels["sport"]: item.value for item in by_name(samples, "garmin_daily_vo2max")
    }
    assert by_sport == {"generic": 48.2, "cycling": 44.9}


def test_vo2max_date_lives_inside_the_sport_block() -> None:
    """The wrapper has no calendarDate; a null cycling block must not raise."""
    payload = [
        {
            "userId": 1,
            "generic": {"calendarDate": "2026-10-25", "vo2MaxPreciseValue": 48.2},
            "cycling": None,
        }
    ]
    items = vo2max_samples(payload)
    assert [(item.value, item.labels["sport"]) for item in items] == [(48.2, "generic")]
    assert items[0].timestamp == datetime(2026, 10, 25, tzinfo=UTC)


def test_vo2max_without_a_calendar_date_is_skipped() -> None:
    assert vo2max_samples([{"generic": {"vo2MaxPreciseValue": 48.2}}]) == []


# --------------------------------------------------------------------------- #
# Activities
# --------------------------------------------------------------------------- #
def test_activity_carries_type_and_id_labels(samples: list[Sample]) -> None:
    labels = by_name(samples, "garmin_activity_duration_seconds")[0].labels
    assert labels["activity_type"] == "running"
    assert labels["activity_id"] == "987654321"


def test_all_activity_metrics_share_one_label_set(samples: list[Sample]) -> None:
    label_sets = {
        tuple(sorted(item.labels.items()))
        for item in samples
        if item.name.startswith("garmin_activity_")
    }
    assert len(label_sets) == 1


def test_activity_has_both_training_effects() -> None:
    """The real payload has no ``trainingEffect``: it has aerobic *and* anaerobic."""
    payload = [
        {
            "activityId": 1,
            "startTimeGMT": "2026-10-28 07:15:30",
            "aerobicTrainingEffect": 3.4,
            "anaerobicTrainingEffect": 1.2,
        }
    ]
    names = {item.name for item in activity_samples(payload)}
    assert "garmin_activity_aerobic_training_effect" in names
    assert "garmin_activity_anaerobic_training_effect" in names


def test_activity_accepts_a_millisecond_begin_timestamp() -> None:
    """``beginTimestamp`` is a ms epoch for the same instant as ``startTimeGMT``."""
    payload = [{"activityId": 1, "beginTimestamp": 1793171730000, "duration": 60}]
    assert activity_samples(payload)[0].timestamp == datetime(2026, 10, 28, 7, 15, 30, tzinfo=UTC)


def test_activity_without_id_is_skipped() -> None:
    assert activity_samples([{"startTimeGMT": "2026-10-28 07:15:30", "duration": 1}]) == []


def test_activity_without_start_time_is_skipped_not_fatal() -> None:
    assert activity_samples([{"activityId": 1, "duration": 100}]) == []


def test_non_dict_activities_are_ignored() -> None:
    assert activity_samples(["nonsense", 42, None]) == []


# --------------------------------------------------------------------------- #
# Resilience: Garmin omits data constantly
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("payload", [None, {}, [], {"data": []}, {"data": [{}]}])
def test_summaries_never_raise(payload: object) -> None:
    assert summary_samples(payload) == []


@pytest.mark.parametrize("payload", [None, {}, [], {"individualStats": [{}]}])
def test_sleep_never_raise(payload: object) -> None:
    assert sleep_samples(payload) == []


@pytest.mark.parametrize("payload", [None, {}, [], [{}]])
def test_rhr_stress_body_battery_never_raise(payload: object) -> None:
    assert rhr_samples(payload) == []
    assert stress_samples(payload) == []
    assert body_battery_samples(payload) == []


def test_unparseable_date_is_dropped() -> None:
    assert (
        summary_samples(
            {"data": [{"calendarDate": "not-a-date", "movement": {"steps": {"value": 5}}}]}
        )
        == []
    )


# --------------------------------------------------------------------------- #
# Dedupe
# --------------------------------------------------------------------------- #
def test_dedupe_keeps_distinct_timestamps_apart() -> None:
    payload = {
        "data": [
            {"calendarDate": "2026-10-28", "movement": {"steps": {"value": 100}}},
            {"calendarDate": "2026-10-29", "movement": {"steps": {"value": 200}}},
        ]
    }
    steps = by_name(summary_samples(payload), "garmin_daily_steps")
    assert len(steps) == 2
    assert {item.timestamp.day for item in steps} == {28, 29}


def test_dedupe_prefers_the_last_source() -> None:
    """Order in _HANDLERS decides precedence, so it must be explicit and tested.

    RHR is fetched after summaries, therefore wins on an identical series. Without
    this, the same day's resting heart rate could flip between runs.
    """
    samples = transform_all(
        RawPayloads(
            start="2026-10-28",
            end="2026-10-28",
            sources={
                "summaries": {
                    "data": [{"calendarDate": "2026-10-28", "heartRate": {"restingValue": 52}}]
                },
                "rhr": [{"calendarDate": "2026-10-28", "value": 99}],
            },
        )
    )
    resting = [item for item in samples if item.name == "garmin_daily_resting_heart_rate"]
    assert len(resting) == 1
    assert resting[0].value == 99


# --------------------------------------------------------------------------- #
# Models and rendering
# --------------------------------------------------------------------------- #
def test_sample_rejects_naive_timestamps() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        Sample(name="garmin_daily_steps", value=1, timestamp=datetime(2026, 10, 28))


def test_sample_rejects_invalid_names() -> None:
    with pytest.raises(ValueError, match="invalid metric name"):
        Sample(name="garmin.daily.steps", value=1, timestamp=datetime.now(UTC))


def test_sample_helper_drops_missing_and_non_numeric() -> None:
    ts = datetime.now(UTC)
    assert sample("garmin_daily_steps", None, ts) is None
    assert sample("garmin_daily_steps", "12", ts) is None
    assert sample("garmin_daily_steps", 12, ts) is not None


def test_prometheus_line_has_explicit_millisecond_timestamp() -> None:
    item = Sample(
        name="garmin_daily_steps",
        value=11234,
        timestamp=DAY,
        labels={"device": "fenix7"},
    )
    expected_ts = int(DAY.timestamp() * 1000)
    line = render_prometheus([item])
    assert line.strip() == f'garmin_daily_steps{{device="fenix7"}} 11234 {expected_ts}'


def test_label_values_are_escaped() -> None:
    item = Sample(
        name="garmin_activity_duration_seconds",
        value=1,
        timestamp=DAY,
        labels={"activity_type": 'we"ird\\name'},
    )
    assert '\\"ird\\\\name' in render_prometheus([item])


def test_render_empty_is_empty_not_a_stray_newline() -> None:
    assert render_prometheus([]) == ""


@pytest.mark.parametrize(
    ("value", "expected"),
    [(11234.0, "11234"), (48.2, "48.2"), (0.0, "0"), (-12.5, "-12.5")],
)
def test_float_values_render_without_exponent(value: float, expected: str) -> None:
    assert _fmt_value(value) == expected


def test_midnight_utc_is_aware() -> None:
    assert midnight_utc("2026-10-28").tzinfo is UTC


# --------------------------------------------------------------------------- #
# Payload unwrapping
# --------------------------------------------------------------------------- #
def test_as_list_handles_every_real_wrapper_shape() -> None:
    assert as_list([{"a": 1}]) == [{"a": 1}]  # bare list
    assert as_list({"data": [{"a": 1}]}, "data") == [{"a": 1}]  # summaries
    assert as_list({"hrvSummaries": [{"a": 1}]}) == [{"a": 1}]  # hrv
    assert as_list({"data": {"summaries": [{"a": 1}]}}) == [{"a": 1}]
    assert as_list(None) == []
    assert as_list({"nothing": 3}) == []
