SHELL := /bin/bash
.DEFAULT_GOAL := help

UV ?= uv
UVX ?= uvx
NPM ?= npm
CURL ?= curl
DEMO_RUNTIME := bash scripts/demo-runtime.sh
DATAHUB_VERSION ?= 1.6.0
DATAHUB_RELEASE ?= v$(DATAHUB_VERSION)
DATAHUB_CLI := $(UVX) --python 3.11 --from acryl-datahub==$(DATAHUB_VERSION) datahub
DATAHUB_ACTIONS_VERSION ?= 1.6.0.15
DATAHUB_ACTIONS_RECIPE ?= demo/datahub/schema-drift-actions.yml
DATAHUB_KAFKA_BOOTSTRAP ?= 127.0.0.1:9092
DATAHUB_SCHEMA_REGISTRY_URL ?= http://127.0.0.1:8081
DATAHUB_MCL_TOPIC ?= MetadataChangeLog_Versioned_v1
DATARESCUE_API_URL ?= http://127.0.0.1:8000

POSTGRES_DB ?= datarescue
POSTGRES_USER ?= datarescue
POSTGRES_PASSWORD ?= datarescue
POSTGRES_HOST ?= 127.0.0.1
POSTGRES_PORT ?= 55432
export POSTGRES_DB POSTGRES_USER POSTGRES_PASSWORD POSTGRES_HOST POSTGRES_PORT

DBT_PROJECT_DIR := demo/dbt
DBT_CORE_VERSION ?= 1.9.8
DBT_POSTGRES_VERSION ?= 1.9.0
DBT := $(UV) run --python 3.11 --with dbt-core==$(DBT_CORE_VERSION) \
	--with dbt-postgres==$(DBT_POSTGRES_VERSION) -- dbt
DBT_FLAGS := --project-dir $(DBT_PROJECT_DIR) --profiles-dir $(DBT_PROJECT_DIR)
PSQL := $(DEMO_RUNTIME) psql

API_CMD ?= $(UV) run uvicorn apps.api.main:app --reload --host 0.0.0.0 --port 8000
WEB_CMD ?= $(NPM) --prefix apps/web run dev -- --host 0.0.0.0 --port 5173
DATAHUB_GMS_URL ?= http://127.0.0.1:8080
DEMO_POSTGRES_DSN := postgresql://$(POSTGRES_USER):$(POSTGRES_PASSWORD)@$(POSTGRES_HOST):$(POSTGRES_PORT)/$(POSTGRES_DB)

.PHONY: help install bootstrap api-install web-install postgres-up postgres-down postgres-logs \
	demo-data-reset demo-drift demo-containment demo-data-verify dbt-debug dbt-build \
	dbt-build-healthy dbt-candidates-current dbt-candidates dbt-docs dbt-artifacts-current \
	dbt-artifacts-healthy dbt-artifacts datahub-check datahub-ingest-postgres \
	datahub-ingest-dbt datahub-ingest datahub-up datahub-down datahub-seed-healthy \
	datahub-apply-drift datahub-validate datahub-actions-validate datahub-actions-run \
	demo-ready dev demo demo-replay demo-connected test-demo test lint build check \
	clean demo-clean

help: ## Show available commands.
	@awk 'BEGIN {FS = ":.*## "; printf "DataRescue commands:\n\n"} /^[a-zA-Z0-9_-]+:.*## / {printf "  %-25s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

bootstrap: api-install web-install ## Install API and web dependencies.

install: bootstrap ## Judge-friendly alias for installing all dependencies.

api-install:
	$(UV) sync --all-extras

web-install:
	@if [[ -f apps/web/package-lock.json ]]; then \
		$(NPM) --prefix apps/web ci; \
	else \
		$(NPM) --prefix apps/web install; \
	fi

postgres-up: ## Start PostgreSQL 16 and wait until it is healthy.
	$(DEMO_RUNTIME) postgres-up

postgres-down: ## Stop demo services without deleting data.
	$(DEMO_RUNTIME) postgres-down

postgres-logs: ## Follow PostgreSQL logs.
	$(DEMO_RUNTIME) postgres-logs

demo-data-reset: postgres-up ## Restore the healthy pre-drift amount column.
	$(PSQL) -f - < demo/postgres/reset_healthy.sql

demo-drift: postgres-up ## Apply amount -> gross_amount + net_amount drift.
	$(PSQL) -f - < demo/postgres/apply_drift.sql
	$(PSQL) -f - < demo/postgres/verify_drift.sql

demo-containment: postgres-up ## Apply the fail-closed fixture with no safe candidate.
	$(PSQL) -f - < demo/postgres/apply_containment.sql

demo-data-verify: postgres-up ## Assert exact +3.40% gross drift and 100% PK overlap.
	$(PSQL) -f - < demo/postgres/verify_drift.sql

