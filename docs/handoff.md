# Agent handoff

## 2026-07-22 - Codex

- Task: Built the DataRescue hackathon product from an empty repository through the evidence-gated vertical slice.
- Changed: FastAPI case engine and guard, deterministic policy and evidence packages, PostgreSQL/dbt fixtures, DataHub MCL/MCP/GraphQL adapters, OpenAI structured candidate generation, git/draft-PR integration, React Forensic Console, replay evidence, CI, Pages workflow, and submission docs.
- Verified: 23 Python tests, 13 web tests, replay manifest hashes, Python lint/types, production and Pages builds, DataHub Actions recipe validation, and live PostgreSQL/dbt safe plus fail-closed workflows. Re-run the final gate before publishing because this note was written immediately before release.
- Memory: None; no repo semantic-memory system is installed. This chronological note is the handoff source.
- Next: Publish the public repository and use the app to open the real draft PR. Do not merge it. Full DataHub Core/Kafka/MCP/GraphQL execution remains a connected-environment gate and must be shown as `NOT_RUN` until its services and credentials are present.

## 2026-07-22 - Codex (release follow-up)

- Task: Fixed the first GitHub-hosted CI bootstrap failure after publishing `main`.
- Changed: Pinned `astral-sh/setup-uv` to the real `v9.0.0` release because the upstream repository does not publish a floating `v9` ref.
- Verified: GitHub reported the missing `v9` ref in both CI jobs; the `v9.0.0` release and action definition were checked through the GitHub API. Await the replacement CI run before treating the release as green.
- Memory: None; the exact-action-tag requirement is recorded here as a durable release gotcha.
- Next: Push the CI fix, wait for CI and Pages, then create and verify the application-generated draft PR without merging it.

## 2026-07-22 - Codex (published proof)

- Task: Completed public release and exercised the real GitHub draft-PR path through the running DataRescue application.
- Changed: Published `girginomer10/datarescue`, enabled GitHub Pages with Actions, and added the hosted replay plus draft-PR evidence links to the README. The application created branch `datarescue/dr-996c48f0` and draft PR #1 with only the allowlisted dbt model change.
- Verified: `main` CI and Pages passed; the hosted replay returned HTTP 200 and rendered `RECORDED_REPLAY EVIDENCE`; PR #1 is open/draft, changes one file, and passed `quality` plus `postgres-dbt-proof`. Case `DR-996C48F0` remained `PR_OPEN` with an active local incident after PR creation.
- Memory: None; release evidence is captured in this handoff and the external GitHub checks.
- Next: Do not merge PR #1 automatically. A human merge must be followed by `verify-deployment`. Full DataHub Core/Kafka/MCP/GraphQL execution is still `NOT_RUN` because that connected stack and its credentials were unavailable on this machine.

## 2026-07-22 - Codex (persistent completion checkpoint)

- Task: Persisted the exact remaining definition of done so a later `devam et` resumes from the live DataHub gap instead of treating the MVP as complete.
- Changed: Added repo-scoped semantic-memory sources, offline SQLite indexing/search/doctor tooling, future-agent instructions, and `memory/decisions/datarescue-hackathon-definition-of-done.md`.
- Verified: Memory source security scan, incremental index, completion-query retrieval, stale-reference doctor, Python lint, and repository diff checks.
- Memory: The active completion note is the authoritative resume order; the local vector cache is rebuildable and remains outside Git.
- Next: Start with Docker Compose v2 plus the real DataHub MCL-to-incident path, then live MCP/GraphQL/OpenAI, merge-SHA closure, connected containment, fresh-clone demo, genuine replay, video, and Devpost.

## 2026-07-22 - Codex (fresh-run PostgreSQL readiness fix)

- Task: Fixed the GitHub-hosted dbt proof race exposed by the memory-checkpoint push.
- Changed: `scripts/demo-runtime.sh` now waits past PostgreSQL's temporary entrypoint initialization server and proves the initialized audit fixture exists before declaring readiness. Added a durable bugfix memory note.
- Verified: Shell syntax, Ruff, local `make test-demo`, memory sync, and memory doctor passed. The replacement fresh GitHub runner remains the final regression proof.
- Memory: Added `memory/bugfixes/postgres-entrypoint-readiness-race.md`; do not replace final-server readiness with a single `pg_isready` probe.
- Next: Push the readiness fix and require both GitHub `quality` and `postgres-dbt-proof` to pass.
