---
title: "Wait past the PostgreSQL entrypoint initialization server"
date: 2026-07-22
status: active
tags: bugfix,postgres,docker,ci,readiness
related_files: scripts/demo-runtime.sh,.github/workflows/ci.yml,demo/postgres/init/00-bootstrap.sql
---

# Wait past the PostgreSQL entrypoint initialization server

## Symptom

On a fresh GitHub runner, `postgres-up` printed `PostgreSQL is ready`, but the immediately following `psql` command failed because no server socket was available. The same CI job passed when its demo volume had already been initialized.

## Root cause

The official PostgreSQL image starts a temporary server while it executes `/docker-entrypoint-initdb.d`. A plain `pg_isready` can succeed against that temporary server just before the entrypoint stops it and starts the final server. The next command can land in the restart gap.

## Fix and regression proof

`postgres_wait` must first observe the entrypoint's initialization-complete or initialization-skipped marker, then require `pg_isready` and a query proving `audit.payments_fct_last_good` exists. A fresh GitHub-hosted `postgres-dbt-proof` run is the regression gate.

## Do not repeat

Do not treat one successful readiness probe during container bootstrap as proof that the final database process and initialized fixture are ready.
