# Repository agent instructions

<!-- repo-semantic-memory:start -->
## Repo Semantic Memory

- Before architecture, debugging, refactoring, API, workflow, or data-model work, run `make memory-search QUERY='<task intent>'` and inspect the most relevant notes.
- After retrieval, inspect current code and live state. Current code is authoritative when it conflicts with memory; mark the note `needs-review` rather than trusting it silently.
- Add or update curated notes after durable decisions, verified bug root causes, recurring gotchas, file responsibility changes, or external API assumptions.
- Keep chronological session updates in `docs/handoff.md`; do not index raw transcripts or logs as semantic memory.
- Never store credentials, tokens, `.env` content, private keys, passwords, raw production logs, user data, dependencies, generated artifacts, or binaries in memory.
- Keep all memory repo-scoped. The Markdown under `memory/` is authoritative; the local SQLite vector cache is rebuildable with `make memory-reindex`.
<!-- repo-semantic-memory:end -->
