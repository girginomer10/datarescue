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
