# Runbook

One section per alert, named exactly like the alert so the link in its
description lands here. Start with the first command in each section; it
usually tells you which of the causes listed applies.

Severity: **critical** means data is not flowing, so act today. **warning**
means data is degraded, so look when convenient. **info** is a personal
health signal, not a system fault.

---

## Exporter

### GarminExporterStale

*Critical.* Runs are happening, but none has succeeded for 6 hours.

```bash
make logs                       # the last run's error
make sync                       # run once now and watch it
```

- `authentication failed` → see [GarminAuthFailing](#garminauthfailing).
- `run produced no usable data` → every Garmin source failed. Usually Garmin
  is down or rate-limiting. Check whether the Garmin Connect app works; if it
  does, compare `make dump` output with `exporter/tests/fixtures/`.
- Collector connection errors → see [OtelCollectorDown](#otelcollectordown).

### GarminExporterSilent

*Critical.* No exporter metrics have reached VictoriaMetrics for 2 hours.
Unlike `GarminExporterStale`, this fires when nothing arrives at all.

```bash
make status                     # is the CronJob there? suspended? jobs failing to start?
kubectl --context minikube -n garmin get events --sort-by=.lastTimestamp | tail
```

- No recent Jobs: the CronJob is suspended or missed its `startingDeadlineSeconds`.
- Pods in `ImagePullBackOff` / `ErrImageNeverPull`: rebuild with `make image-load`.
- Jobs succeed but nothing is stored: the Collector or remote write is broken.
  `OtelCollectorDown` or `OtelCollectorExportFailing` should be firing too.

### GarminExporterFailing

*Critical.* Every run in the last 3 hours failed. This also fires on a fresh
install whose first run has never succeeded. Same steps as
[GarminExporterStale](#garminexporterstale).

### GarminAuthFailing

*Critical.* Garmin rejected the session. A CronJob cannot answer an MFA
prompt, so this does not recover on its own.

```bash
make login                      # on your laptop; answers MFA, refreshes exporter/.tokens/
make token-seed                 # copy the new token file into the cluster
make sync                       # confirm
```

This alert suppresses the stale, failing and data-gap alerts it causes, so
expect them to resolve once it clears.

### GarminSourcesFailing

*Warning.* One or more Garmin endpoints have failed on every run for
2 hours. The rest of the data is fine.

```bash
make logs | grep '"source failed"'
```

- HTTP 400: Garmin tightened a window limit. Check `MAX_SPAN_DAYS` and
  `Source.span_days` in `exporter/src/garmin_exporter/fetch.py`.
- HTTP 429: rate limited. Raise `GARMIN_REQUEST_PAUSE_SECONDS` on the CronJob.
- `missing_method`: a `garminconnect` upgrade renamed a method.

### GarminSleepDataGap

*Warning.* There is no sleep record dated within the last 36 hours.

Most often the watch wasn't worn overnight or hasn't synced to the phone yet.
If the Garmin app *does* show last night's sleep, the sleep source is failing:
see [GarminSourcesFailing](#garminsourcesfailing).

---

## Collector

See [otel-collector.md](otel-collector.md) for how the Collector works and
for the debugging commands referred to below.

### OtelCollectorDown

*Critical.* The Collector's self-telemetry has stopped arriving. Every Garmin
write goes through the Collector, so nothing is being stored.

```bash
make status
make collector-logs
kubectl --context minikube -n garmin describe deployment otel-collector
```

- `CrashLoopBackOff` with a config error in the log: validate the config
  ([command](otel-collector.md#debugging)) and roll back the last change to
  `deploy/config/collector.yaml`.
- `OOMKilled`: memory_limiter should prevent this. Check
  `GOMEMLIMIT` and the `limits.memory` on the Deployment still match the
  limiter's percentages.

No data is lost while it is down, as long as it comes back within the
exporter's 2-day lookback: each run re-sends the last 2 days.

### OtelCollectorExportFailing

*Critical.* The Collector dropped points it couldn't write to
VictoriaMetrics, either because retries ran out or because its queue was full.

```bash
make collector-logs | grep -i 'remote write\|dropping'
kubectl --context minikube -n garmin get pods -l app.kubernetes.io/name=victoriametrics
```

- VictoriaMetrics down or restarting: once it's back, the next exporter runs
  re-send the last 2 days.
- During a large backfill: the queue filled faster than VictoriaMetrics could
  write. Re-run the affected range with a smaller `--chunk-days`. Writes are
  idempotent, so overlapping is harmless.

### OtelCollectorRefusingData

*Warning.* The Collector's receiver refused points. This is almost always
`memory_limiter` protecting the process. The exporter run that was refused
fails, and its data is re-sent next run.

Normal during a very large backfill. If it happens during normal operation,
look at the "Collector memory" panel on the Garmin Pipeline dashboard, and
raise the Deployment's memory limit together with `GOMEMLIMIT`.

### GarminMetricStoppedArriving

*Warning.* A metric reached the Collector in the previous 24 hours but not in
the last 24 hours. This comes from the count connector, so it means the
exporter stopped *sending* the metric, not that storage lost it.

Usually Garmin changed a response shape and one transform now finds nothing:

```bash
make dump DATE=$(date +%F)       # fresh raw payloads in exporter/fixtures/
# Compare the *shape* (key paths), not the values:
shape() { jq -r 'paths | map(tostring) | join(".")' "$1" | sed -E 's/\.[0-9]+/[]/g' | sort -u; }
diff <(shape exporter/tests/fixtures/raw-2026-10-28.json) \
     <(shape exporter/fixtures/$(date +%F)/raw.json)
```

Find the source that feeds the named metric in `transform.py`, fix the path,
and add the new payload shape to the fixture.

---

## Health (info)

These are signals about you, not about the system. They are routed with a
24-hour repeat.

### GarminStepsBelowGoal

Each of the last three completed days was below that day's step goal.

### GarminRestingHeartRateElevated

Yesterday's resting heart rate was more than 10% above the average of the
7 days before. This is a common early sign of illness, overtraining or poor
recovery. It needs at least 5 days of baseline before it can fire.

---

## WhatsApp delivery

Alertmanager has no native WhatsApp integration. Alerts go to the gateway
in `deploy/80-whatsapp-gateway.yaml` (script:
`deploy/helper/whatsapp-gateway.py`), which calls CallMeBot.

1. Save the CallMeBot WhatsApp number and send it the registration message
   from [callmebot.com](https://www.callmebot.com/blog/free-api-whatsapp-messages/).
   You'll get an API key back.
2. `make whatsapp-secret PHONE=+351XXXXXXXXX APIKEY=XXXXXX`

Until step 2, the gateway accepts alerts and only logs them, which is a useful
way to check routing:

```bash
kubectl --context minikube -n garmin logs deployment/whatsapp-gateway -f
```

Alertmanager authenticates to the gateway with a bearer token from the
`alert-webhook-token` Secret. `make secret` generates it once. A `401` in the
gateway log means Alertmanager and the gateway hold different tokens: restart
both after the Secret changes.

To use Twilio or another provider instead, change `send_whatsapp()` in the
gateway script. The Alertmanager side does not change.
