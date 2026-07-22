---
title: "Prove the connected DataHub v1.6 contract end to end"
date: 2026-07-23
status: active
tags: datahub,mcl,mcp,lineage,kafka,dbt,idempotency,evidence,incident,git,provenance
related_files: Makefile,demo/datahub/postgres-ingestion.yml,demo/datahub/dbt-ingestion.yml,docker-compose.yml,scripts/demo-runtime.sh,scripts/datahub-actions.sh,scripts/datahub-mcl-proof.sh,scripts/datahub-mcp.sh,scripts/seed_datahub_context.py,scripts/verify-connected-case.py,apps/api/cli.py,apps/api/store.py,apps/api/workflow.py,packages/datahub/mcp.py,packages/datahub/graphql.py,packages/evidence/executor.py,packages/remediation/github.py
---

# Prove the connected DataHub v1.6 contract end to end

## Summary

The connected path is safe only when catalog identity, event ownership,
semantic context, lineage topology, dbt artifacts, and evidence persistence are
all verified from the live system. A healthy endpoint, a successful mutation
response, or the presence of expected URNs is not enough.

## Durable invariants

- DataHub Core v1.6 is mapped through GMS on host port `18080`; its internal
  Schema Registry is reached through the GMS `/schema-registry/api/` proxy.
  Keep PostgreSQL and dbt ingestion on the canonical dataset URN without a
  platform instance.
- dbt ingestion must consume the `run_results.json` produced by `dbt build`.
  Generate docs in an isolated target and publish only `manifest.json` and
  `catalog.json`. Build one unique complete artifact snapshot, keep it unchanged
  for the entire ingestion process, and mount it read-only; a serialized copy
  into a shared mutable target does not provide the same provenance guarantee.
- Seed semantic metadata without replacing unrelated properties or attachment
  fields. The verified lineage contract contains four directed edges: physical
  source to dbt source, dbt source to dbt staging, dbt staging to materialized
  PostgreSQL staging, and materialized staging to the dbt mart. Official dbt
  ingestion also emits one native physical source to materialized staging edge;
  allow exactly that parallel edge, not arbitrary shortcuts or star topology.
- The official MCP v0.6 context is composite: `get_entities`,
  `list_schema_fields`, and `get_lineage`. A canonical context is current only
  when the directed topology, deterministic context document, Finance Data
  owner, complete schema, and semantic definition are all present.
- A successful `save_document` response is not persistence proof. MCP document
  and asset reads can also be truncated or omit aspects in DataHub v1.6. Read
  the exact document through GMS `documentInfo` and verify its title, content,
  and related asset before recording evidence write-back as successful.
- GraphQL incident mutations require independent state proof. Read the created
  incident through GMS `incidentInfo` and require the exact linked asset, title,
  and remote `ACTIVE` state; after resolution require remote `RESOLVED`. Never
  infer remote state from mutation success or a local default.
- Never forward a DataHub GMS credential to an arbitrary MCP URL. MCP
  authentication is a separate setting; externally managed endpoints require
  transport protection, while the repository-managed mutation server remains
  loopback-only and bound to a verified startup configuration.
- A Kafka proof owns a unique consumer group, captures the post-baseline offsets
  for every partition, and replays the same range after an API restart. The API
  process and state database must be proof-owned so an unrelated service cannot
  satisfy health checks or fake durable deduplication.
- Connected launchers must fail closed when semantic context is incomplete and
  must not let concurrent dbt invocations share mutable target artifacts.
- Reconcile the exact `<candidate_schema>.fct_revenue` relation created and
  tested by dbt, not a separately rendered candidate query. Do not trust process
  exit code alone: `run_results.json` must be present and valid, describe a
  `dbt build`, include both expected models and the full test set, and report
  every required node as successful.
- Parse one explicit PostgreSQL URL and derive both dbt's environment and
  psycopg reconciliation from it. Override inherited `POSTGRES_*` values, and
  reject ambiguous multi-host, keyword/service, incomplete, duplicate-option,
  or unsupported-TLS forms. Never include the DSN or password in errors or
  evidence.
- Treat durable workflow state as the recovery checkpoint. A redelivered MCL
  event must resume safely from `DETECTED`, `CONTEXT_GATHERED`, or
  `PATCH_READY`; per-process active/settled sets are only duplicate suppression,
  not the source of truth. Downstream execution is allowed only when no
  unresolved case exists; `FAILED` and intermediate states remain blocked with
  exit code `75` just like `CONTAINED`.
- Post-deploy verification must fetch the configured `origin/<base>` itself and
  require the caller's merge SHA to equal that exact current tip. It must also
  prove the merged file byte-for-byte matches the validated patch before fresh
  dbt and reconciliation evidence can resolve the incident.

## Open provenance gap at the 2026-07-23 pause

Initial candidate validation still runs from the mutable local checkout, while
draft-PR creation fetches `origin/<base>` later. Before the connected workflow
can be called complete, fetch and persist one base SHA before validation, run
candidate dbt/reconciliation in a detached isolated worktree at that SHA, and
require PR creation to use the same SHA or fail if the remote base moved. Add
regressions for a dirty/divergent local checkout and for a base-branch movement
between validation and PR creation. Do not confuse the stricter post-deploy
tip check with closure of this earlier validation-to-PR race.

## Regression proof

`make datahub-mcl-proof` proves automatic Kafka MCL intake, a live incident
mutation with remote `ACTIVE` `incidentInfo` read-back, fail-closed containment,
and restart-safe deduplication.
`make datahub-mcp-proof` proves official MCP reads and verified DataHub document
write-back. These bounded proofs do not claim that OpenAI proposals, a draft PR,
human merge, or exact-SHA post-deploy closure ran.

When all external credentials are configured, `make demo-connected` adds a
bounded case-level proof and must fail unless one proof-owned MCL case reaches
`PR_OPEN` with verified context, candidate evidence, write-back, draft-PR URL,
and remote `ACTIVE` incident state. That still does not prove human merge,
deployment, or exact-SHA incident resolution; those remain separate authorized
gates.
