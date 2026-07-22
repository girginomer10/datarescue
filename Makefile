SHELL := /bin/bash
.DEFAULT_GOAL := help

UV ?= uv
UVX ?= uvx
NPM ?= npm
CURL ?= curl
PYTHON_VERSION ?= 3.11
UV_RUN := $(UV) run --python $(PYTHON_VERSION)
DEMO_RUNTIME := bash scripts/demo-runtime.sh
DATAHUB_VERSION ?= 1.6.0
DATAHUB_RELEASE ?= v$(DATAHUB_VERSION)
DATAHUB_CLI := $(UVX) --python $(PYTHON_VERSION) --from acryl-datahub==$(DATAHUB_VERSION) datahub
DATAHUB_ACTIONS_VERSION ?= 1.6.0.15
DATAHUB_ACTIONS_RECIPE ?= demo/datahub/schema-drift-actions.yml
DATAHUB_MAPPED_GMS_PORT ?= 18080
DATAHUB_GMS_URL ?= http://127.0.0.1:$(DATAHUB_MAPPED_GMS_PORT)
DATAHUB_GMS_URL_CONTAINER ?= http://host.docker.internal:$(DATAHUB_MAPPED_GMS_PORT)
DATAHUB_KAFKA_BOOTSTRAP ?= 127.0.0.1:9092
DATAHUB_SCHEMA_REGISTRY_URL ?= $(DATAHUB_GMS_URL)/schema-registry/api/
DATAHUB_MCL_TOPIC ?= MetadataChangeLog_Versioned_v1
DATARESCUE_API_URL ?= http://127.0.0.1:8000
DATAHUB_MCP_HOST ?= 127.0.0.1
DATAHUB_MCP_PORT ?= 8001
DATAHUB_MCP_URL ?= http://$(DATAHUB_MCP_HOST):$(DATAHUB_MCP_PORT)/mcp
DATAHUB_CONTEXT := $(UV_RUN) --with acryl-datahub==$(DATAHUB_VERSION) -- \
	python scripts/seed_datahub_context.py
ifeq ($(strip $(DATAHUB_TOKEN)),)
DATAHUB_TOKEN := $(DATARESCUE_DATAHUB_TOKEN)
endif
ifeq ($(strip $(DATARESCUE_DATAHUB_TOKEN)),)
DATARESCUE_DATAHUB_TOKEN := $(DATAHUB_TOKEN)
endif
export DATAHUB_MAPPED_GMS_PORT DATAHUB_GMS_URL DATAHUB_GMS_URL_CONTAINER
export DATAHUB_TOKEN DATARESCUE_DATAHUB_TOKEN

POSTGRES_DB ?= datarescue
POSTGRES_USER ?= datarescue
POSTGRES_PASSWORD ?= datarescue
POSTGRES_HOST ?= 127.0.0.1
POSTGRES_PORT ?= 55432
export POSTGRES_DB POSTGRES_USER POSTGRES_PASSWORD POSTGRES_HOST POSTGRES_PORT

DBT_PROJECT_DIR := demo/dbt
DBT_CORE_VERSION ?= 1.9.8
DBT_POSTGRES_VERSION ?= 1.9.0
DBT := $(UV_RUN) --with dbt-core==$(DBT_CORE_VERSION) \
	--with dbt-postgres==$(DBT_POSTGRES_VERSION) -- dbt
DBT_FLAGS := --project-dir $(DBT_PROJECT_DIR) --profiles-dir $(DBT_PROJECT_DIR)
DBT_ARTIFACT_LOCK ?= $(CURDIR)/.data/dbt-artifact-publish.lock
DBT_ARTIFACT_SNAPSHOT_ROOT ?= $(CURDIR)/.data
PSQL := $(DEMO_RUNTIME) psql

API_CMD ?= $(UV_RUN) uvicorn apps.api.main:app --reload --host 0.0.0.0 --port 8000
WEB_CMD ?= $(NPM) --prefix apps/web run dev -- --host 0.0.0.0 --port 5173
DEMO_POSTGRES_DSN := postgresql://$(POSTGRES_USER):$(POSTGRES_PASSWORD)@$(POSTGRES_HOST):$(POSTGRES_PORT)/$(POSTGRES_DB)

