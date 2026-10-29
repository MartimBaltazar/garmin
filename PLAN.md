# Garmin Observability: Project Plan

## 0. Goal and "done" definition
A personal health observability stack on Kubernetes. A Python exporter pulls Garmin data into VictoriaMetrics via OpenTelemetry, Grafana shows dashboards and alerts, and Terraform and Helm provision everything.

**The resume bullet is true when:**
- [ ] It runs on a real k3s cluster, not only locally
- [ ] Dashboards and alerts are in git, not click-ops
- [ ] Infra is Terraform and the app is Helm
- [ ] CI lints, tests and builds the image
- [ ] The README has a live link or screenshots

## 1. Architecture
```
Garmin Connect ──► exporter (CronJob, Python)
                      ├─ live path:     OTel SDK ─► OTel Collector ─► VictoriaMetrics
                      └─ backfill path: direct import API ──────────► VictoriaMetrics
                                                                         │
Cloudflare Access + Tunnel ◄── Grafana ◄────────────────────────────────┘
                                  └─ alerts-as-code
vmbackup CronJob ──► Cloudflare R2
```

**Note:** the OTel SDK stamps metrics with collection time, so it can't backfill history. Use OTel for the live path. Backfill writes straight to VictoriaMetrics (`/api/v1/import/prometheus` with explicit timestamps). It's a separate code path and worth a sentence in the README.

## 2. Repo layout
```
garmin-observability/
├── exporter/          # Python package, Dockerfile, tests
├── charts/garmin-obs/ # Helm chart (or umbrella over upstream charts)
├── dashboards/        # Grafana JSON or Grafonnet
├── alerts/            # alert rules as code
├── terraform/
│   ├── oracle/        # VM, network, k3s install
│   └── cloudflare/    # tunnel, access policy, R2 bucket, DNS
├── hack/              # k3d config, Makefile helpers
├── .github/workflows/ # CI
├── docs/              # architecture, runbook, decisions (ADRs)
└── README.md
```

