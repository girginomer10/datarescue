---
title: "Prove identity, provenance, and ownership at retry boundaries"
date: 2026-07-22
status: active
tags: idempotency,event-id,case-id,evidence-provenance,github,retry,postgres,dbt,python
related_files: apps/api/store.py,apps/api/workflow.py,packages/evidence/executor.py,packages/datahub/actions.py,packages/remediation/github.py,Makefile
---

# Prove identity, provenance, and ownership at retry boundaries

## Summary

Do not infer recovery safety from a mode string, an identifier alone, or the
existence of a branch. Post-deploy closure, MCL redelivery, case allocation,
candidate-schema naming, and GitHub retries each need an explicit proof of what
the observed state represents before DataRescue can reuse it or report success.

## Durable invariants

- `execution_mode=postgres` is not evidence provenance. Only an executor that
  explicitly declares live evidence may perform post-deploy recomputation; the
  default PostgreSQL executor also requires a non-empty DSN at startup.
- An `event_id` is idempotent only when the normalized asset, schema, source,
  event type, state, dedup key, and payload match. Reuse with different content
  is a `409 Conflict`, not a duplicate success.
- Keep the normal eight-hex case ID for operator readability, but allocate a
  longer deterministic digest prefix when that ID is already owned by another
  drift. The event store must reject a second detection event in one case even
  while the first case is still in `DETECTED`.
- PostgreSQL silently truncates identifiers to 63 bytes. Long candidate-schema
  names must retain a source hint and a digest of the full identity; blind
  truncation can make gross and net candidates share a schema and overwrite
  evidence artifacts.
- GitHub retry state is reusable only after the resolved origin matches the
  repository allowlist and the PR/remote head proves the expected case, patch
  hash, exact single-file content, base ancestry, owner, repository, branch,
  draft state, and URL. Reconcile again after `gh pr create`: a successful URL
  response is not proof that the allowed origin branch still points at the PR
  head. Never delete an unknown local branch/worktree; an interrupted
  `worktree add` may be cleaned only when path, branch, and starting commit all
  prove it belongs to the failed attempt.
- Live post-deploy closure fetches the allowlisted origin base, canonicalizes
  the requested commit to a full object ID, requires that commit in the fetched
  base history, and compares the merged model byte-for-byte plus SHA-256 with
  the stored patch. dbt then runs from a detached worktree at that exact commit,
  with fresh successful `DATAHUB_MCP` context. Current checkout contents and
  caller-supplied metrics are not evidence.
- Cleanup is best-effort after an external success or a completed verification.
  A worktree or temporary-directory cleanup error must not rewrite a proven PR
  or verification result.
- Cached browser evidence becomes captured/stale as soon as a refresh fails or
  the case disappears after reset. Never retain `CURRENT`, live-integration, or
  live-policy language for a snapshot whose freshness is no longer proven.
- The project environment and every `uv run`/dbt path use Python 3.11. Without a
  shared pin, a fresh bootstrap can create a newer-version environment that dbt
  later replaces, leaving the final workflow without its installed tools.

## Regression proof

The control-plane, GitHub retry, toolchain, and long candidate-schema tests
cover these boundaries. `make test-demo` additionally proves two distinct live
PostgreSQL/dbt evidence directories, gross `+3.40%` rejection, net `0.00%`
selection, `8/8` tests, and candidate-schema cleanup.