.PHONY: help install bootstrap api-install web-install postgres-up postgres-down postgres-logs \
	demo-data-reset demo-drift demo-containment demo-data-verify dbt-debug dbt-build \
	dbt-build-healthy dbt-candidates-current dbt-candidates dbt-docs dbt-artifacts-current \
	dbt-artifacts-selected dbt-artifacts-healthy dbt-artifacts datahub-check datahub-ingest-postgres \
	datahub-ingest-dbt datahub-ingest datahub-up datahub-down datahub-seed-healthy \
	datahub-seed-context datahub-verify-context datahub-mcp-start datahub-mcp-stop \
	datahub-mcp-verify datahub-mcp-proof \
	datahub-apply-drift datahub-validate datahub-actions-validate datahub-actions-run \
	datahub-mcl-proof demo-ready dev demo demo-replay demo-connected test-demo test lint build check \
	clean demo-clean memory-init memory-sync memory-reindex memory-search memory-doctor memory-add

help: ## Show available commands.
	@awk 'BEGIN {FS = ":.*## "; printf "DataRescue commands:\n\n"} /^[a-zA-Z0-9_-]+:.*## / {printf "  %-25s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

bootstrap: api-install web-install ## Install API and web dependencies.

install: bootstrap ## Judge-friendly alias for installing all dependencies.

api-install:
	$(UV) sync --python $(PYTHON_VERSION) --all-extras --locked

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
	@set -euo pipefail; \
		target_path="$${DBT_TARGET_PATH:-}"; \
		managed_target=false; \
		if [[ -z "$$target_path" ]]; then \
			target_path="$$(mktemp -d "$${TMPDIR:-/tmp}/datarescue-dbt-build.XXXXXX")"; \
			managed_target=true; \
		else \
			mkdir -p "$$target_path"; \
		fi; \
		cleanup() { \
			if [[ "$$managed_target" == "true" ]]; then rm -rf -- "$$target_path"; fi; \
		}; \
		trap cleanup EXIT INT TERM; \
		POSTGRES_HOST=$(POSTGRES_HOST) POSTGRES_PORT=$(POSTGRES_PORT) \
			POSTGRES_DB=$(POSTGRES_DB) POSTGRES_USER=$(POSTGRES_USER) \
			POSTGRES_PASSWORD=$(POSTGRES_PASSWORD) \
			DATARESCUE_REVENUE_COLUMN=$${REVENUE_COLUMN:-net_amount} \
			DBT_SCHEMA=$${DBT_SCHEMA:-analytics} $(DBT) build $(DBT_FLAGS) \
			--target-path "$$target_path"

dbt-build-healthy: demo-data-reset ## Build and test the healthy amount model.
	REVENUE_COLUMN=amount DBT_SCHEMA=analytics $(MAKE) dbt-build

dbt-candidates-current: ## Validate both candidates against the currently drifted source.
	REVENUE_COLUMN=gross_amount DBT_SCHEMA=candidate_gross $(MAKE) dbt-build
	REVENUE_COLUMN=net_amount DBT_SCHEMA=candidate_net $(MAKE) dbt-build
	$(PSQL) -f - < demo/postgres/verify_candidates.sql

dbt-candidates: demo-drift ## Apply drift and validate both candidates.
	@$(MAKE) dbt-candidates-current

