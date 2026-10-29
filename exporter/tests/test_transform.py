"""Transform tests.

These run against a saved payload, never the network. The fixture is synthetic
(shaped from the library's documented responses) until ``garmin-exporter dump``
captures a real one — at which point replacing the fixture re-tests everything
and any field-path drift shows up here rather than in Grafana.

Note what is *not* tested: exact field names. That is intentional. A test that
asserts ``deepSleepSeconds`` is present only tells you the fixture still has it.
The tests assert the contract instead — metric names, labels, timestamps, and
the promise that missing data never raises.
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
    sleep_samples,
    summary_samples,
    transform_all,
)

FIXTURE = Path(__file__).parent / "fixtures" / "raw-2026-10-28.json"


@pytest.fixture()
def payloads() -> RawPayloads:
    data = json.loads(FIXTURE.read_text())
    return RawPayloads(
        start=data["start"],
        end=data["end"],
        sources=data["sources"],
        activities=data["activities"],
    )


def by_name(samples: list[Sample], name: str) -> list[Sample]:
    return [item for item in samples if item.name == name]


# --------------------------------------------------------------------------- #
# Contract
# --------------------------------------------------------------------------- #
def test_every_metric_uses_the_garmin_namespace(payloads: RawPayloads) -> None:
    for item in transform_all(payloads):
        assert item.name.startswith("garmin_"), item.name


def test_daily_metrics_land_on_midnight_utc(payloads: RawPayloads) -> None:
    steps = by_name(transform_all(payloads), "garmin_daily_steps")
    assert len(steps) == 1
    assert steps[0].value == 11234
    assert steps[0].timestamp == datetime(2026, 10, 28, tzinfo=UTC)


def test_sleep_stages_are_separate_series(payloads: RawPayloads) -> None:
    samples = transform_all(payloads)
    expected = {
        "garmin_daily_sleep_duration_seconds": 26400,
        "garmin_daily_deep_sleep_seconds": 5100,
        "garmin_daily_rem_sleep_seconds": 4800,
        "garmin_daily_light_sleep_seconds": 15900,
        "garmin_daily_awake_sleep_seconds": 600,
    }
    for name, value in expected.items():
        found = by_name(samples, name)
        assert len(found) == 1, name
        assert found[0].value == value, name


def test_sleep_score_reads_the_nested_overall_value(payloads: RawPayloads) -> None:
    score = by_name(transform_all(payloads), "garmin_daily_sleep_score")
    assert [item.value for item in score] == [78]


def test_intensity_minutes_are_split_not_summed(payloads: RawPayloads) -> None:
    """Garmin counts vigorous minutes double. Summing here would bake a policy
    into storage; the query should decide."""
    samples = by_name(transform_all(payloads), "garmin_daily_intensity_minutes")
    kinds = {item.labels["kind"]: item.value for item in samples}
    assert kinds == {"moderate": 25, "vigorous": 12}


def test_daily_metric_has_exactly_one_sample_per_day(payloads: RawPayloads) -> None:
    for item in by_name(transform_all(payloads), "garmin_daily_steps"):
        assert len(by_name(transform_all(payloads), item.name)) == 1


def test_vo2max_is_labelled_by_sport(payloads: RawPayloads) -> None:
    vo2 = by_name(transform_all(payloads), "garmin_daily_vo2max")
    by_sport = {item.labels["sport"]: item.value for item in vo2}
    assert by_sport == {"generic": 48.2, "cycling": 44.9}


# --------------------------------------------------------------------------- #
# Activities
# --------------------------------------------------------------------------- #
def test_activity_uses_event_time_not_midnight(payloads: RawPayloads) -> None:
    samples = activity_samples(payloads.activities)
    duration = by_name(samples, "garmin_activity_duration_seconds")
    assert len(duration) == 1
    assert duration[0].timestamp == datetime(2026, 10, 28, 7, 15, 30, tzinfo=UTC)


def test_activity_carries_type_and_id_labels(payloads: RawPayloads) -> None:
    samples = activity_samples(payloads.activities)
    labels = samples[0].labels
    assert labels["activity_type"] == "running"
    assert labels["activity_id"] == "987654321"


def test_all_activity_metrics_share_one_label_set(payloads: RawPayloads) -> None:
    samples = activity_samples(payloads.activities)
    label_sets = {tuple(sorted(item.labels.items())) for item in samples}
    assert len(label_sets) == 1


def test_activity_without_start_time_is_skipped_not_fatal() -> None:
    assert activity_samples([{"activityId": 1, "duration": 100}]) == []


# --------------------------------------------------------------------------- #
# Resilience: Garmin omits data constantly
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("payload", [None, {}, [], {"summaries": []}, {"summaries": [{}]}])
def test_empty_or_junk_payloads_never_raise(payload: object) -> None:
    assert summary_samples(payload) == []


@pytest.mark.parametrize("payload", [None, {}, {"dailySleepDTOList": [{}]}])
def test_sleep_never_raises_on_missing_fields(payload: object) -> None:
    assert sleep_samples(payload) == []


def test_unparseable_date_is_dropped() -> None:
    assert summary_samples({"summaries": [{"calendarDate": "not-a-date", "totalSteps": 5}]}) == []


# --------------------------------------------------------------------------- #
# Dedupe
# --------------------------------------------------------------------------- #
def test_duplicate_daily_value_collapses_to_one() -> None:
    """Summary and RHR endpoints both report resting heart rate. One series."""
    samples = transform_all(
        RawPayloads(
            start="2026-10-28",
            end="2026-10-28",
            sources={
                "summaries": {
                    "summaries": [{"calendarDate": "2026-10-28", "restingHeartRate": 51}]
                },
                "rhr": {"restingHeartRateList": [{"calendarDate": "2026-10-28", "value": 51}]},
            },
        )
    )
    assert len(by_name(samples, "garmin_daily_resting_heart_rate")) == 1


def test_dedupe_keeps_distinct_timestamps_apart() -> None:
    samples = transform_all(
        RawPayloads(
            start="2026-10-28",
            end="2026-10-29",
            sources={
                "summaries": {
                    "summaries": [
                        {"calendarDate": "2026-10-28", "totalSteps": 100},
                        {"calendarDate": "2026-10-29", "totalSteps": 200},
                    ]
                }
            },
        )
    )
    assert len(by_name(samples, "garmin_daily_steps")) == 2


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
        timestamp=datetime(2026, 10, 28, tzinfo=UTC),
        labels={"device": "fenix7"},
    )
    expected_ts = int(datetime(2026, 10, 28, tzinfo=UTC).timestamp() * 1000)
    line = render_prometheus([item])
    assert line.strip() == f'garmin_daily_steps{{device="fenix7"}} 11234 {expected_ts}'


def test_label_values_are_escaped() -> None:
    item = Sample(
        name="garmin_activity_duration_seconds",
        value=1,
        timestamp=datetime(2026, 10, 28, tzinfo=UTC),
        labels={"activity_type": 'we"ird\\name'},
    )
    line = render_prometheus([item])
    assert '\\"ird\\\\name' in line


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
def test_as_list_handles_bare_list_and_nested_wrapper() -> None:
    assert as_list([{"a": 1}]) == [{"a": 1}]
    assert as_list({"summaries": [{"a": 1}]}) == [{"a": 1}]
    assert as_list({"data": {"summaries": [{"a": 1}]}}) == [{"a": 1}]
    assert as_list(None) == []
    assert as_list({"nothing": 3}) == []
