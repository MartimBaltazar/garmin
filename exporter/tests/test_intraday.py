"""Transform mappings for the intraday wellness endpoints.

Every test here mimics the raw shapes those endpoints return — acts of truth
template, since each hand-mimicked endpoint shape encodes an assumed field
path. If the shape changes upstream, one of these tests fails, and the failing
test's payload literal documents what Garmin used to return.
"""

from __future__ import annotations

from datetime import UTC, datetime

from garmin_exporter.fetch import RawPayloads
from garmin_exporter.transform import (
    intraday_heart_rate_samples,
    intraday_respiration_samples,
    intraday_steps_samples,
    intraday_stress_samples,
    transform_all,
)


def test_intraday_heart_rate_maps_minute_series() -> None:
    payload = {
        "heartRateValues": [
            [1769817600000, 58],
            [1769817660000, 61],
            [1769817720000, None],  # Garmin emits None when there is no reading
        ]
    }
    items = intraday_heart_rate_samples(payload)
    assert len(items) == 2
    assert [i.value for i in items] == [58.0, 61.0]
    assert items[0].timestamp == datetime(2026, 1, 31, 0, 0, tzinfo=UTC)


def test_intraday_stress_and_body_battery_share_one_endpoint() -> None:
    payload = {
        "stressValuesArray": [[1769817600000, 12], [1769817960000, None]],
        "bodyBatteryValuesArray": [[1769817600000, 3, 84], [1769817960000, 1, 85]],
    }
    out = intraday_stress_samples(payload)
    stress = [i for i in out if i.name == "garmin_stress"]
    battery = [i for i in out if i.name == "garmin_body_battery"]
    assert [i.value for i in stress] == [12.0]
    # Body battery takes the level slot (2), not the feed-in delta (1)
    assert [i.value for i in battery] == [84.0, 85.0]


def test_intraday_respiration() -> None:
    out = intraday_respiration_samples(
        {"respirationValuesArray": [[1769817600000, 14.2], [1769817660000, None]]}
    )
    assert [i.value for i in out] == [14.2]


def test_intraday_steps_accumulate_within_one_day() -> None:
    payload = [
        {"startGMT": "2026-10-31 08:15:00", "steps": 200},
        {"startGMT": "2026-10-31 08:00:00", "steps": 100},
        {"startGMT": "2026-10-31 09:00:00", "steps": 50},
        {"startGMT": "2026-11-01 00:00:00", "steps": 10},
    ]
    out = intraday_steps_samples(payload)
    assert [(i.value, i.timestamp) for i in out] == [
        (100.0, datetime(2026, 10, 31, 8, 0, tzinfo=UTC)),
        (300.0, datetime(2026, 10, 31, 8, 15, tzinfo=UTC)),
        (350.0, datetime(2026, 10, 31, 9, 0, tzinfo=UTC)),
        (10.0, datetime(2026, 11, 1, 0, 0, tzinfo=UTC)),
    ]


def test_transform_all_honours_intraday_sources() -> None:
    payloads = RawPayloads(
        start="2026-10-31",
        end="2026-10-31",
        sources={"intraday_hr": {"heartRateValues": [[1793433600000, 55]]}},
    )
    samples = transform_all(payloads)
    assert any(s.name == "garmin_heart_rate" and s.value == 55.0 for s in samples)


def test_intraday_steps_handles_iso_t_timestamps() -> None:
    """Live startGMT is 'YYYY-MM-DDTHH:MM:SS.0'; the space form appeared only
    in the samples dump."""
    payload = [{"startGMT": "2026-10-30T08:00:00.0", "steps": 120}]
    out = intraday_steps_samples(payload)
    assert out[0].value == 120.0
    assert out[0].timestamp == datetime(2026, 10, 30, 8, 0, tzinfo=UTC)
