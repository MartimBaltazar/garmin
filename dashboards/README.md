# Dashboards: design notes and metric contract

Reference implementation studied: [`arpanghosh8453/garmin-grafana`](https://github.com/arpanghosh8453/garmin-grafana)
(single 60+ panel dashboard + a separate strength-training dashboard, InfluxDB/InfluxQL backend).

This document records what we borrow, what we deliberately reject, and — most importantly — the
**metric naming contract** that the exporter, the collector and every panel agree on.

---

## 1. What that project gets right

**Row-based narrative.** Its rows read as a zoom-out: `Intraday Health Stats` → `Recent Activities` →
`Long-term visualization`. You start at "what is happening today" and end at "what does a year look
like". Copy this ordering.

**The 3/21 split.** A vertical rail of narrow `stat`/`gauge` panels on the left (today's RHR, today's
steps, last sleep hours) with a wide `timeseries` on the right showing the same metric's trend. It
componentises into a reusable row and answers "value now + trajectory" in one glance. This is the
single best layout idea in the dashboard.

**`state-timeline` for sleep stages,** not `timeseries`. Sleep stages are a *state*, not a
measurement — a timeline renders awake/light/deep/REM as readable bands instead of four sawtooth
lines.

**`barchart` for one-value-per-day data.** Steps, sleep score, calories, VO2 max, intensity minutes
are all plotted as bars. See §4: this is the correct answer to our sparse-data problem, and it is
strictly better than the `last_over_time` mitigation in PLAN.md §7.

**`Last Sync` stat with threshold colouring.** A green/amber panel that goes red when the sync is
stale. That is our "exporter hasn't succeeded in 6h" alert, made visible instead of only routed.

**`marcusolsson-hourly-heatmap-panel`** for hour-of-day patterns: steps by hour, and a "sleep
regularity" grid showing bedtime/wake-time drift across weeks. Cheap and genuinely insightful.

**"Week / Month at a glance" tables.** Aggregated rows beat squinting at 90 days of line.

## 2. What we deliberately do not copy

| Their approach | Why not for us |
|---|---|
| One 60-panel monolith | Slow to load, unreviewable in git diffs, and every panel hits the DB on open. We split by question (§3). |
| **GPS tracks as map panels** (`ActivityGPS` with lat/lon per point) | Lat/lon must not become metric labels — that is unbounded cardinality and pathological in Prometheus/VM. Maps require a trace/log store or object storage, not a TSDB. **Explicit non-goal.** |
| **FIT file parsing** (per-lap, per-set, strength volume, 1RM estimates) | Requires `fitparse`/Garmin FIT SDK, indoor/outdoor special-casing, and a whole second ingestion path. Out of scope for the resume project; noted as a future extension. |
| InfluxQL `GROUP BY time(1m)` on raw intraday | MetricsQL expresses this as `avg_over_time(...[1m])`, but we'd rather not ingest 1-minute HR at all until daily panels are boring and reliable. |
| Unbounded `activity_id` tags everywhere | See §5 — allowed on `garmin_activity_*` only, never on intraday metrics. |

## 3. Dashboard set

Four dashboards, matching PLAN.md §7, plus one optional. Each is a separate JSON file in this
directory, provisioned by the Grafana sidecar from a ConfigMap.

| # | Dashboard | Answers | Core panels |
|---|---|---|---|
| 1 | `daily-overview.json` | "How am I today, and how does that compare to the last month?" | Gauge rail (steps, RHR, sleep hours, body battery) + wide 30d trends; week-at-a-glance table; 90d bar charts |
| 2 | `sleep.json` | "Am I sleeping enough, and is the quality holding?" | Stage `state-timeline`; sleep score bars; deep/REM duration bars; sleep-regularity heatmap; HRV vs sleep-score scatter |
| 3 | `training-load.json` | "Am I training consistently, and is it costing me recovery?" | Weekly volume bars; HR-zone distribution; RHR vs weekly load overlay; VO2 max trend |
| 4 | `exporter-health.json` | "Is the pipeline alive, and is anything backing up?" | Last-success stat (thresholds 1h/3h/6h); run duration p95; API error rate; `otelcol_exporter_queue_size`; VM series count |
| 5 | *(optional)* `intraday.json` | "What happened in the last 24h?" | Heart rate over the day (mirrors their `Intraday Health Stats` row). Only build once 1–4 are stable. |

## 4. Sparse data: use the right panel, not a wider window

Garmin daily metrics are **one sample per day**. A `timeseries` panel assumes ~5-minute staleness,
so it draws gaps or straight interpolation between daily points.

PLAN.md §7 proposes `last_over_time(metric[1d])` or a wide `$__interval`. That works, but it is
papering over a panel-type mistake. Preference order:

1. **`barchart` / `stat` for daily-grain metrics.** One bar per day is the honest visualisation, and
   it needs no query gymnastics. This is what the reference dashboard does, and why it looks clean.
2. **Recording rules for anything smoothed.** Grafana cannot store derived series; vmalert can. Define
   `garmin_daily:sleep_score:7d_avg` once in `alerts/` and let every panel and alert read it.
3. **`timeseries` + `last_over_time` only** when you genuinely need to overlay daily and intraday
   signals on one time axis. Set *Connect null values → Threshold* (e.g. 2d) so genuine outages still
   show as gaps.

The `garmin_daily_` prefix (§5) exists so it is obvious at a glance which rule applies.

## 5. Metric naming contract

This is the interface. Changing it later means rewriting every panel and alert, so it is fixed here
first.

**Rules**

- Namespace `garmin_`. OTLP names use dots (`garmin.daily.steps`); the `prometheusremotewrite`
  exporter converts them to underscores. Never emit a pair of names that collide after that
  conversion.
- **`garmin_daily_*`** = exactly one sample per day, event-timestamped. Always query with a daily
  aggregation or a bar chart.
- **`garmin_activity_*`** = one sample per workout, labelled `activity_type`.
- **Intraday metrics carry no unbounded labels.** No per-sample IDs.
- Units in the name: `_seconds`, `_meters`, `_ms`, `_bpm`, `_percent`. Bare names are counts.
- Timestamps are **event time**, not poll time. The backfill path guarantees this; the live OTLP path
  cannot (the SDK stamps at observation), so daily metrics are written by the backfill path.

**Daily (`garmin_daily_*`)**

| Metric | Unit | Labels |
|---|---|---|
| `garmin_daily_steps` | count | `device` |
| `garmin_daily_resting_heart_rate` | bpm | `device` |
| `garmin_daily_sleep_duration_seconds` | s | — |
| `garmin_daily_sleep_score` | 0–100 | — |
| `garmin_daily_deep_sleep_seconds` | s | — |
| `garmin_daily_rem_sleep_seconds` | s | — |
| `garmin_daily_stress_avg` | 0–100 | — |
| `garmin_daily_body_battery_max` | 0–100 | — |
| `garmin_daily_body_battery_min` | 0–100 | — |
| `garmin_daily_hrv_ms` | ms | — |
| `garmin_daily_vo2max` | ml/kg/min | `activity_type` |
| `garmin_daily_calories` | kcal | `device` |
| `garmin_daily_intensity_minutes` | minutes | — |

**Per activity (`garmin_activity_*`)** — labels: `activity_type`, `activity_id`

| Metric | Unit |
|---|---|
| `garmin_activity_duration_seconds` | s |
| `garmin_activity_distance_meters` | m |
| `garmin_activity_avg_heart_rate` | bpm |
| `garmin_activity_max_heart_rate` | bpm |
| `garmin_activity_calories` | kcal |
| `garmin_activity_training_effect` | 0–5 |
| `garmin_activity_elevation_gain_meters` | m |

`activity_id` is the one tolerated high-cardinality label: ~350 activities/year × 7 metrics ≈ 2.5k
series/year, which VictoriaMetrics handles without noticing. It stays off intraday and daily metrics
regardless. It also powers the `$activity` template variable that replaces their `ActivitySelector`.

**Intraday (optional, phase 2)**

| Metric | Cadence | Labels |
|---|---|---|
| `garmin_heart_rate` | 1–5 min | — |
| `garmin_sleep_stage` | per stage change | `stage` (awake/light/deep/rem) |
| `garmin_stress` | 3 min | — |
| `garmin_body_battery` | 3 min | — |

**Exporter self-observability (`garmin_exporter_*`)** — the alert surface from PLAN.md §4

| Metric | Unit | Notes |
|---|---|---|
| `garmin_exporter_last_success_timestamp_seconds` | s | Unix ts. The 6h alert reads this |
| `garmin_exporter_run_success` | 0/1 | — |
| `garmin_exporter_run_duration_seconds` | s | histogram |
| `garmin_exporter_api_errors_total` | count | `kind` label (auth/rate_limit/network) |
| `garmin_exporter_records_written_total` | count | sanity check on backfill |

## 6. Implementation notes

- **Grafana plugins.** Dashboard 2 needs `marcusolsson-hourly-heatmap-panel`. Install via
  `GF_INSTALL_PLUGINS` in the upstream Grafana chart values, or the panel renders blank — and a blank
  panel looks identical to a broken query. Pin the version.
- **Provisioning.** Dashboards go in a ConfigMap labelled `grafana_dashboard: "1"`, picked up by the
  sidecar. Alert rules are *not* sidecar-provisioned — they need `provisioning/alerting/*.yaml`
  mounted at `/etc/grafana/provisioning/alerting`, a different path from the dashboard sidecar.
- **Datasource UID.** Pin an explicit `uid` in the VM datasource and reference it in every panel.
  Otherwise dashboards exported from one Grafana break in another.
- **Template variables.** `$device`, `$activity_type`, and a `$__rate_interval`-equivalent. Keep
  variables low-cardinality or the dropdown itself becomes slow.
- **Authoring.** Hand-written JSON is fine at four dashboards and keeps the diff readable. Grafonnet
  is the upgrade if the count grows past ~8.

## 7. Findings that should update PLAN.md

1. **Garmin archives intraday data older than ~6 months.** Daily aggregates remain available for any
   past date, but 1-minute HR and sleep-stage detail do not. PLAN.md §11 says "backfill in chunks, a
   month at a time" — correct, but it should also say **backfill the last 6 months of intraday first**,
   because that window closes permanently. Daily aggregates can be backfilled lazily, forever.
2. **Sparse-data mitigation in §7 is weaker than it should be.** Bar charts and recording rules beat
   `last_over_time`, per §4 above.
3. **GPS and FIT parsing are out of scope.** Worth an ADR line so nobody expects a map panel.