dbt-docs: postgres-up ## Generate dbt manifest and catalog for the selected mapping.
	@set -euo pipefail; \
		lock_runtime="$$(mktemp -d "$${TMPDIR:-/tmp}/datarescue-dbt-lock.XXXXXX")"; \
		owner_pid="$$$$"; \
		lock_pid=""; \
		docs_target=""; \
		publish_target=""; \
		cleanup() { \
			if [[ -n "$$lock_pid" ]]; then \
				kill -TERM "$$lock_pid" 2>/dev/null || true; \
				wait "$$lock_pid" 2>/dev/null || true; \
			fi; \
			[[ -z "$$docs_target" ]] || rm -rf -- "$$docs_target"; \
			[[ -z "$$publish_target" ]] || rm -rf -- "$$publish_target"; \
			rm -rf -- "$$lock_runtime"; \
		}; \
		trap cleanup EXIT INT TERM; \
		python3 scripts/dbt-artifact-lock.py --lock "$(DBT_ARTIFACT_LOCK)" \
			--ready "$$lock_runtime/ready" --owner-pid "$$owner_pid" & \
		lock_pid=$$!; \
		for _attempt in $$(seq 1 100); do \
			[[ -f "$$lock_runtime/ready" ]] && break; \
			if ! kill -0 "$$lock_pid" 2>/dev/null; then \
				wait "$$lock_pid" 2>/dev/null || true; \
				lock_pid=""; \
				echo "Unable to acquire the dbt artifact publication lock." >&2; \
				exit 1; \
			fi; \
			sleep 0.05; \
		done; \
		test -f "$$lock_runtime/ready" || { \
			echo "Timed out acquiring the dbt artifact publication lock." >&2; \
			exit 1; \
		}; \
		docs_target="$$(mktemp -d "$${TMPDIR:-/tmp}/datarescue-dbt-docs.XXXXXX")"; \
		publish_root="$${DBT_ARTIFACT_OUTPUT_DIR:-$(DBT_PROJECT_DIR)/target}"; \
		mkdir -p "$$publish_root"; \
		publish_target="$$(mktemp -d "$$publish_root/.publish-docs.XXXXXX")"; \
		POSTGRES_HOST=$(POSTGRES_HOST) POSTGRES_PORT=$(POSTGRES_PORT) \
			POSTGRES_DB=$(POSTGRES_DB) POSTGRES_USER=$(POSTGRES_USER) \
			POSTGRES_PASSWORD=$(POSTGRES_PASSWORD) \
			DATARESCUE_REVENUE_COLUMN=$${REVENUE_COLUMN:-net_amount} \
			DBT_SCHEMA=$${DBT_SCHEMA:-analytics} $(DBT) docs generate $(DBT_FLAGS) \
			--target-path "$$docs_target"; \
		test -s "$$docs_target/manifest.json"; \
		test -s "$$docs_target/catalog.json"; \
		cp "$$docs_target/manifest.json" "$$publish_target/manifest.json"; \
		cp "$$docs_target/catalog.json" "$$publish_target/catalog.json"; \
		mv -f "$$publish_target/manifest.json" "$$publish_root/manifest.json"; \
		mv -f "$$publish_target/catalog.json" "$$publish_root/catalog.json"

dbt-artifacts-current: ## Build selected net mapping and DataHub dbt artifacts.
	@REVENUE_COLUMN=net_amount DBT_SCHEMA=analytics $(MAKE) dbt-artifacts-selected

dbt-artifacts-healthy: demo-data-reset ## Build healthy amount artifacts for initial DataHub ingestion.
	@REVENUE_COLUMN=amount DBT_SCHEMA=analytics $(MAKE) dbt-artifacts-selected

