.PHONY: help venv install test lint fmt image dry-run clean

VENV := .venv
PY := $(VENV)/bin/python
# Override with `make install PYTHON=python3.12` to match the container image.
PYTHON ?= python3.13

help:
	@grep -E '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

venv: ## Create the virtualenv
	$(PYTHON) -m venv $(VENV)
	$(PY) -m pip install --upgrade pip

install: venv ## Install the exporter with dev deps
	$(PY) -m pip install -e "exporter[dev]"

test: ## Run tests
	cd exporter && ../$(PY) -m pytest

lint: ## Ruff + mypy
	cd exporter && ../$(PY) -m ruff check . && ../$(PY) -m mypy

fmt: ## Format with ruff
	cd exporter && ../$(PY) -m ruff format .

image: ## Build the container image
	docker build -t garmin-exporter:dev exporter

dry-run: ## Exercise the pipeline without emitting
	cd exporter && ../$(PY) -m garmin_exporter run --dry-run

clean: ## Remove local artifacts
	rm -rf $(VENV) exporter/build exporter/dist exporter/*.egg-info
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
