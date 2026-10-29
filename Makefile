.PHONY: help venv install test lint fmt image config login dump dry-run backfill clean

VENV := .venv
PY := $(VENV)/bin/python
# Override with `make install PYTHON=python3.12` to match the container image.
PYTHON ?= python3.13
DATE ?= $(shell date +%F)
# Run every exporter command from exporter/ so the relative .env and .tokens
# paths resolve the same way regardless of where make is invoked.
RUN := cd exporter && ../$(PY) -m garmin_exporter

help:
	@grep -E '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

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

backfill: ## Backfill history. Usage: make backfill FROM=2026-04-01 TO=2026-10-01
	$(RUN) backfill --from $(FROM) --to $(TO)

test: ## Run tests
	cd exporter && ../$(PY) -m pytest

lint: ## Ruff + mypy
	cd exporter && ../$(PY) -m ruff check . && ../$(PY) -m mypy

fmt: ## Format with ruff
	cd exporter && ../$(PY) -m ruff format .

image: ## Build the container image
	docker build -t garmin-exporter:dev exporter

clean: ## Remove local artifacts
	rm -rf $(VENV) exporter/build exporter/dist exporter/*.egg-info
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
