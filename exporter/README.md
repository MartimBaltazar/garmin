# garmin-exporter

Pulls Garmin Connect data and emits it as metrics. Runs as a CronJob every 30
minutes; also has a backfill mode that writes real history.

```
fetch (garminconnect) -> transform (typed Samples) -> emit (OTLP | VM import)
```

Each stage is a separate module so the transform can be tested against saved
JSON with no network, and the emitters can be swapped without touching I/O.

## Commands

```bash
# 1. Once, interactively. Handles MFA. Writes tokens to GARMIN_TOKENSTORE.
garmin-exporter login

# 2. Capture real payloads for one day -> verifies the transform field paths.
garmin-exporter dump --date 2026-10-28 --out fixtures

# 3. What the CronJob runs. Trailing window, pushed through the collector.
garmin-exporter run

# 4. History. Always writes to VictoriaMetrics (OTLP cannot carry event times).
garmin-exporter backfill --from 2026-04-01 --to 2026-10-01 --chunk-days 30
```

`--dry-run` logs samples instead of emitting, which is how to check the whole
pipeline without a collector running.

## Why two emit paths

The OTel Python SDK stamps metrics at **observation** time. That is fine for the
live path but makes history impossible: backfilling yesterday would write
yesterday's steps with today's timestamp. So the backfill path POSTs Prometheus
exposition with an explicit timestamp to `/api/v1/import/prometheus`, which is
also why re-running a backfill is idempotent — the timestamp is part of the
series identity.

Both paths consume the same `Sample` list and therefore produce identical
series names and labels. They must; otherwise live and historical data form two
separate series.

## Field paths are unverified

`transform.py` maps Garmin's JSON to metrics using field names taken from the
library's documented response shapes — not from a live capture. Garmin omits
fields habitually and changes them between API versions.

So: **run `dump` first**, then fix any wrong paths in one place. The dump writes
`inventory.json` next to `raw.json`, which lists the actual top-level keys of
every source, so the correction is a diff rather than a hunt.

## Self-observability

Every run emits its own metrics, including on failure:

| Metric | Why |
|---|---|
| `garmin_exporter_last_success_timestamp_seconds` | Drives the "no success in 6h" alert |
| `garmin_exporter_run_success` | 0 on any failure, so the alert sees failures that never wrote data |
| `garmin_exporter_run_duration_seconds` | Catch a slow/stuck run before it trips the job deadline |
| `garmin_exporter_api_errors_total{kind}` | Distinguish auth failures from rate limits from network |
| `garmin_exporter_samples_emitted` | Sanity check: a successful run that emitted 0 samples is a bug |

`last_success` is persisted to a small state file on the token volume, because
CronJob pods are otherwise stateless.

## Tests

```bash
python -m pytest
```

Tests assert the *contract* — metric names, labels, timestamps, one-sample-per-day,
and that missing data never raises. They deliberately do not assert that a
particular Garmin field is present, because that only tests the fixture.