dbt-artifacts-selected: postgres-up ## Build isolated artifacts and publish one internally consistent set.
	@set -euo pipefail; \
		lock_runtime="$$(mktemp -d "$${TMPDIR:-/tmp}/datarescue-dbt-lock.XXXXXX")"; \
		owner_pid="$$$$"; \
		lock_pid=""; \
		build_target=""; \
		docs_target=""; \
		publish_target=""; \
		cleanup() { \
			if [[ -n "$$lock_pid" ]]; then \
				kill -TERM "$$lock_pid" 2>/dev/null || true; \
				wait "$$lock_pid" 2>/dev/null || true; \
			fi; \
			[[ -z "$$build_target" ]] || rm -rf -- "$$build_target"; \
			[[ -z "$$docs_target" ]] || rm -rf -- "$$docs_target"; \
			[[ -z "$$publish_target" ]] || rm -rf -- "$$publish_target"; \
			rm -rf -- "$$lock_runtime"; \
		}; \
		trap cleanup EXIT INT TERM; \
		python3 scripts/dbt-artifact-lock.py --lock "$(DBT_ARTIFACT_LOCK)" \
			--ready "$$lock_runtime/ready" --owner-pid "$$owner_pid" & \
		lock_pid=$$!; \
		for _attempt in $$(seq 1 100); do \
			[[ -f "$$lock_runtime/ready" ]] && break; \
			if ! kill -0 "$$lock_pid" 2>/dev/null; then \
				wait "$$lock_pid" 2>/dev/null || true; \
				lock_pid=""; \
				echo "Unable to acquire the dbt artifact publication lock." >&2; \
				exit 1; \
			fi; \
			sleep 0.05; \
		done; \
		test -f "$$lock_runtime/ready" || { \
			echo "Timed out acquiring the dbt artifact publication lock." >&2; \
			exit 1; \
		}; \
		build_target="$$(mktemp -d "$${TMPDIR:-/tmp}/datarescue-dbt-artifact-build.XXXXXX")"; \
		docs_target="$$(mktemp -d "$${TMPDIR:-/tmp}/datarescue-dbt-artifact-docs.XXXXXX")"; \
		publish_root="$${DBT_ARTIFACT_OUTPUT_DIR:-$(DBT_PROJECT_DIR)/target}"; \
		mkdir -p "$$publish_root"; \
		publish_target="$$(mktemp -d "$$publish_root/.publish-artifacts.XXXXXX")"; \
		POSTGRES_HOST=$(POSTGRES_HOST) POSTGRES_PORT=$(POSTGRES_PORT) \
			POSTGRES_DB=$(POSTGRES_DB) POSTGRES_USER=$(POSTGRES_USER) \
			POSTGRES_PASSWORD=$(POSTGRES_PASSWORD) \
			DATARESCUE_REVENUE_COLUMN=$${REVENUE_COLUMN:-net_amount} \
			DBT_SCHEMA=$${DBT_SCHEMA:-analytics} $(DBT) build $(DBT_FLAGS) \
			--target-path "$$build_target"; \
		POSTGRES_HOST=$(POSTGRES_HOST) POSTGRES_PORT=$(POSTGRES_PORT) \
			POSTGRES_DB=$(POSTGRES_DB) POSTGRES_USER=$(POSTGRES_USER) \
			POSTGRES_PASSWORD=$(POSTGRES_PASSWORD) \
			DATARESCUE_REVENUE_COLUMN=$${REVENUE_COLUMN:-net_amount} \
			DBT_SCHEMA=$${DBT_SCHEMA:-analytics} $(DBT) docs generate $(DBT_FLAGS) \
			--target-path "$$docs_target"; \
		test -s "$$build_target/run_results.json"; \
		test -s "$$docs_target/manifest.json"; \
		test -s "$$docs_target/catalog.json"; \
		python3 -c 'import json, pathlib, sys; data=json.loads(pathlib.Path(sys.argv[1]).read_text()); assert data.get("args", {}).get("which") == "build", "run_results.json is not from dbt build"' "$$build_target/run_results.json"; \
		cp "$$build_target/run_results.json" "$$publish_target/run_results.json"; \
		cp "$$docs_target/manifest.json" "$$publish_target/manifest.json"; \
		cp "$$docs_target/catalog.json" "$$publish_target/catalog.json"; \
		mv -f "$$publish_target/run_results.json" "$$publish_root/run_results.json"; \
		mv -f "$$publish_target/manifest.json" "$$publish_root/manifest.json"; \
		mv -f "$$publish_target/catalog.json" "$$publish_root/catalog.json"

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
	DATAHUB_MAPPED_GMS_PORT=$(DATAHUB_MAPPED_GMS_PORT) \
		$(DATAHUB_CLI) docker quickstart --version $(DATAHUB_RELEASE) --dump-logs-on-failure
	@$(MAKE) datahub-check

