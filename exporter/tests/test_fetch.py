"""Fetch-layer tests.

No network. What is tested here is the arithmetic that decides *how many* Garmin
calls happen and how their responses are stitched back together, because both
failure modes are silent:

* too wide a window -> HTTP 400 on stress/body-battery, and a source that quietly
  produces nothing for the whole range;
* a bad merge -> duplicate days, or a source that looks empty because the second
  chunk overwrote the first.
"""

from __future__ import annotations

import pytest

from garmin_exporter.fetch import (
    MAX_SPAN_DAYS,
    SOURCES,
    RawPayloads,
    iter_windows,
    merge_chunks,
)


# --------------------------------------------------------------------------- #
# Windowing
# --------------------------------------------------------------------------- #
def test_single_day_is_one_window() -> None:
    assert list(iter_windows("2026-10-28", "2026-10-28", 28)) == [("2026-10-28", "2026-10-28")]


def test_window_at_the_limit_is_not_split() -> None:
    """Garmin allows a 28-day window, so exactly 28 days must be one call."""
    windows = list(iter_windows("2026-10-01", "2026-10-28", 28))
    assert windows == [("2026-10-01", "2026-10-28")]


def test_window_one_day_over_the_limit_splits() -> None:
    windows = list(iter_windows("2026-10-01", "2026-10-29", 28))
    assert len(windows) == 2
    assert windows[0] == ("2026-10-01", "2026-10-28")
    assert windows[1] == ("2026-10-29", "2026-10-29")


def test_windows_are_contiguous_and_cover_the_range_exactly() -> None:
    windows = list(iter_windows("2026-01-01", "2026-12-31", 28))
    assert windows[0][0] == "2026-01-01"
    assert windows[-1][1] == "2026-12-31"
    for (_, previous_end), (next_start, _) in zip(windows, windows[1:], strict=False):
        assert previous_end < next_start
    # No gaps: each window starts the day after the previous one ends.
    from datetime import date, timedelta

    for (_, previous_end), (next_start, _) in zip(windows, windows[1:], strict=False):
        assert date.fromisoformat(next_start) == date.fromisoformat(previous_end) + timedelta(
            days=1
        )


def test_leap_year_range_is_handled() -> None:
    windows = list(iter_windows("2028-02-01", "2028-03-01", 28))
    assert windows[0] == ("2028-02-01", "2028-02-28")
    assert windows[-1][1] == "2028-03-01"


@pytest.mark.parametrize("span", [0, -1])
def test_invalid_span_is_rejected(span: int) -> None:
    with pytest.raises(ValueError, match="span_days"):
        list(iter_windows("2026-10-01", "2026-10-28", span))


def test_reversed_range_is_rejected() -> None:
    with pytest.raises(ValueError, match="after"):
        list(iter_windows("2026-10-28", "2026-10-01", 28))


# --------------------------------------------------------------------------- #
# Merging
# --------------------------------------------------------------------------- #
def test_merge_concatenates_plain_lists() -> None:
    assert merge_chunks([[1, 2], [3]]) == [1, 2, 3]


def test_merge_concatenates_every_list_key_of_a_dict() -> None:
    merged = merge_chunks(
        [
            {"hrvSummaries": [{"d": 1}], "userProfilePk": 7},
            {"hrvSummaries": [{"d": 2}], "userProfilePk": 7},
        ]
    )
    assert merged == {"hrvSummaries": [{"d": 1}, {"d": 2}], "userProfilePk": 7}


def test_merge_handles_the_summaries_wrapper() -> None:
    """get_daily_summaries returns {"data": [...]}, not a bare list."""
    merged = merge_chunks([{"data": [1]}, {"data": [2, 3]}])
    assert merged == {"data": [1, 2, 3]}


def test_merge_ignores_none_chunks() -> None:
    assert merge_chunks([None, [1], None]) == [1]
    assert merge_chunks([None, None]) is None


def test_merge_of_a_single_chunk_is_passed_through_untouched() -> None:
    chunk = {"data": [1]}
    assert merge_chunks([chunk]) is chunk


def test_merge_of_mixed_shapes_does_not_invent_a_result() -> None:
    """A source that changes shape mid-range is a bug, not something to guess at."""
    assert merge_chunks([[1], {"data": [2]}]) == [1]


# --------------------------------------------------------------------------- #
# Source table
# --------------------------------------------------------------------------- #
def test_no_source_asks_for_a_window_garmin_will_reject() -> None:
    for source in SOURCES:
        assert 1 <= source.span_days <= MAX_SPAN_DAYS, source.key


def test_sleep_uses_the_range_method_not_the_single_day_one() -> None:
    """get_daily_sleep(cdate, non_sleep_buffer_minutes) is NOT a range call.

    Passing a date as its second argument raises
    ``non_sleep_buffer_minutes must be an integer``.
    """
    sleep = next(source for source in SOURCES if source.key == "sleep")
    assert sleep.method == "get_sleep_daily"


def test_every_source_key_is_unique() -> None:
    keys = [source.key for source in SOURCES]
    assert len(keys) == len(set(keys))


# --------------------------------------------------------------------------- #
# RawPayloads
# --------------------------------------------------------------------------- #
def test_raw_payloads_serialise_counters_as_plain_dicts() -> None:
    from collections import Counter

    payloads = RawPayloads(start="2026-10-28", end="2026-10-29", errors=Counter({"sleep:error": 2}))
    assert payloads.to_dict()["errors"] == {"sleep:error": 2}