dbt-debug: postgres-up ## Validate the dbt/PostgreSQL connection.
	POSTGRES_HOST=$(POSTGRES_HOST) POSTGRES_PORT=$(POSTGRES_PORT) \
		POSTGRES_DB=$(POSTGRES_DB) POSTGRES_USER=$(POSTGRES_USER) \
		POSTGRES_PASSWORD=$(POSTGRES_PASSWORD) $(DBT) debug $(DBT_FLAGS)

dbt-build: postgres-up ## Build one candidate (set REVENUE_COLUMN and DBT_SCHEMA).
	POSTGRES_HOST=$(POSTGRES_HOST) POSTGRES_PORT=$(POSTGRES_PORT) \
		POSTGRES_DB=$(POSTGRES_DB) POSTGRES_USER=$(POSTGRES_USER) \
		POSTGRES_PASSWORD=$(POSTGRES_PASSWORD) \
		DATARESCUE_REVENUE_COLUMN=$${REVENUE_COLUMN:-net_amount} \
		DBT_SCHEMA=$${DBT_SCHEMA:-analytics} $(DBT) build $(DBT_FLAGS)

dbt-build-healthy: demo-data-reset ## Build and test the healthy amount model.
	REVENUE_COLUMN=amount DBT_SCHEMA=analytics $(MAKE) dbt-build

dbt-candidates-current: ## Validate both candidates against the currently drifted source.
	REVENUE_COLUMN=gross_amount DBT_SCHEMA=candidate_gross $(MAKE) dbt-build
	REVENUE_COLUMN=net_amount DBT_SCHEMA=candidate_net $(MAKE) dbt-build
	$(PSQL) -f - < demo/postgres/verify_candidates.sql

dbt-candidates: demo-drift ## Apply drift and validate both candidates.
	@$(MAKE) dbt-candidates-current

dbt-docs: postgres-up ## Generate dbt manifest and catalog for the selected mapping.
	POSTGRES_HOST=$(POSTGRES_HOST) POSTGRES_PORT=$(POSTGRES_PORT) \
		POSTGRES_DB=$(POSTGRES_DB) POSTGRES_USER=$(POSTGRES_USER) \
		POSTGRES_PASSWORD=$(POSTGRES_PASSWORD) \
		DATARESCUE_REVENUE_COLUMN=$${REVENUE_COLUMN:-net_amount} \
		DBT_SCHEMA=$${DBT_SCHEMA:-analytics} $(DBT) docs generate $(DBT_FLAGS)

dbt-artifacts-current: ## Build selected net mapping and DataHub dbt artifacts.
	REVENUE_COLUMN=net_amount DBT_SCHEMA=analytics $(MAKE) dbt-build
	REVENUE_COLUMN=net_amount DBT_SCHEMA=analytics $(MAKE) dbt-docs

dbt-artifacts-healthy: demo-data-reset ## Build healthy amount artifacts for initial DataHub ingestion.
	REVENUE_COLUMN=amount DBT_SCHEMA=analytics $(MAKE) dbt-build
	REVENUE_COLUMN=amount DBT_SCHEMA=analytics $(MAKE) dbt-docs

dbt-artifacts: demo-drift ## Generate drifted manifest, run-results and catalog.
	@$(MAKE) dbt-artifacts-current

datahub-check: ## Check the separately managed DataHub GMS.
	@$(CURL) --fail --silent --show-error "$(DATAHUB_GMS_URL)/config" >/dev/null
	@echo "DataHub GMS is reachable at $(DATAHUB_GMS_URL)."

datahub-up: ## Start supported DataHub v1.6 Quickstart (requires Docker Compose v2).
	@if ! docker compose version >/dev/null 2>&1; then \
		echo "DataHub's supported quickstart requires the Docker Compose v2 plugin." >&2; \
		echo "Install/repair Compose, then rerun 'make datahub-up'." >&2; \
		exit 1; \
	fi
	$(DATAHUB_CLI) docker quickstart --version $(DATAHUB_RELEASE) --dump-logs-on-failure
	@$(MAKE) datahub-check

datahub-down: ## Stop the supported DataHub Quickstart stack.
	$(DATAHUB_CLI) docker quickstart --stop

datahub-ingest-postgres: postgres-up datahub-check ## Ingest PostgreSQL metadata into DataHub.
	$(DEMO_RUNTIME) datahub-ingest-postgres

datahub-ingest-dbt: dbt-artifacts datahub-check ## Ingest dbt artifacts into DataHub.
	$(DEMO_RUNTIME) datahub-ingest-dbt

datahub-ingest: dbt-artifacts datahub-check ## Build artifacts, then run both DataHub recipes.
	$(DEMO_RUNTIME) datahub-ingest-postgres
	$(DEMO_RUNTIME) datahub-ingest-dbt

datahub-validate: dbt-artifacts ## Dry-run both recipes without writing to DataHub.
	DATAHUB_INGEST_DRY_RUN=1 $(DEMO_RUNTIME) datahub-ingest-postgres
	DATAHUB_INGEST_DRY_RUN=1 $(DEMO_RUNTIME) datahub-ingest-dbt

