# garmin-exporter

Pulls Garmin Connect data and emits it as metrics. Runs as a CronJob every 30
minutes; also has a backfill mode that writes real history.

```
fetch (garminconnect) -> transform (typed Samples) -> emit (OTLP | VM import)
```

Each stage is a separate module so the transform can be tested against saved
JSON with no network, and the emitters can be swapped without touching I/O.

## Commands

The package installs a `garmin-exporter` entrypoint **inside the virtualenv**, so
it is not on your PATH unless you activate it. Two equivalent ways in:

```bash
# Option A: activate once per shell
source .venv/bin/activate        # from the repo root
cd exporter

# Option B: don't activate, always go through make
make help
```

```bash
# 0. Where am I? Which .env was read, which token store, is auth configured.
garmin-exporter config         # or: make config

# 1. Once, interactively. Handles MFA. Writes tokens to GARMIN_TOKENSTORE.
garmin-exporter login          # or: make login

# 2. Capture real payloads for one day -> verifies the transform field paths.
garmin-exporter dump --date 2026-10-28 --out fixtures   # or: make dump DATE=2026-10-28

# 3. What the CronJob runs. Trailing window, pushed through the collector.
garmin-exporter run            # or: make dry-run

# 4. History. Always writes to VictoriaMetrics (OTLP cannot carry event times).
garmin-exporter backfill --from 2026-04-01 --to 2026-10-01 --chunk-days 30
```

`--dry-run` logs samples instead of emitting, which is how to check the whole
pipeline without a collector running.

Every command except `config` and `login` checks for credentials *before*
touching the network. If they are missing it prints the resolved paths and exits
2 — no traceback, because "no cached tokens" is useless when you are staring at a
correct-looking `.env` that was never read.

## Configuration: `.env` is loaded automatically

`Config.from_env()` reads a dotenv file before looking at the environment, so
`cp .env.example .env` is sufficient — no `source`, no `export`. It looks at
`./.env`, then `../.env`, or at `$GARMIN_ENV_FILE` if set.

**An already-set environment variable always wins.** That ordering is
deliberate: a Kubernetes Secret must beat a stray `.env`, and a CronJob has no
`.env` at all. The file is a local-development convenience.


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

## Field paths are verified, and the shapes are surprising

`transform.py` was originally written from the library's *documented* response
shapes. Every metric it produced was wrong: `totalSteps` does not exist (steps
live at `movement.steps.value`), there is no `trainingEffect` (there are separate
aerobic and anaerobic effects), and the sleep range endpoint wraps everything in
a `values` dict. The mapping is now checked line by line against a real
`garmin-exporter dump` capture.

Three things about the real API are worth knowing before touching this code:

**1. Not every "range" endpoint takes a range.** `get_daily_sleep(cdate,
non_sleep_buffer_minutes=60)` is a *single-day* call whose second parameter is a
buffer in minutes. Passing a date there fails with
`non_sleep_buffer_minutes must be an integer`. The range variant is
`get_sleep_daily(start, end)`, which chunks past Garmin's 28-day sleep limit
itself.

**2. Some endpoints have a hard 28-day server limit.** `get_daily_stress_stats`
and `get_daily_body_battery_stats` return HTTP 400 beyond 28 days and the library
does **not** chunk them. The fetcher therefore windows every source at 28 days
(`fetch.MAX_SPAN_DAYS`) and merges the responses, so callers never have to know.
Asking for a year is 14 calls, not one failure.

**3. Data is sparse in ways that look like bugs.** VO2 max only exists on days
with a qualifying activity. HRV `baseline` is `null` until Garmin has enough
history. A missing datapoint is not an error and is never zero-filled.

`dump` still writes `inventory.json` next to `raw.json` with the actual top-level
keys of every source, so the next API change is a diff rather than a hunt.

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

Two kinds of test live here, and the distinction matters:

- **Transform tests** run against `tests/fixtures/raw-2026-10-28.json`. That
  fixture is *structurally* real — derived from an actual dump, with every key,
  nesting level and null preserved and only the values and identifiers replaced,
  so no health data is committed. An earlier fixture written from the docs passed
  its tests while the real API returned something entirely different; tests that
  validate a guess only validate the guess.
- **Fetch tests** cover the window arithmetic and chunk merging, because both
  failure modes are silent: too wide a window means a source quietly produces
  nothing for the whole range, and a bad merge means duplicate days or an empty
  source.

They assert the contract — metric names, labels, timestamps, one-sample-per-day,
and that a missing field never raises.
