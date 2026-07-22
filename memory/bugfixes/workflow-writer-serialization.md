---
title: "All event-stream writers must hold the worker lock"
date: 2026-07-22
status: active
tags: concurrency,worker-lock,reset,verify-deployment,event-sourcing,idempotency,race-condition
related_files: apps/api/workflow.py,apps/api/store.py,apps/api/main.py,tests/test_backend_units.py
---

# All event-stream writers must hold the worker lock

## Summary

`WorkflowService._worker_lock` is the single-writer guard for the append-only
event store. Any operation that appends to a case's stream — full ingest, demo
reset, and post-deploy `verify_deployment` — must run under it. Originally only
`_advance` held the lock while `ingest`'s initial `SCHEMA_CHANGE_DETECTED`
append, `store.reset`, and `verify_deployment` ran outside it.

## Root cause and failure

`ingest` appended the detection event, then acquired the lock for `_advance`.
A concurrent `store.reset()` (which took only the store's own `RLock`) could
append `SYSTEM_RESET` between those two steps, moving the reset scope forward.
The later `_advance` appends then landed in the new scope while the detection
event stayed in the old one. `project_case` reads events in the current scope
only and requires the first event to be `SCHEMA_CHANGE_DETECTED`; with the root
event stranded in a prior scope, both `get_case` and `list_cases` raised
`ValueError` — a permanent HTTP 500 until another reset hid the case entirely.
Separately, two concurrent `verify_deployment` calls could both pass the
`PR_OPEN` guard and double-record a deployment / double-resolve the incident,
and in postgres mode run dbt/Postgres/git concurrently against shared state.

## Fix and invariant

Wrap the entire `ingest` flow (dedup check, initial append, `_advance`) in
`with self._worker_lock`, add `WorkflowService.reset()` and
`verify_deployment` bodies under the same lock, and route `POST /demo/reset`
through `WorkflowService.reset`. Invariant to preserve: never append to the
event store from a code path that does not hold `_worker_lock`; a reset must
only ever land between two complete ingests. `store.append` also now treats an
`(event_id, reset_scope)` unique-index collision as a `DuplicateEventError`
rather than re-raising a raw `sqlite3.IntegrityError`.
