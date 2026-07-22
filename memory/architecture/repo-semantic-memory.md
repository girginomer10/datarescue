---
title: "How DataRescue repo memory works"
date: 2026-07-22
status: active
tags: memory,agents,workflow
related_files: AGENTS.md,scripts/repo_memory.py,memory/README.md
---

# How DataRescue repo memory works

## Summary

Curated Markdown under `memory/` is the repository-scoped source of truth. `scripts/repo_memory.py` builds a local, offline SQLite vector cache in a repo-specific namespace so later Codex sessions can retrieve decisions and gotchas.

## Required agent behavior

Search memory before architecture, debugging, workflow, API, and data-model work, then inspect the current code. Current code overrides stale notes. Keep secrets, raw logs, dependencies, and build output out of memory.

## Rebuildability

The local cache can be deleted and recreated with `make memory-reindex`; no durable knowledge exists only in the cache.
