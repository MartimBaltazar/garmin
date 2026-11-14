"""Run-outcome and retry-policy tests.

These pin down the failure modes that are silent in production:

* a run in which every Garmin call failed must not count as a success, or it
  pushes ``last_success`` forward and silences the staleness alert;
* a session Garmin rejects mid-run (tokens load, then 401) must surface as an
  auth failure instead of twelve "source failed" warnings;
* errors that cannot succeed on retry (400, 401, a bad argument) must not be
  retried, because retries cost backoff time against the Job deadline.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from garmin_exporter import runner
from garmin_exporter.auth import AuthError
from garmin_exporter.config import Config
from garmin_exporter.fetch import (
    INTRADAY_SOURCES,
    SOURCES,
    Fetcher,
    RawPayloads,
    http_status,
    is_auth_failure,
    is_retryable,
)
from garmin_exporter.models import Sample, sample


class _Response:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class HTTPError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
        self.response = _Response(status_code)


class GarminConnectTooManyRequestsError(Exception):
    """Same name as the library's class; matched by name on purpose."""


class GarminConnectAuthenticationError(Exception):
    pass


def _cfg(tmp_path: Path, **overrides: Any) -> Config:
    base: dict[str, Any] = {
        "email": None,
        "password": None,
        "tokenstore": tmp_path,
        "emit": "none",
        "otlp_endpoint": "http://localhost:4317",
        "otlp_insecure": True,
        "vm_url": None,
        "lookback_days": 1,
        "request_pause_seconds": 0.0,
        "log_level": "INFO",
    }
    base.update(overrides)
    return Config(**base)


class FakeClient:
    """Every Garmin method the fetcher calls, each answering via ``behaviour``."""

    def __init__(self, behaviour: Any) -> None:
        self.calls: list[str] = []
        self._behaviour = behaviour

    def __getattr__(self, name: str) -> Any:
        if not name.startswith("get_"):
            raise AttributeError(name)

        def method(*args: Any) -> Any:
            self.calls.append(name)
            return self._behaviour(name, *args)

        return method


# --------------------------------------------------------------------------- #
# Retry classification
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_transient_http_errors_are_retried(status: int) -> None:
    assert is_retryable(HTTPError(status))


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_client_errors_are_not_retried(status: int) -> None:
    assert not is_retryable(HTTPError(status))


def test_status_is_found_through_the_exception_chain() -> None:
    """garminconnect wraps the transport error; the status lives on the cause."""
    try:
        try:
            raise HTTPError(401)
        except HTTPError as inner:
            raise RuntimeError("wrapped") from inner
    except RuntimeError as outer:
        assert http_status(outer) == 401
        assert is_auth_failure(outer)
        assert not is_retryable(outer)


def test_library_rate_limit_error_is_retried() -> None:
    assert is_retryable(GarminConnectTooManyRequestsError("slow down"))


def test_library_auth_error_is_an_auth_failure() -> None:
    assert is_auth_failure(GarminConnectAuthenticationError("nope"))
    assert not is_retryable(GarminConnectAuthenticationError("nope"))


def test_programming_errors_are_not_retried() -> None:
    assert not is_retryable(TypeError("bad argument"))


def test_unknown_transport_errors_are_retried() -> None:
    assert is_retryable(ConnectionResetError("reset by peer"))


def test_non_retryable_errors_are_called_once(tmp_path: Path) -> None:
    def behaviour(name: str, *args: Any) -> Any:
        raise HTTPError(400)

    client = FakeClient(behaviour)
    payloads = Fetcher(_cfg(tmp_path), client=client).fetch_range("2026-10-28", "2026-10-28")
    # One call per source plus activities: nothing was retried.
    assert len(client.calls) == len(SOURCES) + len(INTRADAY_SOURCES) + 1
    assert payloads.all_sources_failed()


# --------------------------------------------------------------------------- #
# Auth rejected mid-run
# --------------------------------------------------------------------------- #
def test_a_rejected_session_stops_the_fetch_with_auth_error(tmp_path: Path) -> None:
    def behaviour(name: str, *args: Any) -> Any:
        raise HTTPError(401)

    client = FakeClient(behaviour)
    with pytest.raises(AuthError, match="rejected the session"):
        Fetcher(_cfg(tmp_path), client=client).fetch_range("2026-10-28", "2026-10-28")
    assert len(client.calls) == 1, "must stop at the first 401, not try every source"


# --------------------------------------------------------------------------- #
# Run outcome
# --------------------------------------------------------------------------- #
def _sample() -> Sample:
    result = sample("garmin_daily_steps", 1, datetime(2026, 10, 28, tzinfo=UTC))
    assert result is not None
    return result


