# DataRescue repo semantic memory

This directory is the durable, reviewable source of truth for DataRescue project memory. It preserves high-signal decisions, architecture, bug causes, gotchas, file responsibilities, and external API notes across Codex sessions. It does not make a context window infinite, replace current-code inspection, or turn chronological logs into trusted truth.

## Rules

- Search this memory before architecture, debugging, workflow, API, or data-model work.
- Inspect the current repository after retrieval; current code wins if it conflicts with a note.
- Keep session history in `docs/handoff.md`; curate only durable lessons here.
- Never store credentials, tokens, `.env` contents, raw production logs, user data, build output, dependencies, or generated binaries.
- Mark stale knowledge `needs-review`; do not silently delete it.

## Categories

- `architecture/`: system boundaries and structural choices
- `decisions/`: durable product and implementation decisions
- `bugfixes/`: root causes and non-obvious fixes
- `gotchas/`: pitfalls and do-not-repeat lessons
- `file-map/`: module/file ownership
- `api-notes/`: external and internal API contracts

Each category contains an `_template.md` file. Templates are not indexed.

## Commands

```bash
make memory-init
make memory-sync
make memory-search QUERY='When is DataRescue complete?'
make memory-doctor
make memory-reindex
```

Create a note:

```bash
uv run python scripts/repo_memory.py add \
  --category decisions \
  --slug example-decision \
  --title 'Example decision' \
  --summary 'One durable sentence.' \
  --tags 'example,decision' \
  --related-files 'README.md'
```

The Markdown files are authoritative. The rebuildable local cache is stored outside the repository at `~/.codex-memory/repo_memory.sqlite3`, namespaced by repository path and remote. Its source manifest is stored under `~/.codex-memory/manifests/`. The local embedding is deterministic and offline; no repository content is sent to an online vector service.