datahub-down: ## Stop the supported DataHub Quickstart stack.
	$(DATAHUB_CLI) docker quickstart --version $(DATAHUB_RELEASE) --stop

datahub-ingest-postgres: postgres-up datahub-check ## Ingest PostgreSQL metadata into DataHub.
	$(DEMO_RUNTIME) datahub-ingest-postgres

datahub-ingest-dbt: postgres-up datahub-check ## Build and ingest one immutable dbt artifact snapshot.
	@set -euo pipefail; \
		mkdir -p "$(DBT_ARTIFACT_SNAPSHOT_ROOT)"; \
		snapshot="$$(mktemp -d "$(DBT_ARTIFACT_SNAPSHOT_ROOT)/dbt-ingest.XXXXXX")"; \
		cleanup() { rm -rf -- "$$snapshot"; }; \
		trap cleanup EXIT INT TERM; \
		REVENUE_COLUMN="$${REVENUE_COLUMN:-net_amount}" \
			DBT_SCHEMA="$${DBT_SCHEMA:-analytics}" \
			DBT_ARTIFACT_OUTPUT_DIR="$$snapshot" \
			$(MAKE) dbt-artifacts-selected; \
		DBT_ARTIFACT_ROOT_HOST="$$snapshot" $(DEMO_RUNTIME) datahub-ingest-dbt

datahub-ingest: demo-drift datahub-check ## Apply drift, then ingest PostgreSQL and immutable dbt artifacts.
	$(DEMO_RUNTIME) datahub-ingest-postgres
	@REVENUE_COLUMN=net_amount DBT_SCHEMA=analytics $(MAKE) datahub-ingest-dbt

datahub-seed-context: datahub-check ## Idempotently seed glossary, owner, document, and verify dbt lineage.
	@DATAHUB_GMS_URL="$(DATAHUB_GMS_URL)" $(DATAHUB_CONTEXT) seed

datahub-verify-context: datahub-check ## Fail closed unless exact connected semantic context exists.
	@DATAHUB_GMS_URL="$(DATAHUB_GMS_URL)" $(DATAHUB_CONTEXT) verify

datahub-mcp-start: datahub-check ## Start the pinned official DataHub MCP HTTP server.
	@DATAHUB_GMS_URL="$(DATAHUB_GMS_URL)" DATAHUB_MCP_HOST="$(DATAHUB_MCP_HOST)" \
		DATAHUB_MCP_PORT="$(DATAHUB_MCP_PORT)" bash scripts/datahub-mcp.sh start

datahub-mcp-stop: ## Stop the MCP process owned by the repository launcher.
	@DATAHUB_MCP_HOST="$(DATAHUB_MCP_HOST)" DATAHUB_MCP_PORT="$(DATAHUB_MCP_PORT)" \
		bash scripts/datahub-mcp.sh stop

datahub-mcp-verify: datahub-verify-context ## Verify a running official MCP server and live context calls.
	@DATAHUB_MCP_HOST="$(DATAHUB_MCP_HOST)" DATAHUB_MCP_PORT="$(DATAHUB_MCP_PORT)" \
		bash scripts/datahub-mcp.sh verify

datahub-mcp-proof: api-install ## Prove official MCP context reads and real DataHub write-back.
	@$(MAKE) datahub-up
	@$(MAKE) datahub-seed-healthy
	@set -euo pipefail; \
		started_here=false; \
		if ! DATAHUB_MCP_HOST="$(DATAHUB_MCP_HOST)" DATAHUB_MCP_PORT="$(DATAHUB_MCP_PORT)" \
			bash scripts/datahub-mcp.sh status >/dev/null 2>&1; then \
			DATAHUB_GMS_URL="$(DATAHUB_GMS_URL)" DATAHUB_MCP_HOST="$(DATAHUB_MCP_HOST)" \
				DATAHUB_MCP_PORT="$(DATAHUB_MCP_PORT)" bash scripts/datahub-mcp.sh start; \
			started_here=true; \
		fi; \
		cleanup() { \
			if [[ "$$started_here" == "true" ]]; then \
				DATAHUB_MCP_HOST="$(DATAHUB_MCP_HOST)" DATAHUB_MCP_PORT="$(DATAHUB_MCP_PORT)" \
					bash scripts/datahub-mcp.sh stop >/dev/null 2>&1 || true; \
			fi; \
		}; \
		trap cleanup EXIT INT TERM; \
		DATAHUB_MCP_HOST="$(DATAHUB_MCP_HOST)" DATAHUB_MCP_PORT="$(DATAHUB_MCP_PORT)" \
			bash scripts/datahub-mcp.sh verify; \
		DATARESCUE_DATAHUB_MCP_URL="$(DATAHUB_MCP_URL)" \
			$(UV_RUN) python scripts/verify-datahub-mcp-proof.py

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