datahub-actions-validate: ## Validate the pinned DataHub Actions recipe and custom action import.
	DATAHUB_ACTIONS_VERSION=$(DATAHUB_ACTIONS_VERSION) \
		DATAHUB_ACTIONS_RECIPE=$(DATAHUB_ACTIONS_RECIPE) \
		DATAHUB_KAFKA_BOOTSTRAP=$(DATAHUB_KAFKA_BOOTSTRAP) \
		DATAHUB_SCHEMA_REGISTRY_URL=$(DATAHUB_SCHEMA_REGISTRY_URL) \
		DATAHUB_MCL_TOPIC=$(DATAHUB_MCL_TOPIC) \
		DATARESCUE_API_URL=$(DATARESCUE_API_URL) \
		bash scripts/datahub-actions.sh validate

datahub-actions-run: ## Consume schemaMetadata MCLs; requires a ready DataRescue API.
	@$(CURL) --fail --silent --show-error "$(DATARESCUE_API_URL)/health" >/dev/null || { \
		echo "Start the DataRescue API before the MCL consumer." >&2; \
		exit 1; \
	}
	DATAHUB_ACTIONS_VERSION=$(DATAHUB_ACTIONS_VERSION) \
		DATAHUB_ACTIONS_RECIPE=$(DATAHUB_ACTIONS_RECIPE) \
		DATAHUB_KAFKA_BOOTSTRAP=$(DATAHUB_KAFKA_BOOTSTRAP) \
		DATAHUB_SCHEMA_REGISTRY_URL=$(DATAHUB_SCHEMA_REGISTRY_URL) \
		DATAHUB_MCL_TOPIC=$(DATAHUB_MCL_TOPIC) \
		DATARESCUE_API_URL=$(DATARESCUE_API_URL) \
		bash scripts/datahub-actions.sh run

datahub-seed-healthy: dbt-artifacts-healthy datahub-check ## Ingest the healthy schema as MCL baseline.
	$(DEMO_RUNTIME) datahub-ingest-postgres
	$(DEMO_RUNTIME) datahub-ingest-dbt

datahub-apply-drift: demo-drift datahub-check ## Emit real drift MCL, validate candidates and ingest selected dbt state.
	$(DEMO_RUNTIME) datahub-ingest-postgres
	@$(MAKE) dbt-candidates-current
	@$(MAKE) dbt-artifacts-current
	$(DEMO_RUNTIME) datahub-ingest-dbt

demo-ready: dbt-candidates ## Prepare deterministic replay data and both validation results.
	@echo "Demo data is ready: gross rejected (+3.40%), net selected (0.00%)."

dev: ## Run API and web together; Ctrl-C stops both.
	@set -e; \
	$(API_CMD) & api_pid=$$!; \
	$(WEB_CMD) & web_pid=$$!; \
	cleanup() { kill $$api_pid $$web_pid 2>/dev/null || true; }; \
	trap cleanup EXIT INT TERM; \
	wait $$api_pid $$web_pid

demo: bootstrap demo-ready ## Launch recorded context with live PostgreSQL/dbt validation.
	@echo "Starting DataRescue at http://127.0.0.1:5173 (API: http://127.0.0.1:8000)."
	@echo "DataHub context is recorded replay; PostgreSQL/dbt candidate validation is live."
	@DATARESCUE_REPLAY_MODE=true DATARESCUE_EXECUTION_MODE=postgres \
		DATARESCUE_POSTGRES_DSN=$(DEMO_POSTGRES_DSN) $(MAKE) dev

demo-replay: bootstrap ## Launch the fastest deterministic app without live candidate execution.
	@echo "Starting the explicitly labeled all-replay demo."
	@DATARESCUE_REPLAY_MODE=true DATARESCUE_EXECUTION_MODE=replay $(MAKE) dev

demo-connected: bootstrap ## Run the fail-fast DataHub/OpenAI/GitHub connected path.
	@bash scripts/demo-connected.sh

test-demo: dbt-candidates ## Run PostgreSQL/dbt plus live workflow integration proof.
	DATARESCUE_POSTGRES_DSN=$(DEMO_POSTGRES_DSN) $(UV) run python scripts/verify-live-workflow.py

test: ## Run API unit tests, web tests and demo integration tests.
	$(UV) run pytest
	$(NPM) --prefix apps/web test
	$(MAKE) test-demo

lint: ## Run Python and TypeScript static checks.
	$(UV) run ruff check .
	$(UV) run mypy apps packages
	$(NPM) --prefix apps/web run build

build: ## Build the production web bundle.
	$(NPM) --prefix apps/web run build

check: lint test build ## Run static checks, tests, integrations, and production build.

clean: ## Remove dbt build artifacts only.
	$(DBT) clean $(DBT_FLAGS)

demo-clean: ## Delete demo containers and the PostgreSQL volume.
	@echo "Deleting the DataRescue demo database volume."
	$(DEMO_RUNTIME) clean
