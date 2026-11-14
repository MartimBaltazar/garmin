"""Typed sample model shared by every transform and emitter."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime


@dataclass(frozen=True, slots=True)
class Sample:
    """A single metric observation.

    ``timestamp`` is the **event time**, never the collection time. Both emit
    paths write it verbatim: the OTLP path sets ``time_unix_nano`` from it (see
    ``otlp``), and the VM import path renders it as the exposition timestamp.
    """

    name: str
    value: float
    timestamp: datetime
    labels: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None:
            raise ValueError(f"{self.name}: timestamp must be timezone-aware")
        if not self.name or not self.name.replace("_", "").isalnum():
            raise ValueError(f"invalid metric name: {self.name!r}")

    @property
    def timestamp_ms(self) -> int:
        return int(self.timestamp.timestamp() * 1000)


def midnight_utc(day: str) -> datetime:
    """Daily Garmin metrics are attributed to a calendar date, not an instant.

    We stamp them at 00:00 UTC of that date. Garmin reports dates in the user's
    local timezone, so a non-UTC user shifts each point by at most one offset —
    irrelevant for daily-grain panels, and worth knowing before writing
    ``date_trunc`` queries.
    """
    return datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC)


def sample(name: str, value: object, ts: datetime, **labels: str) -> Sample | None:
    """Build a Sample, coercing and dropping unusable values.

    Garmin omits fields habitually (no sleep record, no VO2 max for a day). A
    missing value is not an error; it is an absent datapoint.
    """
    if value is None:
        return None
    # bool is a subclass of int, so this also covers True/False — deliberate,
    # since Garmin returns booleans for some flag-like fields.
    if isinstance(value, (int, float)):
        numeric = float(value)
    else:
        return None
    # Python's json module accepts NaN/Infinity. Neither is a measurement, and
    # both break the Prometheus renderer's int() formatting.
    if not math.isfinite(numeric):
        return None
    return Sample(name=name, value=numeric, timestamp=ts, labels=dict(labels))