## 3. Phase 1: Data discovery (day 1-2)
- [x] Choose the source: **`garminconnect`** (unofficial, richer data). Decided. Strava is activities-only and would lose sleep, stress, body battery and HRV.
- [ ] Log in locally and dump raw JSON for one day. Handle MFA and cache the token on disk (the library does this), so you don't re-login every run. `garmin-exporter dump --date YYYY-MM-DD` writes the raw payloads to `exporter/fixtures/`.
- [ ] **Re-verify field names against real payloads** before trusting the transform layer. The mapping in `exporter/src/garmin_exporter/transform.py` is written from the library's documented shapes, not a live response.
- **Library note (v0.3.x):** no more `garth`. Python 3.12+, tokens at `<tokenstore>/garmin_tokens.json`, and `login()` self-heals by discarding rejected tokens and doing a fresh credential login. `return_on_mfa=True` makes MFA resumable in two steps (`login()` → `resume_login()`), which is what lets the CronJob run without credentials.
- **Range endpoints change the backfill cost:** `get_daily_summaries(start, end)`, `get_daily_sleep`, `get_rhr_daily`, `get_daily_stress_stats`, `get_daily_body_battery_stats`, `get_hrv_data_range` all take a date range and return N days in one call. A month of backfill is a handful of requests, not 30.
- **Dashboard reference:** [`arpanghosh8453/garmin-grafana`](https://github.com/arpanghosh8453/garmin-grafana) — a mature InfluxDB + Grafana implementation of this exact idea. Design notes, what to borrow, and the metric naming contract are in [`dashboards/README.md`](dashboards/README.md). Read that before writing any panel.
- [x] Decide metrics. The full contract is in [`dashboards/README.md`](dashboards/README.md): a `garmin_daily_*` prefix for one-sample-per-day metrics, `garmin_activity_*` for per-workout, `garmin_exporter_*` for self-observability.
  - Daily: steps, resting HR, sleep duration and score, stress, body battery, HRV, VO2 max
  - Per activity: distance, duration, avg HR, calories
- [ ] **Risk:** it's unofficial, so Garmin can block or break it. Mitigation: low poll rate (every 15-30 min), cached tokens, and a Strava fallback noted in docs.

## 4. Phase 2: Exporter (week 1)
- [x] Python package with a clean fetch -> transform -> emit split, so transforms are unit-testable on saved JSON fixtures
- [x] Live mode: OTel SDK gauges, OTLP to the Collector
- [x] Backfill mode: `--from/--to` flags, writes timestamped samples to the VictoriaMetrics import endpoint. It must be idempotent, since re-running overwrites the same timestamps
- [x] Self-observability: metrics for run success, duration, API errors and last-success timestamp (this feeds an alert later)
- [x] Structured logging, retry with backoff, a Garmin rate-limit guard
- [x] Dockerfile: multi-stage, non-root, pinned deps
- [ ] Tests: pytest on fixtures (49 tests, green), plus one integration test against a local VictoriaMetrics container

## 5. Phase 3: Local cluster (week 1-2)
- [ ] `k3d cluster create` with a config file (same k3s as production)
- [ ] Deploy VictoriaMetrics single-node, OTel Collector, Grafana
- [ ] **Set `-retentionPeriod` explicitly** (e.g. `10y`). The default is 1 month and would silently drop your backfill.
- [ ] Credentials as a Kubernetes Secret created from env at deploy time (`kubectl create secret ... --from-env-file`, git-ignored). **Never commit credentials.**
- [x] CronJobs: live exporter (`hack/exporter-cronjob.yaml`, `*/30 * * * *`)
- [ ] A one-shot backfill Job (currently run via `garmin-exporter backfill` from the CLI or `kubectl create job --from=cronjob/...`)
- [ ] Persistent volumes for VictoriaMetrics and Grafana
- [ ] `make up / down / backfill / test` targets

## 6. Phase 4: Helm chart (week 2)
- [ ] Own chart for the exporter and CronJobs, with `values.yaml` for local and `values-prod.yaml`
- [ ] Reuse upstream charts (VictoriaMetrics, Grafana, OTel Collector) as dependencies instead of rewriting them
- [ ] Resource requests and limits sized for a small ARM node
- [ ] Liveness/readiness probes where applicable
- [ ] `helm lint` and `helm template | kubeconform` in CI

## 7. Phase 5: Dashboards and alerts as code (week 2-3)
- [ ] **Dashboards in git**, provisioned via sidecar or ConfigMap: Daily Overview, Sleep, Training Load, Exporter Health
- [ ] **Follow `dashboards/README.md`** for the panel set, layout conventions and the `garmin_daily_*` naming contract. Borrowed from `arpanghosh8453/garmin-grafana`: the 3/21 gauge-rail + wide-trend split, `state-timeline` for sleep stages, bar charts for daily-grain data, and a threshold-coloured "Last Sync" panel.
- [ ] **Sparse-data gotcha, corrected:** Garmin data is daily or sparse, but Grafana assumes 5-minute staleness, so lines look broken. Do **not** lead with `last_over_time(metric[1d])`. Instead: (1) use `barchart`/`stat` for one-sample-per-day metrics, (2) push anything smoothed into a **recording rule** so it becomes a real dense series, (3) only then reach for `last_over_time` + connect-null-values on panels that genuinely mix daily and intraday signals.
- [ ] **Out of scope by decision:** GPS map panels (lat/lon as metric labels is pathological cardinality) and FIT-file parsing (per-lap splits, strength sets, 1RM). Record both in an ADR so nobody expects a map tab.
- [ ] **Alerts as code** (Grafana provisioning YAML or Terraform Grafana provider):
  - Exporter hasn't succeeded in 6h
  - Garmin auth failing
  - Data gap (no new sleep record for 36h)
  - Optional personal: resting HR up 10% over a 7-day baseline
- [ ] Route alerts to a free notification channel (Telegram or email), not a paging service

## 8. Phase 6: CI/CD (week 3)
GitHub Actions, since the repo is public:
- [ ] Lint (ruff), type check (mypy), tests (pytest)
- [ ] `helm lint`, `kubeconform`, `terraform fmt/validate`, `tflint`
- [ ] Build and push the image to GHCR on tag, multi-arch (`linux/arm64` is required for Oracle ARM)
- [ ] Dependabot or Renovate for deps
- [ ] Secret scanning (gitleaks) as a CI step, given the Garmin credentials

## 9. Phase 7: Oracle Always Free + Terraform (week 3)
- [ ] **Start the Oracle signup and instance creation attempt now.** ARM capacity is often unavailable, so retry in the background. Fallback: Hetzner CAX11, about EUR 4-5, same code.
- [ ] Terraform: VCN, subnet, security list (SSH from your IP only, nothing else inbound), A1 instance (4 OCPU / 24 GB or smaller)
- [ ] Install k3s via cloud-init, with Traefik disabled if you'll use Tunnel only
- [ ] Keep Terraform state remote (R2 or Terraform Cloud free tier), not in git
- [ ] **Avoid reclamation:** Oracle can reclaim idle Always Free instances. The exporter and Grafana give it steady load, but add a note and monitor CPU

## 10. Phase 8: Cloudflare (week 3-4)
- [ ] Terraform Cloudflare provider: Tunnel, DNS record, Access application and policy (your email only), R2 bucket
- [ ] `cloudflared` as a Deployment in the cluster, so there are no open inbound ports
- [ ] Grafana at `grafana.<yourdomain>` behind Access
- [ ] `vmbackup` CronJob to R2 (S3-compatible), plus a documented restore test

## 11. Phase 9: Production deploy and backfill (week 4)
- [ ] `helm upgrade --install` with `values-prod.yaml`
- [ ] **Backfill the last 6 months of intraday data first.** Garmin moves intraday detail (1-minute HR, sleep stages) older than ~6 months into cold storage; it is not served by the regular API endpoints any more. Daily aggregates stay available for any past date, forever. So the only thing that expires is the thing you'd lose permanently — do it first, in one pass.
- [ ] Then backfill **daily aggregates** backwards in chunks (a month at a time). These are safe to do lazily. The range endpoints return a month per call, so this is cheap.
- [ ] Verify with spot checks against Garmin's own app
- [ ] Restore test: delete the PVC in a scratch namespace and restore from R2

## 12. Phase 10: Polish for the resume (week 4)
- [ ] README: architecture diagram, screenshots, one-command local start, design decisions (why VictoriaMetrics, why two ingest paths, why k3s)
- [ ] `docs/runbook.md`: what to do when the exporter fails or auth expires
- [ ] 2-3 ADRs (VictoriaMetrics vs Prometheus, source choice, hosting choice)
- [ ] Make sure the repo has **no secrets in history**
- [ ] Replace the placeholder GitHub URL on the resume, and decide whether to link the live Grafana (read-only public dashboard, or screenshots only, since it's health data)

## 13. Timeline
| Week | Focus | Exit check |
|---|---|---|
| 1 | Phases 1-3 | Data flows into local VictoriaMetrics |
| 2 | Phases 4-5 | `make up` gives a full local stack with dashboards |
| 3 | Phases 6-7 | CI green, Oracle (or Hetzner) cluster running |
| 4 | Phases 8-10 | Live behind Access, backups tested, README done |

Oracle's signup and capacity retry should be in week 1 so it doesn't block week 3.

## 14. Risks
| Risk | Mitigation |
|---|---|
| Garmin blocks the unofficial API | Low poll rate, token cache, Strava fallback |
| **Intraday data older than 6 months is archived by Garmin** | Pull the recent 6 months of intraday first; daily aggregates never expire so they can wait. Document that older intraday is unrecoverable in bulk. |
| Oracle ARM capacity or reclamation | Retry early, Hetzner fallback, steady load |
| Backfill silently dropped | Explicit retention period and spot checks |
| Credentials leaked | Kubernetes Secret only, gitleaks in CI, git-ignored env files |
| Scope creep | Weeks 1-2 deliver a working local stack by themselves. GPS maps and FIT parsing are explicitly out of scope. |

## 15. Resume wording, once true
Keep the bullet to what you built. If Cloudflare Access and R2 backups are in, you can add "behind Cloudflare Access, with backups to R2". Don't mention anything that's still on this list.