datahub-seed-healthy: demo-data-reset datahub-check ## Ingest the healthy schema as MCL baseline.
	$(DEMO_RUNTIME) datahub-ingest-postgres
	@REVENUE_COLUMN=amount DBT_SCHEMA=analytics $(MAKE) datahub-ingest-dbt
	@$(MAKE) datahub-seed-context

datahub-apply-drift: demo-drift datahub-check ## Emit real drift MCL, validate candidates and ingest selected dbt state.
	$(DEMO_RUNTIME) datahub-ingest-postgres
	@$(MAKE) dbt-candidates-current
	@REVENUE_COLUMN=net_amount DBT_SCHEMA=analytics $(MAKE) datahub-ingest-dbt

datahub-mcl-proof: api-install ## Prove automatic MCL intake and live GraphQL incident creation without cloud credentials.
	@bash scripts/datahub-mcl-proof.sh

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
	DATARESCUE_POSTGRES_DSN=$(DEMO_POSTGRES_DSN) $(UV_RUN) python scripts/verify-live-workflow.py

test: ## Run API unit tests, web tests and demo integration tests.
	$(UV_RUN) pytest
	$(NPM) --prefix apps/web test
	$(MAKE) test-demo

lint: ## Run Python and TypeScript static checks.
	$(UV_RUN) ruff check .
	$(UV_RUN) mypy apps packages
	$(NPM) --prefix apps/web run build

build: ## Build the production web bundle.
	$(NPM) --prefix apps/web run build

check: lint test build ## Run static checks, tests, integrations, and production build.

clean: ## Remove dbt build artifacts only.
	$(DBT) clean $(DBT_FLAGS)

demo-clean: ## Delete demo containers and the PostgreSQL volume.
	@echo "Deleting the DataRescue demo database volume."
	$(DEMO_RUNTIME) clean

memory-init: ## Initialize and index repo-scoped semantic memory.
	$(UV_RUN) python scripts/repo_memory.py init

memory-sync: ## Incrementally sync curated memory Markdown into the local cache.
	$(UV_RUN) python scripts/repo_memory.py sync

memory-reindex: ## Rebuild this repository's local memory namespace from Markdown.
	$(UV_RUN) python scripts/repo_memory.py reindex

memory-search: ## Search repo memory (usage: make memory-search QUERY='question').
	@test -n "$(QUERY)" || { echo "QUERY is required." >&2; exit 2; }
	$(UV_RUN) python scripts/repo_memory.py search "$(QUERY)"

memory-doctor: ## Check memory sources, cache, manifest, and stale references.
	$(UV_RUN) python scripts/repo_memory.py doctor

memory-add: ## Add a curated note; requires CATEGORY, SLUG, TITLE, and SUMMARY.
	@test -n "$(CATEGORY)" -a -n "$(SLUG)" -a -n "$(TITLE)" -a -n "$(SUMMARY)" || { \
		echo "CATEGORY, SLUG, TITLE, and SUMMARY are required." >&2; exit 2; \
	}
	$(UV_RUN) python scripts/repo_memory.py add --category "$(CATEGORY)" --slug "$(SLUG)" \
		--title "$(TITLE)" --summary "$(SUMMARY)" --tags "$(TAGS)" \
		--related-files "$(RELATED_FILES)"