def test_all_sources_failing_is_a_failed_run() -> None:
    payloads = RawPayloads(start="a", end="b", attempted_sources=3)
    payloads.failed_sources.update({"x", "y", "z"})
    payloads.errors["x:error"] += 1
    with pytest.raises(runner.RunFailed):
        runner.check_outcome(payloads, [_sample()])


def test_no_samples_with_errors_is_a_failed_run() -> None:
    payloads = RawPayloads(start="a", end="b", attempted_sources=3)
    payloads.failed_sources.add("x")
    payloads.errors["x:error"] += 1
    with pytest.raises(runner.RunFailed):
        runner.check_outcome(payloads, [])


def test_partial_failure_with_data_is_a_success() -> None:
    payloads = RawPayloads(start="a", end="b", attempted_sources=3)
    payloads.failed_sources.add("x")
    payloads.errors["x:error"] += 1
    runner.check_outcome(payloads, [_sample()])


def test_no_data_and_no_errors_is_a_success() -> None:
    """Garmin legitimately has nothing yet (e.g. just after midnight)."""
    runner.check_outcome(RawPayloads(start="a", end="b", attempted_sources=3), [])


class RecordingEmitter:
    def __init__(self) -> None:
        self.samples: list[Sample] = []

    def emit(self, samples: Any) -> None:
        self.samples.extend(samples)

    def close(self) -> None:
        pass


def test_a_run_where_everything_failed_does_not_advance_last_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(tmp_path)
    cfg.state_file().write_text(json.dumps({"last_success": 1_000}))

    def behaviour(name: str, *args: Any) -> Any:
        raise HTTPError(500)

    emitter = RecordingEmitter()
    monkeypatch.setattr(runner, "build_emitter", lambda _cfg: emitter)
    monkeypatch.setattr(runner, "Fetcher", lambda c: Fetcher(c, client=FakeClient(behaviour)))
    # Retries back off for real; make them instant.
    monkeypatch.setattr("time.sleep", lambda _s: None)

    assert runner.run_live(cfg) == 1
    assert json.loads(cfg.state_file().read_text())["last_success"] == 1_000

    by_name = {s.name: s for s in emitter.samples}
    assert by_name[runner.METRIC_RUN_SUCCESS].value == 0
    assert by_name[runner.METRIC_SOURCES_FAILED].value == len(SOURCES) + len(INTRADAY_SOURCES)
    assert by_name[runner.METRIC_LAST_SUCCESS].value == 1_000


def test_sources_failed_is_emitted_as_zero_on_a_clean_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dense series: alerts must see 0, not "no data", when all is well."""
    emitter = RecordingEmitter()
    runner._emit_self(
        emitter,
        success=True,
        duration=1.0,
        samples=10,
        cfg=_cfg(tmp_path),
        payloads=RawPayloads(start="a", end="b", attempted_sources=3),
    )
    by_name = {s.name: s for s in emitter.samples}
    assert by_name[runner.METRIC_SOURCES_FAILED].value == 0


# --------------------------------------------------------------------------- #
# Backfill
# --------------------------------------------------------------------------- #
def test_backfill_chunks_match_garmins_widest_window() -> None:
    from garmin_exporter.fetch import MAX_SPAN_DAYS

    assert runner.DEFAULT_BACKFILL_CHUNK_DAYS == MAX_SPAN_DAYS


def test_backfill_failure_logs_where_to_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    calls = {"n": 0}

    class FlakyEmitter(RecordingEmitter):
        def emit(self, samples: Any) -> None:
            calls["n"] += 1
            if calls["n"] == 2:
                raise ConnectionError("collector went away")

    def behaviour(name: str, *args: Any) -> Any:
        if name == "get_daily_summaries":
            return {"data": [{"calendarDate": args[0], "movement": {"steps": {"value": 5}}}]}
        return None

    monkeypatch.setattr(runner, "build_emitter", lambda _cfg: FlakyEmitter())
    monkeypatch.setattr(runner, "Fetcher", lambda c: Fetcher(c, client=FakeClient(behaviour)))
    assert runner.run_backfill(_cfg(tmp_path), "2026-10-01", "2026-10-10", chunk_days=5) == 1
    record = next(r for r in caplog.records if r.getMessage() == "backfill failed")
    assert record.resume_from == "2026-10-06"  # type: ignore[attr-defined]


def test_backfill_rejects_a_reversed_range(tmp_path: Path) -> None:
    assert runner.run_backfill(_cfg(tmp_path), "2026-10-10", "2026-10-01") == 2


# --------------------------------------------------------------------------- #
# Sample hygiene
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_non_finite_values_are_dropped(value: float) -> None:
    assert sample("garmin_daily_steps", value, datetime(2026, 10, 28, tzinfo=UTC)) is None
