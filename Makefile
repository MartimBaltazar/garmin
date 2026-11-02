.PHONY: help venv install test lint fmt image config login dump dry-run backfill clean \
        cluster-up image-load secret token-seed up down status logs grafana \
        cluster-backfill sync query

VENV := .venv
PY := $(VENV)/bin/python
# Override with `make install PYTHON=python3.12` to match the container image.
PYTHON ?= python3.13
DATE ?= $(shell date +%F)
# Run every exporter command from exporter/ so the relative .env and .tokens
# paths resolve the same way regardless of where make is invoked.
RUN := cd exporter && ../$(PY) -m garmin_exporter

# --- local cluster -------------------------------------------------------- #
# Every kubectl call names the context explicitly. The machine's ambient
# context is an EKS cluster, and a stray `kubectl apply` must never be able to
# reach it by accident.
KUBECTL ?= kubectl --context minikube
NS := garmin
IMAGE ?= garmin-exporter:dev
FROM ?= 2026-10-12
TO ?= $(shell date +%F)
export GRAFANA_PASSWORD ?= admin

help:
	@grep -E '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

venv: ## Create the virtualenv
	$(PYTHON) -m venv $(VENV)
	$(PY) -m pip install --upgrade pip

install: venv ## Install the exporter with dev deps
	$(PY) -m pip install -e "exporter[dev]"

config: ## Show resolved settings and which .env file was read
	$(RUN) config

login: ## Interactive Garmin login (needs GARMIN_EMAIL/GARMIN_PASSWORD in exporter/.env)
	$(RUN) login

dump: ## Dump raw Garmin payloads. Usage: make dump DATE=2026-10-28
	$(RUN) dump --date $(DATE) --out fixtures

dry-run: ## Exercise the pipeline without emitting
	$(RUN) run --dry-run

backfill: ## Backfill history locally. Usage: make backfill FROM=2026-04-01 TO=2026-10-01
	$(RUN) backfill --from $(FROM) --to $(TO)

test: ## Run tests
	cd exporter && ../$(PY) -m pytest

lint: ## Ruff + mypy
	cd exporter && ../$(PY) -m ruff check . && ../$(PY) -m mypy

fmt: ## Format with ruff
	cd exporter && ../$(PY) -m ruff format .

image: ## Build the container image
	docker build -t $(IMAGE) exporter

clean: ## Remove local artifacts
	rm -rf $(VENV) exporter/build exporter/dist exporter/*.egg-info
	find . -name __pycache__ -type d -prune -exec rm -rf {} +

# --- cluster -------------------------------------------------------------- #
cluster-up: ## Start minikube if it is not running
	@if ! minikube status -p minikube >/dev/null 2>&1; then \
		minikube start -p minikube; \
	fi

image-load: image ## Build the exporter image and load it into minikube
	minikube image load -p minikube $(IMAGE)

secret: ## Create the Grafana admin Secret (never committed)
	@$(KUBECTL) -n $(NS) create secret generic grafana-credentials \
		--from-literal=password="$$GRAFANA_PASSWORD" \
		--dry-run=client -o yaml | $(KUBECTL) apply -f -

token-seed: ## Copy the local Garmin token file into the cluster's token PVC
	@test -f exporter/.tokens/garmin_tokens.json || { \
		echo "no token file at exporter/.tokens/garmin_tokens.json -- run 'make login' first"; \
		exit 1; \
	}
	@$(KUBECTL) -n $(NS) delete pod garmin-token-seed --ignore-not-found --wait=false >/dev/null 2>&1 || true
	$(KUBECTL) -n $(NS) apply -f deploy/helper/token-seed-pod.yaml
	$(KUBECTL) -n $(NS) wait --for=condition=Ready pod/garmin-token-seed --timeout=120s
	$(KUBECTL) -n $(NS) exec -i garmin-token-seed -- \
		sh -c 'cat > /tokens/garmin_tokens.json && chown 10001:10001 /tokens/garmin_tokens.json && chmod 600 /tokens/garmin_tokens.json' \
		< exporter/.tokens/garmin_tokens.json
	@$(KUBECTL) -n $(NS) delete pod garmin-token-seed --wait=false >/dev/null 2>&1 || true
	@echo "token seeded"

up: cluster-up image-load secret ## Bring up the whole local stack
	$(KUBECTL) apply -k deploy/
	$(KUBECTL) -n $(NS) wait --for=condition=Available deployment/victoriametrics --timeout=300s
	$(KUBECTL) -n $(NS) wait --for=condition=Available deployment/otel-collector --timeout=300s
	$(KUBECTL) -n $(NS) wait --for=condition=Available deployment/grafana --timeout=300s
	$(MAKE) token-seed
	@echo
	@echo "Grafana:   make grafana   (then open http://localhost:3000, admin / $$GRAFANA_PASSWORD)"
	@echo "VictoriaMetrics: make query Q='garmin_daily_steps'"

down: ## Remove the stack, including its data (keeps the cluster running)
	$(KUBECTL) delete namespace $(NS) --ignore-not-found

status: ## Show what is running
	$(KUBECTL) -n $(NS) get pods,svc,pvc,cronjob,job

logs: ## Tail the exporter logs
	$(KUBECTL) -n $(NS) logs -l app.kubernetes.io/name=garmin-exporter -f --tail=50

grafana: ## Port-forward Grafana to localhost:3000
	$(KUBECTL) -n $(NS) port-forward svc/grafana 3000:3000

cluster-backfill: ## Backfill history into the cluster. Usage: make cluster-backfill FROM=2026-10-12 TO=2026-10-29
	$(KUBECTL) -n $(NS) delete job garmin-backfill --ignore-not-found
	sed -e 's/__FROM__/$(FROM)/' -e 's/__TO__/$(TO)/' deploy/50-backfill-job.yaml | $(KUBECTL) apply -f -
	$(KUBECTL) -n $(NS) wait --for=condition=complete job/garmin-backfill --timeout=1800s
	$(KUBECTL) -n $(NS) logs job/garmin-backfill --tail=20

sync: ## Force a one-off exporter run (uses cached tokens, no MFA). Usage: make sync
	@$(KUBECTL) -n $(NS) delete job garmin-exporter-sync --ignore-not-found --wait=false >/dev/null 2>&1 || true
	$(KUBECTL) -n $(NS) create job --from=cronjob/garmin-exporter garmin-exporter-sync >/dev/null 2>&1
	$(KUBECTL) -n $(NS) wait --for=condition=complete job/garmin-exporter-sync --timeout=600s
	$(KUBECTL) -n $(NS) logs job/garmin-exporter-sync --tail=5

query: ## Query VictoriaMetrics. Usage: make query Q='garmin_daily_steps'
	$(KUBECTL) -n $(NS) exec deployment/victoriametrics -- \
		wget -qO- "http://127.0.0.1:8428/api/v1/query?query=$(Q)"
