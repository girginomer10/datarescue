---
title: "DataRescue hackathon definition of done and resume order"
date: 2026-07-22
status: active
tags: completion,tamamlama,bitmiş,devam-et,datahub,mcl,mcp,graphql,openai,github,dbt,demo
related_files: README.md,Makefile,scripts/demo-connected.sh,packages/datahub/actions.py,packages/datahub/mcp.py,packages/datahub/graphql.py,apps/api/workflow.py,docs/demo-script.md
---

# DataRescue hackathon definition of done and resume order

## Current honest status

DataRescue is a published hackathon MVP, not a fully connected finished product. Real PostgreSQL/dbt validation, deterministic policy, containment, CI, Pages replay, and an application-generated draft GitHub PR have been proven. The hosted UI is explicitly recorded evidence. Live DataHub Core/Kafka/MCP/GraphQL and live OpenAI candidate generation have not been proven end to end.

This note answers exactly when DataRescue is completely finished and where to resume when the user says `devam et`: start with the live DataHub MCL-to-incident gap and follow the ordered gates below.

## Exact finish line

DataRescue is complete for the hackathon only when all of these pass:

1. A clean clone can run the promised one-command `make demo` flow, starting PostgreSQL, DataHub Core v1.6, Kafka, Schema Registry, API, worker, web, fixtures, and ingestion without undocumented manual steps.
2. Real DataHub contains the healthy baseline, canonical dataset URN, `NetRevenue` glossary term, Finance Data owner, documentation, and current source-to-dbt lineage.
3. The real DataHub Actions MCL consumer detects `amount → gross_amount + net_amount` automatically; no manual schema-change POST is used, duplicate MCLs do not create a second case, and deduplication survives restart.
4. Live DataHub MCP supplies schema, lineage, glossary, owner, and docs. Live OpenAI Responses structured output proposes only candidate mappings and evidence references. Deterministic policy remains the sole decision authority.
5. DataHub GraphQL opens a real incident and evidence write-back succeeds. Isolated PostgreSQL/dbt validation rejects `gross_amount` at `+3.40%` and selects `net_amount` at `0.00%`, `100%` PK overlap, and `8/8` tests.
6. A real draft PR opens while the real incident remains active. PR creation is never called recovery.
7. Only after explicit human merge, DataRescue checks out the exact merge SHA, reruns dbt and reconciliation, clears the degraded state, resolves the real DataHub incident, and transitions the case to `RESOLVED`.
8. The connected fail-closed fixture creates no PR, leaves the incident active, and blocks the downstream command with exit `75`.
9. Hosted replay artifacts and their SHA-256 manifest are regenerated from the genuine connected run. A real 2:50 demo video and final Devpost submission are produced from that evidence.
10. External failure, restart/idempotency, prompt and SQL injection, path/worktree safety, secret redaction, desktop/mobile browser behavior, and accessibility checks pass without fake success states.

## Resume order

When the user says `devam et`, start with live DataHub infrastructure and the automatic MCL-to-real-incident path. Then prove live MCP/GraphQL/OpenAI, the connected safe-repair run, human merge plus merge-SHA closure, connected containment, fresh-clone `make demo`, genuine replay, and finally the video/Devpost package.

Do not expand the UI or add Slack, PagerDuty, Snowflake, BigQuery, Redis/Celery, or a general multi-agent framework before this connected vertical slice passes.

## Required user-controlled inputs

- A working Docker Compose v2 environment capable of running DataHub Quickstart.
- DataHub and MCP endpoints/tokens plus an OpenAI API key supplied through secrets, never committed.
- Explicit human authorization before merging the draft recovery PR.

## Completion claim boundary

Never say the product is fully finished merely because the replay UI, local fixtures, adapters, or draft PR work. The completion claim requires a fresh-clone full-stack run, automatic real MCL detection, real MCP/GraphQL operations, human merge with exact-SHA post-deploy verification, connected containment, genuine replay, and the recorded demo.
